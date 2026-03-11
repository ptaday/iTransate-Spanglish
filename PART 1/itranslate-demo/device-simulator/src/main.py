# Device simulator entry point — orchestrates all subsystems.
#
# DeviceSimulator wires together:
#   - Audio capture (MicrophoneSource)
#   - Streaming client (AssemblyAIStreamingClient) with reconnection
#   - Transcript handler (display + state + file export)
#   - Prompting config (keyterm boosting)
#
# Execution flow:
#   main() parses CLI args → builds configs → creates DeviceSimulator
#   DeviceSimulator.run() → connect WebSocket → stream_audio()
#   Ctrl+C / SIGTERM → stop() → disconnect → print session summary
#
# Two connection modes:
#   Direct (default) — device sends the AssemblyAI API key in the Authorization
#     header. Simple, no extra service needed.
#   Token service (--token-service) — device authenticates with the token service,
#     which issues a short-lived AssemblyAI token. The master API key never leaves
#     the server. Use this for production deployments.

import asyncio
from pathlib import Path
from .config import settings
from .audio.capture import create_audio_source, AudioSource
from .audio.buffer import AudioBuffer, OfflineRecorder
from .streaming.client import (
    AssemblyAIStreamingClient,
    SessionConfig,
    TranscriptMessage,
    BufferConfig,
)
from .streaming.reconnect import ReconnectConfig, ConnectionState
from .streaming.errors import StreamingError
from .transcription.handler import TranscriptHandler, TranscriptSegment
from .prompts.modes import (
    PromptConfig,
    PromptMode,
    create_prompt_config,
)


class DeviceSimulator:
    def __init__(
        self,
        reconnect_config: ReconnectConfig | None = None,
        buffer_config: BufferConfig | None = None,
        enable_diarization: bool = False,
        use_token_service: bool = False,
        speaker_names: list[str] | None = None,
        output_path: str | None = None,
        session_duration: int = 3600,
    ):
        self.reconnect_config = reconnect_config or ReconnectConfig()
        self.buffer_config = buffer_config or BufferConfig()
        self.enable_diarization = enable_diarization
        self.output_path = output_path

        self.client = AssemblyAIStreamingClient(
            token_service_url=settings.token_service_url,
            device_api_key=settings.device_api_key,
            device_id=settings.device_id,
            reconnect_config=self.reconnect_config,
            buffer_config=self.buffer_config,
            use_token_service=use_token_service,
            api_key=settings.assemblyai_api_key,
            session_duration=session_duration,
        )
        self.handler = TranscriptHandler(
            on_final_transcript=self._on_final_transcript,
            display_partials=True,
            show_speaker_labels=enable_diarization,
            speaker_names=speaker_names or [],
        )
        self.audio_source: AudioSource | None = None
        self.prompt_config: PromptConfig | None = None
        self._running = False
        self._errors: list[StreamingError] = []
        self._saved_audio_files: list[str] = []

    async def _on_final_transcript(self, text: str, segment: TranscriptSegment) -> None:
        if segment.speaker:
            print(f"\n[Pipeline] Ready for translation [{segment.speaker}]: \"{text}\"")
        else:
            print(f"\n[Pipeline] Ready for translation: \"{text}\"")

    async def _handle_transcript(self, message: TranscriptMessage) -> None:
        await self.handler.handle_transcript(message)

    async def _handle_error(self, error: StreamingError) -> None:
        self._errors.append(error)
        print(f"\n[Error] {error}")

    async def _handle_state_change(
        self,
        old_state: ConnectionState,
        new_state: ConnectionState,
    ) -> None:
        if new_state == ConnectionState.RECONNECTING:
            print("\n[Device] Connection lost, buffering audio...")
        elif new_state == ConnectionState.CONNECTED and old_state == ConnectionState.RECONNECTING:
            stats = self.client.buffer_stats
            if stats.get("lost_chunks", 0) > 0:
                print(f"[Device] Reconnected. Lost {stats['lost_chunks']} audio chunks during disconnect.")
            else:
                print("[Device] Reconnected successfully.")
        elif new_state == ConnectionState.FAILED:
            print("\n[Device] Connection failed permanently, audio saved to disk")

    async def _handle_buffer_saved(self, filepath: str) -> None:
        self._saved_audio_files.append(filepath)
        print(f"[Device] Audio saved to: {filepath}")

    async def run(
        self,
        prompt_config: PromptConfig | None = None,
        min_turn_silence_ms: int = 100,
        max_turn_silence_ms: int = 1000,
    ) -> None:
        self._running = True
        self.prompt_config = prompt_config or PromptConfig()

        keyterms_prompt = self.prompt_config.get_keyterms_prompt()

        print("\n" + "=" * 60)
        print("  iTranslate Device Simulator (V3 API)")
        print("=" * 60)
        print(f"  Device ID:         {settings.device_id}")
        print(f"  Audio Source:      microphone")
        print(f"  Sample Rate:       {settings.sample_rate} Hz")
        print(f"  Turn Silence:      {min_turn_silence_ms}-{max_turn_silence_ms}ms")
        print(f"  Prompting:         {self.prompt_config.get_description()}")
        print(f"  Max Retries:       {self.reconnect_config.max_retries}")
        print(f"  Audio Buffering:   {'Enabled' if self.buffer_config.enable_buffering else 'Disabled'}")
        print(f"  Speaker Diarization: {'Enabled' if self.enable_diarization else 'Disabled'}")
        if self.client.use_token_service:
            print(f"  Session Duration:  {self.client.session_duration}s (token service)")
        if keyterms_prompt:
            preview = keyterms_prompt[:5]
            more = f" (+{len(keyterms_prompt) - 5} more)" if len(keyterms_prompt) > 5 else ""
            print(f"  Keyterms:          {', '.join(preview)}{more}")
        print("=" * 60 + "\n")

        self.audio_source = await create_audio_source()

        session_config = SessionConfig(
            sample_rate=settings.sample_rate,
            speech_model="u3-rt-pro",
            keyterms_prompt=keyterms_prompt,
            min_turn_silence=min_turn_silence_ms,
            max_turn_silence=max_turn_silence_ms,
            enable_speaker_diarization=self.enable_diarization,
        )

        try:
            await self.client.connect(
                config=session_config,
                on_transcript=self._handle_transcript,
                on_error=self._handle_error,
                on_state_change=self._handle_state_change,
                on_buffer_saved=self._handle_buffer_saved,
            )

            print("[Device] Listening... (Press Ctrl+C to stop)\n")

            await self.client.stream_audio(self.audio_source)

        except asyncio.CancelledError:
            print("\n\n[Device] Stopping...")
        except Exception as e:
            print(f"\n[Device] Error: {e}")
        finally:
            await self.stop()

    async def force_end_turn(self) -> None:
        """V3: Force end the current turn."""
        await self.client.force_end_turn()

    async def update_keyterms(self, keyterms: list[str]) -> None:
        """V3: Dynamically update keyterms during session."""
        await self.client.update_configuration(keyterms_prompt=keyterms)

    async def stop(self) -> None:
        self._running = False

        try:
            async with asyncio.timeout(4.0):
                if self.audio_source:
                    await self.audio_source.stop()

                if self.client.is_connected or self.client.connection_state != ConnectionState.DISCONNECTED:
                    # Force-finalize any in-progress turn before closing so the
                    # last utterance is saved rather than lost as a partial.
                    await self.client.force_end_turn()
                    await asyncio.sleep(0.5)
                    await self.client.disconnect()
        except (asyncio.TimeoutError, Exception):
            pass

        if self.output_path and self.handler.state.segments:
            self.handler.save_to_file(self.output_path)

        self._print_session_summary()

    def _print_session_summary(self) -> None:
        final_transcript = self.handler.get_full_transcript()
        connection_stats = self.client.connection_stats
        buffer_stats = self.client.buffer_stats
        speaker_summary = self.handler.get_speaker_summary()

        print("\n" + "=" * 60)
        print("  Session Summary")
        print("=" * 60)

        if self.prompt_config:
            print(f"\n  Prompt Config: {self.prompt_config.get_description()}")

        print("\n  Connection Statistics:")
        print(f"    Total Connects:    {connection_stats['total_connects']}")
        print(f"    Total Disconnects: {connection_stats['total_disconnects']}")
        print(f"    Reconnect Attempts:{connection_stats['total_reconnects']}")
        print(f"    Failed Reconnects: {connection_stats['failed_reconnects']}")
        print(f"    Uptime:            {connection_stats['total_uptime_seconds']:.1f}s")

        if buffer_stats['buffered_duration_seconds'] > 0 or self._saved_audio_files:
            print("\n  Buffer Statistics:")
            print(f"    Currently Buffered: {buffer_stats['buffered_duration_seconds']:.1f}s")
            if buffer_stats.get('lost_chunks', 0) > 0:
                print(f"    Lost Chunks:        {buffer_stats['lost_chunks']}")
            if self._saved_audio_files:
                print(f"    Saved Audio Files:  {len(self._saved_audio_files)}")
                for filepath in self._saved_audio_files:
                    print(f"      - {filepath}")

        if self._errors:
            print(f"\n  Errors Encountered: {len(self._errors)}")
            for error in self._errors[-5:]:
                print(f"    - {error.category.value}: {error.message}")

        if speaker_summary:
            print("\n  Speaker Statistics:")
            print("-" * 60)
            for speaker_id, stats in speaker_summary.items():
                print(f"  {speaker_id}:")
                print(f"    Segments:  {stats['segments']}")
                print(f"    Words:     {stats['words']}")
                print(f"    Duration:  {stats['duration_ms']}ms")
                print()

        if final_transcript:
            print("-" * 60)
            print(f"  Full Transcript:")
            print(f"    {final_transcript}")

        print("=" * 60 + "\n")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="iTranslate Device Simulator (V3 API)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Default mode (no boosting)
  python -m src.main

  # With speaker diarization
  python -m src.main --diarization

  # Use a preset configuration
  python -m src.main --preset travel_general

  # Keyterms mode with specific domains
  python -m src.main --mode keyterms --domains travel medical

  # Task-based mode (with prompt)
  python -m src.main --mode task --task restaurant --domains hospitality

  # Custom keyterms
  python -m src.main --mode custom --keyterms "iTranslate" "Pocketalk" "WiFi"

  # V3 turn detection tuning
  python -m src.main --min-turn-silence 200 --max-turn-silence 1500

  # With reconnection and buffer settings
  python -m src.main --max-retries 10 --buffer-duration 600

  # List available options
  python -m src.main --list-options
        """,
    )

    prompt_group = parser.add_argument_group("Prompting Configuration")
    prompt_group.add_argument(
        "--keyterms",
        type=str,
        nargs="+",
        metavar="TERM",
        help="Custom keyterms to boost recognition for (e.g. --keyterms \"iTranslate\" \"AssemblyAI\")",
    )

    timing_group = parser.add_argument_group("Timing Configuration (V3)")
    timing_group.add_argument(
        "--session-duration",
        type=int,
        default=3600,
        metavar="SECONDS",
        help="Max streaming session duration in seconds (60–10800, default: 3600). Only applies in --token-service mode.",
    )
    timing_group.add_argument(
        "--min-turn-silence",
        type=int,
        default=100,
        help="V3: Pause duration (ms) that triggers a partial transcript (default: 100)",
    )
    timing_group.add_argument(
        "--max-turn-silence",
        type=int,
        default=1000,
        help="V3: Max pause (ms) before final transcript is emitted (default: 1000)",
    )

    reliability_group = parser.add_argument_group("Reliability Configuration")
    reliability_group.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Maximum reconnection attempts (default: 5)",
    )
    reliability_group.add_argument(
        "--initial-delay",
        type=int,
        default=1000,
        help="Initial reconnection delay in ms (default: 1000)",
    )
    reliability_group.add_argument(
        "--max-delay",
        type=int,
        default=30000,
        help="Maximum reconnection delay in ms (default: 30000)",
    )
    reliability_group.add_argument(
        "--no-jitter",
        action="store_true",
        help="Disable jitter in reconnection backoff",
    )

    buffer_group = parser.add_argument_group("Buffer Configuration")
    buffer_group.add_argument(
        "--no-buffer",
        action="store_true",
        help="Disable audio buffering during disconnection",
    )
    buffer_group.add_argument(
        "--buffer-duration",
        type=int,
        default=300,
        help="Maximum buffer duration in seconds (default: 300)",
    )
    buffer_group.add_argument(
        "--buffer-dir",
        type=str,
        help="Directory to save buffered audio files",
    )
    buffer_group.add_argument(
        "--no-save-on-failure",
        action="store_true",
        help="Don't save buffered audio when connection fails permanently",
    )

    diarization_group = parser.add_argument_group("Speaker Diarization")
    diarization_group.add_argument(
        "--diarization",
        action="store_true",
        help="Enable speaker diarization",
    )
    diarization_group.add_argument(
        "--speakers",
        type=str,
        nargs="+",
        metavar="NAME",
        help="Map speakers to names in order of first appearance (e.g. --speakers Pushkar John)",
    )

    parser.add_argument(
        "--token-service",
        action="store_true",
        help="Use the token service for authentication instead of a direct API key",
    )
    parser.add_argument(
        "--output",
        type=str,
        metavar="FILE",
        help="Save transcript to a .txt file (e.g. --output ./transcripts/session.txt)",
    )

    args = parser.parse_args()

    prompt_config = create_prompt_config(
        mode="custom" if args.keyterms else "default",
        custom_keyterms=args.keyterms,
    )

    reconnect_config = ReconnectConfig(
        max_retries=args.max_retries,
        initial_delay_ms=args.initial_delay,
        max_delay_ms=args.max_delay,
        jitter=not args.no_jitter,
    )

    buffer_config = BufferConfig(
        enable_buffering=not args.no_buffer,
        max_buffer_seconds=args.buffer_duration,
        save_on_failure=not args.no_save_on_failure,
        storage_dir=args.buffer_dir,
    )

    simulator = DeviceSimulator(
        reconnect_config=reconnect_config,
        buffer_config=buffer_config,
        enable_diarization=args.diarization,
        use_token_service=args.token_service,
        speaker_names=args.speakers,
        output_path=args.output,
        session_duration=args.session_duration,
    )

    try:
        asyncio.run(
            simulator.run(
                prompt_config=prompt_config,
                min_turn_silence_ms=args.min_turn_silence,
                max_turn_silence_ms=args.max_turn_silence,
            )
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
