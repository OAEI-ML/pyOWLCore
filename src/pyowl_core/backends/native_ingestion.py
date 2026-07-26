"""Fail-closed Python seam for WP16-owned native ingestion bindings."""

from __future__ import annotations

import hashlib
import time
import warnings
from collections.abc import Callable, Iterable
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Protocol, cast

from pyowl_core.exceptions import BackendProtocolError

from . import native

if TYPE_CHECKING:
    from pyowl_core.backends.native_handoff_v2 import NativeDiagnosticReferenceKindsV2
    from pyowl_core.cancellation import CancellationToken
    from pyowl_core.config import LoadOptions
    from pyowl_core.diagnostics import Diagnostic
    from pyowl_core.document.snapshot import OntologySnapshot
    from pyowl_core.io.formats.detection import FormatDetection
    from pyowl_core.io.resolver import ImportResolver
    from pyowl_core.io.source import SourcePayload
    from pyowl_core.limits import ParseLimits
    from pyowl_core.model import IRI, StructuralNode


class NativeIngestionExtension(Protocol):
    INGESTION_FEATURES: tuple[str, ...]


class _RetainedStructuralExtension(NativeIngestionExtension, Protocol):
    _retain_structural_snapshot_v2: Callable[..., object]
    _merge_parsed_structural_snapshot_v2: Callable[..., object]
    _prepare_parsed_structural_snapshot_v2: Callable[..., object]
    _finalize_parsed_structural_snapshot_v2: Callable[..., object]
    _prepare_parsed_structural_closure_v2: Callable[..., object]
    _finalize_parsed_structural_closure_v2: Callable[..., object]


def _closure_publication_checkpoint_v2(
    cancellation_token: CancellationToken | None,
) -> None:
    """Keep resolver-built closure preparation cooperatively cancellable."""

    if cancellation_token is not None:
        cancellation_token.check()


def _closure_canonical_rows_v2(
    values: Iterable[StructuralNode],
    cancellation_token: CancellationToken | None,
) -> tuple[bytes, ...]:
    """Encode one root collection with cancellation around every row."""

    from pyowl_core.model import canonical_bytes

    rows: list[bytes] = []
    for value in values:
        _closure_publication_checkpoint_v2(cancellation_token)
        rows.append(canonical_bytes(value))
        _closure_publication_checkpoint_v2(cancellation_token)
    return tuple(rows)


def _snapshot_anonymous_scope_targets_v2(
    snapshot: OntologySnapshot,
    raw_documents: tuple[object, ...],
    effective_documents: tuple[object, ...],
) -> tuple[bytes | None, ...]:
    """Return nonzero anonymous scopes aligned with retained document owners."""

    from pyowl_core.model import encode_varint

    records = snapshot.import_manifest.documents
    grouped: dict[bytes, list[tuple[bytes, str, int]]] = {}
    for document_ordinal, record in enumerate(records):
        grouped.setdefault(record.document_fingerprint.digest, []).append(
            (record.source_sha256, record.document_key, document_ordinal)
        )
    ordinals = [0] * len(records)
    for group in grouped.values():
        for scope_ordinal, (_source, _key, document_ordinal) in enumerate(sorted(group)):
            ordinals[document_ordinal] = scope_ordinal
    return tuple(
        (
            hashlib.sha256(
                b"pyowl-core:snapshot-document-scope:v1\x00"
                + record.document_fingerprint.digest
                + encode_varint(scope_ordinal)
            ).digest()
            if scope_ordinal > 0 and raw != effective
            else None
        )
        for record, scope_ordinal, raw, effective in zip(
            records,
            ordinals,
            raw_documents,
            effective_documents,
            strict=True,
        )
    )


def _snapshot_anonymous_scope_candidates_v2(
    snapshot: OntologySnapshot,
) -> tuple[bytes | None, ...]:
    """Return repeated-fingerprint scope candidates using O(documents) metadata."""

    from pyowl_core.model import encode_varint

    records = snapshot.import_manifest.documents
    grouped: dict[bytes, list[tuple[bytes, str, int]]] = {}
    for document_ordinal, record in enumerate(records):
        grouped.setdefault(record.document_fingerprint.digest, []).append(
            (record.source_sha256, record.document_key, document_ordinal)
        )
    ordinals = [0] * len(records)
    for group in grouped.values():
        for scope_ordinal, (_source, _key, document_ordinal) in enumerate(sorted(group)):
            ordinals[document_ordinal] = scope_ordinal
    return tuple(
        (
            hashlib.sha256(
                b"pyowl-core:snapshot-document-scope:v1\x00"
                + record.document_fingerprint.digest
                + encode_varint(scope_ordinal)
            ).digest()
            if scope_ordinal > 0
            else None
        )
        for record, scope_ordinal in zip(records, ordinals, strict=True)
    )


def require_ingestion_binding(capability: str) -> NativeIngestionExtension:
    """Require a capability registered specifically by the ingestion seam."""

    extension = native.require(capability)
    if capability not in extension.INGESTION_FEATURES:
        raise BackendProtocolError(
            "native capability is not registered by the ingestion binding seam",
            code="NATIVE_INGESTION_REGISTRATION",
        )
    return cast(NativeIngestionExtension, extension)


@dataclass(frozen=True, slots=True)
class _CanonicalOccurrenceV2:
    encoded: bytes
    byte_start: int
    byte_end: int
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class _ScannedFunctionalResultV2:
    ontology_iri: IRI | None
    ontology_iri_row: bytes | None
    version_iri: IRI | None
    version_iri_row: bytes | None
    imports: tuple[tuple[bytes, IRI], ...]
    rows: tuple[tuple[bytes, ...], tuple[bytes, ...], tuple[bytes, ...]]
    occurrences: tuple[_CanonicalOccurrenceV2, ...]
    logical_axioms: tuple[bytes, ...]
    logical_extensions: tuple[bytes, ...]
    signature_entities: tuple[bytes, ...]
    decoded_codepoint_length: int
    canonical_rows_scanned: int
    structural_occurrence_rows_scanned: int
    metadata_iri_objects_materialized: int
    has_anonymous: bool


class _CanonicalResultScannerV2:
    """Validate canonical rows and collect fingerprint inputs without models."""

    __slots__ = (
        "_cancel",
        "_limits",
        "_started",
        "_terms",
        "anonymous",
        "entities",
        "rows",
    )

    def __init__(self, limits: object, cancellation_token: CancellationToken | None) -> None:
        from pyowl_core.limits import ParseLimits

        if not isinstance(limits, ParseLimits):
            raise TypeError("limits must be ParseLimits")
        self._limits = limits
        self._cancel = cancellation_token
        self._started = time.monotonic()
        self._terms = 0
        self.anonymous = False
        self.entities: set[bytes] = set()
        self.rows = 0

    def scan(self, payload: bytes) -> tuple[int, tuple[tuple[int, int], ...]]:
        if type(payload) is not bytes:
            raise BackendProtocolError(
                "native parser returned a non-bytes canonical row",
                code="NATIVE_PARSE_MODEL",
            )
        self.rows += 1
        tag, end, components = self._node(payload, 0, 0)
        if end != len(payload):
            raise BackendProtocolError(
                "native parser returned trailing canonical model data",
                code="NATIVE_PARSE_MODEL",
            )
        return tag, components

    def _node(
        self,
        data: bytes,
        offset: int,
        depth: int,
    ) -> tuple[int, int, tuple[tuple[int, int], ...]]:
        from pyowl_core.model.registry import SPEC_BY_TAG

        self._check(depth)
        node_start = offset
        tag, offset = _canonical_varint_v2(data, offset)
        spec = SPEC_BY_TAG.get(tag)
        if spec is None:
            raise BackendProtocolError(
                "native parser returned an unknown canonical model tag",
                code="NATIVE_PARSE_MODEL",
            )
        components: list[tuple[int, int]] = []
        for _field in spec.fields:
            component_start = offset
            offset = self._component(data, offset, depth + 1)
            components.append((component_start, offset))
        encoded = data[node_start:offset]
        if spec.tag_name == "ENTITY":
            self.entities.add(encoded)
        elif spec.tag_name == "ANONYMOUS_INDIVIDUAL":
            self.anonymous = True
        return tag, offset, tuple(components)

    def _component(
        self,
        data: bytes,
        offset: int,
        depth: int,
        *,
        allow_collections: bool = True,
    ) -> int:
        if offset >= len(data):
            raise BackendProtocolError(
                "native parser returned a truncated canonical component",
                code="NATIVE_PARSE_MODEL",
            )
        marker = data[offset]
        offset += 1
        if marker == 0:
            return offset
        if marker in {2, 3, 5}:
            payload, offset = _canonical_frame_v2(data, offset)
            if marker in {2, 5}:
                try:
                    payload.decode("utf-8" if marker == 2 else "ascii")
                except UnicodeError as error:
                    raise BackendProtocolError(
                        "native parser returned invalid canonical text",
                        code="NATIVE_PARSE_MODEL",
                    ) from error
            return offset
        if marker == 4:
            _value, offset = _canonical_varint_v2(data, offset)
            return offset
        if marker == 1:
            payload, offset = _canonical_frame_v2(data, offset)
            _tag, consumed, _components = self._node(payload, 0, depth)
            if consumed != len(payload):
                raise BackendProtocolError(
                    "native parser returned trailing nested canonical data",
                    code="NATIVE_PARSE_MODEL",
                )
            return offset
        if marker not in {6, 7} or not allow_collections:
            raise BackendProtocolError(
                "native parser returned an unknown canonical component marker",
                code="NATIVE_PARSE_MODEL",
            )
        size, offset = _canonical_varint_v2(data, offset)
        self._limits.enforce("max_sequence_arity", size)
        previous: bytes | None = None
        for _ in range(size):
            if marker == 6:
                payload, offset = _canonical_frame_v2(data, offset)
                _tag, consumed, _components = self._node(payload, 0, depth)
                if consumed != len(payload):
                    raise BackendProtocolError(
                        "native parser returned trailing canonical set data",
                        code="NATIVE_PARSE_MODEL",
                    )
                if previous is not None and payload <= previous:
                    raise BackendProtocolError(
                        "native parser returned a noncanonical structural set",
                        code="NATIVE_PARSE_MODEL",
                    )
                previous = payload
            else:
                offset = self._component(
                    data,
                    offset,
                    depth,
                    allow_collections=False,
                )
        return offset

    def _check(self, depth: int) -> None:
        self._terms += 1
        self._limits.enforce("max_terms", self._terms)
        self._limits.enforce("max_nesting_depth", depth)
        if self._cancel is not None and (
            self._terms % self._limits.cancellation_check_interval == 0
        ):
            self._cancel.check()
        deadline = self._limits.deadline_seconds
        if deadline is not None:
            elapsed = time.monotonic() - self._started
            if elapsed >= deadline:
                from pyowl_core.exceptions import ResourceLimitError

                raise ResourceLimitError(
                    "resource limit deadline_seconds exceeded",
                    limit="deadline_seconds",
                    observed=elapsed,
                    allowed=deadline,
                )


def _canonical_varint_v2(data: bytes, offset: int) -> tuple[int, int]:
    from pyowl_core.model import encode_varint

    start = offset
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            if data[start:offset] != encode_varint(value):
                raise BackendProtocolError(
                    "native parser returned a nonminimal canonical integer",
                    code="NATIVE_PARSE_MODEL",
                )
            return value, offset
        shift += 7
        if shift > 1_000_000:
            raise BackendProtocolError(
                "native parser returned an unreasonably large canonical integer",
                code="NATIVE_PARSE_MODEL",
            )
    raise BackendProtocolError(
        "native parser returned a truncated canonical integer",
        code="NATIVE_PARSE_MODEL",
    )


def _canonical_frame_v2(data: bytes, offset: int) -> tuple[bytes, int]:
    length, offset = _canonical_varint_v2(data, offset)
    end = offset + length
    if end < offset or end > len(data):
        raise BackendProtocolError(
            "native parser returned a truncated canonical frame",
            code="NATIVE_PARSE_MODEL",
        )
    return data[offset:end], end


def _iri_from_canonical_v2(
    payload: bytes,
    scanner: _CanonicalResultScannerV2,
) -> IRI:
    from pyowl_core.exceptions import ModelError
    from pyowl_core.model import IRI
    from pyowl_core.model.registry import SPEC_BY_TAG

    tag, components = scanner.scan(payload)
    spec = SPEC_BY_TAG[tag]
    if spec.tag_name != "IRI" or len(components) != 1:
        raise BackendProtocolError(
            "native parser returned a non-IRI metadata row",
            code="NATIVE_PARSE_MODEL",
        )
    start, end = components[0]
    if start >= end or payload[start] != 2:
        raise BackendProtocolError(
            "native parser returned an invalid canonical IRI",
            code="NATIVE_PARSE_MODEL",
        )
    value, consumed = _canonical_frame_v2(payload, start + 1)
    if consumed != end:
        raise BackendProtocolError(
            "native parser returned trailing canonical IRI data",
            code="NATIVE_PARSE_MODEL",
        )
    scanner._limits.enforce("max_iri_bytes", len(value))
    try:
        return IRI(value.decode("utf-8"))
    except (UnicodeError, ModelError) as error:
        raise BackendProtocolError(
            "native parser returned invalid UTF-8 in an IRI",
            code="NATIVE_PARSE_MODEL",
        ) from error


def _without_top_level_annotations_v2(
    payload: bytes,
    tag: int,
    components: tuple[tuple[int, int], ...],
) -> bytes:
    from pyowl_core.model.registry import SPEC_BY_TAG

    spec = SPEC_BY_TAG[tag]
    try:
        ordinal = spec.fields.index("annotations")
    except ValueError:
        return payload
    start, end = components[ordinal]
    empty = bytes((6, 0))
    if payload[start:end] == empty:
        return payload
    return payload[:start] + empty + payload[end:]


def _scan_functional_result_v2(
    encoded: bytes,
    *,
    limits: ParseLimits,
    cancellation_token: CancellationToken | None,
    collect_provenance: bool,
) -> _ScannedFunctionalResultV2:
    from pyowl_core.extensions.swrl import SWRLRule
    from pyowl_core.model.axioms import AxiomNode
    from pyowl_core.model.registry import SPEC_BY_TAG

    reader = native._ResultReader(encoded)
    magic, schema, format_tag, decoded_codepoints = native._PARSE_RESULT_HEADER.unpack(
        reader.take(native._PARSE_RESULT_HEADER.size)
    )
    if magic != native._PARSE_RESULT_MAGIC or schema != 1 or format_tag != 4:
        raise BackendProtocolError(
            "native parser result has incompatible metadata",
            code="NATIVE_PARSE_VERSION",
        )
    scanner = _CanonicalResultScannerV2(limits, cancellation_token)
    metadata_objects = 0

    def optional_iri() -> tuple[IRI | None, bytes | None]:
        nonlocal metadata_objects
        marker = reader.u8()
        if marker == 0:
            return None, None
        if marker != 1:
            raise BackendProtocolError(
                "native parser returned an invalid optional value",
                code="NATIVE_PARSE_FRAMING",
            )
        row = reader.frame()
        value = _iri_from_canonical_v2(row, scanner)
        metadata_objects += 1
        return value, row

    ontology_iri, ontology_iri_row = optional_iri()
    version_iri, version_iri_row = optional_iri()
    if version_iri is not None and ontology_iri is None:
        raise BackendProtocolError(
            "native parser returned a version IRI without an ontology IRI",
            code="NATIVE_PARSE_MODEL",
        )
    import_count = reader.u64()
    reader.require_count(import_count, 1)
    import_rows: dict[bytes, IRI] = {}
    for _ in range(import_count):
        row = reader.frame()
        import_rows[row] = _iri_from_canonical_v2(row, scanner)
        metadata_objects += 1

    rows: list[list[bytes]] = [[], [], []]
    occurrences: list[_CanonicalOccurrenceV2] = []
    logical_axioms: set[bytes] = set()
    logical_extensions: set[bytes] = set()
    structural_occurrence_rows = 0

    def spanned(partition: int) -> None:
        nonlocal structural_occurrence_rows
        count = reader.u64()
        structural_occurrence_rows += count
        reader.require_count(count, 33)
        if partition == 0:
            limits.enforce("max_annotations", count)
        elif partition == 1:
            limits.enforce("max_axioms", count)
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
            row = reader.frame()
            tag, components = scanner.scan(row)
            spec = SPEC_BY_TAG[tag]
            if partition == 0:
                valid = spec.tag_name == "ANNOTATION"
            elif partition == 1:
                valid = issubclass(spec.constructor, AxiomNode)
            else:
                valid = spec.constructor is SWRLRule
            if not valid:
                raise BackendProtocolError(
                    "native parser returned a value in the wrong result partition",
                    code="NATIVE_PARSE_MODEL",
                )
            rows[partition].append(row)
            if collect_provenance:
                occurrences.append(_CanonicalOccurrenceV2(row, byte_start, byte_end, line, column))
            if partition == 1 and spec.category == "logical_axiom":
                logical_axioms.add(_without_top_level_annotations_v2(row, tag, components))
            elif partition == 2:
                logical_extensions.add(_without_top_level_annotations_v2(row, tag, components))

    spanned(0)
    spanned(1)
    spanned(2)
    prefix_count = reader.u64()
    reader.require_count(prefix_count, 2)
    limits.enforce("max_prefixes", prefix_count)
    previous_prefix: tuple[str, str] | None = None
    for _ in range(prefix_count):
        selected_prefix = (reader.text(), reader.text())
        if previous_prefix is not None and selected_prefix <= previous_prefix:
            raise BackendProtocolError(
                "native parser prefixes are not canonical",
                code="NATIVE_PARSE_FRAMING",
            )
        previous_prefix = selected_prefix
    reader.finish()
    occurrences.sort(key=lambda item: (item.byte_start, item.byte_end))
    return _ScannedFunctionalResultV2(
        ontology_iri,
        ontology_iri_row,
        version_iri,
        version_iri_row,
        tuple(sorted(import_rows.items())),
        cast(
            tuple[tuple[bytes, ...], tuple[bytes, ...], tuple[bytes, ...]],
            tuple(tuple(sorted(set(values))) for values in rows),
        ),
        tuple(occurrences),
        tuple(sorted(logical_axioms)),
        tuple(sorted(logical_extensions)),
        tuple(sorted(scanner.entities)),
        decoded_codepoints,
        scanner.rows,
        structural_occurrence_rows,
        metadata_objects,
        scanner.anonymous,
    )


def _frame_v2(value: bytes) -> bytes:
    from pyowl_core.model import encode_varint

    return encode_varint(len(value)) + value


def _collection_preimage_v2(rows: tuple[bytes, ...]) -> bytes:
    from pyowl_core.model import encode_varint

    return encode_varint(len(rows)) + b"".join(_frame_v2(row) for row in rows)


def _fingerprint_preimages_v2(
    scanned: _ScannedFunctionalResultV2,
    manifest: object,
    document_key: str,
) -> tuple[bytes, bytes, bytes, bytes]:
    from pyowl_core.document.imports import ImportManifest
    from pyowl_core.model import encode_varint

    if not isinstance(manifest, ImportManifest):
        raise TypeError("manifest must be ImportManifest")
    optional_identifiers = []
    for row in (scanned.ontology_iri_row, scanned.version_iri_row):
        optional_identifiers.append(b"0" if row is None else b"1" + _frame_v2(row))
    import_rows = tuple(row for row, _iri in scanned.imports)
    document = b"".join(
        (
            b"pyowl-core:document-fingerprint:v1\x00",
            *optional_identifiers,
            _collection_preimage_v2(import_rows),
            *(_collection_preimage_v2(rows) for rows in scanned.rows),
        )
    )
    structural = b"".join(
        (
            b"pyowl-core:snapshot-structural:v1\x00",
            _frame_v2(manifest.canonical_bytes()),
            _frame_v2(document_key.encode("ascii")),
            *(_collection_preimage_v2(rows) for rows in scanned.rows),
        )
    )
    logical = b"".join(
        (
            b"pyowl-core:snapshot-logical:v1\x00",
            b"datatype-policy:owl2-v1\x00",
            encode_varint(len(scanned.logical_axioms)),
            *(_frame_v2(row) for row in scanned.logical_axioms),
            encode_varint(len(scanned.logical_extensions)),
            *(b"E" + _frame_v2(row) for row in scanned.logical_extensions),
        )
    )
    signature = b"".join(
        (
            b"pyowl-core:snapshot-signature:v1\x00",
            b"\x01",
            encode_varint(len(scanned.signature_entities)),
            *(_frame_v2(row) for row in scanned.signature_entities),
        )
    )
    return document, structural, logical, signature


def _document_key_v2(
    ontology_iri: IRI | None,
    version_iri: IRI | None,
    document_fingerprint: bytes,
) -> str:
    if ontology_iri is None:
        payload = b"anonymous" + document_fingerprint
    else:
        identity = (
            ("ontology", ontology_iri.value)
            if version_iri is None
            else ("version", ontology_iri.value, version_iri.value)
        )
        payload = b"named" + b"".join(_frame_v2(item.encode("utf-8")) for item in identity)
    digest = hashlib.sha256(b"pyowl-core:document-key:v1\x00" + payload).hexdigest()
    return f"d1:{digest}"


def _structural_digest_v2(row: bytes) -> bytes:
    from pyowl_core.model import encode_varint

    return hashlib.sha256(b"pyowl-core:structural-value:v1\x00" + encode_varint(1) + row).digest()


@dataclass(frozen=True, slots=True)
class _RetainedFingerprintEvidenceV2:
    preimage_byte_length: int
    digest: bytes


@dataclass(frozen=True, slots=True)
class _RetainedFunctionalSeedV2:
    decoded_codepoint_length: int
    canonical_rows_scanned: int
    structural_occurrence_rows_scanned: int
    rows: tuple[int, int, int]
    metadata_iri_objects_materialized: int
    document_fingerprint: _RetainedFingerprintEvidenceV2
    ontology_iri: str | None
    version_iri: str | None
    imports: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _RetainedRdfXmlSeedV2:
    structural: _RetainedFunctionalSeedV2
    total_triples: int


@dataclass(frozen=True, slots=True)
class _RetainedRecordInventoryV1:
    count: int
    canonical_bytes: int
    transcript_bytes: int
    digest: bytes


@dataclass(frozen=True, slots=True)
class _PreparedRetainedPublicationV2:
    fingerprints: tuple[
        _RetainedFingerprintEvidenceV2,
        _RetainedFingerprintEvidenceV2,
        _RetainedFingerprintEvidenceV2,
        _RetainedFingerprintEvidenceV2,
    ]
    content_digests: tuple[bytes, bytes, bytes, bytes, bytes, bytes]
    record_inventories: tuple[
        _RetainedRecordInventoryV1,
        _RetainedRecordInventoryV1,
        _RetainedRecordInventoryV1,
        _RetainedRecordInventoryV1,
    ]
    root_count: int
    node_count: int
    source_map_rows_retained: int
    source_prefix_rows_retained: int
    origin_rows_retained: int
    max_facade_row_bytes: int
    canonical_rows_encoded: int
    canonical_bytes_encoded: int
    fingerprint_temporary_bytes: int
    origin_bytes_retained: int
    prepare_seconds: float
    scoped_roots: bool
    rdf_report: _PreparedRetainedRdfReportV2 | None


@dataclass(frozen=True, slots=True)
class _PreparedRetainedRdfReportV2:
    conformant: bool
    consumed_triples: int
    total_triples: int
    unconsumed_triple_count: int
    rule_count: int
    diagnostic_count: int
    digest: bytes
    retained_bytes: int


def _read_u16_v2(reader: native._ResultReader) -> int:
    return int.from_bytes(reader.take(2), "little")


def _read_text64_v2(reader: native._ResultReader, *, maximum: int) -> str:
    size = reader.u64()
    if size > maximum:
        raise BackendProtocolError(
            "native retained metadata text exceeds its configured bound",
            code="NATIVE_PARSE_MODEL",
        )
    try:
        return reader.take(size).decode("utf-8")
    except UnicodeError as error:
        raise BackendProtocolError(
            "native retained metadata is not UTF-8",
            code="NATIVE_PARSE_MODEL",
        ) from error


def _decode_retained_functional_seed_v2(
    encoded: bytes,
    limits: ParseLimits,
) -> _RetainedFunctionalSeedV2:
    reader = native._ResultReader(encoded)
    magic = reader.take(8)
    schema = _read_u16_v2(reader)
    flags = _read_u16_v2(reader)
    if magic != native._RETAINED_FUNCTIONAL_SEED_MAGIC_V2 or schema != 1 or flags != 0:
        raise BackendProtocolError(
            "native retained Functional seed has incompatible metadata",
            code="NATIVE_PARSE_VERSION",
        )
    decoded_codepoints = reader.u64()
    canonical_rows = reader.u64()
    occurrences = reader.u64()
    rows = (reader.u64(), reader.u64(), reader.u64())
    metadata_iris = reader.u64()
    document = _RetainedFingerprintEvidenceV2(reader.u64(), reader.take(32))
    if document.preimage_byte_length == 0:
        raise BackendProtocolError(
            "native retained document fingerprint has an empty preimage",
            code="NATIVE_PARSE_MODEL",
        )

    def optional_iri() -> str | None:
        marker = reader.u8()
        if marker == 0:
            return None
        if marker != 1:
            raise BackendProtocolError(
                "native retained metadata has an invalid optional marker",
                code="NATIVE_PARSE_FRAMING",
            )
        return _read_text64_v2(reader, maximum=limits.max_iri_bytes)

    ontology_iri = optional_iri()
    version_iri = optional_iri()
    if version_iri is not None and ontology_iri is None:
        raise BackendProtocolError(
            "native retained metadata has a version IRI without an ontology IRI",
            code="NATIVE_PARSE_MODEL",
        )
    import_count = reader.u64()
    reader.require_count(import_count, 8)
    imports = tuple(
        _read_text64_v2(reader, maximum=limits.max_iri_bytes) for _ in range(import_count)
    )
    reader.finish()
    from pyowl_core.model import encode_varint

    import_rows = tuple(
        b"\x01\x02" + encode_varint(len(raw)) + raw
        for value in imports
        for raw in (value.encode("utf-8"),)
    )
    if import_rows != tuple(sorted(set(import_rows))):
        raise BackendProtocolError(
            "native retained imports are not canonical unique",
            code="NATIVE_PARSE_MODEL",
        )
    limits.enforce("max_annotations", rows[0])
    limits.enforce("max_axioms", rows[1])
    if canonical_rows < occurrences or metadata_iris != sum(
        (ontology_iri is not None, version_iri is not None, len(imports))
    ):
        raise BackendProtocolError(
            "native retained seed counters are internally inconsistent",
            code="NATIVE_PARSE_MODEL",
        )
    return _RetainedFunctionalSeedV2(
        decoded_codepoints,
        canonical_rows,
        occurrences,
        rows,
        metadata_iris,
        document,
        ontology_iri,
        version_iri,
        imports,
    )


def _decode_retained_rdfxml_seed_v2(
    encoded: bytes,
    limits: ParseLimits,
) -> _RetainedRdfXmlSeedV2:
    if (
        len(encoded) < 20
        or encoded[:8] != native._RETAINED_RDFXML_SEED_MAGIC_V2
        or int.from_bytes(encoded[8:10], "little") != 1
        or int.from_bytes(encoded[10:12], "little") != 0
    ):
        raise BackendProtocolError(
            "native retained RDF/XML seed has incompatible metadata",
            code="NATIVE_PARSE_VERSION",
        )
    total_triples = int.from_bytes(encoded[-8:], "little")
    limits.enforce("max_triples", total_triples)
    structural = _decode_retained_functional_seed_v2(
        native._RETAINED_FUNCTIONAL_SEED_MAGIC_V2 + encoded[8:-8],
        limits,
    )
    return _RetainedRdfXmlSeedV2(structural, total_triples)


def _decode_prepared_retained_publication_v2(
    encoded: bytes,
    *,
    collect_provenance: bool,
    preserve_source_map: bool,
    expect_rdf_report: bool = False,
    allow_partial_rdf_mapping: bool = False,
) -> _PreparedRetainedPublicationV2:
    reader = native._ResultReader(encoded)
    magic = reader.take(8)
    schema = _read_u16_v2(reader)
    flags = _read_u16_v2(reader)
    if (
        magic != native._RETAINED_FUNCTIONAL_PREPARED_MAGIC_V2
        or schema != 3
        or flags & 1 != int(expect_rdf_report)
        or flags & ~3
    ):
        raise BackendProtocolError(
            "native retained publication summary has incompatible metadata",
            code="NATIVE_PARSE_VERSION",
        )
    fingerprints = cast(
        tuple[
            _RetainedFingerprintEvidenceV2,
            _RetainedFingerprintEvidenceV2,
            _RetainedFingerprintEvidenceV2,
            _RetainedFingerprintEvidenceV2,
        ],
        tuple(_RetainedFingerprintEvidenceV2(reader.u64(), reader.take(32)) for _ in range(4)),
    )
    content = cast(
        tuple[bytes, bytes, bytes, bytes, bytes, bytes],
        tuple(reader.take(32) for _ in range(6)),
    )
    inventories = cast(
        tuple[
            _RetainedRecordInventoryV1,
            _RetainedRecordInventoryV1,
            _RetainedRecordInventoryV1,
            _RetainedRecordInventoryV1,
        ],
        tuple(
            _RetainedRecordInventoryV1(
                reader.u64(),
                reader.u64(),
                reader.u64(),
                reader.take(32),
            )
            for _ in range(4)
        ),
    )
    root_count = reader.u64()
    node_count = reader.u64()
    source_map_rows = reader.u64()
    source_prefix_rows = reader.u64()
    origin_rows = reader.u64()
    max_row = reader.u64()
    canonical_rows = reader.u64()
    canonical_bytes = reader.u64()
    fingerprint_temporary_bytes = reader.u64()
    origin_bytes = reader.u64()
    prepare_ns = reader.u64()
    rdf_report = None
    if expect_rdf_report:
        conformant_raw = reader.u8()
        if conformant_raw not in {0, 1}:
            raise BackendProtocolError(
                "native retained RDF report has an invalid conformance flag",
                code="NATIVE_RDF_REPORT",
            )
        rdf_report = _PreparedRetainedRdfReportV2(
            bool(conformant_raw),
            reader.u64(),
            reader.u64(),
            reader.u64(),
            reader.u64(),
            reader.u64(),
            reader.take(32),
            reader.u64(),
        )
    reader.finish()
    if any(item.preimage_byte_length == 0 for item in fingerprints) or max_row == 0:
        raise BackendProtocolError(
            "native retained publication summary has invalid fingerprint or row bounds",
            code="NATIVE_PARSE_MODEL",
        )
    if root_count != sum(item.count for item in inventories[:3]) or node_count < root_count:
        raise BackendProtocolError(
            "native retained publication summary has inconsistent structural counts",
            code="NATIVE_PARSE_MODEL",
        )
    if (not collect_provenance and (origin_rows != 0 or origin_bytes != 0)) or (
        origin_rows == 0 and origin_bytes != 0
    ):
        raise BackendProtocolError(
            "native retained publication summary has inconsistent provenance counters",
            code="NATIVE_PARSE_MODEL",
        )
    if not preserve_source_map and (source_map_rows != 0 or source_prefix_rows != 0):
        raise BackendProtocolError(
            "native retained publication summary has inconsistent source-map counters",
            code="NATIVE_PARSE_MODEL",
        )
    if rdf_report is not None:
        remaining = rdf_report.total_triples - rdf_report.consumed_triples
        conformant_shape = (
            rdf_report.conformant
            and remaining == 0
            and rdf_report.unconsumed_triple_count == 0
            and rdf_report.rule_count == 0
            and rdf_report.diagnostic_count == 0
            and rdf_report.retained_bytes == 17
        )
        partial_shape = (
            allow_partial_rdf_mapping
            and not rdf_report.conformant
            and remaining > 0
            and 0 < rdf_report.unconsumed_triple_count <= remaining
            and rdf_report.rule_count == 1
            and rdf_report.diagnostic_count == 0
            and rdf_report.retained_bytes > 17
        )
        if (
            rdf_report.consumed_triples > rdf_report.total_triples
            or not (conformant_shape or partial_shape)
            or len(rdf_report.digest) != 32
            or max_row < 17
        ):
            raise BackendProtocolError(
                "native retained RDF report summary is inconsistent",
                code="NATIVE_RDF_REPORT",
            )
    return _PreparedRetainedPublicationV2(
        fingerprints,
        content,
        inventories,
        root_count,
        node_count,
        source_map_rows,
        source_prefix_rows,
        origin_rows,
        max_row,
        canonical_rows,
        canonical_bytes,
        fingerprint_temporary_bytes,
        origin_bytes,
        prepare_ns / 1_000_000_000,
        bool(flags & 2),
        rdf_report,
    )


@dataclass(frozen=True, slots=True)
class _PreparedRetainedClosureDocumentV2:
    fingerprint: _RetainedFingerprintEvidenceV2
    raw_counts: tuple[int, int, int]
    effective_counts: tuple[int, int, int]
    source_map_rows: int
    source_prefix_rows: int
    raw_origin_rows: int
    effective_origin_rows: int
    rdf_report: _PreparedRetainedRdfReportV2 | None


@dataclass(frozen=True, slots=True)
class _PreparedRetainedClosureV2:
    fingerprints: tuple[
        _RetainedFingerprintEvidenceV2,
        _RetainedFingerprintEvidenceV2,
        _RetainedFingerprintEvidenceV2,
    ]
    content_digests: tuple[bytes, bytes, bytes, bytes, bytes, bytes]
    closure_counts: tuple[int, int, int]
    closure_origin_rows: int
    max_facade_row_bytes: int
    parser_summary_bytes_materialized: int
    canonical_rows_scanned: int
    structural_occurrence_rows_scanned: int
    metadata_iri_objects_materialized: int
    canonical_rows_encoded: int
    canonical_bytes_encoded: int
    fingerprint_temporary_bytes: int
    origin_bytes_retained: int
    prepare_seconds: float
    documents: tuple[_PreparedRetainedClosureDocumentV2, ...]


def _decode_prepared_retained_closure_v2(
    encoded: bytes,
    *,
    document_count: int,
    collect_provenance: bool,
    preserve_source_map: bool,
    allow_partial_rdf_mapping: bool,
    limits: ParseLimits,
) -> _PreparedRetainedClosureV2:
    reader = native._ResultReader(encoded)
    magic = reader.take(8)
    schema = _read_u16_v2(reader)
    flags = _read_u16_v2(reader)
    observed_documents = reader.u64()
    expected_flags = int(preserve_source_map) | (int(collect_provenance) << 1)
    if (
        magic != native._RETAINED_CLOSURE_PREPARED_MAGIC_V2
        or schema != 1
        or flags & ~7
        or flags & 3 != expected_flags
        or observed_documents != document_count
    ):
        raise BackendProtocolError(
            "native retained closure summary has incompatible metadata",
            code="NATIVE_PARSE_VERSION",
        )
    fingerprints = cast(
        tuple[
            _RetainedFingerprintEvidenceV2,
            _RetainedFingerprintEvidenceV2,
            _RetainedFingerprintEvidenceV2,
        ],
        tuple(_RetainedFingerprintEvidenceV2(reader.u64(), reader.take(32)) for _ in range(3)),
    )
    content = cast(
        tuple[bytes, bytes, bytes, bytes, bytes, bytes],
        tuple(reader.take(32) for _ in range(6)),
    )
    closure_counts = cast(tuple[int, int, int], tuple(reader.u64() for _ in range(3)))
    closure_origins = reader.u64()
    max_row = reader.u64()
    parser_summary_bytes_materialized = reader.u64()
    canonical_rows_scanned = reader.u64()
    structural_occurrence_rows_scanned = reader.u64()
    metadata_iri_objects_materialized = reader.u64()
    canonical_rows = reader.u64()
    canonical_bytes = reader.u64()
    fingerprint_temporary_bytes = reader.u64()
    origin_bytes = reader.u64()
    prepare_ns = reader.u64()
    documents: list[_PreparedRetainedClosureDocumentV2] = []
    rdf_count = 0
    for _ordinal in range(document_count):
        fingerprint = _RetainedFingerprintEvidenceV2(reader.u64(), reader.take(32))
        raw_counts = cast(tuple[int, int, int], tuple(reader.u64() for _ in range(3)))
        effective_counts = cast(tuple[int, int, int], tuple(reader.u64() for _ in range(3)))
        source_rows = reader.u64()
        source_prefixes = reader.u64()
        raw_origins = reader.u64()
        effective_origins = reader.u64()
        rdf_marker = reader.u8()
        rdf_report = None
        if rdf_marker == 1:
            rdf_count += 1
            conformant = reader.u8()
            if conformant not in {0, 1}:
                raise BackendProtocolError(
                    "native closure RDF report has an invalid conformance flag",
                    code="NATIVE_RDF_REPORT",
                )
            rdf_report = _PreparedRetainedRdfReportV2(
                bool(conformant),
                reader.u64(),
                reader.u64(),
                reader.u64(),
                reader.u64(),
                reader.u64(),
                reader.take(32),
                reader.u64(),
            )
        elif rdf_marker != 0:
            raise BackendProtocolError(
                "native closure RDF report has an invalid presence marker",
                code="NATIVE_RDF_REPORT",
            )
        documents.append(
            _PreparedRetainedClosureDocumentV2(
                fingerprint,
                raw_counts,
                effective_counts,
                source_rows,
                source_prefixes,
                raw_origins,
                effective_origins,
                rdf_report,
            )
        )
    reader.finish()
    if (
        any(item.preimage_byte_length == 0 for item in fingerprints)
        or any(document.fingerprint.preimage_byte_length == 0 for document in documents)
        or max_row == 0
        or parser_summary_bytes_materialized == 0
        or canonical_rows_scanned < structural_occurrence_rows_scanned
        or canonical_rows_scanned < metadata_iri_objects_materialized
        or (not collect_provenance and (closure_origins != 0 or origin_bytes != 0))
        or bool(flags & 4) != bool(rdf_count)
    ):
        raise BackendProtocolError(
            "native retained closure summary has inconsistent counters",
            code="NATIVE_PARSE_MODEL",
        )
    for document in documents:
        limits.enforce("max_annotations", document.raw_counts[0])
        limits.enforce("max_axioms", document.raw_counts[1])
        limits.enforce("max_annotations", document.effective_counts[0])
        limits.enforce("max_axioms", document.effective_counts[1])
        limits.enforce("max_source_map_entries", document.source_map_rows)
        limits.enforce("max_origin_entries", document.raw_origin_rows)
        limits.enforce("max_origin_entries", document.effective_origin_rows)
        if not preserve_source_map and (
            document.source_map_rows != 0 or document.source_prefix_rows != 0
        ):
            raise BackendProtocolError(
                "native retained closure source-map counters are inconsistent",
                code="NATIVE_PARSE_MODEL",
            )
        if not collect_provenance and (
            document.raw_origin_rows != 0 or document.effective_origin_rows != 0
        ):
            raise BackendProtocolError(
                "native retained closure origin counters are inconsistent",
                code="NATIVE_PARSE_MODEL",
            )
        if document.rdf_report is not None:
            _validate_prepared_closure_rdf_report_v2(
                document.rdf_report,
                allow_partial_rdf_mapping=allow_partial_rdf_mapping,
            )
    limits.enforce("max_origin_entries", closure_origins)
    return _PreparedRetainedClosureV2(
        fingerprints,
        content,
        closure_counts,
        closure_origins,
        max_row,
        parser_summary_bytes_materialized,
        canonical_rows_scanned,
        structural_occurrence_rows_scanned,
        metadata_iri_objects_materialized,
        canonical_rows,
        canonical_bytes,
        fingerprint_temporary_bytes,
        origin_bytes,
        prepare_ns / 1_000_000_000,
        tuple(documents),
    )


def _validate_prepared_closure_rdf_report_v2(
    report: _PreparedRetainedRdfReportV2,
    *,
    allow_partial_rdf_mapping: bool,
) -> None:
    remaining = report.total_triples - report.consumed_triples
    conformant_shape = (
        report.conformant
        and remaining == 0
        and report.unconsumed_triple_count == 0
        and report.rule_count == 0
        and report.diagnostic_count == 0
        and report.retained_bytes == 17
    )
    partial_shape = (
        allow_partial_rdf_mapping
        and not report.conformant
        and remaining > 0
        and 0 < report.unconsumed_triple_count <= remaining
        and report.rule_count == 1
        and report.diagnostic_count == 0
        and report.retained_bytes > 17
    )
    if (
        report.consumed_triples > report.total_triples
        or not (conformant_shape or partial_shape)
        or len(report.digest) != 32
    ):
        raise BackendProtocolError(
            "native retained closure RDF report summary is inconsistent",
            code="NATIVE_RDF_REPORT",
        )


def publish_retained_functional_snapshot_v2(
    summary: bytes,
    *,
    parsed_native_storage: object,
    phase_timings: tuple[tuple[str, float], ...],
    payload: SourcePayload,
    detection: FormatDetection,
    document_iri: IRI | None,
    media_type: str | None,
    options: LoadOptions,
    resolver: ImportResolver | None,
    cancellation_token: CancellationToken | None,
    load_started: float,
    root_parse_started: float,
) -> OntologySnapshot:
    """Publish one parser-owned Functional load from bounded native evidence."""

    seed = _decode_retained_functional_seed_v2(summary, options.limits)
    extension = native.require("parse-functional-v1")
    return _publish_retained_snapshot_v2(
        summary,
        seed=seed,
        rdf_total_triples=None,
        allow_partial_rdf_mapping=False,
        expected_format="functional",
        extension=extension,
        parsed_native_storage=parsed_native_storage,
        phase_timings=phase_timings,
        payload=payload,
        detection=detection,
        document_iri=document_iri,
        media_type=media_type,
        options=options,
        resolver=resolver,
        cancellation_token=cancellation_token,
        load_started=load_started,
        root_parse_started=root_parse_started,
    )


def publish_retained_rdfxml_snapshot_v2(
    summary: bytes,
    *,
    parsed_native_storage: object,
    phase_timings: tuple[tuple[str, float], ...],
    payload: SourcePayload,
    detection: FormatDetection,
    document_iri: IRI | None,
    media_type: str | None,
    options: LoadOptions,
    resolver: ImportResolver | None,
    cancellation_token: CancellationToken | None,
    load_started: float,
    root_parse_started: float,
    allow_partial_rdf_mapping: bool = False,
) -> OntologySnapshot:
    """Publish one privately selected RDF/XML retained-owner checkpoint."""

    if type(allow_partial_rdf_mapping) is not bool:
        raise TypeError("allow_partial_rdf_mapping must be bool")
    decoded = _decode_retained_rdfxml_seed_v2(summary, options.limits)
    runtime = native._runtime()
    extension = runtime.extension
    if not runtime.probe.available or extension is None:
        raise BackendProtocolError(
            "retained RDF/XML parser storage outlived its compatible extension",
            code="NATIVE_INGESTION_REGISTRATION",
        )
    return _publish_retained_snapshot_v2(
        summary,
        seed=decoded.structural,
        rdf_total_triples=decoded.total_triples,
        allow_partial_rdf_mapping=allow_partial_rdf_mapping,
        expected_format="rdfxml",
        extension=extension,
        parsed_native_storage=parsed_native_storage,
        phase_timings=phase_timings,
        payload=payload,
        detection=detection,
        document_iri=document_iri,
        media_type=media_type,
        options=options,
        resolver=resolver,
        cancellation_token=cancellation_token,
        load_started=load_started,
        root_parse_started=root_parse_started,
    )


def publish_retained_turtle_snapshot_v2(
    summary: bytes,
    *,
    parsed_native_storage: object,
    phase_timings: tuple[tuple[str, float], ...],
    payload: SourcePayload,
    detection: FormatDetection,
    document_iri: IRI | None,
    media_type: str | None,
    options: LoadOptions,
    resolver: ImportResolver | None,
    cancellation_token: CancellationToken | None,
    load_started: float,
    root_parse_started: float,
    allow_partial_rdf_mapping: bool = False,
) -> OntologySnapshot:
    """Publish one privately selected Turtle retained-owner checkpoint."""

    if type(allow_partial_rdf_mapping) is not bool:
        raise TypeError("allow_partial_rdf_mapping must be bool")
    decoded = _decode_retained_rdfxml_seed_v2(summary, options.limits)
    runtime = native._runtime()
    extension = runtime.extension
    if not runtime.probe.available or extension is None:
        raise BackendProtocolError(
            "retained Turtle parser storage outlived its compatible extension",
            code="NATIVE_INGESTION_REGISTRATION",
        )
    return _publish_retained_snapshot_v2(
        summary,
        seed=decoded.structural,
        rdf_total_triples=decoded.total_triples,
        allow_partial_rdf_mapping=allow_partial_rdf_mapping,
        expected_format="turtle",
        extension=extension,
        parsed_native_storage=parsed_native_storage,
        phase_timings=phase_timings,
        payload=payload,
        detection=detection,
        document_iri=document_iri,
        media_type=media_type,
        options=options,
        resolver=resolver,
        cancellation_token=cancellation_token,
        load_started=load_started,
        root_parse_started=root_parse_started,
    )


def publish_retained_owlxml_snapshot_v2(
    summary: bytes,
    *,
    parsed_native_storage: object,
    phase_timings: tuple[tuple[str, float], ...],
    payload: SourcePayload,
    detection: FormatDetection,
    document_iri: IRI | None,
    media_type: str | None,
    options: LoadOptions,
    resolver: ImportResolver | None,
    cancellation_token: CancellationToken | None,
    load_started: float,
    root_parse_started: float,
) -> OntologySnapshot:
    """Publish one privately selected OWL/XML retained-owner checkpoint."""

    seed = _decode_retained_functional_seed_v2(summary, options.limits)
    runtime = native._runtime()
    extension = runtime.extension
    if not runtime.probe.available or extension is None:
        raise BackendProtocolError(
            "retained OWL/XML parser storage outlived its compatible extension",
            code="NATIVE_INGESTION_REGISTRATION",
        )
    return _publish_retained_snapshot_v2(
        summary,
        seed=seed,
        rdf_total_triples=None,
        allow_partial_rdf_mapping=False,
        expected_format="owlxml",
        extension=extension,
        parsed_native_storage=parsed_native_storage,
        phase_timings=phase_timings,
        payload=payload,
        detection=detection,
        document_iri=document_iri,
        media_type=media_type,
        options=options,
        resolver=resolver,
        cancellation_token=cancellation_token,
        load_started=load_started,
        root_parse_started=root_parse_started,
    )


def _publish_retained_snapshot_v2(
    summary: bytes,
    *,
    seed: _RetainedFunctionalSeedV2,
    rdf_total_triples: int | None,
    allow_partial_rdf_mapping: bool,
    expected_format: str,
    extension: native._Extension,
    parsed_native_storage: object,
    phase_timings: tuple[tuple[str, float], ...],
    payload: SourcePayload,
    detection: FormatDetection,
    document_iri: IRI | None,
    media_type: str | None,
    options: LoadOptions,
    resolver: ImportResolver | None,
    cancellation_token: CancellationToken | None,
    load_started: float,
    root_parse_started: float,
) -> OntologySnapshot:
    """Publish one guarded parser-owned load from bounded native evidence."""

    if allow_partial_rdf_mapping and rdf_total_triples is None:
        raise BackendProtocolError(
            "partial RDF mapping cannot be selected for a non-RDF retained parser",
            code="NATIVE_RDF_REPORT",
        )

    from pyowl_core.backends.native_handoff import (
        NativeDocumentPublicationV1,
        NativeLoadReportPublicationV1,
        freeze_native_diagnostic_publication_v1,
        freeze_native_import_manifest_publication_v1,
        freeze_native_provenance_publication_v1,
    )
    from pyowl_core.backends.native_handoff_v2 import (
        NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2,
        NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2,
        NativeClosureFacadeCardinalitiesV2,
        NativeDiagnosticReferenceSidecarsV2,
        NativeDocumentFacadeCardinalitiesV2,
        NativeFacadeCardinalitySummaryV2,
        NativeSnapshotContentDigestsV2,
        _seal_native_snapshot_owner_v2,
        freeze_native_snapshot_publication_v2,
        native_diagnostic_reference_kinds_v2,
        native_snapshot_publication_attestation_v2,
    )
    from pyowl_core.config import BackendPreference, ImportPolicy, LoadOptions
    from pyowl_core.document import Fingerprint, OntologyID
    from pyowl_core.document.imports import (
        DocumentRecord,
        DocumentStatus,
        ImportEdge,
        ImportManifest,
        ImportStatus,
        _record_unresolved_without_resolver,
    )
    from pyowl_core.document.native_storage import (
        _NO_ANONYMOUS_SCOPES_SEAL_V2,
        _WIRE_STRUCTURAL_ALIAS_SEAL_V1,
        _NativeCommonContractFingerprintEvidenceV1,
        _NativeCommonContractRecordInventoryV1,
        _NativeCommonContractSummaryV1,
        _NativeIngestionCountersV2,
        ontology_snapshot_from_native_publication_v2,
    )
    from pyowl_core.document.provenance import DocumentProvenance
    from pyowl_core.exceptions import ModelError, UnresolvedImportWarning
    from pyowl_core.io.formats.detection import FormatDetection
    from pyowl_core.io.resolver import resolver_configuration_fingerprint
    from pyowl_core.io.source import SourcePayload
    from pyowl_core.model import IRI

    if not isinstance(options, LoadOptions):
        raise TypeError("options must be LoadOptions")
    if not isinstance(payload, SourcePayload) or not isinstance(detection, FormatDetection):
        raise TypeError("retained parser publication received invalid source metadata")
    if (
        options.backend not in {BackendPreference.AUTO, BackendPreference.NATIVE}
        or (
            options.preserve_source_map
            and expected_format not in {"functional", "rdfxml", "turtle", "owlxml"}
        )
        or options.validate_owl2_dl
        or detection.format.value != expected_format
    ):
        raise AssertionError("retained publication was invoked for an ineligible load")
    if seed.imports and (
        options.imports in {ImportPolicy.RESOLVE_LOCAL, ImportPolicy.RESOLVE_STRICT}
        or (options.imports is ImportPolicy.RECORD_UNRESOLVED and resolver is not None)
    ):
        raise AssertionError("retained publication cannot bypass resolver-backed imports")
    if cancellation_token is not None:
        cancellation_token.check()
    options.limits.enforce("max_documents", 1)
    options.limits.enforce("max_total_source_bytes", payload.byte_length)
    for _iri in seed.imports:
        options.limits.enforce("max_import_depth", 1)

    try:
        ontology_iri = None if seed.ontology_iri is None else IRI(seed.ontology_iri)
        version_iri = None if seed.version_iri is None else IRI(seed.version_iri)
        direct_imports = tuple(IRI(value) for value in seed.imports)
    except ModelError as error:
        raise BackendProtocolError(
            "native retained metadata contains an invalid IRI",
            code="NATIVE_PARSE_MODEL",
        ) from error
    ontology_id = OntologyID(ontology_iri, version_iri)
    document_fingerprint = Fingerprint("sha256", 1, seed.document_fingerprint.digest)
    document_key = _document_key_v2(
        ontology_iri,
        version_iri,
        document_fingerprint.digest,
    )
    provenance = DocumentProvenance(
        payload.source_sha256,
        payload.digest_kind,
        payload.byte_length,
        (
            payload.decoded_codepoint_length
            if payload.decoded_codepoint_length is not None
            else seed.decoded_codepoint_length
        ),
        document_iri,
        payload.locator,
        detection.format,
        detection.basis,
        media_type,
        parser="pyowl_core.backends.native",
        backend="native",
    )
    record = DocumentRecord(
        document_key,
        ontology_id,
        document_iri,
        payload.source_sha256,
        document_fingerprint,
        detection.format,
        DocumentStatus.ROOT,
    )
    edges: tuple[ImportEdge, ...]
    public_diagnostics: tuple[Diagnostic, ...]
    if options.imports is ImportPolicy.IGNORE:
        edges = tuple(ImportEdge(document_key, iri, ImportStatus.IGNORED) for iri in direct_imports)
        public_diagnostics = ()
        resolution_attempts = 0
    elif direct_imports:
        edges, public_diagnostics, resolution_attempts = _record_unresolved_without_resolver(
            document_key,
            document_iri,
            direct_imports,
            options,
        )
    else:
        edges = ()
        public_diagnostics = ()
        resolution_attempts = 0
    manifest = ImportManifest(
        options.imports,
        options.offline,
        resolver_configuration_fingerprint(resolver),
        (record,),
        edges,
    )

    prepare = getattr(extension, "_prepare_parsed_structural_snapshot_v2", None)
    if not callable(prepare):
        raise BackendProtocolError(
            "native parser-built storage has no publication preparation boundary",
            code="NATIVE_INGESTION_REGISTRATION",
        )
    with native._relay(extension, options.limits, cancellation_token) as cancel:
        prepared_encoded = native._call_parse_value(
            extension,
            lambda: prepare(
                parsed_native_storage,
                manifest.canonical_bytes(),
                document_key,
                options.collect_provenance,
                options.preserve_source_map,
                cancel,
            ),
        )
    if type(prepared_encoded) is not bytes:
        raise BackendProtocolError(
            "native retained publication preparation returned a non-bytes summary",
            code="NATIVE_RESULT_TYPE",
        )
    prepared = _decode_prepared_retained_publication_v2(
        prepared_encoded,
        collect_provenance=options.collect_provenance,
        preserve_source_map=options.preserve_source_map,
        expect_rdf_report=rdf_total_triples is not None,
        allow_partial_rdf_mapping=allow_partial_rdf_mapping,
    )
    options.limits.enforce("max_origin_entries", prepared.origin_rows_retained)
    options.limits.enforce("max_source_map_entries", prepared.source_map_rows_retained)
    if prepared.rdf_report is not None:
        options.limits.enforce("max_diagnostics", prepared.rdf_report.unconsumed_triple_count)
    if (
        options.preserve_source_map
        and prepared.source_map_rows_retained < seed.structural_occurrence_rows_scanned
    ) or (not options.preserve_source_map and prepared.source_map_rows_retained != 0):
        raise BackendProtocolError(
            "native retained source-map count diverges from parser metadata",
            code="NATIVE_PARSE_MODEL",
        )
    if prepared.fingerprints[0] != seed.document_fingerprint:
        raise BackendProtocolError(
            "native retained document fingerprint summaries diverge",
            code="NATIVE_PARSE_MODEL",
        )
    if rdf_total_triples is not None and (
        prepared.rdf_report is None or prepared.rdf_report.total_triples != rdf_total_triples
    ):
        raise BackendProtocolError(
            "native retained RDF report diverges from parser metadata",
            code="NATIVE_RDF_REPORT",
        )
    fingerprints = tuple(Fingerprint("sha256", 1, item.digest) for item in prepared.fingerprints)
    try:
        inventory_rows = tuple(
            _NativeCommonContractRecordInventoryV1(
                item.count,
                item.canonical_bytes,
                item.transcript_bytes,
                item.digest,
            )
            for item in prepared.record_inventories
        )
        common_contract_summary = _NativeCommonContractSummaryV1(
            schema=1,
            document_fingerprint=_NativeCommonContractFingerprintEvidenceV1(
                prepared.fingerprints[0].preimage_byte_length,
                prepared.fingerprints[0].digest,
            ),
            structural_fingerprint=_NativeCommonContractFingerprintEvidenceV1(
                prepared.fingerprints[1].preimage_byte_length,
                prepared.fingerprints[1].digest,
            ),
            logical_fingerprint=_NativeCommonContractFingerprintEvidenceV1(
                prepared.fingerprints[2].preimage_byte_length,
                prepared.fingerprints[2].digest,
            ),
            signature_fingerprint=_NativeCommonContractFingerprintEvidenceV1(
                prepared.fingerprints[3].preimage_byte_length,
                prepared.fingerprints[3].digest,
            ),
            ontology_annotations=inventory_rows[0],
            axioms=inventory_rows[1],
            extensions=inventory_rows[2],
            signature=inventory_rows[3],
            root_count=prepared.root_count,
            node_count=prepared.node_count,
        )
    except (IndexError, ValueError) as error:
        raise BackendProtocolError(
            "native retained common-contract summary is invalid",
            code="NATIVE_PARSE_MODEL",
        ) from error
    if tuple(item.count for item in inventory_rows[:3]) != seed.rows:
        raise BackendProtocolError(
            "native retained common-contract inventories diverge from parser metadata",
            code="NATIVE_PARSE_MODEL",
        )

    retained_rdf = prepared.rdf_report
    rdf_conformant = None if retained_rdf is None else retained_rdf.conformant
    rdf_digest = None if retained_rdf is None else retained_rdf.digest
    raw_origin_rows_retained = (
        seed.structural_occurrence_rows_scanned if options.collect_provenance else 0
    )

    documents = (
        NativeDocumentPublicationV1(
            document_key=document_key,
            ontology_id=ontology_id,
            document_iri=document_iri,
            direct_imports=direct_imports,
            provenance=freeze_native_provenance_publication_v1(provenance),
            document_fingerprint=fingerprints[0],
            diagnostics=(),
            ontology_annotation_count=seed.rows[0],
            axiom_count=seed.rows[1],
            extension_count=seed.rows[2],
            source_map_entry_count=prepared.source_map_rows_retained,
            origin_entry_count=raw_origin_rows_retained,
            rdf_mapping_conformant=rdf_conformant,
            rdf_mapping_report_sha256=rdf_digest,
        ),
    )
    timings = {
        "load_seconds": time.monotonic() - load_started,
        "root_parse_seconds": time.monotonic() - root_parse_started,
    }
    timings.update(phase_timings)
    timings["native_publication_prepare_seconds"] = prepared.prepare_seconds
    diagnostics = tuple(
        freeze_native_diagnostic_publication_v1(value) for value in public_diagnostics
    )
    report = NativeLoadReportPublicationV1(
        backend="native",
        api_version=(0, 1),
        model_schema=1,
        document_count=1,
        total_source_bytes=payload.byte_length,
        effective_axiom_count=seed.rows[1],
        resolution_attempts=resolution_attempts,
        acquisition_cache_hits=0,
        document_cache_hits=0,
        timings=tuple(sorted(timings.items(), key=lambda item: item[0].encode("utf-8"))),
        structural_fingerprint=fingerprints[1],
        logical_fingerprint=fingerprints[2],
        signature_fingerprint=fingerprints[3],
        owl2_dl_validated=False,
        owl2_dl_conforms=None,
        owl2_dl_report_sha256=None,
    )
    capability_bits = (
        7
        | (8 if options.preserve_source_map else 0)
        | (16 if options.collect_provenance else 0)
        | (32 if retained_rdf is not None else 0)
    )
    import_manifest = freeze_native_import_manifest_publication_v1(manifest)
    sidecars = NativeDiagnosticReferenceSidecarsV2(
        snapshot=tuple(
            native_diagnostic_reference_kinds_v2(
                document_reference=cast(IRI | str | None, value.document_iri),
                import_chain=cast(tuple[IRI | str, ...], value.import_chain),
            )
            for value in public_diagnostics
        ),
        documents=((),),
        import_edges=tuple(
            (
                None
                if edge.diagnostic is None
                else native_diagnostic_reference_kinds_v2(
                    document_reference=cast(
                        IRI | str | None,
                        edge.diagnostic.document_iri,
                    ),
                    import_chain=cast(
                        tuple[IRI | str, ...],
                        edge.diagnostic.import_chain,
                    ),
                )
            )
            for edge in manifest.edges
        ),
    )
    facade_summary = NativeFacadeCardinalitySummaryV2(
        documents=(
            NativeDocumentFacadeCardinalitiesV2(
                document_key=document_key,
                effective_annotation_count=seed.rows[0],
                effective_axiom_count=seed.rows[1],
                effective_extension_count=seed.rows[2],
                effective_origin_count=prepared.origin_rows_retained,
                raw_source_prefix_count=prepared.source_prefix_rows_retained,
                rdf_unconsumed_triple_count=(
                    0 if retained_rdf is None else retained_rdf.unconsumed_triple_count
                ),
                rdf_rule_count=0 if retained_rdf is None else retained_rdf.rule_count,
                rdf_diagnostic_count=(0 if retained_rdf is None else retained_rdf.diagnostic_count),
            ),
        ),
        closure=NativeClosureFacadeCardinalitiesV2(
            effective_annotation_count=seed.rows[0],
            effective_axiom_count=seed.rows[1],
            effective_extension_count=seed.rows[2],
            effective_origin_count=prepared.origin_rows_retained,
        ),
    )
    content = NativeSnapshotContentDigestsV2(
        root_table_sha256=prepared.content_digests[0],
        effective_root_table_sha256=prepared.content_digests[1],
        fingerprint_inputs_sha256=prepared.content_digests[2],
        source_manifest_sha256=prepared.content_digests[3],
        provenance_manifest_sha256=prepared.content_digests[4],
        effective_origin_manifest_sha256=prepared.content_digests[5],
    )
    attestation = native_snapshot_publication_attestation_v2(
        documents=documents,
        import_manifest=import_manifest,
        root_document_key=document_key,
        load_options=options,
        diagnostics=diagnostics,
        diagnostic_reference_sidecars=sidecars,
        facade_cardinality_summary=facade_summary,
        report=report,
        capability_bits=capability_bits,
        content_digests=content,
        max_facade_row_bytes=prepared.max_facade_row_bytes,
        owl2_dl_report_summary=None,
    )
    hook = getattr(extension, "_finalize_parsed_structural_snapshot_v2", None)
    if not callable(hook):
        raise BackendProtocolError(
            "native parser-built storage has no final publication boundary",
            code="NATIVE_INGESTION_REGISTRATION",
        )
    with native._relay(extension, options.limits, cancellation_token) as cancel:
        raw_owner = native._call(
            extension,
            lambda: hook(
                parsed_native_storage,
                prepared_encoded,
                attestation,
                cancel,
            ),
        )
    values: dict[str, object] = {
        "version": NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2,
        "ledger_sha256": NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2,
        "handle": _seal_native_snapshot_owner_v2(raw_owner),
        "documents": documents,
        "import_manifest": import_manifest,
        "root_document_key": document_key,
        "load_options": options,
        "diagnostics": diagnostics,
        "diagnostic_reference_sidecars": sidecars,
        "facade_cardinality_summary": facade_summary,
        "report": report,
        "capability_bits": capability_bits,
        "max_facade_row_bytes": prepared.max_facade_row_bytes,
        "owl2_dl_report_summary": None,
    }
    for field in fields(content):
        values[field.name] = getattr(content, field.name)
    publication = freeze_native_snapshot_publication_v2(values)
    ingestion_counters = _NativeIngestionCountersV2(
        parser_result_bytes_scanned=0,
        parser_summary_bytes_materialized=len(summary) + len(prepared_encoded),
        canonical_rows_scanned=seed.canonical_rows_scanned,
        structural_occurrence_rows_scanned=seed.structural_occurrence_rows_scanned,
        structural_root_rows_published=sum(seed.rows),
        eager_structural_objects_materialized=0,
        metadata_iri_objects_materialized=seed.metadata_iri_objects_materialized,
        provenance_occurrence_records_materialized=0,
        canonical_bytes_copied_to_python=0,
        fingerprint_preimage_bytes_materialized_in_python=0,
        native_publication_canonical_rows_encoded=prepared.canonical_rows_encoded,
        native_publication_canonical_bytes_encoded=prepared.canonical_bytes_encoded,
        native_fingerprint_temporary_bytes=prepared.fingerprint_temporary_bytes,
        native_origin_rows_retained=prepared.origin_rows_retained,
        native_origin_bytes_retained=prepared.origin_bytes_retained,
    )
    snapshot = ontology_snapshot_from_native_publication_v2(
        publication,
        _wire_structural_aliases=(
            None if prepared.scoped_roots else _WIRE_STRUCTURAL_ALIAS_SEAL_V1
        ),
        _ingestion_counters=ingestion_counters,
        _anonymous_scope_evidence=(None if prepared.scoped_roots else _NO_ANONYMOUS_SCOPES_SEAL_V2),
        _common_contract_summary=common_contract_summary,
    )
    for diagnostic in public_diagnostics:
        warnings.warn(diagnostic.message, UnresolvedImportWarning, stacklevel=3)
    return snapshot


def retain_native_snapshot_v2(
    snapshot: OntologySnapshot,
    *,
    cancellation_token: CancellationToken | None = None,
    parsed_native_storage: object | None = None,
) -> OntologySnapshot:
    """Promote one narrowly eligible native parse into the typed V2 owner.

    The bridge remains deliberately unadvertised while WP16's complete
    format/import/source-map matrix is unfinished. An ineligible load stays on
    its existing Python storage before owner publication; an eligible load
    selected explicitly or by the existing AUTO policy either publishes the
    retained owner or fails without fallback.
    """

    _closure_publication_checkpoint_v2(cancellation_token)

    from pyowl_core.config import BackendPreference, DocumentFormat, ImportPolicy

    formats = {document.provenance.format for document in snapshot.documents}
    functional_documents = formats == {DocumentFormat.FUNCTIONAL}
    rdf_documents = bool(formats) and formats <= {
        DocumentFormat.RDF_XML,
        DocumentFormat.TURTLE,
    }
    rdf_reports_eligible = rdf_documents and all(
        (report := document.rdf_mapping_report) is not None
        and report.conformant
        and not report.unconsumed
        and not report.rule_ids
        and not report.diagnostics
        for document in snapshot.documents
    )
    common_ineligible = (
        snapshot.load_options.backend not in {BackendPreference.AUTO, BackendPreference.NATIVE}
        or not snapshot.documents
        or (
            snapshot.load_options.preserve_source_map
            and len(snapshot.documents) == 1
            and not (rdf_reports_eligible and parsed_native_storage is not None)
        )
        or snapshot.load_options.validate_owl2_dl
        or not (functional_documents or rdf_reports_eligible)
        or any(
            document.provenance.backend != "native"
            or (functional_documents and document.rdf_mapping_report is not None)
            for document in snapshot.documents
        )
    )
    single_document = (
        len(snapshot.documents) == 1 and snapshot.load_options.imports is ImportPolicy.IGNORE
    )
    retained_closure = len(snapshot.documents) > 1
    retained_rdf_single = (
        len(snapshot.documents) == 1 and rdf_reports_eligible and parsed_native_storage is not None
    )
    if common_ineligible or not (single_document or retained_closure or retained_rdf_single):
        return snapshot
    if functional_documents:
        extension = native.require("parse-functional-v1")
    else:
        runtime = native._runtime()
        runtime_extension = runtime.extension
        if not runtime.probe.available or runtime_extension is None:
            return snapshot
        extension = runtime_extension
    if retained_closure or retained_rdf_single:
        parsed_native_storages = (
            parsed_native_storage
            if type(parsed_native_storage) is tuple
            and len(parsed_native_storage) == len(snapshot.documents)
            else ((parsed_native_storage,) if retained_rdf_single else None)
        )
        if parsed_native_storages is not None and functional_documents:
            from pyowl_core.backends.parser import _NativeBackendDriver

            if not _NativeBackendDriver().supports_retained_storage_fork():
                parsed_native_storages = None
        if rdf_documents and parsed_native_storages is None:
            return snapshot
        required_hooks = (
            (
                "_prepare_parsed_structural_closure_v2",
                "_finalize_parsed_structural_closure_v2",
            )
            if parsed_native_storages is not None
            else ("_retain_structural_snapshot_v2",)
        )
        if any(not callable(getattr(extension, name, None)) for name in required_hooks):
            raise BackendProtocolError(
                "native closure has no retained publication boundary",
                code="NATIVE_INGESTION_REGISTRATION",
            )
        return _publish_structural_closure_snapshot_v2(
            snapshot,
            extension,
            cancellation_token,
            parsed_native_storages,
        )
    if parsed_native_storage is None:
        if snapshot.load_options.backend is BackendPreference.AUTO:
            return snapshot
        if not snapshot.load_options.collect_provenance:
            return snapshot
        hook = getattr(extension, "_retain_structural_snapshot_v2", None)
        if not callable(hook):
            return snapshot
    elif not callable(getattr(extension, "_finalize_parsed_structural_snapshot_v2", None)):
        raise BackendProtocolError(
            "native parser-built storage has no final publication boundary",
            code="NATIVE_INGESTION_REGISTRATION",
        )
    return _publish_structural_snapshot_v2(
        snapshot,
        extension,
        cancellation_token,
        parsed_native_storage,
    )


def _publish_structural_snapshot_v2(
    snapshot: OntologySnapshot,
    extension: native._Extension,
    cancellation_token: CancellationToken | None,
    parsed_native_storage: object | None,
) -> OntologySnapshot:
    from dataclasses import fields

    from pyowl_core.backends.native_handoff import (
        NativeDocumentPublicationV1,
        NativeLoadReportPublicationV1,
        freeze_native_diagnostic_publication_v1,
        freeze_native_import_manifest_publication_v1,
        freeze_native_provenance_publication_v1,
    )
    from pyowl_core.backends.native_handoff_v2 import (
        NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2,
        NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2,
        NativeClosureFacadeCardinalitiesV2,
        NativeDiagnosticReferenceSidecarsV2,
        NativeDocumentFacadeCardinalitiesV2,
        NativeFacadeCardinalitySummaryV2,
        NativeFacadeCollectionV2,
        NativeFacadeScopeV2,
        NativeFingerprintEvidenceV2,
        NativeOriginRowV2,
        NativeSignatureKindV2,
        _seal_native_snapshot_owner_v2,
        encode_native_auxiliary_row_v2,
        freeze_native_snapshot_publication_v2,
        native_snapshot_content_digests_v2,
        native_snapshot_publication_attestation_v2,
    )
    from pyowl_core.document.fingerprint import (
        document_fingerprint_bytes,
        logical_fingerprint_bytes,
        signature_fingerprint_bytes,
        snapshot_structural_fingerprint_bytes,
    )
    from pyowl_core.document.native_storage import (
        _WIRE_STRUCTURAL_ALIAS_SEAL_V1,
        ontology_snapshot_from_native_publication_v2,
    )
    from pyowl_core.document.snapshot import AxiomScope
    from pyowl_core.model import canonical_bytes

    document = snapshot.root
    record = snapshot.import_manifest.documents[0]
    effective_rows = (
        tuple(
            canonical_bytes(value)
            for value in snapshot.ontology_annotations(
                scope=AxiomScope.DOCUMENT,
                document_key=record.document_key,
            )
        ),
        tuple(
            canonical_bytes(value)
            for value in snapshot.iter_axioms(
                scope=AxiomScope.DOCUMENT,
                document_key=record.document_key,
            )
        ),
        tuple(
            canonical_bytes(value)
            for value in snapshot.iter_extensions(
                scope=AxiomScope.DOCUMENT,
                document_key=record.document_key,
            )
        ),
    )
    if parsed_native_storage is None:
        raw_rows = (
            tuple(canonical_bytes(value) for value in document.ontology_annotations),
            tuple(canonical_bytes(value) for value in document.axioms),
            tuple(canonical_bytes(value) for value in document.extension_components),
        )
    else:
        # The parser orchestration discards parser-built storage before this
        # point whenever anonymous canonical re-scoping changes the roots.
        raw_rows = effective_rows

    raw_origin_index = document.origin_index
    if raw_origin_index is None:
        return snapshot
    encoded_origin_tables: list[tuple[bytes, ...]] = []
    for raw_owner_role, origin_index in (
        (True, raw_origin_index),
        (False, snapshot.origin_index),
    ):
        origin_items: list[tuple[bytes, bytes, int, bytes]] = []
        for digest, occurrences in origin_index.entries.items():
            for occurrence in occurrences:
                if not raw_owner_role and occurrence.document_key != record.document_key:
                    return snapshot
                origin = NativeOriginRowV2(
                    digest=digest,
                    # Raw document occurrences retain the provisional document
                    # fingerprint used by the Python parser. Effective rows use
                    # the authoritative import-manifest identity.
                    document_key=(
                        occurrence.document_key if raw_owner_role else record.document_key
                    ),
                    occurrence=occurrence.occurrence,
                    span=occurrence.span,
                )
                collection, encoded_row = encode_native_auxiliary_row_v2(
                    origin,
                    max_row_bytes=snapshot.load_options.limits.max_wire_bytes,
                )
                if collection is not NativeFacadeCollectionV2.ORIGIN_ENTRIES:
                    raise AssertionError(collection)
                origin_items.append(
                    (
                        digest,
                        occurrence.document_key.encode("utf-8"),
                        occurrence.occurrence,
                        encoded_row,
                    )
                )
        if raw_owner_role:
            # Stable digest sorting retains producer order and multiplicity
            # within a raw document's digest groups.
            origin_items.sort(key=lambda item: item[0])
        else:
            origin_items.sort()
        encoded_rows = tuple(item[3] for item in origin_items)
        if not raw_owner_role and len(set(encoded_rows)) != len(encoded_rows):
            return snapshot
        encoded_origin_tables.append(encoded_rows)
    raw_origin_rows, effective_origin_rows = encoded_origin_tables

    document_diagnostics = tuple(
        freeze_native_diagnostic_publication_v1(value) for value in document.diagnostics
    )
    documents = (
        NativeDocumentPublicationV1(
            document_key=record.document_key,
            ontology_id=document.ontology_id,
            document_iri=document.document_iri,
            direct_imports=document.direct_imports,
            provenance=freeze_native_provenance_publication_v1(document.provenance),
            document_fingerprint=document.document_fingerprint,
            diagnostics=document_diagnostics,
            ontology_annotation_count=len(raw_rows[0]),
            axiom_count=len(raw_rows[1]),
            extension_count=len(raw_rows[2]),
            source_map_entry_count=0,
            origin_entry_count=len(raw_origin_rows),
            rdf_mapping_conformant=None,
            rdf_mapping_report_sha256=None,
        ),
    )
    diagnostics = tuple(
        freeze_native_diagnostic_publication_v1(value) for value in snapshot.diagnostics
    )
    report = NativeLoadReportPublicationV1(
        backend="native",
        api_version=snapshot.report.api_version,
        model_schema=snapshot.report.model_schema,
        document_count=1,
        total_source_bytes=snapshot.report.total_source_bytes,
        effective_axiom_count=len(effective_rows[1]),
        resolution_attempts=snapshot.report.resolution_attempts,
        acquisition_cache_hits=snapshot.report.acquisition_cache_hits,
        document_cache_hits=snapshot.report.document_cache_hits,
        timings=tuple(
            sorted(snapshot.report.timings.items(), key=lambda item: item[0].encode("utf-8"))
        ),
        structural_fingerprint=snapshot.structural_fingerprint,
        logical_fingerprint=snapshot.logical_fingerprint,
        signature_fingerprint=snapshot.signature_fingerprint,
        owl2_dl_validated=False,
        owl2_dl_conforms=None,
        owl2_dl_report_sha256=None,
    )
    capability_bits = 7 | (16 if snapshot.load_options.collect_provenance else 0)
    import_manifest = freeze_native_import_manifest_publication_v1(snapshot.import_manifest)
    sidecars = NativeDiagnosticReferenceSidecarsV2(
        snapshot=tuple(_diagnostic_reference_kinds(value) for value in snapshot.diagnostics),
        documents=(tuple(_diagnostic_reference_kinds(value) for value in document.diagnostics),),
        import_edges=tuple(
            None if edge.diagnostic is None else _diagnostic_reference_kinds(edge.diagnostic)
            for edge in snapshot.import_manifest.edges
        ),
    )
    facade_summary = NativeFacadeCardinalitySummaryV2(
        documents=(
            NativeDocumentFacadeCardinalitiesV2(
                document_key=record.document_key,
                effective_annotation_count=len(effective_rows[0]),
                effective_axiom_count=len(effective_rows[1]),
                effective_extension_count=len(effective_rows[2]),
                effective_origin_count=len(effective_origin_rows),
                raw_source_prefix_count=0,
                rdf_unconsumed_triple_count=0,
                rdf_rule_count=0,
                rdf_diagnostic_count=0,
            ),
        ),
        closure=NativeClosureFacadeCardinalitiesV2(
            effective_annotation_count=len(effective_rows[0]),
            effective_axiom_count=len(effective_rows[1]),
            effective_extension_count=len(effective_rows[2]),
            effective_origin_count=len(effective_origin_rows),
        ),
    )
    collections = {
        (collection, scope, ordinal, NativeSignatureKindV2.ALL, True): effective_values
        for collection, effective_values in zip(
            (
                NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS,
                NativeFacadeCollectionV2.AXIOMS,
                NativeFacadeCollectionV2.EXTENSIONS,
            ),
            effective_rows,
            strict=True,
        )
        for scope, ordinal in (
            (NativeFacadeScopeV2.DOCUMENT, 0),
            (NativeFacadeScopeV2.CLOSURE, None),
        )
    }
    for scope, ordinal in (
        (NativeFacadeScopeV2.DOCUMENT, 0),
        (NativeFacadeScopeV2.CLOSURE, None),
    ):
        collections[
            (
                NativeFacadeCollectionV2.ORIGIN_ENTRIES,
                scope,
                ordinal,
                NativeSignatureKindV2.ALL,
                True,
            )
        ] = effective_origin_rows
    raw_collections = None
    if raw_rows != effective_rows or raw_origin_rows != effective_origin_rows:
        raw_collections = dict(collections)
        for collection, raw_values in zip(
            (
                NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS,
                NativeFacadeCollectionV2.AXIOMS,
                NativeFacadeCollectionV2.EXTENSIONS,
            ),
            raw_rows,
            strict=True,
        ):
            raw_collections[
                (
                    collection,
                    NativeFacadeScopeV2.DOCUMENT,
                    0,
                    NativeSignatureKindV2.ALL,
                    True,
                )
            ] = raw_values
        raw_collections[
            (
                NativeFacadeCollectionV2.ORIGIN_ENTRIES,
                NativeFacadeScopeV2.DOCUMENT,
                0,
                NativeSignatureKindV2.ALL,
                True,
            )
        ] = raw_origin_rows
    preimages = (
        document_fingerprint_bytes(document),
        snapshot_structural_fingerprint_bytes(
            snapshot.import_manifest,
            (
                (
                    record.document_key,
                    snapshot.ontology_annotations(
                        scope=AxiomScope.DOCUMENT,
                        document_key=record.document_key,
                    ),
                    tuple(
                        snapshot.iter_axioms(
                            scope=AxiomScope.DOCUMENT,
                            document_key=record.document_key,
                        )
                    ),
                    tuple(
                        snapshot.iter_extensions(
                            scope=AxiomScope.DOCUMENT,
                            document_key=record.document_key,
                        )
                    ),
                ),
            ),
        ),
        logical_fingerprint_bytes(
            tuple(snapshot.iter_axioms()),
            tuple(snapshot.iter_extensions()),
        ),
        signature_fingerprint_bytes(snapshot.signature(), include_builtins=True),
    )
    fingerprints = (
        documents[0].document_fingerprint,
        report.structural_fingerprint,
        report.logical_fingerprint,
        report.signature_fingerprint,
    )
    evidence = tuple(
        NativeFingerprintEvidenceV2(
            tag=tag,
            document_key=record.document_key if tag == 1 else None,
            preimage_byte_length=len(preimage),
            fingerprint_schema=fingerprint.schema,
            digest=hashlib.sha256(preimage).digest(),
        )
        for tag, preimage, fingerprint in zip((1, 2, 3, 4), preimages, fingerprints, strict=True)
    )
    max_facade_row_bytes = max(
        (
            1,
            *(len(row) for roots in effective_rows for row in roots),
            *(len(row) for roots in raw_rows for row in roots),
            *(len(row) for row in effective_origin_rows),
            *(len(row) for row in raw_origin_rows),
        )
    )
    content = native_snapshot_content_digests_v2(
        documents=documents,
        report=report,
        root_document_key=record.document_key,
        load_options=snapshot.load_options,
        capability_bits=capability_bits,
        collections=collections,
        fingerprint_evidence=evidence,
        fingerprint_preimages=preimages,
        owl2_dl_report_summary=None,
        facade_cardinality_summary=facade_summary,
        raw_document_collections=raw_collections,
    )
    attestation = native_snapshot_publication_attestation_v2(
        documents=documents,
        import_manifest=import_manifest,
        root_document_key=record.document_key,
        load_options=snapshot.load_options,
        diagnostics=diagnostics,
        diagnostic_reference_sidecars=sidecars,
        facade_cardinality_summary=facade_summary,
        report=report,
        capability_bits=capability_bits,
        content_digests=content,
        max_facade_row_bytes=max_facade_row_bytes,
        owl2_dl_report_summary=None,
    )
    with native._relay(extension, snapshot.load_options.limits, cancellation_token) as cancel:
        selected_extension = cast(_RetainedStructuralExtension, extension)
        if parsed_native_storage is None:
            config = native._encode_config(
                snapshot.load_options.limits,
                cancellation_token,
                verify=False,
            )
            raw_owner = native._call(
                extension,
                lambda: selected_extension._retain_structural_snapshot_v2(
                    (raw_rows,),
                    raw_origin_rows,
                    attestation,
                    config,
                    cancel,
                    effective_documents=(effective_rows,) if raw_collections is not None else None,
                    effective_origins=(
                        effective_origin_rows if raw_collections is not None else None
                    ),
                ),
            )
        else:
            raw_owner = native._call(
                extension,
                lambda: selected_extension._finalize_parsed_structural_snapshot_v2(
                    parsed_native_storage,
                    effective_origin_rows if snapshot.load_options.collect_provenance else None,
                    attestation,
                    cancel,
                ),
            )
    values: dict[str, object] = {
        "version": NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2,
        "ledger_sha256": NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2,
        "handle": _seal_native_snapshot_owner_v2(raw_owner),
        "documents": documents,
        "import_manifest": import_manifest,
        "root_document_key": record.document_key,
        "load_options": snapshot.load_options,
        "diagnostics": diagnostics,
        "diagnostic_reference_sidecars": sidecars,
        "facade_cardinality_summary": facade_summary,
        "report": report,
        "capability_bits": capability_bits,
        "max_facade_row_bytes": max_facade_row_bytes,
        "owl2_dl_report_summary": None,
    }
    for field in fields(content):
        values[field.name] = getattr(content, field.name)
    publication = freeze_native_snapshot_publication_v2(values)
    return ontology_snapshot_from_native_publication_v2(
        publication,
        _wire_structural_aliases=(
            _WIRE_STRUCTURAL_ALIAS_SEAL_V1 if raw_collections is None else None
        ),
    )


def _publish_parsed_structural_closure_snapshot_v2(
    snapshot: OntologySnapshot,
    extension: native._Extension,
    cancellation_token: CancellationToken | None,
    parsed_native_storages: tuple[object, ...],
) -> OntologySnapshot:
    """Publish parser owners from bounded Rust-prepared closure evidence."""

    from dataclasses import fields

    from pyowl_core.backends.native_handoff import (
        NativeDocumentPublicationV1,
        NativeLoadReportPublicationV1,
        freeze_native_diagnostic_publication_v1,
        freeze_native_import_manifest_publication_v1,
        freeze_native_provenance_publication_v1,
    )
    from pyowl_core.backends.native_handoff_v2 import (
        NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2,
        NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2,
        NativeClosureFacadeCardinalitiesV2,
        NativeDiagnosticReferenceSidecarsV2,
        NativeDocumentFacadeCardinalitiesV2,
        NativeFacadeCardinalitySummaryV2,
        NativeSnapshotContentDigestsV2,
        _seal_native_snapshot_owner_v2,
        freeze_native_snapshot_publication_v2,
        native_snapshot_publication_attestation_v2,
    )
    from pyowl_core.document import Fingerprint
    from pyowl_core.document.native_storage import (
        _NativeIngestionCountersV2,
        ontology_snapshot_from_native_publication_v2,
    )

    records = snapshot.import_manifest.documents
    if (
        type(parsed_native_storages) is not tuple
        or len(parsed_native_storages) != len(records)
        or len(snapshot.documents) != len(records)
    ):
        raise BackendProtocolError(
            "native parser-owner closure is not document-aligned",
            code="NATIVE_PARSE_MODEL",
        )
    selected_extension = cast(_RetainedStructuralExtension, extension)
    prepare = getattr(selected_extension, "_prepare_parsed_structural_closure_v2", None)
    finalize = getattr(selected_extension, "_finalize_parsed_structural_closure_v2", None)
    prepared_type = getattr(extension, "_NativePreparedStructuralClosureV2", None)
    if not callable(prepare) or not callable(finalize) or not isinstance(prepared_type, type):
        raise BackendProtocolError(
            "native parser-owner closure lacks its bounded publication seam",
            code="NATIVE_INGESTION_REGISTRATION",
        )
    topology = tuple((ordinal,) for ordinal in range(len(records)))
    closure_ordinals = tuple(range(len(records)))
    config = native._encode_config(
        snapshot.load_options.limits,
        cancellation_token,
        verify=False,
    )
    with native._relay(extension, snapshot.load_options.limits, cancellation_token) as cancel:
        result = native._call(
            extension,
            lambda: prepare(
                parsed_native_storages,
                snapshot.import_manifest.canonical_bytes(),
                snapshot.root_document_key,
                tuple(record.document_key for record in records),
                snapshot.load_options.collect_provenance,
                snapshot.load_options.preserve_source_map,
                config,
                cancel,
                effective_document_ordinals=topology,
                closure_document_ordinals=closure_ordinals,
                anonymous_scope_targets=_snapshot_anonymous_scope_candidates_v2(snapshot),
            ),
        )
    if (
        type(result) is not tuple
        or len(result) != 2
        or type(result[0]) is not bytes
        or type(result[1]) is not prepared_type
    ):
        raise BackendProtocolError(
            "native closure preparation returned invalid result members",
            code="NATIVE_RESULT_TYPE",
        )
    prepared_encoded = result[0]
    prepared_owner = result[1]
    prepared = _decode_prepared_retained_closure_v2(
        prepared_encoded,
        document_count=len(records),
        collect_provenance=snapshot.load_options.collect_provenance,
        preserve_source_map=snapshot.load_options.preserve_source_map,
        allow_partial_rdf_mapping=False,
        limits=snapshot.load_options.limits,
    )
    document_fingerprints = tuple(
        Fingerprint("sha256", 1, document.fingerprint.digest) for document in prepared.documents
    )
    global_fingerprints = tuple(
        Fingerprint("sha256", 1, evidence.digest) for evidence in prepared.fingerprints
    )
    if (
        document_fingerprints
        != tuple(document.document_fingerprint for document in snapshot.documents)
        or document_fingerprints != tuple(record.document_fingerprint for record in records)
        or global_fingerprints
        != (
            snapshot.structural_fingerprint,
            snapshot.logical_fingerprint,
            snapshot.signature_fingerprint,
        )
    ):
        raise BackendProtocolError(
            "native prepared closure fingerprints diverge from resolver metadata",
            code="NATIVE_FINGERPRINT_INPUTS",
        )

    document_diagnostics = tuple(
        tuple(freeze_native_diagnostic_publication_v1(value) for value in document.diagnostics)
        for document in snapshot.documents
    )
    documents: list[NativeDocumentPublicationV1] = []
    for record, document, diagnostics, selected in zip(
        records,
        snapshot.documents,
        document_diagnostics,
        prepared.documents,
        strict=True,
    ):
        mapping = document.rdf_mapping_report
        retained_rdf = selected.rdf_report
        if retained_rdf is None:
            if mapping is not None:
                raise BackendProtocolError(
                    "native closure omitted an RDF mapping report",
                    code="NATIVE_RDF_REPORT",
                )
        elif (
            mapping is None
            or mapping.conformant is not retained_rdf.conformant
            or mapping.consumed_triples != retained_rdf.consumed_triples
            or mapping.total_triples != retained_rdf.total_triples
            or len(mapping.unconsumed) != retained_rdf.unconsumed_triple_count
            or len(mapping.rule_ids) != retained_rdf.rule_count
            or len(mapping.diagnostics) != retained_rdf.diagnostic_count
        ):
            raise BackendProtocolError(
                "native closure RDF mapping evidence diverges from resolver metadata",
                code="NATIVE_RDF_REPORT",
            )
        documents.append(
            NativeDocumentPublicationV1(
                document_key=record.document_key,
                ontology_id=document.ontology_id,
                document_iri=document.document_iri,
                direct_imports=document.direct_imports,
                provenance=freeze_native_provenance_publication_v1(document.provenance),
                document_fingerprint=document_fingerprints[len(documents)],
                diagnostics=diagnostics,
                ontology_annotation_count=selected.raw_counts[0],
                axiom_count=selected.raw_counts[1],
                extension_count=selected.raw_counts[2],
                source_map_entry_count=selected.source_map_rows,
                origin_entry_count=selected.raw_origin_rows,
                rdf_mapping_conformant=(None if retained_rdf is None else retained_rdf.conformant),
                rdf_mapping_report_sha256=(None if retained_rdf is None else retained_rdf.digest),
            )
        )
    frozen_documents = tuple(documents)
    diagnostics = tuple(
        freeze_native_diagnostic_publication_v1(value) for value in snapshot.diagnostics
    )
    timings = dict(snapshot.report.timings)
    timings["native_closure_publication_prepare_seconds"] = prepared.prepare_seconds
    report = NativeLoadReportPublicationV1(
        backend="native",
        api_version=snapshot.report.api_version,
        model_schema=snapshot.report.model_schema,
        document_count=len(frozen_documents),
        total_source_bytes=snapshot.report.total_source_bytes,
        effective_axiom_count=prepared.closure_counts[1],
        resolution_attempts=snapshot.report.resolution_attempts,
        acquisition_cache_hits=snapshot.report.acquisition_cache_hits,
        document_cache_hits=snapshot.report.document_cache_hits,
        timings=tuple(sorted(timings.items(), key=lambda item: item[0].encode("utf-8"))),
        structural_fingerprint=global_fingerprints[0],
        logical_fingerprint=global_fingerprints[1],
        signature_fingerprint=global_fingerprints[2],
        owl2_dl_validated=False,
        owl2_dl_conforms=None,
        owl2_dl_report_sha256=None,
    )
    capability_bits = (
        7
        | (8 if snapshot.load_options.preserve_source_map else 0)
        | (16 if snapshot.load_options.collect_provenance else 0)
        | (32 if any(document.rdf_report is not None for document in prepared.documents) else 0)
    )
    import_manifest = freeze_native_import_manifest_publication_v1(snapshot.import_manifest)
    sidecars = NativeDiagnosticReferenceSidecarsV2(
        snapshot=tuple(_diagnostic_reference_kinds(value) for value in snapshot.diagnostics),
        documents=tuple(
            tuple(_diagnostic_reference_kinds(value) for value in document.diagnostics)
            for document in snapshot.documents
        ),
        import_edges=tuple(
            None if edge.diagnostic is None else _diagnostic_reference_kinds(edge.diagnostic)
            for edge in snapshot.import_manifest.edges
        ),
    )
    facade_summary = NativeFacadeCardinalitySummaryV2(
        documents=tuple(
            NativeDocumentFacadeCardinalitiesV2(
                document_key=record.document_key,
                effective_annotation_count=selected.effective_counts[0],
                effective_axiom_count=selected.effective_counts[1],
                effective_extension_count=selected.effective_counts[2],
                effective_origin_count=selected.effective_origin_rows,
                raw_source_prefix_count=selected.source_prefix_rows,
                rdf_unconsumed_triple_count=(
                    0
                    if selected.rdf_report is None
                    else selected.rdf_report.unconsumed_triple_count
                ),
                rdf_rule_count=(
                    0 if selected.rdf_report is None else selected.rdf_report.rule_count
                ),
                rdf_diagnostic_count=(
                    0 if selected.rdf_report is None else selected.rdf_report.diagnostic_count
                ),
            )
            for record, selected in zip(records, prepared.documents, strict=True)
        ),
        closure=NativeClosureFacadeCardinalitiesV2(
            effective_annotation_count=prepared.closure_counts[0],
            effective_axiom_count=prepared.closure_counts[1],
            effective_extension_count=prepared.closure_counts[2],
            effective_origin_count=prepared.closure_origin_rows,
        ),
    )
    content = NativeSnapshotContentDigestsV2(
        root_table_sha256=prepared.content_digests[0],
        effective_root_table_sha256=prepared.content_digests[1],
        fingerprint_inputs_sha256=prepared.content_digests[2],
        source_manifest_sha256=prepared.content_digests[3],
        provenance_manifest_sha256=prepared.content_digests[4],
        effective_origin_manifest_sha256=prepared.content_digests[5],
    )
    attestation = native_snapshot_publication_attestation_v2(
        documents=frozen_documents,
        import_manifest=import_manifest,
        root_document_key=snapshot.root_document_key,
        load_options=snapshot.load_options,
        diagnostics=diagnostics,
        diagnostic_reference_sidecars=sidecars,
        facade_cardinality_summary=facade_summary,
        report=report,
        capability_bits=capability_bits,
        content_digests=content,
        max_facade_row_bytes=prepared.max_facade_row_bytes,
        owl2_dl_report_summary=None,
    )
    with native._relay(extension, snapshot.load_options.limits, cancellation_token) as cancel:
        raw_owner = native._call(
            extension,
            lambda: finalize(
                parsed_native_storages,
                prepared_owner,
                prepared_encoded,
                attestation,
                cancel,
            ),
        )
    values: dict[str, object] = {
        "version": NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2,
        "ledger_sha256": NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2,
        "handle": _seal_native_snapshot_owner_v2(raw_owner),
        "documents": frozen_documents,
        "import_manifest": import_manifest,
        "root_document_key": snapshot.root_document_key,
        "load_options": snapshot.load_options,
        "diagnostics": diagnostics,
        "diagnostic_reference_sidecars": sidecars,
        "facade_cardinality_summary": facade_summary,
        "report": report,
        "capability_bits": capability_bits,
        "max_facade_row_bytes": prepared.max_facade_row_bytes,
        "owl2_dl_report_summary": None,
    }
    for field in fields(content):
        values[field.name] = getattr(content, field.name)
    publication = freeze_native_snapshot_publication_v2(values)
    ingestion_counters = _NativeIngestionCountersV2(
        parser_result_bytes_scanned=0,
        parser_summary_bytes_materialized=(
            prepared.parser_summary_bytes_materialized + len(prepared_encoded)
        ),
        canonical_rows_scanned=prepared.canonical_rows_scanned,
        structural_occurrence_rows_scanned=prepared.structural_occurrence_rows_scanned,
        structural_root_rows_published=sum(prepared.closure_counts),
        eager_structural_objects_materialized=0,
        metadata_iri_objects_materialized=prepared.metadata_iri_objects_materialized,
        provenance_occurrence_records_materialized=0,
        canonical_bytes_copied_to_python=0,
        fingerprint_preimage_bytes_materialized_in_python=0,
        native_publication_canonical_rows_encoded=prepared.canonical_rows_encoded,
        native_publication_canonical_bytes_encoded=prepared.canonical_bytes_encoded,
        native_fingerprint_temporary_bytes=prepared.fingerprint_temporary_bytes,
        native_origin_rows_retained=prepared.closure_origin_rows,
        native_origin_bytes_retained=prepared.origin_bytes_retained,
    )
    return ontology_snapshot_from_native_publication_v2(
        publication,
        _wire_structural_aliases=None,
        _ingestion_counters=ingestion_counters,
        _anonymous_scope_evidence=None,
        _common_contract_summary=None,
    )


def _publish_structural_closure_snapshot_v2(
    snapshot: OntologySnapshot,
    extension: native._Extension,
    cancellation_token: CancellationToken | None,
    parsed_native_storages: tuple[object, ...] | None = None,
) -> OntologySnapshot:
    """Publish a resolver-built structural closure through one typed arena."""

    from dataclasses import fields

    from pyowl_core.backends.native_handoff import (
        NativeDocumentPublicationV1,
        NativeLoadReportPublicationV1,
        freeze_native_diagnostic_publication_v1,
        freeze_native_import_manifest_publication_v1,
        freeze_native_provenance_publication_v1,
    )
    from pyowl_core.backends.native_handoff_v2 import (
        NATIVE_RDF_MAPPING_REPORT_DOMAIN_V2,
        NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2,
        NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2,
        NativeClosureFacadeCardinalitiesV2,
        NativeDiagnosticReferenceSidecarsV2,
        NativeDocumentFacadeCardinalitiesV2,
        NativeFacadeCardinalitySummaryV2,
        NativeFacadeCollectionV2,
        NativeFacadeScopeV2,
        NativeFingerprintEvidenceV2,
        NativeOriginRowV2,
        NativeRDFReportHeaderRowV2,
        NativeSignatureKindV2,
        NativeSourceMapRowV2,
        NativeSourcePrefixRowV2,
        _seal_native_snapshot_owner_v2,
        encode_native_auxiliary_row_v2,
        freeze_native_snapshot_publication_v2,
        native_snapshot_content_digests_v2,
        native_snapshot_publication_attestation_v2,
    )
    from pyowl_core.document.fingerprint import (
        document_fingerprint_bytes,
        logical_fingerprint_bytes,
        signature_fingerprint_bytes,
        snapshot_structural_fingerprint_bytes,
    )
    from pyowl_core.document.native_storage import (
        ontology_snapshot_from_native_publication_v2,
    )
    from pyowl_core.document.snapshot import AxiomScope

    if not snapshot.documents or (len(snapshot.documents) == 1 and parsed_native_storages is None):
        raise AssertionError("retained closure publication received an ineligible snapshot")
    records = snapshot.import_manifest.documents
    if len(records) != len(snapshot.documents):
        raise AssertionError("retained closure records are not aligned")
    _closure_publication_checkpoint_v2(cancellation_token)
    if parsed_native_storages is not None:
        return _publish_parsed_structural_closure_snapshot_v2(
            snapshot,
            extension,
            cancellation_token,
            parsed_native_storages,
        )

    raw_documents = tuple(
        (
            _closure_canonical_rows_v2(document.ontology_annotations, cancellation_token),
            _closure_canonical_rows_v2(document.axioms, cancellation_token),
            _closure_canonical_rows_v2(document.extension_components, cancellation_token),
        )
        for document in snapshot.documents
    )
    source_map_documents: tuple[tuple[tuple[bytes, ...], tuple[bytes, ...]], ...] = ()
    if snapshot.load_options.preserve_source_map:
        encoded_source_maps: list[tuple[tuple[bytes, ...], tuple[bytes, ...]]] = []
        for document in snapshot.documents:
            _closure_publication_checkpoint_v2(cancellation_token)
            if document.source_map is None:
                return snapshot
            entry_items: list[tuple[bytes, bytes]] = []
            for digest, source_occurrences in document.source_map.entries.items():
                for source_occurrence in source_occurrences:
                    _closure_publication_checkpoint_v2(cancellation_token)
                    collection, encoded = encode_native_auxiliary_row_v2(
                        NativeSourceMapRowV2(
                            digest=digest,
                            occurrence=source_occurrence.occurrence,
                            span=source_occurrence.span,
                            lexical=tuple(
                                sorted(
                                    source_occurrence.lexical.items(),
                                    key=lambda item: item[0].encode("utf-8"),
                                )
                            ),
                        ),
                        max_row_bytes=snapshot.load_options.limits.max_wire_bytes,
                    )
                    if collection is not NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES:
                        raise AssertionError(collection)
                    entry_items.append((digest, encoded))
                    _closure_publication_checkpoint_v2(cancellation_token)
            entry_items.sort(key=lambda item: item[0])
            prefix_items: list[tuple[bytes, bytes]] = []
            for prefix, iri in document.source_map.prefixes.items():
                _closure_publication_checkpoint_v2(cancellation_token)
                collection, encoded = encode_native_auxiliary_row_v2(
                    NativeSourcePrefixRowV2(prefix=prefix, iri=iri),
                    max_row_bytes=snapshot.load_options.limits.max_wire_bytes,
                )
                if collection is not NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES:
                    raise AssertionError(collection)
                prefix_items.append((prefix.encode("utf-8"), encoded))
                _closure_publication_checkpoint_v2(cancellation_token)
            prefix_items.sort(key=lambda item: item[0])
            encoded_source_maps.append(
                (
                    tuple(item[1] for item in entry_items),
                    tuple(item[1] for item in prefix_items),
                )
            )
        source_map_documents = tuple(encoded_source_maps)
    effective_documents = tuple(
        (
            _closure_canonical_rows_v2(
                snapshot.ontology_annotations(
                    scope=AxiomScope.DOCUMENT,
                    document_key=record.document_key,
                ),
                cancellation_token,
            ),
            _closure_canonical_rows_v2(
                snapshot.iter_axioms(
                    scope=AxiomScope.DOCUMENT,
                    document_key=record.document_key,
                ),
                cancellation_token,
            ),
            _closure_canonical_rows_v2(
                snapshot.iter_extensions(
                    scope=AxiomScope.DOCUMENT,
                    document_key=record.document_key,
                ),
                cancellation_token,
            ),
        )
        for record in records
    )
    closure_rows = (
        _closure_canonical_rows_v2(snapshot.ontology_annotations(), cancellation_token),
        _closure_canonical_rows_v2(snapshot.iter_axioms(), cancellation_token),
        _closure_canonical_rows_v2(snapshot.iter_extensions(), cancellation_token),
    )
    raw_origin_documents: tuple[tuple[bytes, ...], ...] = ()
    effective_origin_documents: tuple[tuple[bytes, ...], ...] = ()
    closure_origin_rows: tuple[bytes, ...] = ()
    if snapshot.load_options.collect_provenance:
        raw_tables: list[tuple[bytes, ...]] = []
        for record, document in zip(records, snapshot.documents, strict=True):
            _closure_publication_checkpoint_v2(cancellation_token)
            if document.origin_index is None:
                return snapshot
            raw_items: list[tuple[bytes, bytes]] = []
            for digest, occurrences in document.origin_index.entries.items():
                for occurrence in occurrences:
                    _closure_publication_checkpoint_v2(cancellation_token)
                    origin = NativeOriginRowV2(
                        digest=digest,
                        document_key=record.document_key,
                        occurrence=occurrence.occurrence,
                        span=occurrence.span,
                    )
                    collection, encoded = encode_native_auxiliary_row_v2(
                        origin,
                        max_row_bytes=snapshot.load_options.limits.max_wire_bytes,
                    )
                    if collection is not NativeFacadeCollectionV2.ORIGIN_ENTRIES:
                        raise AssertionError(collection)
                    raw_items.append((digest, encoded))
                    _closure_publication_checkpoint_v2(cancellation_token)
            raw_items.sort(key=lambda item: item[0])
            raw_tables.append(tuple(item[1] for item in raw_items))
        raw_origin_documents = tuple(raw_tables)

        ordinals_by_key = {record.document_key: ordinal for ordinal, record in enumerate(records)}
        effective_items: list[list[tuple[bytes, bytes, int, bytes]]] = [[] for _record in records]
        for digest, occurrences in snapshot.origin_index.entries.items():
            for occurrence in occurrences:
                _closure_publication_checkpoint_v2(cancellation_token)
                ordinal = ordinals_by_key.get(occurrence.document_key)
                if ordinal is None:
                    return snapshot
                origin = NativeOriginRowV2(
                    digest=digest,
                    document_key=occurrence.document_key,
                    occurrence=occurrence.occurrence,
                    span=occurrence.span,
                )
                collection, encoded = encode_native_auxiliary_row_v2(
                    origin,
                    max_row_bytes=snapshot.load_options.limits.max_wire_bytes,
                )
                if collection is not NativeFacadeCollectionV2.ORIGIN_ENTRIES:
                    raise AssertionError(collection)
                effective_items[ordinal].append(
                    (
                        digest,
                        occurrence.document_key.encode("utf-8"),
                        occurrence.occurrence,
                        encoded,
                    )
                )
                _closure_publication_checkpoint_v2(cancellation_token)
        for items in effective_items:
            items.sort()
            if len({item[3] for item in items}) != len(items):
                return snapshot
        effective_origin_documents = tuple(
            tuple(item[3] for item in items) for items in effective_items
        )
        closure_items = sorted(item for items in effective_items for item in items)
        closure_origin_rows = tuple(item[3] for item in closure_items)
        if len(set(closure_origin_rows)) != len(closure_origin_rows):
            return snapshot

    rdf_report_documents: tuple[
        tuple[bytes, tuple[bytes, ...], tuple[bytes, ...], tuple[bytes, ...]] | None,
        ...,
    ] = (None,) * len(records)
    rdf_report_digests: tuple[bytes | None, ...] = (None,) * len(records)
    if all(document.provenance.format.value == "rdfxml" for document in snapshot.documents):
        encoded_reports: list[
            tuple[bytes, tuple[bytes, ...], tuple[bytes, ...], tuple[bytes, ...]]
        ] = []
        report_digests: list[bytes] = []
        for record, document in zip(records, snapshot.documents, strict=True):
            _closure_publication_checkpoint_v2(cancellation_token)
            mapping_report = document.rdf_mapping_report
            if (
                mapping_report is None
                or not mapping_report.conformant
                or mapping_report.unconsumed
                or mapping_report.rule_ids
                or mapping_report.diagnostics
            ):
                return snapshot
            collection, header = encode_native_auxiliary_row_v2(
                NativeRDFReportHeaderRowV2(
                    conformant=True,
                    consumed_triples=mapping_report.consumed_triples,
                    total_triples=mapping_report.total_triples,
                ),
                max_row_bytes=snapshot.load_options.limits.max_wire_bytes,
            )
            if collection is not NativeFacadeCollectionV2.RDF_REPORT_HEADER:
                raise AssertionError(collection)
            encoded_reports.append((header, (), (), ()))
            document_key = record.document_key.encode("utf-8")
            body = (
                len(document_key).to_bytes(8, "little")
                + document_key
                + len(header).to_bytes(8, "little")
                + header
                + (0).to_bytes(8, "little") * 3
            )
            report_digests.append(
                hashlib.sha256(
                    NATIVE_RDF_MAPPING_REPORT_DOMAIN_V2.encode("ascii") + b"\0" + body
                ).digest()
            )
            _closure_publication_checkpoint_v2(cancellation_token)
        rdf_report_documents = tuple(encoded_reports)
        rdf_report_digests = tuple(report_digests)

    document_diagnostics = tuple(
        tuple(freeze_native_diagnostic_publication_v1(value) for value in document.diagnostics)
        for document in snapshot.documents
    )
    _closure_publication_checkpoint_v2(cancellation_token)
    documents = tuple(
        NativeDocumentPublicationV1(
            document_key=record.document_key,
            ontology_id=document.ontology_id,
            document_iri=document.document_iri,
            direct_imports=document.direct_imports,
            provenance=freeze_native_provenance_publication_v1(document.provenance),
            document_fingerprint=document.document_fingerprint,
            diagnostics=diagnostics,
            ontology_annotation_count=len(raw_rows[0]),
            axiom_count=len(raw_rows[1]),
            extension_count=len(raw_rows[2]),
            source_map_entry_count=len(source_rows[0]),
            origin_entry_count=len(raw_origins),
            rdf_mapping_conformant=(None if rdf_report is None else True),
            rdf_mapping_report_sha256=rdf_digest,
        )
        for (
            record,
            document,
            diagnostics,
            raw_rows,
            raw_origins,
            source_rows,
            rdf_report,
            rdf_digest,
        ) in zip(
            records,
            snapshot.documents,
            document_diagnostics,
            raw_documents,
            (
                raw_origin_documents
                if snapshot.load_options.collect_provenance
                else ((),) * len(records)
            ),
            (
                source_map_documents
                if snapshot.load_options.preserve_source_map
                else (((), ()),) * len(records)
            ),
            rdf_report_documents,
            rdf_report_digests,
            strict=True,
        )
    )
    diagnostics = tuple(
        freeze_native_diagnostic_publication_v1(value) for value in snapshot.diagnostics
    )
    _closure_publication_checkpoint_v2(cancellation_token)
    report = NativeLoadReportPublicationV1(
        backend="native",
        api_version=snapshot.report.api_version,
        model_schema=snapshot.report.model_schema,
        document_count=len(documents),
        total_source_bytes=snapshot.report.total_source_bytes,
        effective_axiom_count=len(closure_rows[1]),
        resolution_attempts=snapshot.report.resolution_attempts,
        acquisition_cache_hits=snapshot.report.acquisition_cache_hits,
        document_cache_hits=snapshot.report.document_cache_hits,
        timings=tuple(
            sorted(snapshot.report.timings.items(), key=lambda item: item[0].encode("utf-8"))
        ),
        structural_fingerprint=snapshot.structural_fingerprint,
        logical_fingerprint=snapshot.logical_fingerprint,
        signature_fingerprint=snapshot.signature_fingerprint,
        owl2_dl_validated=False,
        owl2_dl_conforms=None,
        owl2_dl_report_sha256=None,
    )
    capability_bits = (
        7
        | (8 if snapshot.load_options.preserve_source_map else 0)
        | (16 if snapshot.load_options.collect_provenance else 0)
        | (32 if any(rdf_rows is not None for rdf_rows in rdf_report_documents) else 0)
    )
    import_manifest = freeze_native_import_manifest_publication_v1(snapshot.import_manifest)
    sidecars = NativeDiagnosticReferenceSidecarsV2(
        snapshot=tuple(_diagnostic_reference_kinds(value) for value in snapshot.diagnostics),
        documents=tuple(
            tuple(_diagnostic_reference_kinds(value) for value in document.diagnostics)
            for document in snapshot.documents
        ),
        import_edges=tuple(
            None if edge.diagnostic is None else _diagnostic_reference_kinds(edge.diagnostic)
            for edge in snapshot.import_manifest.edges
        ),
    )
    facade_summary = NativeFacadeCardinalitySummaryV2(
        documents=tuple(
            NativeDocumentFacadeCardinalitiesV2(
                document_key=record.document_key,
                effective_annotation_count=len(rows[0]),
                effective_axiom_count=len(rows[1]),
                effective_extension_count=len(rows[2]),
                effective_origin_count=len(origin_rows),
                raw_source_prefix_count=len(source_rows[1]),
                rdf_unconsumed_triple_count=0,
                rdf_rule_count=0,
                rdf_diagnostic_count=0,
            )
            for record, rows, origin_rows, source_rows in zip(
                records,
                effective_documents,
                (
                    effective_origin_documents
                    if snapshot.load_options.collect_provenance
                    else ((),) * len(records)
                ),
                (
                    source_map_documents
                    if snapshot.load_options.preserve_source_map
                    else (((), ()),) * len(records)
                ),
                strict=True,
            )
        ),
        closure=NativeClosureFacadeCardinalitiesV2(
            effective_annotation_count=len(closure_rows[0]),
            effective_axiom_count=len(closure_rows[1]),
            effective_extension_count=len(closure_rows[2]),
            effective_origin_count=len(closure_origin_rows),
        ),
    )
    collections: dict[
        tuple[
            NativeFacadeCollectionV2,
            NativeFacadeScopeV2,
            int | None,
            NativeSignatureKindV2,
            bool,
        ],
        tuple[bytes, ...],
    ] = {
        (
            collection,
            NativeFacadeScopeV2.DOCUMENT,
            ordinal,
            NativeSignatureKindV2.ALL,
            True,
        ): values
        for ordinal, rows in enumerate(effective_documents)
        for collection, values in zip(
            (
                NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS,
                NativeFacadeCollectionV2.AXIOMS,
                NativeFacadeCollectionV2.EXTENSIONS,
            ),
            rows,
            strict=True,
        )
    }
    for collection, values in zip(
        (
            NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS,
            NativeFacadeCollectionV2.AXIOMS,
            NativeFacadeCollectionV2.EXTENSIONS,
        ),
        closure_rows,
        strict=True,
    ):
        collections[
            (
                collection,
                NativeFacadeScopeV2.CLOSURE,
                None,
                NativeSignatureKindV2.ALL,
                True,
            )
        ] = values
    if snapshot.load_options.collect_provenance:
        for ordinal, values in enumerate(effective_origin_documents):
            collections[
                (
                    NativeFacadeCollectionV2.ORIGIN_ENTRIES,
                    NativeFacadeScopeV2.DOCUMENT,
                    ordinal,
                    NativeSignatureKindV2.ALL,
                    True,
                )
            ] = values
        collections[
            (
                NativeFacadeCollectionV2.ORIGIN_ENTRIES,
                NativeFacadeScopeV2.CLOSURE,
                None,
                NativeSignatureKindV2.ALL,
                True,
            )
        ] = closure_origin_rows
    if snapshot.load_options.preserve_source_map:
        for ordinal, (entries, prefixes) in enumerate(source_map_documents):
            collections[
                (
                    NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES,
                    NativeFacadeScopeV2.DOCUMENT,
                    ordinal,
                    NativeSignatureKindV2.ALL,
                    True,
                )
            ] = entries
            collections[
                (
                    NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES,
                    NativeFacadeScopeV2.DOCUMENT,
                    ordinal,
                    NativeSignatureKindV2.ALL,
                    True,
                )
            ] = prefixes
    for ordinal, rdf_rows in enumerate(rdf_report_documents):
        if rdf_rows is None:
            continue
        collections[
            (
                NativeFacadeCollectionV2.RDF_REPORT_HEADER,
                NativeFacadeScopeV2.DOCUMENT,
                ordinal,
                NativeSignatureKindV2.ALL,
                True,
            )
        ] = (rdf_rows[0],)
    raw_collections = None
    if raw_documents != effective_documents or raw_origin_documents != effective_origin_documents:
        raw_collections = dict(collections)
        for ordinal, rows in enumerate(raw_documents):
            for collection, values in zip(
                (
                    NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS,
                    NativeFacadeCollectionV2.AXIOMS,
                    NativeFacadeCollectionV2.EXTENSIONS,
                ),
                rows,
                strict=True,
            ):
                raw_collections[
                    (
                        collection,
                        NativeFacadeScopeV2.DOCUMENT,
                        ordinal,
                        NativeSignatureKindV2.ALL,
                        True,
                    )
                ] = values
        if snapshot.load_options.collect_provenance:
            for ordinal, values in enumerate(raw_origin_documents):
                raw_collections[
                    (
                        NativeFacadeCollectionV2.ORIGIN_ENTRIES,
                        NativeFacadeScopeV2.DOCUMENT,
                        ordinal,
                        NativeSignatureKindV2.ALL,
                        True,
                    )
                ] = values

    structural_documents = tuple(
        (
            record.document_key,
            snapshot.ontology_annotations(
                scope=AxiomScope.DOCUMENT,
                document_key=record.document_key,
            ),
            tuple(
                snapshot.iter_axioms(
                    scope=AxiomScope.DOCUMENT,
                    document_key=record.document_key,
                )
            ),
            tuple(
                snapshot.iter_extensions(
                    scope=AxiomScope.DOCUMENT,
                    document_key=record.document_key,
                )
            ),
        )
        for record in records
    )
    _closure_publication_checkpoint_v2(cancellation_token)
    document_preimages: list[bytes] = []
    for document in snapshot.documents:
        document_preimages.append(document_fingerprint_bytes(document))
        _closure_publication_checkpoint_v2(cancellation_token)
    structural_preimage = snapshot_structural_fingerprint_bytes(
        snapshot.import_manifest,
        structural_documents,
    )
    _closure_publication_checkpoint_v2(cancellation_token)
    logical_preimage = logical_fingerprint_bytes(
        tuple(snapshot.iter_axioms()),
        tuple(snapshot.iter_extensions()),
    )
    _closure_publication_checkpoint_v2(cancellation_token)
    signature_preimage = signature_fingerprint_bytes(
        snapshot.signature(),
        include_builtins=True,
    )
    _closure_publication_checkpoint_v2(cancellation_token)
    preimages = (
        *document_preimages,
        structural_preimage,
        logical_preimage,
        signature_preimage,
    )
    fingerprints = (
        *(document.document_fingerprint for document in snapshot.documents),
        report.structural_fingerprint,
        report.logical_fingerprint,
        report.signature_fingerprint,
    )
    tags = (*((1,) * len(documents)), 2, 3, 4)
    evidence = tuple(
        NativeFingerprintEvidenceV2(
            tag=tag,
            document_key=(documents[ordinal].document_key if tag == 1 else None),
            preimage_byte_length=len(preimage),
            fingerprint_schema=fingerprint.schema,
            digest=hashlib.sha256(preimage).digest(),
        )
        for ordinal, (tag, preimage, fingerprint) in enumerate(
            zip(tags, preimages, fingerprints, strict=True)
        )
    )
    max_facade_row_bytes = max(
        (
            1,
            *(len(row) for document in effective_documents for rows in document for row in rows),
            *(len(row) for document in raw_documents for rows in document for row in rows),
            *(len(row) for rows in closure_rows for row in rows),
            *(len(row) for rows in effective_origin_documents for row in rows),
            *(len(row) for rows in raw_origin_documents for row in rows),
            *(len(row) for row in closure_origin_rows),
            *(
                len(row)
                for entries, prefixes in source_map_documents
                for rows in (entries, prefixes)
                for row in rows
            ),
            *(len(rdf_rows[0]) for rdf_rows in rdf_report_documents if rdf_rows is not None),
        )
    )
    content = native_snapshot_content_digests_v2(
        documents=documents,
        report=report,
        root_document_key=snapshot.root_document_key,
        load_options=snapshot.load_options,
        capability_bits=capability_bits,
        collections=collections,
        fingerprint_evidence=evidence,
        fingerprint_preimages=preimages,
        owl2_dl_report_summary=None,
        facade_cardinality_summary=facade_summary,
        raw_document_collections=raw_collections,
    )
    _closure_publication_checkpoint_v2(cancellation_token)
    attestation = native_snapshot_publication_attestation_v2(
        documents=documents,
        import_manifest=import_manifest,
        root_document_key=snapshot.root_document_key,
        load_options=snapshot.load_options,
        diagnostics=diagnostics,
        diagnostic_reference_sidecars=sidecars,
        facade_cardinality_summary=facade_summary,
        report=report,
        capability_bits=capability_bits,
        content_digests=content,
        max_facade_row_bytes=max_facade_row_bytes,
        owl2_dl_report_summary=None,
    )
    _closure_publication_checkpoint_v2(cancellation_token)
    topology = tuple((ordinal,) for ordinal in range(len(documents)))
    closure_ordinals = tuple(range(len(documents)))
    with native._relay(extension, snapshot.load_options.limits, cancellation_token) as cancel:
        selected_extension = cast(_RetainedStructuralExtension, extension)
        config = native._encode_config(
            snapshot.load_options.limits,
            cancellation_token,
            verify=False,
        )
        origins = (
            (
                raw_origin_documents
                if raw_origin_documents != effective_origin_documents
                else effective_origin_documents
            )
            if snapshot.load_options.collect_provenance
            else None
        )
        source_maps = source_map_documents if snapshot.load_options.preserve_source_map else None
        effective_origins = (
            effective_origin_documents
            if raw_origin_documents != effective_origin_documents
            else None
        )
        if parsed_native_storages is None:
            raw_owner = native._call(
                extension,
                lambda: selected_extension._retain_structural_snapshot_v2(
                    raw_documents,
                    origins,
                    attestation,
                    config,
                    cancel,
                    source_maps=source_maps,
                    effective_documents=(
                        effective_documents if raw_documents != effective_documents else None
                    ),
                    effective_origins=effective_origins,
                    effective_document_ordinals=topology,
                    closure_document_ordinals=closure_ordinals,
                ),
            )
        else:
            raw_owner = native._call(
                extension,
                lambda: selected_extension._merge_parsed_structural_snapshot_v2(
                    parsed_native_storages,
                    origins,
                    attestation,
                    config,
                    cancel,
                    source_maps=source_maps,
                    rdf_reports=(
                        rdf_report_documents
                        if any(rdf_rows is not None for rdf_rows in rdf_report_documents)
                        else None
                    ),
                    effective_origins=effective_origins,
                    effective_document_ordinals=topology,
                    closure_document_ordinals=closure_ordinals,
                    anonymous_scope_targets=_snapshot_anonymous_scope_targets_v2(
                        snapshot,
                        raw_documents,
                        effective_documents,
                    ),
                ),
            )
    publication_values: dict[str, object] = {
        "version": NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2,
        "ledger_sha256": NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2,
        "handle": _seal_native_snapshot_owner_v2(raw_owner),
        "documents": documents,
        "import_manifest": import_manifest,
        "root_document_key": snapshot.root_document_key,
        "load_options": snapshot.load_options,
        "diagnostics": diagnostics,
        "diagnostic_reference_sidecars": sidecars,
        "facade_cardinality_summary": facade_summary,
        "report": report,
        "capability_bits": capability_bits,
        "max_facade_row_bytes": max_facade_row_bytes,
        "owl2_dl_report_summary": None,
    }
    for field in fields(content):
        publication_values[field.name] = getattr(content, field.name)
    publication = freeze_native_snapshot_publication_v2(publication_values)
    return ontology_snapshot_from_native_publication_v2(
        publication,
        _wire_structural_aliases=None,
    )


def _diagnostic_reference_kinds(
    value: Diagnostic,
) -> NativeDiagnosticReferenceKindsV2:
    from pyowl_core.backends.native_handoff_v2 import (
        native_diagnostic_reference_kinds_v2,
    )
    from pyowl_core.model import IRI

    return native_diagnostic_reference_kinds_v2(
        document_reference=cast(IRI | str | None, value.document_iri),
        import_chain=cast(tuple[IRI | str, ...], value.import_chain),
    )


__all__ = ["NativeIngestionExtension", "require_ingestion_binding"]
