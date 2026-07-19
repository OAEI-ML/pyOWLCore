"""Authoritative private ABI for the optional native extension."""

from __future__ import annotations

from typing import Final

ABI_VERSION: Final[int]
MODEL_SCHEMA_VERSION: Final[int]
WIRE_FORMAT_VERSION: Final[tuple[int, int]]
FEATURES: Final[tuple[str, ...]]
INGESTION_FEATURES: Final[tuple[str, ...]]
VIEW_FEATURES: Final[tuple[str, ...]]

class _NativeError(Exception): ...

class _Cancellation:
    def __init__(self, deadline_seconds: float | None = None) -> None: ...
    @property
    def cancelled(self) -> bool: ...
    def cancel(self) -> bool: ...

def version() -> tuple[str, int]: ...
def self_test() -> None: ...
def validate_canonical(
    canonical: object,
    config: object,
    cancel: _Cancellation | None = None,
) -> bytes: ...
def validate_wire(
    snapshot_wire: object,
    config: object,
    cancel: _Cancellation | None = None,
) -> bytes: ...
def roundtrip_wire(
    snapshot_wire: object,
    config: object,
    cancel: _Cancellation | None = None,
) -> bytes: ...
def parse_document(
    source: object,
    config: object,
    cancel: _Cancellation | None = None,
) -> bytes: ...
def build_snapshot(documents: object, config: bytes, cancel: object) -> bytes: ...
def build_index(
    snapshot_wire: object,
    request: object,
    cancel: _Cancellation | None = None,
) -> bytes: ...
def _work_probe(iterations: int, config: object, cancel: _Cancellation | None = None) -> int: ...
def _panic_probe() -> None: ...
