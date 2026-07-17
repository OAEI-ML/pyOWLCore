"""Dependency-free PYOCORE v1 framing oracle.

This file intentionally does not import ``pyowl_core.wire``. Keeping the
layout/digest implementation independent lets golden tests detect a shared
production encoder/decoder bug.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Mapping
from dataclasses import dataclass

_MAGIC = b"PYOCORE\x00"
_HEADER = struct.Struct("<8sHHIIIIIQQQ32sII")
_DIRECTORY = struct.Struct("<HHIQQQQ32s")
_HEADER_SIZE = 96
_DIRECTORY_SIZE = 72
_REQUIRED = frozenset(range(1, 15))
_KNOWN_TABLES = _REQUIRED | {0x8001, 0x8002}


@dataclass(frozen=True, slots=True)
class ReferenceEntry:
    kind: int
    flags: int
    schema: int
    offset: int
    stored_length: int
    decoded_length: int
    row_count: int
    digest: bytes


@dataclass(frozen=True, slots=True)
class ReferenceImage:
    minor: int
    feature_flags: int
    file_digest: bytes
    entries: tuple[ReferenceEntry, ...]
    sections: Mapping[int, bytes]


def read_wire(data: bytes) -> ReferenceImage:
    if not isinstance(data, bytes):
        raise TypeError("reference reader accepts exact bytes")
    if len(data) < _HEADER_SIZE:
        raise ValueError("truncated header")
    values = _HEADER.unpack_from(data)
    (
        magic,
        major,
        minor,
        header_size,
        feature_flags,
        count,
        model_schema,
        profile,
        total,
        directory_offset,
        directory_length,
        file_digest,
        header_crc,
        reserved,
    ) = values
    if magic != _MAGIC or major != 1 or header_size != 96:
        raise ValueError("unsupported header")
    if model_schema != 1 or profile != 1 or reserved != 0:
        raise ValueError("unsupported schema/profile/reserved field")
    if total != len(data) or directory_offset != 96 or directory_length != count * 72:
        raise ValueError("invalid total/directory bounds")
    if directory_offset + directory_length > len(data):
        raise ValueError("truncated directory")
    zeroed = bytearray(data[:96])
    zeroed[56:92] = bytes(36)
    if _crc32c(zeroed) != header_crc:
        raise ValueError("header CRC mismatch")
    entries: list[ReferenceEntry] = []
    sections: dict[int, bytes] = {}
    previous_kind = -1
    for index in range(count):
        entry = ReferenceEntry(*_DIRECTORY.unpack_from(data, 96 + index * 72))
        if entry.kind <= previous_kind or entry.flags not in (1, 2):
            raise ValueError("noncanonical directory")
        previous_kind = entry.kind
        if entry.offset % 8 or entry.offset < 96 + directory_length:
            raise ValueError("invalid section offset")
        end = entry.offset + entry.stored_length
        if end < entry.offset or end > len(data) or entry.decoded_length != entry.stored_length:
            raise ValueError("invalid section bounds")
        section = data[entry.offset:end]
        if hashlib.sha256(section).digest() != entry.digest:
            raise ValueError("section digest mismatch")
        if entry.kind in _KNOWN_TABLES:
            _validate_table(section, entry.row_count)
        entries.append(entry)
        sections[entry.kind] = section
    if {entry.kind for entry in entries if entry.flags == 1} != _REQUIRED:
        raise ValueError("required section mismatch")
    ordered = sorted(entries, key=lambda entry: entry.offset)
    cursor = 96 + directory_length
    for entry in ordered:
        if entry.offset < cursor or any(data[cursor : entry.offset]):
            raise ValueError("overlap or nonzero padding")
        cursor = entry.offset + entry.stored_length
    if cursor != len(data):
        raise ValueError("trailing bytes")
    digest = hashlib.sha256(data[:56] + bytes(36) + data[92:]).digest()
    if digest != file_digest:
        raise ValueError("file digest mismatch")
    return ReferenceImage(minor, feature_flags, file_digest, tuple(entries), sections)


def reencode(image: ReferenceImage) -> bytes:
    return encode_sections(
        image.sections,
        feature_flags=image.feature_flags,
        minor=image.minor,
    )


def encode_sections(
    sections: Mapping[int, bytes],
    *,
    feature_flags: int = 0,
    minor: int = 0,
) -> bytes:
    kinds = tuple(sorted(sections))
    if not _REQUIRED.issubset(kinds):
        raise ValueError("all required sections must be supplied")
    directory_length = len(kinds) * 72
    cursor = _align(96 + directory_length)
    entries: list[ReferenceEntry] = []
    for kind in kinds:
        section = sections[kind]
        rows = struct.unpack_from("<Q", section)[0] if kind in _KNOWN_TABLES else 0
        entries.append(
            ReferenceEntry(
                kind,
                1 if kind in _REQUIRED else 2,
                1,
                cursor,
                len(section),
                len(section),
                rows,
                hashlib.sha256(section).digest(),
            )
        )
        cursor = _align(cursor + len(section)) if kind != kinds[-1] else cursor + len(section)
    output = bytearray(cursor)
    for index, entry in enumerate(entries):
        _DIRECTORY.pack_into(
            output,
            96 + index * 72,
            entry.kind,
            entry.flags,
            entry.schema,
            entry.offset,
            entry.stored_length,
            entry.decoded_length,
            entry.row_count,
            entry.digest,
        )
        output[entry.offset : entry.offset + entry.stored_length] = sections[entry.kind]
    _HEADER.pack_into(
        output,
        0,
        _MAGIC,
        1,
        minor,
        96,
        feature_flags,
        len(entries),
        1,
        1,
        len(output),
        96,
        directory_length,
        bytes(32),
        0,
        0,
    )
    header = bytearray(output[:96])
    header[56:92] = bytes(36)
    struct.pack_into("<I", output, 88, _crc32c(header))
    output[56:88] = hashlib.sha256(output[:56] + bytes(36) + output[92:]).digest()
    return bytes(output)


def _validate_table(section: bytes, expected_count: int) -> None:
    if len(section) < 16:
        raise ValueError("truncated table")
    count = struct.unpack_from("<Q", section)[0]
    if count != expected_count or 8 * (count + 2) > len(section):
        raise ValueError("invalid table count")
    payload = len(section) - 8 * (count + 2)
    previous_offset = 0
    previous_row: bytes | None = None
    for index in range(count + 1):
        offset = struct.unpack_from("<Q", section, 8 + index * 8)[0]
        if offset < previous_offset or offset > payload or (index == 0 and offset != 0):
            raise ValueError("invalid table offsets")
        if index < count:
            end = struct.unpack_from("<Q", section, 16 + index * 8)[0]
            row = section[8 * (count + 2) + offset : 8 * (count + 2) + end]
            if previous_row is not None and row <= previous_row:
                raise ValueError("noncanonical table rows")
            previous_row = row
        previous_offset = offset
    if previous_offset != payload:
        raise ValueError("table payload coverage mismatch")


def _align(value: int) -> int:
    return (value + 7) & ~7


def _crc32c(data: bytes | bytearray) -> int:
    crc = 0xFFFF_FFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return crc ^ 0xFFFF_FFFF


__all__ = ["ReferenceEntry", "ReferenceImage", "encode_sections", "read_wire", "reencode"]
