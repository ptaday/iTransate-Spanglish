# Transcript handler — consumes Turn messages from the streaming client and
# manages display, state accumulation, and file export.
#
# Key responsibilities:
#   - Display partial transcripts in-place (overwritten on update)
#   - Display final transcripts with confidence and optional speaker label
#   - Accumulate final segments into ConversationState for the session summary
#   - Map raw speaker labels (A, B, …) to human-readable names via _resolve_speaker
#   - Save the full session transcript to a structured .txt file on request
#
# Speaker name mapping:
#   Raw labels from AssemblyAI are single letters (A, B, C, …) assigned in order
#   of first appearance. The --speakers CLI flag provides a name list that is
#   applied in the same order. If fewer names than speakers are provided, the
#   remaining speakers keep their raw label.

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Coroutine, Any
from ..streaming.client import TranscriptMessage, MessageType, WordInfo


@dataclass
class TranscriptSegment:
    """One finalized (end_of_turn=True) transcript turn from AssemblyAI V3."""
    text: str
    confidence: float        # Derived from word-level average (V3 has no turn-level confidence)
    audio_start: int         # Milliseconds from session start
    audio_end: int
    is_final: bool           # Always True for segments stored in ConversationState
    timestamp: datetime = field(default_factory=datetime.now)
    words: list[WordInfo] = field(default_factory=list)
    speaker: str | None = None   # Raw label from AssemblyAI (e.g. "A", "B")
    turn_order: int = 0          # Monotonically increasing per diarization turn

    @property
    def duration_ms(self) -> int:
        return self.audio_end - self.audio_start


@dataclass
class SpeakerStats:
    speaker_id: str
    segment_count: int = 0
    word_count: int = 0
    total_duration_ms: int = 0
    texts: list[str] = field(default_factory=list)

    def add_segment(self, segment: TranscriptSegment) -> None:
        self.segment_count += 1
        self.word_count += len(segment.words)
        self.total_duration_ms += segment.duration_ms
        if segment.text:
            self.texts.append(segment.text)

    @property
    def full_text(self) -> str:
        return " ".join(self.texts)


@dataclass
class ConversationState:
    """Accumulates the full conversation across all turns in a session."""
    segments: list[TranscriptSegment] = field(default_factory=list)
    current_partial: str = ""                         # Latest partial transcript (overwritten)
    current_partial_speaker: str | None = None
    final_transcript: str = ""                        # Concatenation of all final turns
    speaker_stats: dict[str, SpeakerStats] = field(default_factory=dict)

    def add_segment(self, segment: TranscriptSegment) -> None:
        if segment.is_final:
            self.segments.append(segment)

            if segment.speaker:
                prefix = f"[{segment.speaker}] "
            else:
                prefix = ""

            if self.final_transcript:
                self.final_transcript += " " + prefix + segment.text
            else:
                self.final_transcript = prefix + segment.text

            self.current_partial = ""
            self.current_partial_speaker = None

            if segment.speaker:
                if segment.speaker not in self.speaker_stats:
                    self.speaker_stats[segment.speaker] = SpeakerStats(speaker_id=segment.speaker)
                self.speaker_stats[segment.speaker].add_segment(segment)
        else:
            self.current_partial = segment.text
            self.current_partial_speaker = segment.speaker

    def get_display_text(self) -> str:
        if self.current_partial:
            return f"{self.final_transcript} {self.current_partial}".strip()
        return self.final_transcript

    def get_speaker_summary(self) -> dict[str, dict]:
        return {
            speaker_id: {
                "segments": stats.segment_count,
                "words": stats.word_count,
                "duration_ms": stats.total_duration_ms,
                "text": stats.full_text,
            }
            for speaker_id, stats in self.speaker_stats.items()
        }

    def clear(self) -> None:
        self.segments.clear()
        self.current_partial = ""
        self.current_partial_speaker = None
        self.final_transcript = ""
        self.speaker_stats.clear()


FinalTranscriptCallback = Callable[[str, TranscriptSegment], Coroutine[Any, Any, None]]


class TranscriptHandler:
    def __init__(
        self,
        on_final_transcript: FinalTranscriptCallback | None = None,
        display_partials: bool = True,
        show_speaker_labels: bool = True,
        speaker_names: list[str] | None = None,
    ):
        self.state = ConversationState()
        self.on_final_transcript = on_final_transcript
        self.display_partials = display_partials
        self.show_speaker_labels = show_speaker_labels
        self.speaker_names = speaker_names or []
        self._speaker_map: dict[str, str] = {}
        self._last_partial_length = 0
        self._last_speaker: str | None = None
        self._session_start = datetime.now()

    def _resolve_speaker(self, raw_label: str) -> str:
        """Map a raw AssemblyAI label (e.g. 'A') to a display name.

        Names are assigned in order of first appearance. If the caller
        supplied --speakers Alice Bob, then 'A' → 'Alice', 'B' → 'Bob'.
        Any label beyond the names list keeps its raw label.
        """
        if raw_label not in self._speaker_map:
            idx = len(self._speaker_map)
            name = self.speaker_names[idx] if idx < len(self.speaker_names) else raw_label
            self._speaker_map[raw_label] = name
        return self._speaker_map[raw_label]

    async def handle_transcript(self, message: TranscriptMessage) -> None:
        """Handle V3 Turn message.

        U3-Pro emits partials on detected pauses (min_turn_silence), not continuously.
        This gives fewer but higher-quality partial updates vs word-by-word streaming.

        - end_of_turn: false → partial (emitted on short pause, display-only)
        - end_of_turn: true  → final (emitted on terminal punctuation or max silence)
        """
        if not message.text:
            return

        segment = TranscriptSegment(
            text=message.text,
            confidence=message.confidence,
            audio_start=message.audio_start,
            audio_end=message.audio_end,
            is_final=message.is_final,  # V3: end_of_turn
            words=message.words,
            speaker=message.speaker,  # V3: speaker_label
            turn_order=message.turn_order,  # V3: turn_order
        )

        self.state.add_segment(segment)

        # V3 uses end_of_turn to indicate final vs partial
        if message.is_final:
            self._clear_partial_display()
            self._display_final(message)

            if self.on_final_transcript:
                await self.on_final_transcript(message.text, segment)

        elif self.display_partials:
            self._update_partial_display(message.text, message.speaker)

    def _display_final(self, message: TranscriptMessage) -> None:
        resolved = self._resolve_speaker(message.speaker) if message.speaker else None
        speaker_changed = message.speaker != self._last_speaker and message.speaker is not None

        if self.show_speaker_labels and resolved:
            if speaker_changed:
                print(f"\n[{resolved}]")
            print(f"[Final] {message.text}  ({message.confidence:.2%})")
        else:
            print(f"\n[Final] {message.text}  ({message.confidence:.2%})")


        if message.speaker:
            self._last_speaker = message.speaker

    def _update_partial_display(self, text: str, speaker: str | None = None) -> None:
        clear_chars = "\b" * self._last_partial_length + " " * self._last_partial_length + "\b" * self._last_partial_length
        resolved = self._resolve_speaker(speaker) if speaker else None

        if self.show_speaker_labels and resolved:
            display_text = f"[{resolved}] [Partial] {text}"
        else:
            display_text = f"[Partial] {text}"

        print(f"{clear_chars}{display_text}", end="", flush=True)
        self._last_partial_length = len(display_text)

    def _clear_partial_display(self) -> None:
        if self._last_partial_length > 0:
            clear_chars = "\b" * self._last_partial_length + " " * self._last_partial_length + "\b" * self._last_partial_length
            print(clear_chars, end="", flush=True)
            self._last_partial_length = 0

    def get_full_transcript(self) -> str:
        return self.state.final_transcript

    def get_segments(self) -> list[TranscriptSegment]:
        return self.state.segments.copy()

    def get_speaker_summary(self) -> dict[str, dict]:
        return self.state.get_speaker_summary()

    def get_speakers(self) -> list[str]:
        return list(self.state.speaker_stats.keys())

    def save_to_file(self, path: str) -> None:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)

        segments = self.state.segments
        session_end = datetime.now()
        duration = (session_end - self._session_start).total_seconds()

        with open(output, "w", encoding="utf-8") as f:
            f.write("=" * 60 + "\n")
            f.write("  iTranslate Session Transcript\n")
            f.write("=" * 60 + "\n")
            f.write(f"  Started:  {self._session_start.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"  Ended:    {session_end.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"  Duration: {duration:.1f}s\n")
            f.write(f"  Turns:    {len(segments)}\n")
            if self._speaker_map:
                f.write(f"  Speakers: {', '.join(self._speaker_map.values())}\n")
            f.write("=" * 60 + "\n\n")

            for seg in segments:
                start_s = seg.audio_start / 1000
                end_s = seg.audio_end / 1000
                speaker = self._resolve_speaker(seg.speaker) if seg.speaker else None

                f.write("-" * 60 + "\n")
                f.write(f"[{start_s:07.3f}s → {end_s:07.3f}s]")
                if speaker:
                    f.write(f"  Speaker: {speaker}")
                f.write("\n")
                f.write(f"Text: {seg.text}\n")

                if seg.words:
                    f.write("Words:\n")
                    for word in seg.words:
                        ws = word.start / 1000
                        we = word.end / 1000
                        f.write(f"  {word.text:<20} [{ws:07.3f}s → {we:07.3f}s]\n")
                f.write("\n")

            f.write("=" * 60 + "\n")
            f.write(f"Full Transcript:\n{self.state.final_transcript}\n")
            f.write("=" * 60 + "\n")

        print(f"[Device] Transcript saved to: {output.resolve()}")

    def clear(self) -> None:
        self.state.clear()
        self._last_partial_length = 0
        self._last_speaker = None
