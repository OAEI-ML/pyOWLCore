"""Small consumer-style decoder paired with the runtime trust boundary."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NoReturn, Protocol, cast

from pyowl_core.backends.native_views import validate_encoded_structural_view_v1
from pyowl_core.document.snapshot import AxiomScope, OntologyView
from pyowl_core.limits import ParseLimits

_SCHEMA_NAME = "pyowl-core/structural-columns"
_SCHEMA_VERSION = 1
_MODEL_SCHEMA = 1
_DESCRIPTOR_SHA256 = bytes.fromhex(
    "9ad29db6a7e616f65cea2957bc5ba8d1f9b99ef0eb1fe1432c09be25786267b5"
)

_BUFFER_WIDTHS = {
    "root_kinds": 1,
    "root_ids": 4,
    "node_tags": 2,
    "node_field_offsets": 8,
    "field_kinds": 1,
    "field_values": 8,
    "field_lengths": 8,
    "item_kinds": 1,
    "item_values": 8,
    "item_lengths": 8,
    "scalar_bytes": 1,
}

_DIRECT = 1
_OVERLAY_BASE = 2
_OVERLAY_DELTA = 3
_COMPOSITE_MEMBER = 4
_COMPOSITE_BRIDGE = 5

_ALL = 0
_INCLUDE = 1
_EXCLUDE = 2

_NONE = 0
_NODE = 1
_TEXT = 2
_BYTES = 3
_INTEGER = 4
_ENUM = 5
_SET = 6
_SEQUENCE = 7


class _Fingerprint(Protocol):
    algorithm: str
    schema: int
    digest: bytes


class _Segment(Protocol):
    role: int
    owner: object
    source: _View | None
    posting_mode: int
    root_ids: memoryview
    anonymous_scope_map: memoryview
    member_token: bytes | None


class _View(Protocol):
    schema_name: str
    schema_version: int
    model_schema: int
    owner: object
    buffers: Mapping[str, memoryview]
    descriptor: bytes
    structural_fingerprint: _Fingerprint
    segments: tuple[_Segment, ...]
    scope: object
    document_key: str | None


class IndependentSegmentError(ValueError):
    """Fail-closed error raised by the independent segmented decoder."""

    code: str

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class IndependentRootLocator:
    """Stable source-local root identity, qualified by composite member tokens."""

    member_tokens: tuple[bytes, ...]
    origin_fingerprint: bytes
    origin_scope: str
    origin_document_key: str | None
    local_root_id: int


@dataclass(frozen=True, slots=True)
class IndependentAnonymousIdentity:
    """Anonymous identity without rewriting a referenced member's canonical bytes."""

    member_tokens: tuple[bytes, ...]
    document_scope: bytes
    local_key: bytes


@dataclass(frozen=True, slots=True)
class IndependentLocatedRoot:
    locator: IndependentRootLocator
    source_locators: tuple[IndependentRootLocator, ...]
    root_kind: int
    canonical: bytes
    anonymous_identities: tuple[IndependentAnonymousIdentity, ...]


@dataclass(frozen=True, slots=True)
class IndependentDecodeProof:
    """Observable ownership/copy boundary for the consumer-style lane."""

    retained_views: tuple[object, ...]
    referenced_buffer_views: tuple[memoryview, ...]
    referenced_buffer_copy_bytes: int
    scalar_traversal_calls: int
    canonical_output_bytes: int


@dataclass(frozen=True, slots=True)
class IndependentSegmentDecode:
    roots: tuple[IndependentLocatedRoot, ...]
    proof: IndependentDecodeProof


@dataclass(frozen=True, slots=True)
class _AnonymousIdentity:
    document_scope: bytes
    local_key: bytes
    scope_offset: int


@dataclass(frozen=True, slots=True)
class _DecodedComponent:
    canonical: bytes
    anonymous_identities: tuple[_AnonymousIdentity, ...]
    scalar_payload: bytes | None = None
    scalar_payload_offset: int | None = None


@dataclass(frozen=True, slots=True)
class _LocalRoot:
    root_kind: int
    canonical: bytes
    anonymous_identities: tuple[_AnonymousIdentity, ...]


@dataclass(frozen=True, slots=True)
class _ResolvedRoot:
    located: IndependentLocatedRoot
    origin_view_id: int
    anonymous_occurrences: tuple[_AnonymousIdentity, ...]


class _Column:
    __slots__ = ("_data", "_name", "_width")

    def __init__(self, data: memoryview, width: int, name: str) -> None:
        if len(data) % width:
            _fail(f"misaligned {name} column", "INDEPENDENT_COLUMNS")
        self._data = data
        self._width = width
        self._name = name

    def __len__(self) -> int:
        return len(self._data) // self._width

    def __getitem__(self, index: int) -> int:
        if index < 0 or index >= len(self):
            _fail(f"out-of-range {self._name} row", "INDEPENDENT_COLUMNS")
        start = index * self._width
        return int.from_bytes(self._data[start : start + self._width], "little")


class _DecodeState:
    __slots__ = (
        "active",
        "cache",
        "referenced_buffer_views",
        "referenced_ids",
        "retained_ids",
        "retained_views",
    )

    def __init__(self) -> None:
        self.active: set[int] = set()
        self.cache: dict[int, tuple[_ResolvedRoot, ...]] = {}
        self.referenced_ids: set[int] = set()
        self.referenced_buffer_views: list[memoryview] = []
        self.retained_ids: set[int] = set()
        self.retained_views: list[object] = []

    def retain(self, view: _View) -> None:
        identity = id(view)
        if identity not in self.retained_ids:
            self.retained_ids.add(identity)
            self.retained_views.append(view)

    def reference(self, view: _View) -> None:
        identity = id(view)
        if identity in self.referenced_ids:
            return
        self.referenced_ids.add(identity)
        for name in _BUFFER_WIDTHS:
            self.referenced_buffer_views.append(view.buffers[name])


def _fail(message: str, code: str) -> NoReturn:
    raise IndependentSegmentError(message, code=code)


def _varint(value: int) -> bytes:
    output = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        output.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(output)


def _frame(value: bytes) -> bytes:
    return _varint(len(value)) + value


def _validate_buffer(name: str, value: object) -> memoryview:
    if type(value) is not memoryview:
        _fail(f"{name} is not an exact memoryview", "INDEPENDENT_COLUMNS")
    selected = cast(memoryview, value)
    if (
        not selected.readonly
        or not selected.c_contiguous
        or selected.ndim != 1
        or selected.itemsize != 1
        or selected.format != "B"
        or selected.shape != (len(selected),)
        or selected.strides != (1,)
    ):
        _fail(f"{name} is not a readonly contiguous byte view", "INDEPENDENT_COLUMNS")
    return selected


def _columns(buffers: Mapping[str, memoryview]) -> dict[str, _Column | memoryview]:
    if set(buffers) != set(_BUFFER_WIDTHS):
        _fail("encoded buffer names do not match schema v1", "INDEPENDENT_COLUMNS")
    result: dict[str, _Column | memoryview] = {}
    for name, width in _BUFFER_WIDTHS.items():
        value = _validate_buffer(name, buffers[name])
        result[name] = value if name == "scalar_bytes" else _Column(value, width, name)
    return result


def _deduplicate_anonymous(
    values: list[_AnonymousIdentity],
) -> tuple[_AnonymousIdentity, ...]:
    return tuple(
        sorted(
            set(values),
            key=lambda value: (value.scope_offset, value.document_scope, value.local_key),
        )
    )


def _shift_anonymous(
    values: tuple[_AnonymousIdentity, ...], offset: int
) -> tuple[_AnonymousIdentity, ...]:
    return tuple(
        _AnonymousIdentity(value.document_scope, value.local_key, value.scope_offset + offset)
        for value in values
    )


def _decode_local_roots(buffers: Mapping[str, memoryview]) -> tuple[_LocalRoot, ...]:
    columns = _columns(buffers)
    root_kinds = cast(_Column, columns["root_kinds"])
    root_ids = cast(_Column, columns["root_ids"])
    tags = cast(_Column, columns["node_tags"])
    field_offsets = cast(_Column, columns["node_field_offsets"])
    field_kinds = cast(_Column, columns["field_kinds"])
    field_values = cast(_Column, columns["field_values"])
    field_lengths = cast(_Column, columns["field_lengths"])
    item_kinds = cast(_Column, columns["item_kinds"])
    item_values = cast(_Column, columns["item_values"])
    item_lengths = cast(_Column, columns["item_lengths"])
    scalars = cast(memoryview, columns["scalar_bytes"])
    if len(root_kinds) != len(root_ids):
        _fail("root columns have different lengths", "INDEPENDENT_COLUMNS")
    if len(field_offsets) != len(tags) + 1:
        _fail("node field offsets do not cover nodes", "INDEPENDENT_COLUMNS")
    if not len(field_offsets) or field_offsets[0] != 0:
        _fail("node field offsets do not start at zero", "INDEPENDENT_COLUMNS")
    if field_offsets[len(field_offsets) - 1] != len(field_kinds):
        _fail("node field offsets do not end at field count", "INDEPENDENT_COLUMNS")
    if len(field_kinds) != len(field_values) or len(field_kinds) != len(field_lengths):
        _fail("field columns have different lengths", "INDEPENDENT_COLUMNS")
    if len(item_kinds) != len(item_values) or len(item_kinds) != len(item_lengths):
        _fail("item columns have different lengths", "INDEPENDENT_COLUMNS")

    memo: dict[int, _DecodedComponent] = {}
    active: set[int] = set()

    def scalar(start: int, length: int) -> bytes:
        end = start + length
        if end < start or end > len(scalars):
            _fail("scalar range exceeds scalar_bytes", "INDEPENDENT_COLUMNS")
        return bytes(scalars[start:end])

    def component(kind: int, value: int, length: int) -> _DecodedComponent:
        if kind == _NONE:
            if value or length:
                _fail("none component carries a payload", "INDEPENDENT_COLUMNS")
            return _DecodedComponent(b"\x00", ())
        if kind == _NODE:
            if length:
                _fail("node component carries a length", "INDEPENDENT_COLUMNS")
            child = node(value)
            prefix = b"\x01" + _varint(len(child.canonical))
            return _DecodedComponent(
                prefix + child.canonical,
                _shift_anonymous(child.anonymous_identities, len(prefix)),
            )
        if kind in {_TEXT, _BYTES, _ENUM}:
            payload = scalar(value, length)
            prefix = bytes((kind,)) + _varint(len(payload))
            return _DecodedComponent(prefix + payload, (), payload, len(prefix))
        if kind == _INTEGER:
            payload = scalar(value, length)
            if not payload:
                _fail("integer component has no payload", "INDEPENDENT_COLUMNS")
            integer = int.from_bytes(payload, "little")
            return _DecodedComponent(b"\x04" + _varint(integer), ())
        if kind not in {_SET, _SEQUENCE}:
            _fail("component kind is not in schema v1", "INDEPENDENT_COLUMNS")
        end = value + length
        if end < value or end > len(item_kinds):
            _fail("component item range exceeds item columns", "INDEPENDENT_COLUMNS")
        encoded = bytearray(bytes((_SET if kind == _SET else _SEQUENCE,)) + _varint(length))
        anonymous: list[_AnonymousIdentity] = []
        for index in range(value, end):
            if kind == _SET:
                if item_kinds[index] != _NODE or item_lengths[index]:
                    _fail("canonical-set item is not a node", "INDEPENDENT_COLUMNS")
                item = node(item_values[index])
                offset = len(encoded) + len(_varint(len(item.canonical)))
                encoded.extend(_frame(item.canonical))
            else:
                item = component(item_kinds[index], item_values[index], item_lengths[index])
                offset = len(encoded)
                encoded.extend(item.canonical)
            anonymous.extend(_shift_anonymous(item.anonymous_identities, offset))
        return _DecodedComponent(bytes(encoded), _deduplicate_anonymous(anonymous))

    def node(node_id: int) -> _DecodedComponent:
        cached = memo.get(node_id)
        if cached is not None:
            return cached
        if node_id <= 0 or node_id > len(tags):
            _fail("node ID is outside the dense table", "INDEPENDENT_COLUMNS")
        if node_id in active:
            _fail("node graph is cyclic", "INDEPENDENT_COLUMNS")
        active.add(node_id)
        try:
            index = node_id - 1
            start = field_offsets[index]
            end = field_offsets[index + 1]
            if end < start or end > len(field_kinds):
                _fail("node field range exceeds field columns", "INDEPENDENT_COLUMNS")
            fields: list[_DecodedComponent] = []
            field_starts: list[int] = []
            output = bytearray(_varint(tags[index]))
            anonymous: list[_AnonymousIdentity] = []
            for field_index in range(start, end):
                decoded = component(
                    field_kinds[field_index],
                    field_values[field_index],
                    field_lengths[field_index],
                )
                fields.append(decoded)
                field_starts.append(len(output))
                output.extend(decoded.canonical)
                anonymous.extend(
                    _shift_anonymous(decoded.anonymous_identities, field_starts[-1])
                )
            if tags[index] == 3:
                document_scope = fields[0].scalar_payload if len(fields) == 2 else None
                local_key = fields[1].scalar_payload if len(fields) == 2 else None
                scope_payload_offset = (
                    fields[0].scalar_payload_offset if len(fields) == 2 else None
                )
                if (
                    len(fields) != 2
                    or document_scope is None
                    or local_key is None
                    or scope_payload_offset is None
                ):
                    _fail(
                        "anonymous individual does not expose scope and local key",
                        "INDEPENDENT_COLUMNS",
                    )
                anonymous.append(
                    _AnonymousIdentity(
                        document_scope,
                        local_key,
                        field_starts[0] + scope_payload_offset,
                    )
                )
            decoded_node = _DecodedComponent(
                bytes(output), _deduplicate_anonymous(anonymous)
            )
            memo[node_id] = decoded_node
            return decoded_node
        finally:
            active.remove(node_id)

    roots: list[_LocalRoot] = []
    for index in range(len(root_ids)):
        decoded = node(root_ids[index])
        roots.append(_LocalRoot(root_kinds[index], decoded.canonical, decoded.anonymous_identities))
    return tuple(roots)


def decode_root_canonical_bytes(
    buffers: Mapping[str, memoryview],
) -> tuple[tuple[int, bytes], ...]:
    """Decode only the documented columns into canonical-model-v1 root bytes."""

    return tuple((root.root_kind, root.canonical) for root in _decode_local_roots(buffers))


def _view_metadata(view: _View) -> tuple[bytes, str, str | None]:
    try:
        schema_name = view.schema_name
        schema_version = view.schema_version
        model_schema = view.model_schema
        descriptor = view.descriptor
        fingerprint = view.structural_fingerprint
        raw_scope = view.scope
        document_key = view.document_key
    except Exception as error:
        raise IndependentSegmentError(
            "encoded view metadata is not readable", code="INDEPENDENT_DESCRIPTOR"
        ) from error
    if (
        type(schema_name) is not str
        or schema_name != _SCHEMA_NAME
        or type(schema_version) is not int
        or schema_version != _SCHEMA_VERSION
        or type(model_schema) is not int
        or model_schema != _MODEL_SCHEMA
        or type(descriptor) is not bytes
        or hashlib.sha256(descriptor).digest() != _DESCRIPTOR_SHA256
    ):
        _fail("encoded view does not match frozen schema v1", "INDEPENDENT_DESCRIPTOR")
    try:
        algorithm = fingerprint.algorithm
        fingerprint_schema = fingerprint.schema
        digest = fingerprint.digest
    except Exception as error:
        raise IndependentSegmentError(
            "encoded fingerprint is not readable", code="INDEPENDENT_DESCRIPTOR"
        ) from error
    if (
        algorithm != "sha256"
        or type(fingerprint_schema) is not int
        or fingerprint_schema != 1
        or type(digest) is not bytes
        or len(digest) != 32
    ):
        _fail("encoded fingerprint is invalid", "INDEPENDENT_DESCRIPTOR")
    scope_value = getattr(raw_scope, "value", raw_scope)
    if type(scope_value) is not str or scope_value not in {"root", "closure", "document"}:
        _fail("encoded view scope is invalid", "INDEPENDENT_DESCRIPTOR")
    if (scope_value == "document") != (type(document_key) is str and bool(document_key)):
        _fail("encoded view document selection is invalid", "INDEPENDENT_DESCRIPTOR")
    if scope_value != "document" and document_key is not None:
        _fail("encoded view document key is unexpected", "INDEPENDENT_DESCRIPTOR")
    return digest, scope_value, document_key


def _segment_fields(
    segment: object,
) -> tuple[int, object, _View | None, int, memoryview, memoryview, bytes | None]:
    selected = cast(_Segment, segment)
    try:
        role = selected.role
        owner = selected.owner
        source = selected.source
        posting_mode = selected.posting_mode
        root_ids = selected.root_ids
        anonymous_scope_map = selected.anonymous_scope_map
        member_token = selected.member_token
    except Exception as error:
        raise IndependentSegmentError(
            "encoded segment metadata is not readable", code="INDEPENDENT_SEGMENTS"
        ) from error
    if type(role) is not int or role not in {
        _DIRECT,
        _OVERLAY_BASE,
        _OVERLAY_DELTA,
        _COMPOSITE_MEMBER,
        _COMPOSITE_BRIDGE,
    }:
        _fail("encoded segment role is invalid", "INDEPENDENT_SEGMENTS")
    if type(posting_mode) is not int or posting_mode not in {_ALL, _INCLUDE, _EXCLUDE}:
        _fail("encoded posting mode is invalid", "INDEPENDENT_SEGMENTS")
    postings = _validate_buffer("segment.root_ids", root_ids)
    if len(postings) % 4:
        _fail("encoded postings are not u32 rows", "INDEPENDENT_SEGMENTS")
    scope_map = _validate_buffer("segment.anonymous_scope_map", anonymous_scope_map)
    if len(scope_map) % 64:
        _fail("anonymous scope map is not 64-byte rows", "INDEPENDENT_SEGMENTS")
    previous: bytes | None = None
    for offset in range(0, len(scope_map), 64):
        current = bytes(scope_map[offset : offset + 32])
        target = bytes(scope_map[offset + 32 : offset + 64])
        if (previous is not None and current <= previous) or current == target:
            _fail(
                "anonymous scope map is unsorted, duplicate, or identity",
                "INDEPENDENT_SEGMENTS",
            )
        previous = current
    return role, owner, source, posting_mode, postings, scope_map, member_token


def _posting_ids(postings: memoryview, mode: int, root_count: int) -> frozenset[int]:
    column = _Column(postings, 4, "segment.root_ids")
    if mode == _ALL:
        if len(column):
            _fail("ALL postings carry root IDs", "INDEPENDENT_SEGMENTS")
        return frozenset()
    if not len(column):
        _fail("INCLUDE/EXCLUDE postings are empty", "INDEPENDENT_SEGMENTS")
    values: list[int] = []
    previous = 0
    for index in range(len(column)):
        value = column[index]
        if value <= previous or value > root_count:
            _fail("postings are not sorted unique source-local IDs", "INDEPENDENT_SEGMENTS")
        values.append(value)
        previous = value
    return frozenset(values)


def _apply_postings(
    roots: tuple[_ResolvedRoot, ...],
    source: _View,
    mode: int,
    postings: memoryview,
) -> tuple[_ResolvedRoot, ...]:
    root_count = len(_Column(source.buffers["root_ids"], 4, "root_ids"))
    selected = _posting_ids(postings, mode, root_count)
    if mode == _ALL:
        return roots
    if mode == _INCLUDE:
        return tuple(
            root
            for root in roots
            if root.origin_view_id == id(source)
            and root.located.locator.local_root_id in selected
        )
    return tuple(
        root
        for root in roots
        if root.origin_view_id != id(source)
        or root.located.locator.local_root_id not in selected
    )


def _apply_scope_map(
    roots: tuple[_ResolvedRoot, ...], scope_map: memoryview
) -> tuple[_ResolvedRoot, ...]:
    if not len(scope_map):
        return roots
    replacements = {
        bytes(scope_map[offset : offset + 32]): bytes(scope_map[offset + 32 : offset + 64])
        for offset in range(0, len(scope_map), 64)
    }
    mapped: list[_ResolvedRoot] = []
    for root in roots:
        if not any(
            occurrence.document_scope in replacements
            for occurrence in root.anonymous_occurrences
        ):
            mapped.append(root)
            continue
        canonical = bytearray(root.located.canonical)
        occurrences: list[_AnonymousIdentity] = []
        for occurrence in root.anonymous_occurrences:
            target = replacements.get(occurrence.document_scope, occurrence.document_scope)
            start = occurrence.scope_offset
            end = start + len(occurrence.document_scope)
            if (
                len(occurrence.document_scope) != 32
                or len(target) != 32
                or bytes(canonical[start:end]) != occurrence.document_scope
            ):
                _fail(
                    "anonymous scope occurrence does not match canonical bytes",
                    "INDEPENDENT_SCOPE_MAP",
                )
            canonical[start:end] = target
            occurrences.append(
                _AnonymousIdentity(target, occurrence.local_key, occurrence.scope_offset)
            )
        located = root.located
        mapped.append(
            _ResolvedRoot(
                IndependentLocatedRoot(
                    located.locator,
                    located.source_locators,
                    located.root_kind,
                    bytes(canonical),
                    tuple(
                        IndependentAnonymousIdentity(
                            identity.member_tokens,
                            replacements.get(identity.document_scope, identity.document_scope),
                            identity.local_key,
                        )
                        for identity in located.anonymous_identities
                    ),
                ),
                root.origin_view_id,
                tuple(occurrences),
            )
        )
    return tuple(mapped)


def _prefix_member(root: _ResolvedRoot, token: bytes) -> _ResolvedRoot:
    located = root.located
    locator = located.locator
    identities = tuple(
        IndependentAnonymousIdentity(
            (token, *identity.member_tokens),
            identity.document_scope,
            identity.local_key,
        )
        for identity in located.anonymous_identities
    )
    return _ResolvedRoot(
        IndependentLocatedRoot(
            IndependentRootLocator(
                (token, *locator.member_tokens),
                locator.origin_fingerprint,
                locator.origin_scope,
                locator.origin_document_key,
                locator.local_root_id,
            ),
            tuple(
                IndependentRootLocator(
                    (token, *source_locator.member_tokens),
                    source_locator.origin_fingerprint,
                    source_locator.origin_scope,
                    source_locator.origin_document_key,
                    source_locator.local_root_id,
                )
                for source_locator in located.source_locators
            ),
            located.root_kind,
            located.canonical,
            identities,
        ),
        root.origin_view_id,
        root.anonymous_occurrences,
    )


def _root_key(root: _ResolvedRoot) -> tuple[object, ...]:
    locator = root.located.locator
    return (
        root.located.root_kind,
        root.located.canonical,
        locator.member_tokens,
        locator.origin_fingerprint,
        locator.origin_scope,
        "" if locator.origin_document_key is None else locator.origin_document_key,
        locator.local_root_id,
    )


def _reject_duplicate_locators(roots: tuple[_ResolvedRoot, ...]) -> None:
    seen: set[IndependentRootLocator] = set()
    for root in roots:
        if root.located.locator in seen:
            _fail("segmented view emitted a duplicate root locator", "INDEPENDENT_LOCATOR")
        seen.add(root.located.locator)


def _locator_key(locator: IndependentRootLocator) -> tuple[object, ...]:
    return (
        locator.member_tokens,
        locator.origin_fingerprint,
        locator.origin_scope,
        "" if locator.origin_document_key is None else locator.origin_document_key,
        locator.local_root_id,
    )


def _deduplicate_roots(
    roots: tuple[_ResolvedRoot, ...],
) -> tuple[IndependentLocatedRoot, ...]:
    grouped: dict[tuple[int, bytes], list[_ResolvedRoot]] = {}
    for root in roots:
        grouped.setdefault((root.located.root_kind, root.located.canonical), []).append(root)
    result: list[IndependentLocatedRoot] = []
    for (root_kind, canonical), occurrences in sorted(grouped.items()):
        locators = tuple(
            sorted(
                {
                    locator
                    for occurrence in occurrences
                    for locator in occurrence.located.source_locators
                },
                key=_locator_key,
            )
        )
        identities = tuple(
            sorted(
                {
                    identity
                    for occurrence in occurrences
                    for identity in occurrence.located.anonymous_identities
                },
                key=lambda identity: (
                    identity.member_tokens,
                    identity.document_scope,
                    identity.local_key,
                ),
            )
        )
        result.append(
            IndependentLocatedRoot(
                locators[0],
                locators,
                root_kind,
                canonical,
                identities,
            )
        )
    return tuple(result)


def _resolve(view: _View, state: _DecodeState, *, referenced: bool) -> tuple[_ResolvedRoot, ...]:
    identity = id(view)
    if identity in state.active:
        _fail("segmented view graph is cyclic", "INDEPENDENT_CYCLE")
    if referenced:
        state.reference(view)
    cached = state.cache.get(identity)
    if cached is not None:
        return cached
    state.retain(view)
    state.active.add(identity)
    try:
        digest, scope, document_key = _view_metadata(view)
        local = _decode_local_roots(view.buffers)
        local_roots = tuple(
            _ResolvedRoot(
                IndependentLocatedRoot(
                    locator := IndependentRootLocator(
                        (), digest, scope, document_key, index
                    ),
                    (locator,),
                    root.root_kind,
                    root.canonical,
                    tuple(
                        IndependentAnonymousIdentity(
                            (), identity.document_scope, identity.local_key
                        )
                        for identity in root.anonymous_identities
                    ),
                ),
                id(view),
                root.anonymous_identities,
            )
            for index, root in enumerate(local, 1)
        )
        try:
            segments = view.segments
            top_owner = view.owner
        except Exception as error:
            raise IndependentSegmentError(
                "encoded segment table is not readable", code="INDEPENDENT_SEGMENTS"
            ) from error
        if type(segments) is not tuple or not segments:
            _fail("encoded segment table is not a nonempty tuple", "INDEPENDENT_SEGMENTS")
        fields = tuple(_segment_fields(segment) for segment in segments)
        roles = tuple(item[0] for item in fields)
        resolved: list[_ResolvedRoot] = []
        if roles == (_DIRECT,):
            role, owner, source, mode, postings, scope_map, token = fields[0]
            del role
            if (
                owner is not top_owner
                or source is not None
                or mode != _ALL
                or len(postings)
                or len(scope_map)
                or token is not None
            ):
                _fail("direct segment is not canonical", "INDEPENDENT_SEGMENTS")
            resolved.extend(local_roots)
        elif roles in {(_OVERLAY_BASE,), (_OVERLAY_BASE, _OVERLAY_DELTA)}:
            _role, owner, source, mode, postings, scope_map, token = fields[0]
            if (
                source is None
                or owner is not source.owner
                or mode not in {_ALL, _EXCLUDE}
                or token is not None
            ):
                _fail("overlay base segment is invalid", "INDEPENDENT_SEGMENTS")
            resolved.extend(
                _apply_postings(
                    _apply_scope_map(
                        _resolve(source, state, referenced=True), scope_map
                    ),
                    source,
                    mode,
                    postings,
                )
            )
            if len(fields) == 1:
                if local_roots:
                    _fail("overlay without delta has local roots", "INDEPENDENT_SEGMENTS")
            else:
                _role, owner, source, mode, postings, scope_map, token = fields[1]
                if (
                    owner is not top_owner
                    or source is not None
                    or mode != _ALL
                    or len(postings)
                    or len(scope_map)
                    or token is not None
                    or not local_roots
                ):
                    _fail("overlay delta segment is invalid", "INDEPENDENT_SEGMENTS")
                resolved.extend(local_roots)
        else:
            member_count = roles.count(_COMPOSITE_MEMBER)
            bridge_count = roles.count(_COMPOSITE_BRIDGE)
            expected = (_COMPOSITE_MEMBER,) * member_count + (
                (_COMPOSITE_BRIDGE,) if bridge_count else ()
            )
            if member_count < 2 or bridge_count > 1 or roles != expected:
                _fail("composite segment roles are invalid", "INDEPENDENT_SEGMENTS")
            tokens: list[bytes] = []
            for (
                _role,
                owner,
                source,
                mode,
                postings,
                scope_map,
                token,
            ) in fields[:member_count]:
                if (
                    source is None
                    or owner is not source.owner
                    or mode not in {_ALL, _INCLUDE, _EXCLUDE}
                    or type(token) is not bytes
                    or len(token) != 32
                ):
                    _fail("composite member segment is invalid", "INDEPENDENT_SEGMENTS")
                tokens.append(token)
                selected = _apply_postings(
                    _apply_scope_map(
                        _resolve(source, state, referenced=True), scope_map
                    ),
                    source,
                    mode,
                    postings,
                )
                resolved.extend(_prefix_member(root, token) for root in selected)
            if tokens != sorted(tokens):
                _fail("composite member tokens are unordered", "INDEPENDENT_SEGMENTS")
            if len(set(tokens)) != len(tokens):
                _fail("composite member tokens collide", "INDEPENDENT_LOCATOR")
            if bridge_count:
                _role, owner, source, mode, postings, scope_map, token = fields[-1]
                if (
                    owner is not top_owner
                    or source is not None
                    or mode != _ALL
                    or len(postings)
                    or len(scope_map)
                    or token is not None
                    or not local_roots
                ):
                    _fail("composite bridge segment is invalid", "INDEPENDENT_SEGMENTS")
                resolved.extend(local_roots)
            elif local_roots:
                _fail("composite without bridge has local roots", "INDEPENDENT_SEGMENTS")
        result = tuple(sorted(resolved, key=_root_key))
        _reject_duplicate_locators(result)
        state.cache[identity] = result
        return result
    finally:
        state.active.remove(identity)


def _decode_segmented_root_canonical_bytes(view: object) -> IndependentSegmentDecode:
    """Resolve a core-validated or synthetic V1 segment graph.

    INCLUDE and EXCLUDE address only roots local to the referenced source view.
    Nested referenced roots survive EXCLUDE and are omitted by INCLUDE. Composite
    tokens qualify both root locators and anonymous ``(document_scope, local_key)``
    identities, so identical member-local blank identities remain distinct.
    """

    state = _DecodeState()
    resolved = _resolve(cast(_View, view), state, referenced=False)
    roots = _deduplicate_roots(resolved)
    return IndependentSegmentDecode(
        roots,
        IndependentDecodeProof(
            tuple(state.retained_views),
            tuple(state.referenced_buffer_views),
            referenced_buffer_copy_bytes=0,
            scalar_traversal_calls=0,
            canonical_output_bytes=sum(len(root.canonical) for root in roots),
        ),
    )


def decode_segmented_root_canonical_bytes(
    candidate: object,
    *,
    expected_owner: OntologyView,
    expected_scope: AxiomScope,
    expected_document_key: str | None,
    limits: ParseLimits | None = None,
) -> IndependentSegmentDecode:
    """Validate the complete graph before entering the independent decoder.

    Core validation recursively authenticates each referenced source fingerprint
    and freezes hostile exporters. The independent decoder then consumes only the
    returned validated graph.
    """

    validated = validate_encoded_structural_view_v1(
        candidate,
        expected_owner=expected_owner,
        expected_scope=expected_scope,
        expected_document_key=expected_document_key,
        limits=limits,
    )
    return _decode_segmented_root_canonical_bytes(validated)


__all__ = [
    "IndependentAnonymousIdentity",
    "IndependentDecodeProof",
    "IndependentLocatedRoot",
    "IndependentRootLocator",
    "IndependentSegmentDecode",
    "IndependentSegmentError",
    "decode_root_canonical_bytes",
    "decode_segmented_root_canonical_bytes",
]
