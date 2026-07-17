"""Immutable document contracts."""

from .document import Fingerprint, OntologyDocument, OntologyID
from .provenance import (
    DetectionBasis,
    DigestKind,
    DocumentProvenance,
    OriginIndex,
    OriginOccurrence,
    RDFMappingReport,
    RDFTripleEvidence,
    SourceMap,
    SourceOccurrence,
)

__all__ = [
    "DetectionBasis",
    "DigestKind",
    "DocumentProvenance",
    "Fingerprint",
    "OntologyDocument",
    "OntologyID",
    "OriginIndex",
    "OriginOccurrence",
    "RDFMappingReport",
    "RDFTripleEvidence",
    "SourceMap",
    "SourceOccurrence",
]
