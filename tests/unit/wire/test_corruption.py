from __future__ import annotations

import hashlib
import struct

import pytest

from pyowl_core import (
    ParseLimits,
    WireCorruptionError,
    WireLimitError,
    WireVersionError,
    decode_snapshot,
    encode_snapshot,
)

from .conftest import snapshot


def _resign(value: bytearray) -> bytes:
    header = bytearray(value[:96])
    header[56:92] = bytes(36)
    struct.pack_into("<I", value, 88, _crc32c(header))
    value[56:88] = hashlib.sha256(value[:56] + bytes(36) + value[92:]).digest()
    return bytes(value)


def _crc32c(data: bytes | bytearray) -> int:
    crc = 0xFFFF_FFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return crc ^ 0xFFFF_FFFF


def test_all_truncation_points_fail_with_typed_wire_error() -> None:
    encoded = encode_snapshot(snapshot())
    for length in range(len(encoded)):
        with pytest.raises(WireCorruptionError):
            decode_snapshot(encoded[:length])


@pytest.mark.parametrize("offset", (0, 55, 96, 167, -1))
def test_payload_and_directory_corruption_is_rejected(offset: int) -> None:
    encoded = bytearray(encode_snapshot(snapshot("A")))
    encoded[offset] ^= 0x40
    with pytest.raises((WireCorruptionError, WireVersionError)):
        decode_snapshot(encoded)


def test_unknown_major_model_schema_required_section_and_flags_fail_as_version_errors() -> None:
    original = encode_snapshot(snapshot("A"))
    cases: list[bytearray] = []
    major = bytearray(original)
    struct.pack_into("<H", major, 8, 2)
    cases.append(major)
    model = bytearray(original)
    struct.pack_into("<I", model, 24, 2)
    cases.append(model)
    flags = bytearray(original)
    struct.pack_into("<I", flags, 16, 0x8000_0000)
    cases.append(flags)
    required = bytearray(original)
    section_count = struct.unpack_from("<I", required, 20)[0]
    last_directory = 96 + (section_count - 1) * 72
    struct.pack_into("<H", required, last_directory, 60_000)
    cases.append(required)
    for case in cases:
        with pytest.raises(WireVersionError):
            decode_snapshot(_resign(case))


def test_limits_are_checked_before_table_allocation() -> None:
    encoded = encode_snapshot(snapshot("A"))
    with pytest.raises(WireLimitError):
        decode_snapshot(encoded, limits=ParseLimits(max_wire_bytes=len(encoded) - 1))

    hostile = bytearray(encoded)
    # First directory row's count field is at +32; its section digest/file
    # digest do not need recomputing because count validation precedes them.
    struct.pack_into("<Q", hostile, 96 + 32, 500_000_001)
    with pytest.raises(WireLimitError):
        decode_snapshot(hostile)
