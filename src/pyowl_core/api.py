"""Curated parse, load, and identity-preserving coercion facade."""

from __future__ import annotations

from typing import TypeAlias, cast

from pyowl_core.backends.python.parser import parse_document as _parse_document
from pyowl_core.cancellation import CancellationToken
from pyowl_core.config import BackendPreference, DocumentFormat, LoadOptions
from pyowl_core.document.composite import OntologyComposite, compose_views
from pyowl_core.document.delta import OntologyDelta
from pyowl_core.document.document import OntologyDocument
from pyowl_core.document.imports import DocumentInput
from pyowl_core.document.imports import load_snapshot as _load_snapshot
from pyowl_core.document.overlay import OntologyOverlay, apply_delta
from pyowl_core.document.snapshot import (
    OntologySnapshot,
    OntologyView,
    SnapshotProvider,
    _is_ontology_view,
)
from pyowl_core.exceptions import AdapterCompatibilityError, OptionConflictError
from pyowl_core.io.resolver import ImportResolver
from pyowl_core.io.source import DocumentSource
from pyowl_core.model import IRI
from pyowl_core.wire import decode_snapshot, encode_snapshot, open_snapshot, write_snapshot

OntologyInput: TypeAlias = DocumentInput | OntologyView | SnapshotProvider


def parse_document(
    source: DocumentSource,
    *,
    format: DocumentFormat | str | None = None,
    document_iri: IRI | str | None = None,
    options: LoadOptions | None = None,
) -> OntologyDocument:
    """Parse exactly one source; snapshots/providers are rejected and never inspected."""

    if (
        isinstance(source, OntologyDocument)
        or _is_ontology_view(source)
        or isinstance(source, SnapshotProvider)
    ):
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
    document_iri: IRI | str | None = None,
    options: LoadOptions | None = None,
    resolver: ImportResolver | None = None,
    cancellation_token: CancellationToken | None = None,
) -> OntologySnapshot:
    """Create a concrete immutable closure from one root source or document.

    ``document_iri`` supplies the base/identity of an unparsed root source. It
    is required for streams and cannot rebase an accepted ``OntologyDocument``.
    """

    if _is_ontology_view(source) or isinstance(source, SnapshotProvider):
        raise TypeError("load_snapshot accepts a document source or OntologyDocument")
    return _load_snapshot(
        source,
        document_iri=document_iri,
        options=options,
        resolver=resolver,
        cancellation_token=cancellation_token,
    )


def coerce_snapshot(
    source: OntologyInput,
    *,
    document_iri: IRI | str | None = None,
    options: LoadOptions | None = None,
    resolver: ImportResolver | None = None,
    cancellation_token: CancellationToken | None = None,
) -> OntologyView:
    """Return an existing compatible view by identity; parse only document input."""

    _validate_document_iri(document_iri)
    if _is_ontology_view(source):
        _reject_bound_document_iri(document_iri)
        _validate_view(source, options, resolver)
        return source
    if isinstance(source, SnapshotProvider):
        _reject_bound_document_iri(document_iri)
        supplied = source.owl_snapshot()
        if not _is_ontology_view(supplied):
            raise AdapterCompatibilityError(
                "SnapshotProvider.owl_snapshot() did not return OntologyView",
                code="ADAPTER_PROVIDER_RESULT",
            )
        _validate_view(supplied, options, resolver)
        return supplied
    return load_snapshot(
        cast(DocumentInput, source),
        document_iri=document_iri,
        options=options,
        resolver=resolver,
        cancellation_token=cancellation_token,
    )


def _validate_document_iri(document_iri: IRI | str | None) -> None:
    if document_iri is not None and not isinstance(document_iri, (IRI, str)):
        raise TypeError("document_iri must be IRI, str, or None")


def _reject_bound_document_iri(document_iri: IRI | str | None) -> None:
    if document_iri is not None:
        raise OptionConflictError(
            "document_iri applies only to an unparsed root source",
            code="DOCUMENT_IRI_SOURCE_CONFLICT",
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
    check = getattr(view, "_check_open", None)
    if callable(check):
        check()
    else:
        # Foreign/future views may expose lifecycle validation only through report.
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
    snapshots = tuple(_leaf_snapshots(view))
    if snapshots:
        if any(
            options.imports is not snapshot.import_manifest.policy
            or options.offline != snapshot.import_manifest.offline
            for snapshot in snapshots
        ):
            raise OptionConflictError(
                "import options conflict with an existing view member manifest",
                code="VIEW_IMPORT_OPTION_CONFLICT",
            )
        if options.format is not None:
            raise OptionConflictError(
                "format cannot be applied to an existing ontology view",
                code="VIEW_FORMAT_OPTION_CONFLICT",
            )
        if options.preserve_source_map and any(
            item.source_map is None for snapshot in snapshots for item in snapshot.documents
        ):
            raise OptionConflictError(
                "an existing view member does not contain requested source maps",
                code="VIEW_SOURCE_MAP_CONFLICT",
            )
        if options.validate_owl2_dl and any(
            snapshot.owl2_dl_report is None for snapshot in snapshots
        ):
            raise OptionConflictError(
                "OWL 2 DL validation cannot be retroactively applied to an existing view",
                code="VIEW_VALIDATION_OPTION_CONFLICT",
            )
        if options.validate_owl2_dl and "owl2-dl-validated" not in capabilities.features:
            raise OptionConflictError(
                "the effective existing view is not OWL 2 DL validated",
                code="VIEW_VALIDATION_OPTION_CONFLICT",
            )
    if options.backend is BackendPreference.NATIVE and capabilities.backend != "native":
        raise OptionConflictError(
            "requested backend conflicts with existing view backend",
            code="VIEW_BACKEND_CONFLICT",
        )


def _leaf_snapshots(view: OntologyView) -> tuple[OntologySnapshot, ...]:
    if isinstance(view, OntologySnapshot):
        return (view,)
    if isinstance(view, OntologyOverlay):
        return _leaf_snapshots(view.base)
    if isinstance(view, OntologyComposite):
        values: list[OntologySnapshot] = []
        observed: set[int] = set()
        for member in view.members:
            for snapshot in _leaf_snapshots(member.view):
                if id(snapshot) not in observed:
                    observed.add(id(snapshot))
                    values.append(snapshot)
        return tuple(values)
    return ()


__all__ = [
    "DocumentInput",
    "OntologyDelta",
    "OntologyInput",
    "apply_delta",
    "coerce_snapshot",
    "compose_views",
    "decode_snapshot",
    "encode_snapshot",
    "load_snapshot",
    "open_snapshot",
    "parse_document",
    "write_snapshot",
]
