"""Authoritative private ABI for the optional native extension."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from .backends.native_handoff import NativeSnapshotAttestationV1
from .backends.native_handoff_v2 import (
    NativeFacadeCardinalitySummaryV2,
    NativeFacadeContainsRequestV2,
    NativeFacadeCountersV2,
    NativeFacadePageRequestV2,
    NativeFacadePageV2,
    NativeFingerprintEvidenceV2,
    NativeOWL2DLReportSummaryV2,
    NativeSnapshotAttestationV2,
)
from .config import LoadOptions

ABI_VERSION: Final[int]
MODEL_SCHEMA_VERSION: Final[int]
WIRE_FORMAT_VERSION: Final[tuple[int, int]]
FEATURES: Final[tuple[str, ...]]
INGESTION_FEATURES: Final[tuple[str, ...]]
VIEW_FEATURES: Final[tuple[str, ...]]

class _NativeError(Exception): ...

class _Cancellation:
    def __init__(self, deadline_seconds: float | None = None) -> None: ...
    @property
    def cancelled(self) -> bool: ...
    def cancel(self) -> bool: ...

class _NativeDocumentHandle:
    def _publication_closed_v1(self) -> bool: ...
    def _publication_close_v1(self) -> None: ...
    def _publication_attestation_v2(self) -> NativeSnapshotAttestationV2: ...
    def _publication_closed_v2(self) -> bool: ...
    def _publication_close_v2(self) -> None: ...
    def _publication_page_v2(self, request: NativeFacadePageRequestV2) -> NativeFacadePageV2: ...
    def _publication_contains_v2(self, request: NativeFacadeContainsRequestV2) -> bool: ...
    def _publication_counters_v2(self) -> NativeFacadeCountersV2: ...

class _NativeSnapshotHandle:
    def _publication_attestation_v1(self) -> NativeSnapshotAttestationV1: ...
    def _publication_closed_v1(self) -> bool: ...
    def _publication_close_v1(self) -> None: ...
    def _publication_attestation_v2(self) -> NativeSnapshotAttestationV2: ...
    def _publication_closed_v2(self) -> bool: ...
    def _publication_close_v2(self) -> None: ...
    def _publication_page_v2(self, request: NativeFacadePageRequestV2) -> NativeFacadePageV2: ...
    def _publication_contains_v2(self, request: NativeFacadeContainsRequestV2) -> bool: ...
    def _publication_counters_v2(self) -> NativeFacadeCountersV2: ...
    def _publication_document_v2(self, document_ordinal: int) -> _NativeDocumentHandle: ...

class _NativeParsedStructuralStorageV2: ...

class _NativeRetainedAxiomTypeIndexV1:
    def _binding_v1(self) -> tuple[bytes, bytes]: ...
    def _canonical_sizes_v1(self) -> tuple[int, ...]: ...
    def _layout_v1(
        self,
    ) -> tuple[
        tuple[int, ...],
        tuple[int, ...],
        tuple[int, ...],
        tuple[int, ...],
        tuple[int, ...],
        dict[str, int],
    ]: ...
    def _page_v1(
        self,
        tag: int,
        start: int,
        max_rows: int,
        max_bytes: int,
        config: object,
        cancel: _Cancellation | None = None,
    ) -> tuple[tuple[bytes, ...], int, int | None]: ...

class _NativeRetainedSignatureIndexV1:
    def _layout_v1(
        self,
    ) -> tuple[
        bytes,
        bytes,
        tuple[int, ...],
        tuple[int, ...],
        tuple[int, ...],
        dict[str, int],
    ]: ...

class _NativeRetainedOntologyIdentityIndexV1:
    def _layout_v1(
        self,
    ) -> tuple[str, bytes, bytes, bytes, dict[str, int]]: ...

def version() -> tuple[str, int]: ...
def self_test() -> None: ...
def validate_canonical(
    canonical: object,
    config: object,
    cancel: _Cancellation | None = None,
) -> bytes: ...
def validate_wire(
    snapshot_wire: object,
    config: object,
    cancel: _Cancellation | None = None,
) -> bytes: ...
def roundtrip_wire(
    snapshot_wire: object,
    config: object,
    cancel: _Cancellation | None = None,
) -> bytes: ...
def parse_document(
    source: object,
    config: object,
    cancel: _Cancellation | None = None,
) -> bytes: ...
def build_snapshot(documents: object, config: bytes, cancel: object) -> bytes: ...
def build_index(
    snapshot_wire: object,
    request: object,
    cancel: _Cancellation | None = None,
) -> bytes: ...
def _encoded_structural_columns_v1(
    handle: _NativeSnapshotHandle,
    scope: object,
    document_ordinal: int | None,
    config: object,
    cancel: _Cancellation | None = None,
) -> tuple[dict[str, memoryview], dict[str, int]]: ...
def _encoded_structural_document_columns_v1(
    handle: _NativeDocumentHandle,
    config: object,
    cancel: _Cancellation | None = None,
) -> tuple[dict[str, memoryview], dict[str, int]]: ...
def _retained_axiom_type_index_v1(
    handle: _NativeSnapshotHandle,
    scope: object,
    document_ordinal: int | None,
    config: object,
    cancel: _Cancellation | None = None,
) -> _NativeRetainedAxiomTypeIndexV1: ...
def _retained_signature_index_v1(
    handle: _NativeSnapshotHandle,
    scope: object,
    document_ordinal: int | None,
    config: object,
    cancel: _Cancellation | None = None,
) -> _NativeRetainedSignatureIndexV1: ...
def _retained_ontology_identity_index_v1(
    handle: _NativeSnapshotHandle,
) -> _NativeRetainedOntologyIdentityIndexV1: ...
def _retain_structural_snapshot_v2(
    documents: object,
    origins: object,
    attestation: NativeSnapshotAttestationV2,
    config: object,
    cancel: _Cancellation | None = None,
) -> _NativeSnapshotHandle: ...
def _parse_functional_retained_v2(
    source: object,
    config: object,
    collect_provenance: bool,
    record_unresolved: bool,
    require_empty_imports: bool,
    cancel: _Cancellation | None = None,
) -> tuple[bytes, _NativeParsedStructuralStorageV2, tuple[int, int, int, int]]: ...
def _parse_rdfxml_retained_v2(
    source: object,
    document_iri: str | None,
    config: object,
    collect_provenance: bool,
    allow_partial_rdf_mapping: bool,
    require_empty_imports: bool,
    cancel: _Cancellation | None = None,
) -> tuple[bytes, _NativeParsedStructuralStorageV2, tuple[int, int, int, int, int]]: ...
def _prepare_parsed_structural_snapshot_v2(
    parsed: _NativeParsedStructuralStorageV2,
    manifest: bytes,
    document_key: str,
    collect_provenance: bool,
    cancel: _Cancellation | None = None,
) -> bytes: ...
def _finalize_parsed_structural_snapshot_v2(
    parsed: _NativeParsedStructuralStorageV2,
    prepared_summary: bytes,
    attestation: NativeSnapshotAttestationV2,
    cancel: _Cancellation | None = None,
) -> _NativeSnapshotHandle: ...
def _work_probe(iterations: int, config: object, cancel: _Cancellation | None = None) -> int: ...
def _panic_probe() -> None: ...
def _encoded_structural_fixture_v1() -> _NativeSnapshotHandle: ...
def _publication_fixture_v2(
    attestation: NativeSnapshotAttestationV2,
    collections: Mapping[tuple[object, ...], Sequence[bytes]],
    *,
    documents: tuple[object, ...],
    report: object,
    root_document_key: str,
    load_options: LoadOptions,
    capability_bits: int,
    fingerprint_evidence: tuple[NativeFingerprintEvidenceV2, ...],
    fingerprint_preimages: tuple[bytes, ...],
    facade_cardinality_summary: NativeFacadeCardinalitySummaryV2,
    owl2_dl_report_summary: NativeOWL2DLReportSummaryV2 | None = None,
    raw_document_collections: Mapping[tuple[object, ...], Sequence[bytes]] | None = None,
    max_retained_bytes: int = 67_108_864,
) -> _NativeSnapshotHandle: ...
def _unique_axiom_publication_fixture_v2(
    attestation: NativeSnapshotAttestationV2,
    row_count: int,
    *,
    max_retained_bytes: int = 1_073_741_824,
) -> _NativeSnapshotHandle: ...
