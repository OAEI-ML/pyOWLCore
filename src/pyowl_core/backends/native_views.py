"""WP17 encoded structural columns and fail-closed native view seams.

The producer in this module deliberately consumes only the public
``OntologyView`` scalar traversal surface.  The normalized buffers are a core
schema, not a projection of Python or Rust object layout.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar, Final, NoReturn, Protocol, cast

from pyowl_core.document.document import Fingerprint
from pyowl_core.document.overlay import view_limits
from pyowl_core.document.snapshot import AxiomScope, OntologyView
from pyowl_core.exceptions import BackendProtocolError, ResourceLimitError
from pyowl_core.limits import ParseLimits
from pyowl_core.model import (
    CONSTRUCTOR_SPECS,
    Annotation,
    CanonicalSet,
    StructuralNode,
    canonical_bytes,
    constructor_spec,
    decode_canonical,
    encode_varint,
    walk,
)
from pyowl_core.model.axioms import AxiomNode
from pyowl_core.model.swrl import SWRLRule

from . import native

if TYPE_CHECKING:
    from pyowl_core.cancellation import CancellationToken
    from pyowl_core.index.cache import IndexBuildBudget

ENCODED_STRUCTURAL_SCHEMA_NAME_V1: Final = "pyowl-core/structural-columns"
ENCODED_STRUCTURAL_SCHEMA_VERSION_V1: Final = 1
ENCODED_STRUCTURAL_MODEL_SCHEMA_V1: Final = 1

_ROOT_ONTOLOGY_ANNOTATION = 1
_ROOT_AXIOM = 2
_ROOT_EXTENSION = 3

_SEGMENT_DIRECT = 1
_SEGMENT_OVERLAY_BASE = 2
_SEGMENT_OVERLAY_DELTA = 3
_SEGMENT_COMPOSITE_MEMBER = 4
_SEGMENT_COMPOSITE_BRIDGE = 5

_POSTINGS_ALL = 0
_POSTINGS_INCLUDE = 1
_POSTINGS_EXCLUDE = 2

_NONE = 0
_NODE = 1
_TEXT = 2
_BYTES = 3
_INTEGER = 4
_ENUM = 5
_SET = 6
_SEQUENCE = 7

_BUFFER_SPECS: Final = (
    ("root_kinds", 1, "u8"),
    ("root_ids", 4, "u32"),
    ("node_tags", 2, "u16"),
    ("node_field_offsets", 8, "u64"),
    ("field_kinds", 1, "u8"),
    ("field_values", 8, "u64"),
    ("field_lengths", 8, "u64"),
    ("item_kinds", 1, "u8"),
    ("item_values", 8, "u64"),
    ("item_lengths", 8, "u64"),
    ("scalar_bytes", 1, "bytes"),
)
_BUFFER_NAMES: Final = tuple(row[0] for row in _BUFFER_SPECS)
_BUFFER_WIDTHS: Final = MappingProxyType({row[0]: row[1] for row in _BUFFER_SPECS})

_CONSTRUCTOR_ROWS: Final = tuple(
    {
        "category": spec.category,
        "fields": list(spec.fields),
        "name": spec.tag_name,
        "tag": spec.tag,
    }
    for spec in CONSTRUCTOR_SPECS
)
_CONSTRUCTOR_BY_TAG: Final = MappingProxyType(
    {spec.tag: (spec.tag_name, spec.category, spec.fields) for spec in CONSTRUCTOR_SPECS}
)

_DESCRIPTOR_TREE: Final = {
    "buffers": [
        {"item_width": width, "name": name, "scalar": scalar}
        for name, width, scalar in _BUFFER_SPECS
    ],
    "byte_order": "little",
    "component_kinds": [
        {"name": "none", "value": _NONE},
        {"name": "node", "value": _NODE},
        {"name": "text", "value": _TEXT},
        {"name": "bytes", "value": _BYTES},
        {"name": "nonnegative_integer", "value": _INTEGER},
        {"name": "enum_ascii", "value": _ENUM},
        {"name": "canonical_set", "value": _SET},
        {"name": "ordered_sequence", "value": _SEQUENCE},
    ],
    "constructors": list(_CONSTRUCTOR_ROWS),
    "dense_id_order": "ascending canonical-model-v1 bytes; zero is reserved",
    "field_rows": "constructor field order",
    "format": "pyowl-core/encoded-structural-descriptor",
    "integer_payload": "minimal unsigned little-endian; zero is 00",
    "model_schema": ENCODED_STRUCTURAL_MODEL_SCHEMA_V1,
    "root_kinds": [
        {"name": "ontology_annotation", "value": _ROOT_ONTOLOGY_ANNOTATION},
        {"name": "axiom", "value": _ROOT_AXIOM},
        {"name": "extension", "value": _ROOT_EXTENSION},
    ],
    "root_order": "root kind then ascending canonical-model-v1 bytes",
    "root_tag_rules": {
        "axiom_categories": ["annotation_axiom", "declaration_axiom", "logical_axiom"],
        "extension_tags": [148],
        "ontology_annotation_tags": [5],
    },
    "segment_fields": [
        {"name": "role", "type": "u8-schema-tag"},
        {"name": "owner", "type": "strong-OntologyView-reference"},
        {"name": "source", "type": "EncodedStructuralViewV1-or-local"},
        {"name": "posting_mode", "type": "u8-schema-tag"},
        {"name": "root_ids", "type": "readonly-little-endian-u32"},
        {"name": "member_token", "type": "bytes32-or-none"},
    ],
    "segment_posting_modes": [
        {"name": "all", "value": _POSTINGS_ALL},
        {"name": "include", "value": _POSTINGS_INCLUDE},
        {"name": "exclude", "value": _POSTINGS_EXCLUDE},
    ],
    "segment_roles": [
        {"name": "direct", "value": _SEGMENT_DIRECT},
        {"name": "overlay_base", "value": _SEGMENT_OVERLAY_BASE},
        {"name": "overlay_delta", "value": _SEGMENT_OVERLAY_DELTA},
        {"name": "composite_member", "value": _SEGMENT_COMPOSITE_MEMBER},
        {"name": "composite_bridge", "value": _SEGMENT_COMPOSITE_BRIDGE},
    ],
    "segment_rules": {
        "composite": (
            "members sorted by unique member token; optional nonempty local bridge "
            "selecting all local roots last; local buffers empty without a bridge"
        ),
        "direct": "one local segment selecting all roots",
        "overlay": (
            "one referenced base then optional nonempty local delta selecting all local "
            "roots; local buffers empty without a delta"
        ),
        "postings": (
            "sorted unique source-local root IDs; ALL requires empty postings; "
            "INCLUDE and EXCLUDE require nonempty postings"
        ),
        "references": "acyclic validated views with exact retained owners and fingerprints",
    },
    "schema_name": ENCODED_STRUCTURAL_SCHEMA_NAME_V1,
    "schema_version": ENCODED_STRUCTURAL_SCHEMA_VERSION_V1,
}
ENCODED_STRUCTURAL_DESCRIPTOR_V1: Final = json.dumps(
    _DESCRIPTOR_TREE,
    ensure_ascii=True,
    separators=(",", ":"),
    sort_keys=True,
).encode("ascii")
ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1: Final = hashlib.sha256(
    ENCODED_STRUCTURAL_DESCRIPTOR_V1
).digest()
_FROZEN_DESCRIPTOR_SHA256_V1: Final = bytes.fromhex(
    "29bf111466b3946d4765c29c0d4742ab3ec7b355fdaa5be1ca18d15ebc3b452a"
)
if ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1 != _FROZEN_DESCRIPTOR_SHA256_V1:
    raise RuntimeError(
        "encoded structural descriptor v1 drifted without a version decision: "
        f"{ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1.hex()}"
    )

_TRUSTED_ZERO_COPY = object()
_VALIDATED_VIEW_SEAL = object()


class NativeViewExtension(Protocol):
    VIEW_FEATURES: tuple[str, ...]


class EncodedStructuralPublicationV1(Protocol):
    """Structural surface accepted from a future native producer."""

    schema_name: str
    schema_version: int
    model_schema: int
    owner: OntologyView
    buffers: Mapping[str, memoryview]
    descriptor: bytes
    structural_fingerprint: Fingerprint
    segments: tuple[object, ...]
    scope: AxiomScope
    document_key: str | None


class EncodedStructuralSegmentPublicationV1(Protocol):
    """Object-level segment metadata accepted at the hostile boundary."""

    role: int
    owner: OntologyView
    source: EncodedStructuralViewV1 | None
    posting_mode: int
    root_ids: memoryview
    member_token: bytes | None


@dataclass(frozen=True, slots=True)
class EncodedStructuralOptionsV1:
    """Canonical request options for the frozen structural-column schema."""

    schema_version: int = ENCODED_STRUCTURAL_SCHEMA_VERSION_V1
    scope: AxiomScope = AxiomScope.CLOSURE
    document_key: str | None = None
    limits: ParseLimits | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != ENCODED_STRUCTURAL_SCHEMA_VERSION_V1
        ):
            raise ValueError(
                "schema_version must select encoded structural schema version 1"
            )
        scope = self.scope
        if isinstance(scope, str) and not isinstance(scope, AxiomScope):
            try:
                scope = AxiomScope(scope)
            except ValueError as error:
                raise ValueError("scope must be a valid AxiomScope") from error
            object.__setattr__(self, "scope", scope)
        elif not isinstance(scope, AxiomScope):
            raise TypeError("scope must be AxiomScope")
        _validate_selection(scope, self.document_key)
        if self.limits is not None and not isinstance(self.limits, ParseLimits):
            raise TypeError("limits must be ParseLimits or None")


@dataclass(frozen=True, slots=True, eq=False)
class EncodedStructuralSegmentV1:
    """One local or referenced segment in an encoded structural view."""

    role: int
    owner: OntologyView
    source: EncodedStructuralViewV1 | None
    posting_mode: int
    root_ids: memoryview
    member_token: bytes | None
    _retained_source: object


@dataclass(frozen=True, eq=False)
class EncodedStructuralViewV1:
    """One validated, owner-retaining encoded structural column set."""

    SCHEMA_NAME: ClassVar[str] = ENCODED_STRUCTURAL_SCHEMA_NAME_V1
    SCHEMA_VERSION: ClassVar[int] = ENCODED_STRUCTURAL_SCHEMA_VERSION_V1
    OPTIONS_TYPE: ClassVar[type[object]] = EncodedStructuralOptionsV1
    DEPENDENCIES: ClassVar[tuple[type[object], ...]] = ()

    schema_name: str
    schema_version: int
    model_schema: int
    owner: OntologyView
    buffers: Mapping[str, memoryview]
    descriptor: bytes
    structural_fingerprint: Fingerprint
    segments: tuple[EncodedStructuralSegmentV1, ...]
    scope: AxiomScope
    document_key: str | None
    _retained_source: object
    _seal: object

    @classmethod
    def _build(
        cls,
        ontology: object,
        options: object,
        budget: IndexBuildBudget,
        cancellation_token: CancellationToken | None,
        started: float,
    ) -> EncodedStructuralViewV1:
        del started
        if not isinstance(options, EncodedStructuralOptionsV1):
            raise TypeError("options must be EncodedStructuralOptionsV1")
        if not isinstance(ontology, OntologyView):
            raise TypeError("ontology must implement OntologyView")
        budget.check()
        created = produce_encoded_structural_view_v1(
            ontology,
            scope=options.scope,
            document_key=options.document_key,
            limits=options.limits,
            _budget=budget,
        )
        budget.check()
        return created


# Stable public request type; the V1 spelling remains available for callers
# that explicitly pin the publication schema.
EncodedStructuralView = EncodedStructuralViewV1


@dataclass(frozen=True, slots=True)
class _UIntColumn:
    data: memoryview
    width: int

    def __len__(self) -> int:
        return len(self.data) // self.width

    def __getitem__(self, index: int) -> int:
        if index < 0 or index >= len(self):
            raise IndexError(index)
        start = index * self.width
        return int.from_bytes(self.data[start : start + self.width], "little")


@dataclass(frozen=True, slots=True)
class _Columns:
    roots_kind: _UIntColumn
    roots_id: _UIntColumn
    tags: _UIntColumn
    field_offsets: _UIntColumn
    field_kinds: _UIntColumn
    field_values: _UIntColumn
    field_lengths: _UIntColumn
    item_kinds: _UIntColumn
    item_values: _UIntColumn
    item_lengths: _UIntColumn
    scalar_bytes: memoryview


def require_view_binding(capability: str) -> NativeViewExtension:
    """Require a capability registered specifically by the view seam."""

    extension = native.require(capability)
    if capability not in extension.VIEW_FEATURES:
        raise BackendProtocolError(
            "native capability is not registered by the view binding seam",
            code="NATIVE_VIEW_REGISTRATION",
        )
    return cast(NativeViewExtension, extension)


def produce_encoded_structural_view_v1(
    owner: OntologyView,
    *,
    scope: AxiomScope = AxiomScope.CLOSURE,
    document_key: str | None = None,
    limits: ParseLimits | None = None,
    _budget: IndexBuildBudget | None = None,
) -> EncodedStructuralViewV1:
    """Build deterministic v1 columns through public scalar traversal only."""

    _validate_selection(scope, document_key)
    if not isinstance(owner, OntologyView):
        raise TypeError("owner must implement OntologyView")
    selected_limits = _selected_limits(owner, limits)

    def reserve(table: str, bytes_: int) -> None:
        if _budget is not None:
            _budget.add(table, rows=0, bytes_=bytes_)

    # The offset column contains its initial zero even for an empty view.  Its
    # reservation is deliberately first so a tight cache policy fails before
    # scalar traversal or proportional temporary state begins.
    reserve("encoded_node_field_offsets", 8)
    reserve("encoded_segments", 128)

    roots: list[tuple[int, StructuralNode, bytes]] = []
    axiom_root_count = 0

    def append_root(kind: int, value: StructuralNode, key: bytes) -> None:
        nonlocal axiom_root_count
        selected_limits.enforce("max_index_rows", len(roots) + 1)
        if kind == _ROOT_AXIOM:
            axiom_root_count += 1
            selected_limits.enforce("max_axioms", axiom_root_count)
        reserve("encoded_roots", 5)
        roots.append((kind, value, key))

    annotations = owner.ontology_annotations(scope=scope, document_key=document_key)
    for annotation_value in annotations:
        if not isinstance(annotation_value, Annotation):
            _fail("ontology annotation traversal returned a non-Annotation", "ENCODED_VIEW_ROOT")
        append_root(
            _ROOT_ONTOLOGY_ANNOTATION,
            annotation_value,
            canonical_bytes(annotation_value, limits=selected_limits),
        )
    for axiom_value in owner.iter_axioms(scope=scope, document_key=document_key):
        if not isinstance(axiom_value, AxiomNode):
            _fail("axiom traversal returned a non-axiom", "ENCODED_VIEW_ROOT")
        append_root(
            _ROOT_AXIOM,
            axiom_value,
            canonical_bytes(axiom_value, limits=selected_limits),
        )
    for extension_value in owner.iter_extensions(scope=scope, document_key=document_key):
        if not isinstance(extension_value, StructuralNode):
            _fail("extension traversal returned a non-structural value", "ENCODED_VIEW_ROOT")
        append_root(
            _ROOT_EXTENSION,
            extension_value,
            canonical_bytes(extension_value, limits=selected_limits),
        )
    roots.sort(key=lambda row: (row[0], row[2]))

    nodes_by_key: dict[bytes, StructuralNode] = {}
    keys_by_identity: dict[int, bytes] = {}
    walked_nodes = 0
    try:
        for _kind, root, _key in roots:
            for node in walk(root):
                walked_nodes += 1
                selected_limits.enforce("max_canonical_work", walked_nodes)
                key = canonical_bytes(node, limits=selected_limits)
                if key not in nodes_by_key:
                    selected_limits.enforce("max_terms", len(nodes_by_key) + 1)
                    selected_limits.enforce("max_index_rows", len(nodes_by_key) + 1)
                    reserve("encoded_nodes", 10)
                nodes_by_key[key] = node
                keys_by_identity[id(node)] = key
    except (TypeError, ValueError) as error:
        raise BackendProtocolError(
            "scalar traversal contains an unsupported structural component",
            code="ENCODED_VIEW_UNSUPPORTED_TAG",
        ) from error

    node_rows = sorted(nodes_by_key.items())
    if len(node_rows) >= 2**32:
        _fail("encoded structural node ID space is exhausted", "ENCODED_VIEW_ID_SPACE")
    ids_by_key = {key: index for index, (key, _node) in enumerate(node_rows, 1)}

    root_kinds: list[int] = []
    root_ids: list[int] = []
    for kind, _root, key in roots:
        root_kinds.append(kind)
        root_ids.append(ids_by_key[key])

    node_tags: list[int] = []
    field_offsets: list[int] = [0]
    field_kinds: list[int] = []
    field_values: list[int] = []
    field_lengths: list[int] = []
    item_kinds: list[int] = []
    item_values: list[int] = []
    item_lengths: list[int] = []
    scalar_bytes = bytearray()

    def append_scalar(value: object) -> tuple[int, int, int]:
        kind: int
        payload: bytes
        if value is None:
            return _NONE, 0, 0
        if isinstance(value, Enum):
            if not isinstance(value.value, str):
                _fail("model enum values must be strings", "ENCODED_VIEW_COMPONENT")
            kind = _ENUM
            payload = value.value.encode("ascii")
        elif isinstance(value, str):
            kind = _TEXT
            payload = value.encode("utf-8")
        elif isinstance(value, bytes):
            kind = _BYTES
            payload = value
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _fail("unsupported encoded structural scalar", "ENCODED_VIEW_COMPONENT")
        else:
            kind = _INTEGER
            width = max(1, (value.bit_length() + 7) // 8)
            payload = value.to_bytes(width, "little")
        offset = len(scalar_bytes)
        selected_limits.enforce("max_index_bytes", offset + len(payload))
        selected_limits.enforce("max_canonical_work", walked_nodes + offset + len(payload))
        reserve("encoded_scalar_bytes", len(payload))
        scalar_bytes.extend(payload)
        return kind, offset, len(payload)

    def node_id(value: StructuralNode) -> int:
        key = keys_by_identity.get(id(value))
        if key is None:
            key = canonical_bytes(value, limits=selected_limits)
        try:
            return ids_by_key[key]
        except KeyError as error:  # pragma: no cover - guarded by exhaustive walk
            raise AssertionError("structural child was not collected") from error

    def append_item(value: object) -> None:
        selected_limits.enforce("max_index_rows", len(item_kinds) + 1)
        reserve("encoded_items", 17)
        if isinstance(value, StructuralNode):
            item_kinds.append(_NODE)
            item_values.append(node_id(value))
            item_lengths.append(0)
            return
        kind, offset, length = append_scalar(value)
        item_kinds.append(kind)
        item_values.append(offset)
        item_lengths.append(length)

    def append_field(value: object) -> None:
        selected_limits.enforce("max_index_rows", len(field_kinds) + 1)
        reserve("encoded_fields", 17)
        if isinstance(value, StructuralNode):
            field_kinds.append(_NODE)
            field_values.append(node_id(value))
            field_lengths.append(0)
            return
        if isinstance(value, CanonicalSet):
            selected_limits.enforce("max_sequence_arity", len(value))
            start = len(item_kinds)
            for item in value:
                append_item(item)
            field_kinds.append(_SET)
            field_values.append(start)
            field_lengths.append(len(value))
            return
        if isinstance(value, tuple):
            selected_limits.enforce("max_sequence_arity", len(value))
            start = len(item_kinds)
            for item in value:
                append_item(item)
            field_kinds.append(_SEQUENCE)
            field_values.append(start)
            field_lengths.append(len(value))
            return
        kind, offset, length = append_scalar(value)
        field_kinds.append(kind)
        field_values.append(offset)
        field_lengths.append(length)

    for _key, node in node_rows:
        spec = constructor_spec(node)
        if spec.tag > 0xFFFF:
            _fail("model tag does not fit encoded-view v1 u16", "ENCODED_VIEW_UNSUPPORTED_TAG")
        node_tags.append(spec.tag)
        for field_name in spec.fields:
            append_field(getattr(node, field_name))
        field_offsets.append(len(field_kinds))

    payloads = {
        "root_kinds": _pack_unsigned(root_kinds, 1),
        "root_ids": _pack_unsigned(root_ids, 4),
        "node_tags": _pack_unsigned(node_tags, 2),
        "node_field_offsets": _pack_unsigned(field_offsets, 8),
        "field_kinds": _pack_unsigned(field_kinds, 1),
        "field_values": _pack_unsigned(field_values, 8),
        "field_lengths": _pack_unsigned(field_lengths, 8),
        "item_kinds": _pack_unsigned(item_kinds, 1),
        "item_values": _pack_unsigned(item_values, 8),
        "item_lengths": _pack_unsigned(item_lengths, 8),
        "scalar_bytes": bytes(scalar_bytes),
    }
    total_bytes = sum(len(payload) for payload in payloads.values())
    selected_limits.enforce("max_index_bytes", total_bytes)
    selected_limits.enforce("max_canonical_work", total_bytes)
    if selected_limits.max_memory_bytes is not None:
        selected_limits.enforce("max_memory_bytes", total_bytes)
    buffers = MappingProxyType({name: memoryview(payloads[name]) for name in _BUFFER_NAMES})
    direct_segment = EncodedStructuralSegmentV1(
        _SEGMENT_DIRECT,
        owner,
        None,
        _POSTINGS_ALL,
        memoryview(b""),
        None,
        owner,
    )
    segments = (direct_segment,)
    fingerprint = _fingerprint(buffers, segments)
    candidate = EncodedStructuralViewV1(
        ENCODED_STRUCTURAL_SCHEMA_NAME_V1,
        ENCODED_STRUCTURAL_SCHEMA_VERSION_V1,
        ENCODED_STRUCTURAL_MODEL_SCHEMA_V1,
        owner,
        buffers,
        ENCODED_STRUCTURAL_DESCRIPTOR_V1,
        fingerprint,
        segments,
        scope,
        document_key,
        owner,
        None,
    )
    return _freeze_encoded_structural_view_v1(
        candidate,
        expected_owner=owner,
        expected_scope=scope,
        expected_document_key=document_key,
        limits=selected_limits,
        trusted_zero_copy=_TRUSTED_ZERO_COPY,
        active_views=frozenset(),
    )


def validate_encoded_structural_view_v1(
    candidate: object,
    *,
    expected_owner: OntologyView,
    expected_scope: AxiomScope,
    expected_document_key: str | None,
    limits: ParseLimits | None = None,
) -> EncodedStructuralViewV1:
    """Validate an untrusted publication, copying exporters to immutable bytes."""

    return _freeze_encoded_structural_view_v1(
        candidate,
        expected_owner=expected_owner,
        expected_scope=expected_scope,
        expected_document_key=expected_document_key,
        limits=limits,
        trusted_zero_copy=None,
        active_views=frozenset(),
    )


def _freeze_encoded_structural_view_v1(
    candidate: object,
    *,
    expected_owner: OntologyView,
    expected_scope: AxiomScope,
    expected_document_key: str | None,
    limits: ParseLimits | None,
    trusted_zero_copy: object | None,
    active_views: frozenset[int],
) -> EncodedStructuralViewV1:
    """Shared validator; zero-copy is restricted to module-owned producers."""

    _validate_selection(expected_scope, expected_document_key)
    if not isinstance(expected_owner, OntologyView):
        raise TypeError("expected_owner must implement OntologyView")
    selected_limits = _selected_limits(expected_owner, limits)
    candidate_identity = id(candidate)
    if candidate_identity in active_views:
        _fail("encoded structural segment graph is cyclic", "ENCODED_VIEW_SEGMENTS")
    selected_limits.enforce("max_overlay_depth", len(active_views) + 1)
    active_views = active_views | {candidate_identity}
    publication = cast(EncodedStructuralPublicationV1, candidate)
    try:
        schema_name = publication.schema_name
        schema_version = publication.schema_version
        model_schema = publication.model_schema
        owner = publication.owner
        descriptor = publication.descriptor
        fingerprint = publication.structural_fingerprint
        raw_segments = publication.segments
        scope = publication.scope
        document_key = publication.document_key
        raw_buffers = publication.buffers
    except Exception as error:
        raise BackendProtocolError(
            "encoded structural publication attributes are not readable",
            code="ENCODED_VIEW_DESCRIPTOR",
        ) from error

    if type(schema_name) is not str or schema_name != ENCODED_STRUCTURAL_SCHEMA_NAME_V1:
        _fail("encoded structural schema name does not match v1", "ENCODED_VIEW_DESCRIPTOR")
    if type(schema_version) is not int or schema_version != ENCODED_STRUCTURAL_SCHEMA_VERSION_V1:
        _fail("encoded structural schema version does not match v1", "ENCODED_VIEW_DESCRIPTOR")
    if type(model_schema) is not int or model_schema != ENCODED_STRUCTURAL_MODEL_SCHEMA_V1:
        _fail("encoded structural model schema does not match v1", "ENCODED_VIEW_DESCRIPTOR")
    if type(descriptor) is not bytes or descriptor != ENCODED_STRUCTURAL_DESCRIPTOR_V1:
        _fail(
            "encoded structural descriptor is not the frozen v1 descriptor",
            "ENCODED_VIEW_DESCRIPTOR",
        )
    if owner is not expected_owner:
        _fail("encoded structural publication did not retain the exact owner", "ENCODED_VIEW_OWNER")
    if scope is not expected_scope or document_key != expected_document_key:
        _fail("encoded structural selection metadata does not match", "ENCODED_VIEW_OPTIONS")
    _validate_selection(scope, document_key)
    if type(fingerprint) is not Fingerprint:
        _fail(
            "encoded structural fingerprint must be an exact Fingerprint",
            "ENCODED_VIEW_FINGERPRINT",
        )
    if type(raw_segments) is not tuple:
        _fail("encoded structural segments must be an exact tuple", "ENCODED_VIEW_SEGMENTS")
    if not isinstance(raw_buffers, Mapping):
        _fail("encoded structural buffers must be a mapping", "ENCODED_VIEW_BUFFERS")

    frozen: dict[str, memoryview] = {}
    try:
        for key, raw in raw_buffers.items():
            if len(frozen) >= len(_BUFFER_NAMES):
                _fail("encoded structural buffer mapping has extra entries", "ENCODED_VIEW_BUFFERS")
            if type(key) is not str or key in frozen:
                _fail("encoded structural buffer names are invalid", "ENCODED_VIEW_BUFFERS")
            if type(raw) is not memoryview:
                _fail(
                    "encoded structural buffers must be exact memoryviews", "ENCODED_VIEW_BUFFERS"
                )
            if (
                raw.readonly is not True
                or raw.ndim != 1
                or raw.itemsize != 1
                or raw.format != "B"
                or not raw.c_contiguous
                or raw.shape != (len(raw),)
                or raw.strides != (1,)
            ):
                _fail(
                    "encoded structural buffers must be read-only contiguous byte views",
                    "ENCODED_VIEW_BUFFERS",
                )
            if trusted_zero_copy is _TRUSTED_ZERO_COPY and type(raw.obj) is not bytes:
                _fail(
                    "module-owned zero-copy buffers require immutable bytes exporters",
                    "ENCODED_VIEW_BUFFERS",
                )
            frozen[key] = raw
    except (BackendProtocolError, ResourceLimitError):
        raise
    except Exception as error:
        raise BackendProtocolError(
            "encoded structural buffer mapping is hostile",
            code="ENCODED_VIEW_BUFFERS",
        ) from error
    if set(frozen) != set(_BUFFER_NAMES):
        _fail("encoded structural buffer set does not match v1", "ENCODED_VIEW_BUFFERS")
    for name, width, _scalar in _BUFFER_SPECS:
        if name != "scalar_bytes" and len(frozen[name]) % width:
            _fail("encoded structural column has a partial scalar", "ENCODED_VIEW_BUFFERS")
    total_bytes = sum(len(value) for value in frozen.values())
    selected_limits.enforce("max_index_bytes", total_bytes)
    selected_limits.enforce("max_canonical_work", total_bytes)
    selected_limits.enforce("max_terms", len(frozen["node_tags"]) // 2)
    selected_limits.enforce(
        "max_index_rows",
        max(
            len(frozen["root_kinds"]),
            len(frozen["node_tags"]) // 2,
            max(0, len(frozen["node_field_offsets"]) // 8 - 1),
            len(frozen["field_kinds"]),
            len(frozen["item_kinds"]),
        ),
    )
    if trusted_zero_copy is not _TRUSTED_ZERO_COPY:
        selected_limits.enforce("max_temporary_bytes", total_bytes)
        if selected_limits.max_memory_bytes is not None:
            selected_limits.enforce("max_memory_bytes", total_bytes)
        frozen = {name: memoryview(bytes(frozen[name])) for name in _BUFFER_NAMES}
    else:
        frozen = {name: frozen[name][:] for name in _BUFFER_NAMES}

    immutable_buffers: Mapping[str, memoryview] = MappingProxyType(
        {name: frozen[name] for name in _BUFFER_NAMES}
    )
    try:
        _validate_columns(immutable_buffers, selected_limits)
    except (BackendProtocolError, ResourceLimitError):
        raise
    except Exception as error:
        raise BackendProtocolError(
            "encoded structural validation failed without escaping the boundary",
            code="ENCODED_VIEW_STRUCTURE",
        ) from error
    segments = _freeze_segments(
        raw_segments,
        top_owner=expected_owner,
        local_root_count=len(immutable_buffers["root_ids"]) // 4,
        local_buffer_bytes=total_bytes,
        limits=selected_limits,
        trusted_zero_copy=trusted_zero_copy,
        active_views=active_views,
    )
    if fingerprint != _fingerprint(immutable_buffers, segments):
        _fail(
            "encoded structural fingerprint does not cover the buffers", "ENCODED_VIEW_FINGERPRINT"
        )
    return EncodedStructuralViewV1(
        schema_name,
        schema_version,
        model_schema,
        expected_owner,
        immutable_buffers,
        descriptor,
        fingerprint,
        segments,
        expected_scope,
        expected_document_key,
        candidate,
        _VALIDATED_VIEW_SEAL,
    )


def _freeze_segments(
    raw_segments: tuple[object, ...],
    *,
    top_owner: OntologyView,
    local_root_count: int,
    local_buffer_bytes: int,
    limits: ParseLimits,
    trusted_zero_copy: object | None,
    active_views: frozenset[int],
) -> tuple[EncodedStructuralSegmentV1, ...]:
    if not raw_segments:
        _fail("encoded structural segment table must not be empty", "ENCODED_VIEW_SEGMENTS")
    limits.enforce("max_index_rows", len(raw_segments))
    limits.enforce("max_composite_members", max(0, len(raw_segments) - 1))
    frozen: list[EncodedStructuralSegmentV1] = []
    posting_bytes = 0
    posting_rows = 0
    for raw_segment in raw_segments:
        publication = cast(EncodedStructuralSegmentPublicationV1, raw_segment)
        try:
            role = publication.role
            owner = publication.owner
            source = publication.source
            posting_mode = publication.posting_mode
            raw_root_ids = publication.root_ids
            member_token = publication.member_token
        except Exception as error:
            raise BackendProtocolError(
                "encoded structural segment attributes are not readable",
                code="ENCODED_VIEW_SEGMENTS",
            ) from error
        if type(role) is not int or role not in {
            _SEGMENT_DIRECT,
            _SEGMENT_OVERLAY_BASE,
            _SEGMENT_OVERLAY_DELTA,
            _SEGMENT_COMPOSITE_MEMBER,
            _SEGMENT_COMPOSITE_BRIDGE,
        }:
            _fail("encoded structural segment role is invalid", "ENCODED_VIEW_SEGMENTS")
        try:
            owner_is_view = isinstance(owner, OntologyView)
        except Exception as error:
            raise BackendProtocolError(
                "encoded structural segment owner is hostile",
                code="ENCODED_VIEW_SEGMENTS",
            ) from error
        if not owner_is_view:
            _fail("encoded structural segment owner is invalid", "ENCODED_VIEW_SEGMENTS")
        if type(posting_mode) is not int or posting_mode not in {
            _POSTINGS_ALL,
            _POSTINGS_INCLUDE,
            _POSTINGS_EXCLUDE,
        }:
            _fail("encoded structural posting mode is invalid", "ENCODED_VIEW_SEGMENTS")
        if type(raw_root_ids) is not memoryview:
            _fail(
                "encoded structural segment postings must be a memoryview", "ENCODED_VIEW_SEGMENTS"
            )
        if (
            not raw_root_ids.readonly
            or raw_root_ids.ndim != 1
            or raw_root_ids.itemsize != 1
            or raw_root_ids.format != "B"
            or not raw_root_ids.c_contiguous
            or raw_root_ids.shape != (len(raw_root_ids),)
            or raw_root_ids.strides != (1,)
            or len(raw_root_ids) % 4
        ):
            _fail(
                "encoded structural segment postings must be readonly contiguous u32 bytes",
                "ENCODED_VIEW_SEGMENTS",
            )
        posting_bytes += len(raw_root_ids)
        posting_rows += len(raw_root_ids) // 4
        limits.enforce("max_index_rows", posting_rows)
        limits.enforce("max_index_bytes", local_buffer_bytes + posting_bytes)
        limits.enforce("max_canonical_work", local_buffer_bytes + posting_bytes)
        if trusted_zero_copy is _TRUSTED_ZERO_COPY:
            if type(raw_root_ids.obj) is not bytes:
                _fail(
                    "module-owned zero-copy postings require immutable bytes exporters",
                    "ENCODED_VIEW_SEGMENTS",
                )
            root_ids = raw_root_ids[:]
        else:
            limits.enforce("max_temporary_bytes", local_buffer_bytes + posting_bytes)
            if limits.max_memory_bytes is not None:
                limits.enforce("max_memory_bytes", local_buffer_bytes + posting_bytes)
            root_ids = memoryview(bytes(raw_root_ids))

        frozen_source: EncodedStructuralViewV1 | None = None
        if source is None:
            if owner is not top_owner:
                _fail(
                    "local encoded structural segment did not retain the top owner",
                    "ENCODED_VIEW_SEGMENTS",
                )
            referenced_root_count = local_root_count
        else:
            source_publication = cast(EncodedStructuralPublicationV1, source)
            try:
                source_scope = source_publication.scope
                source_document_key = source_publication.document_key
            except Exception as error:
                raise BackendProtocolError(
                    "referenced encoded structural view options are not readable",
                    code="ENCODED_VIEW_SEGMENTS",
                ) from error
            try:
                _validate_selection(source_scope, source_document_key)
            except (TypeError, ValueError) as error:
                raise BackendProtocolError(
                    "referenced encoded structural view options are invalid",
                    code="ENCODED_VIEW_SEGMENTS",
                ) from error
            source_trust = (
                _TRUSTED_ZERO_COPY
                if type(source) is EncodedStructuralViewV1 and source._seal is _VALIDATED_VIEW_SEAL
                else None
            )
            frozen_source = _freeze_encoded_structural_view_v1(
                source,
                expected_owner=owner,
                expected_scope=source_scope,
                expected_document_key=source_document_key,
                limits=limits,
                trusted_zero_copy=source_trust,
                active_views=active_views,
            )
            referenced_root_count = len(frozen_source.buffers["root_ids"]) // 4

        postings = _UIntColumn(root_ids, 4)
        previous_root_id = 0
        for index in range(len(postings)):
            root_id = postings[index]
            if root_id <= previous_root_id or root_id > referenced_root_count:
                _fail(
                    "encoded structural segment postings are not sorted unique in-range IDs",
                    "ENCODED_VIEW_SEGMENTS",
                )
            previous_root_id = root_id
        if posting_mode == _POSTINGS_ALL and len(postings):
            _fail("ALL segment mode requires empty postings", "ENCODED_VIEW_SEGMENTS")
        if posting_mode in {_POSTINGS_INCLUDE, _POSTINGS_EXCLUDE} and not len(postings):
            _fail("INCLUDE and EXCLUDE segment modes require postings", "ENCODED_VIEW_SEGMENTS")
        if role == _SEGMENT_COMPOSITE_MEMBER:
            if type(member_token) is not bytes or len(member_token) != 32:
                _fail(
                    "composite member segments require exact bytes32 tokens",
                    "ENCODED_VIEW_SEGMENTS",
                )
        elif member_token is not None:
            _fail("only composite member segments have tokens", "ENCODED_VIEW_SEGMENTS")
        frozen.append(
            EncodedStructuralSegmentV1(
                role,
                owner,
                frozen_source,
                posting_mode,
                root_ids,
                member_token,
                raw_segment,
            )
        )

    metadata_bytes = len(frozen) * 128
    limits.enforce(
        "max_composite_members",
        sum(segment.role == _SEGMENT_COMPOSITE_MEMBER for segment in frozen),
    )
    limits.enforce("max_index_bytes", local_buffer_bytes + posting_bytes + metadata_bytes)
    limits.enforce("max_canonical_work", posting_bytes + metadata_bytes)
    _validate_segment_family(tuple(frozen), top_owner, local_root_count)
    return tuple(frozen)


def _validate_segment_family(
    segments: tuple[EncodedStructuralSegmentV1, ...],
    top_owner: OntologyView,
    local_root_count: int,
) -> None:
    roles = tuple(segment.role for segment in segments)
    if roles == (_SEGMENT_DIRECT,):
        segment = segments[0]
        if (
            segment.owner is not top_owner
            or segment.source is not None
            or segment.posting_mode != _POSTINGS_ALL
            or len(segment.root_ids)
            or segment.member_token is not None
        ):
            _fail("direct segment metadata is not canonical", "ENCODED_VIEW_SEGMENTS")
        return
    if roles in {
        (_SEGMENT_OVERLAY_BASE,),
        (_SEGMENT_OVERLAY_BASE, _SEGMENT_OVERLAY_DELTA),
    }:
        base = segments[0]
        if (
            base.source is None
            or base.owner is not base.source.owner
            or base.posting_mode not in {_POSTINGS_ALL, _POSTINGS_EXCLUDE}
            or base.member_token is not None
        ):
            _fail("overlay base segment metadata is invalid", "ENCODED_VIEW_SEGMENTS")
        if len(segments) == 1:
            if local_root_count:
                _fail(
                    "overlay without a delta must have empty local buffers",
                    "ENCODED_VIEW_SEGMENTS",
                )
        else:
            delta = segments[1]
            if (
                delta.owner is not top_owner
                or delta.source is not None
                or delta.posting_mode != _POSTINGS_ALL
                or delta.member_token is not None
                or not local_root_count
            ):
                _fail("overlay delta segment metadata is invalid", "ENCODED_VIEW_SEGMENTS")
        return
    bridge_count = roles.count(_SEGMENT_COMPOSITE_BRIDGE)
    member_count = roles.count(_SEGMENT_COMPOSITE_MEMBER)
    if member_count < 2 or bridge_count > 1:
        _fail("composite segment family is invalid", "ENCODED_VIEW_SEGMENTS")
    expected_roles = (_SEGMENT_COMPOSITE_MEMBER,) * member_count + (
        (_SEGMENT_COMPOSITE_BRIDGE,) if bridge_count else ()
    )
    if roles != expected_roles:
        _fail("composite segments are not in canonical role order", "ENCODED_VIEW_SEGMENTS")
    tokens: list[bytes] = []
    for member in segments[:member_count]:
        if (
            member.source is None
            or member.owner is not member.source.owner
            or member.posting_mode not in {_POSTINGS_ALL, _POSTINGS_INCLUDE, _POSTINGS_EXCLUDE}
            or member.member_token is None
        ):
            _fail("composite member segment metadata is invalid", "ENCODED_VIEW_SEGMENTS")
        tokens.append(member.member_token)
    if tokens != sorted(set(tokens)):
        _fail("composite member tokens collide or are unordered", "ENCODED_VIEW_SEGMENTS")
    if bridge_count:
        bridge = segments[-1]
        if (
            bridge.owner is not top_owner
            or bridge.source is not None
            or bridge.posting_mode != _POSTINGS_ALL
            or bridge.member_token is not None
            or not local_root_count
        ):
            _fail("composite bridge segment metadata is invalid", "ENCODED_VIEW_SEGMENTS")
    elif local_root_count:
        _fail(
            "composite without a bridge must have empty local buffers",
            "ENCODED_VIEW_SEGMENTS",
        )


def _validate_columns(buffers: Mapping[str, memoryview], limits: ParseLimits) -> None:
    columns = _Columns(
        _UIntColumn(buffers["root_kinds"], 1),
        _UIntColumn(buffers["root_ids"], 4),
        _UIntColumn(buffers["node_tags"], 2),
        _UIntColumn(buffers["node_field_offsets"], 8),
        _UIntColumn(buffers["field_kinds"], 1),
        _UIntColumn(buffers["field_values"], 8),
        _UIntColumn(buffers["field_lengths"], 8),
        _UIntColumn(buffers["item_kinds"], 1),
        _UIntColumn(buffers["item_values"], 8),
        _UIntColumn(buffers["item_lengths"], 8),
        buffers["scalar_bytes"],
    )
    node_count = len(columns.tags)
    field_count = len(columns.field_kinds)
    item_count = len(columns.item_kinds)
    root_count = len(columns.roots_kind)
    limits.enforce("max_terms", node_count)
    limits.enforce("max_index_rows", max(root_count, node_count, field_count, item_count))
    limits.enforce("max_canonical_work", node_count + field_count + item_count)
    limits.enforce(
        "max_axioms",
        sum(columns.roots_kind[index] == _ROOT_AXIOM for index in range(root_count)),
    )
    limits.enforce(
        "max_annotations",
        sum(columns.tags[index] == 5 for index in range(node_count)),
    )
    limits.enforce(
        "max_rule_atoms",
        sum(141 <= columns.tags[index] <= 147 for index in range(node_count)),
    )
    if node_count >= 2**32:
        _fail("encoded structural node count exhausts u32 IDs", "ENCODED_VIEW_STRUCTURE")
    if len(columns.roots_kind) != len(columns.roots_id):
        _fail("encoded structural root columns differ in length", "ENCODED_VIEW_STRUCTURE")
    if len(columns.field_offsets) != node_count + 1:
        _fail("encoded structural field offsets do not match node count", "ENCODED_VIEW_STRUCTURE")
    if len(columns.field_values) != field_count or len(columns.field_lengths) != field_count:
        _fail("encoded structural field columns differ in length", "ENCODED_VIEW_STRUCTURE")
    if len(columns.item_values) != item_count or len(columns.item_lengths) != item_count:
        _fail("encoded structural item columns differ in length", "ENCODED_VIEW_STRUCTURE")

    expected_field = 0
    item_cursor = 0
    scalar_cursor = 0
    for node_index in range(node_count):
        tag = columns.tags[node_index]
        row = _CONSTRUCTOR_BY_TAG.get(tag)
        if row is None:
            _fail(
                "encoded structural view contains an unsupported tag",
                "ENCODED_VIEW_UNSUPPORTED_TAG",
            )
        start = columns.field_offsets[node_index]
        end = columns.field_offsets[node_index + 1]
        if start != expected_field or end - start != len(row[2]):
            _fail("encoded structural node field arity is invalid", "ENCODED_VIEW_STRUCTURE")
        expected_field = end
        for field_index in range(start, end):
            kind = columns.field_kinds[field_index]
            value = columns.field_values[field_index]
            length = columns.field_lengths[field_index]
            if kind in {_SET, _SEQUENCE}:
                limits.enforce("max_sequence_arity", length)
                if value != item_cursor or length > item_count - item_cursor:
                    _fail(
                        "encoded structural collection range is invalid", "ENCODED_VIEW_STRUCTURE"
                    )
                collection_end = item_cursor + length
                for item_index in range(item_cursor, collection_end):
                    item_kind = columns.item_kinds[item_index]
                    if kind == _SET and item_kind != _NODE:
                        _fail(
                            "canonical-set postings must contain node IDs", "ENCODED_VIEW_STRUCTURE"
                        )
                    scalar_cursor = _validate_leaf(
                        item_kind,
                        columns.item_values[item_index],
                        columns.item_lengths[item_index],
                        node_count,
                        columns.scalar_bytes,
                        scalar_cursor,
                    )
                item_cursor = collection_end
            else:
                scalar_cursor = _validate_leaf(
                    kind,
                    value,
                    length,
                    node_count,
                    columns.scalar_bytes,
                    scalar_cursor,
                )
    if expected_field != field_count or item_cursor != item_count:
        _fail("encoded structural postings are not exactly covered", "ENCODED_VIEW_STRUCTURE")
    if scalar_cursor != len(columns.scalar_bytes):
        _fail("encoded structural scalar arena is not exactly covered", "ENCODED_VIEW_STRUCTURE")

    memo: dict[int, bytes] = {}
    previous_node: bytes | None = None
    for node_id in range(1, node_count + 1):
        encoded = _canonical_node(columns, node_id, memo, set(), None, limits)
        if previous_node is not None and encoded <= previous_node:
            _fail(
                "encoded structural node IDs are not canonical and unique", "ENCODED_VIEW_STRUCTURE"
            )
        previous_node = encoded

    reached: set[int] = set()
    previous_root: tuple[int, bytes] | None = None
    for root_index in range(len(columns.roots_kind)):
        root_kind = columns.roots_kind[root_index]
        root_id = columns.roots_id[root_index]
        if root_kind not in {_ROOT_ONTOLOGY_ANNOTATION, _ROOT_AXIOM, _ROOT_EXTENSION}:
            _fail("encoded structural root kind is invalid", "ENCODED_VIEW_STRUCTURE")
        if not 1 <= root_id <= node_count:
            _fail("encoded structural root ID is out of range", "ENCODED_VIEW_STRUCTURE")
        encoded = _canonical_node(columns, root_id, memo, set(), reached, limits)
        order_key = (root_kind, encoded)
        if previous_root is not None and order_key <= previous_root:
            _fail(
                "encoded structural roots are not canonical and unique",
                "ENCODED_VIEW_STRUCTURE",
            )
        previous_root = order_key
        try:
            decoded = decode_canonical(encoded, limits=limits)
        except ResourceLimitError:
            raise
        except Exception as error:
            raise BackendProtocolError(
                "encoded structural root does not decode as canonical model v1",
                code="ENCODED_VIEW_STRUCTURE",
            ) from error
        _validate_root_type(root_kind, decoded)
    if reached != set(range(1, node_count + 1)):
        _fail("encoded structural view contains unreachable nodes", "ENCODED_VIEW_STRUCTURE")


def _validate_leaf(
    kind: int,
    value: int,
    length: int,
    node_count: int,
    scalar_bytes: memoryview,
    scalar_cursor: int,
) -> int:
    if kind == _NONE:
        if value or length:
            _fail("none components must have zero value and length", "ENCODED_VIEW_STRUCTURE")
        return scalar_cursor
    if kind == _NODE:
        if length or not 1 <= value <= node_count:
            _fail("node component ID is out of range", "ENCODED_VIEW_STRUCTURE")
        return scalar_cursor
    if kind not in {_TEXT, _BYTES, _INTEGER, _ENUM}:
        _fail("encoded structural component kind is invalid", "ENCODED_VIEW_STRUCTURE")
    if value != scalar_cursor or length > len(scalar_bytes) - scalar_cursor:
        _fail("encoded structural scalar range is invalid", "ENCODED_VIEW_STRUCTURE")
    end = scalar_cursor + length
    payload = scalar_bytes[scalar_cursor:end]
    if kind == _TEXT:
        try:
            bytes(payload).decode("utf-8")
        except UnicodeDecodeError as error:
            raise BackendProtocolError(
                "encoded structural text is not UTF-8",
                code="ENCODED_VIEW_STRUCTURE",
            ) from error
    elif kind == _ENUM:
        try:
            bytes(payload).decode("ascii")
        except UnicodeDecodeError as error:
            raise BackendProtocolError(
                "encoded structural enum is not ASCII",
                code="ENCODED_VIEW_STRUCTURE",
            ) from error
    elif kind == _INTEGER and (length == 0 or (length > 1 and payload[-1] == 0)):
        _fail("encoded structural integer is not minimal", "ENCODED_VIEW_STRUCTURE")
    return end


def _canonical_node(
    columns: _Columns,
    node_id: int,
    memo: dict[int, bytes],
    active: set[int],
    reached: set[int] | None,
    limits: ParseLimits,
) -> bytes:
    if reached is not None:
        reached.add(node_id)
    cached = memo.get(node_id)
    if cached is not None:
        if reached is not None:
            _mark_reachable(columns, node_id, reached)
        return cached
    if node_id in active:
        _fail("encoded structural graph is cyclic", "ENCODED_VIEW_STRUCTURE")
    limits.enforce("max_nesting_depth", len(active))
    active.add(node_id)
    try:
        output = bytearray(encode_varint(columns.tags[node_id - 1]))
        start = columns.field_offsets[node_id - 1]
        end = columns.field_offsets[node_id]
        for field_index in range(start, end):
            output.extend(
                _canonical_component(
                    columns,
                    columns.field_kinds[field_index],
                    columns.field_values[field_index],
                    columns.field_lengths[field_index],
                    memo,
                    active,
                    reached,
                    limits,
                )
            )
        encoded = bytes(output)
        memo[node_id] = encoded
        return encoded
    finally:
        active.remove(node_id)


def _canonical_component(
    columns: _Columns,
    kind: int,
    value: int,
    length: int,
    memo: dict[int, bytes],
    active: set[int],
    reached: set[int] | None,
    limits: ParseLimits,
) -> bytes:
    if kind == _NONE:
        return bytes((_NONE,))
    if kind == _NODE:
        nested = _canonical_node(columns, value, memo, active, reached, limits)
        return bytes((_NODE,)) + _frame(nested)
    if kind in {_TEXT, _BYTES, _ENUM, _INTEGER}:
        payload = bytes(columns.scalar_bytes[value : value + length])
        if kind == _INTEGER:
            return bytes((_INTEGER,)) + encode_varint(int.from_bytes(payload, "little"))
        return bytes((kind,)) + _frame(payload)
    marker = _SET if kind == _SET else _SEQUENCE
    output = bytearray((marker,))
    output.extend(encode_varint(length))
    previous: bytes | None = None
    for item_index in range(value, value + length):
        item_kind = columns.item_kinds[item_index]
        item_value = columns.item_values[item_index]
        item_length = columns.item_lengths[item_index]
        if item_kind == _NODE:
            nested = _canonical_node(columns, item_value, memo, active, reached, limits)
            if kind == _SET:
                if previous is not None and nested <= previous:
                    _fail(
                        "canonical-set postings are not sorted and unique", "ENCODED_VIEW_STRUCTURE"
                    )
                previous = nested
                output.extend(_frame(nested))
            else:
                output.extend(bytes((_NODE,)) + _frame(nested))
        else:
            if kind == _SET:
                _fail("canonical-set postings must contain nodes", "ENCODED_VIEW_STRUCTURE")
            output.extend(
                _canonical_component(
                    columns,
                    item_kind,
                    item_value,
                    item_length,
                    memo,
                    active,
                    reached,
                    limits,
                )
            )
    return bytes(output)


def _mark_reachable(columns: _Columns, root_id: int, reached: set[int]) -> None:
    stack = [root_id]
    while stack:
        node_id = stack.pop()
        if node_id in reached:
            # The caller inserts the root before this helper.  Its children still
            # need one scan, so use a separate local expansion set.
            pass
        else:
            reached.add(node_id)
        start = columns.field_offsets[node_id - 1]
        end = columns.field_offsets[node_id]
        for field_index in range(start, end):
            kind = columns.field_kinds[field_index]
            if kind == _NODE:
                child = columns.field_values[field_index]
                if child not in reached:
                    stack.append(child)
            elif kind in {_SET, _SEQUENCE}:
                item_start = columns.field_values[field_index]
                item_end = item_start + columns.field_lengths[field_index]
                for item_index in range(item_start, item_end):
                    if columns.item_kinds[item_index] == _NODE:
                        child = columns.item_values[item_index]
                        if child not in reached:
                            stack.append(child)


def _validate_root_type(kind: int, value: StructuralNode) -> None:
    if kind == _ROOT_ONTOLOGY_ANNOTATION and not isinstance(value, Annotation):
        _fail("ontology-annotation root has the wrong constructor", "ENCODED_VIEW_STRUCTURE")
    if kind == _ROOT_AXIOM and not isinstance(value, AxiomNode):
        _fail("axiom root has the wrong constructor", "ENCODED_VIEW_STRUCTURE")
    if kind == _ROOT_EXTENSION and not isinstance(value, SWRLRule):
        _fail("extension root has the wrong constructor", "ENCODED_VIEW_STRUCTURE")


def _fingerprint(
    buffers: Mapping[str, memoryview],
    segments: tuple[EncodedStructuralSegmentV1, ...],
) -> Fingerprint:
    hasher = hashlib.sha256()
    hasher.update(b"pyowl-core:encoded-structural-view:v1\x00")
    hasher.update(_frame(ENCODED_STRUCTURAL_DESCRIPTOR_V1))
    for name in _BUFFER_NAMES:
        hasher.update(_frame(name.encode("ascii")))
        value = buffers[name]
        hasher.update(len(value).to_bytes(8, "little"))
        hasher.update(value)
    hasher.update(len(segments).to_bytes(8, "little"))
    for segment in segments:
        hasher.update(bytes((segment.role, segment.posting_mode)))
        if segment.source is None:
            hasher.update(b"\x00")
        else:
            hasher.update(b"\x01")
            source_fingerprint = segment.source.structural_fingerprint
            hasher.update(source_fingerprint.schema.to_bytes(4, "little"))
            hasher.update(source_fingerprint.digest)
        if segment.member_token is None:
            hasher.update(b"\x00")
        else:
            hasher.update(b"\x01" + segment.member_token)
        hasher.update(len(segment.root_ids).to_bytes(8, "little"))
        hasher.update(segment.root_ids)
    return Fingerprint("sha256", 1, hasher.digest())


def _pack_unsigned(values: Sequence[int], width: int) -> bytes:
    maximum = 1 << (width * 8)
    output = bytearray()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < maximum:
            _fail("encoded structural scalar exceeds its fixed width", "ENCODED_VIEW_ID_SPACE")
        output.extend(value.to_bytes(width, "little"))
    return bytes(output)


def _selected_limits(owner: OntologyView, requested: ParseLimits | None) -> ParseLimits:
    owner_limits = view_limits(owner)
    if requested is None:
        return owner_limits
    if not isinstance(requested, ParseLimits):
        raise TypeError("limits must be ParseLimits or None")
    return owner_limits.tightened_with(requested)


def _validate_selection(scope: AxiomScope, document_key: str | None) -> None:
    if not isinstance(scope, AxiomScope):
        raise TypeError("scope must be AxiomScope")
    if scope is AxiomScope.DOCUMENT:
        if type(document_key) is not str or not document_key:
            raise ValueError("AxiomScope.DOCUMENT requires document_key")
    elif document_key is not None:
        raise ValueError("document_key is valid only with AxiomScope.DOCUMENT")


def _frame(value: bytes) -> bytes:
    return encode_varint(len(value)) + value


def _fail(message: str, code: str) -> NoReturn:
    raise BackendProtocolError(message, code=code)


__all__ = [
    "ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1",
    "ENCODED_STRUCTURAL_DESCRIPTOR_V1",
    "ENCODED_STRUCTURAL_MODEL_SCHEMA_V1",
    "ENCODED_STRUCTURAL_SCHEMA_NAME_V1",
    "ENCODED_STRUCTURAL_SCHEMA_VERSION_V1",
    "EncodedStructuralOptionsV1",
    "EncodedStructuralPublicationV1",
    "EncodedStructuralSegmentPublicationV1",
    "EncodedStructuralSegmentV1",
    "EncodedStructuralView",
    "EncodedStructuralViewV1",
    "NativeViewExtension",
    "produce_encoded_structural_view_v1",
    "require_view_binding",
    "validate_encoded_structural_view_v1",
]
