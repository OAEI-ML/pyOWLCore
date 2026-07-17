from __future__ import annotations

import pytest

from pyowl_core import (
    IRI,
    AnonymousIndividual,
    BackendPreference,
    Class,
    Declaration,
    DeltaError,
    ImportPolicy,
    LoadOptions,
    OntologyDelta,
    compose_views,
    load_snapshot,
    walk,
)


def _anonymous_snapshot(identity: str = "same"):  # type: ignore[no-untyped-def]
    return load_snapshot(
        (f"Prefix(:=<urn:test#>) Ontology(<urn:{identity}> ClassAssertion(:A _:x))").encode(),
        options=LoadOptions(
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
        ),
    )


def test_equal_anonymous_members_are_standardized_apart_and_order_independent() -> None:
    first = _anonymous_snapshot()
    second = _anonymous_snapshot()
    forward = compose_views(first, second, roles=("left", "right"))
    reverse = compose_views(second, first, roles=("right", "left"))
    axioms = tuple(forward.iter_axioms())

    assert len(axioms) == 2
    individuals = [
        item for axiom in axioms for item in walk(axiom) if isinstance(item, AnonymousIndividual)
    ]
    assert len(individuals) == 2
    assert individuals[0].document_scope != individuals[1].document_scope
    assert forward.structural_fingerprint == reverse.structural_fingerprint
    assert forward.logical_fingerprint == reverse.logical_fingerprint
    assert forward.signature_fingerprint == reverse.signature_fingerprint
    assert all(len(forward.origins_for(axiom)) == 1 for axiom in axioms)


def test_anonymous_scopes_survive_explicit_materialization_exactly() -> None:
    composite = compose_views(_anonymous_snapshot(), _anonymous_snapshot())
    before = tuple(composite.iter_axioms())
    materialized = composite.materialize()
    after = tuple(materialized.iter_axioms())

    assert before == after
    assert materialized.structural_fingerprint == composite.structural_fingerprint
    assert materialized.logical_fingerprint == composite.logical_fingerprint
    assert materialized.signature_fingerprint == composite.signature_fingerprint
    assert all(
        materialized.origin_index.origins_for(axiom) == composite.origins_for(axiom)
        for axiom in before
    )


def test_already_distinct_member_anonymous_scopes_are_preserved() -> None:
    first = _anonymous_snapshot("left")
    second = _anonymous_snapshot("right")
    expected = {
        item.document_scope
        for source in (first, second)
        for axiom in source.iter_axioms()
        for item in walk(axiom)
        if isinstance(item, AnonymousIndividual)
    }

    actual = {
        item.document_scope
        for axiom in compose_views(first, second).iter_axioms()
        for item in walk(axiom)
        if isinstance(item, AnonymousIndividual)
    }
    assert actual == expected


def test_nested_anonymous_composition_matches_flat_composition() -> None:
    first = _anonymous_snapshot()
    second = _anonymous_snapshot()
    third = _anonymous_snapshot()
    nested = compose_views(compose_views(first, second), third)
    flat = compose_views(first, second, third)

    assert tuple(nested.iter_axioms()) == tuple(flat.iter_axioms())
    assert nested.structural_fingerprint == flat.structural_fingerprint
    assert nested.logical_fingerprint == flat.logical_fingerprint
    assert nested.signature_fingerprint == flat.signature_fingerprint


def test_nested_anonymous_composition_retains_an_inner_bridge() -> None:
    first = _anonymous_snapshot()
    second = _anonymous_snapshot()
    third = _anonymous_snapshot()
    bridge = Declaration(Class(IRI("urn:test#Bridge")))
    nested = compose_views(
        compose_views(first, second, delta=OntologyDelta(add_axioms={bridge})),
        third,
    )
    flat = compose_views(
        first,
        second,
        third,
        delta=OntologyDelta(add_axioms={bridge}),
    )

    assert tuple(nested.iter_axioms()) == tuple(flat.iter_axioms())
    assert nested.structural_fingerprint == flat.structural_fingerprint
    assert nested.logical_fingerprint == flat.logical_fingerprint
    assert nested.signature_fingerprint == flat.signature_fingerprint


def test_ambiguous_anonymous_bridge_requires_a_composed_identity() -> None:
    first = _anonymous_snapshot()
    second = _anonymous_snapshot()
    ambiguous = next(first.iter_axioms())

    with pytest.raises(DeltaError) as error:
        compose_views(
            first,
            second,
            delta=OntologyDelta(add_axioms={ambiguous}),
        )
    assert error.value.code == "COMPOSITION_ANONYMOUS_BRIDGE_AMBIGUOUS"
