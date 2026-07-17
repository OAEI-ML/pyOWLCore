"""Bounded, immutable progress reporting primitives."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, TypeAlias, runtime_checkable

from ._immutable import FrozenMap, freeze_mapping

ProgressScalar: TypeAlias = str | int | float | bool


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    stage: str
    completed: int
    total: int | None = None
    unit: str = "items"
    message: str | None = None
    details: Mapping[str, ProgressScalar] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        if not isinstance(self.stage, str) or not self.stage:
            raise ValueError("stage must be a nonempty string")
        if isinstance(self.completed, bool) or not isinstance(self.completed, int):
            raise TypeError("completed must be an integer")
        if self.completed < 0:
            raise ValueError("completed must be nonnegative")
        if self.total is not None:
            if isinstance(self.total, bool) or not isinstance(self.total, int):
                raise TypeError("total must be an integer or None")
            if self.total < 0:
                raise ValueError("total must be nonnegative")
            if self.completed > self.total:
                raise ValueError("completed must not exceed total")
        if not isinstance(self.unit, str) or not self.unit:
            raise ValueError("unit must be a nonempty string")
        if self.message is not None and not isinstance(self.message, str):
            raise TypeError("message must be a string or None")
        clean: dict[str, ProgressScalar] = {}
        for key, value in self.details.items():
            if not isinstance(key, str) or not key:
                raise TypeError("progress detail keys must be nonempty strings")
            if isinstance(value, (str, int, float, bool)):
                clean[key] = value
            else:
                raise TypeError("progress detail values must be scalar")
        object.__setattr__(self, "details", freeze_mapping(clean))


@runtime_checkable
class ProgressReporter(Protocol):
    def __call__(self, event: ProgressEvent, /) -> None: ...


ProgressCallback: TypeAlias = Callable[[ProgressEvent], None]


class ProgressBuffer:
    """A thread-safe reporter retaining only the newest bounded events."""

    __slots__ = ("_events", "_limit", "_lock")

    def __init__(self, limit: int = 256) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        self._limit = limit
        self._events: list[ProgressEvent] = []
        self._lock = threading.Lock()

    def __call__(self, event: ProgressEvent, /) -> None:
        if not isinstance(event, ProgressEvent):
            raise TypeError("event must be ProgressEvent")
        with self._lock:
            self._events.append(event)
            overflow = len(self._events) - self._limit
            if overflow > 0:
                del self._events[:overflow]

    def snapshot(self) -> tuple[ProgressEvent, ...]:
        with self._lock:
            return tuple(self._events)


def report_progress(
    reporter: ProgressReporter | None,
    event: ProgressEvent,
) -> None:
    """Invoke an optional reporter outside all core internal locks."""

    if reporter is not None:
        reporter(event)


__all__ = [
    "ProgressBuffer",
    "ProgressCallback",
    "ProgressEvent",
    "ProgressReporter",
    "ProgressScalar",
    "report_progress",
]
