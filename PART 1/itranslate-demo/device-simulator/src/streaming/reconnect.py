# Reconnection module — manages WebSocket connection state and implements
# exponential backoff with jitter for automatic reconnection.
#
# Flow:
#   DISCONNECTED → CONNECTING → CONNECTED
#                     ↓ (on drop)
#                 RECONNECTING → (retry loop) → CONNECTED  or  FAILED
#
# The `reconnect_loop` method drives the retry cycle. Each attempt waits an
# increasing delay (initial * 2^attempt, capped at max_delay) with ±20% jitter
# to avoid thundering-herd problems when many devices reconnect simultaneously.
# Non-recoverable errors (e.g. bad API key) call disable_reconnect() to skip
# the loop entirely and move straight to FAILED.

import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Coroutine, Any


class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"       # All retries exhausted or non-recoverable error


@dataclass
class ReconnectConfig:
    max_retries: int = 5
    initial_delay_ms: int = 1000     # First retry waits 1 second
    max_delay_ms: int = 30000        # Never wait longer than 30 seconds
    backoff_multiplier: float = 2.0  # Doubles each attempt
    jitter: bool = True              # Adds ±20% random spread


@dataclass
class ConnectionStats:
    """Tracks connect/disconnect history for the session summary."""
    total_connects: int = 0
    total_disconnects: int = 0
    total_reconnects: int = 0
    failed_reconnects: int = 0
    last_connected_at: datetime | None = None
    last_disconnected_at: datetime | None = None
    current_session_start: datetime | None = None
    total_uptime_seconds: float = 0.0

    def record_connect(self) -> None:
        self.total_connects += 1
        self.last_connected_at = datetime.now()
        self.current_session_start = datetime.now()

    def record_disconnect(self) -> None:
        self.total_disconnects += 1
        self.last_disconnected_at = datetime.now()
        if self.current_session_start:
            duration = (datetime.now() - self.current_session_start).total_seconds()
            self.total_uptime_seconds += duration
            self.current_session_start = None

    def record_reconnect(self, success: bool) -> None:
        self.total_reconnects += 1
        if not success:
            self.failed_reconnects += 1

    def get_summary(self) -> dict[str, Any]:
        return {
            "total_connects": self.total_connects,
            "total_disconnects": self.total_disconnects,
            "total_reconnects": self.total_reconnects,
            "failed_reconnects": self.failed_reconnects,
            "success_rate": (
                (self.total_reconnects - self.failed_reconnects) / self.total_reconnects
                if self.total_reconnects > 0 else 1.0
            ),
            "total_uptime_seconds": self.total_uptime_seconds,
        }


StateChangeCallback = Callable[[ConnectionState, ConnectionState], Coroutine[Any, Any, None]]


class ReconnectManager:
    def __init__(
        self,
        config: ReconnectConfig | None = None,
        on_state_change: StateChangeCallback | None = None,
    ):
        self.config = config or ReconnectConfig()
        self.on_state_change = on_state_change  # Called every time state transitions
        self._state = ConnectionState.DISCONNECTED
        self._retry_count = 0
        self._stats = ConnectionStats()
        self._should_reconnect = True  # Set to False to prevent any further retries

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def stats(self) -> ConnectionStats:
        return self._stats

    @property
    def can_retry(self) -> bool:
        return self._retry_count < self.config.max_retries and self._should_reconnect

    async def _set_state(self, new_state: ConnectionState) -> None:
        if new_state != self._state:
            old_state = self._state
            self._state = new_state
            print(f"[Connection] State: {old_state.value} → {new_state.value}")
            if self.on_state_change:
                await self.on_state_change(old_state, new_state)

    def _calculate_delay(self) -> float:
        """Compute the wait time before the next retry (in seconds)."""
        delay_ms = min(
            self.config.initial_delay_ms * (self.config.backoff_multiplier ** self._retry_count),
            self.config.max_delay_ms
        )

        if self.config.jitter:
            jitter_range = delay_ms * 0.2
            delay_ms += random.uniform(-jitter_range, jitter_range)

        return delay_ms / 1000

    async def on_connect_success(self) -> None:
        await self._set_state(ConnectionState.CONNECTED)
        self._retry_count = 0  # Reset so the next disconnect starts from attempt 1
        self._stats.record_connect()

    async def on_disconnect(self, error: Exception | None = None) -> None:
        self._stats.record_disconnect()

        if error:
            print(f"[Connection] Disconnected with error: {error}")
        else:
            print("[Connection] Disconnected normally")

        if self._should_reconnect and self.can_retry:
            await self._set_state(ConnectionState.RECONNECTING)
        else:
            await self._set_state(ConnectionState.DISCONNECTED)

    async def attempt_reconnect(
        self,
        connect_fn: Callable[[], Coroutine[Any, Any, bool]],
    ) -> bool:
        """Make a single reconnect attempt after the backoff delay."""
        if not self.can_retry:
            await self._set_state(ConnectionState.FAILED)
            return False

        self._retry_count += 1
        delay = self._calculate_delay()

        print(f"[Connection] Reconnect attempt {self._retry_count}/{self.config.max_retries} in {delay:.1f}s")
        await asyncio.sleep(delay)

        await self._set_state(ConnectionState.CONNECTING)

        try:
            success = await connect_fn()
            self._stats.record_reconnect(success)

            if success:
                await self.on_connect_success()
                return True
            else:
                await self._set_state(ConnectionState.RECONNECTING)
                return False

        except Exception as e:
            print(f"[Connection] Reconnect failed: {e}")
            self._stats.record_reconnect(False)
            await self._set_state(ConnectionState.RECONNECTING)
            return False

    async def reconnect_loop(
        self,
        connect_fn: Callable[[], Coroutine[Any, Any, bool]],
    ) -> bool:
        """Keep retrying until success or max_retries is exhausted."""
        while self.can_retry:
            if await self.attempt_reconnect(connect_fn):
                return True

        await self._set_state(ConnectionState.FAILED)
        print(f"[Connection] All {self.config.max_retries} reconnect attempts failed")
        return False

    def reset(self) -> None:
        self._retry_count = 0
        self._should_reconnect = True

    def disable_reconnect(self) -> None:
        """Call this on non-recoverable errors (e.g. authentication failure)."""
        self._should_reconnect = False

    def enable_reconnect(self) -> None:
        self._should_reconnect = True
