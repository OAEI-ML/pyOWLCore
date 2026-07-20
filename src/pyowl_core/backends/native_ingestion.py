"""Fail-closed Python seam for WP16-owned native ingestion bindings."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, cast

from pyowl_core.exceptions import BackendProtocolError

from . import native

if TYPE_CHECKING:
    from pyowl_core.backends.native_handoff import NativeDiagnosticPublicationV1
    from pyowl_core.backends.native_handoff_v2 import NativeDiagnosticReferenceKindsV2
    from pyowl_core.cancellation import CancellationToken
    from pyowl_core.document.snapshot import OntologySnapshot


class NativeIngestionExtension(Protocol):
    INGESTION_FEATURES: tuple[str, ...]


class _RetainedStructuralExtension(NativeIngestionExtension, Protocol):
    _retain_structural_snapshot_v2: Callable[..., object]


def require_ingestion_binding(capability: str) -> NativeIngestionExtension:
    """Require a capability registered specifically by the ingestion seam."""

    extension = native.require(capability)
    if capability not in extension.INGESTION_FEATURES:
        raise BackendProtocolError(
            "native capability is not registered by the ingestion binding seam",
            code="NATIVE_INGESTION_REGISTRATION",
        )
    return cast(NativeIngestionExtension, extension)


def retain_forced_native_snapshot_v2(
    snapshot: OntologySnapshot,
    *,
    cancellation_token: CancellationToken | None = None,
) -> OntologySnapshot:
    """Promote one forced-native test load into the real typed V2 owner.

    The bridge remains deliberately unadvertised and explicitly test-gated
    while WP16's complete format/import/source-map matrix is unfinished.  An
    ineligible load stays on its existing Python storage; an eligible, opted-in
    load either publishes the retained owner or fails without fallback.
    """

    from pyowl_core.config import BackendPreference, DocumentFormat

    if os.environ.get("PYOWL_CORE_TEST_RETAINED_NATIVE_LOAD") != "1":
        return snapshot
    if (
        snapshot.load_options.backend is not BackendPreference.NATIVE
        or len(snapshot.documents) != 1
        or snapshot.root.provenance.backend != "native"
        or snapshot.root.provenance.format is not DocumentFormat.FUNCTIONAL
        or snapshot.load_options.preserve_source_map
        or snapshot.load_options.collect_provenance
        or snapshot.load_options.validate_owl2_dl
        or snapshot.root.rdf_mapping_report is not None
    ):
        return snapshot
    extension = native.require("parse-functional-v1")
    hook = getattr(extension, "_retain_structural_snapshot_v2", None)
    if not callable(hook):
        return snapshot
    return _publish_structural_snapshot_v2(
        snapshot,
        extension,
        cancellation_token,
    )


def _publish_structural_snapshot_v2(
    snapshot: OntologySnapshot,
    extension: native._Extension,
    cancellation_token: CancellationToken | None,
) -> OntologySnapshot:
    from dataclasses import fields

    from pyowl_core.backends.native_handoff import (
        NativeDocumentPublicationV1,
        NativeLoadReportPublicationV1,
        freeze_native_diagnostic_publication_v1,
        freeze_native_import_manifest_publication_v1,
        freeze_native_provenance_publication_v1,
    )
    from pyowl_core.backends.native_handoff_v2 import (
        NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2,
        NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2,
        NativeClosureFacadeCardinalitiesV2,
        NativeDiagnosticReferenceSidecarsV2,
        NativeDocumentFacadeCardinalitiesV2,
        NativeFacadeCardinalitySummaryV2,
        NativeFacadeCollectionV2,
        NativeFacadeScopeV2,
        NativeFingerprintEvidenceV2,
        NativeSignatureKindV2,
        _seal_native_snapshot_owner_v2,
        freeze_native_snapshot_publication_v2,
        native_snapshot_content_digests_v2,
        native_snapshot_publication_attestation_v2,
    )
    from pyowl_core.document.fingerprint import (
        document_fingerprint_bytes,
        logical_fingerprint_bytes,
        signature_fingerprint_bytes,
        snapshot_structural_fingerprint_bytes,
    )
    from pyowl_core.document.native_storage import ontology_snapshot_from_native_publication_v2
    from pyowl_core.document.snapshot import AxiomScope
    from pyowl_core.model import canonical_bytes

    document = snapshot.root
    record = snapshot.import_manifest.documents[0]
    raw_rows = (
        tuple(canonical_bytes(value) for value in document.ontology_annotations),
        tuple(canonical_bytes(value) for value in document.axioms),
        tuple(canonical_bytes(value) for value in document.extension_components),
    )
    effective_rows = (
        tuple(
            canonical_bytes(value)
            for value in snapshot.ontology_annotations(
                scope=AxiomScope.DOCUMENT,
                document_key=record.document_key,
            )
        ),
        tuple(
            canonical_bytes(value)
            for value in snapshot.iter_axioms(
                scope=AxiomScope.DOCUMENT,
                document_key=record.document_key,
            )
        ),
        tuple(
            canonical_bytes(value)
            for value in snapshot.iter_extensions(
                scope=AxiomScope.DOCUMENT,
                document_key=record.document_key,
            )
        ),
    )
    # Typed V2 currently has no anonymous re-scope sidecar.  Never publish a
    # raw-document owner whose roots would silently differ from Python.
    if raw_rows != effective_rows:
        return snapshot

    document_diagnostics = tuple(
        freeze_native_diagnostic_publication_v1(value) for value in document.diagnostics
    )
    documents = (
        NativeDocumentPublicationV1(
            document_key=record.document_key,
            ontology_id=document.ontology_id,
            document_iri=document.document_iri,
            direct_imports=document.direct_imports,
            provenance=freeze_native_provenance_publication_v1(document.provenance),
            document_fingerprint=document.document_fingerprint,
            diagnostics=document_diagnostics,
            ontology_annotation_count=len(raw_rows[0]),
            axiom_count=len(raw_rows[1]),
            extension_count=len(raw_rows[2]),
            source_map_entry_count=0,
            origin_entry_count=0,
            rdf_mapping_conformant=None,
            rdf_mapping_report_sha256=None,
        ),
    )
    diagnostics = tuple(
        freeze_native_diagnostic_publication_v1(value) for value in snapshot.diagnostics
    )
    report = NativeLoadReportPublicationV1(
        backend="native",
        api_version=snapshot.report.api_version,
        model_schema=snapshot.report.model_schema,
        document_count=1,
        total_source_bytes=snapshot.report.total_source_bytes,
        effective_axiom_count=len(effective_rows[1]),
        resolution_attempts=snapshot.report.resolution_attempts,
        acquisition_cache_hits=snapshot.report.acquisition_cache_hits,
        document_cache_hits=snapshot.report.document_cache_hits,
        timings=tuple(
            sorted(snapshot.report.timings.items(), key=lambda item: item[0].encode("utf-8"))
        ),
        structural_fingerprint=snapshot.structural_fingerprint,
        logical_fingerprint=snapshot.logical_fingerprint,
        signature_fingerprint=snapshot.signature_fingerprint,
        owl2_dl_validated=False,
        owl2_dl_conforms=None,
        owl2_dl_report_sha256=None,
    )
    capability_bits = 7
    import_manifest = freeze_native_import_manifest_publication_v1(snapshot.import_manifest)
    sidecars = NativeDiagnosticReferenceSidecarsV2(
        snapshot=tuple(_diagnostic_reference_kinds(value) for value in diagnostics),
        documents=(
            tuple(_diagnostic_reference_kinds(value) for value in document_diagnostics),
        ),
        import_edges=tuple(
            None
            if edge.diagnostic is None
            else _diagnostic_reference_kinds(edge.diagnostic)
            for edge in import_manifest.edges
        ),
    )
    facade_summary = NativeFacadeCardinalitySummaryV2(
        documents=(
            NativeDocumentFacadeCardinalitiesV2(
                document_key=record.document_key,
                effective_annotation_count=len(effective_rows[0]),
                effective_axiom_count=len(effective_rows[1]),
                effective_extension_count=len(effective_rows[2]),
                effective_origin_count=0,
                raw_source_prefix_count=0,
                rdf_unconsumed_triple_count=0,
                rdf_rule_count=0,
                rdf_diagnostic_count=0,
            ),
        ),
        closure=NativeClosureFacadeCardinalitiesV2(
            effective_annotation_count=len(effective_rows[0]),
            effective_axiom_count=len(effective_rows[1]),
            effective_extension_count=len(effective_rows[2]),
            effective_origin_count=0,
        ),
    )
    collections = {
        (collection, scope, ordinal, NativeSignatureKindV2.ALL, True): values
        for collection, values in zip(
            (
                NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS,
                NativeFacadeCollectionV2.AXIOMS,
                NativeFacadeCollectionV2.EXTENSIONS,
            ),
            effective_rows,
            strict=True,
        )
        for scope, ordinal in (
            (NativeFacadeScopeV2.DOCUMENT, 0),
            (NativeFacadeScopeV2.CLOSURE, None),
        )
    }
    preimages = (
        document_fingerprint_bytes(document),
        snapshot_structural_fingerprint_bytes(
            snapshot.import_manifest,
            (
                (
                    record.document_key,
                    snapshot.ontology_annotations(
                        scope=AxiomScope.DOCUMENT,
                        document_key=record.document_key,
                    ),
                    tuple(
                        snapshot.iter_axioms(
                            scope=AxiomScope.DOCUMENT,
                            document_key=record.document_key,
                        )
                    ),
                    tuple(
                        snapshot.iter_extensions(
                            scope=AxiomScope.DOCUMENT,
                            document_key=record.document_key,
                        )
                    ),
                ),
            ),
        ),
        logical_fingerprint_bytes(
            tuple(snapshot.iter_axioms()),
            tuple(snapshot.iter_extensions()),
        ),
        signature_fingerprint_bytes(snapshot.signature(), include_builtins=True),
    )
    fingerprints = (
        documents[0].document_fingerprint,
        report.structural_fingerprint,
        report.logical_fingerprint,
        report.signature_fingerprint,
    )
    evidence = tuple(
        NativeFingerprintEvidenceV2(
            tag=tag,
            document_key=record.document_key if tag == 1 else None,
            preimage_byte_length=len(preimage),
            fingerprint_schema=fingerprint.schema,
            digest=hashlib.sha256(preimage).digest(),
        )
        for tag, preimage, fingerprint in zip(
            (1, 2, 3, 4), preimages, fingerprints, strict=True
        )
    )
    max_facade_row_bytes = max(1, *(len(row) for roots in effective_rows for row in roots))
    content = native_snapshot_content_digests_v2(
        documents=documents,
        report=report,
        root_document_key=record.document_key,
        load_options=snapshot.load_options,
        capability_bits=capability_bits,
        collections=collections,
        fingerprint_evidence=evidence,
        fingerprint_preimages=preimages,
        owl2_dl_report_summary=None,
        facade_cardinality_summary=facade_summary,
    )
    attestation = native_snapshot_publication_attestation_v2(
        documents=documents,
        import_manifest=import_manifest,
        root_document_key=record.document_key,
        load_options=snapshot.load_options,
        diagnostics=diagnostics,
        diagnostic_reference_sidecars=sidecars,
        facade_cardinality_summary=facade_summary,
        report=report,
        capability_bits=capability_bits,
        content_digests=content,
        max_facade_row_bytes=max_facade_row_bytes,
        owl2_dl_report_summary=None,
    )
    config = native._encode_config(
        snapshot.load_options.limits,
        cancellation_token,
        verify=False,
    )
    with native._relay(extension, snapshot.load_options.limits, cancellation_token) as cancel:
        retain = cast(_RetainedStructuralExtension, extension)._retain_structural_snapshot_v2
        raw_owner = native._call(
            extension,
            lambda: retain(
                (raw_rows,),
                attestation,
                config,
                cancel,
            ),
        )
    values: dict[str, object] = {
        "version": NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2,
        "ledger_sha256": NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2,
        "handle": _seal_native_snapshot_owner_v2(raw_owner),
        "documents": documents,
        "import_manifest": import_manifest,
        "root_document_key": record.document_key,
        "load_options": snapshot.load_options,
        "diagnostics": diagnostics,
        "diagnostic_reference_sidecars": sidecars,
        "facade_cardinality_summary": facade_summary,
        "report": report,
        "capability_bits": capability_bits,
        "max_facade_row_bytes": max_facade_row_bytes,
        "owl2_dl_report_summary": None,
    }
    for field in fields(content):
        values[field.name] = getattr(content, field.name)
    publication = freeze_native_snapshot_publication_v2(values)
    return ontology_snapshot_from_native_publication_v2(publication)


def _diagnostic_reference_kinds(
    value: NativeDiagnosticPublicationV1,
) -> NativeDiagnosticReferenceKindsV2:
    from pyowl_core.backends.native_handoff_v2 import (
        NativeDiagnosticReferenceKindsV2,
        NativeDiagnosticReferenceKindV2,
    )

    return NativeDiagnosticReferenceKindsV2(
        document_reference_kind=(
            None if value.document_iri is None else NativeDiagnosticReferenceKindV2.TEXT
        ),
        import_chain_kinds=tuple(
            NativeDiagnosticReferenceKindV2.TEXT for _item in value.import_chain
        ),
    )


__all__ = ["NativeIngestionExtension", "require_ingestion_binding"]
