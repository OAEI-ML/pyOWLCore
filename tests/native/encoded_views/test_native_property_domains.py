from __future__ import annotations

from typing import Any

import pytest

import pyowl_core as core
from pyowl_core.index import domains
from pyowl_core.model import canonical_bytes, structural_digest
from tests.native.foundation._support import load_extension

SOURCE = b"""Ontology(<urn:domains>
 ObjectPropertyDomain(<urn:p> <urn:A>)
 ObjectPropertyDomain(Annotation(<urn:note> "keep") <urn:p> <urn:A>)
 ObjectPropertyRange(<urn:p> <urn:B>)
 ObjectPropertyDomain(<urn:p> ObjectIntersectionOf(<urn:A> <urn:B>))
 ObjectPropertyDomain(ObjectInverseOf(<urn:p>) <urn:C>)
 DataPropertyDomain(<urn:p> <urn:A>)
 DataPropertyRange(<urn:p> DatatypeRestriction(
  <http://www.w3.org/2001/XMLSchema#integer>
  <http://www.w3.org/2001/XMLSchema#minInclusive>
  "1"^^<http://www.w3.org/2001/XMLSchema#integer>))
 DataPropertyRange(<urn:d> <http://www.w3.org/2001/XMLSchema#string>)
 AnnotationPropertyDomain(<urn:p> <urn:annotation-domain>)
 AnnotationPropertyRange(<urn:a> <urn:annotation-range>)
 AnnotationAssertion(<urn:label> <urn:A> "unrelated") Declaration(Class(<urn:unused>))
)"""


@pytest.fixture(scope="module", autouse=True)
def extension() -> Any:
    result = load_extension()
    assert result.NATIVE_PROPERTY_DOMAINS_API_VERSION == 1
    return result


def owner(backend: core.BackendPreference = core.BackendPreference.NATIVE) -> Any:
    return core.load_snapshot(
        SOURCE, options=core.LoadOptions(backend=backend, imports=core.ImportPolicy.IGNORE)
    )


def strict(source: Any, **options: Any) -> Any:
    return source.view(core.PropertyDomainRangeView, require_native_pipeline=True, **options)


def forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("native domain query entered Python ontology traversal")


def summary(view: Any) -> Any:
    properties = list(view.properties())
    probes = [*properties, core.ObjectProperty(core.IRI("urn:absent"))]
    return {
        "properties": properties,
        "all": [tuple(view.iter(p)) for p in probes],
        "domains": [tuple(view.domains(p)) for p in probes],
        "ranges": [tuple(view.ranges(p)) for p in probes],
        "named_domains": [view.named(p, "domain") for p in probes],
        "named_ranges": [view.named(p, "range") for p in probes],
    }


def test_native_six_constructors_inverse_punning_and_complex_values_match_reference(
    monkeypatch: Any,
) -> None:
    expected = summary(
        owner(core.BackendPreference.PYTHON).view(
            core.PropertyDomainRangeView, include_origins=False
        )
    )
    source = owner()
    monkeypatch.setattr(type(source), "iter_axioms", forbidden)
    monkeypatch.setattr(core.PropertyDomainRangeView, "__init__", forbidden)
    monkeypatch.setattr(domains, "_record", forbidden)
    view = strict(source, include_origins=False)
    assert view.native_report["published_axioms"] == 0
    assert summary(view) == expected
    assert view.native_report["scanned_roots"] == 12
    assert view.native_report["selected_axioms"] == 10
    assert view.native_report["properties"] == 6
    assert strict(source, include_origins=False) is view
    p = core.ObjectProperty(core.IRI("urn:p"))
    assert view.named(p, "domain").filtered_complex_count == 1
    assert len(view.named(p, "domain").records) == 2
    assert len(list(view.iter(p, limit=1))) == 1
    assert list(view.properties(limit=0)) == []
    assert list(view.iter(p, limit=0)) == []


def test_repeated_queries_visit_only_selected_property_postings() -> None:
    view = strict(owner(), include_origins=False)
    p = core.ObjectProperty(core.IRI("urn:p"))
    for _ in range(25):
        assert len(list(view.ranges(p))) == 1
    assert view.native_report["query_rows_visited"] == 100
    assert view.native_report["published_axioms"] == 25


def test_pagination_preserves_canonical_order_without_restart() -> None:
    text = (
        "Ontology(<urn:pages> "
        + " ".join(f"ObjectPropertyDomain(<urn:p> <urn:C{i}>)" for i in range(140))
        + ")"
    )
    source = core.load_snapshot(
        text.encode(), options=core.LoadOptions(backend=core.BackendPreference.NATIVE)
    )
    view = strict(source, include_origins=False)
    p = core.ObjectProperty(core.IRI("urn:p"))
    rows = list(view.domains(p))
    assert len(rows) == 140
    assert [canonical_bytes(r.axiom) for r in rows] == sorted(
        canonical_bytes(r.axiom) for r in rows
    )
    assert view.native_report["query_rows_visited"] == 140
    assert view.native_report["published_axioms"] == 140


def test_requested_origins_and_owner_lifecycle() -> None:
    source = owner()
    view = strict(source)
    row = next(view.iter(core.ObjectProperty(core.IRI("urn:p")), limit=1))
    assert row.origins == source.origin_index.entries.get(structural_digest(row.axiom), ())
    source.close()
    assert row.axiom
    with pytest.raises(core.ClosedSnapshotError):
        list(view.properties())


def test_import_scope_selection_keeps_unrelated_root_and_imported_assertions() -> None:
    imported = b"Ontology(<urn:imported> ObjectPropertyRange(<urn:p> <urn:imported-class>))"
    source = core.load_snapshot(
        SOURCE.replace(b"Ontology(<urn:domains>", b"Ontology(<urn:domains> Import(<urn:imported>)"),
        options=core.LoadOptions(backend=core.BackendPreference.NATIVE),
        resolver=core.MappingResolver({"urn:imported": imported}),
    )
    p = core.ObjectProperty(core.IRI("urn:p"))
    assert len(list(strict(source).ranges(p))) == 2
    assert len(list(strict(source, scope=core.AxiomScope.ROOT).ranges(p))) == 1
    document_key = next(
        x.document_key
        for x in source.import_manifest.documents
        if x.document_iri == core.IRI("urn:imported")
    )
    assert (
        len(
            list(
                strict(source, scope=core.AxiomScope.DOCUMENT, document_key=document_key).ranges(p)
            )
        )
        == 1
    )


def test_annotation_overlay_reuses_index_and_domain_delta_fails_before_scalar_iteration(
    monkeypatch: Any,
) -> None:
    source = owner()
    base = strict(source)
    annotation = core.AnnotationAssertion(
        core.AnnotationProperty(core.IRI("urn:note")), core.IRI("urn:A"), core.IRI("urn:value")
    )
    overlay = core.apply_delta(
        source, core.OntologyDelta(add_axioms=core.CanonicalSet((annotation,)))
    )
    monkeypatch.setattr(type(overlay), "iter_axioms", forbidden)
    view = strict(overlay)
    assert view._ontology is overlay and view._native_index is base._native_index
    p = core.ObjectProperty(core.IRI("urn:p"))
    assert list(view.iter(p)) == list(base.iter(p))
    changed = core.apply_delta(
        overlay,
        core.OntologyDelta(
            add_axioms=core.CanonicalSet(
                (core.ObjectPropertyDomain(p, core.Class(core.IRI("urn:new"))),)
            )
        ),
    )
    with pytest.raises(core.BackendProtocolError, match="changes in overlays"):
        strict(changed)
    assert list(strict(changed, scope=core.AxiomScope.ROOT).iter(p)) == list(
        strict(source, scope=core.AxiomScope.ROOT).iter(p)
    )


def test_limits_cancellation_and_incompatible_owner_fail_closed(monkeypatch: Any) -> None:
    from pyowl_core.backends.native_validation import _native_owner
    from pyowl_core.backends.native_views import _invoke_native_column_operation_v2

    source = owner()
    raw, extension = _native_owner(source)
    for limits in [
        core.ParseLimits(max_index_rows=1),
        core.ParseLimits(max_index_bytes=1),
        core.ParseLimits(max_memory_bytes=1),
    ]:
        with pytest.raises(core.ResourceLimitError):
            _invoke_native_column_operation_v2(
                extension, extension._property_domains_v1, (raw, "closure", None), limits, None
            )
    cancelled = core.CancellationSource()
    cancelled.cancel()
    with pytest.raises(core.OperationCancelledError):
        strict(source, cancellation_token=cancelled.token)
    incompatible = owner(core.BackendPreference.PYTHON)
    monkeypatch.setattr(type(incompatible), "iter_axioms", forbidden)
    with pytest.raises(core.BackendProtocolError, match="unavailable for this owner"):
        strict(incompatible)


def test_property_pages_and_filtered_tail_keep_forward_only_cursors() -> None:
    properties = " ".join(f"ObjectPropertyDomain(<urn:p{i}> <urn:A>)" for i in range(140))
    source = core.load_snapshot(
        ("Ontology(<urn:many> " + properties + ")").encode(),
        options=core.LoadOptions(backend=core.BackendPreference.NATIVE),
    )
    view = strict(source, include_origins=False)
    keys = list(view.properties())
    assert len(keys) == 140
    assert keys == sorted(keys, key=canonical_bytes)
    assert view.native_report["published_axioms"] == 0
    facts = [f"ObjectPropertyDomain(<urn:p> <urn:C{i}>)" for i in range(65)]
    facts += [
        f"ObjectPropertyDomain(<urn:p> ObjectIntersectionOf(<urn:C{i}> <urn:D>))" for i in range(75)
    ]
    source = core.load_snapshot(
        ("Ontology(<urn:filtered> " + " ".join(facts) + ")").encode(),
        options=core.LoadOptions(backend=core.BackendPreference.NATIVE),
    )
    view = strict(source, include_origins=False)
    rows = list(view.domains(core.ObjectProperty(core.IRI("urn:p")), named_only=True))
    assert len(rows) == 65
    assert view.native_report["query_rows_visited"] == 140
    assert view.native_report["published_axioms"] == 65
