import asyncio
import wave
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator
from collections import deque
from ..config import settings


@dataclass
class BufferedChunk:
    data: bytes
    timestamp: datetime = field(default_factory=datetime.now)


class AudioBuffer:
    def __init__(
        self,
        max_duration_seconds: int = 300,
        sample_rate: int = settings.sample_rate,
        channels: int = settings.channels,
        chunk_duration_ms: int = settings.chunk_duration_ms,
        storage_dir: str | None = None,
    ):
        self.max_duration_seconds = max_duration_seconds
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_duration_ms = chunk_duration_ms
        self.bytes_per_chunk = int(sample_rate * chunk_duration_ms / 1000) * 2

        self._max_chunks = int((max_duration_seconds * 1000) / chunk_duration_ms)
        self._buffer: deque[BufferedChunk] = deque(maxlen=self._max_chunks)
        self._is_buffering = False
        self._buffer_start_time: datetime | None = None
        self._lost_chunks_count = 0

        self.storage_dir = Path(storage_dir) if storage_dir else Path.cwd() / "audio_backup"
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    @property
    def is_buffering(self) -> bool:
        return self._is_buffering

    @property
    def buffered_duration_seconds(self) -> float:
        return len(self._buffer) * self.chunk_duration_ms / 1000

    @property
    def buffered_chunks_count(self) -> int:
        return len(self._buffer)

    def start_buffering(self) -> None:
        if not self._is_buffering:
            self._is_buffering = True
            self._buffer_start_time = datetime.now()
            print(f"[Buffer] Started buffering audio (max {self.max_duration_seconds}s)")

    def stop_buffering(self) -> None:
        if self._is_buffering:
            self._is_buffering = False
            duration = self.buffered_duration_seconds
            print(f"[Buffer] Stopped buffering ({duration:.1f}s buffered)")

    def add_chunk(self, audio_data: bytes) -> None:
        if self._is_buffering:
            if len(self._buffer) >= self._max_chunks:
                self._lost_chunks_count += 1
            self._buffer.append(BufferedChunk(data=audio_data))

    def get_buffered_audio(self) -> list[bytes]:
        return [chunk.data for chunk in self._buffer]

    def clear(self) -> None:
        self._buffer.clear()
        self._buffer_start_time = None
        self._lost_chunks_count = 0
        print("[Buffer] Buffer cleared")

    async def drain_to_stream(self) -> AsyncIterator[bytes]:
        chunks = list(self._buffer)
        self._buffer.clear()

        print(f"[Buffer] Draining {len(chunks)} buffered chunks")

        for chunk in chunks:
            yield chunk.data
            await asyncio.sleep(0.001)

        print("[Buffer] Buffer drained")

    def save_to_file(self, filename: str | None = None) -> Path:
        if not self._buffer:
            raise ValueError("No audio data to save")

        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"buffered_audio_{timestamp}.wav"

        filepath = self.storage_dir / filename

        audio_data = b"".join(chunk.data for chunk in self._buffer)

        with wave.open(str(filepath), "wb") as wav_file:
            wav_file.setnchannels(self.channels)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(audio_data)

        duration = self.buffered_duration_seconds
        print(f"[Buffer] Saved {duration:.1f}s of audio to {filepath}")

        return filepath

    def get_stats(self) -> dict:
        return {
            "is_buffering": self._is_buffering,
            "buffered_chunks": len(self._buffer),
            "lost_chunks": self._lost_chunks_count,
            "buffered_duration_seconds": self.buffered_duration_seconds,
            "max_duration_seconds": self.max_duration_seconds,
            "buffer_start_time": self._buffer_start_time.isoformat() if self._buffer_start_time else None,
        }


class OfflineRecorder:
    def __init__(
        self,
        storage_dir: str | None = None,
        sample_rate: int = settings.sample_rate,
        channels: int = settings.channels,
    ):
        self.storage_dir = Path(storage_dir) if storage_dir else Path.cwd() / "recordings"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.sample_rate = sample_rate
        self.channels = channels

        self._current_file: wave.Wave_write | None = None
        self._current_filepath: Path | None = None
        self._is_recording = False
        self._chunks_written = 0

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    def start_recording(self, filename: str | None = None) -> Path:
        if self._is_recording:
            self.stop_recording()

        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"offline_recording_{timestamp}.wav"

        self._current_filepath = self.storage_dir / filename
        self._current_file = wave.open(str(self._current_filepath), "wb")
        self._current_file.setnchannels(self.channels)
        self._current_file.setsampwidth(2)
        self._current_file.setframerate(self.sample_rate)

        self._is_recording = True
        self._chunks_written = 0

        print(f"[Recorder] Started recording to {self._current_filepath}")
        return self._current_filepath

    def write_chunk(self, audio_data: bytes) -> None:
        if self._is_recording and self._current_file:
            self._current_file.writeframes(audio_data)
            self._chunks_written += 1

    def stop_recording(self) -> Path | None:
        if not self._is_recording:
            return None

        filepath = self._current_filepath

        if self._current_file:
            self._current_file.close()
            self._current_file = None

        self._is_recording = False

        duration = (self._chunks_written * settings.chunk_duration_ms) / 1000
        print(f"[Recorder] Stopped recording ({duration:.1f}s saved to {filepath})")

        return filepath

    def list_recordings(self) -> list[Path]:
        return sorted(self.storage_dir.glob("*.wav"))
