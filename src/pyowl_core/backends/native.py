"""Validated Python adapter to the optional private Rust extension."""

from __future__ import annotations

import importlib
import math
import os
import platform
import re
import struct
import sysconfig
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias, TypeVar, cast

from pyowl_core._evidence import bounded_evidence_text
from pyowl_core.cancellation import CancellationToken
from pyowl_core.diagnostics import Diagnostic, Severity
from pyowl_core.exceptions import (
    BackendProtocolError,
    BackendUnavailableError,
    OntologySyntaxError,
    OperationCancelledError,
    ResourceLimitError,
    UnsupportedSyntaxError,
    WireCorruptionError,
    WireVersionError,
)
from pyowl_core.limits import ParseLimits

if TYPE_CHECKING:
    from pyowl_core.document.snapshot import OntologySnapshot, OntologyView
    from pyowl_core.io.formats.common import ParsedOntology
    from pyowl_core.model.axioms import AxiomNode

_ABI_VERSION = 3
_MODEL_SCHEMA_VERSION = 2
_WIRE_FORMAT_VERSION = (1, 2)
_CONFIG = struct.Struct("<8sHHI37Q")
_RECEIPT = struct.Struct("<8sIIHHIQ32sIQ")
_CONFIG_MAGIC = b"PYNCONF\0"
_RECEIPT_MAGIC = b"PYNVAL1\0"
_PARSE_REQUEST = struct.Struct("<8sHHQ")

_RetainedBindingMetadata: TypeAlias = tuple[
    tuple[int, ...],
    tuple[tuple[int, ...], tuple[int, ...]],
]
_PARSE_RESULT_HEADER = struct.Struct("<8sHHQ")
_PARSE_REQUEST_MAGIC = b"PYNFSS1\0"
_PARSE_RESULT_MAGIC = b"PYNFSSR1"
_RETAINED_FUNCTIONAL_SEED_MAGIC_V2 = b"PYNFRS2\0"
_RETAINED_RDFXML_SEED_MAGIC_V2 = b"PYNRRS2\0"
_RETAINED_FUNCTIONAL_PREPARED_MAGIC_V2 = b"PYNFPP2\0"
_RETAINED_CLOSURE_PREPARED_MAGIC_V2 = b"PYNFCP2\0"
_INDEX_SOURCE_HEADER = struct.Struct("<8sHHQ")
_INDEX_RESULT_HEADER = struct.Struct("<8sHHQ")
_INDEX_SOURCE_MAGIC = b"PYNIDXS1"
_INDEX_REQUEST_MAGIC = b"PYNIDXQ1"
_INDEX_RESULT_MAGIC = b"PYNIDXR1"
_CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_LIMIT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LIMIT_DETAIL_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RDF_REIFICATION_ERROR_MAGIC = b"PYNRFE2\0"
_MAX_RDF_EVIDENCE_BYTES = 4_096
_MAX_RDF_REIFICATION_PAYLOAD_BYTES = 32 * 1_024
_EXTENSION_NAME = "pyowl_core._native"
_FOUNDATION_FEATURES = frozenset(
    {
        "canonical-model-v2",
        "cancellation",
        "deadlines",
        "gil-release",
        "owned-buffers",
        "panic-containment",
        "safe-rust",
        "wire-v1",
    }
)
_FOUNDATION_FEATURE_LEDGER = _FOUNDATION_FEATURES | {"index-axiom-types-v1"}
_INGESTION_FEATURE_LEDGER = frozenset(
    {
        "parse-functional-v1",
        "parse-owlxml-v1",
        "parse-rdfxml-v1",
        "parse-turtle-v1",
    }
)
_VIEW_FEATURE_LEDGER = frozenset({"pyowl-core/structural-columns"})


class _NativeCancellation(Protocol):
    @property
    def cancelled(self) -> bool: ...

    def cancel(self) -> bool: ...


class _NativeRetainedAxiomTypeIndex(Protocol):
    def _binding_v1(self) -> tuple[bytes, bytes]: ...

    def _canonical_sizes_v1(self) -> tuple[int, ...]: ...

    def _layout_v1(
        self,
    ) -> tuple[
        tuple[int, ...],
        tuple[int, ...],
        tuple[int, ...],
        tuple[int, ...],
        tuple[int, ...],
        dict[str, int],
    ]: ...

    def _page_v1(
        self,
        tag: int,
        start: int,
        max_rows: int,
        max_bytes: int,
        config: object,
        cancel: _NativeCancellation | None = None,
    ) -> tuple[tuple[bytes, ...], int, int | None]: ...


class _NativeRetainedSignatureIndex(Protocol):
    def _layout_v1(
        self,
    ) -> tuple[
        bytes,
        bytes,
        tuple[int, ...],
        tuple[int, ...],
        tuple[int, ...],
        dict[str, int],
    ]: ...


class _NativeRetainedOntologyIdentityIndex(Protocol):
    def _layout_v1(
        self,
    ) -> tuple[str, bytes, bytes, bytes, dict[str, int]]: ...


class _Extension(Protocol):
    ABI_VERSION: int
    MODEL_SCHEMA_VERSION: int
    WIRE_FORMAT_VERSION: tuple[int, int]
    FEATURES: tuple[str, ...]
    INGESTION_FEATURES: tuple[str, ...]
    VIEW_FEATURES: tuple[str, ...]
    _NativeError: type[Exception]
    _NativeParsedStructuralStorageV2: type[object]
    _Cancellation: Callable[[float | None], _NativeCancellation]

    def version(self) -> tuple[str, int]: ...

    def self_test(self) -> None: ...

    def validate_canonical(
        self, data: object, config: object, cancel: _NativeCancellation | None = None
    ) -> bytes: ...

    def validate_wire(
        self, data: object, config: object, cancel: _NativeCancellation | None = None
    ) -> bytes: ...

    def roundtrip_wire(
        self, data: object, config: object, cancel: _NativeCancellation | None = None
    ) -> bytes: ...

    def parse_document(
        self, data: object, config: object, cancel: _NativeCancellation | None = None
    ) -> bytes: ...

    def _parse_functional_retained_v2(
        self,
        data: object,
        config: object,
        collect_provenance: bool,
        preserve_source_map: bool,
        record_unresolved: bool,
        require_empty_imports: bool,
        cancel: _NativeCancellation | None = None,
        *,
        materialize_document: bool = False,
    ) -> tuple[bytes, object, _RetainedBindingMetadata]: ...

    def _parse_rdfxml_retained_v2(
        self,
        data: object,
        document_iri: str | None,
        config: object,
        collect_provenance: bool,
        allow_partial_rdf_mapping: bool,
        allow_swrl: bool,
        require_empty_imports: bool,
        cancel: _NativeCancellation | None = None,
    ) -> tuple[bytes, object, _RetainedBindingMetadata]: ...

    def _parse_rdfxml_retained_source_map_v2(
        self,
        data: object,
        document_iri: str | None,
        config: object,
        collect_provenance: bool,
        allow_partial_rdf_mapping: bool,
        allow_swrl: bool,
        require_empty_imports: bool,
        cancel: _NativeCancellation | None = None,
    ) -> tuple[bytes, object, _RetainedBindingMetadata]: ...

    def _parse_turtle_retained_v2(
        self,
        data: object,
        document_iri: str | None,
        config: object,
        collect_provenance: bool,
        allow_partial_rdf_mapping: bool,
        allow_swrl: bool,
        require_empty_imports: bool,
        cancel: _NativeCancellation | None = None,
    ) -> tuple[bytes, object, _RetainedBindingMetadata]: ...

    def _parse_turtle_retained_source_map_v2(
        self,
        data: object,
        document_iri: str | None,
        config: object,
        collect_provenance: bool,
        allow_partial_rdf_mapping: bool,
        allow_swrl: bool,
        require_empty_imports: bool,
        cancel: _NativeCancellation | None = None,
    ) -> tuple[bytes, object, _RetainedBindingMetadata]: ...

    def _parse_owlxml_retained_v2(
        self,
        data: object,
        document_iri: str | None,
        config: object,
        collect_provenance: bool,
        allow_swrl: bool,
        require_empty_imports: bool,
        cancel: _NativeCancellation | None = None,
    ) -> tuple[bytes, object, _RetainedBindingMetadata]: ...

    def _parse_owlxml_retained_source_map_v2(
        self,
        data: object,
        document_iri: str | None,
        config: object,
        collect_provenance: bool,
        allow_swrl: bool,
        require_empty_imports: bool,
        cancel: _NativeCancellation | None = None,
    ) -> tuple[bytes, object, _RetainedBindingMetadata]: ...

    def _fork_parsed_structural_storage_v2(
        self,
        parsed: object,
        cancel: _NativeCancellation | None = None,
    ) -> object: ...

    def build_index(
        self, data: object, request: object, cancel: _NativeCancellation | None = None
    ) -> bytes: ...


class _Subinterpreters(Protocol):
    def get_current(self) -> int: ...

    def get_main(self) -> int: ...


class _InterpreterReference(Protocol):
    @property
    def id(self) -> int: ...


class _Interpreters(Protocol):
    def get_current(self) -> _InterpreterReference: ...

    def get_main(self) -> _InterpreterReference: ...


@dataclass(frozen=True, slots=True)
class NativeProbe:
    available: bool
    reason: str | None
    version: str | None
    features: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NativeValidation:
    wire_minor: int
    feature_flags: int
    total_length: int
    file_digest: bytes
    section_count: int
    total_rows: int


@dataclass(frozen=True, slots=True)
class NativeAxiomPartition:
    postings: dict[type[AxiomNode], tuple[AxiomNode, ...]]
    canonical_sizes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class NativeRetainedAxiomPartition:
    owner: _NativeRetainedAxiomTypeIndex
    tags: tuple[int, ...]
    offsets: tuple[int, ...]
    category_codes: tuple[int, ...]
    category_offsets: tuple[int, ...]
    postings: tuple[int, ...]
    canonical_sizes: tuple[int, ...]
    axiom_rows: int
    constructor_groups: int
    category_groups: int
    retained_buffer_bytes: int
    peak_owned_bytes: int
    canonical_work: int
    complete_root_encode_calls: int


@dataclass(frozen=True, slots=True)
class NativeRetainedSignatureCounts:
    owner: _NativeRetainedSignatureIndex
    referenced_counts: tuple[int, ...]
    nonannotation_counts: tuple[int, ...]
    declaration_counts: tuple[int, ...]
    structural_root_rows: int
    entity_rows: int
    referenced_links: int
    nonannotation_links: int
    declaration_links: int
    retained_buffer_bytes: int
    peak_owned_bytes: int
    canonical_work: int
    complete_root_encode_calls: int


@dataclass(frozen=True, slots=True)
class _NativeRetainedFunctionalParseV2:
    parsed: ParsedOntology | None
    storage: object | None
    phase_timings: tuple[tuple[str, float], ...]
    encoded: bytes | None = None
    summary: bytes | None = None


_ANONYMOUS_SHAPE_TIMING_NAMES = (
    "native_anonymous_component_count",
    "native_anonymous_total_labels",
    "native_anonymous_total_arcs",
    "native_anonymous_largest_component_labels",
    "native_anonymous_largest_component_arcs",
    "native_anonymous_largest_component_roots",
    "native_anonymous_maximum_root_interval_span",
    "native_anonymous_maximum_open_root_intervals",
)
_ANONYMOUS_WORK_TIMING_NAMES = (
    "native_anonymous_total_setup_work",
    "native_anonymous_total_refinement_work",
    "native_anonymous_total_candidate_order_work",
    "native_anonymous_total_canonical_work",
    "native_anonymous_largest_component_work",
    "native_anonymous_maximum_refinement_rounds",
    "native_anonymous_total_permutations_examined",
)
_ANONYMOUS_ALLOCATION_TIMING_NAMES = ("native_anonymous_accounted_bytes",)
_MAX_EXACT_FLOAT_INTEGER = 1 << 53


def _retained_phase_timings(
    metadata: object,
    phase_names: tuple[str, ...],
    *,
    label: str,
) -> tuple[tuple[str, float], ...]:
    if type(metadata) is not tuple or len(metadata) != 2:
        raise BackendProtocolError(
            f"native retained {label} parser returned invalid phase metadata",
            code="NATIVE_RESULT_TYPE",
        )
    phases, anonymous = metadata
    if (
        type(phases) is not tuple
        or len(phases) != len(phase_names)
        or not all(type(value) is int and value >= 0 for value in phases)
        or type(anonymous) is not tuple
        or len(anonymous) != 3
    ):
        raise BackendProtocolError(
            f"native retained {label} parser returned invalid phase metadata",
            code="NATIVE_RESULT_TYPE",
        )
    shape, work, allocation = anonymous
    if (
        type(shape) is not tuple
        or len(shape) != len(_ANONYMOUS_SHAPE_TIMING_NAMES)
        or type(work) is not tuple
        or len(work) != len(_ANONYMOUS_WORK_TIMING_NAMES)
        or type(allocation) is not tuple
        or len(allocation) != len(_ANONYMOUS_ALLOCATION_TIMING_NAMES)
    ):
        raise BackendProtocolError(
            f"native retained {label} parser returned invalid anonymous metrics",
            code="NATIVE_RESULT_TYPE",
        )
    metrics = shape + work + allocation
    if not all(type(value) is int and 0 <= value <= _MAX_EXACT_FLOAT_INTEGER for value in metrics):
        raise BackendProtocolError(
            f"native retained {label} parser returned inexact anonymous metrics",
            code="NATIVE_RESULT_TYPE",
        )
    return tuple(
        (name, value / 1_000_000_000) for name, value in zip(phase_names, phases, strict=True)
    ) + tuple(
        (name, float(value))
        for name, value in zip(
            _ANONYMOUS_SHAPE_TIMING_NAMES
            + _ANONYMOUS_WORK_TIMING_NAMES
            + _ANONYMOUS_ALLOCATION_TIMING_NAMES,
            metrics,
            strict=True,
        )
    )


@dataclass(frozen=True, slots=True)
class _CachedRuntime:
    key: tuple[int, int]
    probe: NativeProbe
    extension: _Extension | None


_probe_lock = threading.Lock()
_cached_runtime: _CachedRuntime | None = None
T = TypeVar("T")


def _after_fork_child() -> None:
    global _cached_runtime, _probe_lock
    _probe_lock = threading.Lock()
    _cached_runtime = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)


def probe(capability: str | None = None, *, refresh: bool = False) -> NativeProbe:
    """Probe ABI/self-test compatibility once, then apply one capability gate."""

    if capability is not None and (not isinstance(capability, str) or not capability):
        raise ValueError("capability must be a nonempty string or None")
    runtime = _runtime(refresh=refresh)
    result = runtime.probe
    if result.available and capability is not None and capability not in result.features:
        return NativeProbe(
            False,
            f"installed native backend lacks required capability {capability!r}",
            result.version,
            result.features,
        )
    return result


def require(capability: str) -> _Extension:
    """Return a compatible extension or raise before operation work starts."""

    result = probe(capability)
    runtime = _runtime()
    if not result.available or runtime.extension is None:
        raise BackendUnavailableError(
            f"native backend unavailable: {result.reason or 'unknown compatibility failure'}",
            code=(
                "NATIVE_CAPABILITY_UNAVAILABLE"
                if runtime.probe.available
                else "NATIVE_BACKEND_UNAVAILABLE"
            ),
        )
    return runtime.extension


def validate_canonical(
    data: object,
    *,
    limits: ParseLimits | None = None,
    cancellation_token: CancellationToken | None = None,
) -> bytes:
    extension = require("canonical-model-v2")
    selected = _coerce_limits(limits)
    config = _encode_config(selected, cancellation_token, verify=True)
    with _relay(extension, selected, cancellation_token) as cancel:
        return _call(extension, lambda: extension.validate_canonical(data, config, cancel))


def _snapshot_writable_wire_input(data: object) -> object:
    """Own mutable wire bytes before any capability or cancellation setup."""

    if type(data) is bytes:
        return data
    try:
        view = memoryview(cast(Any, data))
    except (TypeError, ValueError):
        return data
    try:
        if view.readonly or not view.c_contiguous:
            return data
        return view.tobytes()
    finally:
        view.release()


def validate_wire(
    data: object,
    *,
    limits: ParseLimits | None = None,
    verify: bool = True,
    cancellation_token: CancellationToken | None = None,
) -> NativeValidation:
    data = _snapshot_writable_wire_input(data)
    extension = require("wire-v1")
    selected = _coerce_limits(limits)
    if not isinstance(verify, bool):
        raise TypeError("verify must be bool")
    config = _encode_config(selected, cancellation_token, verify=verify)
    with _relay(extension, selected, cancellation_token) as cancel:
        receipt = _call(extension, lambda: extension.validate_wire(data, config, cancel))
    return _decode_receipt(receipt)


def roundtrip_wire(
    data: object,
    *,
    limits: ParseLimits | None = None,
    verify: bool = True,
    cancellation_token: CancellationToken | None = None,
) -> bytes:
    data = _snapshot_writable_wire_input(data)
    extension = require("wire-v1")
    selected = _coerce_limits(limits)
    if not isinstance(verify, bool):
        raise TypeError("verify must be bool")
    config = _encode_config(selected, cancellation_token, verify=verify)
    with _relay(extension, selected, cancellation_token) as cancel:
        result = _call(extension, lambda: extension.roundtrip_wire(data, config, cancel))
    if not isinstance(result, bytes):
        raise BackendProtocolError(
            "native wire operation returned a non-bytes result",
            code="NATIVE_RESULT_TYPE",
        )
    return result


def parse_functional(
    data: bytes,
    *,
    limits: ParseLimits | None = None,
    allow_swrl: bool = False,
    cancellation_token: CancellationToken | None = None,
) -> ParsedOntology:
    """Parse one complete Functional-Style document through the native capability."""

    extension = require("parse-functional-v1")
    selected = _coerce_limits(limits)
    if not isinstance(data, bytes):
        raise TypeError("native Functional Syntax source must be bytes")
    if not isinstance(allow_swrl, bool):
        raise TypeError("allow_swrl must be bool")
    selected.enforce("max_source_bytes", len(data))
    request = (
        _PARSE_REQUEST.pack(
            _PARSE_REQUEST_MAGIC,
            1,
            int(allow_swrl),
            len(data),
        )
        + data
    )
    config = _encode_config(selected, cancellation_token, verify=False)
    with _relay(extension, selected, cancellation_token) as cancel:
        result = _call_parse(
            extension,
            lambda: extension.parse_document(request, config, cancel),
        )
    return _decode_parsed_functional(result, selected)


def _parse_functional_retained_v2(
    data: bytes,
    *,
    limits: ParseLimits | None = None,
    allow_swrl: bool = False,
    collect_provenance: bool = True,
    preserve_source_map: bool = False,
    record_unresolved: bool = False,
    require_empty_imports: bool = False,
    materialize_document: bool = False,
    cancellation_token: CancellationToken | None = None,
) -> _NativeRetainedFunctionalParseV2:
    """Parse once and retain the parser-built structural arena when available."""

    extension = require("parse-functional-v1")
    hook = getattr(extension, "_parse_functional_retained_v2", None)
    if not callable(hook):
        return _NativeRetainedFunctionalParseV2(
            parse_functional(
                data,
                limits=limits,
                allow_swrl=allow_swrl,
                cancellation_token=cancellation_token,
            ),
            None,
            (),
        )
    selected = _coerce_limits(limits)
    if not isinstance(data, bytes):
        raise TypeError("native Functional Syntax source must be bytes")
    if not isinstance(allow_swrl, bool):
        raise TypeError("allow_swrl must be bool")
    if not isinstance(collect_provenance, bool):
        raise TypeError("collect_provenance must be bool")
    if not isinstance(preserve_source_map, bool):
        raise TypeError("preserve_source_map must be bool")
    if not isinstance(record_unresolved, bool):
        raise TypeError("record_unresolved must be bool")
    if not isinstance(require_empty_imports, bool):
        raise TypeError("require_empty_imports must be bool")
    if not isinstance(materialize_document, bool):
        raise TypeError("materialize_document must be bool")
    selected.enforce("max_source_bytes", len(data))
    request = (
        _PARSE_REQUEST.pack(
            _PARSE_REQUEST_MAGIC,
            1,
            int(allow_swrl),
            len(data),
        )
        + data
    )
    config = _encode_config(selected, cancellation_token, verify=False)
    with _relay(extension, selected, cancellation_token) as cancel:
        result = _call_parse_value(
            extension,
            lambda: hook(
                request,
                config,
                collect_provenance,
                preserve_source_map,
                record_unresolved,
                require_empty_imports,
                cancel,
                materialize_document=materialize_document,
            ),
        )
    if type(result) is not tuple or len(result) != 3:
        raise BackendProtocolError(
            "native retained parser returned invalid result framing",
            code="NATIVE_RESULT_TYPE",
        )
    framing, storage, phases = result
    storage_type = getattr(extension, "_NativeParsedStructuralStorageV2", None)
    if not isinstance(framing, bytes) or not isinstance(storage_type, type):
        raise BackendProtocolError(
            "native retained parser returned invalid result members",
            code="NATIVE_RESULT_TYPE",
        )
    if type(storage) is not storage_type:
        raise BackendProtocolError(
            "native retained parser returned an invalid storage owner",
            code="NATIVE_RESULT_TYPE",
        )
    names = (
        "native_syntax_parse_seconds",
        "native_result_encode_seconds",
        "native_arena_construction_seconds",
        "native_freeze_seconds",
    )
    phase_timings = _retained_phase_timings(
        phases,
        names,
        label="Functional",
    )
    if framing.startswith(_PARSE_RESULT_MAGIC):
        return _NativeRetainedFunctionalParseV2(
            None,
            storage,
            phase_timings,
            framing,
        )
    if framing.startswith(_RETAINED_FUNCTIONAL_SEED_MAGIC_V2):
        return _NativeRetainedFunctionalParseV2(
            None,
            storage,
            phase_timings,
            summary=framing,
        )
    raise BackendProtocolError(
        "native retained parser returned an unknown result framing",
        code="NATIVE_PARSE_VERSION",
    )


def _parse_rdfxml_retained_v2(
    data: bytes,
    *,
    document_iri: str | None,
    limits: ParseLimits | None = None,
    collect_provenance: bool = False,
    preserve_source_map: bool = False,
    allow_partial_rdf_mapping: bool = False,
    allow_swrl: bool = False,
    require_empty_imports: bool = False,
    cancellation_token: CancellationToken | None = None,
) -> _NativeRetainedFunctionalParseV2:
    """Use the private retained RDF/XML operation behind its public capability."""

    return _parse_structural_retained_v2(
        "rdfxml",
        data,
        document_iri=document_iri,
        limits=limits,
        collect_provenance=collect_provenance,
        preserve_source_map=preserve_source_map,
        allow_partial_rdf_mapping=allow_partial_rdf_mapping,
        allow_swrl=allow_swrl,
        require_empty_imports=require_empty_imports,
        cancellation_token=cancellation_token,
    )


def _parse_turtle_retained_v2(
    data: bytes,
    *,
    document_iri: str | None,
    limits: ParseLimits | None = None,
    collect_provenance: bool = False,
    preserve_source_map: bool = False,
    allow_partial_rdf_mapping: bool = False,
    allow_swrl: bool = False,
    require_empty_imports: bool = False,
    cancellation_token: CancellationToken | None = None,
) -> _NativeRetainedFunctionalParseV2:
    """Use the private retained Turtle operation behind its public capability."""

    return _parse_structural_retained_v2(
        "turtle",
        data,
        document_iri=document_iri,
        limits=limits,
        collect_provenance=collect_provenance,
        preserve_source_map=preserve_source_map,
        allow_partial_rdf_mapping=allow_partial_rdf_mapping,
        allow_swrl=allow_swrl,
        require_empty_imports=require_empty_imports,
        cancellation_token=cancellation_token,
    )


def _parse_owlxml_retained_v2(
    data: bytes,
    *,
    document_iri: str | None,
    limits: ParseLimits | None = None,
    collect_provenance: bool = False,
    preserve_source_map: bool = False,
    allow_swrl: bool = False,
    require_empty_imports: bool = False,
    cancellation_token: CancellationToken | None = None,
) -> _NativeRetainedFunctionalParseV2:
    """Use the private retained OWL/XML operation behind its public capability."""

    return _parse_structural_retained_v2(
        "owlxml",
        data,
        document_iri=document_iri,
        limits=limits,
        collect_provenance=collect_provenance,
        preserve_source_map=preserve_source_map,
        allow_partial_rdf_mapping=False,
        allow_swrl=allow_swrl,
        require_empty_imports=require_empty_imports,
        cancellation_token=cancellation_token,
    )


def _parse_structural_retained_v2(
    syntax: str,
    data: bytes,
    *,
    document_iri: str | None,
    limits: ParseLimits | None,
    collect_provenance: bool,
    preserve_source_map: bool,
    allow_partial_rdf_mapping: bool,
    allow_swrl: bool,
    require_empty_imports: bool,
    cancellation_token: CancellationToken | None,
) -> _NativeRetainedFunctionalParseV2:
    if syntax not in {"rdfxml", "turtle", "owlxml"}:
        raise AssertionError("unknown retained structural syntax")
    label = {"rdfxml": "RDF/XML", "turtle": "Turtle", "owlxml": "OWL/XML"}[syntax]
    if syntax == "owlxml" and allow_partial_rdf_mapping:
        raise AssertionError("OWL/XML has no partial RDF mapping mode")
    runtime = _runtime()
    extension = runtime.extension
    if not runtime.probe.available or extension is None:
        raise BackendUnavailableError(
            "native backend unavailable: "
            f"{runtime.probe.reason or 'unknown compatibility failure'}",
            code="NATIVE_BACKEND_UNAVAILABLE",
        )
    hook_name = (
        f"_parse_{syntax}_retained_source_map_v2"
        if preserve_source_map
        else f"_parse_{syntax}_retained_v2"
    )
    hook = getattr(extension, hook_name, None)
    if not callable(hook):
        raise BackendUnavailableError(
            f"native backend lacks the private retained {label} ingestion seam",
            code="NATIVE_CAPABILITY_UNAVAILABLE",
        )
    selected = _coerce_limits(limits)
    if not isinstance(data, bytes):
        raise TypeError(f"native {label} source must be bytes")
    if document_iri is not None and not isinstance(document_iri, str):
        raise TypeError("document_iri must be str or None")
    for name, value in (
        ("collect_provenance", collect_provenance),
        ("preserve_source_map", preserve_source_map),
        ("allow_partial_rdf_mapping", allow_partial_rdf_mapping),
        ("allow_swrl", allow_swrl),
        ("require_empty_imports", require_empty_imports),
    ):
        if not isinstance(value, bool):
            raise TypeError(f"{name} must be bool")
    selected.enforce("max_source_bytes", len(data))
    config = _encode_config(selected, cancellation_token, verify=False)
    with _relay(extension, selected, cancellation_token) as cancel:
        if syntax == "owlxml":
            result = _call_parse_value(
                extension,
                lambda: hook(
                    data,
                    document_iri,
                    config,
                    collect_provenance,
                    allow_swrl,
                    require_empty_imports,
                    cancel,
                ),
            )
        else:
            result = _call_parse_value(
                extension,
                lambda: hook(
                    data,
                    document_iri,
                    config,
                    collect_provenance,
                    allow_partial_rdf_mapping,
                    allow_swrl,
                    require_empty_imports,
                    cancel,
                ),
            )
    if type(result) is not tuple or len(result) != 3:
        raise BackendProtocolError(
            f"native retained {label} parser returned invalid result framing",
            code="NATIVE_RESULT_TYPE",
        )
    framing, storage, phases = result
    storage_type = getattr(extension, "_NativeParsedStructuralStorageV2", None)
    if (
        not isinstance(framing, bytes)
        or not isinstance(storage_type, type)
        or type(storage) is not storage_type
    ):
        raise BackendProtocolError(
            f"native retained {label} parser returned invalid result members",
            code="NATIVE_RESULT_TYPE",
        )
    expected_magic = (
        _RETAINED_FUNCTIONAL_SEED_MAGIC_V2 if syntax == "owlxml" else _RETAINED_RDFXML_SEED_MAGIC_V2
    )
    if not framing.startswith(expected_magic):
        raise BackendProtocolError(
            f"native retained {label} parser returned unknown metadata",
            code="NATIVE_PARSE_VERSION",
        )
    names = (
        f"native_{syntax}_syntax_parse_seconds",
        (
            "native_owlxml_structural_mapping_seconds"
            if syntax == "owlxml"
            else "native_rdf_mapping_seconds"
        ),
        "native_result_encode_seconds",
        "native_arena_construction_seconds",
        "native_freeze_seconds",
    )
    phase_timings = _retained_phase_timings(
        phases,
        names,
        label=label,
    )
    return _NativeRetainedFunctionalParseV2(
        None,
        storage,
        phase_timings,
        summary=framing,
    )


def _fork_parsed_structural_storage_v2(
    parsed_native_storage: object,
    *,
    limits: ParseLimits,
    cancellation_token: CancellationToken | None = None,
) -> object:
    """Fork parser root manifests while retaining the original arena owner."""

    runtime = _runtime()
    extension = runtime.extension
    if not runtime.probe.available or extension is None:
        raise BackendUnavailableError(
            "native backend unavailable: "
            f"{runtime.probe.reason or 'unknown compatibility failure'}",
            code="NATIVE_BACKEND_UNAVAILABLE",
        )
    hook = getattr(extension, "_fork_parsed_structural_storage_v2", None)
    storage_type = getattr(extension, "_NativeParsedStructuralStorageV2", None)
    if not callable(hook) or not isinstance(storage_type, type):
        raise BackendUnavailableError(
            "native backend lacks the private parser-storage fork seam",
            code="NATIVE_CAPABILITY_UNAVAILABLE",
        )
    if type(parsed_native_storage) is not storage_type:
        raise TypeError("parsed_native_storage has an invalid native owner type")
    selected = _coerce_limits(limits)
    with _relay(extension, selected, cancellation_token) as cancel:
        fork = _call(
            extension,
            lambda: hook(parsed_native_storage, cancel),
        )
    if type(fork) is not storage_type:
        raise BackendProtocolError(
            "native parser-storage fork returned an invalid owner",
            code="NATIVE_RESULT_TYPE",
        )
    return fork


def partition_axioms(
    axioms: tuple[AxiomNode, ...],
    *,
    limits: ParseLimits | None = None,
    cancellation_token: CancellationToken | None = None,
) -> NativeAxiomPartition:
    """Build exact-constructor postings through one coarse native operation."""

    extension = require("index-axiom-types-v1")
    selected = _coerce_limits(limits)
    if not isinstance(axioms, tuple):
        raise TypeError("axioms must be a tuple")
    from pyowl_core.model import canonical_bytes
    from pyowl_core.model.axioms import AxiomNode

    if not all(isinstance(value, AxiomNode) for value in axioms):
        raise TypeError("axioms must contain AxiomNode values")
    selected.enforce("max_index_rows", len(axioms))
    source = bytearray(_INDEX_SOURCE_HEADER.pack(_INDEX_SOURCE_MAGIC, 1, 0, len(axioms)))
    canonical_sizes: list[int] = []
    for ordinal, axiom in enumerate(axioms, 1):
        if cancellation_token is not None and (ordinal % selected.cancellation_check_interval == 0):
            cancellation_token.check()
        encoded = canonical_bytes(axiom, limits=selected)
        canonical_sizes.append(len(encoded))
        source.extend(struct.pack("<Q", len(encoded)))
        source.extend(encoded)
        selected.enforce("max_index_bytes", len(source))
    config = _encode_config(selected, cancellation_token, verify=False)
    request = _INDEX_REQUEST_MAGIC + config
    with _relay(extension, selected, cancellation_token) as cancel:
        result = _call_index(
            extension,
            lambda: extension.build_index(source, request, cancel),
        )
    return NativeAxiomPartition(
        _decode_axiom_partition(result, axioms, selected),
        tuple(canonical_sizes),
    )


def _retained_axiom_partition_v1(
    ontology: OntologyView,
    *,
    scope: object,
    document_key: str | None,
    limits: ParseLimits,
    cancellation_token: CancellationToken | None,
) -> NativeRetainedAxiomPartition | None:
    """Build postings directly from a compatible retained V2 arena."""

    state = getattr(ontology, "_native_snapshot_state", None)
    owner_state = getattr(state, "owner", None)
    handle = getattr(owner_state, "handle", None)
    if handle is None:
        return None
    try:
        raw_owner = object.__getattribute__(handle, "_owner_v2")
    except AttributeError:
        return None
    try:
        extension = cast(_Extension, importlib.import_module(type(raw_owner).__module__))
    except (ImportError, ValueError):
        return None
    raw_operation = getattr(extension, "_retained_axiom_type_index_v1", None)
    owner_type = getattr(extension, "_NativeRetainedAxiomTypeIndexV1", None)
    if not callable(raw_operation) or not isinstance(owner_type, type):
        return None
    native_scope = getattr(ontology, "_native_scope", None)
    if not callable(native_scope):
        return None
    selected_scope, document_ordinal = native_scope(scope, document_key)
    scope_value = getattr(selected_scope, "value", None)
    if type(scope_value) is not str or scope_value not in {"closure", "document"}:
        raise BackendProtocolError(
            "native retained index returned an invalid scope",
            code="NATIVE_INDEX_SCOPE",
        )
    config = _encode_config(limits, cancellation_token, verify=False)
    operation = cast(Callable[..., object], raw_operation)
    with _relay(extension, limits, cancellation_token) as cancel:
        raw_index = _call_index_value(
            extension,
            lambda: operation(
                raw_owner,
                scope_value,
                document_ordinal,
                config,
                cancel,
            ),
        )
    if type(raw_index) is not owner_type:
        raise BackendProtocolError(
            "native retained index returned an invalid owner",
            code="NATIVE_INDEX_RESULT",
        )
    retained_owner = cast(_NativeRetainedAxiomTypeIndex, raw_index)
    binding = retained_owner._binding_v1()
    attestation_method = getattr(handle, "_attestation_v2", None)
    if (
        type(binding) is not tuple
        or len(binding) != 2
        or any(type(value) is not bytes or len(value) != 32 for value in binding)
        or not callable(attestation_method)
    ):
        raise BackendProtocolError(
            "native retained index returned an invalid publication binding",
            code="NATIVE_INDEX_RESULT",
        )
    attestation = attestation_method()
    if binding != (
        getattr(attestation, "root_table_sha256", None),
        getattr(attestation, "effective_root_table_sha256", None),
    ):
        raise BackendProtocolError(
            "native retained index belongs to a foreign publication",
            code="NATIVE_INDEX_RESULT",
        )
    canonical_sizes = retained_owner._canonical_sizes_v1()
    if type(canonical_sizes) is not tuple or any(
        type(value) is not int or not 0 < value < 1 << 64 for value in canonical_sizes
    ):
        raise BackendProtocolError(
            "native retained index returned invalid canonical sizes",
            code="NATIVE_INDEX_RESULT",
        )
    layout = retained_owner._layout_v1()
    if type(layout) is not tuple or len(layout) != 6:
        raise BackendProtocolError(
            "native retained index returned invalid framing",
            code="NATIVE_INDEX_RESULT",
        )
    tags, offsets, category_codes, category_offsets, postings, counters = layout
    for name, values, maximum in (
        ("tags", tags, 0xFFFF),
        ("offsets", offsets, (1 << 64) - 1),
        ("category codes", category_codes, 0xFF),
        ("category offsets", category_offsets, (1 << 64) - 1),
        ("postings", postings, (1 << 64) - 1),
    ):
        if type(values) is not tuple or any(
            type(value) is not int or not 0 <= value <= maximum for value in values
        ):
            raise BackendProtocolError(
                f"native retained index returned invalid {name}",
                code="NATIVE_INDEX_RESULT",
            )
    expected_counter_names = {
        "axiom_rows",
        "constructor_groups",
        "category_groups",
        "retained_buffer_bytes",
        "peak_owned_bytes",
        "canonical_work",
        "complete_root_encode_calls",
    }
    if (
        type(counters) is not dict
        or set(counters) != expected_counter_names
        or any(type(value) is not int or value < 0 for value in counters.values())
    ):
        raise BackendProtocolError(
            "native retained index returned invalid counters",
            code="NATIVE_INDEX_RESULT",
        )
    if (
        tags != tuple(sorted(set(tags)))
        or category_codes != tuple(sorted(set(category_codes)))
        or any(value not in {1, 2, 3} for value in category_codes)
        or len(offsets) != len(tags) + 1
        or len(category_offsets) != len(category_codes) + 1
        or offsets[0] != 0
        or category_offsets[0] != 0
        or offsets[-1] != len(postings)
        or category_offsets[-1] != len(postings)
        or any(left > right for left, right in pairwise(offsets))
        or any(left > right for left, right in pairwise(category_offsets))
        or postings != tuple(range(len(postings)))
        or len(canonical_sizes) != len(postings)
        or counters["axiom_rows"] != len(postings)
        or counters["constructor_groups"] != len(tags)
        or counters["category_groups"] != len(category_codes)
        or counters["complete_root_encode_calls"] != 0
        or counters["peak_owned_bytes"] < counters["retained_buffer_bytes"]
    ):
        raise BackendProtocolError(
            "native retained index layout is internally inconsistent",
            code="NATIVE_INDEX_RESULT",
        )
    from pyowl_core.model.axioms import AxiomNode
    from pyowl_core.model.registry import SPEC_BY_TAG

    category_code_by_name = {
        "declaration_axiom": 1,
        "logical_axiom": 2,
        "annotation_axiom": 3,
    }
    expected_category_codes: list[int] = []
    expected_category_offsets = [0]
    previous_category: int | None = None
    for group, tag in enumerate(tags):
        spec = SPEC_BY_TAG.get(tag)
        category = None if spec is None else category_code_by_name.get(spec.category)
        if spec is None or not issubclass(spec.constructor, AxiomNode) or category is None:
            raise BackendProtocolError(
                "native retained index contains an unknown axiom tag",
                code="NATIVE_INDEX_RESULT",
            )
        if category != previous_category:
            if previous_category is not None:
                expected_category_offsets.append(offsets[group])
            expected_category_codes.append(category)
            previous_category = category
    if expected_category_codes:
        expected_category_offsets.append(len(postings))
    if category_codes != tuple(expected_category_codes) or category_offsets != tuple(
        expected_category_offsets
    ):
        raise BackendProtocolError(
            "native retained index category groups diverge from the model schema",
            code="NATIVE_INDEX_RESULT",
        )
    return NativeRetainedAxiomPartition(
        cast(_NativeRetainedAxiomTypeIndex, raw_index),
        tags,
        offsets,
        category_codes,
        category_offsets,
        postings,
        canonical_sizes,
        counters["axiom_rows"],
        counters["constructor_groups"],
        counters["category_groups"],
        counters["retained_buffer_bytes"],
        counters["peak_owned_bytes"],
        counters["canonical_work"],
        counters["complete_root_encode_calls"],
    )


def _iter_retained_axiom_rows_v1(
    partition: NativeRetainedAxiomPartition,
    *,
    tag: int,
    limits: ParseLimits,
    cancellation_token: CancellationToken | None = None,
) -> Iterator[bytes]:
    """Page one exact constructor directly from a retained arena owner."""

    if type(partition) is not NativeRetainedAxiomPartition:
        raise TypeError("partition must be NativeRetainedAxiomPartition")
    if type(tag) is not int or not 0 <= tag <= 0xFFFF:
        raise ValueError("tag must be an unsigned 16-bit integer")
    if not isinstance(limits, ParseLimits):
        raise TypeError("limits must be ParseLimits")
    try:
        group = partition.tags.index(tag)
    except ValueError:
        expected_total = 0
        posting_start = 0
    else:
        posting_start = partition.offsets[group]
        expected_total = partition.offsets[group + 1] - posting_start
    try:
        extension = cast(_Extension, importlib.import_module(type(partition.owner).__module__))
    except (ImportError, ValueError) as error:
        raise BackendProtocolError(
            "native retained index owner module is unavailable",
            code="NATIVE_INDEX_RESULT",
        ) from error
    operation = getattr(partition.owner, "_page_v1", None)
    if not callable(operation):
        raise BackendProtocolError(
            "native retained index owner has no paging operation",
            code="NATIVE_INDEX_RESULT",
        )
    page_bytes = min(
        8 * 1024 * 1024,
        limits.max_temporary_bytes,
        limits.max_index_bytes,
        limits.max_wire_bytes,
        *(() if limits.max_memory_bytes is None else (limits.max_memory_bytes,)),
    )
    if page_bytes < 1:
        raise ResourceLimitError(
            "retained axiom page has no available temporary budget",
            limit="max_temporary_bytes",
            observed=1,
            allowed=page_bytes,
        )
    config = _encode_config(limits, cancellation_token, verify=False)
    cursor = 0
    with _relay(extension, limits, cancellation_token) as cancel:
        while True:

            def page_call(selected_cursor: int = cursor) -> object:
                return cast(Callable[..., object], operation)(
                    tag,
                    selected_cursor,
                    64,
                    page_bytes,
                    config,
                    cancel,
                )

            raw_page = _call_index_value(
                extension,
                page_call,
            )
            if type(raw_page) is not tuple or len(raw_page) != 3:
                raise BackendProtocolError(
                    "native retained index returned invalid page framing",
                    code="NATIVE_INDEX_RESULT",
                )
            rows, total_count, next_cursor = raw_page
            if (
                type(rows) is not tuple
                or len(rows) > 64
                or any(type(row) is not bytes or not row for row in rows)
                or type(total_count) is not int
                or total_count != expected_total
                or (
                    next_cursor is not None
                    and (type(next_cursor) is not int or next_cursor <= cursor)
                )
                or (not rows and next_cursor is not None)
            ):
                raise BackendProtocolError(
                    "native retained index returned an inconsistent page",
                    code="NATIVE_INDEX_RESULT",
                )
            for offset, row in enumerate(rows):
                posting_index = posting_start + cursor + offset
                if posting_index >= len(partition.postings):
                    raise BackendProtocolError(
                        "native retained index page exceeds its postings",
                        code="NATIVE_INDEX_RESULT",
                    )
                ordinal = partition.postings[posting_index]
                if len(row) != partition.canonical_sizes[ordinal]:
                    raise BackendProtocolError(
                        "native retained index page row has the wrong size",
                        code="NATIVE_INDEX_RESULT",
                    )
                yield row
            if next_cursor is None:
                if cursor + len(rows) != expected_total:
                    raise BackendProtocolError(
                        "native retained index terminated before its total",
                        code="NATIVE_INDEX_RESULT",
                    )
                return
            if next_cursor != cursor + len(rows) or next_cursor >= expected_total:
                raise BackendProtocolError(
                    "native retained index returned an invalid next cursor",
                    code="NATIVE_INDEX_RESULT",
                )
            cursor = next_cursor


def _retained_signature_counts_v1(
    ontology: OntologyView,
    *,
    scope: object,
    document_key: str | None,
    limits: ParseLimits,
    cancellation_token: CancellationToken | None,
) -> NativeRetainedSignatureCounts | None:
    """Count signature contributions directly over a compatible V2 arena."""

    state = getattr(ontology, "_native_snapshot_state", None)
    owner_state = getattr(state, "owner", None)
    handle = getattr(owner_state, "handle", None)
    if handle is None:
        return None
    try:
        raw_owner = object.__getattribute__(handle, "_owner_v2")
    except AttributeError:
        return None
    try:
        extension = cast(_Extension, importlib.import_module(type(raw_owner).__module__))
    except (ImportError, ValueError):
        return None
    raw_operation = getattr(extension, "_retained_signature_index_v1", None)
    owner_type = getattr(extension, "_NativeRetainedSignatureIndexV1", None)
    if not callable(raw_operation) or not isinstance(owner_type, type):
        return None
    native_scope = getattr(ontology, "_native_scope", None)
    if not callable(native_scope):
        return None
    selected_scope, document_ordinal = native_scope(scope, document_key)
    scope_value = getattr(selected_scope, "value", None)
    if type(scope_value) is not str or scope_value not in {"closure", "document"}:
        raise BackendProtocolError(
            "native retained signature returned an invalid scope",
            code="NATIVE_INDEX_SCOPE",
        )
    config = _encode_config(limits, cancellation_token, verify=False)
    operation = cast(Callable[..., object], raw_operation)
    with _relay(extension, limits, cancellation_token) as cancel:
        raw_index = _call_index_value(
            extension,
            lambda: operation(
                raw_owner,
                scope_value,
                document_ordinal,
                config,
                cancel,
            ),
        )
    if type(raw_index) is not owner_type:
        raise BackendProtocolError(
            "native retained signature returned an invalid owner",
            code="NATIVE_INDEX_RESULT",
        )
    layout = cast(_NativeRetainedSignatureIndex, raw_index)._layout_v1()
    if type(layout) is not tuple or len(layout) != 6:
        raise BackendProtocolError(
            "native retained signature returned invalid framing",
            code="NATIVE_INDEX_RESULT",
        )
    (
        root_table_sha256,
        effective_root_table_sha256,
        referenced,
        nonannotation,
        declarations,
        counters,
    ) = layout
    attestation_method = getattr(handle, "_attestation_v2", None)
    if not callable(attestation_method):
        raise BackendProtocolError(
            "native retained signature has no publication attestation",
            code="NATIVE_INDEX_RESULT",
        )
    attestation = attestation_method()
    if (
        type(root_table_sha256) is not bytes
        or len(root_table_sha256) != 32
        or type(effective_root_table_sha256) is not bytes
        or len(effective_root_table_sha256) != 32
        or root_table_sha256 != getattr(attestation, "root_table_sha256", None)
        or effective_root_table_sha256 != getattr(attestation, "effective_root_table_sha256", None)
    ):
        raise BackendProtocolError(
            "native retained signature belongs to a foreign publication",
            code="NATIVE_INDEX_RESULT",
        )
    for name, values in (
        ("referenced counts", referenced),
        ("nonannotation counts", nonannotation),
        ("declaration counts", declarations),
    ):
        if type(values) is not tuple or any(
            type(value) is not int or not 0 <= value < 1 << 64 for value in values
        ):
            raise BackendProtocolError(
                f"native retained signature returned invalid {name}",
                code="NATIVE_INDEX_RESULT",
            )
    expected_counter_names = {
        "structural_root_rows",
        "entity_rows",
        "referenced_links",
        "nonannotation_links",
        "declaration_links",
        "retained_buffer_bytes",
        "peak_owned_bytes",
        "canonical_work",
        "complete_root_encode_calls",
    }
    if (
        type(counters) is not dict
        or set(counters) != expected_counter_names
        or any(type(value) is not int or value < 0 for value in counters.values())
    ):
        raise BackendProtocolError(
            "native retained signature returned invalid counters",
            code="NATIVE_INDEX_RESULT",
        )
    if (
        len(referenced) != len(nonannotation)
        or len(referenced) != len(declarations)
        or counters["entity_rows"] != len(referenced)
        or any(value == 0 for value in referenced)
        or any(
            nonannotation_value > referenced_value
            for referenced_value, nonannotation_value in zip(referenced, nonannotation, strict=True)
        )
        or any(
            declaration_value > referenced_value
            for referenced_value, declaration_value in zip(referenced, declarations, strict=True)
        )
        or counters["referenced_links"] != sum(referenced)
        or counters["nonannotation_links"] != sum(nonannotation)
        or counters["declaration_links"] != sum(declarations)
        or counters["retained_buffer_bytes"] < len(referenced) * 3 * 8
        or counters["peak_owned_bytes"] < counters["retained_buffer_bytes"]
        or counters["complete_root_encode_calls"] != 0
    ):
        raise BackendProtocolError(
            "native retained signature layout is internally inconsistent",
            code="NATIVE_INDEX_RESULT",
        )
    return NativeRetainedSignatureCounts(
        cast(_NativeRetainedSignatureIndex, raw_index),
        referenced,
        nonannotation,
        declarations,
        counters["structural_root_rows"],
        counters["entity_rows"],
        counters["referenced_links"],
        counters["nonannotation_links"],
        counters["declaration_links"],
        counters["retained_buffer_bytes"],
        counters["peak_owned_bytes"],
        counters["canonical_work"],
        counters["complete_root_encode_calls"],
    )


def _retained_ontology_identity_index_owner_v1(
    ontology: OntologyView,
) -> _NativeRetainedOntologyIdentityIndex | None:
    """Retain attested identity/import/diagnostic metadata without root work."""

    state = getattr(ontology, "_native_snapshot_state", None)
    owner_state = getattr(state, "owner", None)
    handle = getattr(owner_state, "handle", None)
    if handle is None:
        return None
    try:
        raw_owner = object.__getattribute__(handle, "_owner_v2")
    except AttributeError:
        return None
    try:
        extension = cast(_Extension, importlib.import_module(type(raw_owner).__module__))
    except (ImportError, ValueError):
        return None
    raw_operation = getattr(extension, "_retained_ontology_identity_index_v1", None)
    owner_type = getattr(extension, "_NativeRetainedOntologyIdentityIndexV1", None)
    attestation_method = getattr(handle, "_attestation_v2", None)
    if (
        not callable(raw_operation)
        or not isinstance(owner_type, type)
        or not callable(attestation_method)
    ):
        return None
    attestation = attestation_method()
    expected_root = getattr(attestation, "root_document_key", None)
    expected_metadata = getattr(attestation, "metadata_manifest_sha256", None)
    expected_diagnostics = getattr(attestation, "diagnostics_manifest_sha256", None)
    expected_report = getattr(attestation, "report_sha256", None)
    expected_counts = {
        "document_count": getattr(attestation, "document_count", None),
        "import_edge_count": getattr(attestation, "import_edge_count", None),
        "diagnostic_count": getattr(attestation, "diagnostic_count", None),
    }
    if (
        type(expected_root) is not str
        or not expected_root
        or any(
            type(value) is not bytes or len(value) != 32
            for value in (
                expected_metadata,
                expected_diagnostics,
                expected_report,
            )
        )
        or any(type(value) is not int or value < 0 for value in expected_counts.values())
    ):
        raise BackendProtocolError(
            "native retained identity attestation is invalid",
            code="NATIVE_INDEX_RESULT",
        )
    operation = cast(Callable[[object], object], raw_operation)
    raw_index = _call_index_value(extension, lambda: operation(raw_owner))
    if type(raw_index) is not owner_type:
        raise BackendProtocolError(
            "native retained identity index returned an invalid owner",
            code="NATIVE_INDEX_RESULT",
        )
    layout = cast(_NativeRetainedOntologyIdentityIndex, raw_index)._layout_v1()
    if type(layout) is not tuple or len(layout) != 5:
        raise BackendProtocolError(
            "native retained identity index returned invalid framing",
            code="NATIVE_INDEX_RESULT",
        )
    root_document_key, metadata_digest, diagnostic_digest, report_digest, counters = layout
    expected_counter_names = {
        "document_count",
        "import_edge_count",
        "diagnostic_count",
        "retained_owner_bytes",
        "complete_root_encode_calls",
    }
    if (
        type(root_document_key) is not str
        or not root_document_key
        or any(
            type(value) is not bytes or len(value) != 32
            for value in (
                metadata_digest,
                diagnostic_digest,
                report_digest,
            )
        )
        or type(counters) is not dict
        or set(counters) != expected_counter_names
        or any(type(value) is not int or value < 0 for value in counters.values())
    ):
        raise BackendProtocolError(
            "native retained identity index returned invalid metadata",
            code="NATIVE_INDEX_RESULT",
        )
    manifest = getattr(ontology, "import_manifest", None)
    manifest_documents = getattr(manifest, "documents", None)
    manifest_edges = getattr(manifest, "edges", None)
    public_root = getattr(ontology, "root_document_key", None)
    if (
        root_document_key != expected_root
        or root_document_key != public_root
        or metadata_digest != expected_metadata
        or diagnostic_digest != expected_diagnostics
        or report_digest != expected_report
        or counters["document_count"] != expected_counts["document_count"]
        or counters["import_edge_count"] != expected_counts["import_edge_count"]
        or counters["diagnostic_count"] != expected_counts["diagnostic_count"]
        or type(manifest_documents) is not tuple
        or type(manifest_edges) is not tuple
        or counters["document_count"] != len(manifest_documents)
        or counters["import_edge_count"] != len(manifest_edges)
        or counters["retained_owner_bytes"] == 0
        or counters["complete_root_encode_calls"] != 0
    ):
        raise BackendProtocolError(
            "native retained identity index diverges from its publication",
            code="NATIVE_INDEX_RESULT",
        )
    return cast(_NativeRetainedOntologyIdentityIndex, raw_index)


def encode_snapshot(
    snapshot: OntologyView,
    *,
    limits: ParseLimits | None = None,
    cancellation_token: CancellationToken | None = None,
) -> bytes:
    """Cross the native boundary only through frozen canonical wire bytes."""

    from pyowl_core.wire.codec import encode_snapshot as python_encode

    encoded = python_encode(snapshot, limits=limits, cancellation_token=cancellation_token)
    native = roundtrip_wire(encoded, limits=limits, cancellation_token=cancellation_token)
    if native != encoded:
        raise BackendProtocolError(
            "native wire encoder diverged from canonical Python bytes",
            code="NATIVE_WIRE_PARITY",
        )
    return native


def decode_snapshot(
    data: object,
    *,
    limits: ParseLimits | None = None,
    verify: bool = True,
    cancellation_token: CancellationToken | None = None,
) -> OntologySnapshot:
    """Validate natively, then validate/materialize the result in core Python."""

    from pyowl_core.wire.codec import decode_snapshot as python_decode

    native = roundtrip_wire(
        data,
        limits=limits,
        verify=verify,
        cancellation_token=cancellation_token,
    )
    return python_decode(
        native,
        limits=limits,
        verify=verify,
        cancellation_token=cancellation_token,
    )


def _runtime(*, refresh: bool = False) -> _CachedRuntime:
    global _cached_runtime
    key = (os.getpid(), _interpreter_id())
    with _probe_lock:
        retained = _cached_runtime
        if not refresh and retained is not None and retained.key == key:
            return retained
        policy_reason = _runtime_policy_reason()
        if policy_reason is not None:
            selected = _CachedRuntime(key, NativeProbe(False, policy_reason, None, ()), None)
        else:
            selected = _load_runtime(key)
        _cached_runtime = selected
        return selected


def _load_runtime(key: tuple[int, int]) -> _CachedRuntime:
    try:
        module = importlib.import_module(_EXTENSION_NAME)
    except (ImportError, ModuleNotFoundError):
        return _unavailable(key, "native extension is not installed")
    except OSError:
        return _unavailable(key, "native extension could not be loaded")
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return _unavailable(key, "native extension import failed")
    extension = cast(_Extension, module)
    try:
        features = _validate_metadata(extension)
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return _unavailable(key, "native extension metadata is incompatible")
    try:
        extension.self_test()
        version = extension.version()
        if (
            not isinstance(version, tuple)
            or len(version) != 2
            or not isinstance(version[0], str)
            or not version[0]
            or version[1] != _ABI_VERSION
        ):
            return _unavailable(key, "native extension returned invalid version metadata")
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        code = _private_error_code(extension, error)
        suffix = "" if code is None else f" ({code})"
        return _unavailable(key, f"native extension self-test failed{suffix}")
    probe_result = NativeProbe(True, None, version[0], features)
    return _CachedRuntime(key, probe_result, extension)


def _validate_metadata(extension: _Extension) -> tuple[str, ...]:
    if extension.ABI_VERSION != _ABI_VERSION:
        raise ValueError("native ABI mismatch")
    if extension.MODEL_SCHEMA_VERSION != _MODEL_SCHEMA_VERSION:
        raise ValueError("native model schema mismatch")
    if extension.WIRE_FORMAT_VERSION != _WIRE_FORMAT_VERSION:
        raise ValueError("native wire version mismatch")
    features = extension.FEATURES
    if (
        type(features) is not tuple
        or not all(type(value) is str and value and value.isascii() for value in features)
        or tuple(sorted(set(features))) != features
        or not _FOUNDATION_FEATURES.issubset(features)
    ):
        raise ValueError("native feature ledger is invalid")
    ingestion = _validate_feature_partition("ingestion", extension.INGESTION_FEATURES, features)
    views = _validate_feature_partition("view", extension.VIEW_FEATURES, features)
    ingestion_set = set(ingestion)
    view_set = set(views)
    successor_features = ingestion_set | view_set
    if ingestion_set & view_set:
        raise ValueError("native ingestion and view feature partitions overlap")
    if successor_features & _FOUNDATION_FEATURE_LEDGER:
        raise ValueError("native successor feature partitions overlap the foundation")
    if set(features) != _FOUNDATION_FEATURE_LEDGER | successor_features:
        raise ValueError("native feature partitions do not cover the feature ledger")
    return features


def _validate_feature_partition(
    name: str,
    values: object,
    features: tuple[str, ...],
) -> tuple[str, ...]:
    if (
        type(values) is not tuple
        or not all(type(value) is str and value and value.isascii() for value in values)
        or tuple(sorted(set(values))) != values
        or not set(values).issubset(features)
    ):
        raise ValueError(f"native {name} feature partition is invalid")
    return cast(tuple[str, ...], values)


def _runtime_policy_reason() -> str | None:
    if platform.python_implementation() != "CPython":
        return "native extension is supported only on approved CPython builds"
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        return "native extension is not approved for free-threaded CPython"
    if _interpreter_id() != 0:
        return "native extension is not approved in subinterpreters"
    return None


def _interpreter_id() -> int:
    try:
        interpreters = cast(
            _Interpreters,
            importlib.import_module("concurrent.interpreters"),
        )
        current = int(interpreters.get_current().id)
        main = int(interpreters.get_main().id)
    except (ImportError, AttributeError, RuntimeError, TypeError, ValueError):
        pass
    else:
        return 0 if current == main else current + 1

    try:
        legacy = cast(
            _Subinterpreters,
            importlib.import_module("_xxsubinterpreters"),
        )
        current = int(legacy.get_current())
        main = int(legacy.get_main())
    except (ImportError, AttributeError, RuntimeError, TypeError, ValueError):
        return 0
    return 0 if current == main else current + 1


def _unavailable(key: tuple[int, int], reason: str) -> _CachedRuntime:
    return _CachedRuntime(key, NativeProbe(False, reason, None, ()), None)


def _coerce_limits(limits: ParseLimits | None) -> ParseLimits:
    if limits is None:
        return ParseLimits()
    if not isinstance(limits, ParseLimits):
        raise TypeError("limits must be ParseLimits or None")
    return limits


def _encode_config(
    limits: ParseLimits,
    cancellation_token: CancellationToken | None,
    *,
    verify: bool,
) -> bytes:
    if cancellation_token is not None and not isinstance(cancellation_token, CancellationToken):
        raise TypeError("cancellation_token must be CancellationToken or None")
    if cancellation_token is not None:
        cancellation_token.check()
    deadline = limits.deadline_seconds
    if cancellation_token is not None and cancellation_token.remaining_seconds is not None:
        remaining = cancellation_token.remaining_seconds
        deadline = remaining if deadline is None else min(deadline, remaining)
    deadline_ns = 0
    if deadline is not None:
        if not math.isfinite(deadline) or deadline <= 0:
            raise OperationCancelledError("operation deadline exceeded", reason="deadline exceeded")
        deadline_ns = min(0xFFFF_FFFF_FFFF_FFFF, max(1, math.ceil(deadline * 1_000_000_000)))
    memory = 0 if limits.max_memory_bytes is None else _u64_limit(limits.max_memory_bytes)
    values = (
        _u64_limit(limits.max_source_bytes),
        _u64_limit(limits.max_documents),
        _u64_limit(limits.max_total_source_bytes),
        _u64_limit(limits.max_axioms),
        _u64_limit(limits.max_terms),
        _u64_limit(limits.max_nesting_depth),
        _u64_limit(limits.max_rdf_list_length),
        _u64_limit(limits.max_literal_bytes),
        _u64_limit(limits.max_iri_bytes),
        _u64_limit(limits.max_prefixes),
        _u64_limit(limits.max_import_depth),
        _u64_limit(limits.max_redirects),
        _u64_limit(limits.max_diagnostics),
        memory,
        deadline_ns,
        _u64_limit(limits.max_triples),
        _u64_limit(limits.max_strings),
        _u64_limit(limits.max_annotations),
        _u64_limit(limits.max_rule_atoms),
        _u64_limit(limits.max_sequence_arity),
        _u64_limit(limits.max_catalog_rewrites),
        _u64_limit(limits.max_resolver_attempts),
        _u64_limit(limits.max_concurrent_fetches),
        _u64_limit(limits.max_source_map_entries),
        _u64_limit(limits.max_origin_entries),
        _u64_limit(limits.max_overlay_depth),
        _u64_limit(limits.max_delta_entries),
        _u64_limit(limits.max_composite_members),
        _u64_limit(limits.max_index_rows),
        _u64_limit(limits.max_index_bytes),
        _u64_limit(limits.max_wire_rows),
        _u64_limit(limits.max_wire_bytes),
        _u64_limit(limits.max_temporary_bytes),
        _u64_limit(limits.max_disk_cache_bytes),
        _u64_limit(limits.max_decompressed_bytes),
        _u64_limit(limits.max_canonical_work),
        _u64_limit(limits.cancellation_check_interval),
    )
    return _CONFIG.pack(
        _CONFIG_MAGIC,
        1,
        int(verify),
        0,
        *values,
    )


def _u64_limit(value: int) -> int:
    return min(value, 0xFFFF_FFFF_FFFF_FFFF)


class _Relay:
    __slots__ = ("_cancel", "_stop", "_thread", "_token")

    def __init__(
        self,
        extension: _Extension,
        limits: ParseLimits,
        token: CancellationToken | None,
    ) -> None:
        deadline = limits.deadline_seconds
        if token is not None and token.remaining_seconds is not None:
            deadline = (
                token.remaining_seconds
                if deadline is None
                else min(deadline, token.remaining_seconds)
            )
        self._cancel = extension._Cancellation(deadline)
        self._stop = threading.Event()
        self._token = token
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _NativeCancellation:
        if self._token is not None:
            thread = threading.Thread(
                target=self._watch,
                name="pyowl-core-native-cancel",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        return self._cancel

    def __exit__(self, *_error: object) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)

    def _watch(self) -> None:
        token = self._token
        if token is None:
            return
        while not self._stop.wait(0.001):
            if token.cancelled:
                self._cancel.cancel()
                return


def _relay(
    extension: _Extension,
    limits: ParseLimits,
    token: CancellationToken | None,
) -> _Relay:
    return _Relay(extension, limits, token)


def _call(extension: _Extension, operation: Callable[[], T]) -> T:
    try:
        return operation()
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        code = _private_error_code(extension, error)
        if code is None:
            raise BackendProtocolError(
                "native backend raised an unrecognized exception",
                code="NATIVE_EXCEPTION",
            ) from error
        message = _private_error_message(error)
        if code == "NATIVE_CANCELLED":
            raise OperationCancelledError(message, code=code) from error
        if code in {"NATIVE_DEADLINE", "NATIVE_WIRE_LIMIT"}:
            raise _native_resource_limit_error(extension, error, message, code) from error
        if code == "NATIVE_WIRE_VERSION":
            raise WireVersionError(message, code=code) from error
        if code == "NATIVE_WIRE_CORRUPTION":
            raise WireCorruptionError(message, code=code) from error
        if code == "NATIVE_CAPABILITY_UNAVAILABLE":
            raise BackendUnavailableError(message, code=code) from error
        raise BackendProtocolError(message, code=code) from error


def _call_parse_value(extension: _Extension, operation: Callable[[], object]) -> object:
    try:
        result = operation()
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        code = _private_error_code(extension, error)
        if code is None:
            raise BackendProtocolError(
                "native parser raised an unrecognized exception",
                code="NATIVE_EXCEPTION",
            ) from error
        message = _private_error_message(error)
        if code == "NATIVE_CANCELLED":
            raise OperationCancelledError(message, code=code) from error
        if code in {"NATIVE_DEADLINE", "NATIVE_WIRE_LIMIT"}:
            raise _native_resource_limit_error(extension, error, message, code) from error
        if code in {
            "NATIVE_FORMAT_ENCODING",
            "NATIVE_FORMAT_SYNTAX",
            "NATIVE_RDFXML_INVALID_BASE_IRI",
            "NATIVE_RDFXML_IRI_REFERENCE",
            "NATIVE_RDFXML_RELATIVE_IRI_NO_BASE",
            "NATIVE_RDFXML_SYNTAX",
            "NATIVE_OWLXML_ROOT",
            "NATIVE_OWLXML_SYNTAX",
            "NATIVE_TURTLE_ENCODING",
            "NATIVE_TURTLE_RELATIVE_IRI",
            "NATIVE_TURTLE_SYNTAX",
            "NATIVE_XML_FORBIDDEN_CONSTRUCT",
        }:
            raise OntologySyntaxError(message, code=code.removeprefix("NATIVE_")) from error
        if code == "NATIVE_RDF_AXIOM_REIFICATION":
            decoded = _private_reification_diagnostics(extension, error, message)
            if len(error.args) == 3 and decoded is None:
                raise BackendProtocolError(
                    "native RDF reification error payload is invalid",
                    code="NATIVE_ERROR_PAYLOAD",
                ) from error
            evidence: tuple[Diagnostic, ...] = ()
            issue_count: int | None = None
            diagnostic: Diagnostic | None = None
            if decoded is not None:
                evidence, issue_count, diagnostic = decoded
            raise UnsupportedSyntaxError(
                message,
                code="RDF_AXIOM_REIFICATION",
                diagnostic=diagnostic,
                reification_evidence=evidence,
                reification_issue_count=issue_count,
            ) from error
        if code in {
            "NATIVE_EXTENSION_DISABLED",
            "NATIVE_RDFXML_RETAINED_UNSUPPORTED",
            "NATIVE_TURTLE_RETAINED_UNSUPPORTED",
            "NATIVE_OWLXML_RETAINED_UNSUPPORTED",
            "NATIVE_RDF_MAPPING_CARDINALITY",
            "NATIVE_RDF_MAPPING_INCOMPLETE",
            "NATIVE_RDF_MAPPING_TYPE",
            "NATIVE_RDF_MAPPING_UNSUPPORTED",
            "NATIVE_RDF_ONTOLOGY_HEADER",
        }:
            raise UnsupportedSyntaxError(
                message,
                code=code.removeprefix("NATIVE_"),
            ) from error
        if code == "NATIVE_CAPABILITY_UNAVAILABLE":
            raise BackendUnavailableError(message, code=code) from error
        raise BackendProtocolError(message, code=code) from error
    return result


def _call_parse(extension: _Extension, operation: Callable[[], bytes]) -> bytes:
    result = _call_parse_value(extension, operation)
    if not isinstance(result, bytes):
        raise BackendProtocolError(
            "native parser returned a non-bytes result",
            code="NATIVE_RESULT_TYPE",
        )
    return result


def _call_index_value(extension: _Extension, operation: Callable[[], T]) -> T:
    try:
        result = operation()
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        code = _private_error_code(extension, error)
        if code is None:
            raise BackendProtocolError(
                "native index raised an unrecognized exception",
                code="NATIVE_EXCEPTION",
            ) from error
        message = _private_error_message(error)
        if code == "NATIVE_CANCELLED":
            raise OperationCancelledError(message, code=code) from error
        if code in {"NATIVE_DEADLINE", "NATIVE_WIRE_LIMIT"}:
            raise _native_resource_limit_error(extension, error, message, code) from error
        if code == "NATIVE_CAPABILITY_UNAVAILABLE":
            raise BackendUnavailableError(message, code=code) from error
        raise BackendProtocolError(message, code=code) from error
    return result


def _call_index(extension: _Extension, operation: Callable[[], bytes]) -> bytes:
    result = _call_index_value(extension, operation)
    if not isinstance(result, bytes):
        raise BackendProtocolError(
            "native index returned a non-bytes result",
            code="NATIVE_RESULT_TYPE",
        )
    return result


class _ResultReader:
    __slots__ = ("_data", "_framing_code", "_offset")

    def __init__(self, data: bytes, *, framing_code: str = "NATIVE_PARSE_FRAMING") -> None:
        if not isinstance(data, bytes):
            raise BackendProtocolError(
                "native parser result is not bytes",
                code="NATIVE_RESULT_TYPE",
            )
        self._data = data
        self._framing_code = framing_code
        self._offset = 0

    def take(self, size: int) -> bytes:
        end = self._offset + size
        if size < 0 or end < self._offset or end > len(self._data):
            raise BackendProtocolError(
                "native parser result is truncated",
                code=self._framing_code,
            )
        result = self._data[self._offset : end]
        self._offset = end
        return result

    def u8(self) -> int:
        return self.take(1)[0]

    def u64(self) -> int:
        return int.from_bytes(self.take(8), "little")

    def varint(self) -> int:
        value = 0
        shift = 0
        start = self._offset
        while self._offset < len(self._data):
            byte = self.u8()
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                encoded = bytearray()
                remaining = value
                while True:
                    selected = remaining & 0x7F
                    remaining >>= 7
                    encoded.append(selected | (0x80 if remaining else 0))
                    if not remaining:
                        break
                if self._data[start : self._offset] != bytes(encoded):
                    raise BackendProtocolError(
                        "native parser result has a nonminimal integer",
                        code=self._framing_code,
                    )
                return value
            shift += 7
            if shift > 63:
                raise BackendProtocolError(
                    "native parser result integer is too large",
                    code=self._framing_code,
                )
        raise BackendProtocolError(
            "native parser result is truncated",
            code=self._framing_code,
        )

    def frame(self) -> bytes:
        return self.take(self.varint())

    def text(self) -> str:
        try:
            return self.frame().decode("utf-8")
        except UnicodeError as error:
            raise BackendProtocolError(
                "native parser returned invalid UTF-8",
                code=self._framing_code,
            ) from error

    def finish(self) -> None:
        if self._offset != len(self._data):
            raise BackendProtocolError(
                "native parser result contains trailing bytes",
                code=self._framing_code,
            )

    def require_count(self, count: int, minimum_bytes: int) -> None:
        if count > (len(self._data) - self._offset) // minimum_bytes:
            raise BackendProtocolError(
                "native result count exceeds remaining framing",
                code=self._framing_code,
            )


def _decode_parsed_functional(data: bytes, limits: ParseLimits) -> ParsedOntology:
    from pyowl_core.diagnostics import SourceSpan
    from pyowl_core.document import OntologyID
    from pyowl_core.io.formats.common import ParsedOntology
    from pyowl_core.model import IRI, Annotation, StructuralNode, decode_canonical
    from pyowl_core.model.axioms import AxiomNode

    reader = _ResultReader(data)
    magic, schema, format_tag, decoded_codepoints = _PARSE_RESULT_HEADER.unpack(
        reader.take(_PARSE_RESULT_HEADER.size)
    )
    if magic != _PARSE_RESULT_MAGIC or schema != 1 or format_tag != 4:
        raise BackendProtocolError(
            "native parser result has incompatible metadata",
            code="NATIVE_PARSE_VERSION",
        )

    def node() -> StructuralNode:
        payload = reader.frame()
        try:
            return decode_canonical(payload, limits=limits)
        except ResourceLimitError:
            raise
        except Exception as error:
            raise BackendProtocolError(
                "native parser returned invalid canonical model data",
                code="NATIVE_PARSE_MODEL",
            ) from error

    def optional_iri() -> IRI | None:
        marker = reader.u8()
        if marker == 0:
            return None
        if marker != 1:
            raise BackendProtocolError(
                "native parser returned an invalid optional value",
                code="NATIVE_PARSE_FRAMING",
            )
        value = node()
        if not isinstance(value, IRI):
            raise BackendProtocolError(
                "native parser returned a non-IRI ontology identifier",
                code="NATIVE_PARSE_MODEL",
            )
        return value

    ontology_iri = optional_iri()
    version_iri = optional_iri()
    if version_iri is not None and ontology_iri is None:
        raise BackendProtocolError(
            "native parser returned a version IRI without an ontology IRI",
            code="NATIVE_PARSE_MODEL",
        )
    import_count = reader.u64()
    reader.require_count(import_count, 1)
    imports: list[IRI] = []
    for _ in range(import_count):
        value = node()
        if not isinstance(value, IRI):
            raise BackendProtocolError(
                "native parser returned a non-IRI import",
                code="NATIVE_PARSE_MODEL",
            )
        imports.append(value)

    def spanned(
        expected: type[StructuralNode] | tuple[type[StructuralNode], ...],
        maximum_name: str | None,
    ) -> list[tuple[StructuralNode, SourceSpan]]:
        count = reader.u64()
        reader.require_count(count, 33)
        if maximum_name is not None:
            limits.enforce(maximum_name, count)
        values: list[tuple[StructuralNode, SourceSpan]] = []
        for _ in range(count):
            byte_start = reader.u64()
            byte_end = reader.u64()
            line = reader.u64()
            column = reader.u64()
            if byte_end < byte_start or line < 1 or column < 1:
                raise BackendProtocolError(
                    "native parser returned an invalid source span",
                    code="NATIVE_PARSE_FRAMING",
                )
            value = node()
            if not isinstance(value, expected):
                raise BackendProtocolError(
                    "native parser returned a value in the wrong result partition",
                    code="NATIVE_PARSE_MODEL",
                )
            values.append(
                (
                    value,
                    SourceSpan(
                        byte_start=byte_start,
                        byte_end=byte_end,
                        line_start=line,
                        column_start=column,
                    ),
                )
            )
        return values

    from pyowl_core.extensions.swrl import SWRLRule

    annotations = spanned(Annotation, None)
    axioms = spanned(AxiomNode, "max_axioms")
    extensions = spanned(SWRLRule, None)
    prefix_count = reader.u64()
    reader.require_count(prefix_count, 2)
    prefixes: list[tuple[str, str]] = []
    previous_prefix: tuple[str, str] | None = None
    for _ in range(prefix_count):
        prefix = reader.text()
        iri_value = reader.text()
        selected_prefix = (prefix, iri_value)
        if previous_prefix is not None and selected_prefix <= previous_prefix:
            raise BackendProtocolError(
                "native parser prefixes are not canonical",
                code="NATIVE_PARSE_FRAMING",
            )
        previous_prefix = selected_prefix
        prefixes.append(selected_prefix)
    reader.finish()
    occurrences = sorted(
        (*annotations, *axioms, *extensions),
        key=lambda item: (
            -1 if item[1].byte_start is None else item[1].byte_start,
            -1 if item[1].byte_end is None else item[1].byte_end,
        ),
    )
    return ParsedOntology(
        OntologyID(ontology_iri, version_iri),
        tuple(imports),
        tuple(cast(Annotation, value) for value, _span in annotations),
        tuple(cast(AxiomNode, value) for value, _span in axioms),
        tuple(value for value, _span in extensions),
        tuple(prefixes),
        tuple(occurrences),
        decoded_codepoint_length=decoded_codepoints,
    )


def _decode_axiom_partition(
    data: bytes,
    axioms: tuple[AxiomNode, ...],
    limits: ParseLimits,
) -> dict[type[AxiomNode], tuple[AxiomNode, ...]]:
    from pyowl_core.model.axioms import AxiomNode
    from pyowl_core.model.registry import SPEC_BY_TAG

    reader = _ResultReader(data, framing_code="NATIVE_INDEX_FRAMING")
    magic, schema, reserved, group_count = _INDEX_RESULT_HEADER.unpack(
        reader.take(_INDEX_RESULT_HEADER.size)
    )
    if magic != _INDEX_RESULT_MAGIC or schema != 1 or reserved != 0:
        raise BackendProtocolError(
            "native index result has incompatible metadata",
            code="NATIVE_INDEX_VERSION",
        )
    limits.enforce("max_index_rows", len(axioms))
    if group_count > len(axioms):
        raise BackendProtocolError(
            "native index result has too many groups",
            code="NATIVE_INDEX_FRAMING",
        )
    postings: dict[type[AxiomNode], tuple[AxiomNode, ...]] = {}
    seen: set[int] = set()
    previous_tag = -1
    for _ in range(group_count):
        tag = reader.u64()
        count = reader.u64()
        if tag <= previous_tag or count < 1:
            raise BackendProtocolError(
                "native index groups are not canonical",
                code="NATIVE_INDEX_FRAMING",
            )
        previous_tag = tag
        if count > len(axioms) - len(seen):
            raise BackendProtocolError(
                "native index posting exceeds remaining rows",
                code="NATIVE_INDEX_FRAMING",
            )
        reader.require_count(count, 8)
        spec = SPEC_BY_TAG.get(tag)
        if spec is None or not issubclass(spec.constructor, AxiomNode):
            raise BackendProtocolError(
                "native index result contains a non-axiom tag",
                code="NATIVE_INDEX_MODEL",
            )
        selected: list[AxiomNode] = []
        previous_ordinal = -1
        for _ in range(count):
            ordinal = reader.u64()
            if ordinal <= previous_ordinal or ordinal >= len(axioms) or ordinal in seen:
                raise BackendProtocolError(
                    "native index result contains an invalid row ordinal",
                    code="NATIVE_INDEX_FRAMING",
                )
            value = axioms[ordinal]
            if type(value) is not spec.constructor:
                raise BackendProtocolError(
                    "native index tag disagrees with the Python model",
                    code="NATIVE_INDEX_MODEL",
                )
            previous_ordinal = ordinal
            seen.add(ordinal)
            selected.append(value)
        postings[spec.constructor] = tuple(selected)
    reader.finish()
    if len(seen) != len(axioms):
        raise BackendProtocolError(
            "native index result omitted rows",
            code="NATIVE_INDEX_FRAMING",
        )
    return postings


def _private_error_code(extension: _Extension, error: Exception) -> str | None:
    if not isinstance(error, extension._NativeError) or len(error.args) not in {2, 3}:
        return None
    if len(error.args) == 3 and type(error.args[2]) is not dict:
        return None
    code = error.args[0]
    return code if isinstance(code, str) and _CODE.fullmatch(code) else None


def _private_error_message(error: Exception) -> str:
    message = error.args[1] if len(error.args) in {2, 3} else None
    if not isinstance(message, str) or not message or len(message) > 200:
        return "native backend reported an invalid error payload"
    return "".join(character if character.isprintable() else "?" for character in message)


def _private_reification_diagnostics(
    extension: _Extension,
    error: Exception,
    message: str,
) -> tuple[tuple[Diagnostic, ...], int, Diagnostic] | None:
    """Decode bounded structural evidence from native reification failures."""

    if not isinstance(error, extension._NativeError) or len(error.args) != 3:
        return None
    raw = error.args[2]
    if type(raw) is not dict or set(raw) != {"kind", "data"}:
        return None
    if raw["kind"] != "rdf_reification_v2" or type(raw["data"]) is not bytes:
        return None
    data = raw["data"]
    if (
        not data.startswith(_RDF_REIFICATION_ERROR_MAGIC)
        or len(data) > _MAX_RDF_REIFICATION_PAYLOAD_BYTES
    ):
        return None
    offset = len(_RDF_REIFICATION_ERROR_MAGIC)
    if offset + 12 > len(data):
        return None
    issue_count = int.from_bytes(data[offset : offset + 8], "little")
    retained_count = int.from_bytes(data[offset + 8 : offset + 12], "little")
    offset += 12
    if issue_count < 1 or retained_count > issue_count:
        return None
    evidence_count = retained_count
    suppressed_count = issue_count - evidence_count
    diagnostics: list[Diagnostic] = []

    for _ in range(retained_count):
        if offset >= len(data) or data[offset] not in {0, 1, 2}:
            return None
        presence = data[offset]
        offset += 1

        fields: list[tuple[int, str]] = []
        for _ in range(5):
            if offset + 5 > len(data):
                return None
            kind = data[offset]
            length = int.from_bytes(data[offset + 1 : offset + 5], "little")
            offset += 5
            if length > _MAX_RDF_EVIDENCE_BYTES or offset + length > len(data):
                return None
            try:
                text = data[offset : offset + length].decode("utf-8")
            except UnicodeDecodeError:
                return None
            offset += length
            fields.append((kind, text))
        diagnostic = _private_reification_record_diagnostic(
            fields,
            presence=presence,
            message=message,
            issue_count=issue_count,
            evidence_count=evidence_count,
            suppressed_count=suppressed_count,
        )
        if diagnostic is None:
            return None
        diagnostics.append(diagnostic)
    if offset != len(data):
        return None
    evidence = tuple(diagnostics)
    primary = (
        evidence[0]
        if evidence
        else Diagnostic(
            code="RDF_AXIOM_REIFICATION",
            severity=Severity.ERROR,
            message=message,
            details={
                "reification_issue_count": issue_count,
                "reification_evidence_count": 0,
                "reification_suppressed_count": issue_count,
            },
        )
    )
    return evidence, issue_count, primary


def _private_reification_record_diagnostic(
    fields: list[tuple[int, str]],
    *,
    presence: int,
    message: str,
    issue_count: int,
    evidence_count: int,
    suppressed_count: int,
) -> Diagnostic | None:
    reason_kind, reason = fields[0]
    if reason_kind != 4 or reason not in {
        "ANNOTATION_CYCLE",
        "MAIN_TRIPLE_ABSENT",
        "METADATA_AMBIGUOUS",
        "METADATA_INCOMPLETE",
        "METADATA_TYPE_INVALID",
        "NODE_KIND_CONFLICT",
        "UNCLAIMED_MAIN_TRIPLE",
    }:
        return None
    node = _decode_reification_resource(fields[1])
    source = _decode_reification_resource(fields[2])
    property_value = _decode_reification_text(fields[3])
    target = _decode_reification_term(fields[4])
    if any(value is _INVALID_RDF_ERROR_FIELD for value in (node, source, property_value, target)):
        return None

    details: dict[str, str | int | bool] = {
        "reification_error": reason,
        "reification_issue_count": issue_count,
        "reification_evidence_count": evidence_count,
        "reification_suppressed_count": suppressed_count,
    }
    if isinstance(node, tuple):
        details["reification_subject"] = node[0]
    if isinstance(source, tuple):
        details["annotated_source"] = source[0]
    if isinstance(property_value, str):
        details["annotated_property"] = property_value
    if isinstance(target, tuple):
        details["annotated_target"] = target[0]
        details["annotated_target_kind"] = target[1]
    if presence:
        details["main_triple_present"] = presence == 2

    return Diagnostic(
        code="RDF_AXIOM_REIFICATION",
        severity=Severity.ERROR,
        message=message,
        details=details,
    )


_INVALID_RDF_ERROR_FIELD = object()


def _decode_reification_resource(value: tuple[int, str]) -> object:
    kind, text = value
    if kind == 0 and not text:
        return None
    if kind == 1 and text:
        rendered = f"<{text}>"
        return (_bounded_rdf_error_text(rendered), "iri")
    if kind == 2 and text:
        return (_bounded_rdf_error_text("_:" + text), "blank")
    return _INVALID_RDF_ERROR_FIELD


def _decode_reification_term(value: tuple[int, str]) -> object:
    kind, text = value
    if kind in {0, 1, 2}:
        return _decode_reification_resource(value)
    if kind == 3:
        return (_bounded_rdf_error_text(repr(text)), "literal")
    return _INVALID_RDF_ERROR_FIELD


def _decode_reification_text(value: tuple[int, str]) -> object:
    kind, text = value
    if kind == 0 and not text:
        return None
    if kind == 4 and text:
        return _bounded_rdf_error_text(text)
    return _INVALID_RDF_ERROR_FIELD


def _bounded_rdf_error_text(value: str) -> str:
    return bounded_evidence_text(value, max_bytes=_MAX_RDF_EVIDENCE_BYTES)


def _private_resource_limit_payload(
    extension: _Extension,
    error: Exception,
) -> tuple[str, int | float, int | float, Mapping[str, str | int | bool]] | None:
    """Validate the private typed limit frame without consulting its message."""

    if not isinstance(error, extension._NativeError) or len(error.args) != 3:
        return None
    raw = error.args[2]
    if type(raw) is not dict or set(raw) != {
        "kind",
        "limit",
        "observed",
        "allowed",
        "details",
    }:
        return None
    if raw["kind"] != "resource_limit":
        return None
    limit = raw["limit"]
    observed = raw["observed"]
    allowed = raw["allowed"]
    details = raw["details"]
    if not isinstance(limit, str) or _LIMIT_NAME.fullmatch(limit) is None:
        return None
    for value in (observed, allowed):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if value < 0 or not math.isfinite(value):
            return None
    if type(details) is not dict or len(details) > 16:
        return None
    clean_details: dict[str, str | int | bool] = {}
    for key, value in details.items():
        if not isinstance(key, str) or _LIMIT_DETAIL_KEY.fullmatch(key) is None:
            return None
        if not isinstance(value, (str, int, bool)):
            return None
        if isinstance(value, str) and (len(value) > 200 or not value.isprintable()):
            return None
        if isinstance(value, int) and not isinstance(value, bool) and value < 0:
            return None
        clean_details[key] = value
    work_term = clean_details.get("work_term")
    if work_term is not None and work_term not in {
        "setup",
        "refinement",
        "candidate_orders",
    }:
        return None
    return limit, observed, allowed, clean_details


def _native_resource_limit_error(
    extension: _Extension,
    error: Exception,
    message: str,
    code: str,
) -> ResourceLimitError:
    payload = _private_resource_limit_payload(extension, error)
    if payload is None:
        raise BackendProtocolError(
            "native configured-limit failure omitted its typed payload",
            code="NATIVE_LIMIT_PAYLOAD",
        ) from error
    limit, observed, allowed, details = payload
    return ResourceLimitError(
        message,
        limit=limit,
        observed=observed,
        allowed=allowed,
        details=details,
        code=code,
    )


def _decode_receipt(data: bytes) -> NativeValidation:
    if not isinstance(data, bytes) or len(data) != _RECEIPT.size:
        raise BackendProtocolError(
            "native validation receipt has invalid framing",
            code="NATIVE_RECEIPT_FRAMING",
        )
    (
        magic,
        abi,
        model,
        major,
        minor,
        flags,
        length,
        digest,
        sections,
        rows,
    ) = _RECEIPT.unpack(data)
    if (
        magic != _RECEIPT_MAGIC
        or abi != _ABI_VERSION
        or model != _MODEL_SCHEMA_VERSION
        or major != 1
    ):
        raise BackendProtocolError(
            "native validation receipt has incompatible metadata",
            code="NATIVE_RECEIPT_VERSION",
        )
    return NativeValidation(minor, flags, length, digest, sections, rows)


def _reset_probe_cache_for_tests() -> None:
    global _cached_runtime
    with _probe_lock:
        _cached_runtime = None


__all__ = [
    "NativeAxiomPartition",
    "NativeProbe",
    "NativeValidation",
    "decode_snapshot",
    "encode_snapshot",
    "parse_functional",
    "partition_axioms",
    "probe",
    "require",
    "roundtrip_wire",
    "validate_canonical",
    "validate_wire",
]
