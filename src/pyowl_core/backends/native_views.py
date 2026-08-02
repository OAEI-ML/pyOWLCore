"""WP17 encoded structural columns and fail-closed native view seams.

The producer publishes retained native columns or referenced overlay/composite
segments when available and keeps scalar traversal as the complete fallback.
The normalized buffers are a core schema, not a projection of Python or Rust
object layout.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import mmap as _mmap
import sys
from array import array
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar, Final, Literal, NoReturn, Protocol, cast

from pyowl_core.document.document import Fingerprint
from pyowl_core.document.overlay import view_limits
from pyowl_core.document.snapshot import AxiomScope, OntologyView, _is_ontology_view
from pyowl_core.exceptions import (
    BackendProtocolError,
    ClosedSnapshotError,
    OperationCancelledError,
    ResourceLimitError,
)
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

if TYPE_CHECKING:
    from pyowl_core.cancellation import CancellationToken
    from pyowl_core.index.cache import IndexBuildBudget

ENCODED_STRUCTURAL_SCHEMA_NAME_V1: Final = "pyowl-core/structural-columns"
ENCODED_STRUCTURAL_SCHEMA_VERSION_V1: Final = 1
ENCODED_STRUCTURAL_MODEL_SCHEMA_V1: Final = 1
ENCODED_STRUCTURAL_SCHEMA_NAME_V2: Final = "pyowl-core/structural-columns"
ENCODED_STRUCTURAL_SCHEMA_VERSION_V2: Final = 2
ENCODED_STRUCTURAL_MODEL_SCHEMA_V2: Final = 2

_ROOT_ONTOLOGY_ANNOTATION = 1
_ROOT_AXIOM = 2
_ROOT_EXTENSION = 3
_ROOT_DIGEST_DOMAIN: Final = b"pyowl-core:encoded-structural-roots:v1\x00"
_ROOT_DIGEST_DOMAIN_V2: Final = b"pyowl-core:encoded-structural-roots:v2\x00"

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

_DESCRIPTOR_TREE_V1: Final = {
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
        {
            "name": "anonymous_scope_map",
            "type": "readonly-sorted-unique-bytes32-source-target-pairs",
        },
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
        "anonymous_scopes": (
            "sorted unique 64-byte source-current/effective-target rows; identity rows "
            "forbidden; empty for local segments; referenced mappings apply after recursive "
            "source resolution before canonical sort and structural deduplication"
        ),
        "references": "acyclic validated views with exact retained owners and fingerprints",
    },
    "schema_name": ENCODED_STRUCTURAL_SCHEMA_NAME_V1,
    "schema_version": ENCODED_STRUCTURAL_SCHEMA_VERSION_V1,
}
ENCODED_STRUCTURAL_DESCRIPTOR_V1: Final = json.dumps(
    _DESCRIPTOR_TREE_V1,
    ensure_ascii=True,
    separators=(",", ":"),
    sort_keys=True,
).encode("ascii")
ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1: Final = hashlib.sha256(
    ENCODED_STRUCTURAL_DESCRIPTOR_V1
).digest()
_FROZEN_DESCRIPTOR_SHA256_V1: Final = bytes.fromhex(
    "9ad29db6a7e616f65cea2957bc5ba8d1f9b99ef0eb1fe1432c09be25786267b5"
)
if ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1 != _FROZEN_DESCRIPTOR_SHA256_V1:
    raise RuntimeError(
        "encoded structural descriptor v1 drifted without a version decision: "
        f"{ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1.hex()}"
    )

_DESCRIPTOR_TREE_V2 = json.loads(ENCODED_STRUCTURAL_DESCRIPTOR_V1)
if not isinstance(_DESCRIPTOR_TREE_V2, dict):  # pragma: no cover - frozen v1 invariant
    raise RuntimeError("encoded structural descriptor v1 is not an object")
_DESCRIPTOR_TREE_V2["dense_id_order"] = "ascending canonical-model-v2 bytes; zero is reserved"
_DESCRIPTOR_TREE_V2["model_schema"] = ENCODED_STRUCTURAL_MODEL_SCHEMA_V2
_DESCRIPTOR_TREE_V2["root_order"] = "root kind then ascending canonical-model-v2 bytes"
_DESCRIPTOR_TREE_V2["schema_version"] = ENCODED_STRUCTURAL_SCHEMA_VERSION_V2
segment_fields_v2 = _DESCRIPTOR_TREE_V2.get("segment_fields")
if not isinstance(segment_fields_v2, list):  # pragma: no cover - frozen v1 invariant
    raise RuntimeError("encoded structural descriptor v1 segment fields are invalid")
for field_v2 in segment_fields_v2:
    if isinstance(field_v2, dict) and field_v2.get("name") == "source":
        field_v2["type"] = "EncodedStructuralViewV2-or-local"
        break
else:  # pragma: no cover - frozen v1 invariant
    raise RuntimeError("encoded structural descriptor v1 source field is missing")
_DESCRIPTOR_TREE_V2["schema_name"] = ENCODED_STRUCTURAL_SCHEMA_NAME_V2
ENCODED_STRUCTURAL_DESCRIPTOR_V2: Final = json.dumps(
    _DESCRIPTOR_TREE_V2,
    ensure_ascii=True,
    separators=(",", ":"),
    sort_keys=True,
).encode("ascii")
ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V2: Final = hashlib.sha256(
    ENCODED_STRUCTURAL_DESCRIPTOR_V2
).digest()
_FROZEN_DESCRIPTOR_SHA256_V2: Final = bytes.fromhex(
    "c51d0eb7ecf6f29ad3495fe7c40a2ea6741cf03a7cf194d51417bb810df90f51"
)
if ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V2 != _FROZEN_DESCRIPTOR_SHA256_V2:
    raise RuntimeError(
        "encoded structural descriptor v2 drifted without a version decision: "
        f"{ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V2.hex()}"
    )

_TRUSTED_ZERO_COPY = object()
_TRUSTED_MAPPED_ZERO_COPY = object()
_VALIDATED_VIEW_SEAL = object()


def _trusted_buffer_mode(buffers: Mapping[str, memoryview]) -> object | None:
    """Select zero-copy only when the interpreter exposes a provable exporter."""

    exporters = tuple(value.obj for value in buffers.values())
    if exporters and all(type(value) is bytes for value in exporters):
        return _TRUSTED_ZERO_COPY
    if (
        exporters
        and all(type(value) is _mmap.mmap for value in exporters)
        and all(value is exporters[0] for value in exporters[1:])
    ):
        return _TRUSTED_MAPPED_ZERO_COPY
    return None


class NativeViewExtension(Protocol):
    VIEW_FEATURES: tuple[str, ...]


class _NativeCancellationRelay(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(self, *error: object) -> None: ...


class EncodedStructuralPublicationV1(Protocol):
    """Structural surface accepted from a registered native producer."""

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
    anonymous_scope_map: memoryview
    member_token: bytes | None


@dataclass(frozen=True, slots=True)
class EncodedStructuralOptionsV1:
    """Canonical request options for the frozen structural-column schema."""

    schema_version: int = ENCODED_STRUCTURAL_SCHEMA_VERSION_V1
    scope: AxiomScope = AxiomScope.CLOSURE
    document_key: str | None = None
    limits: ParseLimits | None = None
    materialize_segments: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != ENCODED_STRUCTURAL_SCHEMA_VERSION_V1
        ):
            raise ValueError("schema_version must select encoded structural schema version 1")
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
        if type(self.materialize_segments) is not bool:
            raise TypeError("materialize_segments must be bool")


@dataclass(frozen=True, slots=True, eq=False)
class EncodedStructuralSegmentV1:
    """One local or referenced segment in an encoded structural view."""

    role: int
    owner: OntologyView
    source: EncodedStructuralViewV1 | None
    posting_mode: int
    root_ids: memoryview
    anonymous_scope_map: memoryview
    member_token: bytes | None
    _retained_source: object


@dataclass(frozen=True, eq=False)
class EncodedStructuralViewV1:
    """Frozen model-schema-1 request spelling; unavailable in this runtime."""

    SCHEMA_NAME: ClassVar[str] = ENCODED_STRUCTURAL_SCHEMA_NAME_V1
    SCHEMA_VERSION: ClassVar[int] = ENCODED_STRUCTURAL_SCHEMA_VERSION_V1
    MODEL_SCHEMA: ClassVar[int] = ENCODED_STRUCTURAL_MODEL_SCHEMA_V1
    DESCRIPTOR_SHA256: ClassVar[bytes] = ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1
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
        del cls, ontology, options, budget, cancellation_token, started
        raise BackendProtocolError(
            "encoded structural schema 1 cannot publish model-schema-2 values",
            code="ENCODED_VIEW_MODEL_SCHEMA",
        )


class EncodedStructuralPublicationV2(Protocol):
    """Structural surface accepted from a registered schema-2 producer."""

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


class EncodedStructuralSegmentPublicationV2(Protocol):
    """Object-level schema-2 segment metadata accepted at the hostile boundary."""

    role: int
    owner: OntologyView
    source: EncodedStructuralViewV2 | None
    posting_mode: int
    root_ids: memoryview
    anonymous_scope_map: memoryview
    member_token: bytes | None


@dataclass(frozen=True, slots=True)
class EncodedStructuralOptionsV2:
    """Canonical request options for structural-column schema 2."""

    schema_version: int = ENCODED_STRUCTURAL_SCHEMA_VERSION_V2
    scope: AxiomScope = AxiomScope.CLOSURE
    document_key: str | None = None
    limits: ParseLimits | None = None
    materialize_segments: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != ENCODED_STRUCTURAL_SCHEMA_VERSION_V2
        ):
            raise ValueError("schema_version must select encoded structural schema version 2")
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
        if type(self.materialize_segments) is not bool:
            raise TypeError("materialize_segments must be bool")


@dataclass(frozen=True, slots=True, eq=False)
class EncodedStructuralSegmentV2:
    """One local or referenced segment in an encoded structural schema-2 view."""

    role: int
    owner: OntologyView
    source: EncodedStructuralViewV2 | None
    posting_mode: int
    root_ids: memoryview
    anonymous_scope_map: memoryview
    member_token: bytes | None
    _retained_source: object


@dataclass(frozen=True, eq=False)
class EncodedStructuralViewV2:
    """One validated, owner-retaining model-schema-2 structural column set."""

    SCHEMA_NAME: ClassVar[str] = ENCODED_STRUCTURAL_SCHEMA_NAME_V2
    SCHEMA_VERSION: ClassVar[int] = ENCODED_STRUCTURAL_SCHEMA_VERSION_V2
    MODEL_SCHEMA: ClassVar[int] = ENCODED_STRUCTURAL_MODEL_SCHEMA_V2
    DESCRIPTOR_SHA256: ClassVar[bytes] = ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V2
    OPTIONS_TYPE: ClassVar[type[object]] = EncodedStructuralOptionsV2
    DEPENDENCIES: ClassVar[tuple[type[object], ...]] = ()

    schema_name: str
    schema_version: int
    model_schema: int
    owner: OntologyView
    buffers: Mapping[str, memoryview]
    descriptor: bytes
    structural_fingerprint: Fingerprint
    segments: tuple[EncodedStructuralSegmentV2, ...]
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
    ) -> EncodedStructuralViewV2:
        del cls, started
        if not isinstance(options, EncodedStructuralOptionsV2):
            raise TypeError("options must be EncodedStructuralOptionsV2")
        if not _is_ontology_view(ontology):
            raise TypeError("ontology must implement OntologyView")
        budget.check()
        created = produce_encoded_structural_view_v2(
            ontology,
            scope=options.scope,
            document_key=options.document_key,
            limits=options.limits,
            _budget=budget,
            _cancellation_token=cancellation_token,
            materialize_segments=options.materialize_segments,
        )
        budget.check()
        return created


# The generic request always selects the current model-compatible schema.
# V1 remains importable only so explicit stale requests fail closed.
EncodedStructuralView = EncodedStructuralViewV2


def _encoded_v1_unavailable() -> NoReturn:
    raise BackendProtocolError(
        "encoded structural schema 1 cannot represent model-schema-2 values",
        code="ENCODED_VIEW_MODEL_SCHEMA",
    )


def produce_encoded_structural_view_v1(
    owner: OntologyView,
    *,
    scope: AxiomScope = AxiomScope.CLOSURE,
    document_key: str | None = None,
    limits: ParseLimits | None = None,
    materialize_segments: bool = False,
    _budget: IndexBuildBudget | None = None,
    _cancellation_token: CancellationToken | None = None,
) -> NoReturn:
    """Fail closed: this model-schema-2 runtime cannot publish schema 1."""

    del (
        owner,
        scope,
        document_key,
        limits,
        materialize_segments,
        _budget,
        _cancellation_token,
    )
    _encoded_v1_unavailable()


def validate_encoded_structural_view_v1(
    candidate: object,
    *,
    expected_owner: OntologyView,
    expected_scope: AxiomScope,
    expected_document_key: str | None,
    limits: ParseLimits | None = None,
) -> NoReturn:
    """Fail closed before interpreting schema-1 columns as model-schema-2 rows."""

    del candidate, expected_owner, expected_scope, expected_document_key, limits
    _encoded_v1_unavailable()


@dataclass(frozen=True, slots=True)
class _EncodedStructuralWireRowsV1:
    """Frozen wire helper spelling retained only for fail-closed imports."""

    nodes: tuple[tuple[int, str, bytes], ...]
    roots: tuple[tuple[int, bytes], ...]
    scalar_strings: tuple[bytes, ...]
    sequences: tuple[bytes, ...]


def _produce_native_direct_view_v1(*args: object, **kwargs: object) -> NoReturn:
    del args, kwargs
    _encoded_v1_unavailable()


def _produce_native_raw_document_view_v1(*args: object, **kwargs: object) -> NoReturn:
    del args, kwargs
    _encoded_v1_unavailable()


def _anonymous_document_scopes_from_encoded_view_v1(
    view: EncodedStructuralViewV1,
) -> NoReturn:
    del view
    _encoded_v1_unavailable()


def _encoded_structural_root_digest_v1(
    buffers: Mapping[str, memoryview], limits: ParseLimits
) -> NoReturn:
    del buffers, limits
    _encoded_v1_unavailable()


def _encoded_structural_wire_rows_v1(
    publication: EncodedStructuralViewV1,
    limits: ParseLimits,
) -> NoReturn:
    del publication, limits
    _encoded_v1_unavailable()


def _encoded_structural_rows_digest_v1(
    rows: Iterable[tuple[int, bytes | memoryview]],
) -> NoReturn:
    del rows
    _encoded_v1_unavailable()


@dataclass(frozen=True, slots=True)
class _UIntColumn:
    data: memoryview
    width: int
    _length: int = field(init=False, repr=False)
    _values: memoryview | None = field(init=False, repr=False)

    def __post_init__(self) -> None:
        value_format = cast(
            Literal["B", "H", "I", "Q"],
            {1: "B", 2: "H", 4: "I", 8: "Q"}[self.width],
        )
        values = (
            self.data.cast(value_format)
            if sys.byteorder == "little" and len(self.data) % self.width == 0
            else None
        )
        object.__setattr__(self, "_length", len(self.data) // self.width)
        object.__setattr__(self, "_values", values)

    def __len__(self) -> int:
        return self._length

    def __getitem__(self, index: int) -> int:
        if index < 0 or index >= len(self):
            raise IndexError(index)
        if self._values is not None:
            return self._values[index]
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


@dataclass(frozen=True, slots=True)
class _EncodedStructuralWireRowsV2:
    """Canonical rows needed by wire without traversing Python model objects."""

    nodes: tuple[tuple[int, str, bytes], ...]
    roots: tuple[tuple[int, bytes], ...]
    scalar_strings: tuple[bytes, ...]
    sequences: tuple[bytes, ...]


def require_view_binding(capability: str) -> NativeViewExtension:
    """Require a capability registered specifically by the view seam."""

    # Keep the encoded fallback and its top-level public request type usable
    # without importing the optional native extension.  Capability lookup is
    # the only operation in this module that needs the native dispatcher.
    from . import native

    extension = native.require(capability)
    if capability not in extension.VIEW_FEATURES:
        raise BackendProtocolError(
            "native capability is not registered by the view binding seam",
            code="NATIVE_VIEW_REGISTRATION",
        )
    return cast(NativeViewExtension, extension)


def produce_encoded_structural_view_v2(
    owner: OntologyView,
    *,
    scope: AxiomScope = AxiomScope.CLOSURE,
    document_key: str | None = None,
    limits: ParseLimits | None = None,
    materialize_segments: bool = False,
    _budget: IndexBuildBudget | None = None,
    _cancellation_token: CancellationToken | None = None,
) -> EncodedStructuralViewV2:
    """Publish deterministic v2 columns without flattening retained owners."""

    _validate_selection(scope, document_key)
    if not _is_ontology_view(owner):
        raise TypeError("owner must implement OntologyView")
    if type(materialize_segments) is not bool:
        raise TypeError("materialize_segments must be bool")
    selected_limits = _selected_limits(owner, limits)
    if _budget is not None:
        _budget.check()

    if not materialize_segments:
        segmented = _produce_segmented_view_v2(
            owner,
            scope=scope,
            document_key=document_key,
            limits=selected_limits,
            budget=_budget,
            cancellation_token=_cancellation_token,
        )
        if segmented is not None:
            return segmented
    direct = _produce_mapped_direct_view_v2(
        owner,
        scope=scope,
        document_key=document_key,
        limits=selected_limits,
        budget=_budget,
    )
    if direct is not None:
        return direct
    direct = _produce_native_direct_view_v2(
        owner,
        scope=scope,
        document_key=document_key,
        limits=selected_limits,
        budget=_budget,
        cancellation_token=_cancellation_token,
    )
    if direct is not None:
        return direct
    return _produce_local_encoded_structural_view_v2(
        owner,
        scope=scope,
        document_key=document_key,
        limits=selected_limits,
        budget=_budget,
    )


def _produce_segmented_view_v2(
    owner: OntologyView,
    *,
    scope: AxiomScope,
    document_key: str | None,
    limits: ParseLimits,
    budget: IndexBuildBudget | None,
    cancellation_token: CancellationToken | None,
) -> EncodedStructuralViewV2 | None:
    """Return a retained segmented publication when the owner supports one."""

    # Imports stay local so the public view module does not make overlay and
    # composite construction depend on this optional bulk publication path.
    from pyowl_core.document.composite import OntologyComposite
    from pyowl_core.document.overlay import OntologyOverlay

    if cancellation_token is not None:
        cancellation_token.check()
    if isinstance(owner, OntologyOverlay):
        return _produce_overlay_view_v2(
            owner,
            scope=scope,
            document_key=document_key,
            limits=limits,
            budget=budget,
            cancellation_token=cancellation_token,
        )
    if isinstance(owner, OntologyComposite):
        return _produce_composite_view_v2(
            owner,
            scope=scope,
            document_key=document_key,
            limits=limits,
            budget=budget,
            cancellation_token=cancellation_token,
        )
    return None


def _produce_overlay_view_v2(
    owner: OntologyView,
    *,
    scope: AxiomScope,
    document_key: str | None,
    limits: ParseLimits,
    budget: IndexBuildBudget | None,
    cancellation_token: CancellationToken | None,
) -> EncodedStructuralViewV2 | None:
    from pyowl_core.document.delta import OntologyDelta
    from pyowl_core.document.overlay import OntologyOverlay

    overlay = cast(OntologyOverlay, owner)
    if scope is AxiomScope.CLOSURE:
        base_owner = overlay._anchor
        delta = overlay._cumulative_delta
    else:
        # Root/document selections delegate through every overlay layer.
        base_owner = overlay._anchor
        delta = OntologyDelta()
    source = _request_encoded_source_v2(
        base_owner,
        scope=scope,
        document_key=document_key,
        limits=limits,
        cancellation_token=cancellation_token,
    )
    removal_keys = _delta_removal_keys(delta, limits)
    if removal_keys and not _is_direct_encoded_view(source):
        # V2 postings address roots local to the referenced view.  An inherited
        # root cannot be excluded without flattening or changing its locator.
        return None
    excluded, found = _matching_local_root_ids(
        source,
        removal_keys,
        limits=limits,
        cancellation_token=cancellation_token,
    )
    if found != removal_keys:
        return None
    base_segment = EncodedStructuralSegmentV2(
        _SEGMENT_OVERLAY_BASE,
        source.owner,
        source,
        _POSTINGS_EXCLUDE if excluded else _POSTINGS_ALL,
        _posting_bytes(excluded),
        _empty_bytes_view(),
        None,
        source,
    )
    local_roots = _delta_addition_roots(delta)
    segments: tuple[EncodedStructuralSegmentV2, ...] = (base_segment,)
    if local_roots:
        segments += (
            EncodedStructuralSegmentV2(
                _SEGMENT_OVERLAY_DELTA,
                overlay,
                None,
                _POSTINGS_ALL,
                _empty_bytes_view(),
                _empty_bytes_view(),
                None,
                overlay,
            ),
        )
    _reserve_referenced_rows(budget, (source,))
    return _produce_local_encoded_structural_view_v2(
        overlay,
        scope=scope,
        document_key=document_key,
        limits=limits,
        budget=budget,
        root_values=local_roots,
        segments=segments,
    )


def _produce_composite_view_v2(
    owner: OntologyView,
    *,
    scope: AxiomScope,
    document_key: str | None,
    limits: ParseLimits,
    budget: IndexBuildBudget | None,
    cancellation_token: CancellationToken | None,
) -> EncodedStructuralViewV2 | None:
    from pyowl_core.document.composite import OntologyComposite

    composite = cast(OntologyComposite, owner)
    replacements = composite._scope_replacements()
    if replacements is None:
        # The segmented schema requires an explicit current-to-effective map;
        # unknown anonymous lineage must use the complete scalar fallback.
        return None
    sources = tuple(
        _request_encoded_source_v2(
            source_owner,
            scope=scope,
            document_key=document_key,
            limits=limits,
            cancellation_token=cancellation_token,
        )
        for source_owner in composite._sources
    )
    removal_keys = _delta_removal_keys(composite.delta, limits)
    if removal_keys and any(not _is_direct_encoded_view(source) for source in sources):
        return None
    found: set[tuple[int, bytes]] = set()
    member_rows: list[
        tuple[bytes, EncodedStructuralViewV2, tuple[int, ...], Mapping[bytes, bytes]]
    ] = []
    tokens = composite._source_tokens()
    for index, (token, source, mapping) in enumerate(
        zip(tokens, sources, replacements, strict=True)
    ):

        def transform_scope(
            value: StructuralNode,
            selected: int = index,
        ) -> StructuralNode:
            return composite._scope_value(selected, value)

        matched, member_found = _matching_local_root_ids(
            source,
            removal_keys,
            limits=limits,
            transform=transform_scope,
            cancellation_token=cancellation_token,
        )
        found.update(member_found)
        member_rows.append((token, source, matched, mapping))
    if found != removal_keys:
        return None
    member_rows.sort(key=lambda row: row[0])
    segments = tuple(
        EncodedStructuralSegmentV2(
            _SEGMENT_COMPOSITE_MEMBER,
            source.owner,
            source,
            _POSTINGS_EXCLUDE if excluded else _POSTINGS_ALL,
            _posting_bytes(excluded),
            _anonymous_scope_map_bytes(mapping),
            token,
            source,
        )
        for token, source, excluded, mapping in member_rows
    )
    local_roots = _delta_addition_roots(composite.delta)
    if local_roots:
        segments += (
            EncodedStructuralSegmentV2(
                _SEGMENT_COMPOSITE_BRIDGE,
                composite,
                None,
                _POSTINGS_ALL,
                _empty_bytes_view(),
                _empty_bytes_view(),
                None,
                composite,
            ),
        )
    _reserve_referenced_rows(budget, sources)
    return _produce_local_encoded_structural_view_v2(
        composite,
        scope=scope,
        document_key=document_key,
        limits=limits,
        budget=budget,
        root_values=local_roots,
        segments=segments,
    )


def _request_encoded_source_v2(
    owner: OntologyView,
    *,
    scope: AxiomScope,
    document_key: str | None,
    limits: ParseLimits,
    cancellation_token: CancellationToken | None,
) -> EncodedStructuralViewV2:
    options: dict[str, object] = {
        "schema_version": ENCODED_STRUCTURAL_SCHEMA_VERSION_V2,
        "scope": scope,
    }
    if document_key is not None:
        options["document_key"] = document_key
    if cancellation_token is not None:
        options["cancellation_token"] = cancellation_token
    result = owner.view(EncodedStructuralViewV2, **options)
    if type(result) is not EncodedStructuralViewV2 or result._seal is not _VALIDATED_VIEW_SEAL:
        _fail("referenced owner returned an invalid encoded view", "ENCODED_VIEW_SEGMENTS")
    trusted_zero_copy = _trusted_buffer_mode(result.buffers)
    if trusted_zero_copy is None:
        # A sealed publication must already have been normalized onto an
        # approved exporter. Force the immutable-exporter check so mutation of
        # a sealed view fails at the buffer boundary instead of being copied.
        trusted_zero_copy = _TRUSTED_ZERO_COPY
    return _freeze_encoded_structural_view_v2(
        result,
        expected_owner=owner,
        expected_scope=scope,
        expected_document_key=document_key,
        limits=limits,
        trusted_zero_copy=trusted_zero_copy,
        active_views=frozenset(),
    )


def _delta_addition_roots(delta: object) -> tuple[tuple[int, StructuralNode], ...]:
    from pyowl_core.document.delta import OntologyDelta

    selected = cast(OntologyDelta, delta)
    return (
        *((_ROOT_ONTOLOGY_ANNOTATION, value) for value in selected.add_ontology_annotations),
        *((_ROOT_AXIOM, value) for value in selected.add_axioms),
    )


def _delta_removal_keys(delta: object, limits: ParseLimits) -> frozenset[tuple[int, bytes]]:
    from pyowl_core.document.delta import OntologyDelta

    selected = cast(OntologyDelta, delta)
    return frozenset(
        (
            *(
                (
                    _ROOT_ONTOLOGY_ANNOTATION,
                    canonical_bytes(value, limits=limits),
                )
                for value in selected.remove_ontology_annotations
            ),
            *(
                (
                    _ROOT_AXIOM,
                    canonical_bytes(value, limits=limits),
                )
                for value in selected.remove_axioms
            ),
        )
    )


def _matching_local_root_ids(
    source: EncodedStructuralViewV2,
    targets: frozenset[tuple[int, bytes]],
    *,
    limits: ParseLimits,
    transform: Callable[[StructuralNode], StructuralNode] | None = None,
    cancellation_token: CancellationToken | None = None,
) -> tuple[tuple[int, ...], frozenset[tuple[int, bytes]]]:
    if not targets:
        return (), frozenset()
    columns = _columns_from_buffers(source.buffers)
    matched: list[int] = []
    found: set[tuple[int, bytes]] = set()
    canonical_work = 0
    for root_index in range(len(columns.roots_id)):
        if cancellation_token is not None and root_index % limits.cancellation_check_interval == 0:
            cancellation_token.check()
        encoded = _canonical_node(
            columns,
            columns.roots_id[root_index],
            {},
            set(),
            None,
            limits,
        )
        canonical_work += len(encoded)
        limits.enforce("max_canonical_work", canonical_work)
        if transform is not None:
            value = decode_canonical(encoded, limits=limits)
            encoded = canonical_bytes(transform(value), limits=limits)
            canonical_work += len(encoded)
            limits.enforce("max_canonical_work", canonical_work)
        key = (columns.roots_kind[root_index], encoded)
        if key in targets:
            matched.append(root_index + 1)
            found.add(key)
    return tuple(matched), frozenset(found)


def _columns_from_buffers(buffers: Mapping[str, memoryview]) -> _Columns:
    return _Columns(
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


def _anonymous_document_scopes_from_encoded_view_v2(
    view: EncodedStructuralViewV2,
) -> frozenset[bytes]:
    """Read document scopes from already-validated columns without model rows."""

    if type(view) is not EncodedStructuralViewV2 or view._seal is not _VALIDATED_VIEW_SEAL:
        _fail("anonymous scopes require a validated encoded view", "ENCODED_VIEW_STRUCTURE")
    columns = _columns_from_buffers(view.buffers)
    scopes: set[bytes] = set()
    for node_index in range(len(columns.tags)):
        if columns.tags[node_index] != 3:
            continue
        start = columns.field_offsets[node_index]
        end = columns.field_offsets[node_index + 1]
        if (
            end - start != 2
            or columns.field_kinds[start] != _BYTES
            or columns.field_lengths[start] != 32
        ):
            _fail(
                "anonymous individual has an invalid document scope field",
                "ENCODED_VIEW_STRUCTURE",
            )
        offset = columns.field_values[start]
        if offset > len(columns.scalar_bytes) - 32:
            _fail(
                "anonymous individual document scope exceeds scalar bytes",
                "ENCODED_VIEW_STRUCTURE",
            )
        scopes.add(bytes(columns.scalar_bytes[offset : offset + 32]))
    return frozenset(scopes)


def _is_direct_encoded_view(source: EncodedStructuralViewV2) -> bool:
    return tuple(segment.role for segment in source.segments) == (_SEGMENT_DIRECT,)


def _posting_bytes(root_ids: Sequence[int]) -> memoryview:
    return memoryview(_pack_unsigned(root_ids, 4))


def _anonymous_scope_map_bytes(mapping: Mapping[bytes, bytes]) -> memoryview:
    payload = bytearray()
    for current, target in sorted(mapping.items()):
        if (
            type(current) is not bytes
            or type(target) is not bytes
            or len(current) != 32
            or len(target) != 32
            or current == target
        ):
            _fail("composite produced an invalid anonymous scope map", "ENCODED_VIEW_SEGMENTS")
        payload.extend(current)
        payload.extend(target)
    return memoryview(bytes(payload))


def _empty_bytes_view() -> memoryview:
    return memoryview(b"")


def _reserve_referenced_rows(
    budget: IndexBuildBudget | None,
    sources: Sequence[EncodedStructuralViewV2],
) -> None:
    if budget is None:
        return
    budget.add_shared_rows(sum(len(source.buffers["root_ids"]) // 4 for source in sources))


def _produce_mapped_direct_view_v2(
    owner: OntologyView,
    *,
    scope: AxiomScope,
    document_key: str | None,
    limits: ParseLimits,
    budget: IndexBuildBudget | None,
) -> EncodedStructuralViewV2 | None:
    """Return closure columns borrowed from a validated PYOCORE mapping."""

    # Keep the dependency local: wire mapping imports the public encoded view
    # only when a caller requests it through OntologyView.view(...).
    from pyowl_core.wire.mapping import MappedOntologySnapshot

    if type(owner) is not MappedOntologySnapshot:
        return None
    acquire = getattr(owner, "_encoded_structural_columns_v2", None)
    if not callable(acquire):
        return None
    result = acquire(scope, document_key, limits)
    if result is None:
        return None
    if type(result) is not tuple or len(result) != 2:
        _fail("mapped encoded-view result has invalid framing", "ENCODED_VIEW_MAPPED")
    raw_buffers, lease = result
    if not isinstance(raw_buffers, Mapping) or set(raw_buffers) != set(_BUFFER_NAMES):
        _fail("mapped encoded-view result has invalid columns", "ENCODED_VIEW_MAPPED")
    buffers = MappingProxyType(
        {name: cast(memoryview, raw_buffers[name]) for name in _BUFFER_NAMES}
    )
    if budget is not None:
        budget.add_shared_rows(
            len(buffers["root_ids"]) // 4
            + len(buffers["node_tags"]) // 2
            + len(buffers["field_kinds"])
            + len(buffers["item_kinds"])
        )
        budget.add("encoded_mapped_metadata", rows=0, bytes_=256)
    retained = (owner, lease)
    segment = EncodedStructuralSegmentV2(
        _SEGMENT_DIRECT,
        owner,
        None,
        _POSTINGS_ALL,
        _empty_bytes_view(),
        _empty_bytes_view(),
        None,
        retained,
    )
    segments = (segment,)
    candidate = EncodedStructuralViewV2(
        ENCODED_STRUCTURAL_SCHEMA_NAME_V2,
        ENCODED_STRUCTURAL_SCHEMA_VERSION_V2,
        ENCODED_STRUCTURAL_MODEL_SCHEMA_V2,
        owner,
        buffers,
        ENCODED_STRUCTURAL_DESCRIPTOR_V2,
        _fingerprint(buffers, segments),
        segments,
        scope,
        document_key,
        retained,
        None,
    )
    trusted_zero_copy = _trusted_buffer_mode(buffers)
    if trusted_zero_copy is _TRUSTED_MAPPED_ZERO_COPY:
        return _freeze_encoded_structural_view_v2(
            candidate,
            expected_owner=owner,
            expected_scope=scope,
            expected_document_key=document_key,
            limits=limits,
            trusted_zero_copy=trusted_zero_copy,
            active_views=frozenset(),
        )
    try:
        return _freeze_encoded_structural_view_v2(
            candidate,
            expected_owner=owner,
            expected_scope=scope,
            expected_document_key=document_key,
            limits=limits,
            trusted_zero_copy=None,
            active_views=frozenset(),
        )
    finally:
        # The fallback owns immutable copies, so it must not leave lifecycle
        # correctness dependent on interpreter-specific finalizer timing.
        lease.release()


def _produce_native_direct_view_v2(
    owner: OntologyView,
    *,
    scope: AxiomScope,
    document_key: str | None,
    limits: ParseLimits,
    budget: IndexBuildBudget | None,
    cancellation_token: CancellationToken | None,
) -> EncodedStructuralViewV2 | None:
    """Return retained native columns when the installed backend exposes them."""

    state = getattr(owner, "_native_snapshot_state", None)
    owner_state = getattr(state, "owner", None)
    handle = getattr(owner_state, "handle", None)
    if handle is None:
        return None
    try:
        raw_owner = object.__getattribute__(handle, "_owner_v2")
    except AttributeError:
        return None
    try:
        extension = importlib.import_module(type(raw_owner).__module__)
    except (ImportError, ValueError):
        return None
    raw_operation = getattr(extension, "_encoded_structural_columns_v2", None)
    if not callable(raw_operation):
        return None
    native_scope = getattr(owner, "_native_scope", None)
    if not callable(native_scope):
        return None
    selected_scope, document_ordinal = native_scope(scope, document_key)
    scope_value = getattr(selected_scope, "value", None)
    if type(scope_value) is not str or scope_value not in {"closure", "document"}:
        _fail("native owner returned an invalid encoded-view scope", "NATIVE_VIEW_SCOPE")

    operation = cast(Callable[..., object], raw_operation)
    result = _invoke_native_column_operation_v2(
        extension,
        operation,
        (raw_owner, scope_value, document_ordinal),
        limits,
        cancellation_token,
    )
    return _native_direct_view_from_result_v2(
        owner,
        scope=scope,
        document_key=document_key,
        result=result,
        limits=limits,
        budget=budget,
        retained_source=owner,
    )


def _produce_native_raw_document_view_v2(
    owner: OntologyView,
    *,
    document_key: str,
    limits: ParseLimits,
    budget: IndexBuildBudget | None,
    cancellation_token: CancellationToken | None,
) -> EncodedStructuralViewV2 | None:
    """Return raw retained document columns for internal wire publication."""

    document_reader = getattr(owner, "document", None)
    if not callable(document_reader):
        return None
    document = document_reader(document_key)
    state = getattr(document, "_native_document_state", None)
    owner_state = getattr(state, "owner", None)
    handle = getattr(owner_state, "handle", None)
    if handle is None:
        return None
    try:
        raw_owner = object.__getattribute__(handle, "_owner_v2")
    except AttributeError:
        return None
    try:
        extension = importlib.import_module(type(raw_owner).__module__)
    except (ImportError, ValueError):
        return None
    raw_operation = getattr(extension, "_encoded_structural_document_columns_v2", None)
    if not callable(raw_operation):
        return None
    operation = cast(Callable[..., object], raw_operation)
    result = _invoke_native_column_operation_v2(
        extension,
        operation,
        (raw_owner,),
        limits,
        cancellation_token,
    )
    return _native_direct_view_from_result_v2(
        owner,
        scope=AxiomScope.DOCUMENT,
        document_key=document_key,
        result=result,
        limits=limits,
        budget=budget,
        retained_source=(owner, document),
    )


def _invoke_native_column_operation_v2(
    extension: object,
    operation: Callable[..., object],
    arguments: tuple[object, ...],
    limits: ParseLimits,
    cancellation_token: CancellationToken | None,
) -> object:
    from . import native

    config_encoder = cast(Callable[..., bytes], native._encode_config)
    relay_factory = cast(
        Callable[[object, ParseLimits, object | None], _NativeCancellationRelay],
        native._relay,
    )
    config = config_encoder(limits, cancellation_token, verify=False)
    try:
        with relay_factory(extension, limits, cancellation_token) as cancel:
            return operation(*arguments, config, cancel)
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except (ClosedSnapshotError, OperationCancelledError, ResourceLimitError):
        raise
    except Exception as error:
        code_reader = cast(Callable[[object, Exception], str | None], native._private_error_code)
        message_reader = cast(Callable[[Exception], str], native._private_error_message)
        code = code_reader(extension, error)
        message = message_reader(error)
        if code == "NATIVE_CANCELLED":
            raise OperationCancelledError(message, code=code) from error
        if code in {"NATIVE_DEADLINE", "NATIVE_WIRE_LIMIT"}:
            limit_reader = cast(
                Callable[[object, Exception, str, str], ResourceLimitError],
                native._native_resource_limit_error,
            )
            raise limit_reader(extension, error, message, code) from error
        if code is None:
            raise BackendProtocolError(
                "native encoded-view producer raised an unrecognized exception",
                code="NATIVE_EXCEPTION",
            ) from error
        raise BackendProtocolError(message, code=code) from error


def _native_direct_view_from_result_v2(
    owner: OntologyView,
    *,
    scope: AxiomScope,
    document_key: str | None,
    result: object,
    limits: ParseLimits,
    budget: IndexBuildBudget | None,
    retained_source: object,
) -> EncodedStructuralViewV2:
    if type(result) is not tuple or len(result) != 2:
        _fail("native encoded-view result has invalid framing", "NATIVE_VIEW_RESULT")
    raw_buffers, raw_counters = result
    if not isinstance(raw_buffers, Mapping) or not isinstance(raw_counters, Mapping):
        _fail("native encoded-view result has invalid tables", "NATIVE_VIEW_RESULT")
    if set(raw_buffers) != set(_BUFFER_NAMES):
        _fail("native encoded-view result has invalid columns", "NATIVE_VIEW_RESULT")
    buffers = MappingProxyType(
        {name: cast(memoryview, raw_buffers[name]) for name in _BUFFER_NAMES}
    )
    counters = _validate_native_column_counters(buffers, raw_counters)
    if budget is not None:
        budget.add(
            "encoded_native_columns",
            rows=(
                counters["root_rows"]
                + counters["node_rows"]
                + counters["field_rows"]
                + counters["item_rows"]
            ),
            bytes_=counters["retained_buffer_bytes"] + 128,
        )
    segment = EncodedStructuralSegmentV2(
        _SEGMENT_DIRECT,
        owner,
        None,
        _POSTINGS_ALL,
        _empty_bytes_view(),
        _empty_bytes_view(),
        None,
        retained_source,
    )
    segments = (segment,)
    candidate = EncodedStructuralViewV2(
        ENCODED_STRUCTURAL_SCHEMA_NAME_V2,
        ENCODED_STRUCTURAL_SCHEMA_VERSION_V2,
        ENCODED_STRUCTURAL_MODEL_SCHEMA_V2,
        owner,
        buffers,
        ENCODED_STRUCTURAL_DESCRIPTOR_V2,
        _fingerprint(buffers, segments),
        segments,
        scope,
        document_key,
        retained_source,
        None,
    )
    return _freeze_encoded_structural_view_v2(
        candidate,
        expected_owner=owner,
        expected_scope=scope,
        expected_document_key=document_key,
        limits=limits,
        trusted_zero_copy=_TRUSTED_ZERO_COPY,
        active_views=frozenset(),
    )


def _validate_native_column_counters(
    buffers: Mapping[str, memoryview], raw_counters: Mapping[object, object]
) -> Mapping[str, int]:
    required = {
        "root_rows",
        "node_rows",
        "field_rows",
        "item_rows",
        "scalar_bytes",
        "retained_buffer_bytes",
        "retained_metadata_bytes",
        "peak_owned_bytes",
        "peak_workspace_bytes",
        "scalar_copy_bytes",
        "canonical_work",
        "canonical_comparison_bytes",
        "complete_root_encode_calls",
        "python_bridge_copy_bytes",
    }
    if set(raw_counters) != required:
        _fail("native encoded-view counters are incomplete", "NATIVE_VIEW_COUNTERS")
    counters: dict[str, int] = {}
    for name in sorted(required):
        value = raw_counters[name]
        if type(value) is not int or value < 0:
            _fail("native encoded-view counter is invalid", "NATIVE_VIEW_COUNTERS")
        counters[name] = value
    expected = {
        "root_rows": len(buffers["root_ids"]) // 4,
        "node_rows": len(buffers["node_tags"]) // 2,
        "field_rows": len(buffers["field_kinds"]),
        "item_rows": len(buffers["item_kinds"]),
        "scalar_bytes": len(buffers["scalar_bytes"]),
        "retained_buffer_bytes": sum(len(value) for value in buffers.values()),
    }
    if any(counters[name] != value for name, value in expected.items()):
        _fail("native encoded-view counters disagree with columns", "NATIVE_VIEW_COUNTERS")
    if counters["complete_root_encode_calls"] != 0 or counters["python_bridge_copy_bytes"] != 0:
        _fail("native encoded-view path copied complete roots", "NATIVE_VIEW_COUNTERS")
    return MappingProxyType(counters)


def _produce_local_encoded_structural_view_v2(
    owner: OntologyView,
    *,
    scope: AxiomScope,
    document_key: str | None,
    limits: ParseLimits,
    budget: IndexBuildBudget | None,
    root_values: tuple[tuple[int, StructuralNode], ...] | None = None,
    segments: tuple[EncodedStructuralSegmentV2, ...] | None = None,
) -> EncodedStructuralViewV2:
    """Build only the local buffers owned by one direct or segmented view."""

    selected_limits = limits
    _budget = budget

    def reserve(table: str, bytes_: int) -> None:
        if _budget is not None:
            _budget.add(table, rows=0, bytes_=bytes_)

    # The offset column contains its initial zero even for an empty view.  Its
    # reservation is deliberately first so a tight cache policy fails before
    # scalar traversal or proportional temporary state begins.
    reserve("encoded_node_field_offsets", 8)
    selected_segment_count = 1 if segments is None else len(segments)
    reserve("encoded_segments", selected_segment_count * 128)
    if segments is not None:
        reserve(
            "encoded_segment_postings",
            sum(len(segment.root_ids) + len(segment.anonymous_scope_map) for segment in segments),
        )

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

    if root_values is None:
        annotations = owner.ontology_annotations(scope=scope, document_key=document_key)
        for annotation_value in annotations:
            if not isinstance(annotation_value, Annotation):
                _fail(
                    "ontology annotation traversal returned a non-Annotation",
                    "ENCODED_VIEW_ROOT",
                )
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
                _fail(
                    "extension traversal returned a non-structural value",
                    "ENCODED_VIEW_ROOT",
                )
            append_root(
                _ROOT_EXTENSION,
                extension_value,
                canonical_bytes(extension_value, limits=selected_limits),
            )
    else:
        for kind, value in root_values:
            _validate_root_type(kind, value)
            append_root(kind, value, canonical_bytes(value, limits=selected_limits))
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
            _fail("model tag does not fit encoded-view v2 u16", "ENCODED_VIEW_UNSUPPORTED_TAG")
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
    if segments is None:
        direct_segment = EncodedStructuralSegmentV2(
            _SEGMENT_DIRECT,
            owner,
            None,
            _POSTINGS_ALL,
            memoryview(b""),
            memoryview(b""),
            None,
            owner,
        )
        segments = (direct_segment,)
    fingerprint = _fingerprint(buffers, segments)
    candidate = EncodedStructuralViewV2(
        ENCODED_STRUCTURAL_SCHEMA_NAME_V2,
        ENCODED_STRUCTURAL_SCHEMA_VERSION_V2,
        ENCODED_STRUCTURAL_MODEL_SCHEMA_V2,
        owner,
        buffers,
        ENCODED_STRUCTURAL_DESCRIPTOR_V2,
        fingerprint,
        segments,
        scope,
        document_key,
        owner,
        None,
    )
    return _freeze_encoded_structural_view_v2(
        candidate,
        expected_owner=owner,
        expected_scope=scope,
        expected_document_key=document_key,
        limits=selected_limits,
        trusted_zero_copy=_TRUSTED_ZERO_COPY,
        active_views=frozenset(),
    )


def validate_encoded_structural_view_v2(
    candidate: object,
    *,
    expected_owner: OntologyView,
    expected_scope: AxiomScope,
    expected_document_key: str | None,
    limits: ParseLimits | None = None,
) -> EncodedStructuralViewV2:
    """Validate an untrusted publication, copying exporters to immutable bytes."""

    return _freeze_encoded_structural_view_v2(
        candidate,
        expected_owner=expected_owner,
        expected_scope=expected_scope,
        expected_document_key=expected_document_key,
        limits=limits,
        trusted_zero_copy=None,
        active_views=frozenset(),
    )


def _freeze_encoded_structural_view_v2(
    candidate: object,
    *,
    expected_owner: OntologyView,
    expected_scope: AxiomScope,
    expected_document_key: str | None,
    limits: ParseLimits | None,
    trusted_zero_copy: object | None,
    active_views: frozenset[int],
) -> EncodedStructuralViewV2:
    """Shared validator; zero-copy is restricted to module-owned producers."""

    _validate_selection(expected_scope, expected_document_key)
    if not _is_ontology_view(expected_owner):
        raise TypeError("expected_owner must implement OntologyView")
    selected_limits = _selected_limits(expected_owner, limits)
    candidate_identity = id(candidate)
    if candidate_identity in active_views:
        _fail("encoded structural segment graph is cyclic", "ENCODED_VIEW_SEGMENTS")
    selected_limits.enforce("max_overlay_depth", len(active_views) + 1)
    active_views = active_views | {candidate_identity}
    publication = cast(EncodedStructuralPublicationV2, candidate)
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

    if type(schema_name) is not str or schema_name != ENCODED_STRUCTURAL_SCHEMA_NAME_V2:
        _fail("encoded structural schema name does not match v2", "ENCODED_VIEW_DESCRIPTOR")
    if type(schema_version) is not int or schema_version != ENCODED_STRUCTURAL_SCHEMA_VERSION_V2:
        _fail("encoded structural schema version does not match v2", "ENCODED_VIEW_DESCRIPTOR")
    if type(model_schema) is not int or model_schema != ENCODED_STRUCTURAL_MODEL_SCHEMA_V2:
        _fail("encoded structural model schema does not match v2", "ENCODED_VIEW_DESCRIPTOR")
    if type(descriptor) is not bytes or descriptor != ENCODED_STRUCTURAL_DESCRIPTOR_V2:
        _fail(
            "encoded structural descriptor is not the frozen v2 descriptor",
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
    mapped_exporter: object | None = None
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
            if trusted_zero_copy is _TRUSTED_ZERO_COPY:
                if type(raw.obj) is not bytes:
                    _fail(
                        "module-owned zero-copy buffers require immutable bytes exporters",
                        "ENCODED_VIEW_BUFFERS",
                    )
            elif trusted_zero_copy is _TRUSTED_MAPPED_ZERO_COPY:
                if type(raw.obj) is not _mmap.mmap:
                    _fail(
                        "mapped zero-copy buffers require an mmap exporter",
                        "ENCODED_VIEW_BUFFERS",
                    )
                if mapped_exporter is None:
                    mapped_exporter = raw.obj
                elif raw.obj is not mapped_exporter:
                    _fail(
                        "mapped zero-copy buffers must share one exporter",
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
        _fail("encoded structural buffer set does not match v2", "ENCODED_VIEW_BUFFERS")
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
    trusted = trusted_zero_copy in {_TRUSTED_ZERO_COPY, _TRUSTED_MAPPED_ZERO_COPY}
    if not trusted:
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
    retained_source = candidate if trusted else expected_owner
    return EncodedStructuralViewV2(
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
        retained_source,
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
) -> tuple[EncodedStructuralSegmentV2, ...]:
    trusted = trusted_zero_copy in {_TRUSTED_ZERO_COPY, _TRUSTED_MAPPED_ZERO_COPY}
    if not raw_segments:
        _fail("encoded structural segment table must not be empty", "ENCODED_VIEW_SEGMENTS")
    limits.enforce("max_index_rows", len(raw_segments))
    limits.enforce("max_composite_members", max(0, len(raw_segments) - 1))
    frozen: list[EncodedStructuralSegmentV2] = []
    posting_bytes = 0
    posting_rows = 0
    for raw_segment in raw_segments:
        publication = cast(EncodedStructuralSegmentPublicationV2, raw_segment)
        try:
            role = publication.role
            owner = publication.owner
            source = publication.source
            posting_mode = publication.posting_mode
            raw_root_ids = publication.root_ids
            raw_scope_map = publication.anonymous_scope_map
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
            owner_is_view = _is_ontology_view(owner)
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
        if type(raw_scope_map) is not memoryview:
            _fail(
                "encoded structural anonymous scope maps must be a memoryview",
                "ENCODED_VIEW_SEGMENTS",
            )
        if (
            not raw_scope_map.readonly
            or raw_scope_map.ndim != 1
            or raw_scope_map.itemsize != 1
            or raw_scope_map.format != "B"
            or not raw_scope_map.c_contiguous
            or raw_scope_map.shape != (len(raw_scope_map),)
            or raw_scope_map.strides != (1,)
            or len(raw_scope_map) % 64
        ):
            _fail(
                "encoded structural anonymous scope maps must be readonly 64-byte rows",
                "ENCODED_VIEW_SEGMENTS",
            )
        posting_bytes += len(raw_root_ids) + len(raw_scope_map)
        posting_rows += len(raw_root_ids) // 4 + len(raw_scope_map) // 64
        limits.enforce("max_index_rows", posting_rows)
        limits.enforce("max_index_bytes", local_buffer_bytes + posting_bytes)
        limits.enforce("max_canonical_work", local_buffer_bytes + posting_bytes)
        if trusted:
            allowed_exporters = (
                (bytes,)
                if trusted_zero_copy is _TRUSTED_ZERO_COPY
                else (
                    bytes,
                    _mmap.mmap,
                )
            )
            if not isinstance(raw_root_ids.obj, allowed_exporters) or not isinstance(
                raw_scope_map.obj, allowed_exporters
            ):
                _fail(
                    "module-owned zero-copy segment rows require immutable bytes exporters",
                    "ENCODED_VIEW_SEGMENTS",
                )
            root_ids = raw_root_ids[:]
            anonymous_scope_map = raw_scope_map[:]
        else:
            limits.enforce("max_temporary_bytes", local_buffer_bytes + posting_bytes)
            if limits.max_memory_bytes is not None:
                limits.enforce("max_memory_bytes", local_buffer_bytes + posting_bytes)
            root_ids = memoryview(bytes(raw_root_ids))
            anonymous_scope_map = memoryview(bytes(raw_scope_map))

        frozen_source: EncodedStructuralViewV2 | None = None
        if source is None:
            if owner is not top_owner:
                _fail(
                    "local encoded structural segment did not retain the top owner",
                    "ENCODED_VIEW_SEGMENTS",
                )
            referenced_root_count = local_root_count
        else:
            source_publication = cast(EncodedStructuralPublicationV2, source)
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
            source_trust: object | None = None
            if type(source) is EncodedStructuralViewV2 and source._seal is _VALIDATED_VIEW_SEAL:
                # Recheck the exporter invariant so object.__setattr__ cannot
                # turn the validation seal into a zero-copy bypass.
                source_trust = _trusted_buffer_mode(source.buffers)
                if source_trust is None:
                    source_trust = _TRUSTED_ZERO_COPY
            frozen_source = _freeze_encoded_structural_view_v2(
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
        previous_scope: bytes | None = None
        for offset in range(0, len(anonymous_scope_map), 64):
            current_scope = bytes(anonymous_scope_map[offset : offset + 32])
            target_scope = bytes(anonymous_scope_map[offset + 32 : offset + 64])
            if (
                previous_scope is not None and current_scope <= previous_scope
            ) or current_scope == target_scope:
                _fail(
                    "anonymous scope map sources must be sorted unique with no identity rows",
                    "ENCODED_VIEW_SEGMENTS",
                )
            previous_scope = current_scope
        if role == _SEGMENT_COMPOSITE_MEMBER:
            if type(member_token) is not bytes or len(member_token) != 32:
                _fail(
                    "composite member segments require exact bytes32 tokens",
                    "ENCODED_VIEW_SEGMENTS",
                )
        elif member_token is not None:
            _fail("only composite member segments have tokens", "ENCODED_VIEW_SEGMENTS")
        retained_source = raw_segment if trusted else (owner, frozen_source)
        frozen.append(
            EncodedStructuralSegmentV2(
                role,
                owner,
                frozen_source,
                posting_mode,
                root_ids,
                anonymous_scope_map,
                member_token,
                retained_source,
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
    segments: tuple[EncodedStructuralSegmentV2, ...],
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
            or len(segment.anonymous_scope_map)
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
                or len(delta.anonymous_scope_map)
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
            or len(bridge.anonymous_scope_map)
            or bridge.member_token is not None
            or not local_root_count
        ):
            _fail("composite bridge segment metadata is invalid", "ENCODED_VIEW_SEGMENTS")
    elif local_root_count:
        _fail(
            "composite without a bridge must have empty local buffers",
            "ENCODED_VIEW_SEGMENTS",
        )


def _validate_columns(buffers: Mapping[str, memoryview], limits: ParseLimits) -> bytes:
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
    _validate_column_nesting(columns, limits)

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
    root_hasher = hashlib.sha256(_ROOT_DIGEST_DOMAIN_V2)
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
        root_hasher.update(root_kind.to_bytes(1, "little"))
        root_hasher.update(len(encoded).to_bytes(8, "little"))
        root_hasher.update(encoded)
        _validate_root_tag(root_kind, columns.tags[root_id - 1])
    if reached != set(range(1, node_count + 1)):
        _fail("encoded structural view contains unreachable nodes", "ENCODED_VIEW_STRUCTURE")
    return root_hasher.digest()


def _encoded_structural_root_digest_v2(
    buffers: Mapping[str, memoryview], limits: ParseLimits
) -> bytes:
    """Validate one column set and return its canonical effective-root digest."""

    return _validate_columns(buffers, limits)


def _encoded_structural_wire_rows_v2(
    publication: EncodedStructuralViewV2,
    limits: ParseLimits,
) -> _EncodedStructuralWireRowsV2:
    """Reconstruct canonical rows from one already validated direct publication.

    This is deliberately private wire plumbing. It accepts only a sealed
    direct view, whether closure, effective document, or raw document, so
    segmented owners and caller-forged buffers cannot bypass the normal scalar
    materialization path.
    """

    if (
        type(publication) is not EncodedStructuralViewV2
        or publication._seal is not _VALIDATED_VIEW_SEAL
        or len(publication.segments) != 1
        or publication.segments[0].role != _SEGMENT_DIRECT
    ):
        _fail(
            "wire canonical rows require a sealed direct closure publication",
            "ENCODED_VIEW_WIRE_SOURCE",
        )
    columns = _Columns(
        _UIntColumn(publication.buffers["root_kinds"], 1),
        _UIntColumn(publication.buffers["root_ids"], 4),
        _UIntColumn(publication.buffers["node_tags"], 2),
        _UIntColumn(publication.buffers["node_field_offsets"], 8),
        _UIntColumn(publication.buffers["field_kinds"], 1),
        _UIntColumn(publication.buffers["field_values"], 8),
        _UIntColumn(publication.buffers["field_lengths"], 8),
        _UIntColumn(publication.buffers["item_kinds"], 1),
        _UIntColumn(publication.buffers["item_values"], 8),
        _UIntColumn(publication.buffers["item_lengths"], 8),
        publication.buffers["scalar_bytes"],
    )
    memo: dict[int, bytes] = {}
    nodes: list[tuple[int, str, bytes]] = []
    scalar_strings: set[bytes] = set()
    sequences: set[bytes] = set()
    temporary = 0
    for node_id in range(1, len(columns.tags) + 1):
        encoded = _canonical_node(columns, node_id, memo, set(), None, limits)
        tag = columns.tags[node_id - 1]
        constructor = _CONSTRUCTOR_BY_TAG.get(tag)
        if constructor is None:  # pragma: no cover - sealed view invariant
            _fail(
                "wire canonical rows contain an unsupported constructor",
                "ENCODED_VIEW_WIRE_SOURCE",
            )
        nodes.append((tag, constructor[1], encoded))
        temporary += len(encoded)
        start = columns.field_offsets[node_id - 1]
        end = columns.field_offsets[node_id]
        for field_index in range(start, end):
            kind = columns.field_kinds[field_index]
            value = columns.field_values[field_index]
            length = columns.field_lengths[field_index]
            if kind == _TEXT:
                scalar_strings.add(bytes(columns.scalar_bytes[value : value + length]))
            elif kind == _ENUM and tag == 2:
                # EntityKind is a ``str`` enum, so scalar traversal interns its
                # value in STRINGS even though canonical-model-v2 tags it ENUM.
                scalar_strings.add(bytes(columns.scalar_bytes[value : value + length]))
            elif kind in {_SET, _SEQUENCE}:
                descriptor = _encoded_sequence_descriptor_v2(
                    columns,
                    kind,
                    value,
                    length,
                    memo,
                    limits,
                )
                sequences.add(descriptor)
                temporary += len(descriptor)
        limits.enforce("max_temporary_bytes", max(1, temporary))
    roots = tuple(
        (
            columns.roots_kind[index],
            _canonical_node(columns, columns.roots_id[index], memo, set(), None, limits),
        )
        for index in range(len(columns.roots_id))
    )
    return _EncodedStructuralWireRowsV2(
        tuple(nodes),
        roots,
        tuple(sorted(scalar_strings)),
        tuple(sorted(sequences)),
    )


def _encoded_sequence_descriptor_v2(
    columns: _Columns,
    kind: int,
    start: int,
    length: int,
    memo: dict[int, bytes],
    limits: ParseLimits,
) -> bytes:
    output = bytearray((2 if kind == _SET else 1,))
    output.extend(length.to_bytes(8, "little"))
    for item_index in range(start, start + length):
        item_kind = columns.item_kinds[item_index]
        item_value = columns.item_values[item_index]
        item_length = columns.item_lengths[item_index]
        if item_kind == _NODE:
            payload = _canonical_node(columns, item_value, memo, set(), None, limits)
        else:
            payload = _encoded_sequence_scalar_repr_v2(
                columns,
                item_kind,
                item_value,
                item_length,
            )
        output.extend(hashlib.sha256(payload).digest())
    return bytes(output)


def _encoded_sequence_scalar_repr_v2(
    columns: _Columns,
    kind: int,
    start: int,
    length: int,
) -> bytes:
    if kind == _NONE:
        return repr(None).encode("utf-8")
    payload = bytes(columns.scalar_bytes[start : start + length])
    if kind == _TEXT:
        return repr(payload.decode("utf-8")).encode("utf-8")
    if kind == _BYTES:
        return repr(payload).encode("utf-8")
    if kind == _INTEGER:
        return repr(int.from_bytes(payload, "little")).encode("utf-8")
    # Schema v2 currently has no constructor whose ordered sequence contains
    # enum scalars.  Enum repr also includes its Python class name, so guessing
    # from the payload would make the supposedly language-neutral route unsafe.
    _fail(
        "wire canonical rows do not support scalar enum sequence members",
        "ENCODED_VIEW_WIRE_SOURCE",
    )


def _encoded_structural_rows_digest_v2(
    rows: Iterable[tuple[int, bytes | memoryview]],
) -> bytes:
    """Digest canonical model rows using the encoded-view root ordering."""

    hasher = hashlib.sha256(_ROOT_DIGEST_DOMAIN_V2)
    for kind, encoded in rows:
        hasher.update(kind.to_bytes(1, "little"))
        hasher.update(len(encoded).to_bytes(8, "little"))
        hasher.update(encoded)
    return hasher.digest()


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


def _validate_column_nesting(columns: _Columns, limits: ParseLimits) -> None:
    node_count = len(columns.tags)
    depths = array("Q", [0]) * (node_count + 1)
    states = bytearray(node_count + 1)

    # An explicit DFS also ensures mapped memoryviews remain refcount-releasable;
    # a self-recursive closure would retain them in a GC cycle after validation.

    def child_nodes(node_id: int) -> Iterable[int]:
        start = columns.field_offsets[node_id - 1]
        end = columns.field_offsets[node_id]
        for field_index in range(start, end):
            kind = columns.field_kinds[field_index]
            if kind == _NODE:
                yield columns.field_values[field_index]
            elif kind in {_SET, _SEQUENCE}:
                item_start = columns.field_values[field_index]
                item_end = item_start + columns.field_lengths[field_index]
                for item_index in range(item_start, item_end):
                    if columns.item_kinds[item_index] == _NODE:
                        yield columns.item_values[item_index]

    for first in range(1, node_count + 1):
        if states[first] == 2:
            continue
        stack = [first]
        while stack:
            signed_node_id = stack.pop()
            if signed_node_id > 0:
                node_id = signed_node_id
                state = states[node_id]
                if state == 2:
                    continue
                if state == 1:
                    _fail("encoded structural graph is cyclic", "ENCODED_VIEW_STRUCTURE")
                states[node_id] = 1
                stack.append(-node_id)
                for child in child_nodes(node_id):
                    if states[child] == 1:
                        _fail("encoded structural graph is cyclic", "ENCODED_VIEW_STRUCTURE")
                    if states[child] != 2:
                        stack.append(child)
                continue

            node_id = -signed_node_id
            depth = 0
            for child in child_nodes(node_id):
                if states[child] != 2:
                    _fail("encoded structural graph is cyclic", "ENCODED_VIEW_STRUCTURE")
                depth = max(depth, depths[child] + 1)
            limits.enforce("max_nesting_depth", depth)
            depths[node_id] = depth
            states[node_id] = 2


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


def _validate_root_tag(kind: int, tag: int) -> None:
    constructor = _CONSTRUCTOR_BY_TAG.get(tag)
    if constructor is None:  # pragma: no cover - checked before root traversal
        _fail("structural root has an unsupported constructor", "ENCODED_VIEW_STRUCTURE")
    category = constructor[1]
    if kind == _ROOT_ONTOLOGY_ANNOTATION and tag != 5:
        _fail("ontology-annotation root has the wrong constructor", "ENCODED_VIEW_STRUCTURE")
    if kind == _ROOT_AXIOM and category not in {
        "annotation_axiom",
        "declaration_axiom",
        "logical_axiom",
    }:
        _fail("axiom root has the wrong constructor", "ENCODED_VIEW_STRUCTURE")
    if kind == _ROOT_EXTENSION and tag != 148:
        _fail("extension root has the wrong constructor", "ENCODED_VIEW_STRUCTURE")


def _fingerprint(
    buffers: Mapping[str, memoryview],
    segments: tuple[EncodedStructuralSegmentV2, ...],
) -> Fingerprint:
    hasher = hashlib.sha256()
    hasher.update(b"pyowl-core:encoded-structural-view:v2\x00")
    hasher.update(_frame(ENCODED_STRUCTURAL_DESCRIPTOR_V2))
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
        hasher.update(len(segment.anonymous_scope_map).to_bytes(8, "little"))
        hasher.update(segment.anonymous_scope_map)
    return Fingerprint("sha256", 2, hasher.digest())


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
    "ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V2",
    "ENCODED_STRUCTURAL_DESCRIPTOR_V1",
    "ENCODED_STRUCTURAL_DESCRIPTOR_V2",
    "ENCODED_STRUCTURAL_MODEL_SCHEMA_V1",
    "ENCODED_STRUCTURAL_MODEL_SCHEMA_V2",
    "ENCODED_STRUCTURAL_SCHEMA_NAME_V1",
    "ENCODED_STRUCTURAL_SCHEMA_NAME_V2",
    "ENCODED_STRUCTURAL_SCHEMA_VERSION_V1",
    "ENCODED_STRUCTURAL_SCHEMA_VERSION_V2",
    "EncodedStructuralOptionsV1",
    "EncodedStructuralOptionsV2",
    "EncodedStructuralPublicationV1",
    "EncodedStructuralPublicationV2",
    "EncodedStructuralSegmentPublicationV1",
    "EncodedStructuralSegmentPublicationV2",
    "EncodedStructuralSegmentV1",
    "EncodedStructuralSegmentV2",
    "EncodedStructuralView",
    "EncodedStructuralViewV1",
    "EncodedStructuralViewV2",
    "NativeViewExtension",
    "produce_encoded_structural_view_v1",
    "produce_encoded_structural_view_v2",
    "require_view_binding",
    "validate_encoded_structural_view_v1",
    "validate_encoded_structural_view_v2",
]
