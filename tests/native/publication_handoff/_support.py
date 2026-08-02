from __future__ import annotations

import hashlib
from typing import cast

from pyowl_core.backends.native_handoff import (
    NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256,
    NATIVE_SNAPSHOT_PUBLICATION_VERSION,
    NativeDiagnosticPublicationV1,
    NativeDocumentPublicationV1,
    NativeImportManifestPublicationV1,
    NativeLoadReportPublicationV1,
    NativeSnapshotAttestationV1,
    NativeSnapshotHandleV1,
    _generated_native_snapshot_handle_v1,
    freeze_native_diagnostic_publication_v1,
    freeze_native_import_manifest_publication_v1,
    freeze_native_provenance_publication_v1,
    native_snapshot_publication_attestation_v1,
)
from pyowl_core.config import BackendPreference, DocumentFormat, LoadOptions
from pyowl_core.diagnostics import Diagnostic, Severity
from pyowl_core.document.document import Fingerprint, OntologyID
from pyowl_core.document.imports import DocumentRecord, DocumentStatus, ImportManifest
from pyowl_core.document.provenance import (
    DetectionBasis,
    DigestKind,
    DocumentProvenance,
)
from pyowl_core.model import IRI


def generated_handle(attestation: NativeSnapshotAttestationV1) -> NativeSnapshotHandleV1:
    return _generated_native_snapshot_handle_v1(attestation)


def reattest_fields(values: dict[str, object]) -> None:
    attestation = native_snapshot_publication_attestation_v1(
        documents=cast(tuple[NativeDocumentPublicationV1, ...], values["documents"]),
        import_manifest=cast(NativeImportManifestPublicationV1, values["import_manifest"]),
        root_document_key=cast(str, values["root_document_key"]),
        load_options=cast(LoadOptions, values["load_options"]),
        diagnostics=cast(tuple[NativeDiagnosticPublicationV1, ...], values["diagnostics"]),
        report=cast(NativeLoadReportPublicationV1, values["report"]),
        capability_bits=cast(int, values["capability_bits"]),
        root_table_sha256=cast(bytes, values["root_table_sha256"]),
        fingerprint_inputs_sha256=cast(bytes, values["fingerprint_inputs_sha256"]),
        source_manifest_sha256=cast(bytes, values["source_manifest_sha256"]),
        provenance_manifest_sha256=cast(bytes, values["provenance_manifest_sha256"]),
    )
    values["handle"] = generated_handle(attestation)


def publication_fields() -> dict[str, object]:
    document_key = "d1:" + "1" * 64
    ontology_iri = IRI("urn:handoff:ontology")
    ontology_id = OntologyID(ontology_iri)
    source_digest = hashlib.sha256(b"native handoff source").digest()
    fingerprint = Fingerprint("sha256", 2, hashlib.sha256(b"document").digest())
    provenance = freeze_native_provenance_publication_v1(
        DocumentProvenance(
            source_digest,
            DigestKind.EXACT_BYTES,
            21,
            21,
            ontology_iri,
            None,
            DocumentFormat.FUNCTIONAL,
            DetectionBasis.EXPLICIT,
            parser="pyowl_core.backends.native.fixture",
            backend="native",
        )
    )
    diagnostic = freeze_native_diagnostic_publication_v1(
        Diagnostic(
            "NATIVE_FIXTURE",
            Severity.INFO,
            "retained publication fixture",
            details={"fixture": True},
        )
    )
    document = NativeDocumentPublicationV1(
        document_key=document_key,
        ontology_id=ontology_id,
        document_iri=ontology_iri,
        direct_imports=(),
        provenance=provenance,
        document_fingerprint=fingerprint,
        diagnostics=(diagnostic,),
        ontology_annotation_count=0,
        axiom_count=1,
        extension_count=0,
        source_map_entry_count=0,
        origin_entry_count=1,
        rdf_mapping_conformant=None,
        rdf_mapping_report_sha256=None,
    )
    options = LoadOptions(
        imports=LoadOptions().imports,
        backend=BackendPreference.NATIVE,
    )
    manifest = freeze_native_import_manifest_publication_v1(
        ImportManifest(
            options.imports,
            options.offline,
            hashlib.sha256(b"resolver").digest(),
            (
                DocumentRecord(
                    document_key,
                    ontology_id,
                    ontology_iri,
                    source_digest,
                    fingerprint,
                    DocumentFormat.FUNCTIONAL,
                    DocumentStatus.ROOT,
                ),
            ),
            (),
        )
    )
    report = NativeLoadReportPublicationV1(
        backend="native",
        api_version=(0, 2),
        model_schema=2,
        document_count=1,
        total_source_bytes=21,
        effective_axiom_count=1,
        resolution_attempts=0,
        acquisition_cache_hits=0,
        document_cache_hits=0,
        timings=(("freeze_seconds", 0.001),),
        structural_fingerprint=Fingerprint("sha256", 2, hashlib.sha256(b"structural").digest()),
        logical_fingerprint=Fingerprint("sha256", 2, hashlib.sha256(b"logical").digest()),
        signature_fingerprint=Fingerprint("sha256", 2, hashlib.sha256(b"signature").digest()),
        owl2_dl_validated=False,
        owl2_dl_conforms=None,
        owl2_dl_report_sha256=None,
    )
    values: dict[str, object] = {
        "version": NATIVE_SNAPSHOT_PUBLICATION_VERSION,
        "ledger_sha256": NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256,
        "handle": None,
        "documents": (document,),
        "import_manifest": manifest,
        "root_document_key": document_key,
        "load_options": options,
        "diagnostics": (diagnostic,),
        "report": report,
        "capability_bits": 1 | 2 | 4 | 16,
        "root_table_sha256": hashlib.sha256(b"roots").digest(),
        "fingerprint_inputs_sha256": hashlib.sha256(b"fingerprint inputs").digest(),
        "source_manifest_sha256": hashlib.sha256(b"sources").digest(),
        "provenance_manifest_sha256": hashlib.sha256(b"provenance").digest(),
    }
    reattest_fields(values)
    return values
