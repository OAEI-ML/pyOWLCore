"""Complete one-document pure-Python parser orchestration."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from collections.abc import Mapping
from pathlib import Path

from pyowl_core.cancellation import CancellationToken
from pyowl_core.config import BackendPreference, DocumentFormat, LoadOptions
from pyowl_core.document import OntologyDocument
from pyowl_core.document.document import freeze_document_anonymous
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
from pyowl_core.io.formats.detection import coerce_format, detect_format
from pyowl_core.io.formats.functional import FunctionalLexer, parse_functional
from pyowl_core.io.formats.owlxml import parse_owlxml
from pyowl_core.io.formats.rdfxml import parse_rdfxml
from pyowl_core.io.formats.turtle import TurtleLexer, parse_turtle
from pyowl_core.io.source import DocumentSource, acquire_source
from pyowl_core.model import IRI, Literal, StructuralNode, structural_digest, walk

_NATIVE_AUTO_MIN_SOURCE_BYTES = 256 * 1024


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
        selected_backend = "python"
        if selected_options.backend is not BackendPreference.PYTHON and not (
            selected_options.backend is BackendPreference.AUTO
            and detection.format is DocumentFormat.FUNCTIONAL
            and len(payload.data) < _NATIVE_AUTO_MIN_SOURCE_BYTES
        ):
            from pyowl_core.backends.dispatch import select_backend

            selected_backend = select_backend(
                selected_options.backend,
                capability=f"parse-{detection.format.value}-v1",
                operation=f"{detection.format.value} document parse",
            ).backend
        parsed = _parse_payload(
            payload.data,
            detection.format,
            limits=selected_options.limits,
            document_iri=effective_iri,
            cancellation_token=cancellation_token,
            allow_partial_rdf_mapping=allow_partial_rdf_mapping,
            allow_swrl=allow_swrl,
            backend=selected_backend,
        )
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
        candidates: tuple[StructuralNode, ...] = (*annotations, *axioms, *extensions)
        matcher = _FrozenMatcher(candidates)
        source_map = None
        if selected_options.preserve_source_map:
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
            source_map_entries = 0
            for occurrence, (original, span) in enumerate(parsed.occurrences):
                frozen, digest = matcher.match(original)
                if frozen is not None:
                    details = language_details[occurrence]
                    builder.add_digest(
                        digest,
                        occurrence,
                        span,
                        _root_lexical_details(details),
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
                    selected_options.limits.enforce(
                        "max_source_map_entries", source_map_entries
                    )
            source_map = builder.freeze()
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
        return OntologyDocument(
            parsed.ontology_id,
            effective_iri,
            imports,
            annotations,
            axioms,
            extensions,
            provenance,
            source_map,
            origin_builder.freeze(),
            parsed.rdf_mapping_report,
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


def _root_lexical_details(details: tuple[tuple[Literal, str], ...]) -> Mapping[str, str]:
    if not details:
        return {}
    result = {"language-tag": details[0][1]}
    result.update(
        {
            f"language-tag:{index}": spelling
            for index, (_literal, spelling) in enumerate(details[1:], 2)
        }
    )
    return result


def _parse_payload(
    data: bytes,
    format: DocumentFormat,
    *,
    limits: object,
    document_iri: IRI | None,
    cancellation_token: CancellationToken | None,
    allow_partial_rdf_mapping: bool,
    allow_swrl: bool,
    backend: str,
) -> ParsedOntology:
    from pyowl_core.limits import ParseLimits

    if not isinstance(limits, ParseLimits):
        raise TypeError("limits must be ParseLimits")
    try:
        if format is DocumentFormat.FUNCTIONAL:
            if backend == "native":
                from pyowl_core.backends.dispatch import parse_functional_native

                return parse_functional_native(
                    data,
                    limits=limits,
                    cancellation_token=cancellation_token,
                    allow_swrl=allow_swrl,
                )
            return parse_functional(
                data,
                limits=limits,
                cancellation_token=cancellation_token,
                allow_swrl=allow_swrl,
            )
        if format is DocumentFormat.OWL_XML:
            return parse_owlxml(data, limits=limits, cancellation_token=cancellation_token)
        if format is DocumentFormat.TURTLE:
            return parse_turtle(
                data,
                limits=limits,
                document_iri=document_iri,
                cancellation_token=cancellation_token,
                allow_partial_rdf_mapping=allow_partial_rdf_mapping,
            )
        if format is DocumentFormat.RDF_XML:
            return parse_rdfxml(
                data,
                limits=limits,
                document_iri=document_iri,
                cancellation_token=cancellation_token,
                allow_partial_rdf_mapping=allow_partial_rdf_mapping,
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
