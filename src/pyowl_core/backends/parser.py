"""Backend-aware document parser orchestration.

The semantic-reference :mod:`pyowl_core.backends.python` package remains
independent of the optional extension.  This module injects native selection
and execution only for the public backend-aware facade.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from pyowl_core.backends.python.parser import (
    PythonParser,
    _BackendDriver,
    _ParsedDocumentResult,
    _ParsedPayloadResult,
)
from pyowl_core.cancellation import CancellationToken
from pyowl_core.config import BackendPreference, DocumentFormat, LoadOptions
from pyowl_core.document import OntologyDocument
from pyowl_core.io.formats.common import ParsedOntology
from pyowl_core.io.formats.detection import FormatDetection
from pyowl_core.io.source import DocumentSource, SourcePayload
from pyowl_core.limits import ParseLimits
from pyowl_core.model import IRI

if TYPE_CHECKING:
    from pyowl_core.document.snapshot import OntologySnapshot
    from pyowl_core.io.resolver import ImportResolver


class _NativeBackendDriver:
    """Deferred imports for the optional native execution boundary."""

    __slots__ = ()

    def select(
        self,
        preference: BackendPreference,
        format: DocumentFormat,
    ) -> str:
        from pyowl_core.backends.dispatch import select_backend

        return select_backend(
            preference,
            capability=f"parse-{format.value}-v1",
            operation=f"{format.value} document parse",
        ).backend

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
    ) -> _ParsedPayloadResult:
        if retain_native_storage:
            from pyowl_core.backends.dispatch import _parse_functional_native_retained_v2

            retained = _parse_functional_native_retained_v2(
                data,
                limits=limits,
                cancellation_token=cancellation_token,
                allow_swrl=allow_swrl,
                collect_provenance=collect_provenance,
                preserve_source_map=preserve_source_map,
                record_unresolved=record_unresolved,
                require_empty_imports=require_empty_imports,
                materialize_document=materialize_document,
            )
            return _ParsedPayloadResult(
                ontology=retained.parsed,
                native_storage=retained.storage,
                phase_timings=retained.phase_timings,
                native_encoded=retained.encoded,
                native_summary=retained.summary,
            )
        from pyowl_core.backends.dispatch import parse_functional_native

        return _ParsedPayloadResult(
            parse_functional_native(
                data,
                limits=limits,
                cancellation_token=cancellation_token,
                allow_swrl=allow_swrl,
            )
        )

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
    ) -> OntologySnapshot:
        from pyowl_core.backends.native_ingestion import (
            publish_retained_functional_snapshot_v2,
        )

        return publish_retained_functional_snapshot_v2(
            summary,
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

    def decode_functional(
        self,
        encoded: bytes,
        limits: ParseLimits,
    ) -> ParsedOntology:
        from pyowl_core.backends.native import _decode_parsed_functional

        return _decode_parsed_functional(encoded, limits)


_DRIVER: Final[_BackendDriver] = _NativeBackendDriver()


class _BackendParser(PythonParser):
    __slots__ = ()

    def _backend_driver(self) -> _BackendDriver:
        return _DRIVER


def parse_document(
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
    """Parse one document with capability-first backend selection."""

    return _BackendParser().parse(
        source,
        format=format,
        document_iri=document_iri,
        options=options,
        media_type=media_type,
        allow_partial_rdf_mapping=allow_partial_rdf_mapping,
        allow_swrl=allow_swrl,
        cancellation_token=cancellation_token,
    )


def _parse_document_for_retained_load(
    source: DocumentSource,
    *,
    document_iri: IRI | str | None,
    options: LoadOptions,
    resolver: ImportResolver | None,
    cancellation_token: CancellationToken | None,
    load_started: float,
    root_parse_started: float,
) -> _ParsedDocumentResult:
    """Parse a root while retaining an unadvertised native structural owner."""

    return PythonParser()._parse(
        source,
        document_iri=document_iri,
        options=options,
        cancellation_token=cancellation_token,
        retain_native_storage=True,
        retained_resolver=resolver,
        retained_load_started=load_started,
        retained_root_parse_started=root_parse_started,
        backend_driver=_DRIVER,
    )


def _parse_import_for_retained_load(
    source: DocumentSource,
    *,
    format: DocumentFormat | str | None,
    document_iri: IRI | str | None,
    options: LoadOptions,
    media_type: str | None,
    cancellation_token: CancellationToken | None,
) -> _ParsedDocumentResult:
    """Parse one closure document while retaining its native structural owner."""

    return PythonParser()._parse(
        source,
        format=format,
        document_iri=document_iri,
        options=options,
        media_type=media_type,
        cancellation_token=cancellation_token,
        retain_native_storage=True,
        materialize_native_document=True,
        backend_driver=_DRIVER,
    )


__all__ = ["parse_document"]
