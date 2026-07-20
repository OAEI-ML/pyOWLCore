"""Bounds-first binary framing shared by the production wire reader/writer."""

from __future__ import annotations

import hashlib
import struct
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from pyowl_core.cancellation import CancellationToken
from pyowl_core.exceptions import WireCorruptionError, WireLimitError, WireVersionError
from pyowl_core.limits import ParseLimits

from .schema import (
    ALIGNMENT,
    CANONICAL_PROFILE,
    DIRECTORY_ENTRY_SIZE,
    DIRECTORY_STRUCT,
    HEADER_SIZE,
    HEADER_STRUCT,
    KNOWN_FEATURE_FLAGS,
    KNOWN_OPTIONAL_SECTIONS,
    MAGIC,
    MAX_TABLE_ID,
    MODEL_SCHEMA,
    REQUIRED_SECTIONS,
    SECTION_OPTIONAL,
    SECTION_REQUIRED,
    SECTION_SCHEMAS,
    WIRE_MAJOR,
    SectionKind,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

_U64 = struct.Struct("<Q")


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    kind: int
    flags: int
    schema: int
    offset: int
    stored_length: int
    decoded_length: int
    row_count: int
    digest: bytes

    @property
    def end(self) -> int:
        return self.offset + self.stored_length


@dataclass(frozen=True, slots=True)
class WireHeader:
    minor: int
    feature_flags: int
    total_length: int
    directory_offset: int
    directory_length: int
    file_digest: bytes


class Guard:
    """Low-overhead cancellation/deadline checkpoints for bounded loops."""

    __slots__ = ("_interval", "_limits", "_next", "_started", "_token")

    def __init__(
        self,
        limits: ParseLimits,
        cancellation_token: CancellationToken | None,
    ) -> None:
        self._limits = limits
        self._token = cancellation_token
        self._started = time.monotonic()
        self._interval = limits.cancellation_check_interval
        self._next = self._interval

    def check(self, work: int = 0, *, force: bool = False) -> None:
        if not force and work < self._next:
            return
        if not force:
            while self._next <= work:
                self._next += self._interval
        if self._token is not None:
            self._token.check()
        deadline = self._limits.deadline_seconds
        if deadline is not None and time.monotonic() - self._started > deadline:
            raise WireLimitError("wire operation deadline exceeded", code="WIRE_DEADLINE")


class ByteWriter:
    """Small explicit-width writer used by section-specific schemas."""

    __slots__ = ("data",)

    def __init__(self) -> None:
        self.data = bytearray()

    def u8(self, value: int) -> None:
        self.data.extend(struct.pack("<B", value))

    def u16(self, value: int) -> None:
        self.data.extend(struct.pack("<H", value))

    def u32(self, value: int) -> None:
        self.data.extend(struct.pack("<I", value))

    def u64(self, value: int) -> None:
        self.data.extend(struct.pack("<Q", value))

    def raw(self, value: bytes) -> None:
        self.data.extend(value)

    def ids(self, values: Sequence[int]) -> None:
        self.u64(len(values))
        for value in values:
            self.u32(value)

    def finish(self) -> bytes:
        return bytes(self.data)


class ByteReader:
    """Bounds-checking reader that never trusts a count before checking bytes."""

    __slots__ = ("_data", "_offset", "_section")

    def __init__(self, data: bytes | memoryview, *, section: str) -> None:
        self._data = memoryview(data).cast("B")
        self._offset = 0
        self._section = section

    @property
    def remaining(self) -> int:
        return len(self._data) - self._offset

    def _take(self, size: int) -> memoryview:
        end = self._offset + size
        if size < 0 or end < self._offset or end > len(self._data):
            raise _corrupt(f"truncated {self._section} row")
        result = self._data[self._offset : end]
        self._offset = end
        return result

    def u8(self) -> int:
        return self._take(1)[0]

    def boolean(self) -> bool:
        value = self.u8()
        if value not in (0, 1):
            raise _corrupt(f"invalid boolean in {self._section}")
        return bool(value)

    def u16(self) -> int:
        return cast(int, struct.unpack_from("<H", self._take(2))[0])

    def u32(self) -> int:
        return cast(int, struct.unpack_from("<I", self._take(4))[0])

    def u64(self) -> int:
        return cast(int, struct.unpack_from("<Q", self._take(8))[0])

    def raw(self, size: int) -> bytes:
        return bytes(self._take(size))

    def ids(self, *, maximum: int) -> tuple[int, ...]:
        count = self.u64()
        if count > maximum:
            raise WireLimitError(
                f"wire row reference count exceeds limit in {self._section}",
                code="WIRE_ROW_LIMIT",
            )
        if count > self.remaining // 4:
            raise _corrupt(f"invalid reference count in {self._section}")
        return tuple(self.u32() for _ in range(count))

    def references(
        self,
        *,
        maximum: int,
        target_rows: int,
        collect: bool,
    ) -> tuple[int, ...]:
        """Read one strictly ascending nonzero posting list."""

        count = self.u64()
        if count > maximum:
            raise WireLimitError(
                f"wire row reference count exceeds limit in {self._section}",
                code="WIRE_ROW_LIMIT",
            )
        if count > self.remaining // 4:
            raise _corrupt(f"invalid reference count in {self._section}")
        result: list[int] | None = [] if collect else None
        previous = 0
        for _ in range(count):
            value = self.u32()
            if value <= previous or value > target_rows:
                raise _corrupt(f"invalid canonical reference list in {self._section}")
            previous = value
            if result is not None:
                result.append(value)
        return () if result is None else tuple(result)

    def finish(self) -> None:
        if self.remaining:
            raise _corrupt(f"trailing bytes in {self._section} row")


class TableView:
    """Validated offset table over an immutable wire buffer."""

    __slots__ = ("_data", "_payload", "count", "kind")

    def __init__(self, data: memoryview, entry: DirectoryEntry) -> None:
        self.kind = entry.kind
        self.count = entry.row_count
        self._data = data[entry.offset : entry.end]
        header_bytes = 8 * (entry.row_count + 2)
        self._payload = header_bytes

    def row(self, index: int) -> memoryview:
        if index < 0 or index >= self.count:
            raise IndexError(index)
        start = _U64.unpack_from(self._data, 8 + 8 * index)[0]
        end = _U64.unpack_from(self._data, 16 + 8 * index)[0]
        return self._data[self._payload + start : self._payload + end]

    def rows(self) -> Iterator[memoryview]:
        for index in range(self.count):
            yield self.row(index)

    def release(self) -> None:
        self._data.release()


@dataclass(slots=True)
class WireImage:
    """Validated framing plus lazy tables backed by caller-owned memory."""

    data: memoryview
    header: WireHeader
    entries: tuple[DirectoryEntry, ...]
    tables: Mapping[int, TableView]

    def table(self, kind: SectionKind) -> TableView:
        return self.tables[int(kind)]

    def release(self) -> None:
        for table in self.tables.values():
            table.release()
        self.data.release()


def encode_table(rows: Sequence[bytes]) -> bytes:
    """Encode canonical variable rows with u64 payload-relative offsets."""

    result = bytearray(8 * (len(rows) + 2))
    _U64.pack_into(result, 0, len(rows))
    offset = 0
    for index, row in enumerate(rows):
        _U64.pack_into(result, 8 + 8 * index, offset)
        offset += len(row)
    _U64.pack_into(result, 8 + 8 * len(rows), offset)
    for row in rows:
        result.extend(row)
    return bytes(result)


def validate_wire(
    data: bytes | bytearray | memoryview,
    *,
    limits: ParseLimits,
    verify: bool,
    cancellation_token: CancellationToken | None = None,
) -> WireImage:
    """Validate all generic framing/integrity before semantic allocation."""

    try:
        view = memoryview(data).cast("B")
    except (TypeError, ValueError) as error:
        raise TypeError("wire data must expose a contiguous byte buffer") from error
    guard = Guard(limits, cancellation_token)
    guard.check(force=True)
    if len(view) < HEADER_SIZE:
        raise _corrupt("wire file is shorter than the fixed header")
    limits_value = limits.max_wire_bytes
    if len(view) > limits_value:
        raise WireLimitError("wire file exceeds max_wire_bytes", code="WIRE_BYTE_LIMIT")
    unpacked = HEADER_STRUCT.unpack_from(view)
    (
        magic,
        major,
        minor,
        header_length,
        feature_flags,
        section_count,
        model_schema,
        canonical_profile,
        total_length,
        directory_offset,
        directory_length,
        file_digest,
        header_crc,
        reserved,
    ) = unpacked
    if magic != MAGIC:
        raise _corrupt("invalid PYOCORE magic")
    if major != WIRE_MAJOR:
        raise WireVersionError("unsupported PYOCORE major version", code="WIRE_MAJOR_VERSION")
    if header_length != HEADER_SIZE:
        raise WireVersionError("unsupported PYOCORE header layout", code="WIRE_HEADER_VERSION")
    if model_schema != MODEL_SCHEMA:
        raise WireVersionError("unsupported model schema", code="WIRE_MODEL_SCHEMA")
    if canonical_profile != CANONICAL_PROFILE:
        raise WireVersionError("unsupported canonical profile", code="WIRE_PROFILE_VERSION")
    if feature_flags & ~KNOWN_FEATURE_FLAGS:
        raise WireVersionError("unknown required wire feature flag", code="WIRE_FEATURE_VERSION")
    if reserved:
        raise WireVersionError("nonzero reserved wire header field", code="WIRE_RESERVED")
    if total_length != len(view):
        raise _corrupt("wire total length does not match the source")
    if section_count > limits.max_wire_rows or section_count > MAX_TABLE_ID:
        raise WireLimitError("wire section count exceeds limits", code="WIRE_SECTION_LIMIT")
    expected_directory = section_count * DIRECTORY_ENTRY_SIZE
    if expected_directory != directory_length:
        raise _corrupt("wire directory length/count mismatch")
    if directory_offset != HEADER_SIZE:
        raise _corrupt("wire directory is not immediately after the header")
    directory_end = directory_offset + directory_length
    if directory_end < directory_offset or directory_end > total_length:
        raise _corrupt("wire directory exceeds file bounds")
    header_zeroed = bytearray(view[:HEADER_SIZE])
    header_zeroed[56:92] = bytes(36)
    if crc32c(header_zeroed) != header_crc:
        raise _corrupt("wire header CRC32C mismatch")
    entries: list[DirectoryEntry] = []
    previous_kind = -1
    required_seen: set[SectionKind] = set()
    for index in range(section_count):
        guard.check(index)
        values = DIRECTORY_STRUCT.unpack_from(
            view, directory_offset + index * DIRECTORY_ENTRY_SIZE
        )
        entry = DirectoryEntry(*values)
        if entry.kind <= previous_kind:
            raise _corrupt("wire section directory is not in strict kind order")
        previous_kind = entry.kind
        if entry.flags not in (SECTION_REQUIRED, SECTION_OPTIONAL):
            raise WireVersionError("unknown section flags", code="WIRE_SECTION_FLAGS")
        try:
            known_kind = SectionKind(entry.kind)
        except ValueError:
            known_kind = None
        if entry.flags == SECTION_REQUIRED:
            if known_kind not in REQUIRED_SECTIONS:
                raise WireVersionError(
                    "unknown required wire section", code="WIRE_REQUIRED_SECTION"
                )
            required_seen.add(known_kind)
        elif known_kind is not None and known_kind in REQUIRED_SECTIONS:
            raise WireVersionError(
                "required section marked optional", code="WIRE_REQUIRED_SECTION_FLAGS"
            )
        if known_kind is not None:
            supported_schema = SECTION_SCHEMAS[known_kind]
            if entry.schema != supported_schema and (
                entry.flags == SECTION_REQUIRED or known_kind in KNOWN_OPTIONAL_SECTIONS
            ):
                raise WireVersionError(
                    "unsupported wire section schema", code="WIRE_SECTION_SCHEMA"
                )
        if entry.decoded_length != entry.stored_length:
            raise WireVersionError(
                "compressed required data is unsupported in wire v1",
                code="WIRE_SECTION_ENCODING",
            )
        if entry.offset % ALIGNMENT:
            raise _corrupt("wire section offset is not 8-byte aligned")
        if entry.offset < directory_end:
            raise _corrupt("wire section overlaps header or directory")
        if entry.end < entry.offset or entry.end > total_length:
            raise _corrupt("wire section exceeds file bounds")
        if entry.row_count > limits.max_wire_rows or entry.row_count > MAX_TABLE_ID:
            raise WireLimitError("wire row count exceeds limits", code="WIRE_ROW_LIMIT")
        entries.append(entry)
    if required_seen != set(REQUIRED_SECTIONS):
        raise WireVersionError("missing required wire section", code="WIRE_REQUIRED_SECTION")
    by_offset = sorted(entries, key=lambda item: item.offset)
    cursor = directory_end
    for index, entry in enumerate(by_offset):
        guard.check(index)
        if entry.offset < cursor:
            raise _corrupt("wire sections overlap")
        if any(view[cursor : entry.offset]):
            raise _corrupt("wire alignment padding is not zero")
        cursor = entry.end
    if cursor != total_length and any(view[cursor:]):
        raise _corrupt("wire trailing padding is not zero")
    tables: dict[int, TableView] = {}
    for index, entry in enumerate(entries):
        guard.check(index)
        section = view[entry.offset : entry.end]
        if hashlib.sha256(section).digest() != entry.digest:
            raise _corrupt("wire section SHA-256 mismatch")
        try:
            known_kind = SectionKind(entry.kind)
        except ValueError:
            continue
        if known_kind not in REQUIRED_SECTIONS and known_kind not in KNOWN_OPTIONAL_SECTIONS:
            continue
        _validate_table(section, entry, guard)
        tables[entry.kind] = TableView(view, entry)
    if verify:
        hasher = hashlib.sha256()
        hasher.update(view[:56])
        hasher.update(bytes(36))
        hasher.update(view[92:])
        if hasher.digest() != file_digest:
            raise _corrupt("wire file SHA-256 mismatch")
    guard.check(force=True)
    return WireImage(
        view,
        WireHeader(
            minor,
            feature_flags,
            total_length,
            directory_offset,
            directory_length,
            file_digest,
        ),
        tuple(entries),
        tables,
    )


def _validate_table(section: memoryview, entry: DirectoryEntry, guard: Guard) -> None:
    count = entry.row_count
    if len(section) < 16:
        raise _corrupt("wire table is shorter than its offset header")
    encoded_count = _U64.unpack_from(section, 0)[0]
    if encoded_count != count:
        raise _corrupt("wire table row count disagrees with directory")
    header_bytes = 8 * (count + 2)
    if header_bytes > len(section):
        raise _corrupt("wire table offset array exceeds section bounds")
    payload_size = len(section) - header_bytes
    previous_offset = 0
    previous_row: bytes | None = None
    for index in range(count + 1):
        guard.check(index)
        offset = _U64.unpack_from(section, 8 + 8 * index)[0]
        if index == 0 and offset != 0:
            raise _corrupt("wire table first row offset is not zero")
        if offset < previous_offset or offset > payload_size:
            raise _corrupt("wire table row offsets are invalid")
        if index < count:
            next_offset = _U64.unpack_from(section, 16 + 8 * index)[0]
            if next_offset < offset:
                raise _corrupt("wire table contains a reversed row slice")
            # A one-row table is already canonical by construction.  Avoid an
            # ontology-sized temporary copy for mmap-backed bulk-column rows.
            if count > 1:
                current = bytes(section[header_bytes + offset : header_bytes + next_offset])
                if previous_row is not None and current <= previous_row:
                    raise _corrupt("wire table rows are not strictly canonical")
                previous_row = current
        previous_offset = offset
    if previous_offset != payload_size:
        raise _corrupt("wire table offsets do not exactly cover the payload")


def crc32c(data: bytes | bytearray | memoryview) -> int:
    """Return Castagnoli CRC32C without an optional native dependency."""

    crc = 0xFFFF_FFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return crc ^ 0xFFFF_FFFF


def _corrupt(message: str) -> WireCorruptionError:
    return WireCorruptionError(message, code="WIRE_CORRUPTION")


__all__ = [
    "ByteReader",
    "ByteWriter",
    "DirectoryEntry",
    "Guard",
    "TableView",
    "WireHeader",
    "WireImage",
    "crc32c",
    "encode_table",
    "validate_wire",
]
