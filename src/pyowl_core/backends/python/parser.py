"""Complete one-document pure-Python parser orchestration."""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pyowl_core.cancellation import CancellationToken
from pyowl_core.config import BackendPreference, DocumentFormat, ImportPolicy, LoadOptions
from pyowl_core.document import OntologyDocument
from pyowl_core.document.document import freeze_document_anonymous, provisional_label
from pyowl_core.document.provenance import (
    DocumentProvenance,
    OriginIndexBuilder,
    SourceMapBuilder,
)
from pyowl_core.exceptions import (
    OntologySyntaxError,
    OptionConflictError,
    PyOWLCoreError,
)
from pyowl_core.io.formats.common import ParseContext, ParsedOntology
from pyowl_core.io.formats.detection import FormatDetection, coerce_format, detect_format
from pyowl_core.io.formats.functional import FunctionalLexer, parse_functional
from pyowl_core.io.formats.owlxml import parse_owlxml
from pyowl_core.io.formats.rdfxml import parse_rdfxml
from pyowl_core.io.formats.turtle import TurtleLexer, parse_turtle
from pyowl_core.io.source import DocumentSource, SourcePayload, acquire_source
from pyowl_core.limits import ParseLimits
from pyowl_core.model import (
    IRI,
    AnonymousIndividual,
    Literal,
    StructuralNode,
    structural_digest,
    walk,
)

_NATIVE_AUTO_MIN_SOURCE_BYTES = 256 * 1024

if TYPE_CHECKING:
    from pyowl_core.document.snapshot import OntologySnapshot
    from pyowl_core.io.resolver import ImportResolver


@dataclass(frozen=True, slots=True)
class _ParsedPayloadResult:
    ontology: ParsedOntology | None
    native_storage: object | None = None
    phase_timings: tuple[tuple[str, float], ...] = ()
    native_encoded: bytes | None = None
    native_summary: bytes | None = None


@dataclass(frozen=True, slots=True)
class _ParsedDocumentResult:
    document: OntologyDocument | None
    native_storage: object | None = None
    phase_timings: tuple[tuple[str, float], ...] = ()
    snapshot: OntologySnapshot | None = None


class _BackendDriver(Protocol):
    """Backend-aware operations injected outside the pure Python package."""

    def select(
        self,
        preference: BackendPreference,
        format: DocumentFormat,
    ) -> str: ...

    def parse_functional(
        self,
        data: bytes,
        *,
        limits: ParseLimits,
        cancellation_token: CancellationToken | None,
        allow_swrl: bool,
        retain_native_storage: bool,
        collect_provenance: bool,
        preserve_source_map: bool,
        record_unresolved: bool,
        require_empty_imports: bool,
        materialize_document: bool,
    ) -> _ParsedPayloadResult: ...

    def parse_rdfxml(
        self,
        data: bytes,
        *,
        document_iri: IRI | None,
        limits: ParseLimits,
        cancellation_token: CancellationToken | None,
        allow_partial_rdf_mapping: bool,
        allow_swrl: bool,
        retain_native_storage: bool,
        collect_provenance: bool,
        preserve_source_map: bool,
        require_empty_imports: bool,
    ) -> _ParsedPayloadResult: ...

    def publish_retained_functional(
        self,
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
    ) -> OntologySnapshot: ...

    def publish_retained_rdfxml(
        self,
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
        allow_partial_rdf_mapping: bool,
    ) -> OntologySnapshot: ...

    def decode_functional(
        self,
        encoded: bytes,
        limits: ParseLimits,
    ) -> ParsedOntology: ...


class PythonParser:
    """Reusable stateless parser facade with explicit expert controls."""

    __slots__ = ()

    def parse(
        self,
        source: DocumentSource,
        *,
        format: DocumentFormat | str | None = None,
        document_iri: IRI | str | None = None,
        options: LoadOptions | None = None,
        media_type: str | None = None,
        allow_partial_rdf_mapping: bool = False,
        allow_swrl: bool = False,
        cancellation_token: CancellationToken | None = None,
    ) -> OntologyDocument:
        parse_started = time.monotonic()
        result = self._parse(
            source,
            format=format,
            document_iri=document_iri,
            options=options,
            media_type=media_type,
            allow_partial_rdf_mapping=allow_partial_rdf_mapping,
            allow_swrl=allow_swrl,
            cancellation_token=cancellation_token,
            retain_native_storage=False,
            publish_native_document=True,
            retained_load_started=parse_started,
            retained_root_parse_started=parse_started,
            backend_driver=self._backend_driver(),
        )
        if result.document is None:
            raise AssertionError("single-document parsing did not publish a document")
        return result.document

    def _backend_driver(self) -> _BackendDriver | None:
        return None

    def _parse(
        self,
        source: DocumentSource,
        *,
        format: DocumentFormat | str | None = None,
        document_iri: IRI | str | None = None,
        options: LoadOptions | None = None,
        media_type: str | None = None,
        allow_partial_rdf_mapping: bool = False,
        allow_swrl: bool = False,
        cancellation_token: CancellationToken | None = None,
        retain_native_storage: bool,
        publish_native_document: bool,
        materialize_native_document: bool = False,
        retained_resolver: ImportResolver | None = None,
        retained_load_started: float | None = None,
        retained_root_parse_started: float | None = None,
        backend_driver: _BackendDriver | None = None,
    ) -> _ParsedDocumentResult:
        selected_options = LoadOptions() if options is None else options
        if not isinstance(selected_options, LoadOptions):
            raise TypeError("options must be LoadOptions or None")
        explicit = coerce_format(format)
        if (
            explicit is not None
            and selected_options.format is not None
            and explicit is not selected_options.format
        ):
            raise OptionConflictError(
                "format argument conflicts with LoadOptions.format",
                code="FORMAT_OPTION_CONFLICT",
            )
        forced = explicit or selected_options.format
        iri = _coerce_iri(document_iri)
        preselected_backend: str | None = None
        if (
            backend_driver is not None
            and selected_options.backend is BackendPreference.NATIVE
            and forced is not None
        ):
            # An explicitly formatted forced-native request has enough
            # information to prove capability before opening or reading the
            # source. This keeps unsupported formats outside acquisition and
            # prevents a private/incomplete parser from becoming a fallback.
            preselected_backend = backend_driver.select(selected_options.backend, forced)
        payload = acquire_source(
            source,
            format=forced,
            document_iri=iri,
            limits=selected_options.limits,
            cancellation_token=cancellation_token,
        )
        effective_iri = iri
        if effective_iri is None and payload.locator is not None:
            effective_iri = IRI(Path(payload.locator).absolute().as_uri())
        detection = detect_format(
            payload.data,
            explicit=forced,
            media_type=media_type,
            extension=payload.extension,
        )
        selected_backend = preselected_backend or "python"
        if (
            backend_driver is not None
            and preselected_backend is None
            and selected_options.backend is not BackendPreference.PYTHON
            and not (
                selected_options.backend is BackendPreference.AUTO
                and detection.format is DocumentFormat.FUNCTIONAL
                and len(payload.data) < _NATIVE_AUTO_MIN_SOURCE_BYTES
            )
        ):
            selected_backend = backend_driver.select(
                selected_options.backend,
                detection.format,
            )
        publish_rdfxml_document = (
            publish_native_document
            and detection.format is DocumentFormat.RDF_XML
            and selected_backend == "native"
        )
        retain_payload_storage = retain_native_storage or publish_rdfxml_document
        parsed_result = _parse_payload(
            payload.data,
            detection.format,
            limits=selected_options.limits,
            document_iri=effective_iri,
            cancellation_token=cancellation_token,
            allow_partial_rdf_mapping=allow_partial_rdf_mapping,
            allow_swrl=allow_swrl,
            backend=selected_backend,
            retain_native_storage=retain_payload_storage,
            collect_provenance=selected_options.collect_provenance,
            preserve_source_map=selected_options.preserve_source_map,
            record_unresolved=(selected_options.imports is ImportPolicy.RECORD_UNRESOLVED),
            require_empty_imports=(
                not publish_rdfxml_document
                and (
                    selected_options.imports
                    in {ImportPolicy.RESOLVE_LOCAL, ImportPolicy.RESOLVE_STRICT}
                    or (
                        selected_options.imports is ImportPolicy.RECORD_UNRESOLVED
                        and retained_resolver is not None
                    )
                )
            ),
            materialize_native_document=materialize_native_document,
            backend_driver=backend_driver,
        )
        if parsed_result.native_summary is not None:
            if (
                not retain_payload_storage
                or parsed_result.native_storage is None
                or retained_load_started is None
                or retained_root_parse_started is None
                or backend_driver is None
            ):
                raise AssertionError("retained native result has no publication context")
            publication_options = (
                replace(selected_options, imports=ImportPolicy.IGNORE)
                if publish_rdfxml_document
                else selected_options
            )
            if detection.format is DocumentFormat.FUNCTIONAL:
                snapshot = backend_driver.publish_retained_functional(
                    parsed_result.native_summary,
                    parsed_native_storage=parsed_result.native_storage,
                    phase_timings=parsed_result.phase_timings,
                    payload=payload,
                    detection=detection,
                    document_iri=effective_iri,
                    media_type=media_type,
                    options=publication_options,
                    resolver=retained_resolver,
                    cancellation_token=cancellation_token,
                    load_started=retained_load_started,
                    root_parse_started=retained_root_parse_started,
                )
            elif detection.format is DocumentFormat.RDF_XML:
                snapshot = backend_driver.publish_retained_rdfxml(
                    parsed_result.native_summary,
                    parsed_native_storage=parsed_result.native_storage,
                    phase_timings=parsed_result.phase_timings,
                    payload=payload,
                    detection=detection,
                    document_iri=effective_iri,
                    media_type=media_type,
                    options=publication_options,
                    resolver=retained_resolver,
                    cancellation_token=cancellation_token,
                    load_started=retained_load_started,
                    root_parse_started=retained_root_parse_started,
                    allow_partial_rdf_mapping=allow_partial_rdf_mapping,
                )
            else:
                raise AssertionError("retained native result has an unsupported document format")
            if publish_rdfxml_document:
                return _ParsedDocumentResult(snapshot.root)
            return _ParsedDocumentResult(None, snapshot=snapshot)
        if parsed_result.native_encoded is not None:
            if backend_driver is None:
                raise AssertionError("native framing has no backend driver")
            parsed = backend_driver.decode_functional(
                parsed_result.native_encoded,
                selected_options.limits,
            )
        else:
            selected_parsed = parsed_result.ontology
            if selected_parsed is None:
                raise AssertionError("parser returned neither a model nor retained framing")
            parsed = selected_parsed
        imports, annotations, axioms, extensions = freeze_document_anonymous(
            parsed.ontology_id,
            parsed.imports,
            parsed.annotations,
            parsed.axioms,
            parsed.extensions,
            limits=selected_options.limits,
        )
        decoded_length = (
            payload.decoded_codepoint_length
            if payload.decoded_codepoint_length is not None
            else parsed.decoded_codepoint_length
        )
        provenance = DocumentProvenance(
            payload.source_sha256,
            payload.digest_kind,
            payload.byte_length,
            decoded_length,
            effective_iri,
            payload.locator,
            detection.format,
            detection.basis,
            media_type,
            parser=(
                "pyowl_core.backends.native"
                if selected_backend == "native"
                else "pyowl_core.backends.python"
            ),
            backend=selected_backend,
        )
        matcher = (
            _FrozenMatcher((*annotations, *axioms, *extensions))
            if selected_options.preserve_source_map or selected_options.collect_provenance
            else None
        )
        source_map = None
        if selected_options.preserve_source_map:
            if matcher is None:
                raise AssertionError("source-map construction requires a frozen matcher")
            builder = SourceMapBuilder(dict(parsed.prefixes))
            language_details = _language_details(
                parsed,
                _source_language_tags(
                    payload.data,
                    detection.format,
                    selected_options,
                    cancellation_token,
                ),
            )
            blank_label_details = _blank_label_details(parsed)
            source_map_entries = 0
            for occurrence, (original, span) in enumerate(parsed.occurrences):
                frozen, digest = matcher.match(original)
                if frozen is not None:
                    details = language_details[occurrence]
                    builder.add_digest(
                        digest,
                        occurrence,
                        span,
                        _root_lexical_details(
                            details,
                            blank_label_details[occurrence],
                        ),
                    )
                    source_map_entries += 1
                    for literal, spelling in details:
                        builder.add(
                            literal,
                            occurrence,
                            span,
                            {"language-tag": spelling},
                        )
                        source_map_entries += 1
                    selected_options.limits.enforce("max_source_map_entries", source_map_entries)
            source_map = builder.freeze()
        origin_index = None
        if selected_options.collect_provenance:
            if matcher is None:
                raise AssertionError("origin construction requires a frozen matcher")
            provisional = OntologyDocument(
                parsed.ontology_id,
                effective_iri,
                imports,
                annotations,
                axioms,
                extensions,
                provenance,
                source_map,
                None,
                parsed.rdf_mapping_report,
            )
            origin_builder = OriginIndexBuilder(provisional.document_fingerprint.hex)
            for occurrence, (original, span) in enumerate(parsed.occurrences):
                frozen, digest = matcher.match(original)
                if frozen is not None:
                    origin_builder.add_digest(digest, occurrence, span)
            origin_index = origin_builder.freeze()
        document = OntologyDocument(
            parsed.ontology_id,
            effective_iri,
            imports,
            annotations,
            axioms,
            extensions,
            provenance,
            source_map,
            origin_index,
            parsed.rdf_mapping_report,
        )
        return _ParsedDocumentResult(
            document,
            parsed_result.native_storage,
            (
                parsed_result.phase_timings
                if parsed_result.native_storage is not None
                else ()
            ),
        )


def parse_document(
    source: DocumentSource,
    *,
    format: DocumentFormat | str | None = None,
    document_iri: IRI | str | None = None,
    options: LoadOptions | None = None,
    cancellation_token: CancellationToken | None = None,
) -> OntologyDocument:
    """Parse exactly one document and never resolve an import."""

    return PythonParser().parse(
        source,
        format=format,
        document_iri=document_iri,
        options=options,
        cancellation_token=cancellation_token,
    )


def _source_language_tags(
    data: bytes,
    format: DocumentFormat,
    options: LoadOptions,
    cancellation_token: CancellationToken | None,
) -> tuple[str, ...]:
    if format in (DocumentFormat.FUNCTIONAL, DocumentFormat.TURTLE):
        text = data.decode("utf-8-sig", errors="strict")
        context = ParseContext(options.limits, cancellation_token)
        tokens = (
            FunctionalLexer(text, context).tokenize()
            if format is DocumentFormat.FUNCTIONAL
            else TurtleLexer(text, context).tokenize()
        )
        return tuple(token.value for token in tokens if token.kind == "LANG")
    root = ET.fromstring(data)
    xml_language = "{http://www.w3.org/XML/1998/namespace}lang"
    return tuple(
        language
        for element in root.iter()
        if (language := element.get(xml_language) or element.get("lang")) is not None
    )


def _language_details(
    parsed: ParsedOntology,
    spellings: tuple[str, ...],
) -> tuple[tuple[tuple[Literal, str], ...], ...]:
    by_language: dict[str, deque[str]] = defaultdict(deque)
    for spelling in spellings:
        by_language[spelling.lower()].append(spelling)
    result: list[tuple[tuple[Literal, str], ...]] = []
    for value, _span in parsed.occurrences:
        details: list[tuple[Literal, str]] = []
        for node in walk(value):
            if not isinstance(node, Literal) or node.language is None:
                continue
            candidates = by_language[node.language]
            if candidates:
                details.append((node, candidates.popleft()))
        result.append(tuple(details))
    return tuple(result)


def _blank_label_details(parsed: ParsedOntology) -> tuple[tuple[str, ...], ...]:
    source_labels = frozenset(parsed.source_blank_labels)
    result: list[tuple[str, ...]] = []
    for value, _span in parsed.occurrences:
        labels: set[str] = set()
        for node in walk(value):
            if not isinstance(node, AnonymousIndividual):
                continue
            label = provisional_label(node)
            if label is not None and label in source_labels:
                labels.add(label)
        result.append(tuple(sorted(labels)))
    return tuple(result)


def _root_lexical_details(
    language_details: tuple[tuple[Literal, str], ...],
    blank_labels: tuple[str, ...],
) -> Mapping[str, str]:
    result: dict[str, str] = {}
    for index, (_literal, spelling) in enumerate(language_details, 1):
        key = "language-tag" if index == 1 else f"language-tag:{index}"
        result[key] = spelling
    for index, label in enumerate(blank_labels, 1):
        key = "blank-label" if index == 1 else f"blank-label:{index}"
        result[key] = label
    return result


def _parse_payload(
    data: bytes,
    format: DocumentFormat,
    *,
    limits: ParseLimits,
    document_iri: IRI | None,
    cancellation_token: CancellationToken | None,
    allow_partial_rdf_mapping: bool,
    allow_swrl: bool,
    backend: str,
    retain_native_storage: bool,
    collect_provenance: bool,
    preserve_source_map: bool,
    record_unresolved: bool,
    require_empty_imports: bool,
    materialize_native_document: bool,
    backend_driver: _BackendDriver | None,
) -> _ParsedPayloadResult:
    if not isinstance(limits, ParseLimits):
        raise TypeError("limits must be ParseLimits")
    try:
        if format is DocumentFormat.FUNCTIONAL:
            if backend == "native":
                if backend_driver is None:
                    raise AssertionError("native selection has no backend driver")
                return backend_driver.parse_functional(
                    data,
                    limits=limits,
                    cancellation_token=cancellation_token,
                    allow_swrl=allow_swrl,
                    retain_native_storage=retain_native_storage,
                    collect_provenance=collect_provenance,
                    preserve_source_map=preserve_source_map,
                    record_unresolved=record_unresolved,
                    require_empty_imports=require_empty_imports,
                    materialize_document=materialize_native_document,
                )
            return _ParsedPayloadResult(
                parse_functional(
                    data,
                    limits=limits,
                    cancellation_token=cancellation_token,
                    allow_swrl=allow_swrl,
                )
            )
        if format is DocumentFormat.OWL_XML:
            return _ParsedPayloadResult(
                parse_owlxml(data, limits=limits, cancellation_token=cancellation_token)
            )
        if format is DocumentFormat.TURTLE:
            return _ParsedPayloadResult(
                parse_turtle(
                    data,
                    limits=limits,
                    document_iri=document_iri,
                    cancellation_token=cancellation_token,
                    allow_partial_rdf_mapping=allow_partial_rdf_mapping,
                    allow_swrl=allow_swrl,
                )
            )
        if format is DocumentFormat.RDF_XML:
            if backend == "native":
                if backend_driver is None:
                    raise AssertionError("native selection has no backend driver")
                return backend_driver.parse_rdfxml(
                    data,
                    document_iri=document_iri,
                    limits=limits,
                    cancellation_token=cancellation_token,
                    allow_partial_rdf_mapping=allow_partial_rdf_mapping,
                    allow_swrl=allow_swrl,
                    retain_native_storage=retain_native_storage,
                    collect_provenance=collect_provenance,
                    preserve_source_map=preserve_source_map,
                    require_empty_imports=require_empty_imports,
                )
            return _ParsedPayloadResult(
                parse_rdfxml(
                    data,
                    limits=limits,
                    document_iri=document_iri,
                    cancellation_token=cancellation_token,
                    allow_partial_rdf_mapping=allow_partial_rdf_mapping,
                    allow_swrl=allow_swrl,
                )
            )
    except PyOWLCoreError:
        raise
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except (TypeError, ValueError) as error:
        raise OntologySyntaxError(
            f"invalid {format.value} ontology structure",
            code="ONTOLOGY_STRUCTURE_INVALID",
        ) from error
    raise AssertionError((format, backend))


def _coerce_iri(value: IRI | str | None) -> IRI | None:
    if value is None or isinstance(value, IRI):
        return value
    if isinstance(value, str):
        return IRI(value)
    raise TypeError("document_iri must be IRI, str, or None")


class _FrozenMatcher:
    __slots__ = ("_by_digest", "_by_id", "_by_skeleton", "_digest_cache", "_values")

    def __init__(self, values: tuple[StructuralNode, ...]) -> None:
        self._values = values
        self._by_id = {id(item): item for item in values}
        self._by_digest: dict[bytes, StructuralNode] | None = None
        self._by_skeleton: dict[tuple[type[StructuralNode], bytes], StructuralNode] | None = None
        self._digest_cache: dict[int, bytes] = {}

    def match(self, original: StructuralNode) -> tuple[StructuralNode | None, bytes]:
        candidate = self._by_id.get(id(original))
        if candidate is None:
            digest = structural_digest(original)
            if self._by_digest is None:
                self._by_digest = {structural_digest(item): item for item in self._values}
            candidate = self._by_digest.get(digest)
            if candidate is None:
                from pyowl_core.document.document import _skeleton

                if self._by_skeleton is None:
                    self._by_skeleton = {
                        (type(item), _skeleton(item)): item for item in self._values
                    }
                candidate = self._by_skeleton.get((type(original), _skeleton(original)))
        if candidate is None:
            return None, b"\x00" * 32
        identifier = id(candidate)
        cached_digest = self._digest_cache.get(identifier)
        if cached_digest is None:
            cached_digest = structural_digest(candidate)
            self._digest_cache[identifier] = cached_digest
        return candidate, cached_digest


__all__ = ["PythonParser", "parse_document"]
