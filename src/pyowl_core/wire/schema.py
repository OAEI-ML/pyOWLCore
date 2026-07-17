"""Frozen PYOCORE wire-v1 tags and fixed layouts.

This module is deliberately boring: values here are part of the language-
neutral IPC contract and must never be assigned from Python enum order.
"""

from __future__ import annotations

import struct
from enum import IntEnum

MAGIC = b"PYOCORE\x00"
WIRE_MAJOR = 1
WIRE_MINOR = 1
MODEL_SCHEMA = 1
CANONICAL_PROFILE = 1
HEADER_SIZE = 96
DIRECTORY_ENTRY_SIZE = 72
ALIGNMENT = 8

HEADER_STRUCT = struct.Struct("<8sHHIIIIIQQQ32sII")
DIRECTORY_STRUCT = struct.Struct("<HHIQQQQ32s")

SECTION_REQUIRED = 0x0001
SECTION_OPTIONAL = 0x0002

FEATURE_SWRL = 0x0001
KNOWN_FEATURE_FLAGS = FEATURE_SWRL

MAX_SECTION_ID = 0xFFFF
MAX_TABLE_ID = 0xFFFF_FFFF


class SectionKind(IntEnum):
    """Stable section-kind ledger for PYOCORE v1."""

    STRINGS = 1
    IRIS = 2
    ENTITIES = 3
    LITERALS = 4
    ANONYMOUS = 5
    SEQUENCES = 6
    ANNOTATIONS = 7
    TERMS = 8
    AXIOMS = 9
    DOCUMENTS = 10
    IMPORTS = 11
    VIEW = 12
    ORIGINS = 13
    FOOTER = 14
    SWRL = 0x8001
    VIEW_PROVENANCE = 0x8002


REQUIRED_SECTIONS = tuple(SectionKind(value) for value in range(1, 15))
KNOWN_OPTIONAL_SECTIONS = frozenset((SectionKind.SWRL, SectionKind.VIEW_PROVENANCE))

SECTION_SCHEMAS = {kind: 1 for kind in (*REQUIRED_SECTIONS, *KNOWN_OPTIONAL_SECTIONS)}


__all__ = [
    "ALIGNMENT",
    "CANONICAL_PROFILE",
    "DIRECTORY_ENTRY_SIZE",
    "DIRECTORY_STRUCT",
    "FEATURE_SWRL",
    "HEADER_SIZE",
    "HEADER_STRUCT",
    "KNOWN_FEATURE_FLAGS",
    "KNOWN_OPTIONAL_SECTIONS",
    "MAGIC",
    "MAX_TABLE_ID",
    "MODEL_SCHEMA",
    "REQUIRED_SECTIONS",
    "SECTION_OPTIONAL",
    "SECTION_REQUIRED",
    "SECTION_SCHEMAS",
    "WIRE_MAJOR",
    "WIRE_MINOR",
    "SectionKind",
]
