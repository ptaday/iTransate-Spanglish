# Configuration module — loads all environment variables from the .env file into
# a typed Settings object. Using Pydantic Settings ensures all values are validated
# at startup and avoids direct os.environ access scattered throughout the codebase.

from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # --- AssemblyAI credentials ---
    # Used in --no-token (direct) mode: sent as the Authorization header on the WebSocket.
    assemblyai_api_key: str = Field(
        default="",
        description="AssemblyAI API key for direct connection (bypasses token service)"
    )

    # --- Token service ---
    # In production mode the device talks to this service instead of AssemblyAI directly.
    # The service holds the master API key and issues short-lived tokens.
    token_service_url: str = Field(
        default="http://localhost:3001",
        description="URL of the token service"
    )
    device_api_key: str = Field(
        default="",
        description="Device API key for authentication with token service"
    )
    device_id: str = Field(
        default="device-001",
        description="Unique device identifier"
    )

    # --- Audio capture ---
    # AssemblyAI V3 requires 16kHz mono PCM (pcm_s16le encoding).
    # chunk_duration_ms controls how often audio is flushed to the WebSocket.
    sample_rate: int = Field(
        default=16000,
        description="Audio sample rate in Hz (AssemblyAI requires 16kHz)"
    )
    channels: int = Field(
        default=1,
        description="Number of audio channels (mono)"
    )
    chunk_duration_ms: int = Field(
        default=100,
        description="Duration of each audio chunk in milliseconds"
    )

    # --- AssemblyAI V3 WebSocket endpoint ---
    assemblyai_ws_url: str = Field(
        default="wss://streaming.assemblyai.com/v3/ws",
        description="AssemblyAI V3 WebSocket endpoint"
    )

    # --- Language defaults (reserved for future translation pipeline) ---
    default_source_language: str = Field(
        default="en",
        description="Default source language for transcription"
    )
    default_target_language: str = Field(
        default="es",
        description="Default target language for translation"
    )

    prompting_mode: str = Field(
        default="default",
        description="Prompting mode: default, keyterms, or task"
    )

    # --- Speaker diarization ---
    enable_speaker_diarization: bool = Field(
        default=False,
        description="Enable speaker diarization for multi-speaker conversations"
    )

    # --- Audio buffering during disconnects ---
    # Buffered audio is flushed once reconnected; saved to disk on permanent failure.
    enable_audio_buffering: bool = Field(
        default=True,
        description="Enable audio buffering during disconnection"
    )
    max_buffer_seconds: int = Field(
        default=300,
        description="Maximum buffer duration in seconds"
    )
    audio_storage_dir: str = Field(
        default="./audio_backup",
        description="Directory to save buffered audio files"
    )

    class Config:
        # .env is located two directories above this file (at itranslate-demo/.env)
        env_file = str(Path(__file__).parent.parent.parent / ".env")
        env_file_encoding = "utf-8"
        extra = "ignore"


# Singleton — import `settings` anywhere in the package to access config values.
settings = Settings()
