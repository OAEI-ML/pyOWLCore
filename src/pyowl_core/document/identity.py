"""Immutable document-identity and loader-provenance metadata."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from pyowl_core.diagnostics import Diagnostic

from .document import OntologyID
from .imports import ImportManifest

_DIAGNOSTICS_DOMAIN = b"pyowl-core:loader-diagnostics:v1\x00"


@dataclass(frozen=True, slots=True, order=True)
class OntologyDocumentIdentity:
    """One stable closure document key and its declared ontology identity."""

    document_key: str
    ontology_id: OntologyID

    def __post_init__(self) -> None:
        if not isinstance(self.document_key, str) or not self.document_key:
            raise ValueError("document_key must be a nonempty string")
        try:
            self.document_key.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("document_key must contain Unicode scalar values") from error
        if not isinstance(self.ontology_id, OntologyID):
            raise TypeError("ontology_id must be OntologyID")


@dataclass(frozen=True, slots=True)
class _OntologyIdentityMetadata:
    documents: tuple[OntologyDocumentIdentity, ...]
    import_manifest_digest: bytes
    loader_diagnostics_digest: bytes
    is_complete: bool

    def __post_init__(self) -> None:
        documents = tuple(
            sorted(self.documents, key=lambda item: item.document_key.encode("utf-8"))
        )
        if not all(isinstance(item, OntologyDocumentIdentity) for item in documents):
            raise TypeError("documents must contain OntologyDocumentIdentity values")
        if not documents:
            raise ValueError("documents must not be empty")
        keys = {item.document_key for item in documents}
        if len(keys) != len(documents):
            raise ValueError("document identity keys must be unique")
        for name in ("import_manifest_digest", "loader_diagnostics_digest"):
            value = getattr(self, name)
            if not isinstance(value, bytes) or len(value) != 32:
                raise ValueError(f"{name} must be exactly 32 bytes")
        if not isinstance(self.is_complete, bool):
            raise TypeError("is_complete must be bool")
        object.__setattr__(self, "documents", documents)


def _identity_metadata_from_manifest(
    manifest: ImportManifest,
    diagnostics: Iterable[Diagnostic],
    *,
    is_complete: bool,
) -> _OntologyIdentityMetadata:
    if not isinstance(manifest, ImportManifest):
        raise TypeError("manifest must be ImportManifest")
    values = tuple(diagnostics)
    if not all(isinstance(item, Diagnostic) for item in values):
        raise TypeError("diagnostics must contain Diagnostic values")
    return _OntologyIdentityMetadata(
        tuple(
            OntologyDocumentIdentity(record.document_key, record.ontology_id)
            for record in manifest.documents
        ),
        hashlib.sha256(manifest.canonical_bytes()).digest(),
        _loader_diagnostics_digest(values),
        is_complete,
    )


def _loader_diagnostics_digest(diagnostics: Iterable[Diagnostic]) -> bytes:
    values = tuple(diagnostics)
    if not all(isinstance(item, Diagnostic) for item in values):
        raise TypeError("diagnostics must contain Diagnostic values")
    encoded = json.dumps(
        [item.to_dict() for item in values],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(_DIAGNOSTICS_DOMAIN + encoded).digest()


__all__ = ["OntologyDocumentIdentity"]
