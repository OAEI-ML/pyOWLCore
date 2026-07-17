"""Independent minimal PYOCORE v1 framing validator for publication.

No production schema/codec helper is imported here. The duplicated constants
and digest arithmetic are intentional: atomic publication uses this as a
second implementation, while semantic decode remains the codec's job.
"""

from __future__ import annotations

import hashlib
import mmap
import os
import struct
from dataclasses import dataclass
from pathlib import Path

from pyowl_core.exceptions import WireCorruptionError, WireVersionError

_HEADER = struct.Struct("<8sHHIIIIIQQQ32sII")
_DIRECTORY = struct.Struct("<HHIQQQQ32s")
_REQUIRED = frozenset(range(1, 15))
_KNOWN_TABLES = _REQUIRED | {0x8001, 0x8002}


@dataclass(frozen=True, slots=True)
class ReferenceValidation:
    length: int
    file_digest: bytes
    section_count: int


def validate_reference_file(path: str | os.PathLike[str]) -> ReferenceValidation:
    """Validate a completed file through the independent framing oracle."""

    selected = Path(os.fspath(path))
    fd = os.open(selected, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    mapping: mmap.mmap | None = None
    try:
        size = os.fstat(fd).st_size
        if size < 96:
            raise _corrupt("reference validator found a truncated header")
        mapping = mmap.mmap(fd, size, access=mmap.ACCESS_READ)
        with memoryview(mapping) as view:
            return validate_reference_buffer(view)
    finally:
        if mapping is not None:
            mapping.close()
        os.close(fd)


def validate_reference_buffer(data: memoryview) -> ReferenceValidation:
    if len(data) < 96:
        raise _corrupt("reference validator found a truncated header")
    (
        magic,
        major,
        _minor,
        header_size,
        _feature_flags,
        section_count,
        model_schema,
        profile,
        total_length,
        directory_offset,
        directory_length,
        file_digest,
        header_crc,
        reserved,
    ) = _HEADER.unpack_from(data)
    if magic != b"PYOCORE\0":
        raise _corrupt("reference validator found invalid magic")
    if major != 1 or header_size != 96 or model_schema != 1 or profile != 1 or reserved:
        raise WireVersionError(
            "reference validator found unsupported wire metadata",
            code="WIRE_REFERENCE_VERSION",
        )
    if total_length != len(data):
        raise _corrupt("reference validator total length mismatch")
    if directory_offset != 96 or directory_length != section_count * 72:
        raise _corrupt("reference validator directory mismatch")
    if 96 + directory_length > len(data):
        raise _corrupt("reference validator found a truncated directory")
    header = bytearray(data[:96])
    header[56:92] = bytes(36)
    if _crc32c(header) != header_crc:
        raise _corrupt("reference validator header CRC mismatch")
    entries: list[tuple[int, int, int, int, bytes]] = []
    required: set[int] = set()
    previous_kind = -1
    for index in range(section_count):
        (
            kind,
            flags,
            schema,
            offset,
            stored_length,
            decoded_length,
            row_count,
            digest,
        ) = _DIRECTORY.unpack_from(data, 96 + index * 72)
        if kind <= previous_kind or flags not in (1, 2):
            raise _corrupt("reference validator found a noncanonical directory")
        previous_kind = kind
        if flags == 1:
            if kind not in _REQUIRED:
                raise WireVersionError(
                    "reference validator found unknown required section",
                    code="WIRE_REFERENCE_REQUIRED",
                )
            required.add(kind)
        if schema != 1 and (flags == 1 or kind in _KNOWN_TABLES):
            raise WireVersionError(
                "reference validator found unsupported section schema",
                code="WIRE_REFERENCE_SCHEMA",
            )
        end = offset + stored_length
        if (
            offset % 8
            or offset < 96 + directory_length
            or end < offset
            or end > len(data)
            or decoded_length != stored_length
        ):
            raise _corrupt("reference validator found invalid section bounds")
        section = data[offset:end]
        if hashlib.sha256(section).digest() != digest:
            raise _corrupt("reference validator section digest mismatch")
        if kind in _KNOWN_TABLES:
            _validate_table(section, row_count)
        entries.append((offset, end, kind, row_count, digest))
    if required != _REQUIRED:
        raise WireVersionError(
            "reference validator required-section mismatch",
            code="WIRE_REFERENCE_REQUIRED",
        )
    cursor = 96 + directory_length
    for offset, end, _kind, _rows, _digest in sorted(entries):
        if offset < cursor or any(data[cursor:offset]):
            raise _corrupt("reference validator overlap/nonzero padding")
        cursor = end
    if cursor != len(data):
        raise _corrupt("reference validator trailing bytes")
    hasher = hashlib.sha256()
    hasher.update(data[:56])
    hasher.update(bytes(36))
    hasher.update(data[92:])
    if hasher.digest() != file_digest:
        raise _corrupt("reference validator file digest mismatch")
    return ReferenceValidation(len(data), file_digest, section_count)


def _validate_table(section: memoryview, expected_count: int) -> None:
    if len(section) < 16:
        raise _corrupt("reference validator found a truncated table")
    count = struct.unpack_from("<Q", section)[0]
    header_size = 8 * (count + 2)
    if count != expected_count or header_size > len(section):
        raise _corrupt("reference validator table count mismatch")
    payload_size = len(section) - header_size
    previous_offset = 0
    previous_row: bytes | None = None
    for index in range(count + 1):
        offset = struct.unpack_from("<Q", section, 8 + index * 8)[0]
        if offset < previous_offset or offset > payload_size or (index == 0 and offset):
            raise _corrupt("reference validator table offsets mismatch")
        if index < count:
            end = struct.unpack_from("<Q", section, 16 + index * 8)[0]
            row = bytes(section[header_size + offset : header_size + end])
            if previous_row is not None and row <= previous_row:
                raise _corrupt("reference validator table order mismatch")
            previous_row = row
        previous_offset = offset
    if previous_offset != payload_size:
        raise _corrupt("reference validator table coverage mismatch")


def _crc32c(data: bytes | bytearray) -> int:
    crc = 0xFFFF_FFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return crc ^ 0xFFFF_FFFF


def _corrupt(message: str) -> WireCorruptionError:
    return WireCorruptionError(message, code="WIRE_REFERENCE_CORRUPTION")


__all__ = ["ReferenceValidation", "validate_reference_buffer", "validate_reference_file"]
