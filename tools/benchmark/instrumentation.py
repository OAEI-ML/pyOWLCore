"""Core-operation counters and allocation evidence for no-copy benchmark claims."""

from __future__ import annotations

import tracemalloc
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from typing import Any, TypeVar, cast
from unittest.mock import patch

import pyowl_core.api as core_api
from pyowl_core.backends.python import PythonParser
from pyowl_core.document import (
    OntologyComposite,
    OntologyDocument,
    OntologyOverlay,
    OntologySnapshot,
)
from pyowl_core.document.snapshot import OntologyView
from pyowl_core.io.resolver import MappingResolver

T = TypeVar("T")


@dataclass(slots=True)
class OperationCounters:
    """Counts operations that must remain zero across an in-process handoff."""

    parser_calls: int = 0
    resolver_calls: int = 0
    wire_encode_calls: int = 0
    wire_decode_calls: int = 0
    wire_open_calls: int = 0
    document_constructions: int = 0
    snapshot_constructions: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)

    def assert_handoff_zero(self) -> None:
        nonzero = {name: value for name, value in self.as_dict().items() if value}
        if nonzero:
            details = ", ".join(f"{name}={value}" for name, value in sorted(nonzero.items()))
            raise AssertionError(f"ontology handoff repeated core work: {details}")


@dataclass(frozen=True, slots=True)
class AllocationResult:
    current_bytes: int
    peak_bytes: int

    def __post_init__(self) -> None:
        if self.current_bytes < 0 or self.peak_bytes < 0:
            raise ValueError("allocation deltas must not be negative")


@dataclass(frozen=True, slots=True)
class ArenaEvidence:
    view_kind: str
    leaf_ids: tuple[int, ...]
    expected_leaf_ids: tuple[int, ...]
    identity_preserved: bool
    current_bytes: int
    peak_bytes: int

    def as_dict(self) -> dict[str, str | bool | int | list[int]]:
        return {
            "view_kind": self.view_kind,
            "leaf_ids": list(self.leaf_ids),
            "expected_leaf_ids": list(self.expected_leaf_ids),
            "identity_preserved": self.identity_preserved,
            "current_bytes": self.current_bytes,
            "peak_bytes": self.peak_bytes,
        }


@contextmanager
def instrument_core_operations() -> Iterator[OperationCounters]:
    """Patch stable Python boundaries long enough to count repeated core work."""

    counters = OperationCounters()
    with ExitStack() as stack:
        _count_method(stack, PythonParser, "parse", counters, "parser_calls")
        _count_method(stack, MappingResolver, "resolve_outcome", counters, "resolver_calls")
        _count_method(
            stack,
            OntologyDocument,
            "__post_init__",
            counters,
            "document_constructions",
        )
        _count_method(
            stack,
            OntologySnapshot,
            "__post_init__",
            counters,
            "snapshot_constructions",
        )
        _count_function(stack, core_api, "encode_snapshot", counters, "wire_encode_calls")
        _count_function(stack, core_api, "decode_snapshot", counters, "wire_decode_calls")
        _count_function(stack, core_api, "open_snapshot", counters, "wire_open_calls")
        yield counters


def measure_allocations(operation: Callable[[], T]) -> tuple[T, AllocationResult]:
    """Measure Python tracked heap separately from gate wall-clock samples."""

    if tracemalloc.is_tracing():
        raise RuntimeError("allocation measurement cannot nest an active tracemalloc session")
    tracemalloc.start()
    before_current, _before_peak = tracemalloc.get_traced_memory()
    try:
        result = operation()
        after_current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, AllocationResult(
        current_bytes=max(0, after_current - before_current),
        peak_bytes=max(0, peak - before_current),
    )


def arena_evidence(
    view: OntologyView,
    expected_leaves: tuple[OntologyView, ...],
    allocations: AllocationResult,
) -> ArenaEvidence:
    """Prove an overlay/composite retains the exact resident base identities."""

    observed = leaf_arena_ids(view)
    expected = tuple(sorted(id(value) for value in expected_leaves))
    return ArenaEvidence(
        type(view).__name__,
        observed,
        expected,
        observed == expected,
        allocations.current_bytes,
        allocations.peak_bytes,
    )


def leaf_arena_ids(view: OntologyView) -> tuple[int, ...]:
    if isinstance(view, OntologyOverlay):
        return leaf_arena_ids(view.base)
    if isinstance(view, OntologyComposite):
        retained: set[int] = set()
        for member in view.members:
            retained.update(leaf_arena_ids(member.view))
        return tuple(sorted(retained))
    return (id(view),)


def _count_method(
    stack: ExitStack,
    owner: type[object],
    name: str,
    counters: OperationCounters,
    field: str,
) -> None:
    original = cast(Callable[..., Any], getattr(owner, name))

    def counted(*args: object, **kwargs: object) -> Any:
        setattr(counters, field, cast(int, getattr(counters, field)) + 1)
        return original(*args, **kwargs)

    stack.enter_context(patch.object(owner, name, counted))


def _count_function(
    stack: ExitStack,
    owner: object,
    name: str,
    counters: OperationCounters,
    field: str,
) -> None:
    original = cast(Callable[..., Any], getattr(owner, name))

    def counted(*args: object, **kwargs: object) -> Any:
        setattr(counters, field, cast(int, getattr(counters, field)) + 1)
        return original(*args, **kwargs)

    stack.enter_context(patch.object(owner, name, counted))


__all__ = [
    "AllocationResult",
    "ArenaEvidence",
    "OperationCounters",
    "arena_evidence",
    "instrument_core_operations",
    "leaf_arena_ids",
    "measure_allocations",
]
