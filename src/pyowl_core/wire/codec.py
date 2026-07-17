"""Canonical PYOCORE wire-v1 semantic encoder and decoder."""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import BinaryIO, cast

from pyowl_core.cancellation import CancellationToken
from pyowl_core.config import BackendPreference, DocumentFormat, ImportPolicy, LoadOptions
from pyowl_core.diagnostics import Diagnostic, Severity, SourceSpan
from pyowl_core.document.document import Fingerprint, OntologyDocument, OntologyID
from pyowl_core.document.fingerprint import StructuralContext, StructuralContextKind
from pyowl_core.document.imports import (
    DocumentRecord,
    DocumentStatus,
    ImportEdge,
    ImportManifest,
    ImportStatus,
)
from pyowl_core.document.overlay import view_limits
from pyowl_core.document.provenance import (
    DetectionBasis,
    DigestKind,
    DocumentProvenance,
    OriginIndex,
    OriginOccurrence,
)
from pyowl_core.document.snapshot import (
    AxiomScope,
    OntologySnapshot,
    OntologyView,
    _is_ontology_view,
    materialize_view,
)
from pyowl_core.exceptions import (
    ModelError,
    OperationCancelledError,
    ResourceLimitError,
    WireCorruptionError,
    WireError,
    WireLimitError,
    WireVersionError,
)
from pyowl_core.limits import ParseLimits
from pyowl_core.model import (
    IRI,
    Annotation,
    AnonymousIndividual,
    CanonicalSet,
    Entity,
    Literal,
    StructuralNode,
    canonical_bytes,
    constructor_spec,
    decode_canonical,
    walk,
)
from pyowl_core.model.axioms import AxiomNode
from pyowl_core.model.registry import SPEC_BY_TAG, ConstructorSpec

from ._binary import (
    ByteReader,
    ByteWriter,
    DirectoryEntry,
    Guard,
    TableView,
    WireImage,
    crc32c,
    encode_table,
    validate_wire,
)
from .schema import (
    ALIGNMENT,
    CANONICAL_PROFILE,
    DIRECTORY_ENTRY_SIZE,
    DIRECTORY_STRUCT,
    FEATURE_SWRL,
    HEADER_SIZE,
    HEADER_STRUCT,
    MAGIC,
    MAX_TABLE_ID,
    MODEL_SCHEMA,
    REQUIRED_SECTIONS,
    SECTION_OPTIONAL,
    SECTION_REQUIRED,
    SECTION_SCHEMAS,
    WIRE_MAJOR,
    WIRE_MINOR,
    SectionKind,
)

_NONE_U64 = 0xFFFF_FFFF_FFFF_FFFF

_FORMATS = {
    DocumentFormat.RDF_XML: 1,
    DocumentFormat.TURTLE: 2,
    DocumentFormat.OWL_XML: 3,
    DocumentFormat.FUNCTIONAL: 4,
}
_FORMAT_BY_TAG = {value: key for key, value in _FORMATS.items()}
_POLICIES = {
    ImportPolicy.IGNORE: 1,
    ImportPolicy.RECORD_UNRESOLVED: 2,
    ImportPolicy.RESOLVE_LOCAL: 3,
    ImportPolicy.RESOLVE_STRICT: 4,
}
_POLICY_BY_TAG = {value: key for key, value in _POLICIES.items()}
_DOCUMENT_STATUSES = {DocumentStatus.ROOT: 1, DocumentStatus.RESOLVED: 2}
_DOCUMENT_STATUS_BY_TAG = {value: key for key, value in _DOCUMENT_STATUSES.items()}
_IMPORT_STATUSES = {
    ImportStatus.RESOLVED: 1,
    ImportStatus.UNRESOLVED: 2,
    ImportStatus.IGNORED: 3,
    ImportStatus.DENIED: 4,
    ImportStatus.FAILED: 5,
}
_IMPORT_STATUS_BY_TAG = {value: key for key, value in _IMPORT_STATUSES.items()}
_DIGEST_KINDS = {DigestKind.EXACT_BYTES: 1, DigestKind.NORMALIZED_TEXT: 2}
_DIGEST_KIND_BY_TAG = {value: key for key, value in _DIGEST_KINDS.items()}
_DETECTION_BASES = {
    DetectionBasis.EXPLICIT: 1,
    DetectionBasis.MEDIA_TYPE: 2,
    DetectionBasis.CONTENT: 3,
    DetectionBasis.EXTENSION: 4,
}
_DETECTION_BASIS_BY_TAG = {value: key for key, value in _DETECTION_BASES.items()}


@dataclass(frozen=True, slots=True)
class ViewSummary:
    root_document_key: str
    complete: bool
    structural_context: StructuralContext | None
    structural_fingerprint: Fingerprint
    logical_fingerprint: Fingerprint
    signature_fingerprint: Fingerprint
    document_count: int
    total_source_bytes: int
    effective_axiom_count: int


@dataclass(frozen=True, slots=True)
class InspectedWire:
    image: WireImage
    summary: ViewSummary


@dataclass(frozen=True, slots=True)
class _DocumentMeta:
    key: str
    document_ontology_iri_id: int
    document_version_iri_id: int
    document_document_iri_id: int
    record_ontology_iri_id: int
    record_version_iri_id: int
    record_document_iri_id: int
    record_source_digest: bytes
    document_fingerprint: Fingerprint
    record_format: DocumentFormat
    status: DocumentStatus


@dataclass(frozen=True, slots=True)
class _DocumentWire:
    meta: _DocumentMeta
    provenance_source_digest: bytes
    digest_kind: DigestKind
    byte_length: int
    codepoint_length: int
    provenance_document_iri_id: int
    provenance_format: DocumentFormat
    detection_basis: DetectionBasis
    expected_digest: bytes | None
    parser: str
    backend: str
    api_version: tuple[int, int]
    model_schema: int
    direct_imports: tuple[int, ...]
    raw_annotations: tuple[int, ...]
    raw_axioms: tuple[int, ...]
    raw_extensions: tuple[int, ...]
    effective_annotations: tuple[int, ...]
    effective_axioms: tuple[int, ...]
    effective_extensions: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _DecodedDocument:
    meta: _DocumentMeta
    document: OntologyDocument
    effective_annotations: tuple[int, ...]
    effective_axioms: tuple[int, ...]
    effective_extensions: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _ImportsWire:
    policy: ImportPolicy
    offline: bool
    resolver_digest: bytes
    edges: tuple[tuple[str, int, ImportStatus, str | None, str | None, str | None], ...]


@dataclass(frozen=True, slots=True)
class _ViewRefs:
    annotations: tuple[int, ...]
    axioms: tuple[int, ...]
    extensions: tuple[int, ...]


def encode_snapshot(
    snapshot: OntologyView,
    *,
    limits: ParseLimits | None = None,
    cancellation_token: CancellationToken | None = None,
) -> bytes:
    """Encode one view as deterministic, self-contained PYOCORE v1 bytes."""

    if not _is_ontology_view(snapshot):
        raise TypeError("snapshot must implement OntologyView")
    selected_limits = view_limits(snapshot) if limits is None else limits
    if not isinstance(selected_limits, ParseLimits):
        raise TypeError("limits must be ParseLimits or None")
    guard = Guard(selected_limits, cancellation_token)
    guard.check(force=True)
    concrete = _materialize_for_wire(snapshot, selected_limits)
    rows, flags = _collect_sections(concrete, selected_limits, guard)
    sections: dict[SectionKind, bytes] = {
        kind: encode_table(tuple(sorted(set(values)))) for kind, values in rows.items()
    }
    digests = {kind: hashlib.sha256(data).digest() for kind, data in sections.items()}
    footer = _footer_row(concrete, sections, digests)
    sections[SectionKind.FOOTER] = encode_table((footer,))
    result = _assemble(sections, flags, selected_limits, guard)
    guard.check(force=True)
    return result


def decode_snapshot(
    data: bytes | bytearray | memoryview | BinaryIO,
    *,
    limits: ParseLimits | None = None,
    verify: bool = True,
    cancellation_token: CancellationToken | None = None,
) -> OntologySnapshot:
    """Validate and materialize one immutable snapshot from PYOCORE bytes."""

    selected_limits = ParseLimits() if limits is None else limits
    if not isinstance(selected_limits, ParseLimits):
        raise TypeError("limits must be ParseLimits or None")
    if not isinstance(verify, bool):
        raise TypeError("verify must be bool")
    owned = _read_wire_source(data, selected_limits, cancellation_token)
    inspected = validate_bytes(
        owned,
        limits=selected_limits,
        verify=verify,
        cancellation_token=cancellation_token,
    )
    return checked_materialize_image(
        inspected,
        limits=selected_limits,
        cancellation_token=cancellation_token,
    )


def inspect_image(
    image: WireImage,
    *,
    limits: ParseLimits,
    cancellation_token: CancellationToken | None = None,
    lazy_model_validation: bool = False,
) -> InspectedWire:
    """Validate semantic rows while retaining only bounded metadata."""

    guard = Guard(limits, cancellation_token)
    _validate_feature_sections(image)
    strings = image.table(SectionKind.STRINGS)
    for index, row in enumerate(strings.rows()):
        guard.check(index)
        _decode_utf8(row, "STRINGS")
    _validate_model_table(
        image.table(SectionKind.IRIS), IRI, limits, guard, lazy=lazy_model_validation
    )
    _validate_model_table(
        image.table(SectionKind.ENTITIES), Entity, limits, guard, lazy=lazy_model_validation
    )
    _validate_model_table(
        image.table(SectionKind.LITERALS), Literal, limits, guard, lazy=lazy_model_validation
    )
    _validate_model_table(
        image.table(SectionKind.ANONYMOUS),
        AnonymousIndividual,
        limits,
        guard,
        lazy=lazy_model_validation,
    )
    _validate_sequences(image.table(SectionKind.SEQUENCES), limits, guard)
    _validate_model_table(
        image.table(SectionKind.ANNOTATIONS),
        Annotation,
        limits,
        guard,
        lazy=lazy_model_validation,
    )
    _validate_terms(
        image.table(SectionKind.TERMS), limits, guard, lazy=lazy_model_validation
    )
    _validate_model_table(
        image.table(SectionKind.AXIOMS), AxiomNode, limits, guard, lazy=lazy_model_validation
    )
    swrl = image.tables.get(int(SectionKind.SWRL))
    if swrl is not None:
        _validate_structural_table(swrl, limits, guard, lazy=lazy_model_validation)
    documents = image.table(SectionKind.DOCUMENTS)
    document_wires = tuple(
        _read_document_row(image, row, limits, collect=False) for row in documents.rows()
    )
    document_meta = tuple(item.meta for item in document_wires)
    keys = {item.key for item in document_meta}
    if len(keys) != len(document_meta):
        raise _corrupt("duplicate document key in DOCUMENTS")
    if sum(item.status is DocumentStatus.ROOT for item in document_meta) != 1:
        raise _corrupt("DOCUMENTS must contain exactly one root record")
    _validate_imports(image, keys, limits, collect=False)
    summary = replace(
        _read_view(image, keys, limits, collect=False)[0],
        total_source_bytes=sum(item.byte_length for item in document_wires),
    )
    if len(document_meta) != summary.document_count:
        raise _corrupt("VIEW document count disagrees with DOCUMENTS")
    if summary.root_document_key not in keys:
        raise _corrupt("VIEW root document is absent from DOCUMENTS")
    _validate_origins(image, keys, limits, collect=False)
    _validate_footer(image, summary)
    guard.check(force=True)
    return InspectedWire(image, summary)


def materialize_image(
    inspected: InspectedWire,
    *,
    limits: ParseLimits,
    cancellation_token: CancellationToken | None = None,
) -> OntologySnapshot:
    """Construct Python model objects only after complete wire validation."""

    image = inspected.image
    guard = Guard(limits, cancellation_token)
    cache: dict[tuple[SectionKind, int], StructuralNode] = {}
    document_table = image.table(SectionKind.DOCUMENTS)
    decoded = tuple(
        _decode_document_row(image, row, limits, cache) for row in document_table.rows()
    )
    by_key = {item.meta.key: item for item in decoded}
    manifest = _decode_imports(image, tuple(item.meta for item in decoded), limits, cache)
    documents = tuple(by_key[record.document_key].document for record in manifest.documents)
    summary, _view_refs = _read_view(image, set(by_key), limits, collect=True)
    origins = cast(OriginIndex, _validate_origins(image, set(by_key), limits, collect=True))
    options = LoadOptions(
        imports=manifest.policy,
        backend=BackendPreference.PYTHON,
        limits=limits,
        offline=manifest.offline,
        preserve_source_map=False,
        collect_provenance=True,
        validate_owl2_dl=False,
        deterministic=True,
    )
    root = by_key[summary.root_document_key].document
    try:
        if summary.structural_context is None:
            snapshot = OntologySnapshot(
                root,
                documents,
                manifest,
                summary.root_document_key,
                options,
                _origin_index_override=origins,
                _complete_override=summary.complete,
            )
        else:
            snapshot = OntologySnapshot(
                root,
                documents,
                manifest,
                summary.root_document_key,
                options,
                _preserve_document_scopes=True,
                _origin_index_override=origins,
                _structural_context=summary.structural_context,
                _structural_fingerprint_override=summary.structural_fingerprint,
                _complete_override=summary.complete,
            )
    except (TypeError, ValueError, ModelError, ResourceLimitError) as error:
        raise _translate_model_error(error) from error
    _compare_decoded_view(image, snapshot, decoded, summary, _view_refs)
    guard.check(force=True)
    return snapshot


def validate_bytes(
    data: bytes | bytearray | memoryview,
    *,
    limits: ParseLimits,
    verify: bool,
    cancellation_token: CancellationToken | None = None,
    lazy_model_validation: bool = False,
) -> InspectedWire:
    try:
        image = validate_wire(
            data,
            limits=limits,
            verify=verify,
            cancellation_token=cancellation_token,
        )
        return inspect_image(
            image,
            limits=limits,
            cancellation_token=cancellation_token,
            lazy_model_validation=lazy_model_validation,
        )
    except (WireError, OperationCancelledError):
        raise
    except ResourceLimitError as error:
        raise WireLimitError(
            "wire validation exceeds resource limits", code="WIRE_MODEL_LIMIT"
        ) from error
    except (TypeError, ValueError, KeyError, OverflowError) as error:
        raise _corrupt("wire semantic metadata is invalid") from error


def checked_materialize_image(
    inspected: InspectedWire,
    *,
    limits: ParseLimits,
    cancellation_token: CancellationToken | None = None,
) -> OntologySnapshot:
    """Translate hostile semantic construction failures to the wire taxonomy."""

    try:
        return materialize_image(
            inspected,
            limits=limits,
            cancellation_token=cancellation_token,
        )
    except (WireError, OperationCancelledError):
        raise
    except ResourceLimitError as error:
        raise WireLimitError(
            "wire materialization exceeds resource limits", code="WIRE_MODEL_LIMIT"
        ) from error
    except (TypeError, ValueError, KeyError, OverflowError) as error:
        raise _corrupt("wire rows do not form a valid ontology snapshot") from error


def image_import_options(image: WireImage) -> tuple[ImportPolicy, bool]:
    """Read already-validated manifest policy flags without model allocation."""

    table = image.table(SectionKind.IMPORTS)
    reader = ByteReader(table.row(0), section="IMPORTS")
    policy = cast(ImportPolicy, _enum_tag(reader.u8(), _POLICY_BY_TAG, "import policy"))
    return policy, reader.boolean()


def _materialize_for_wire(view: OntologyView, limits: ParseLimits) -> OntologySnapshot:
    if isinstance(view, OntologySnapshot):
        check = getattr(view, "_check_open", None)
        if callable(check):
            check()
        return view
    materialize = getattr(view, "materialize", None)
    if callable(materialize):
        result = materialize()
        if not isinstance(result, OntologySnapshot):
            raise TypeError("OntologyView.materialize() did not return OntologySnapshot")
        return result
    annotations = view.ontology_annotations()
    axioms = CanonicalSet(view.iter_axioms())
    extensions = CanonicalSet(view.iter_extensions())
    context = StructuralContext.overlay(view.structural_fingerprint)
    return materialize_view(
        view,
        annotations=annotations,
        axioms=axioms,
        extensions=extensions,
        origin_index=view.origin_index,
        structural_context=context,
        structural_fingerprint_override=view.structural_fingerprint,
        limits=limits,
        elapsed_seconds=0.0,
    )


def _collect_sections(
    snapshot: OntologySnapshot,
    limits: ParseLimits,
    guard: Guard,
) -> tuple[dict[SectionKind, list[bytes]], int]:
    rows: dict[SectionKind, list[bytes]] = {kind: [] for kind in REQUIRED_SECTIONS[:-1]}
    strings: set[bytes] = set()
    model_rows: dict[SectionKind, set[bytes]] = {
        SectionKind.IRIS: set(),
        SectionKind.ENTITIES: set(),
        SectionKind.LITERALS: set(),
        SectionKind.ANONYMOUS: set(),
        SectionKind.ANNOTATIONS: set(),
        SectionKind.TERMS: set(),
        SectionKind.AXIOMS: set(),
        SectionKind.SWRL: set(),
    }
    sequence_rows: set[bytes] = set()
    extension_roots: set[bytes] = set()
    for value in _extension_roots(snapshot):
        extension_roots.add(canonical_bytes(value, limits=limits))
    work = 0
    temporary = 0
    for root in _structural_roots(snapshot):
        for node in walk(root):
            work += 1
            guard.check(work)
            encoded = canonical_bytes(node, limits=limits)
            kind = _model_section(node, encoded in extension_roots)
            if encoded not in model_rows[kind]:
                model_rows[kind].add(encoded)
                temporary += len(encoded)
            _collect_scalar_strings(node, strings)
            for descriptor in _sequence_descriptors(node):
                sequence_rows.add(descriptor)
                temporary += len(descriptor)
            if temporary > limits.max_temporary_bytes:
                raise ResourceLimitError(
                    "wire encoder exceeds max_temporary_bytes",
                    limit="max_temporary_bytes",
                    observed=temporary,
                    allowed=limits.max_temporary_bytes,
                )
    for record, document in snapshot.iter_documents():
        strings.add(record.document_key.encode("utf-8"))
        strings.add(document.provenance.parser.encode("utf-8"))
        strings.add(document.provenance.backend.encode("utf-8"))
    for edge in snapshot.import_manifest.edges:
        strings.add(edge.importing_document_key.encode("utf-8"))
        if edge.resolved_document_key is not None:
            strings.add(edge.resolved_document_key.encode("utf-8"))
        if edge.resolver_name is not None:
            strings.add(edge.resolver_name.encode("utf-8"))
        if edge.diagnostic is not None:
            strings.add(edge.diagnostic.code.encode("ascii"))
    for occurrences in snapshot.origin_index.entries.values():
        for occurrence in occurrences:
            strings.add(occurrence.document_key.encode("utf-8"))
    strings.add(snapshot.root_document_key.encode("utf-8"))
    rows[SectionKind.STRINGS] = sorted(strings)
    rows[SectionKind.SEQUENCES] = sorted(sequence_rows)
    for kind, values in model_rows.items():
        if kind is not SectionKind.SWRL:
            rows[kind] = sorted(values)
    _enforce_id_spaces(rows, model_rows[SectionKind.SWRL])
    ids = _build_ids(rows, model_rows[SectionKind.SWRL])
    document_rows = [
        _document_row(snapshot, record, document, ids)
        for record, document in snapshot.iter_documents()
    ]
    rows[SectionKind.DOCUMENTS] = sorted(document_rows)
    rows[SectionKind.IMPORTS] = [_imports_row(snapshot.import_manifest, ids)]
    rows[SectionKind.VIEW] = [_view_row(snapshot, ids)]
    rows[SectionKind.ORIGINS] = _origin_rows(snapshot.origin_index, ids)
    flags = 0
    if model_rows[SectionKind.SWRL]:
        rows[SectionKind.SWRL] = sorted(model_rows[SectionKind.SWRL])
        flags |= FEATURE_SWRL
    return rows, flags


def _structural_roots(snapshot: OntologySnapshot) -> Iterator[StructuralNode]:
    for record, document in snapshot.iter_documents():
        yield from (value for value in document.direct_imports)
        for value in (
            document.ontology_id.ontology_iri,
            document.ontology_id.version_iri,
            document.document_iri,
            document.provenance.document_iri,
            record.ontology_id.ontology_iri,
            record.ontology_id.version_iri,
            record.document_iri,
        ):
            if value is not None:
                yield value
        yield from document.ontology_annotations
        yield from document.axioms
        yield from document.extension_components
        yield from snapshot.ontology_annotations(
            scope=AxiomScope.DOCUMENT, document_key=record.document_key
        )
        yield from snapshot.iter_axioms(
            scope=AxiomScope.DOCUMENT, document_key=record.document_key
        )
        yield from snapshot.iter_extensions(
            scope=AxiomScope.DOCUMENT, document_key=record.document_key
        )
    for edge in snapshot.import_manifest.edges:
        yield edge.import_iri


def _extension_roots(snapshot: OntologySnapshot) -> Iterator[StructuralNode]:
    for record, document in snapshot.iter_documents():
        yield from document.extension_components
        yield from snapshot.iter_extensions(
            scope=AxiomScope.DOCUMENT, document_key=record.document_key
        )


def _model_section(node: StructuralNode, extension_root: bool) -> SectionKind:
    if isinstance(node, IRI):
        return SectionKind.IRIS
    if isinstance(node, Entity):
        return SectionKind.ENTITIES
    if isinstance(node, Literal):
        return SectionKind.LITERALS
    if isinstance(node, AnonymousIndividual):
        return SectionKind.ANONYMOUS
    if isinstance(node, Annotation):
        return SectionKind.ANNOTATIONS
    if isinstance(node, AxiomNode):
        return SectionKind.AXIOMS
    if extension_root or node.__class__.__module__.endswith(".swrl"):
        return SectionKind.SWRL
    return SectionKind.TERMS


def _collect_scalar_strings(node: StructuralNode, target: set[bytes]) -> None:
    for name in constructor_spec(node).fields:
        value = getattr(node, name)
        if isinstance(value, str):
            target.add(value.encode("utf-8"))


def _sequence_descriptors(node: StructuralNode) -> Iterator[bytes]:
    for name in constructor_spec(node).fields:
        value = getattr(node, name)
        if not isinstance(value, (tuple, CanonicalSet)):
            continue
        writer = ByteWriter()
        writer.u8(1 if isinstance(value, tuple) else 2)
        writer.u64(len(value))
        for item in value:
            if isinstance(item, StructuralNode):
                writer.raw(hashlib.sha256(canonical_bytes(item)).digest())
            else:
                writer.raw(hashlib.sha256(repr(item).encode("utf-8")).digest())
        yield writer.finish()


def _enforce_id_spaces(
    rows: Mapping[SectionKind, Sequence[bytes]], swrl: set[bytes]
) -> None:
    for kind, values in (*rows.items(), (SectionKind.SWRL, swrl)):
        if len(values) > MAX_TABLE_ID:
            raise ResourceLimitError(
                f"wire {kind.name} table exceeds the u32 ID space",
                limit="wire_id_space",
                observed=len(values),
                allowed=MAX_TABLE_ID,
                code="WIRE_ID_SPACE",
            )


def _build_ids(
    rows: Mapping[SectionKind, Sequence[bytes]], swrl: set[bytes]
) -> dict[SectionKind, dict[bytes, int]]:
    result = {
        kind: {value: index for index, value in enumerate(values, start=1)}
        for kind, values in rows.items()
    }
    result[SectionKind.SWRL] = {
        value: index for index, value in enumerate(sorted(swrl), start=1)
    }
    return result


def _document_row(
    snapshot: OntologySnapshot,
    record: DocumentRecord,
    document: OntologyDocument,
    ids: Mapping[SectionKind, Mapping[bytes, int]],
) -> bytes:
    writer = ByteWriter()
    writer.u32(_string_id(record.document_key, ids))
    writer.u32(_node_id(document.ontology_id.ontology_iri, SectionKind.IRIS, ids))
    writer.u32(_node_id(document.ontology_id.version_iri, SectionKind.IRIS, ids))
    writer.u32(_node_id(document.document_iri, SectionKind.IRIS, ids))
    writer.u32(_node_id(record.ontology_id.ontology_iri, SectionKind.IRIS, ids))
    writer.u32(_node_id(record.ontology_id.version_iri, SectionKind.IRIS, ids))
    writer.u32(_node_id(record.document_iri, SectionKind.IRIS, ids))
    writer.raw(record.source_sha256)
    _write_fingerprint(writer, record.document_fingerprint)
    writer.u8(_FORMATS[record.format])
    writer.u8(_DOCUMENT_STATUSES[record.status])
    provenance = document.provenance
    writer.raw(provenance.source_sha256)
    writer.u8(_DIGEST_KINDS[provenance.digest_kind])
    writer.u64(provenance.byte_length)
    writer.u64(provenance.decoded_codepoint_length)
    writer.u32(_node_id(provenance.document_iri, SectionKind.IRIS, ids))
    writer.u8(_FORMATS[provenance.format])
    writer.u8(_DETECTION_BASES[provenance.detection_basis])
    writer.u8(1 if provenance.expected_sha256 is not None else 0)
    if provenance.expected_sha256 is not None:
        writer.raw(provenance.expected_sha256)
    writer.u32(_string_id(provenance.parser, ids))
    writer.u32(_string_id(provenance.backend, ids))
    writer.u16(provenance.api_version[0])
    writer.u16(provenance.api_version[1])
    writer.u32(provenance.model_schema)
    _write_references(writer, document.direct_imports, SectionKind.IRIS, ids)
    _write_references(writer, document.ontology_annotations, SectionKind.ANNOTATIONS, ids)
    _write_references(writer, document.axioms, SectionKind.AXIOMS, ids)
    _write_references(writer, document.extension_components, SectionKind.SWRL, ids)
    _write_references(
        writer,
        snapshot.ontology_annotations(
            scope=AxiomScope.DOCUMENT, document_key=record.document_key
        ),
        SectionKind.ANNOTATIONS,
        ids,
    )
    _write_references(
        writer,
        snapshot.iter_axioms(scope=AxiomScope.DOCUMENT, document_key=record.document_key),
        SectionKind.AXIOMS,
        ids,
    )
    _write_references(
        writer,
        snapshot.iter_extensions(scope=AxiomScope.DOCUMENT, document_key=record.document_key),
        SectionKind.SWRL,
        ids,
    )
    return writer.finish()


def _imports_row(
    manifest: ImportManifest,
    ids: Mapping[SectionKind, Mapping[bytes, int]],
) -> bytes:
    writer = ByteWriter()
    writer.u8(_POLICIES[manifest.policy])
    writer.u8(int(manifest.offline))
    writer.raw(manifest.resolver_configuration_fingerprint)
    writer.u64(len(manifest.edges))
    for edge in manifest.edges:
        writer.u32(_string_id(edge.importing_document_key, ids))
        writer.u32(_node_id(edge.import_iri, SectionKind.IRIS, ids))
        writer.u8(_IMPORT_STATUSES[edge.status])
        writer.u32(_string_id(edge.resolved_document_key, ids))
        writer.u32(_string_id(edge.resolver_name, ids))
        writer.u32(_string_id(None if edge.diagnostic is None else edge.diagnostic.code, ids))
    return writer.finish()


def _view_row(
    snapshot: OntologySnapshot,
    ids: Mapping[SectionKind, Mapping[bytes, int]],
) -> bytes:
    writer = ByteWriter()
    writer.u32(_string_id(snapshot.root_document_key, ids))
    writer.u8(int(snapshot.is_complete))
    context = snapshot.structural_context
    if context is None:
        writer.u8(0)
        writer.u32(0)
    else:
        writer.u8(1 if context.kind is StructuralContextKind.OVERLAY else 2)
        writer.u32(len(context.fingerprints))
        for fingerprint in context.fingerprints:
            _write_fingerprint(writer, fingerprint)
    _write_fingerprint(writer, snapshot.structural_fingerprint)
    _write_fingerprint(writer, snapshot.logical_fingerprint)
    _write_fingerprint(writer, snapshot.signature_fingerprint)
    writer.u64(len(snapshot.documents))
    closure_annotations = snapshot.ontology_annotations()
    closure_axioms = tuple(snapshot.iter_axioms())
    closure_extensions = tuple(snapshot.iter_extensions())
    writer.u64(len(closure_axioms))
    _write_references(writer, closure_annotations, SectionKind.ANNOTATIONS, ids)
    _write_references(writer, closure_axioms, SectionKind.AXIOMS, ids)
    _write_references(writer, closure_extensions, SectionKind.SWRL, ids)
    return writer.finish()


def _origin_rows(
    origins: OriginIndex,
    ids: Mapping[SectionKind, Mapping[bytes, int]],
) -> list[bytes]:
    rows: list[bytes] = []
    for digest, occurrences in sorted(origins.entries.items()):
        canonical_occurrences = tuple(sorted(occurrences, key=_origin_sort_key))
        writer = ByteWriter()
        writer.raw(digest)
        writer.u64(len(canonical_occurrences))
        for occurrence in canonical_occurrences:
            writer.u32(_string_id(occurrence.document_key, ids))
            writer.u64(occurrence.occurrence)
            span = occurrence.span
            for value in (
                None if span is None else span.byte_start,
                None if span is None else span.byte_end,
                None if span is None else span.line_start,
                None if span is None else span.column_start,
                None if span is None else span.line_end,
                None if span is None else span.column_end,
            ):
                writer.u64(_NONE_U64 if value is None else value)
        rows.append(writer.finish())
    return rows


def _footer_row(
    snapshot: OntologySnapshot,
    sections: Mapping[SectionKind, bytes],
    digests: Mapping[SectionKind, bytes],
) -> bytes:
    writer = ByteWriter()
    _write_fingerprint(writer, snapshot.structural_fingerprint)
    _write_fingerprint(writer, snapshot.logical_fingerprint)
    _write_fingerprint(writer, snapshot.signature_fingerprint)
    writer.u16(len(REQUIRED_SECTIONS) - 1)
    for kind in REQUIRED_SECTIONS[:-1]:
        writer.u16(int(kind))
        writer.u64(_table_count(sections[kind]))
        writer.raw(digests[kind])
    return writer.finish()


def _assemble(
    sections: Mapping[SectionKind, bytes],
    feature_flags: int,
    limits: ParseLimits,
    guard: Guard,
) -> bytes:
    ordered = tuple(sorted(sections.items(), key=lambda item: int(item[0])))
    directory_length = len(ordered) * DIRECTORY_ENTRY_SIZE
    cursor = _align(HEADER_SIZE + directory_length)
    entries: list[DirectoryEntry] = []
    for index, (kind, section) in enumerate(ordered):
        guard.check(index)
        cursor = _align(cursor)
        flags = SECTION_REQUIRED if kind in REQUIRED_SECTIONS else SECTION_OPTIONAL
        entries.append(
            DirectoryEntry(
                int(kind),
                flags,
                SECTION_SCHEMAS[kind],
                cursor,
                len(section),
                len(section),
                _table_count(section),
                hashlib.sha256(section).digest(),
            )
        )
        cursor += len(section)
    limits.enforce("max_wire_bytes", cursor)
    limits.enforce("max_temporary_bytes", cursor)
    result = bytearray(cursor)
    for index, entry in enumerate(entries):
        DIRECTORY_STRUCT.pack_into(
            result,
            HEADER_SIZE + index * DIRECTORY_ENTRY_SIZE,
            entry.kind,
            entry.flags,
            entry.schema,
            entry.offset,
            entry.stored_length,
            entry.decoded_length,
            entry.row_count,
            entry.digest,
        )
    for (kind, section), entry in zip(ordered, entries, strict=True):
        del kind
        result[entry.offset : entry.end] = section
    HEADER_STRUCT.pack_into(
        result,
        0,
        MAGIC,
        WIRE_MAJOR,
        WIRE_MINOR,
        HEADER_SIZE,
        feature_flags,
        len(entries),
        MODEL_SCHEMA,
        CANONICAL_PROFILE,
        len(result),
        HEADER_SIZE,
        directory_length,
        bytes(32),
        0,
        0,
    )
    header = bytearray(result[:HEADER_SIZE])
    header[56:92] = bytes(36)
    struct.pack_into("<I", result, 88, crc32c(header))
    hasher = hashlib.sha256()
    hasher.update(result[:56])
    hasher.update(bytes(36))
    hasher.update(result[92:])
    result[56:88] = hasher.digest()
    return bytes(result)


def _write_fingerprint(writer: ByteWriter, fingerprint: Fingerprint) -> None:
    if fingerprint.algorithm != "sha256":
        raise ValueError("wire v1 supports only SHA-256 fingerprints")
    writer.u32(fingerprint.schema)
    writer.raw(fingerprint.digest)


def _write_references(
    writer: ByteWriter,
    values: Iterable[StructuralNode],
    kind: SectionKind,
    ids: Mapping[SectionKind, Mapping[bytes, int]],
) -> None:
    selected = sorted({_node_id(value, kind, ids) for value in values})
    writer.ids(selected)


def _node_id(
    value: StructuralNode | None,
    kind: SectionKind,
    ids: Mapping[SectionKind, Mapping[bytes, int]],
) -> int:
    if value is None:
        return 0
    try:
        return ids[kind][canonical_bytes(value)]
    except KeyError as error:
        raise ValueError(
            f"wire encoder did not intern {type(value).__name__} in {kind.name}"
        ) from error


def _string_id(
    value: str | None,
    ids: Mapping[SectionKind, Mapping[bytes, int]],
) -> int:
    if value is None:
        return 0
    return ids[SectionKind.STRINGS][value.encode("utf-8")]


def _table_count(section: bytes) -> int:
    return cast(int, struct.unpack_from("<Q", section)[0])


def _align(value: int) -> int:
    return (value + ALIGNMENT - 1) & ~(ALIGNMENT - 1)


def _validate_feature_sections(image: WireImage) -> None:
    has_swrl = int(SectionKind.SWRL) in image.tables
    flag_swrl = bool(image.header.feature_flags & FEATURE_SWRL)
    if has_swrl != flag_swrl:
        raise WireVersionError(
            "SWRL section/feature capability mismatch", code="WIRE_FEATURE_SECTION"
        )


def _validate_model_table(
    table: TableView,
    expected: type[StructuralNode],
    limits: ParseLimits,
    guard: Guard,
    *,
    lazy: bool,
) -> None:
    for index, row in enumerate(table.rows()):
        guard.check(index)
        if lazy:
            spec = _scan_canonical(row, limits)
            valid = _constructor_matches(spec, expected)
            actual = spec.constructor.__name__
        else:
            value = _decode_model(row, limits)
            valid = isinstance(value, expected)
            actual = type(value).__name__
        if not valid:
            raise _corrupt(
                f"{SectionKind(table.kind).name} row has invalid {actual} category"
            )


def _validate_structural_table(
    table: TableView, limits: ParseLimits, guard: Guard, *, lazy: bool
) -> None:
    for index, row in enumerate(table.rows()):
        guard.check(index)
        if lazy:
            _scan_canonical(row, limits)
        else:
            _decode_model(row, limits)


def _validate_terms(
    table: TableView, limits: ParseLimits, guard: Guard, *, lazy: bool
) -> None:
    forbidden = (IRI, Entity, Literal, AnonymousIndividual, Annotation, AxiomNode)
    for index, row in enumerate(table.rows()):
        guard.check(index)
        if lazy:
            spec = _scan_canonical(row, limits)
            invalid = any(_constructor_matches(spec, item) for item in forbidden)
            actual = spec.constructor.__name__
        else:
            value = _decode_model(row, limits)
            invalid = isinstance(value, forbidden)
            actual = type(value).__name__
        if invalid:
            raise _corrupt(f"TERMS row has invalid {actual} category")


def _validate_sequences(table: TableView, limits: ParseLimits, guard: Guard) -> None:
    for index, row in enumerate(table.rows()):
        guard.check(index)
        reader = ByteReader(row, section="SEQUENCES")
        kind = reader.u8()
        if kind not in (1, 2):
            raise _corrupt("unknown SEQUENCES collection kind")
        count = reader.u64()
        if count > limits.max_sequence_arity:
            raise WireLimitError("SEQUENCES arity exceeds limits", code="WIRE_SEQUENCE_LIMIT")
        if count > reader.remaining // 32 or reader.remaining != count * 32:
            raise _corrupt("invalid SEQUENCES digest vector")
        reader.raw(count * 32)
        reader.finish()


def _read_document_row(
    image: WireImage,
    row: memoryview,
    limits: ParseLimits,
    *,
    collect: bool,
) -> _DocumentWire:
    reader = ByteReader(row, section="DOCUMENTS")
    strings = image.table(SectionKind.STRINGS)
    iris = image.table(SectionKind.IRIS)
    annotations = image.table(SectionKind.ANNOTATIONS)
    axioms = image.table(SectionKind.AXIOMS)
    extensions = image.tables.get(int(SectionKind.SWRL))
    extension_count = 0 if extensions is None else extensions.count
    key_id = _required_ref(reader.u32(), strings.count, "document key")
    key = _string(strings, key_id)
    if not key:
        raise _corrupt("document key is empty")
    try:
        key.encode("ascii")
    except UnicodeEncodeError as error:
        raise _corrupt("document key is not ASCII") from error
    document_ontology = _optional_ref(reader.u32(), iris.count, "document ontology IRI")
    document_version = _optional_ref(reader.u32(), iris.count, "document version IRI")
    document_iri = _optional_ref(reader.u32(), iris.count, "document IRI")
    record_ontology = _optional_ref(reader.u32(), iris.count, "record ontology IRI")
    record_version = _optional_ref(reader.u32(), iris.count, "record version IRI")
    record_iri = _optional_ref(reader.u32(), iris.count, "record document IRI")
    record_source = reader.raw(32)
    fingerprint = _read_fingerprint(reader)
    record_format = _enum_tag(reader.u8(), _FORMAT_BY_TAG, "document format")
    status = _enum_tag(reader.u8(), _DOCUMENT_STATUS_BY_TAG, "document status")
    provenance_source = reader.raw(32)
    digest_kind = _enum_tag(reader.u8(), _DIGEST_KIND_BY_TAG, "digest kind")
    byte_length = reader.u64()
    codepoint_length = reader.u64()
    provenance_iri = _optional_ref(reader.u32(), iris.count, "provenance document IRI")
    provenance_format = _enum_tag(reader.u8(), _FORMAT_BY_TAG, "provenance format")
    detection_basis = _enum_tag(reader.u8(), _DETECTION_BASIS_BY_TAG, "detection basis")
    has_expected = reader.boolean()
    expected = reader.raw(32) if has_expected else None
    parser_id = _required_ref(reader.u32(), strings.count, "parser string")
    backend_id = _required_ref(reader.u32(), strings.count, "backend string")
    parser = _string(strings, parser_id)
    backend = _string(strings, backend_id)
    if not parser or not backend:
        raise _corrupt("empty parser/backend metadata")
    api_version = (reader.u16(), reader.u16())
    model_schema = reader.u32()
    if model_schema != MODEL_SCHEMA:
        raise WireVersionError("document model schema is unsupported", code="WIRE_MODEL_SCHEMA")
    maximum = limits.max_wire_rows
    direct_imports = reader.references(
        maximum=maximum, target_rows=iris.count, collect=collect
    )
    raw_annotations = reader.references(
        maximum=maximum, target_rows=annotations.count, collect=collect
    )
    raw_axioms = reader.references(maximum=maximum, target_rows=axioms.count, collect=collect)
    raw_extensions = reader.references(
        maximum=maximum, target_rows=extension_count, collect=collect
    )
    effective_annotations = reader.references(
        maximum=maximum, target_rows=annotations.count, collect=collect
    )
    effective_axioms = reader.references(
        maximum=maximum, target_rows=axioms.count, collect=collect
    )
    effective_extensions = reader.references(
        maximum=maximum, target_rows=extension_count, collect=collect
    )
    reader.finish()
    meta = _DocumentMeta(
        key,
        document_ontology,
        document_version,
        document_iri,
        record_ontology,
        record_version,
        record_iri,
        record_source,
        fingerprint,
        cast(DocumentFormat, record_format),
        cast(DocumentStatus, status),
    )
    return _DocumentWire(
        meta,
        provenance_source,
        cast(DigestKind, digest_kind),
        byte_length,
        codepoint_length,
        provenance_iri,
        cast(DocumentFormat, provenance_format),
        cast(DetectionBasis, detection_basis),
        expected,
        parser,
        backend,
        api_version,
        model_schema,
        direct_imports,
        raw_annotations,
        raw_axioms,
        raw_extensions,
        effective_annotations,
        effective_axioms,
        effective_extensions,
    )


def _decode_document_row(
    image: WireImage,
    row: memoryview,
    limits: ParseLimits,
    cache: dict[tuple[SectionKind, int], StructuralNode],
) -> _DecodedDocument:
    wire = _read_document_row(image, row, limits, collect=True)
    meta = wire.meta
    ontology_id = OntologyID(
        cast(
            IRI | None,
            _decode_optional(
                image, SectionKind.IRIS, meta.document_ontology_iri_id, limits, cache
            ),
        ),
        cast(
            IRI | None,
            _decode_optional(
                image, SectionKind.IRIS, meta.document_version_iri_id, limits, cache
            ),
        ),
    )
    document_iri = cast(
        IRI | None,
        _decode_optional(image, SectionKind.IRIS, meta.document_document_iri_id, limits, cache),
    )
    provenance_iri = cast(
        IRI | None,
        _decode_optional(image, SectionKind.IRIS, wire.provenance_document_iri_id, limits, cache),
    )
    provenance = DocumentProvenance(
        wire.provenance_source_digest,
        wire.digest_kind,
        wire.byte_length,
        wire.codepoint_length,
        provenance_iri,
        None,
        wire.provenance_format,
        wire.detection_basis,
        expected_sha256=wire.expected_digest,
        parser=wire.parser,
        backend=wire.backend,
        api_version=wire.api_version,
        model_schema=wire.model_schema,
    )
    imports = tuple(
        cast(IRI, _decode_ref(image, SectionKind.IRIS, value, limits, cache))
        for value in wire.direct_imports
    )
    annotations = CanonicalSet(
        cast(Annotation, _decode_ref(image, SectionKind.ANNOTATIONS, value, limits, cache))
        for value in wire.raw_annotations
    )
    axioms = CanonicalSet(
        cast(AxiomNode, _decode_ref(image, SectionKind.AXIOMS, value, limits, cache))
        for value in wire.raw_axioms
    )
    extensions = CanonicalSet(
        _decode_ref(image, SectionKind.SWRL, value, limits, cache)
        for value in wire.raw_extensions
    )
    try:
        document = OntologyDocument(
            ontology_id,
            document_iri,
            imports,
            annotations,
            axioms,
            extensions,
            provenance,
        )
    except (TypeError, ValueError, ModelError, ResourceLimitError) as error:
        raise _translate_model_error(error) from error
    if document.document_fingerprint != meta.document_fingerprint:
        raise _corrupt("DOCUMENTS document fingerprint mismatch")
    return _DecodedDocument(
        meta,
        document,
        wire.effective_annotations,
        wire.effective_axioms,
        wire.effective_extensions,
    )


def _validate_imports(
    image: WireImage,
    document_keys: set[str],
    limits: ParseLimits,
    *,
    collect: bool,
) -> _ImportsWire:
    table = image.table(SectionKind.IMPORTS)
    if table.count != 1:
        raise _corrupt("IMPORTS must contain exactly one manifest row")
    reader = ByteReader(table.row(0), section="IMPORTS")
    policy = cast(ImportPolicy, _enum_tag(reader.u8(), _POLICY_BY_TAG, "import policy"))
    offline = reader.boolean()
    resolver_digest = reader.raw(32)
    edge_count = reader.u64()
    if edge_count > limits.max_wire_rows:
        raise WireLimitError("IMPORTS edge count exceeds limits", code="WIRE_ROW_LIMIT")
    # Every edge needs at least 21 bytes after the count.
    if edge_count > reader.remaining // 21:
        raise _corrupt("IMPORTS edge count exceeds row bounds")
    strings = image.table(SectionKind.STRINGS)
    iris = image.table(SectionKind.IRIS)
    values: list[tuple[str, int, ImportStatus, str | None, str | None, str | None]] | None
    values = [] if collect else None
    previous: tuple[bytes, bytes, str, bytes] | None = None
    for _ in range(edge_count):
        importer = _string(strings, _required_ref(reader.u32(), strings.count, "importer"))
        if importer not in document_keys:
            raise _corrupt("IMPORTS edge importer is absent from DOCUMENTS")
        iri_id = _required_ref(reader.u32(), iris.count, "import IRI")
        status = cast(
            ImportStatus, _enum_tag(reader.u8(), _IMPORT_STATUS_BY_TAG, "import status")
        )
        target_id = _optional_ref(reader.u32(), strings.count, "import target")
        resolver_id = _optional_ref(reader.u32(), strings.count, "resolver name")
        diagnostic_id = _optional_ref(reader.u32(), strings.count, "diagnostic code")
        target = None if target_id == 0 else _string(strings, target_id)
        resolver = None if resolver_id == 0 else _string(strings, resolver_id)
        diagnostic = None if diagnostic_id == 0 else _string(strings, diagnostic_id)
        if status is ImportStatus.RESOLVED:
            if target is None or target not in document_keys:
                raise _corrupt("resolved IMPORTS edge has no valid target")
        elif target is not None:
            raise _corrupt("non-resolved IMPORTS edge has a target")
        sort_key = (
            importer.encode("utf-8"),
            bytes(iris.row(iri_id - 1)),
            status.value,
            b"" if target is None else target.encode("utf-8"),
        )
        if previous is not None and sort_key < previous:
            raise _corrupt("IMPORTS edges are not in canonical order")
        previous = sort_key
        if values is not None:
            values.append((importer, iri_id, status, target, resolver, diagnostic))
    reader.finish()
    return _ImportsWire(policy, offline, resolver_digest, () if values is None else tuple(values))


def _decode_imports(
    image: WireImage,
    documents: tuple[_DocumentMeta, ...],
    limits: ParseLimits,
    cache: dict[tuple[SectionKind, int], StructuralNode],
) -> ImportManifest:
    keys = {item.key for item in documents}
    wire = _validate_imports(image, keys, limits, collect=True)
    records: list[DocumentRecord] = []
    for item in documents:
        record_id = OntologyID(
            cast(
                IRI | None,
                _decode_optional(
                    image, SectionKind.IRIS, item.record_ontology_iri_id, limits, cache
                ),
            ),
            cast(
                IRI | None,
                _decode_optional(
                    image, SectionKind.IRIS, item.record_version_iri_id, limits, cache
                ),
            ),
        )
        record_iri = cast(
            IRI | None,
            _decode_optional(image, SectionKind.IRIS, item.record_document_iri_id, limits, cache),
        )
        records.append(
            DocumentRecord(
                item.key,
                record_id,
                record_iri,
                item.record_source_digest,
                item.document_fingerprint,
                item.record_format,
                item.status,
            )
        )
    edges: list[ImportEdge] = []
    for importer, iri_id, status, target, resolver, diagnostic_code in wire.edges:
        iri = cast(IRI, _decode_ref(image, SectionKind.IRIS, iri_id, limits, cache))
        diagnostic = (
            None
            if diagnostic_code is None
            else Diagnostic(
                diagnostic_code,
                Severity.ERROR,
                "import diagnostic restored from PYOCORE wire metadata",
            )
        )
        edges.append(ImportEdge(importer, iri, status, target, resolver, None, diagnostic))
    try:
        return ImportManifest(
            wire.policy,
            wire.offline,
            wire.resolver_digest,
            tuple(records),
            tuple(edges),
        )
    except (TypeError, ValueError, ModelError) as error:
        raise _translate_model_error(error) from error


def _read_view(
    image: WireImage,
    document_keys: set[str],
    limits: ParseLimits,
    *,
    collect: bool,
) -> tuple[ViewSummary, _ViewRefs]:
    table = image.table(SectionKind.VIEW)
    if table.count != 1:
        raise _corrupt("VIEW must contain exactly one row")
    reader = ByteReader(table.row(0), section="VIEW")
    strings = image.table(SectionKind.STRINGS)
    root_id = _required_ref(reader.u32(), strings.count, "root document key")
    root = _string(strings, root_id)
    if root not in document_keys:
        raise _corrupt("VIEW root document key is invalid")
    complete = reader.boolean()
    context_tag = reader.u8()
    context_count = reader.u32()
    if context_count > limits.max_composite_members:
        raise WireLimitError("VIEW context count exceeds limits", code="WIRE_ROW_LIMIT")
    context_fingerprints = tuple(_read_fingerprint(reader) for _ in range(context_count))
    if context_tag == 0:
        if context_count:
            raise _corrupt("plain VIEW has structural context members")
        context = None
    elif context_tag == 1:
        if context_count != 1:
            raise _corrupt("overlay VIEW context must have one anchor")
        context = StructuralContext(StructuralContextKind.OVERLAY, context_fingerprints)
    elif context_tag == 2:
        if context_count < 2:
            raise _corrupt("composite VIEW context must have at least two members")
        context = StructuralContext(StructuralContextKind.COMPOSITE, context_fingerprints)
    else:
        raise _corrupt("unknown VIEW structural context kind")
    structural = _read_fingerprint(reader)
    logical = _read_fingerprint(reader)
    signature_value = _read_fingerprint(reader)
    document_count = reader.u64()
    if document_count > limits.max_documents:
        raise WireLimitError("VIEW document count exceeds limits", code="WIRE_DOCUMENT_LIMIT")
    effective_axiom_count = reader.u64()
    if effective_axiom_count > limits.max_axioms:
        raise WireLimitError("VIEW axiom count exceeds limits", code="WIRE_AXIOM_LIMIT")
    annotations = reader.references(
        maximum=limits.max_annotations,
        target_rows=image.table(SectionKind.ANNOTATIONS).count,
        collect=collect,
    )
    axioms = reader.references(
        maximum=limits.max_axioms,
        target_rows=image.table(SectionKind.AXIOMS).count,
        collect=collect,
    )
    swrl = image.tables.get(int(SectionKind.SWRL))
    extensions = reader.references(
        maximum=limits.max_wire_rows,
        target_rows=0 if swrl is None else swrl.count,
        collect=collect,
    )
    reader.finish()
    if collect and len(axioms) != effective_axiom_count:
        raise _corrupt("VIEW effective axiom count disagrees with postings")
    return (
        ViewSummary(
            root,
            complete,
            context,
            structural,
            logical,
            signature_value,
            document_count,
            0,
            effective_axiom_count,
        ),
        _ViewRefs(annotations, axioms, extensions),
    )


def _validate_origins(
    image: WireImage,
    document_keys: set[str],
    limits: ParseLimits,
    *,
    collect: bool,
) -> OriginIndex | None:
    table = image.table(SectionKind.ORIGINS)
    strings = image.table(SectionKind.STRINGS)
    entries: dict[bytes, tuple[OriginOccurrence, ...]] | None = {} if collect else None
    total = 0
    previous_digest: bytes | None = None
    for row in table.rows():
        reader = ByteReader(row, section="ORIGINS")
        digest = reader.raw(32)
        if previous_digest is not None and digest <= previous_digest:
            raise _corrupt("ORIGINS digests are not canonical")
        previous_digest = digest
        count = reader.u64()
        total += count
        if total > limits.max_origin_entries:
            raise WireLimitError("ORIGINS entries exceed limits", code="WIRE_ORIGIN_LIMIT")
        if count > reader.remaining // 60:
            raise _corrupt("ORIGINS occurrence count exceeds row bounds")
        occurrences: list[OriginOccurrence] | None = [] if collect else None
        previous_occurrence: tuple[bytes, int, tuple[int, ...]] | None = None
        for _ in range(count):
            document_id = _required_ref(reader.u32(), strings.count, "origin document")
            document_key = _string(strings, document_id)
            if not document_key:
                raise _corrupt("ORIGINS contains an empty document provenance key")
            occurrence = reader.u64()
            span_values = tuple(_optional_u64(reader.u64()) for _ in range(6))
            span = None
            if any(value is not None for value in span_values):
                try:
                    span = SourceSpan(*span_values)
                except (TypeError, ValueError) as error:
                    raise _corrupt("invalid ORIGINS source span") from error
            item = OriginOccurrence(document_key, occurrence, span)
            sort_key = (
                document_key.encode("utf-8"),
                occurrence,
                tuple(_NONE_U64 if value is None else value for value in span_values),
            )
            if previous_occurrence is not None and sort_key <= previous_occurrence:
                raise _corrupt("ORIGINS occurrences are not canonical")
            previous_occurrence = sort_key
            if occurrences is not None:
                occurrences.append(item)
        reader.finish()
        if entries is not None:
            entries[digest] = tuple(occurrences or ())
    return None if entries is None else OriginIndex(entries)


def _validate_footer(image: WireImage, summary: ViewSummary) -> None:
    table = image.table(SectionKind.FOOTER)
    if table.count != 1:
        raise _corrupt("FOOTER must contain exactly one row")
    reader = ByteReader(table.row(0), section="FOOTER")
    fingerprints = tuple(_read_fingerprint(reader) for _ in range(3))
    expected_fingerprints = (
        summary.structural_fingerprint,
        summary.logical_fingerprint,
        summary.signature_fingerprint,
    )
    if fingerprints != expected_fingerprints:
        raise _corrupt("FOOTER fingerprint ledger disagrees with VIEW")
    count = reader.u16()
    if count != len(REQUIRED_SECTIONS) - 1:
        raise _corrupt("FOOTER required-section count is invalid")
    entry_by_kind = {entry.kind: entry for entry in image.entries}
    for expected_kind in REQUIRED_SECTIONS[:-1]:
        kind = reader.u16()
        rows = reader.u64()
        digest = reader.raw(32)
        if kind != int(expected_kind):
            raise _corrupt("FOOTER section kinds are not canonical")
        entry = entry_by_kind[kind]
        if rows != entry.row_count or digest != entry.digest:
            raise _corrupt("FOOTER section ledger disagrees with directory")
    reader.finish()


def _compare_decoded_view(
    image: WireImage,
    snapshot: OntologySnapshot,
    decoded: tuple[_DecodedDocument, ...],
    summary: ViewSummary,
    view_refs: _ViewRefs,
) -> None:
    if (
        snapshot.structural_fingerprint != summary.structural_fingerprint
        or snapshot.logical_fingerprint != summary.logical_fingerprint
        or snapshot.signature_fingerprint != summary.signature_fingerprint
    ):
        raise _corrupt("decoded ontology fingerprints disagree with VIEW")
    if snapshot.is_complete != summary.complete:
        raise _corrupt("decoded completeness disagrees with VIEW")
    root_meta = next(item.meta for item in decoded if item.meta.key == summary.root_document_key)
    if root_meta.status is not DocumentStatus.ROOT:
        raise _corrupt("VIEW root key does not identify the root document record")
    # Compare effective scoped values against encoded postings. This catches
    # anonymous-scope/reference substitution bugs without retaining a second
    # decoded object graph.
    for item in decoded:
        key = item.meta.key
        _assert_postings(
            image.table(SectionKind.ANNOTATIONS),
            item.effective_annotations,
            snapshot.ontology_annotations(scope=AxiomScope.DOCUMENT, document_key=key),
            "DOCUMENTS effective annotations",
        )
        _assert_postings(
            image.table(SectionKind.AXIOMS),
            item.effective_axioms,
            snapshot.iter_axioms(scope=AxiomScope.DOCUMENT, document_key=key),
            "DOCUMENTS effective axioms",
        )
        _assert_postings(
            image.tables.get(int(SectionKind.SWRL)),
            item.effective_extensions,
            snapshot.iter_extensions(scope=AxiomScope.DOCUMENT, document_key=key),
            "DOCUMENTS effective extensions",
        )
    _assert_postings(
        image.table(SectionKind.ANNOTATIONS),
        view_refs.annotations,
        snapshot.ontology_annotations(),
        "VIEW annotations",
    )
    _assert_postings(
        image.table(SectionKind.AXIOMS),
        view_refs.axioms,
        snapshot.iter_axioms(),
        "VIEW axioms",
    )
    _assert_postings(
        image.tables.get(int(SectionKind.SWRL)),
        view_refs.extensions,
        snapshot.iter_extensions(),
        "VIEW extensions",
    )


def _assert_postings(
    table: TableView | None,
    expected: tuple[int, ...],
    values: Iterable[StructuralNode],
    label: str,
) -> None:
    encoded = tuple(canonical_bytes(value) for value in values)
    if len(encoded) != len(expected):
        raise _corrupt(f"{label} count mismatch")
    if table is None:
        if expected or encoded:
            raise _corrupt(f"{label} references absent section")
        return
    for value, reference in zip(encoded, expected, strict=True):
        if value != bytes(table.row(reference - 1)):
            raise _corrupt(f"{label} reference mismatch")


def _decode_model(row: memoryview, limits: ParseLimits) -> StructuralNode:
    try:
        return decode_canonical(bytes(row), limits=limits)
    except ResourceLimitError as error:
        raise WireLimitError(
            "canonical model row exceeds wire limits", code="WIRE_MODEL_LIMIT"
        ) from error
    except (ModelError, TypeError, ValueError, RecursionError) as error:
        raise _corrupt("invalid canonical model row") from error


@dataclass(slots=True)
class _ScanBudget:
    limits: ParseLimits
    terms: int = 0

    def enter(self, depth: int) -> None:
        self.terms += 1
        if depth > self.limits.max_nesting_depth:
            raise WireLimitError(
                "canonical row nesting exceeds limits", code="WIRE_MODEL_LIMIT"
            )
        if self.terms > self.limits.max_terms:
            raise WireLimitError("canonical row term count exceeds limits", code="WIRE_MODEL_LIMIT")


def _scan_canonical(row: memoryview, limits: ParseLimits) -> ConstructorSpec:
    budget = _ScanBudget(limits)
    spec, offset = _scan_node(row, 0, len(row), budget, 0)
    if offset != len(row):
        raise _corrupt("trailing bytes in canonical model row")
    return spec


def _scan_node(
    data: memoryview,
    offset: int,
    end: int,
    budget: _ScanBudget,
    depth: int,
) -> tuple[ConstructorSpec, int]:
    budget.enter(depth)
    tag, offset = _scan_varint(data, offset, end)
    try:
        spec = SPEC_BY_TAG[tag]
    except KeyError as error:
        raise _corrupt("unknown canonical model tag") from error
    for _field in spec.fields:
        if offset >= end:
            raise _corrupt("truncated canonical model component")
        marker = data[offset]
        offset += 1
        if marker == 1:
            payload_start, payload_end, offset = _scan_frame(data, offset, end)
            _child, consumed = _scan_node(
                data, payload_start, payload_end, budget, depth + 1
            )
            if consumed != payload_end:
                raise _corrupt("trailing bytes in nested canonical node")
        elif marker == 6:
            count, offset = _scan_varint(data, offset, end)
            if count > budget.limits.max_sequence_arity:
                raise WireLimitError(
                    "canonical set arity exceeds limits", code="WIRE_MODEL_LIMIT"
                )
            if count > end - offset:
                raise _corrupt("canonical set count exceeds row bounds")
            previous: bytes | None = None
            for _ in range(count):
                payload_start, payload_end, offset = _scan_frame(data, offset, end)
                current = bytes(data[payload_start:payload_end])
                if previous is not None and current <= previous:
                    raise _corrupt("canonical set members are not strictly sorted")
                previous = current
                _child, consumed = _scan_node(
                    data, payload_start, payload_end, budget, depth + 1
                )
                if consumed != payload_end:
                    raise _corrupt("trailing bytes in canonical set member")
        elif marker == 7:
            count, offset = _scan_varint(data, offset, end)
            if count > budget.limits.max_sequence_arity:
                raise WireLimitError(
                    "canonical sequence arity exceeds limits", code="WIRE_MODEL_LIMIT"
                )
            if count > end - offset:
                raise _corrupt("canonical sequence count exceeds row bounds")
            for _ in range(count):
                if offset >= end:
                    raise _corrupt("truncated canonical sequence")
                item_marker = data[offset]
                offset += 1
                if item_marker == 1:
                    payload_start, payload_end, offset = _scan_frame(data, offset, end)
                    _child, consumed = _scan_node(
                        data, payload_start, payload_end, budget, depth + 1
                    )
                    if consumed != payload_end:
                        raise _corrupt("trailing bytes in canonical sequence node")
                else:
                    offset = _scan_scalar(item_marker, data, offset, end)
        else:
            offset = _scan_scalar(marker, data, offset, end)
    return spec, offset


def _scan_scalar(marker: int, data: memoryview, offset: int, end: int) -> int:
    if marker == 0:
        return offset
    if marker in (2, 3, 5):
        payload_start, payload_end, offset = _scan_frame(data, offset, end)
        if marker in (2, 5):
            try:
                bytes(data[payload_start:payload_end]).decode("ascii" if marker == 5 else "utf-8")
            except UnicodeDecodeError as error:
                raise _corrupt("invalid canonical text encoding") from error
        return offset
    if marker == 4:
        _value, offset = _scan_varint(data, offset, end)
        return offset
    raise _corrupt("unknown canonical scalar marker")


def _scan_frame(data: memoryview, offset: int, end: int) -> tuple[int, int, int]:
    length, payload_start = _scan_varint(data, offset, end)
    payload_end = payload_start + length
    if payload_end < payload_start or payload_end > end:
        raise _corrupt("truncated canonical framed component")
    return payload_start, payload_end, payload_end


def _scan_varint(data: memoryview, offset: int, end: int) -> tuple[int, int]:
    start = offset
    value = 0
    shift = 0
    while offset < end:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            if offset - start > 1 and byte == 0:
                raise _corrupt("nonminimal canonical varint")
            return value, offset
        shift += 7
        if shift > 1_000_000:
            raise _corrupt("canonical varint is unreasonably long")
    raise _corrupt("truncated canonical varint")


def _constructor_matches(spec: ConstructorSpec, expected: type[StructuralNode]) -> bool:
    constructor = spec.constructor
    if expected is Entity:
        return constructor is Entity
    return issubclass(constructor, expected)


def _decode_ref(
    image: WireImage,
    kind: SectionKind,
    reference: int,
    limits: ParseLimits,
    cache: dict[tuple[SectionKind, int], StructuralNode],
) -> StructuralNode:
    key = (kind, reference)
    retained = cache.get(key)
    if retained is not None:
        return retained
    table = image.tables.get(int(kind))
    if table is None or reference < 1 or reference > table.count:
        raise _corrupt(f"invalid {kind.name} reference")
    value = _decode_model(table.row(reference - 1), limits)
    cache[key] = value
    return value


def _decode_optional(
    image: WireImage,
    kind: SectionKind,
    reference: int,
    limits: ParseLimits,
    cache: dict[tuple[SectionKind, int], StructuralNode],
) -> StructuralNode | None:
    return None if reference == 0 else _decode_ref(image, kind, reference, limits, cache)


def _decode_utf8(value: bytes | memoryview, section: str) -> str:
    try:
        return bytes(value).decode("utf-8")
    except UnicodeDecodeError as error:
        raise _corrupt(f"invalid UTF-8 in {section}") from error


def _string(table: TableView, reference: int) -> str:
    if reference < 1 or reference > table.count:
        raise _corrupt("invalid STRINGS reference")
    return _decode_utf8(table.row(reference - 1), "STRINGS")


def _required_ref(value: int, target_rows: int, label: str) -> int:
    if value == 0 or value > target_rows:
        raise _corrupt(f"invalid required {label} reference")
    return value


def _optional_ref(value: int, target_rows: int, label: str) -> int:
    if value > target_rows:
        raise _corrupt(f"invalid optional {label} reference")
    return value


def _read_fingerprint(reader: ByteReader) -> Fingerprint:
    schema = reader.u32()
    digest = reader.raw(32)
    try:
        return Fingerprint("sha256", schema, digest)
    except (TypeError, ValueError) as error:
        raise _corrupt("invalid wire fingerprint") from error


def _enum_tag(value: int, values: Mapping[int, object], label: str) -> object:
    try:
        return values[value]
    except KeyError as error:
        raise _corrupt(f"unknown {label} tag") from error


def _optional_u64(value: int) -> int | None:
    return None if value == _NONE_U64 else value


def _origin_sort_key(value: OriginOccurrence) -> tuple[bytes, int, tuple[int, ...]]:
    span = value.span
    components = (
        None if span is None else span.byte_start,
        None if span is None else span.byte_end,
        None if span is None else span.line_start,
        None if span is None else span.column_start,
        None if span is None else span.line_end,
        None if span is None else span.column_end,
    )
    return (
        value.document_key.encode("utf-8"),
        value.occurrence,
        tuple(_NONE_U64 if item is None else item for item in components),
    )


def _translate_model_error(error: Exception) -> WireCorruptionError | WireLimitError:
    if isinstance(error, ResourceLimitError):
        return WireLimitError("decoded snapshot exceeds resource limits", code="WIRE_MODEL_LIMIT")
    return _corrupt("wire rows do not form a valid ontology snapshot")


def _read_wire_source(
    data: bytes | bytearray | memoryview | BinaryIO,
    limits: ParseLimits,
    cancellation_token: CancellationToken | None,
) -> bytes | bytearray | memoryview:
    if isinstance(data, (bytes, bytearray, memoryview)):
        return data
    read = getattr(data, "read", None)
    if not callable(read):
        raise TypeError("data must be bytes-like or a binary file object")
    guard = Guard(limits, cancellation_token)
    result = bytearray()
    while True:
        guard.check(len(result))
        remaining = limits.max_wire_bytes - len(result)
        chunk_size = min(1024 * 1024, remaining + 1)
        chunk = read(chunk_size)
        if not isinstance(chunk, bytes):
            raise TypeError("binary wire stream read() must return bytes")
        if not chunk:
            break
        result.extend(chunk)
        if len(result) > limits.max_wire_bytes:
            raise WireLimitError("wire stream exceeds max_wire_bytes", code="WIRE_BYTE_LIMIT")
    return result


def _corrupt(message: str) -> WireCorruptionError:
    return WireCorruptionError(message, code="WIRE_CORRUPTION")
