"""Backend selection with capability-first fallback and bounded warnings."""

from __future__ import annotations

import os
import threading
import warnings
from dataclasses import dataclass
from typing import Literal

from pyowl_core.config import BackendPreference
from pyowl_core.exceptions import BackendUnavailableError, NativeBackendUnavailableWarning

from . import native

BackendName = Literal["python", "native"]


@dataclass(frozen=True, slots=True)
class BackendSelection:
    backend: BackendName
    capability: str
    native_version: str | None = None


_warning_lock = threading.Lock()
_warning_pid = os.getpid()
_warned_reasons: set[str] = set()


def _after_fork_child() -> None:
    global _warning_lock, _warning_pid
    _warning_lock = threading.Lock()
    _warning_pid = os.getpid()
    _warned_reasons.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)


def select_backend(
    preference: BackendPreference,
    *,
    capability: str,
    operation: str,
) -> BackendSelection:
    """Select exactly once before work for one complete operation."""

    if not isinstance(preference, BackendPreference):
        raise TypeError("preference must be BackendPreference")
    if not isinstance(capability, str) or not capability:
        raise ValueError("capability must be a nonempty string")
    if not isinstance(operation, str) or not operation:
        raise ValueError("operation must be a nonempty string")
    if preference is BackendPreference.PYTHON:
        return BackendSelection("python", capability)
    result = native.probe(capability)
    if result.available:
        return BackendSelection("native", capability, result.version)
    reason = result.reason or "native backend compatibility check failed"
    if preference is BackendPreference.NATIVE:
        raise BackendUnavailableError(
            f"native backend cannot perform {operation}: {reason}",
            code=(
                "NATIVE_CAPABILITY_UNAVAILABLE"
                if result.version is not None
                else "NATIVE_BACKEND_UNAVAILABLE"
            ),
        )
    _warn_once(reason, operation)
    return BackendSelection("python", capability)


def _warn_once(reason: str, operation: str) -> None:
    global _warning_pid
    with _warning_lock:
        pid = os.getpid()
        if pid != _warning_pid:
            _warning_pid = pid
            _warned_reasons.clear()
        if reason in _warned_reasons:
            return
        _warned_reasons.add(reason)
    warnings.warn(
        f"{operation} selected the complete Python backend because {reason}; "
        "install a compatible pyowl-core native wheel or explicitly request "
        "BackendPreference.PYTHON",
        NativeBackendUnavailableWarning,
        stacklevel=3,
    )


def _reset_warnings_for_tests() -> None:
    global _warning_pid
    with _warning_lock:
        _warning_pid = os.getpid()
        _warned_reasons.clear()


__all__ = ["BackendName", "BackendSelection", "select_backend"]
