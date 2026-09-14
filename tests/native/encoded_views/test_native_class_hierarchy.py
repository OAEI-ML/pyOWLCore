from __future__ import annotations

from typing import Any

import pytest

from pyowl_core import (
    IRI,
    AssertedClassHierarchyView,
    BackendPreference,
    BackendProtocolError,
    Class,
    ClassComponent,
    ImportPolicy,
    LoadOptions,
    load_snapshot,
)
from pyowl_core.index import hierarchy
from pyowl_core.model import canonical_bytes
from tests.native.foundation._support import load_extension

SOURCE = b"""Ontology(<urn:hierarchy>
 Declaration(Class(<urn:A>)) Declaration(Class(<urn:B>))
 Declaration(Class(<urn:C>)) Declaration(Class(<urn:D>))
 SubClassOf(<urn:A> <urn:A>) SubClassOf(<urn:A> <urn:B>)
 SubClassOf(Annotation(<urn:note> "different") <urn:A> <urn:B>)
 SubClassOf(<urn:B> <urn:C>)
 SubClassOf(ObjectSomeValuesFrom(<urn:r> <urn:A>) <urn:D>)
 SubClassOf(<urn:C> ObjectAllValuesFrom(<urn:r> <urn:D>))
 EquivalentClasses(<urn:A> <urn:B> ObjectIntersectionOf(<urn:C> <urn:D>))
 EquivalentClasses(<urn:B> <urn:C>)
 DisjointUnion(<urn:D> <urn:A> <urn:C> ObjectSomeValuesFrom(<urn:r> <urn:D>))
)"""


@pytest.fixture(scope="module", autouse=True)
def extension() -> Any:
    selected = load_extension()
    assert selected.NATIVE_CLASS_HIERARCHY_API_VERSION == 1
    return selected


def owner(backend: BackendPreference = BackendPreference.NATIVE) -> Any:
    return load_snapshot(SOURCE, options=LoadOptions(backend=backend, imports=ImportPolicy.IGNORE))


def forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("strict class hierarchy entered Python ontology graph construction")


def key(node: Any) -> Any:
    return (
        ("component", tuple(canonical_bytes(member) for member in node.members))
        if isinstance(node, ClassComponent)
        else ("class", canonical_bytes(node))
    )


def snapshot(view: Any) -> Any:
    nodes = [Class(IRI("urn:" + name)) for name in ("A", "B", "C", "D", "Unknown")]
    components = [view.component(node) for node in nodes]
    return {
        "edges": [
            (key(edge.child), key(edge.parent), canonical_bytes(edge.axiom))
            for edge in view.iter_edges()
        ],
        "equivalence_sets": [
            (tuple(canonical_bytes(item) for item in row.classes), canonical_bytes(row.axiom))
            for row in view.equivalence_sets()
        ],
        "parents": [
            [key(item) for item in view.asserted_parents(node)] for node in [*nodes, *components]
        ],
        "children": [
            [key(item) for item in view.asserted_children(node)] for node in [*nodes, *components]
        ],
        "equivalents": [[key(item) for item in view.equivalents(node)] for node in nodes],
        "components": [key(node) for node in components],
        "ignored": view.ignored_complex_endpoint_count,
    }


@pytest.mark.parametrize("handling", ["preserve", "bidirectional", "component"])
@pytest.mark.parametrize("include_union", [False, True])
def test_native_constructor_and_queries_match_python(
    handling: str, include_union: bool, monkeypatch: Any
) -> None:
    options = dict(
        include_origins=False, equivalence_handling=handling, include_disjoint_union=include_union
    )
    expected = snapshot(owner(BackendPreference.PYTHON).view(AssertedClassHierarchyView, **options))
    source = owner()
    monkeypatch.setattr(type(source), "iter_axioms", forbidden)
    monkeypatch.setattr(hierarchy, "_build_hierarchy", forbidden)
    monkeypatch.setattr(AssertedClassHierarchyView, "__init__", forbidden)
    view = source.view(AssertedClassHierarchyView, require_native_pipeline=True, **options)
    assert snapshot(view) == expected
    assert view.native_report["scanned_roots"] == 13
    assert view.native_report["selected_axioms"] == 9
    assert view.native_report["ignored_complex_endpoint_count"] == 4
    assert source.view(AssertedClassHierarchyView, require_native_pipeline=True, **options) is view


def test_strict_class_owner_rejection_and_lifetime(monkeypatch: Any) -> None:
    source = owner(BackendPreference.PYTHON)
    monkeypatch.setattr(type(source), "iter_axioms", forbidden)
    with pytest.raises(BackendProtocolError, match="native asserted"):
        source.view(AssertedClassHierarchyView, require_native_pipeline=True)
    native = owner()
    view = native.view(AssertedClassHierarchyView, require_native_pipeline=True)
    edge = next(view.iter_edges())
    native.close()
    assert edge.child
    from pyowl_core import ClosedSnapshotError

    with pytest.raises(ClosedSnapshotError):
        list(view.asserted_parents(Class(IRI("urn:A"))))


def test_limited_records_and_selected_origins() -> None:
    source = owner()
    view = source.view(AssertedClassHierarchyView, require_native_pipeline=True)
    assert list(view.iter_edges(limit=0)) == []
    edge = next(view.iter_edges(limit=1))
    from pyowl_core.model import structural_digest

    assert edge.origins == source.origin_index.entries.get(structural_digest(edge.axiom), ())
    assert len(list(view.equivalence_sets(limit=1))) == 1


def test_queries_visit_only_indexed_neighbors() -> None:
    source = owner()
    view = source.view(
        AssertedClassHierarchyView, require_native_pipeline=True, include_origins=False
    )
    for _ in range(25):
        assert len(list(view.asserted_parents(Class(IRI("urn:A"))))) == 2
    assert view.native_report["neighbor_requests"] == 25
    assert view.native_report["neighbor_rows_visited"] == 50
    assert view.native_report["scanned_roots"] == 13


def test_annotation_overlay_reuses_native_class_index(monkeypatch: Any) -> None:
    from pyowl_core import (
        AnnotationAssertion,
        AnnotationProperty,
        CanonicalSet,
        OntologyDelta,
        apply_delta,
    )

    source = owner()
    base = source.view(
        AssertedClassHierarchyView, require_native_pipeline=True, include_origins=False
    )
    overlay = apply_delta(
        source,
        OntologyDelta(
            add_axioms=CanonicalSet(
                (
                    AnnotationAssertion(
                        AnnotationProperty(IRI("urn:label")), IRI("urn:A"), IRI("urn:label-value")
                    ),
                )
            )
        ),
    )
    monkeypatch.setattr(type(overlay), "iter_axioms", forbidden)
    monkeypatch.setattr(type(source), "iter_axioms", forbidden)
    view = overlay.view(
        AssertedClassHierarchyView, require_native_pipeline=True, include_origins=False
    )
    assert view._ontology is overlay
    assert view._native_index is base._native_index
    assert snapshot(view) == snapshot(base)


def test_changed_class_overlay_rejects_before_scalar_iteration(monkeypatch: Any) -> None:
    from pyowl_core import CanonicalSet, OntologyDelta, SubClassOf, apply_delta

    overlay = apply_delta(
        owner(),
        OntologyDelta(
            add_axioms=CanonicalSet((SubClassOf(Class(IRI("urn:D")), Class(IRI("urn:A"))),))
        ),
    )
    monkeypatch.setattr(type(overlay), "iter_axioms", forbidden)
    with pytest.raises(BackendProtocolError, match="changes in overlays"):
        overlay.view(AssertedClassHierarchyView, require_native_pipeline=True)
