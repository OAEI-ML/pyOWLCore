"""Stable names advertised through :class:`~pyowl_core.CoreCapabilities`."""

from __future__ import annotations

from enum import Enum


class CoreFeature(str, Enum):
    """Built-in version-1 ontology-view capability names.

    Consumers compare these string values and never infer capabilities from a
    concrete snapshot, overlay, composite, or mapped layout.
    """

    OWL2_STRUCTURAL = "owl2-structural"
    DOCUMENT_BOUNDARIES = "document-boundaries"
    IMPORT_MANIFEST = "import-manifest"
    IMMUTABLE_SNAPSHOT = "immutable-snapshot"
    DOCUMENT_SCOPED_ANONYMOUS = "document-scoped-anonymous"
    STRUCTURAL_INDEXES = "structural-indexes"
    ONTOLOGY_IDENTITY_INDEX = "ontology-identity-index"
    SOURCE_MAP = "source-map"
    OWL2_DL_VALIDATED = "owl2-dl-validated"
    WIRE_V1 = "wire-v1"
    WIRE_VERIFIED = "wire-verified"
    MMAP_SNAPSHOT = "mmap-snapshot"
    LAZY_MODEL = "lazy-model"
    MATERIALIZED_VIEW = "materialized-view"
    ONTOLOGY_OVERLAY = "ontology-overlay"
    PERSISTENT_DELTA = "persistent-delta"
    ONTOLOGY_COMPOSITE = "ontology-composite"
    ZERO_COPY_VIEW = "zero-copy-view"
    MEMBER_PROVENANCE = "member-provenance"


KNOWN_CORE_FEATURES = frozenset(feature.value for feature in CoreFeature)

STRUCTURAL_CONSUMER_FEATURES = frozenset(
    {
        CoreFeature.OWL2_STRUCTURAL.value,
        CoreFeature.DOCUMENT_BOUNDARIES.value,
        CoreFeature.IMPORT_MANIFEST.value,
        CoreFeature.DOCUMENT_SCOPED_ANONYMOUS.value,
    }
)

IDENTITY_AWARE_CONSUMER_FEATURES = frozenset(
    {*STRUCTURAL_CONSUMER_FEATURES, CoreFeature.ONTOLOGY_IDENTITY_INDEX.value}
)


__all__ = [
    "IDENTITY_AWARE_CONSUMER_FEATURES",
    "KNOWN_CORE_FEATURES",
    "STRUCTURAL_CONSUMER_FEATURES",
    "CoreFeature",
]
