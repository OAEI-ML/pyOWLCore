"""Backend selection with capability-first fallback and bounded warnings."""

from __future__ import annotations

import os
import threading
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pyowl_core.cancellation import CancellationToken
from pyowl_core.config import BackendPreference
from pyowl_core.exceptions import BackendUnavailableError, NativeBackendUnavailableWarning
from pyowl_core.limits import ParseLimits

from . import native

if TYPE_CHECKING:
    from pyowl_core.backends.native import _NativeRetainedFunctionalParseV2
    from pyowl_core.io.formats.common import ParsedOntology

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


def parse_functional_native(
    data: bytes,
    *,
    limits: ParseLimits,
    allow_swrl: bool,
    cancellation_token: CancellationToken | None,
) -> ParsedOntology:
    """Execute the selected native parser without a reverse backend dependency."""

    return native.parse_functional(
        data,
        limits=limits,
        allow_swrl=allow_swrl,
        cancellation_token=cancellation_token,
    )


def _parse_functional_native_retained_v2(
    data: bytes,
    *,
    limits: ParseLimits,
    allow_swrl: bool,
    collect_provenance: bool,
    cancellation_token: CancellationToken | None,
) -> _NativeRetainedFunctionalParseV2:
    """Execute the unadvertised parser-to-retained-arena construction seam."""

    return native._parse_functional_retained_v2(
        data,
        limits=limits,
        allow_swrl=allow_swrl,
        collect_provenance=collect_provenance,
        cancellation_token=cancellation_token,
    )


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


__all__ = [
    "BackendName",
    "BackendSelection",
    "parse_functional_native",
    "select_backend",
]
