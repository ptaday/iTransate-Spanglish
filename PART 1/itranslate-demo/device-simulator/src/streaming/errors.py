# Error classification module — maps AssemblyAI error codes to structured
# StreamingError objects so the rest of the codebase can decide whether to
# retry, surface a warning, or abort the session.
#
# Key design: every error carries a `recoverable` flag so the reconnect logic
# can skip retries for permanent failures (e.g. bad API key).

from enum import Enum
from dataclasses import dataclass
from typing import Any


class ErrorSeverity(Enum):
    WARNING = "warning"  # Non-blocking, safe to continue
    ERROR = "error"      # Operation failed, may retry
    FATAL = "fatal"      # Session must terminate, no retry


class ErrorCategory(Enum):
    AUTHENTICATION = "authentication"
    CONNECTION = "connection"
    RATE_LIMIT = "rate_limit"
    INVALID_REQUEST = "invalid_request"
    SERVER = "server"
    AUDIO = "audio"
    UNKNOWN = "unknown"


@dataclass
class StreamingError:
    code: str
    message: str
    category: ErrorCategory
    severity: ErrorSeverity
    recoverable: bool           # If True, the reconnect loop will retry
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return f"[{self.severity.value.upper()}] {self.category.value}: {self.message} (code: {self.code})"


# Maps AssemblyAI error code strings → (category, severity, recoverable).
# Errors not in this table fall through to UNKNOWN / non-recoverable.
ERROR_MAPPINGS: dict[str, tuple[ErrorCategory, ErrorSeverity, bool]] = {
    "bad_request": (ErrorCategory.INVALID_REQUEST, ErrorSeverity.ERROR, False),
    "unauthorized": (ErrorCategory.AUTHENTICATION, ErrorSeverity.FATAL, False),
    "insufficient_funds": (ErrorCategory.AUTHENTICATION, ErrorSeverity.FATAL, False),
    "rate_limited": (ErrorCategory.RATE_LIMIT, ErrorSeverity.WARNING, True),
    "session_expired": (ErrorCategory.CONNECTION, ErrorSeverity.ERROR, True),
    "session_not_found": (ErrorCategory.CONNECTION, ErrorSeverity.ERROR, True),
    "connection_timeout": (ErrorCategory.CONNECTION, ErrorSeverity.ERROR, True),
    "audio_too_short": (ErrorCategory.AUDIO, ErrorSeverity.WARNING, True),
    "audio_too_long": (ErrorCategory.AUDIO, ErrorSeverity.WARNING, True),
    "invalid_audio": (ErrorCategory.AUDIO, ErrorSeverity.ERROR, False),
    "server_error": (ErrorCategory.SERVER, ErrorSeverity.ERROR, True),
    "service_unavailable": (ErrorCategory.SERVER, ErrorSeverity.ERROR, True),
}


def parse_error(error_data: dict[str, Any]) -> StreamingError:
    """Convert a raw AssemblyAI error JSON dict into a typed StreamingError."""
    error_code = error_data.get("error", "unknown").lower().replace(" ", "_")
    message = error_data.get("message", error_data.get("error", "Unknown error"))

    if error_code in ERROR_MAPPINGS:
        category, severity, recoverable = ERROR_MAPPINGS[error_code]
    else:
        category = ErrorCategory.UNKNOWN
        severity = ErrorSeverity.ERROR
        recoverable = False

    return StreamingError(
        code=error_code,
        message=message,
        category=category,
        severity=severity,
        recoverable=recoverable,
        details=error_data,
    )


# Exception types used internally by the streaming client.

class TokenServiceError(Exception):
    """Raised when the token service returns an error or is unreachable."""
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        # 429/5xx are transient — safe to retry
        self.recoverable = status_code in (429, 500, 502, 503, 504) if status_code else False


class ConnectionError(Exception):
    """Raised when the WebSocket connection cannot be established or drops."""
    def __init__(self, message: str, recoverable: bool = True):
        super().__init__(message)
        self.recoverable = recoverable


class SessionError(Exception):
    """Raised for session-level failures (e.g. session not found)."""
    def __init__(self, message: str, session_id: str | None = None):
        super().__init__(message)
        self.session_id = session_id


class AudioError(Exception):
    """Raised when there is a problem with audio capture or format."""
    def __init__(self, message: str, recoverable: bool = True):
        super().__init__(message)
        self.recoverable = recoverable
