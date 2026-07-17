"""Thread-safe cooperative cancellation and deadline primitives."""

from __future__ import annotations

import math
import threading
import time

from .exceptions import OperationCancelledError


class CancellationToken:
    """Read-only cancellation state safe to poll from worker threads."""

    __slots__ = ("_cancelled", "_deadline", "_lock", "_reason")

    def __init__(self, *, deadline_seconds: float | None = None) -> None:
        if deadline_seconds is not None:
            if isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, (int, float)):
                raise ValueError("deadline_seconds must be a positive finite number or None")
            if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
                raise ValueError("deadline_seconds must be a positive finite number or None")
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None
        self._deadline = None if deadline_seconds is None else time.monotonic() + deadline_seconds

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set() or self.deadline_exceeded

    @property
    def deadline_exceeded(self) -> bool:
        deadline = self._deadline
        return deadline is not None and time.monotonic() >= deadline

    @property
    def reason(self) -> str | None:
        with self._lock:
            reason = self._reason
        if reason is None and self.deadline_exceeded:
            return "deadline exceeded"
        return reason

    @property
    def remaining_seconds(self) -> float | None:
        deadline = self._deadline
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    def check(self) -> None:
        """Raise the public cancellation error if work should stop."""

        if self.cancelled:
            reason = self.reason
            raise OperationCancelledError(
                reason or "operation cancelled",
                reason=reason,
            )

    def _cancel(self, reason: str | None) -> bool:
        if reason is not None and (not isinstance(reason, str) or not reason):
            raise ValueError("reason must be a nonempty string or None")
        with self._lock:
            if self._cancelled.is_set():
                return False
            self._reason = reason
            self._cancelled.set()
            return True


class CancellationSource:
    """Owner that can cancel a token handed to another component."""

    __slots__ = ("_token",)

    def __init__(self, *, deadline_seconds: float | None = None) -> None:
        self._token = CancellationToken(deadline_seconds=deadline_seconds)

    @property
    def token(self) -> CancellationToken:
        return self._token

    def cancel(self, reason: str | None = None) -> bool:
        """Cancel once; return whether this call changed the state."""

        return self._token._cancel(reason)


__all__ = ["CancellationSource", "CancellationToken"]
