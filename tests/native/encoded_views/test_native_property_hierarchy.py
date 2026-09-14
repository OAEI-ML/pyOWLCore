from __future__ import annotations

from typing import Any

import pytest

from pyowl_core import (
    IRI,
    AssertedPropertyHierarchyView,
    BackendPreference,
    BackendProtocolError,
    DataProperty,
    ImportPolicy,
    LoadOptions,
    ObjectProperty,
    PropertyComponent,
    load_snapshot,
)
from pyowl_core.index import hierarchy
from pyowl_core.model import canonical_bytes
from tests.native.foundation._support import load_extension

SOURCE = b"""Ontology(<urn:properties>
 SubObjectPropertyOf(<urn:p> <urn:p>)
 SubObjectPropertyOf(<urn:p> <urn:q>)
 SubObjectPropertyOf(Annotation(<urn:note> "keep") <urn:p> <urn:q>)
 SubObjectPropertyOf(ObjectInverseOf(<urn:p>) <urn:q>)
 SubObjectPropertyOf(ObjectPropertyChain(<urn:p> <urn:q>) <urn:r>)
 EquivalentObjectProperties(<urn:p> <urn:q> ObjectInverseOf(<urn:r>))
 EquivalentObjectProperties(<urn:q> <urn:r>)
 InverseObjectProperties(<urn:p> ObjectInverseOf(<urn:r>))
 SubDataPropertyOf(<urn:d> <urn:e>)
 EquivalentDataProperties(<urn:d> <urn:e>)
 Declaration(Class(<urn:irrelevant>))
)"""


@pytest.fixture(scope="module", autouse=True)
def extension() -> Any:
    selected = load_extension()
    assert selected.NATIVE_PROPERTY_HIERARCHY_API_VERSION == 1
    return selected


def owner(backend: BackendPreference = BackendPreference.NATIVE) -> Any:
    return load_snapshot(SOURCE, options=LoadOptions(backend=backend, imports=ImportPolicy.IGNORE))


def forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("strict property hierarchy entered scalar graph construction")


def key(value: Any) -> Any:
    return (
        tuple(canonical_bytes(row) for row in value.members)
        if isinstance(value, PropertyComponent)
        else canonical_bytes(value)
    )


def summary(view: Any) -> Any:
    edges = list(view.iter_edges())
    nodes = [ObjectProperty(IRI("urn:" + name)) for name in ("p", "q", "r", "unknown")]
    nodes += [DataProperty(IRI("urn:" + name)) for name in ("d", "e")]
    queries = [*nodes, *[row.child for row in edges], *[row.parent for row in edges]]
    return {
        "edges": [(key(row.child), key(row.parent), canonical_bytes(row.axiom)) for row in edges],
        "equivalences": [
            (tuple(canonical_bytes(item) for item in row.properties), canonical_bytes(row.axiom))
            for row in view.equivalence_sets()
        ],
        "inverses": [canonical_bytes(row.axiom) for row in view.inverses()],
        "chains": [canonical_bytes(row.axiom) for row in view.chains()],
        "parents": [[key(row) for row in view.asserted_parents(node)] for node in queries],
        "children": [[key(row) for row in view.asserted_children(node)] for node in queries],
        "equals": [[key(row) for row in view.equivalents(node)] for node in nodes],
        "non_named": view.non_named_endpoint_count,
    }


@pytest.mark.parametrize("handling", ["preserve", "bidirectional", "component"])
def test_native_property_constructor_preserves_all_records(handling: str, monkeypatch: Any) -> None:
    options = dict(equivalence_handling=handling, include_origins=False)
    expected = summary(
        owner(BackendPreference.PYTHON).view(AssertedPropertyHierarchyView, **options)
    )
    source = owner()
    monkeypatch.setattr(type(source), "iter_axioms", forbidden)
    monkeypatch.setattr(hierarchy, "_build_hierarchy", forbidden)
    monkeypatch.setattr(AssertedPropertyHierarchyView, "__init__", forbidden)
    view = source.view(AssertedPropertyHierarchyView, require_native_pipeline=True, **options)
    assert summary(view) == expected
    assert view.non_named_endpoint_count == 3
    assert view.native_report["scanned_roots"] == 11
    assert len(list(view.iter_edges(limit=1))) == min(1, len(expected["edges"]))
    assert list(view.inverses(limit=0)) == []


def test_property_queries_reuse_index_and_reject_scalar_owner(monkeypatch: Any) -> None:
    source = owner()
    view = source.view(
        AssertedPropertyHierarchyView, require_native_pipeline=True, include_origins=False
    )
    for _ in range(25):
        assert list(view.asserted_parents(ObjectProperty(IRI("urn:p")))) == [
            ObjectProperty(IRI("urn:q"))
        ]
    assert view.native_report["neighbor_rows_visited"] == 25
    assert view.native_report["neighbor_requests"] == 25
    source = owner(BackendPreference.PYTHON)
    monkeypatch.setattr(type(source), "iter_axioms", forbidden)
    with pytest.raises(BackendProtocolError, match="native asserted property"):
        source.view(AssertedPropertyHierarchyView, require_native_pipeline=True)


@pytest.mark.parametrize("cycle", [False, True])
def test_native_property_directness_matches_component_reference(cycle: bool) -> None:
    text = (
        """Ontology(<urn:direct>
      EquivalentObjectProperties(<urn:p> <urn:p-alias>)
      SubObjectPropertyOf(<urn:p> <urn:q>) SubObjectPropertyOf(<urn:p-alias> <urn:r>)
      SubObjectPropertyOf(<urn:q> <urn:r>) SubObjectPropertyOf(<urn:r> <urn:s>)
    """
        + ("SubObjectPropertyOf(<urn:r> <urn:q>)" if cycle else "")
        + ")"
    )
    options = dict(include_origins=False, equivalence_handling="component")
    views = [
        load_snapshot(
            text.encode(), options=LoadOptions(backend=backend, imports=ImportPolicy.IGNORE)
        ).view(
            AssertedPropertyHierarchyView,
            require_native_pipeline=backend is BackendPreference.NATIVE,
            **options,
        )
        for backend in (BackendPreference.PYTHON, BackendPreference.NATIVE)
    ]
    for name in ("p", "p-alias", "q", "r", "s", "absent"):
        entity = ObjectProperty(IRI("urn:" + name))
        for method in ("direct_parents", "direct_children"):
            assert [key(row) for row in getattr(views[0], method)(entity)] == [
                key(row) for row in getattr(views[1], method)(entity)
            ]
        assert views[0].component(entity) == views[1].component(entity)
    if not cycle:
        assert list(views[1].direct_parents(ObjectProperty(IRI("urn:p")))) == [
            ObjectProperty(IRI("urn:q"))
        ]
