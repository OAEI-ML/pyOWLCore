"""Immutable resource limits shared by all pyowl-core operations."""

from __future__ import annotations

import math
from dataclasses import dataclass, fields, replace
from typing import Any, cast

from .exceptions import ResourceLimitError


def _positive_integer(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _optional_positive_integer(name: str, value: object) -> None:
    if value is not None:
        _positive_integer(name, value)


@dataclass(frozen=True, slots=True)
class ParseLimits:
    """Resource budget propagated through parsing and every derived operation.

    The first group is the frozen public contract. The additional budgets make
    the security specification enforceable by later work packages without
    adding an unrelated options object. All counters are Python integers, so
    they cannot narrow or overflow silently.
    """

    max_source_bytes: int = 2 * 1024**3
    max_documents: int = 1_000
    max_total_source_bytes: int = 8 * 1024**3
    max_axioms: int = 100_000_000
    max_terms: int = 500_000_000
    max_nesting_depth: int = 512
    max_rdf_list_length: int = 10_000_000
    max_literal_bytes: int = 64 * 1024**2
    max_iri_bytes: int = 1024 * 1024
    max_prefixes: int = 1_000_000
    max_import_depth: int = 128
    max_redirects: int = 5
    max_diagnostics: int = 10_000
    max_memory_bytes: int | None = None
    deadline_seconds: float | None = None

    max_triples: int = 100_000_000
    max_strings: int = 500_000_000
    max_annotations: int = 100_000_000
    max_rule_atoms: int = 10_000_000
    max_sequence_arity: int = 10_000_000
    max_catalog_rewrites: int = 128
    max_resolver_attempts: int = 10_000
    max_concurrent_fetches: int = 8
    max_source_map_entries: int = 100_000_000
    max_origin_entries: int = 100_000_000
    max_overlay_depth: int = 32
    max_delta_entries: int = 10_000_000
    max_composite_members: int = 1_024
    max_index_rows: int = 500_000_000
    max_index_bytes: int = 16 * 1024**3
    max_wire_rows: int = 500_000_000
    max_wire_bytes: int = 16 * 1024**3
    max_temporary_bytes: int = 16 * 1024**3
    max_disk_cache_bytes: int = 64 * 1024**3
    max_decompressed_bytes: int = 8 * 1024**3
    max_canonical_work: int = 1_000_000_000
    cancellation_check_interval: int = 4_096

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name == "deadline_seconds":
                if value is not None:
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise ValueError(
                            "deadline_seconds must be a positive finite number or None"
                        )
                    if not math.isfinite(value) or value <= 0:
                        raise ValueError(
                            "deadline_seconds must be a positive finite number or None"
                        )
                continue
            if item.name == "max_memory_bytes":
                _optional_positive_integer(item.name, value)
            else:
                _positive_integer(item.name, value)

    def allowed(self, name: str) -> int | float | None:
        """Return a named budget, rejecting typos rather than hiding them."""

        if name not in {item.name for item in fields(self)}:
            raise KeyError(f"unknown resource limit: {name}")
        value = getattr(self, name)
        if value is None or isinstance(value, (int, float)):
            return value
        raise AssertionError("ParseLimits contains an invalid value")

    def enforce(self, name: str, observed: int | float) -> None:
        """Raise a sanitized error when ``observed`` exceeds ``name``."""

        if isinstance(observed, bool) or not isinstance(observed, (int, float)):
            raise TypeError("observed must be an integer or float")
        if observed < 0 or not math.isfinite(observed):
            raise ValueError("observed must be nonnegative and finite")
        allowed = self.allowed(name)
        if allowed is not None and observed > allowed:
            raise ResourceLimitError(
                f"resource limit {name} exceeded: observed={observed}, allowed={allowed}",
                limit=name,
                observed=observed,
                allowed=allowed,
            )

    def tightened_with(self, other: ParseLimits) -> ParseLimits:
        """Return the pairwise tighter budget without mutating either input."""

        if not isinstance(other, ParseLimits):
            raise TypeError("other must be ParseLimits")
        values: dict[str, int | float | None] = {}
        for item in fields(self):
            left = getattr(self, item.name)
            right = getattr(other, item.name)
            if left is None:
                values[item.name] = right
            elif right is None:
                values[item.name] = left
            else:
                values[item.name] = min(left, right)
        return replace(self, **cast(Any, values))


__all__ = ["ParseLimits"]
