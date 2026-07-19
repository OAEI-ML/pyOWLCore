from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import fields
from typing import cast

from pyowl_core.backends.native_handoff import (
    NativeDiagnosticPublicationV1,
    NativeDocumentPublicationV1,
    NativeImportManifestPublicationV1,
    NativeLoadReportPublicationV1,
)
from pyowl_core.backends.native_handoff_v2 import (
    NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2,
    NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2,
    NativeClosureFacadeCardinalitiesV2,
    NativeDiagnosticReferenceKindsV2,
    NativeDiagnosticReferenceKindV2,
    NativeDiagnosticReferenceSidecarsV2,
    NativeDocumentFacadeCardinalitiesV2,
    NativeFacadeCardinalitySummaryV2,
    NativeFacadeCollectionV2,
    NativeFacadeScopeV2,
    NativeFingerprintEvidenceV2,
    NativeOriginRowV2,
    NativeOWL2DLReportSummaryV2,
    NativeSignatureKindV2,
    NativeSnapshotAttestationV2,
    NativeSnapshotContentDigestsV2,
    NativeSnapshotHandleV2,
    NativeSnapshotPublicationV2,
    _generated_native_snapshot_handle_v2,
    encode_native_auxiliary_row_v2,
    freeze_native_snapshot_publication_v2,
    native_snapshot_content_digests_v2,
    native_snapshot_publication_attestation_v2,
)
from pyowl_core.config import LoadOptions
from pyowl_core.model import IRI, Class, Declaration, canonical_bytes, structural_digest

from ._support import publication_fields

FixtureKey = tuple[
    NativeFacadeCollectionV2,
    NativeFacadeScopeV2,
    int | None,
    NativeSignatureKindV2,
    bool,
]


def fixture_collections() -> dict[FixtureKey, tuple[bytes, ...]]:
    axiom_value = Declaration(Class(IRI("urn:handoff:Class")))
    axiom = canonical_bytes(axiom_value)
    values = publication_fields()
    document = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])[0]
    _collection, origin = encode_native_auxiliary_row_v2(
        NativeOriginRowV2(
            digest=structural_digest(axiom_value),
            document_key=document.document_key,
            occurrence=0,
            span=None,
        ),
        max_row_bytes=source_load_row_budget(values),
    )
    return {
        (
            NativeFacadeCollectionV2.AXIOMS,
            NativeFacadeScopeV2.DOCUMENT,
            0,
            NativeSignatureKindV2.ALL,
            True,
        ): (axiom,),
        (
            NativeFacadeCollectionV2.AXIOMS,
            NativeFacadeScopeV2.CLOSURE,
            None,
            NativeSignatureKindV2.ALL,
            True,
        ): (axiom,),
        (
            NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            NativeFacadeScopeV2.DOCUMENT,
            0,
            NativeSignatureKindV2.ALL,
            True,
        ): (origin,),
        (
            NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            NativeFacadeScopeV2.CLOSURE,
            None,
            NativeSignatureKindV2.ALL,
            True,
        ): (origin,),
    }


def source_load_row_budget(values: Mapping[str, object] | None = None) -> int:
    source = publication_fields() if values is None else values
    options = cast(LoadOptions, source["load_options"])
    candidates = (
        options.limits.max_canonical_work,
        options.limits.max_index_bytes,
        options.limits.max_wire_bytes,
        options.limits.max_temporary_bytes,
    )
    return min(
        candidates
        if options.limits.max_memory_bytes is None
        else (*candidates, options.limits.max_memory_bytes)
    )


def fixture_max_row_bytes(
    collections: Mapping[FixtureKey, Sequence[bytes]] | None = None,
) -> int:
    selected = fixture_collections() if collections is None else collections
    return max(1, *(len(row) for rows in selected.values() for row in rows))


def fingerprint_evidence(
    values: Mapping[str, object] | None = None,
    preimages: tuple[bytes, ...] | None = None,
) -> tuple[NativeFingerprintEvidenceV2, ...]:
    source = publication_fields() if values is None else values
    documents = cast(tuple[NativeDocumentPublicationV1, ...], source["documents"])
    report = cast(NativeLoadReportPublicationV1, source["report"])
    selected_preimages = fingerprint_preimages(source) if preimages is None else preimages
    if len(selected_preimages) != len(documents) + 3:
        raise ValueError("fixture fingerprint preimages are not aligned")
    return (
        *(
            NativeFingerprintEvidenceV2(
                tag=1,
                document_key=document.document_key,
                preimage_byte_length=len(selected_preimages[ordinal]),
                fingerprint_schema=document.document_fingerprint.schema,
                digest=hashlib.sha256(selected_preimages[ordinal]).digest(),
            )
            for ordinal, document in enumerate(documents)
        ),
        NativeFingerprintEvidenceV2(
            tag=2,
            document_key=None,
            preimage_byte_length=len(selected_preimages[-3]),
            fingerprint_schema=report.structural_fingerprint.schema,
            digest=hashlib.sha256(selected_preimages[-3]).digest(),
        ),
        NativeFingerprintEvidenceV2(
            tag=3,
            document_key=None,
            preimage_byte_length=len(selected_preimages[-2]),
            fingerprint_schema=report.logical_fingerprint.schema,
            digest=hashlib.sha256(selected_preimages[-2]).digest(),
        ),
        NativeFingerprintEvidenceV2(
            tag=4,
            document_key=None,
            preimage_byte_length=len(selected_preimages[-1]),
            fingerprint_schema=report.signature_fingerprint.schema,
            digest=hashlib.sha256(selected_preimages[-1]).digest(),
        ),
    )


def fingerprint_preimages(
    values: Mapping[str, object] | None = None,
) -> tuple[bytes, ...]:
    source = publication_fields() if values is None else values
    documents = cast(tuple[NativeDocumentPublicationV1, ...], source["documents"])
    return (
        *(b"document" for _document in documents),
        b"structural",
        b"logical",
        b"signature",
    )


def content_digests(
    values: Mapping[str, object],
    collections: Mapping[FixtureKey, Sequence[bytes]],
    evidence: tuple[NativeFingerprintEvidenceV2, ...],
    raw_document_collections: Mapping[FixtureKey, Sequence[bytes]] | None = None,
    preimages: tuple[bytes, ...] | None = None,
) -> NativeSnapshotContentDigestsV2:
    selected_summary = cast(
        NativeFacadeCardinalitySummaryV2,
        values.get("facade_cardinality_summary")
        or facade_cardinality_summary(
            values,
            collections,
            raw_document_collections,
        ),
    )
    return native_snapshot_content_digests_v2(
        documents=cast(tuple[NativeDocumentPublicationV1, ...], values["documents"]),
        report=cast(NativeLoadReportPublicationV1, values["report"]),
        root_document_key=cast(str, values["root_document_key"]),
        load_options=cast(LoadOptions, values["load_options"]),
        capability_bits=cast(int, values["capability_bits"]),
        collections=collections,
        fingerprint_evidence=evidence,
        fingerprint_preimages=(fingerprint_preimages(values) if preimages is None else preimages),
        owl2_dl_report_summary=cast(
            NativeOWL2DLReportSummaryV2 | None,
            values.get("owl2_dl_report_summary"),
        ),
        facade_cardinality_summary=selected_summary,
        raw_document_collections=raw_document_collections,
    )


def facade_cardinality_summary(
    values: Mapping[str, object],
    collections: Mapping[FixtureKey, Sequence[bytes]],
    raw_document_collections: Mapping[FixtureKey, Sequence[bytes]] | None = None,
) -> NativeFacadeCardinalitySummaryV2:
    raw = collections if raw_document_collections is None else raw_document_collections
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])

    def count(
        source: Mapping[FixtureKey, Sequence[bytes]],
        collection: NativeFacadeCollectionV2,
        scope: NativeFacadeScopeV2,
        ordinal: int | None,
    ) -> int:
        return len(
            source.get(
                (
                    collection,
                    scope,
                    ordinal,
                    NativeSignatureKindV2.ALL,
                    True,
                ),
                (),
            )
        )

    return NativeFacadeCardinalitySummaryV2(
        documents=tuple(
            NativeDocumentFacadeCardinalitiesV2(
                document_key=document.document_key,
                effective_annotation_count=count(
                    collections,
                    NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS,
                    NativeFacadeScopeV2.DOCUMENT,
                    ordinal,
                ),
                effective_axiom_count=count(
                    collections,
                    NativeFacadeCollectionV2.AXIOMS,
                    NativeFacadeScopeV2.DOCUMENT,
                    ordinal,
                ),
                effective_extension_count=count(
                    collections,
                    NativeFacadeCollectionV2.EXTENSIONS,
                    NativeFacadeScopeV2.DOCUMENT,
                    ordinal,
                ),
                effective_origin_count=count(
                    collections,
                    NativeFacadeCollectionV2.ORIGIN_ENTRIES,
                    NativeFacadeScopeV2.DOCUMENT,
                    ordinal,
                ),
                raw_source_prefix_count=count(
                    raw,
                    NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES,
                    NativeFacadeScopeV2.DOCUMENT,
                    ordinal,
                ),
                rdf_unconsumed_triple_count=count(
                    collections,
                    NativeFacadeCollectionV2.RDF_UNCONSUMED_TRIPLES,
                    NativeFacadeScopeV2.DOCUMENT,
                    ordinal,
                ),
                rdf_rule_count=count(
                    collections,
                    NativeFacadeCollectionV2.RDF_RULE_IDS,
                    NativeFacadeScopeV2.DOCUMENT,
                    ordinal,
                ),
                rdf_diagnostic_count=count(
                    collections,
                    NativeFacadeCollectionV2.RDF_DIAGNOSTICS,
                    NativeFacadeScopeV2.DOCUMENT,
                    ordinal,
                ),
            )
            for ordinal, document in enumerate(documents)
        ),
        closure=NativeClosureFacadeCardinalitiesV2(
            effective_annotation_count=count(
                collections,
                NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS,
                NativeFacadeScopeV2.CLOSURE,
                None,
            ),
            effective_axiom_count=count(
                collections,
                NativeFacadeCollectionV2.AXIOMS,
                NativeFacadeScopeV2.CLOSURE,
                None,
            ),
            effective_extension_count=count(
                collections,
                NativeFacadeCollectionV2.EXTENSIONS,
                NativeFacadeScopeV2.CLOSURE,
                None,
            ),
            effective_origin_count=count(
                collections,
                NativeFacadeCollectionV2.ORIGIN_ENTRIES,
                NativeFacadeScopeV2.CLOSURE,
                None,
            ),
        ),
    )


def diagnostic_reference_sidecars(
    values: Mapping[str, object],
) -> NativeDiagnosticReferenceSidecarsV2:
    def kinds(diagnostic: NativeDiagnosticPublicationV1) -> NativeDiagnosticReferenceKindsV2:
        return NativeDiagnosticReferenceKindsV2(
            document_reference_kind=(
                None if diagnostic.document_iri is None else NativeDiagnosticReferenceKindV2.TEXT
            ),
            import_chain_kinds=tuple(
                NativeDiagnosticReferenceKindV2.TEXT for _ in diagnostic.import_chain
            ),
        )

    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    manifest = cast(NativeImportManifestPublicationV1, values["import_manifest"])
    diagnostics = cast(tuple[NativeDiagnosticPublicationV1, ...], values["diagnostics"])
    return NativeDiagnosticReferenceSidecarsV2(
        snapshot=tuple(kinds(item) for item in diagnostics),
        documents=tuple(
            tuple(kinds(item) for item in document.diagnostics) for document in documents
        ),
        import_edges=tuple(
            None if edge.diagnostic is None else kinds(edge.diagnostic) for edge in manifest.edges
        ),
    )


def attestation(
    values: Mapping[str, object] | None = None,
    *,
    max_facade_row_bytes: int | None = None,
    collections: Mapping[FixtureKey, Sequence[bytes]] | None = None,
    evidence: tuple[NativeFingerprintEvidenceV2, ...] | None = None,
    preimages: tuple[bytes, ...] | None = None,
    raw_document_collections: Mapping[FixtureKey, Sequence[bytes]] | None = None,
) -> NativeSnapshotAttestationV2:
    source = publication_fields() if values is None else values
    selected_collections = fixture_collections() if collections is None else collections
    selected_evidence = fingerprint_evidence(source) if evidence is None else evidence
    selected_preimages = fingerprint_preimages(source) if preimages is None else preimages
    selected_max = fixture_max_row_bytes() if max_facade_row_bytes is None else max_facade_row_bytes
    return native_snapshot_publication_attestation_v2(
        documents=cast(tuple[NativeDocumentPublicationV1, ...], source["documents"]),
        import_manifest=cast(NativeImportManifestPublicationV1, source["import_manifest"]),
        root_document_key=cast(str, source["root_document_key"]),
        load_options=cast(LoadOptions, source["load_options"]),
        diagnostics=cast(tuple[NativeDiagnosticPublicationV1, ...], source["diagnostics"]),
        report=cast(NativeLoadReportPublicationV1, source["report"]),
        capability_bits=cast(int, source["capability_bits"]),
        diagnostic_reference_sidecars=cast(
            NativeDiagnosticReferenceSidecarsV2,
            source.get("diagnostic_reference_sidecars") or diagnostic_reference_sidecars(source),
        ),
        facade_cardinality_summary=cast(
            NativeFacadeCardinalitySummaryV2,
            source.get("facade_cardinality_summary")
            or facade_cardinality_summary(
                source,
                selected_collections,
                raw_document_collections,
            ),
        ),
        content_digests=content_digests(
            source,
            selected_collections,
            selected_evidence,
            raw_document_collections,
            selected_preimages,
        ),
        max_facade_row_bytes=selected_max,
        owl2_dl_report_summary=cast(
            NativeOWL2DLReportSummaryV2 | None,
            source.get("owl2_dl_report_summary"),
        ),
    )


def generated_handle(
    selected_attestation: NativeSnapshotAttestationV2,
    collections: Mapping[FixtureKey, Sequence[bytes]] | None = None,
    evidence: tuple[NativeFingerprintEvidenceV2, ...] | None = None,
    raw_document_collections: Mapping[FixtureKey, Sequence[bytes]] | None = None,
    *,
    values: Mapping[str, object] | None = None,
    preimages: tuple[bytes, ...] | None = None,
) -> NativeSnapshotHandleV2:
    source = publication_fields() if values is None else values
    selected_collections = fixture_collections() if collections is None else collections
    selected_evidence = fingerprint_evidence(source) if evidence is None else evidence
    selected_preimages = fingerprint_preimages(source) if preimages is None else preimages
    selected_summary = cast(
        NativeFacadeCardinalitySummaryV2,
        source.get("facade_cardinality_summary")
        or facade_cardinality_summary(
            source,
            selected_collections,
            raw_document_collections,
        ),
    )
    return _generated_native_snapshot_handle_v2(
        selected_attestation,
        selected_collections,
        selected_evidence,
        selected_preimages,
        documents=cast(tuple[NativeDocumentPublicationV1, ...], source["documents"]),
        report=cast(NativeLoadReportPublicationV1, source["report"]),
        root_document_key=cast(str, source["root_document_key"]),
        load_options=cast(LoadOptions, source["load_options"]),
        capability_bits=cast(int, source["capability_bits"]),
        owl2_dl_report_summary=cast(
            NativeOWL2DLReportSummaryV2 | None,
            source.get("owl2_dl_report_summary"),
        ),
        facade_cardinality_summary=selected_summary,
        raw_document_collections=raw_document_collections,
    )


def publication(
    collections: Mapping[FixtureKey, Sequence[bytes]] | None = None,
    *,
    values: Mapping[str, object] | None = None,
    preimages: tuple[bytes, ...] | None = None,
    raw_document_collections: Mapping[FixtureKey, Sequence[bytes]] | None = None,
) -> NativeSnapshotPublicationV2:
    selected_values = dict(publication_fields() if values is None else values)
    selected_collections = fixture_collections() if collections is None else collections
    selected_values.setdefault("owl2_dl_report_summary", None)
    selected_values.setdefault(
        "diagnostic_reference_sidecars",
        diagnostic_reference_sidecars(selected_values),
    )
    selected_values.setdefault(
        "facade_cardinality_summary",
        facade_cardinality_summary(
            selected_values,
            selected_collections,
            raw_document_collections,
        ),
    )
    selected_preimages = fingerprint_preimages(selected_values) if preimages is None else preimages
    selected_evidence = fingerprint_evidence(selected_values, selected_preimages)
    selected_content = content_digests(
        selected_values,
        selected_collections,
        selected_evidence,
        raw_document_collections,
        selected_preimages,
    )
    max_facade_row_bytes = max(
        fixture_max_row_bytes(selected_collections),
        fixture_max_row_bytes(raw_document_collections)
        if raw_document_collections is not None
        else 1,
    )
    selected_attestation = attestation(
        selected_values,
        max_facade_row_bytes=max_facade_row_bytes,
        collections=selected_collections,
        evidence=selected_evidence,
        preimages=selected_preimages,
        raw_document_collections=raw_document_collections,
    )
    selected_values["version"] = NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2
    selected_values["ledger_sha256"] = NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2
    selected_values["handle"] = generated_handle(
        selected_attestation,
        selected_collections,
        selected_evidence,
        raw_document_collections,
        values=selected_values,
        preimages=selected_preimages,
    )
    selected_values["max_facade_row_bytes"] = max_facade_row_bytes
    for item in fields(selected_content):
        selected_values[item.name] = getattr(selected_content, item.name)
    return freeze_native_snapshot_publication_v2(selected_values)


__all__ = [
    "FixtureKey",
    "attestation",
    "content_digests",
    "diagnostic_reference_sidecars",
    "facade_cardinality_summary",
    "fingerprint_evidence",
    "fingerprint_preimages",
    "fixture_collections",
    "fixture_max_row_bytes",
    "generated_handle",
    "publication",
    "source_load_row_budget",
]
