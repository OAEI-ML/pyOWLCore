"""Stable PYOCORE snapshot handoff, mapping, and cache facade."""

from .cache import (
    CacheEntry,
    CacheGCReport,
    DurabilityPolicy,
    WireCache,
    write_snapshot,
)
from .codec import decode_snapshot, encode_snapshot
from .mapping import MappedOntologySnapshot, open_snapshot
from .schema import SectionKind

__all__ = [
    "CacheEntry",
    "CacheGCReport",
    "DurabilityPolicy",
    "MappedOntologySnapshot",
    "SectionKind",
    "WireCache",
    "decode_snapshot",
    "encode_snapshot",
    "open_snapshot",
    "write_snapshot",
]
