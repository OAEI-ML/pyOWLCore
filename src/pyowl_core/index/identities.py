"""Document identity and loader-provenance metadata for every core view."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from pyowl_core.cancellation import CancellationToken
from pyowl_core.document.composite import OntologyComposite
from pyowl_core.document.identity import (
    OntologyDocumentIdentity,
    _OntologyIdentityMetadata,
)
from pyowl_core.document.overlay import OntologyOverlay
from pyowl_core.document.snapshot import OntologyView, _is_ontology_view
from pyowl_core.model import encode_varint

from .cache import IndexBuildBudget, ViewBuildReport, ViewBuildStrategy, build_report


@dataclass(frozen=True, slots=True)
class OntologyIdentityOptions:
    """The identity view has no semantic options."""


class OntologyIdentityIndex:
    """Immutable closure document identities and provenance digests."""

    SCHEMA_NAME = "pyowl-core/ontology-identity-index"
    SCHEMA_VERSION = 1
    OPTIONS_TYPE = OntologyIdentityOptions
    DEPENDENCIES: tuple[type[object], ...] = ()

    def __init__(
        self,
        ontology: OntologyView,
        metadata: _OntologyIdentityMetadata,
        sources: tuple[OntologyIdentityIndex, ...],
        report: ViewBuildReport,
    ) -> None:
        self._ontology = ontology
        self._metadata = metadata
        self._sources = sources
        self.documents = metadata.documents
        self.document_keys = tuple(item.document_key for item in metadata.documents)
        self.import_manifest_digest = metadata.import_manifest_digest
        self.loader_diagnostics_digest = metadata.loader_diagnostics_digest
        self.is_complete = metadata.is_complete
        self.report = report

    @classmethod
    def _build(
        cls,
        ontology: object,
        options: object,
        budget: IndexBuildBudget,
        cancellation_token: CancellationToken | None,
        started: float,
    ) -> OntologyIdentityIndex:
        if not isinstance(options, OntologyIdentityOptions):
            raise TypeError("options must be OntologyIdentityOptions")
        if not _is_ontology_view(ontology):
            raise TypeError("ontology must implement OntologyView")
        if isinstance(ontology, OntologyOverlay):
            source = ontology.base.view(cls, cancellation_token=cancellation_token)
            budget.add_shared_rows(source.report.total_row_count)
            budget.add("identity_adapter", rows=0, bytes_=128)
            return cls(
                ontology,
                source._metadata,
                (source,),
                build_report(
                    cls,
                    ViewBuildStrategy.PATCHED,
                    budget,
                    started,
                    shared_bytes=source.report.own_bytes + source.report.shared_bytes,
                ),
            )
        if isinstance(ontology, OntologyComposite):
            sources: list[OntologyIdentityIndex] = []
            members: list[tuple[bytes, OntologyIdentityIndex]] = []
            for token, source_view in zip(
                ontology._source_tokens(), ontology._sources, strict=True
            ):
                source = source_view.view(cls, cancellation_token=cancellation_token)
                sources.append(source)
                members.append((token, source))
                budget.add_shared_rows(source.report.total_row_count)
            documents: list[OntologyDocumentIdentity] = []
            for token, source in members:
                prefix = "member:" + token.hex() + ":"
                for document in source.documents:
                    moved = OntologyDocumentIdentity(
                        prefix + document.document_key,
                        document.ontology_id,
                    )
                    documents.append(moved)
                    budget.add(
                        "scoped_document_identities",
                        bytes_=_identity_size(moved),
                    )
            metadata = _OntologyIdentityMetadata(
                tuple(documents),
                _combined_digest(
                    b"pyowl-core:composite-import-manifests:v1\x00",
                    tuple((token, source.import_manifest_digest) for token, source in members),
                ),
                _combined_digest(
                    b"pyowl-core:composite-loader-diagnostics:v1\x00",
                    tuple((token, source.loader_diagnostics_digest) for token, source in members),
                ),
                all(source.is_complete for source in sources),
            )
            shared = sum(item.report.own_bytes + item.report.shared_bytes for item in sources)
            return cls(
                ontology,
                metadata,
                tuple(sources),
                build_report(
                    cls,
                    ViewBuildStrategy.MERGED,
                    budget,
                    started,
                    shared_bytes=shared,
                ),
            )
        metadata_method = getattr(ontology, "_ontology_identity_metadata", None)
        if not callable(metadata_method):
            raise LookupError("ontology view does not advertise ontology-identity-index metadata")
        metadata = metadata_method(cancellation_token=cancellation_token)
        if not isinstance(metadata, _OntologyIdentityMetadata):
            raise TypeError("ontology identity metadata provider returned an invalid value")
        for document in metadata.documents:
            budget.add("document_identities", bytes_=_identity_size(document))
        return cls(
            ontology,
            metadata,
            (),
            build_report(cls, ViewBuildStrategy.FULL_BUILD, budget, started),
        )


def _identity_size(value: OntologyDocumentIdentity) -> int:
    ontology = value.ontology_id
    return (
        96
        + len(value.document_key.encode("utf-8"))
        + (0 if ontology.ontology_iri is None else len(ontology.ontology_iri.value.encode("utf-8")))
        + (0 if ontology.version_iri is None else len(ontology.version_iri.value.encode("utf-8")))
    )


def _combined_digest(domain: bytes, values: tuple[tuple[bytes, bytes], ...]) -> bytes:
    pieces = [domain, encode_varint(len(values))]
    for token, digest in sorted(values):
        pieces.extend((encode_varint(len(token)), token, digest))
    return hashlib.sha256(b"".join(pieces)).digest()


__all__ = [
    "OntologyDocumentIdentity",
    "OntologyIdentityIndex",
    "OntologyIdentityOptions",
]
