"""Curated parse, load, and identity-preserving coercion facade."""

from __future__ import annotations

from typing import TypeAlias

from pyowl_core.backends.python.parser import parse_document as _parse_document
from pyowl_core.cancellation import CancellationToken
from pyowl_core.config import BackendPreference, DocumentFormat, LoadOptions
from pyowl_core.document.document import OntologyDocument
from pyowl_core.document.imports import DocumentInput
from pyowl_core.document.imports import load_snapshot as _load_snapshot
from pyowl_core.document.snapshot import (
    OntologySnapshot,
    OntologyView,
    SnapshotProvider,
)
from pyowl_core.exceptions import AdapterCompatibilityError, OptionConflictError
from pyowl_core.io.resolver import ImportResolver
from pyowl_core.io.source import DocumentSource
from pyowl_core.model import IRI

OntologyInput: TypeAlias = DocumentInput | OntologyView | SnapshotProvider


def parse_document(
    source: DocumentSource,
    *,
    format: DocumentFormat | str | None = None,
    document_iri: IRI | str | None = None,
    options: LoadOptions | None = None,
) -> OntologyDocument:
    """Parse exactly one source; snapshots/providers are rejected and never inspected."""

    if isinstance(source, (OntologyDocument, OntologyView, SnapshotProvider)):
        raise TypeError("parse_document accepts only a one-document source")
    return _parse_document(
        source,
        format=format,
        document_iri=document_iri,
        options=options,
    )


def load_snapshot(
    source: DocumentInput,
    *,
    options: LoadOptions | None = None,
    resolver: ImportResolver | None = None,
    cancellation_token: CancellationToken | None = None,
) -> OntologySnapshot:
    """Create a concrete immutable closure without reparsing accepted documents."""

    if isinstance(source, (OntologyView, SnapshotProvider)):
        raise TypeError("load_snapshot accepts a document source or OntologyDocument")
    return _load_snapshot(
        source,
        options=options,
        resolver=resolver,
        cancellation_token=cancellation_token,
    )


def coerce_snapshot(
    source: OntologyInput,
    *,
    options: LoadOptions | None = None,
    resolver: ImportResolver | None = None,
    cancellation_token: CancellationToken | None = None,
) -> OntologyView:
    """Return an existing compatible view by identity; parse only document input."""

    if isinstance(source, OntologyView):
        _validate_view(source, options, resolver)
        return source
    if isinstance(source, SnapshotProvider):
        supplied = source.owl_snapshot()
        if not isinstance(supplied, OntologyView):
            raise AdapterCompatibilityError(
                "SnapshotProvider.owl_snapshot() did not return OntologyView",
                code="ADAPTER_PROVIDER_RESULT",
            )
        _validate_view(supplied, options, resolver)
        return supplied
    return load_snapshot(
        source,
        options=options,
        resolver=resolver,
        cancellation_token=cancellation_token,
    )


def _validate_view(
    view: OntologyView,
    options: LoadOptions | None,
    resolver: ImportResolver | None,
) -> None:
    capabilities = view.capabilities
    if capabilities.adapter_protocol != 1:
        raise AdapterCompatibilityError(
            "view adapter protocol is incompatible",
            code="ADAPTER_PROTOCOL_MISMATCH",
        )
    if capabilities.model_schema != 1:
        raise AdapterCompatibilityError(
            "view model schema is incompatible",
            code="MODEL_SCHEMA_MISMATCH",
        )
    # Accessing the report also performs lifecycle validation for future mapped views.
    _report = view.report
    if resolver is not None:
        raise OptionConflictError(
            "resolver cannot be applied to an existing ontology view",
            code="VIEW_RESOLVER_CONFLICT",
        )
    if options is None:
        return
    if not isinstance(options, LoadOptions):
        raise TypeError("options must be LoadOptions or None")
    if isinstance(view, OntologySnapshot):
        if (
            options.imports is not view.import_manifest.policy
            or options.offline != view.import_manifest.offline
        ):
            raise OptionConflictError(
                "import options conflict with the existing snapshot manifest",
                code="VIEW_IMPORT_OPTION_CONFLICT",
            )
        if options.format is not None:
            raise OptionConflictError(
                "format cannot be applied to an existing snapshot",
                code="VIEW_FORMAT_OPTION_CONFLICT",
            )
        if options.preserve_source_map and any(item.source_map is None for item in view.documents):
            raise OptionConflictError(
                "existing snapshot does not contain requested source maps",
                code="VIEW_SOURCE_MAP_CONFLICT",
            )
    if options.backend is BackendPreference.NATIVE and capabilities.backend != "native":
        raise OptionConflictError(
            "requested backend conflicts with existing view backend",
            code="VIEW_BACKEND_CONFLICT",
        )


__all__ = [
    "DocumentInput",
    "OntologyInput",
    "coerce_snapshot",
    "load_snapshot",
    "parse_document",
]
