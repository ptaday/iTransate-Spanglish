# Audio capture module — provides an abstract AudioSource interface and a
# concrete MicrophoneSource implementation.
#
# Architecture:
#   AudioSource (ABC)
#     └── MicrophoneSource  — captures live audio from the default system mic
#     └── FileSource        — streams pre-recorded PCM/WAV (kept for reference, unused in CLI)
#
# MicrophoneSource uses sounddevice's InputStream with a callback that converts
# float32 samples to int16 PCM bytes (the format AssemblyAI V3 expects) and
# pushes them into an asyncio.Queue so the async streaming loop can consume them.

import asyncio
import base64
from abc import ABC, abstractmethod
from pathlib import Path
from typing import AsyncIterator, Callable
import numpy as np
import sounddevice as sd
from ..config import settings


# Abstract base — any audio source must implement stream() and stop().
class AudioSource(ABC):
    @abstractmethod
    async def stream(self) -> AsyncIterator[bytes]:
        pass

    @abstractmethod
    async def stop(self) -> None:
        pass


class MicrophoneSource(AudioSource):
    def __init__(
        self,
        sample_rate: int = settings.sample_rate,
        channels: int = settings.channels,
        chunk_duration_ms: int = settings.chunk_duration_ms,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_duration_ms = chunk_duration_ms
        # Number of frames per chunk (e.g. 16000 * 0.1 = 1600 frames at 100ms)
        self.chunk_size = int(sample_rate * chunk_duration_ms / 1000)
        self._running = False
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._stream: sd.InputStream | None = None

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: dict,
        status: sd.CallbackFlags,
    ) -> None:
        # Called by sounddevice on its own thread — must be non-blocking.
        # Convert float32 [-1, 1] → int16 PCM bytes expected by AssemblyAI.
        if status:
            print(f"[Audio] Status: {status}")
        if self._running:
            audio_bytes = (indata * 32767).astype(np.int16).tobytes()
            try:
                self._queue.put_nowait(audio_bytes)
            except asyncio.QueueFull:
                pass  # Drop chunk if the consumer can't keep up

    async def stream(self) -> AsyncIterator[bytes]:
        self._running = True
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype=np.float32,
            blocksize=self.chunk_size,
            callback=self._audio_callback,
        )

        print(f"[Audio] Starting microphone capture at {self.sample_rate}Hz")
        self._stream.start()

        try:
            while self._running:
                try:
                    # Poll with a short timeout so stop() can interrupt the loop
                    audio_data = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=0.5
                    )
                    yield audio_data
                except asyncio.TimeoutError:
                    continue
        finally:
            await self.stop()

    async def stop(self) -> None:
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        print("[Audio] Microphone capture stopped")


class FileSource(AudioSource):
    def __init__(
        self,
        file_path: str | Path,
        sample_rate: int = settings.sample_rate,
        chunk_duration_ms: int = settings.chunk_duration_ms,
    ):
        self.file_path = Path(file_path)
        self.sample_rate = sample_rate
        self.chunk_duration_ms = chunk_duration_ms
        self.chunk_size = int(sample_rate * chunk_duration_ms / 1000) * 2
        self._running = False

    async def stream(self) -> AsyncIterator[bytes]:
        if not self.file_path.exists():
            raise FileNotFoundError(f"Audio file not found: {self.file_path}")

        self._running = True
        print(f"[Audio] Streaming from file: {self.file_path}")

        with open(self.file_path, "rb") as f:
            if self.file_path.suffix.lower() == ".wav":
                f.seek(44)

            while self._running:
                chunk = f.read(self.chunk_size)
                if not chunk:
                    break

                if len(chunk) < self.chunk_size:
                    chunk = chunk + b"\x00" * (self.chunk_size - len(chunk))

                yield chunk
                await asyncio.sleep(self.chunk_duration_ms / 1000)

        print("[Audio] File streaming complete")

    async def stop(self) -> None:
        self._running = False


def encode_audio_chunk(audio_bytes: bytes) -> str:
    return base64.b64encode(audio_bytes).decode("utf-8")


async def create_audio_source(
    source_type: str = "microphone",
    file_path: str | None = None,
) -> AudioSource:
    if source_type == "file" and file_path:
        return FileSource(file_path)
    return MicrophoneSource()
