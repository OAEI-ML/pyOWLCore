from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import cast

import pytest

from pyowl_core.backends import native_handoff_v2
from pyowl_core.backends.native_handoff import NativeLoadReportPublicationV1
from pyowl_core.backends.native_handoff_v2 import (
    NATIVE_OWL2_DL_REPORT_DOMAIN_V2,
    NativeFacadeCollectionV2,
    NativeFacadePageRequestV2,
    NativeFacadeScopeV2,
    NativeOWL2DLIssueRowV2,
    NativeOWL2DLReportSummaryV2,
    NativeOWL2DLRoleEdgeRowV2,
    NativeOWL2DLStructuralIssueRowV2,
    NativeSignatureKindV2,
    decode_native_auxiliary_row_v2,
    encode_native_auxiliary_row_v2,
)
from pyowl_core.config import LoadOptions
from pyowl_core.exceptions import BackendProtocolError
from pyowl_core.model import (
    IRI,
    ObjectInverseOf,
    ObjectProperty,
    ValidationSeverity,
    canonical_bytes,
    decode_canonical,
)

from ._support import publication_fields
from ._support_v2 import (
    FixtureKey,
    content_digests,
    fingerprint_evidence,
    fixture_collections,
    publication,
    source_load_row_budget,
)


def _u64(value: int) -> bytes:
    return value.to_bytes(8, "little")


def _frame(value: bytes) -> bytes:
    return _u64(len(value)) + value


def _report_digest(
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


def _validated_fixture() -> tuple[
    dict[str, object],
    dict[FixtureKey, tuple[bytes, ...]],
    NativeOWL2DLReportSummaryV2,
]:
    values = publication_fields()
    bound = source_load_row_budget(values)
    property_a = canonical_bytes(ObjectProperty(IRI("urn:owl2dl:property:a")))
    property_b = canonical_bytes(ObjectInverseOf(ObjectProperty(IRI("urn:owl2dl:property:b"))))
    _structural_collection, structural_issue = encode_native_auxiliary_row_v2(
        NativeOWL2DLStructuralIssueRowV2(
            code="STRUCTURAL_WARNING",
            severity=ValidationSeverity.WARNING,
            message="a structural warning",
            constructor="ObjectProperty",
        ),
        max_row_bytes=bound,
    )
    _issue_collection, issue = encode_native_auxiliary_row_v2(
        NativeOWL2DLIssueRowV2(
            code="PROFILE_NOTE",
            severity=ValidationSeverity.INFO,
            message="a profile note",
            constructor=None,
        ),
        max_row_bytes=bound,
    )
    _edge_collection, edge = encode_native_auxiliary_row_v2(
        NativeOWL2DLRoleEdgeRowV2(
            sub_property=property_a,
            super_property=property_b,
        ),
        max_row_bytes=bound,
    )
    sections = (
        (structural_issue,),
        (issue,),
        (property_a,),
        (edge,),
        (property_b,),
        (property_a,),
    )
    summary = NativeOWL2DLReportSummaryV2(
        structural_values_checked=7,
        structural_complete=True,
        report_complete=True,
        structural_issue_count=1,
        issue_count=1,
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
        owl2_dl_report_sha256=_report_digest(summary, sections),
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
        collections[
            (
                collection,
                NativeFacadeScopeV2.CLOSURE,
                None,
                NativeSignatureKindV2.ALL,
                True,
            )
        ] = rows
    return values, collections, summary


def _producer_issue_fixture() -> tuple[
    dict[str, object],
    dict[FixtureKey, tuple[bytes, ...]],
    dict[NativeFacadeCollectionV2, tuple[bytes, ...]],
]:
    values, collections, summary = _validated_fixture()
    bound = source_load_row_budget(values)
    encoded_rows: dict[NativeFacadeCollectionV2, tuple[bytes, ...]] = {}
    for collection, row_type, code in (
        (
            NativeFacadeCollectionV2.OWL2_DL_STRUCTURAL_ISSUES,
            NativeOWL2DLStructuralIssueRowV2,
            "A_STRUCTURAL_PRODUCER_SECOND",
        ),
        (
            NativeFacadeCollectionV2.OWL2_DL_ISSUES,
            NativeOWL2DLIssueRowV2,
            "A_PROFILE_PRODUCER_SECOND",
        ),
    ):
        key = next(key for key in collections if key[0] is collection)
        _encoded_collection, second = encode_native_auxiliary_row_v2(
            row_type(
                code=code,
                severity=ValidationSeverity.INFO,
                message="producer second",
                constructor=None,
            ),
            max_row_bytes=bound,
        )
        rows = (collections[key][0], second, second)
        collections[key] = rows
        encoded_rows[collection] = rows

    summary = replace(summary, structural_issue_count=3, issue_count=3)
    sections = tuple(
        collections[
            (
                collection,
                NativeFacadeScopeV2.CLOSURE,
                None,
                NativeSignatureKindV2.ALL,
                True,
            )
        ]
        for collection in (
            NativeFacadeCollectionV2.OWL2_DL_STRUCTURAL_ISSUES,
            NativeFacadeCollectionV2.OWL2_DL_ISSUES,
            NativeFacadeCollectionV2.OWL2_DL_ROLE_PROPERTIES,
            NativeFacadeCollectionV2.OWL2_DL_ROLE_HIERARCHY,
            NativeFacadeCollectionV2.OWL2_DL_ROLE_COMPOSITE,
            NativeFacadeCollectionV2.OWL2_DL_ROLE_NON_SIMPLE,
        )
    )
    values["owl2_dl_report_summary"] = summary
    values["report"] = replace(
        cast(NativeLoadReportPublicationV1, values["report"]),
        owl2_dl_report_sha256=_report_digest(summary, sections),
    )
    return values, collections, encoded_rows


def test_owl2_dl_auxiliary_codecs_round_trip_exact_rows() -> None:
    values, collections, _summary = _validated_fixture()
    bound = source_load_row_budget(values)
    expected_types = (
        (
            NativeFacadeCollectionV2.OWL2_DL_STRUCTURAL_ISSUES,
            NativeOWL2DLStructuralIssueRowV2,
        ),
        (NativeFacadeCollectionV2.OWL2_DL_ISSUES, NativeOWL2DLIssueRowV2),
        (NativeFacadeCollectionV2.OWL2_DL_ROLE_HIERARCHY, NativeOWL2DLRoleEdgeRowV2),
    )
    for collection, expected_type in expected_types:
        key = next(key for key in collections if key[0] is collection)
        row = collections[key][0]
        decoded = decode_native_auxiliary_row_v2(
            collection,
            row,
            max_row_bytes=bound,
        )
        assert type(decoded) is expected_type
        assert encode_native_auxiliary_row_v2(decoded, max_row_bytes=bound) == (
            collection,
            row,
        )


def test_role_edge_retains_both_validation_decodes_page_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values, collections, _summary = _validated_fixture()
    bound = source_load_row_budget(values)
    key = next(
        key for key in collections if key[0] is NativeFacadeCollectionV2.OWL2_DL_ROLE_HIERARCHY
    )
    original_decode = decode_canonical
    calls = 0

    def counted_decode(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original_decode(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(native_handoff_v2, "decode_canonical", counted_decode)
    decoded = cast(
        NativeOWL2DLRoleEdgeRowV2,
        decode_native_auxiliary_row_v2(
            NativeFacadeCollectionV2.OWL2_DL_ROLE_HIERARCHY,
            collections[key][0],
            max_row_bytes=bound,
        ),
    )
    assert calls == 2

    def forbidden_decode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("role endpoint decoded twice")

    monkeypatch.setattr(native_handoff_v2, "decode_canonical", forbidden_decode)
    sub_property, super_property = decoded._validated_properties_v2()
    assert isinstance(sub_property, (ObjectProperty, ObjectInverseOf))
    assert isinstance(super_property, (ObjectProperty, ObjectInverseOf))


def test_validated_owl2_dl_report_is_attested_paged_and_counted() -> None:
    values, collections, summary = _validated_fixture()
    value = publication(collections, values=values)
    assert value.owl2_dl_report_summary == summary
    assert value.handle.attestation.owl2_dl_report_summary == summary
    for collection in (
        NativeFacadeCollectionV2.OWL2_DL_STRUCTURAL_ISSUES,
        NativeFacadeCollectionV2.OWL2_DL_ISSUES,
        NativeFacadeCollectionV2.OWL2_DL_ROLE_PROPERTIES,
        NativeFacadeCollectionV2.OWL2_DL_ROLE_HIERARCHY,
        NativeFacadeCollectionV2.OWL2_DL_ROLE_COMPOSITE,
        NativeFacadeCollectionV2.OWL2_DL_ROLE_NON_SIMPLE,
    ):
        page = value.handle._facade_page_v2(
            NativeFacadePageRequestV2(
                collection=collection,
                scope=NativeFacadeScopeV2.CLOSURE,
                document_ordinal=None,
                start=0,
                max_rows=64,
                max_bytes=8 * 1024 * 1024,
                max_row_bytes=value.max_facade_row_bytes,
            )
        )
        assert page.total_count == 1
        assert page.terminal and len(page.rows) == 1
    counters = value.handle._facade_counters_v2()
    assert counters.retained_owl2_dl_structural_issue_rows == 1
    assert counters.retained_owl2_dl_role_hierarchy_rows == 1
    assert counters.owl2_dl_structural_issue_rows_emitted == 1
    assert counters.owl2_dl_role_non_simple_rows_emitted == 1
    assert counters.retained_owl2_dl_bytes > 0


@pytest.mark.parametrize(
    "collection",
    (
        NativeFacadeCollectionV2.OWL2_DL_STRUCTURAL_ISSUES,
        NativeFacadeCollectionV2.OWL2_DL_ISSUES,
    ),
)
def test_owl_issue_pages_preserve_producer_order_and_duplicates_at_any_page_size(
    collection: NativeFacadeCollectionV2,
) -> None:
    gathered_by_page_size: list[tuple[bytes, ...]] = []
    expected: tuple[bytes, ...] | None = None
    for max_rows in (1, 3):
        values, collections, encoded_rows = _producer_issue_fixture()
        expected = encoded_rows[collection]
        published = publication(collections, values=values)
        gathered: list[bytes] = []
        start = 0
        while True:
            page = published.handle._facade_page_v2(
                NativeFacadePageRequestV2(
                    collection=collection,
                    scope=NativeFacadeScopeV2.CLOSURE,
                    document_ordinal=None,
                    start=start,
                    max_rows=max_rows,
                    max_bytes=published.max_facade_row_bytes * max_rows,
                    max_row_bytes=published.max_facade_row_bytes,
                )
            )
            gathered.extend(page.rows)
            if page.terminal:
                break
            assert page.next_cursor is not None
            start = page.next_cursor
        gathered_by_page_size.append(tuple(gathered))

    assert expected is not None
    assert gathered_by_page_size == [expected, expected]
    assert expected[1] == expected[2]


def test_owl2_dl_report_rejects_digest_count_conforms_and_presence_lies() -> None:
    values, collections, summary = _validated_fixture()
    report = cast(NativeLoadReportPublicationV1, values["report"])
    evidence = fingerprint_evidence(values)

    for changed_report, changed_summary, message in (
        (replace(report, owl2_dl_report_sha256=b"x" * 32), summary, "digest diverges"),
        (
            report,
            replace(
                summary,
                structural_values_checked=summary.structural_values_checked + 1,
            ),
            "digest diverges",
        ),
        (
            report,
            replace(summary, role_hierarchy_count=2),
            "summary counts",
        ),
        (
            report,
            replace(summary, structural_complete=False),
            "complete conforming",
        ),
        (replace(report, owl2_dl_conforms=False), summary, "complete conforming"),
    ):
        changed = dict(values)
        changed["report"] = changed_report
        changed["owl2_dl_report_summary"] = changed_summary
        with pytest.raises(BackendProtocolError, match=message):
            content_digests(changed, collections, evidence)

    unvalidated = dict(publication_fields())
    unvalidated["owl2_dl_report_summary"] = None
    with pytest.raises(BackendProtocolError, match="unvalidated"):
        content_digests(unvalidated, collections, fingerprint_evidence(unvalidated))


def test_owl2_dl_collections_are_closure_only_and_unvalidated_pages_fail_closed() -> None:
    with pytest.raises(ValueError, match="closure scope only"):
        NativeFacadePageRequestV2(
            collection=NativeFacadeCollectionV2.OWL2_DL_ISSUES,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            start=0,
            max_rows=1,
            max_bytes=1,
            max_row_bytes=1,
        )
    value = publication()
    with pytest.raises(BackendProtocolError, match="no validated report"):
        value.handle._facade_page_v2(
            NativeFacadePageRequestV2(
                collection=NativeFacadeCollectionV2.OWL2_DL_ISSUES,
                scope=NativeFacadeScopeV2.CLOSURE,
                document_ordinal=None,
                start=0,
                max_rows=1,
                max_bytes=1,
                max_row_bytes=value.max_facade_row_bytes,
            )
        )
