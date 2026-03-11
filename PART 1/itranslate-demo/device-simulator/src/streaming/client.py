# AssemblyAI V3 Streaming WebSocket client.
#
# Responsibilities:
#   1. Authenticate — either via a short-lived token from the token service
#      (production) or a direct API key in the Authorization header (--no-token).
#   2. Open a WebSocket to wss://streaming.assemblyai.com/v3/ws with session
#      parameters encoded in the query string (sample_rate, speech_model, etc.).
#   3. Run two concurrent asyncio tasks:
#        _receive_loop  — reads inbound JSON messages from AssemblyAI
#        _send_loop     — drains the audio queue and writes raw PCM bytes
#   4. Handle disconnects by delegating to ReconnectManager, which applies
#      exponential backoff before re-establishing the connection.
#   5. Buffer audio during disconnects so no speech is silently dropped.
#
# V3 message protocol (inbound from AssemblyAI):
#   Begin         — session started; provides session ID and expiry
#   SpeechStarted — voice activity detected; provides start timestamp
#   Turn          — transcript (partial or final); end_of_turn=True means final
#   Termination   — server closed the session gracefully
#   Error         — structured error; FATAL errors abort the reconnect loop
#
# V3 message protocol (outbound to AssemblyAI):
#   raw bytes         — PCM audio chunks (pcm_s16le, 16kHz mono)
#   ForceEndpoint     — force the current turn to finalize immediately
#   UpdateConfiguration — change keyterms or silence thresholds mid-session
#   Terminate         — politely close the session

import asyncio
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine
import httpx
import websockets
from websockets.client import WebSocketClientProtocol
from ..config import settings
from ..audio.capture import AudioSource
from ..audio.buffer import AudioBuffer, OfflineRecorder
from .reconnect import ReconnectManager, ReconnectConfig, ConnectionState
from .errors import (
    StreamingError,
    TokenServiceError,
    ConnectionError,
    parse_error,
    ErrorSeverity,
)


# V3 message types received from AssemblyAI
class MessageType(Enum):
    BEGIN = "Begin"
    TURN = "Turn"
    TERMINATION = "Termination"
    SPEECH_STARTED = "SpeechStarted"
    ERROR = "Error"


@dataclass
class SessionConfig:
    # Audio format — AssemblyAI V3 requires 16kHz mono signed 16-bit PCM
    sample_rate: int = settings.sample_rate
    encoding: str = "pcm_s16le"

    # Only valid V3 speech model for real-time streaming
    speech_model: str = "u3-rt-pro"

    # Turn detection (V3 parameters):
    #   min_turn_silence — short pause (ms) that emits a partial transcript
    #   max_turn_silence — longer silence that forces end_of_turn=true (final)
    min_turn_silence: int = 100
    max_turn_silence: int = 1000

    # Keyterm boosting — improves recognition of specific words/phrases.
    # NOTE: mutually exclusive with `prompt`; do not send both.
    keyterms_prompt: list[str] | None = None

    # Speaker diarization — adds speaker_label to each Turn message
    enable_speaker_diarization: bool = False

    # Set True to receive only final transcripts (no partials)
    disable_partial_transcripts: bool = False


@dataclass
class WordInfo:
    text: str
    start: int
    end: int
    confidence: float
    speaker: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WordInfo":
        return cls(
            text=data.get("text", ""),
            start=data.get("start", 0),
            end=data.get("end", 0),
            confidence=data.get("confidence", 0.0),
            speaker=data.get("speaker"),
        )


@dataclass
class TranscriptMessage:
    """V3 Turn message representation."""
    message_type: MessageType
    text: str
    audio_start: int
    audio_end: int
    confidence: float
    words: list[WordInfo]
    is_final: bool  # V3: end_of_turn field
    speaker: str | None = None  # V3: speaker_label field
    turn_order: int = 0  # V3: turn_order field for diarization

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranscriptMessage":
        msg_type = MessageType(data.get("type", ""))
        words_data = data.get("words", [])
        words = [WordInfo.from_dict(w) for w in words_data]

        # V3 uses end_of_turn instead of separate message types
        is_final = data.get("end_of_turn", False)

        # V3 has no top-level confidence — derive it from word-level confidences
        word_confidences = [w.confidence for w in words if w.confidence > 0]
        confidence = sum(word_confidences) / len(word_confidences) if word_confidences else 0.0

        # V3 uses speaker_label field directly on Turn message
        speaker = data.get("speaker_label")

        # Fallback: extract speaker from words if not at turn level
        if not speaker and words:
            speakers = [w.speaker for w in words if w.speaker]
            if speakers:
                from collections import Counter
                speaker = Counter(speakers).most_common(1)[0][0]

        return cls(
            message_type=msg_type,
            text=data.get("transcript", ""),  # V3 uses 'transcript' field
            audio_start=data.get("start", 0),  # V3 uses 'start'
            audio_end=data.get("end", 0),  # V3 uses 'end'
            confidence=confidence,
            words=words,
            is_final=is_final,
            speaker=speaker,
            turn_order=data.get("turn_order", 0),
        )


TranscriptCallback = Callable[[TranscriptMessage], Coroutine[Any, Any, None]]
ErrorCallback = Callable[[StreamingError], Coroutine[Any, Any, None]]
StateChangeCallback = Callable[[ConnectionState, ConnectionState], Coroutine[Any, Any, None]]
BufferCallback = Callable[[str], Coroutine[Any, Any, None]]


@dataclass
class BufferConfig:
    enable_buffering: bool = True      # Buffer audio while disconnected
    max_buffer_seconds: int = 300      # Drop oldest chunks beyond this window
    save_on_failure: bool = True       # Write buffered audio to disk on permanent failure
    storage_dir: str | None = None     # Where to save the audio files


class AssemblyAIStreamingClient:
    def __init__(
        self,
        token_service_url: str = settings.token_service_url,
        device_api_key: str = settings.device_api_key,
        device_id: str = settings.device_id,
        reconnect_config: ReconnectConfig | None = None,
        buffer_config: BufferConfig | None = None,
        use_token_service: bool = True,
        api_key: str = settings.assemblyai_api_key,
        session_duration: int = 3600,
    ):
        self.token_service_url = token_service_url
        self.device_api_key = device_api_key
        self.device_id = device_id
        self.use_token_service = use_token_service
        self.api_key = api_key
        self.session_duration = session_duration  # max_session_duration_seconds for token service mode

        self._websocket: WebSocketClientProtocol | None = None
        self._running = False
        self._session_config: SessionConfig | None = None
        self._session_id: str | None = None

        self._on_transcript: TranscriptCallback | None = None
        self._on_error: ErrorCallback | None = None
        self._on_state_change: StateChangeCallback | None = None
        self._on_buffer_saved: BufferCallback | None = None

        self._reconnect_manager = ReconnectManager(
            config=reconnect_config or ReconnectConfig(),
            on_state_change=self._handle_state_change,
        )

        self._buffer_config = buffer_config or BufferConfig()
        self._audio_buffer = AudioBuffer(
            max_duration_seconds=self._buffer_config.max_buffer_seconds,
            storage_dir=self._buffer_config.storage_dir,
        )
        self._offline_recorder = OfflineRecorder(
            storage_dir=self._buffer_config.storage_dir,
        )

        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        self._receive_task: asyncio.Task | None = None
        self._send_task: asyncio.Task | None = None

    async def _handle_state_change(
        self,
        old_state: ConnectionState,
        new_state: ConnectionState,
    ) -> None:
        if new_state == ConnectionState.RECONNECTING:
            if self._buffer_config.enable_buffering:
                self._audio_buffer.start_buffering()

        elif new_state == ConnectionState.CONNECTED and old_state == ConnectionState.RECONNECTING:
            if self._audio_buffer.is_buffering:
                self._audio_buffer.stop_buffering()
                stats = self._audio_buffer.get_stats()
                if stats["lost_chunks"] > 0:
                    print(f"[Streaming] Reconnected. Lost {stats['lost_chunks']} audio chunks during disconnect.")
                self._audio_buffer.clear()

        elif new_state == ConnectionState.FAILED:
            if self._audio_buffer.is_buffering and self._buffer_config.save_on_failure:
                await self._save_buffered_audio()

        if self._on_state_change:
            await self._on_state_change(old_state, new_state)

    async def _save_buffered_audio(self) -> None:
        if self._audio_buffer.buffered_chunks_count == 0:
            return

        try:
            filepath = self._audio_buffer.save_to_file()
            self._audio_buffer.clear()

            if self._on_buffer_saved:
                await self._on_buffer_saved(str(filepath))

        except Exception as e:
            print(f"[Streaming] Error saving buffered audio: {e}")

    async def _get_token(self) -> tuple[str, str]:
        # expires_in_seconds controls how long the token is valid for OPENING the
        # WebSocket (the connection window). Once the WebSocket handshake completes,
        # this token is no longer referenced — session lifetime is governed by
        # max_session_duration_seconds on the AssemblyAI side.
        # 300 seconds gives the device plenty of time to receive the token and connect.
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.token_service_url}/api/token",
                    headers={
                        "Authorization": f"Bearer {self.device_api_key}",
                        "X-Device-ID": self.device_id,
                    },
                    json={
                        "expires_in_seconds": 300,
                        "max_session_duration_seconds": self.session_duration,
                    },
                )

                if response.status_code == 401:
                    raise TokenServiceError("Invalid device credentials", 401)
                elif response.status_code == 403:
                    raise TokenServiceError("Device not authorized", 403)
                elif response.status_code == 429:
                    raise TokenServiceError("Rate limited", 429)
                elif response.status_code >= 500:
                    raise TokenServiceError(f"Server error: {response.status_code}", response.status_code)

                response.raise_for_status()
                data = response.json()
                return data["token"], data["websocket_url"]

        except httpx.ConnectError as e:
            raise TokenServiceError(f"Cannot connect to token service: {e}")
        except httpx.TimeoutException:
            raise TokenServiceError("Token service request timed out")

    def _build_ws_url(self, base_url: str, token: str | None, config: SessionConfig) -> str:
        import urllib.parse

        params = []
        if token:
            params.append(f"token={token}")
        params += [
            f"sample_rate={config.sample_rate}",
            f"encoding={config.encoding}",
            f"speech_model={config.speech_model}",
        ]

        # V3 turn detection parameters
        params.append(f"min_turn_silence={config.min_turn_silence}")
        params.append(f"max_turn_silence={config.max_turn_silence}")

        # V3 prompting: keyterms_prompt replaces word_boost
        if config.keyterms_prompt:
            keyterms_json = json.dumps(config.keyterms_prompt)
            params.append(f"keyterms_prompt={urllib.parse.quote(keyterms_json)}")

        if config.disable_partial_transcripts:
            params.append("disable_partial_transcripts=true")

        # V3 speaker diarization
        if config.enable_speaker_diarization:
            params.append("speaker_labels=true")

        return f"{base_url}?{'&'.join(params)}"

    async def _establish_connection(self) -> bool:
        # Two authentication modes:
        #   Token service  — device POST /api/token → get short-lived token → embed in WS URL
        #   Direct (--no-token) — embed API key in Authorization header on the WS handshake
        # In both cases all session parameters (sample_rate, model, keyterms, …) go in the URL.
        try:
            config = self._session_config or SessionConfig()

            if self.use_token_service:
                print(f"[Streaming] Requesting token from {self.token_service_url}")
                token, ws_url = await self._get_token()
                print("[Streaming] Token acquired, connecting to AssemblyAI")
                full_url = self._build_ws_url(ws_url, token, config)
                extra_headers = {}
            else:
                print("[Streaming] Connecting directly to AssemblyAI (no token service)")
                ws_url = settings.assemblyai_ws_url
                full_url = self._build_ws_url(ws_url, token=None, config=config)
                extra_headers = {"Authorization": self.api_key}

            # ping_interval=None disables the websockets library's built-in ping/pong
            # because AssemblyAI manages its own heartbeat at the application level.
            self._websocket = await websockets.connect(
                full_url,
                additional_headers=extra_headers,
                ping_interval=None,
            )

            print("[Streaming] WebSocket connected, waiting for session")
            return True

        except TokenServiceError as e:
            print(f"[Streaming] Token service error: {e}")
            if not e.recoverable:
                self._reconnect_manager.disable_reconnect()
            return False
        except websockets.exceptions.WebSocketException as e:
            print(f"[Streaming] WebSocket connection failed: {e}")
            return False
        except Exception as e:
            print(f"[Streaming] Connection error: {e}")
            return False

    async def connect(
        self,
        config: SessionConfig | None = None,
        on_transcript: TranscriptCallback | None = None,
        on_error: ErrorCallback | None = None,
        on_state_change: StateChangeCallback | None = None,
        on_buffer_saved: BufferCallback | None = None,
    ) -> None:
        self._session_config = config or SessionConfig()
        self._on_transcript = on_transcript
        self._on_error = on_error
        self._on_state_change = on_state_change
        self._on_buffer_saved = on_buffer_saved
        self._running = True

        self._reconnect_manager.reset()

        if not await self._establish_connection():
            if self._buffer_config.enable_buffering:
                self._audio_buffer.start_buffering()
                print("[Streaming] Connection failed, buffering audio for later")

            raise ConnectionError("Failed to establish initial connection")

        await self._reconnect_manager.on_connect_success()

        self._receive_task = asyncio.create_task(self._receive_loop())
        self._send_task = asyncio.create_task(self._send_loop())

    async def _receive_loop(self) -> None:
        while self._running:
            try:
                await self._receive_messages()
            except websockets.exceptions.ConnectionClosed as e:
                print(f"[Streaming] Connection closed: {e.code} - {e.reason}")
                await self._handle_disconnect(e)
            except Exception as e:
                print(f"[Streaming] Receive error: {e}")
                await self._handle_disconnect(e)

            if not self._running:
                break

            if self._reconnect_manager.can_retry:
                success = await self._reconnect_manager.reconnect_loop(
                    self._establish_connection
                )
                if success:
                    continue

            break

    async def _receive_messages(self) -> None:
        if not self._websocket:
            return

        async for message in self._websocket:
            if not self._running:
                break

            data = json.loads(message)
            msg_type = data.get("type", "")

            # V3: Begin message (replaces SessionBegins)
            if msg_type == MessageType.BEGIN.value:
                self._session_id = data.get("id", "unknown")
                expires_at = data.get("expires_at", "unknown")
                print(f"[Streaming] V3 Session started: {self._session_id}")
                print(f"[Streaming] Session expires at: {expires_at}")

            # V3: Termination message (replaces SessionTerminated)
            elif msg_type == MessageType.TERMINATION.value:
                reason = data.get("reason", "unknown")
                print(f"[Streaming] Session terminated: {reason}")
                self._running = False
                break

            # V3: SpeechStarted message (new in V3)
            elif msg_type == MessageType.SPEECH_STARTED.value:
                audio_start = data.get("audio_start", 0)
                print(f"[Streaming] Speech detected at {audio_start}ms")

            elif msg_type == MessageType.ERROR.value:
                error = parse_error(data)
                print(f"[Streaming] {error}")

                if self._on_error:
                    await self._on_error(error)

                if error.severity == ErrorSeverity.FATAL:
                    self._reconnect_manager.disable_reconnect()
                    self._running = False
                    break

            # V3: Turn message (replaces PartialTranscript/FinalTranscript)
            elif msg_type == MessageType.TURN.value:
                transcript = TranscriptMessage.from_dict(data)
                if self._on_transcript:
                    await self._on_transcript(transcript)

    async def _send_loop(self) -> None:
        while self._running:
            try:
                audio_data = await asyncio.wait_for(
                    self._audio_queue.get(),
                    timeout=0.5
                )

                if self._audio_buffer.is_buffering:
                    self._audio_buffer.add_chunk(audio_data)

                if self._websocket and self._reconnect_manager.state == ConnectionState.CONNECTED:
                    await self._websocket.send(audio_data)

            except asyncio.TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed:
                await asyncio.sleep(0.1)
            except Exception as e:
                print(f"[Streaming] Send error: {e}")
                await asyncio.sleep(0.1)

    async def _handle_disconnect(self, error: Exception | None = None) -> None:
        await self._reconnect_manager.on_disconnect(error)

        if self._websocket:
            try:
                await self._websocket.close()
            except Exception:
                pass
            self._websocket = None

    async def stream_audio(self, audio_source: AudioSource) -> None:
        if self._reconnect_manager.state != ConnectionState.CONNECTED:
            if not self._audio_buffer.is_buffering:
                raise RuntimeError("Not connected. Call connect() first.")

        print("[Streaming] Starting audio stream")

        try:
            async for audio_chunk in audio_source.stream():
                if not self._running:
                    break

                try:
                    self._audio_queue.put_nowait(audio_chunk)
                except asyncio.QueueFull:
                    pass

        except Exception as e:
            print(f"[Streaming] Error streaming audio: {e}")

    async def force_end_turn(self) -> None:
        """V3: Force end the current turn immediately."""
        if self._websocket and self._reconnect_manager.state == ConnectionState.CONNECTED:
            try:
                await self._websocket.send(json.dumps({
                    "type": "ForceEndpoint"
                }))
                print("[Streaming] Force endpoint signal sent")
            except Exception as e:
                print(f"[Streaming] Error sending ForceEndpoint: {e}")

    async def update_configuration(
        self,
        keyterms_prompt: list[str] | None = None,
        min_turn_silence: int | None = None,
        max_turn_silence: int | None = None,
    ) -> None:
        """V3: Dynamically update session configuration."""
        if self._websocket and self._reconnect_manager.state == ConnectionState.CONNECTED:
            try:
                config_update: dict[str, Any] = {
                    "type": "UpdateConfiguration"
                }

                if keyterms_prompt is not None:
                    config_update["keyterms_prompt"] = keyterms_prompt
                if min_turn_silence is not None:
                    config_update["min_turn_silence"] = min_turn_silence
                if max_turn_silence is not None:
                    config_update["max_turn_silence"] = max_turn_silence

                await self._websocket.send(json.dumps(config_update))
                print(f"[Streaming] Configuration updated: {list(config_update.keys())}")
            except Exception as e:
                print(f"[Streaming] Error updating configuration: {e}")

    async def configure_turn_silence(
        self,
        min_silence_ms: int | None = None,
        max_silence_ms: int | None = None
    ) -> None:
        """V3: Configure turn detection silence thresholds."""
        await self.update_configuration(
            min_turn_silence=min_silence_ms,
            max_turn_silence=max_silence_ms
        )

    async def send_terminate(self) -> None:
        """V3: Gracefully terminate the streaming session."""
        if self._websocket:
            try:
                await self._websocket.send(json.dumps({
                    "type": "Terminate"
                }))
                print("[Streaming] Terminate signal sent")
            except Exception as e:
                print(f"[Streaming] Error sending terminate: {e}")

    async def disconnect(self) -> None:
        self._running = False
        self._reconnect_manager.disable_reconnect()

        if self._audio_buffer.is_buffering and self._buffer_config.save_on_failure:
            await self._save_buffered_audio()

        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        if self._send_task:
            self._send_task.cancel()
            try:
                await self._send_task
            except asyncio.CancelledError:
                pass

        if self._websocket:
            await self.send_terminate()
            try:
                await self._websocket.close()
            except Exception:
                pass
            self._websocket = None

        print("[Streaming] Disconnected")
        print(f"[Streaming] Stats: {self._reconnect_manager.stats.get_summary()}")

    @property
    def is_connected(self) -> bool:
        return self._reconnect_manager.state == ConnectionState.CONNECTED

    @property
    def connection_state(self) -> ConnectionState:
        return self._reconnect_manager.state

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def connection_stats(self) -> dict[str, Any]:
        return self._reconnect_manager.stats.get_summary()

    @property
    def buffer_stats(self) -> dict[str, Any]:
        return self._audio_buffer.get_stats()

    @property
    def is_buffering(self) -> bool:
        return self._audio_buffer.is_buffering
