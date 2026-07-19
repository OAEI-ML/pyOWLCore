from __future__ import annotations

import copy
import hashlib
import os
import pickle
from dataclasses import replace
from typing import Any, cast

import pytest

from pyowl_core.backends.native_handoff import (
    NativeDiagnosticPublicationV1,
    NativeDocumentPublicationV1,
    NativeLoadReportPublicationV1,
)
from pyowl_core.backends.native_handoff_v2 import (
    NATIVE_OWL2_DL_REPORT_DOMAIN_V2,
    NATIVE_RDF_MAPPING_REPORT_DOMAIN_V2,
    NativeAuxiliaryRowV2,
    NativeDiagnosticReferenceKindsV2,
    NativeDiagnosticReferenceKindV2,
    NativeFacadeCollectionV2,
    NativeFacadeScopeV2,
    NativeOWL2DLIssueRowV2,
    NativeOWL2DLReportSummaryV2,
    NativeOWL2DLRoleEdgeRowV2,
    NativeOWL2DLStructuralIssueRowV2,
    NativeRDFDiagnosticRowV2,
    NativeRDFReportHeaderRowV2,
    NativeRDFRuleRowV2,
    NativeRDFTripleRowV2,
    NativeSignatureKindV2,
    NativeSnapshotPublicationV2,
    encode_native_auxiliary_row_v2,
)
from pyowl_core.config import LoadOptions
from pyowl_core.diagnostics import Diagnostic, Severity, SourceSpan
from pyowl_core.document.native_storage import (
    ontology_snapshot_from_native_publication_v2,
)
from pyowl_core.document.provenance import RDFMappingReport, RDFTripleEvidence
from pyowl_core.exceptions import ClosedSnapshotError
from pyowl_core.model import (
    IRI,
    ObjectInverseOf,
    ObjectProperty,
    OWL2DLReport,
    RoleAnalysis,
    RoleEdge,
    StructuralReport,
    ValidationIssue,
    ValidationSeverity,
    canonical_bytes,
)

from ..publication_handoff._support import publication_fields
from ..publication_handoff._support_v2 import (
    FixtureKey,
    fixture_collections,
    publication,
    source_load_row_budget,
)


def _u64(value: int) -> bytes:
    return value.to_bytes(8, "little")


def _frame(value: bytes) -> bytes:
    return _u64(len(value)) + value


def _text(value: str) -> bytes:
    return _frame(value.encode("utf-8"))


def _key(
    collection: NativeFacadeCollectionV2,
    scope: NativeFacadeScopeV2,
    ordinal: int | None,
) -> FixtureKey:
    return (
        collection,
        scope,
        ordinal,
        NativeSignatureKindV2.ALL,
        True,
    )


def _encode(value: NativeAuxiliaryRowV2, *, bound: int) -> bytes:
    return encode_native_auxiliary_row_v2(
        value,
        max_row_bytes=bound,
    )[1]


def _rdf_report_digest(
    document_key: str,
    header: bytes,
    triples: tuple[bytes, ...],
    rules: tuple[bytes, ...],
    diagnostics: tuple[bytes, ...],
) -> bytes:
    body = (
        _text(document_key)
        + _frame(header)
        + _u64(len(triples))
        + b"".join(_frame(row) for row in triples)
        + _u64(len(rules))
        + b"".join(_frame(row) for row in rules)
        + _u64(len(diagnostics))
        + b"".join(_frame(row) for row in diagnostics)
    )
    return hashlib.sha256(
        NATIVE_RDF_MAPPING_REPORT_DOMAIN_V2.encode("ascii") + b"\x00" + body
    ).digest()


def _rdf_publication() -> NativeSnapshotPublicationV2:
    values = publication_fields()
    bound = source_load_row_budget(values)
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    header = _encode(
        NativeRDFReportHeaderRowV2(
            conformant=False,
            consumed_triples=2,
            total_triples=4,
        ),
        bound=bound,
    )
    triples = tuple(
        _encode(
            NativeRDFTripleRowV2(subject=subject, predicate="p", object="o"),
            bound=bound,
        )
        for subject in ("z-producer-first", "a-producer-second")
    )
    rules = tuple(_encode(NativeRDFRuleRowV2(rule_id=rule), bound=bound) for rule in ("R1", "R2"))
    diagnostic_scalar = NativeDiagnosticPublicationV1(
        code="RDF_UNCONSUMED",
        severity="warning",
        message="unconsumed RDF remained",
        document_iri="urn:rdf:document",
        byte_start=3,
        byte_end=7,
        line_start=1,
        column_start=4,
        line_end=1,
        column_end=8,
        import_chain=("urn:rdf:import",),
        details=(("rule", "R1"),),
    )
    diagnostic_kinds = NativeDiagnosticReferenceKindsV2(
        document_reference_kind=NativeDiagnosticReferenceKindV2.IRI,
        import_chain_kinds=(NativeDiagnosticReferenceKindV2.TEXT,),
    )
    diagnostic_rows = (
        _encode(
            NativeRDFDiagnosticRowV2(
                diagnostic=diagnostic_scalar,
                reference_kinds=diagnostic_kinds,
            ),
            bound=bound,
        ),
    )
    digest = _rdf_report_digest(
        documents[0].document_key,
        header,
        triples,
        rules,
        diagnostic_rows,
    )
    values["documents"] = (
        replace(
            documents[0],
            rdf_mapping_conformant=False,
            rdf_mapping_report_sha256=digest,
        ),
    )
    values["capability_bits"] = cast(int, values["capability_bits"]) | 32
    collections = dict(fixture_collections())
    for collection, rows in (
        (NativeFacadeCollectionV2.RDF_REPORT_HEADER, (header,)),
        (NativeFacadeCollectionV2.RDF_UNCONSUMED_TRIPLES, triples),
        (NativeFacadeCollectionV2.RDF_RULE_IDS, rules),
        (NativeFacadeCollectionV2.RDF_DIAGNOSTICS, diagnostic_rows),
    ):
        collections[_key(collection, NativeFacadeScopeV2.DOCUMENT, 0)] = rows
    return publication(collections, values=values)


def _owl_report_digest(
    summary: NativeOWL2DLReportSummaryV2,
    sections: tuple[tuple[bytes, ...], ...],
) -> bytes:
    counts = (
        summary.structural_issue_count,
        summary.issue_count,
        summary.role_property_count,
        summary.role_hierarchy_count,
        summary.role_composite_count,
        summary.role_non_simple_count,
    )
    body = bytearray(
        _u64(summary.structural_values_checked)
        + bytes((int(summary.structural_complete), int(summary.report_complete)))
        + b"".join(_u64(value) for value in counts)
    )
    for tag, rows in enumerate(sections, 1):
        body.extend(bytes((tag,)) + _u64(len(rows)))
        body.extend(b"".join(_frame(row) for row in rows))
    return hashlib.sha256(NATIVE_OWL2_DL_REPORT_DOMAIN_V2.encode("ascii") + b"\x00" + body).digest()


def _owl_publication() -> tuple[NativeSnapshotPublicationV2, ObjectProperty, ObjectInverseOf]:
    values = publication_fields()
    bound = source_load_row_budget(values)
    property_a = ObjectProperty(IRI("urn:owl2dl:property:a"))
    property_b = ObjectInverseOf(ObjectProperty(IRI("urn:owl2dl:property:b")))
    structural_rows = (
        _encode(
            NativeOWL2DLStructuralIssueRowV2(
                code="Z_PRODUCER_FIRST",
                severity=ValidationSeverity.WARNING,
                message="first structural issue",
                constructor="ObjectProperty",
            ),
            bound=bound,
        ),
        _encode(
            NativeOWL2DLStructuralIssueRowV2(
                code="A_PRODUCER_SECOND",
                severity=ValidationSeverity.INFO,
                message="second structural issue",
                constructor=None,
            ),
            bound=bound,
        ),
    )
    issue_rows = (
        _encode(
            NativeOWL2DLIssueRowV2(
                code="Z_PROFILE_FIRST",
                severity=ValidationSeverity.INFO,
                message="first profile issue",
                constructor=None,
            ),
            bound=bound,
        ),
        _encode(
            NativeOWL2DLIssueRowV2(
                code="A_PROFILE_SECOND",
                severity=ValidationSeverity.WARNING,
                message="second profile issue",
                constructor="Declaration",
            ),
            bound=bound,
        ),
    )
    property_a_row = canonical_bytes(property_a)
    property_b_row = canonical_bytes(property_b)
    hierarchy_rows = (
        _encode(
            NativeOWL2DLRoleEdgeRowV2(
                sub_property=property_a_row,
                super_property=property_b_row,
            ),
            bound=bound,
        ),
    )
    sections = (
        structural_rows,
        issue_rows,
        (property_a_row,),
        hierarchy_rows,
        (property_b_row,),
        (property_a_row,),
    )
    summary = NativeOWL2DLReportSummaryV2(
        structural_values_checked=9,
        structural_complete=True,
        report_complete=True,
        structural_issue_count=2,
        issue_count=2,
        role_property_count=1,
        role_hierarchy_count=1,
        role_composite_count=1,
        role_non_simple_count=1,
    )
    report = cast(NativeLoadReportPublicationV1, values["report"])
    values["load_options"] = replace(
        cast(LoadOptions, values["load_options"]),
        validate_owl2_dl=True,
    )
    values["report"] = replace(
        report,
        owl2_dl_validated=True,
        owl2_dl_conforms=True,
        owl2_dl_report_sha256=_owl_report_digest(summary, sections),
    )
    values["owl2_dl_report_summary"] = summary
    collections = dict(fixture_collections())
    for collection, rows in zip(
        (
            NativeFacadeCollectionV2.OWL2_DL_STRUCTURAL_ISSUES,
            NativeFacadeCollectionV2.OWL2_DL_ISSUES,
            NativeFacadeCollectionV2.OWL2_DL_ROLE_PROPERTIES,
            NativeFacadeCollectionV2.OWL2_DL_ROLE_HIERARCHY,
            NativeFacadeCollectionV2.OWL2_DL_ROLE_COMPOSITE,
            NativeFacadeCollectionV2.OWL2_DL_ROLE_NON_SIMPLE,
        ),
        sections,
        strict=True,
    ):
        collections[_key(collection, NativeFacadeScopeV2.CLOSURE, None)] = rows
    return publication(collections, values=values), property_a, property_b


def test_rdf_report_is_page_free_until_a_row_backed_field_is_requested() -> None:
    published = _rdf_publication()
    snapshot = ontology_snapshot_from_native_publication_v2(published)
    report = snapshot.root.rdf_mapping_report
    assert isinstance(report, RDFMappingReport)
    assert report.conformant is False
    assert "storage='native'" in repr(report)
    assert published.handle._facade_counters_v2().page_requests == 0
    assert snapshot._native_python_counters().auxiliary_rows_decoded == 0  # type: ignore[attr-defined]

    assert report.consumed_triples == 2
    assert report.total_triples == 4
    assert published.handle._facade_counters_v2().page_requests == 1
    assert report.unconsumed == (
        RDFTripleEvidence("z-producer-first", "p", "o"),
        RDFTripleEvidence("a-producer-second", "p", "o"),
    )
    assert report.rule_ids == ("R1", "R2")
    assert report.diagnostics == (
        Diagnostic(
            "RDF_UNCONSUMED",
            Severity.WARNING,
            "unconsumed RDF remained",
            IRI("urn:rdf:document"),
            SourceSpan(3, 7, 1, 4, 1, 8),
            ("urn:rdf:import",),
            {"rule": "R1"},
        ),
    )
    assert published.handle._facade_counters_v2().page_requests == 4
    counters = snapshot._native_python_counters()  # type: ignore[attr-defined]
    assert counters.auxiliary_rows_decoded == 6
    assert counters.cache_current_entries == 6


def test_rdf_report_matches_eager_value_and_remains_close_aware() -> None:
    snapshot = ontology_snapshot_from_native_publication_v2(_rdf_publication())
    report = cast(RDFMappingReport, snapshot.root.rdf_mapping_report)
    expected = RDFMappingReport(
        False,
        2,
        4,
        (
            RDFTripleEvidence("z-producer-first", "p", "o"),
            RDFTripleEvidence("a-producer-second", "p", "o"),
        ),
        ("R1", "R2"),
        report.diagnostics,
    )
    assert report == expected
    assert expected == report
    assert hash(report) == hash(expected)
    assert copy.copy(report) is report
    assert copy.deepcopy(report) is report
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(report)
    with pytest.raises(TypeError, match="cannot be replaced"):
        replace(report, conformant=True)

    snapshot.root.close()  # type: ignore[attr-defined]
    assert report.conformant is False
    assert "state='closed'" in repr(report)
    with pytest.raises(ClosedSnapshotError):
        _ = report.consumed_triples
    with pytest.raises(ClosedSnapshotError):
        _ = report.unconsumed


def test_rdf_header_scalar_cache_resets_after_a_process_fork_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyowl_core.document import native_storage

    snapshot = ontology_snapshot_from_native_publication_v2(_rdf_publication())
    report = cast(RDFMappingReport, snapshot.root.rdf_mapping_report)
    assert report.consumed_triples == 2
    before = snapshot._native_python_counters()  # type: ignore[attr-defined]
    assert before.auxiliary_rows_decoded == 1
    current_pid = os.getpid()

    monkeypatch.setattr(cast(Any, native_storage).os, "getpid", lambda: current_pid + 1)
    assert report.consumed_triples == 2
    after = snapshot._native_python_counters()  # type: ignore[attr-defined]
    assert after.auxiliary_rows_decoded == before.auxiliary_rows_decoded + 1
    assert after.cache_current_entries == 1
    assert after.cache_misses == 1


def test_rdf_report_follows_its_independent_document_owner() -> None:
    snapshot = ontology_snapshot_from_native_publication_v2(_rdf_publication())
    document = snapshot.root
    report = cast(RDFMappingReport, document.rdf_mapping_report)

    snapshot.close()  # type: ignore[attr-defined]
    assert snapshot.closed  # type: ignore[attr-defined]
    assert not document.closed  # type: ignore[attr-defined]
    assert report.consumed_triples == 2
    assert tuple(item.subject for item in report.unconsumed) == (
        "z-producer-first",
        "a-producer-second",
    )


def test_owl_report_summary_is_page_free_and_report_rows_preserve_producer_order() -> None:
    published, property_a, property_b = _owl_publication()
    snapshot = ontology_snapshot_from_native_publication_v2(published)
    report = snapshot.owl2_dl_report
    assert isinstance(report, OWL2DLReport)
    assert report is snapshot.report.owl2_dl_report
    assert report.complete and report.conforms
    assert report.structural.values_checked == 9
    assert report.structural.complete
    assert "storage='native'" in repr(report)
    assert "storage='native'" in repr(report.structural)
    assert "storage='native'" in repr(report.roles)
    assert published.handle._facade_counters_v2().page_requests == 0

    structural_issues = report.structural.issues
    issues = report.issues
    assert tuple(item.code for item in structural_issues) == (
        "Z_PRODUCER_FIRST",
        "A_PRODUCER_SECOND",
    )
    assert tuple(item.code for item in issues) == (
        "Z_PROFILE_FIRST",
        "A_PROFILE_SECOND",
    )
    assert report.roles.properties == (property_a,)
    hierarchy = report.roles.hierarchy
    assert hierarchy == (RoleEdge(property_a, property_b),)
    assert report.roles.composite == (property_b,)
    assert report.roles.non_simple == (property_a,)
    native_counters = published.handle._facade_counters_v2()
    python_counters = snapshot._native_python_counters()  # type: ignore[attr-defined]
    assert native_counters.page_requests == 6
    assert native_counters.rows_emitted == 8
    assert python_counters.auxiliary_rows_decoded == 5
    assert python_counters.model_rows_materialized == 2
    assert python_counters.cache_misses == (
        python_counters.auxiliary_rows_decoded + python_counters.model_rows_materialized
    )

    repeated_hierarchy = report.roles.hierarchy
    assert repeated_hierarchy[0].sub_property is hierarchy[0].sub_property
    assert repeated_hierarchy[0].super_property is hierarchy[0].super_property
    repeated_native_counters = published.handle._facade_counters_v2()
    repeated_python_counters = snapshot._native_python_counters()  # type: ignore[attr-defined]
    assert repeated_native_counters.page_requests == native_counters.page_requests + 1
    assert repeated_native_counters.rows_emitted == native_counters.rows_emitted + 1
    assert repeated_python_counters.auxiliary_rows_decoded == (
        python_counters.auxiliary_rows_decoded
    )
    assert repeated_python_counters.model_rows_materialized == (
        python_counters.model_rows_materialized
    )
    assert repeated_python_counters.cache_misses == python_counters.cache_misses
    assert repeated_python_counters.cache_hits == python_counters.cache_hits + 1


def test_owl_report_matches_eager_value_and_closure_owner_lifecycle() -> None:
    published, property_a, property_b = _owl_publication()
    snapshot = ontology_snapshot_from_native_publication_v2(published)
    report = cast(OWL2DLReport, snapshot.owl2_dl_report)
    expected = OWL2DLReport(
        StructuralReport(
            (
                ValidationIssue(
                    "Z_PRODUCER_FIRST",
                    ValidationSeverity.WARNING,
                    "first structural issue",
                    "ObjectProperty",
                ),
                ValidationIssue(
                    "A_PRODUCER_SECOND",
                    ValidationSeverity.INFO,
                    "second structural issue",
                ),
            ),
            9,
            True,
        ),
        (
            ValidationIssue(
                "Z_PROFILE_FIRST",
                ValidationSeverity.INFO,
                "first profile issue",
            ),
            ValidationIssue(
                "A_PROFILE_SECOND",
                ValidationSeverity.WARNING,
                "second profile issue",
                "Declaration",
            ),
        ),
        RoleAnalysis(
            (property_a,),
            (RoleEdge(property_a, property_b),),
            (property_b,),
            (property_a,),
        ),
        True,
    )
    assert report == expected
    assert expected == report
    assert hash(report) == hash(expected)
    counters = snapshot._native_python_counters()  # type: ignore[attr-defined]
    assert counters.auxiliary_rows_decoded == 5
    assert counters.model_rows_materialized == 2
    assert copy.copy(report) is report
    assert copy.deepcopy(report) is report
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(report)
    with pytest.raises(TypeError, match="cannot be replaced"):
        replace(report, complete=False)

    snapshot.close()  # type: ignore[attr-defined]
    assert report.complete and report.conforms
    assert report.structural.values_checked == 9
    assert "state='closed'" in repr(report)
    with pytest.raises(ClosedSnapshotError):
        _ = report.issues
    with pytest.raises(ClosedSnapshotError):
        _ = report.structural.issues
    with pytest.raises(ClosedSnapshotError):
        _ = report.roles.properties
