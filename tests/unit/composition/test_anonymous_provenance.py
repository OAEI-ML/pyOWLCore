from __future__ import annotations

from pathlib import Path

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
    MappedOntologySnapshot,
    OntologyDelta,
    compose_views,
    decode_snapshot,
    encode_snapshot,
    load_snapshot,
    open_snapshot,
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
    originals = tuple(
        sorted(
            (*first.iter_axioms(), *second.iter_axioms()),
            key=lambda value: value.canonical_bytes(),
        )
    )
    expected = {
        item.document_scope
        for source in (first, second)
        for axiom in source.iter_axioms()
        for item in walk(axiom)
        if isinstance(item, AnonymousIndividual)
    }

    composite = compose_views(first, second)
    axioms = tuple(composite.iter_axioms())
    actual = {
        item.document_scope
        for axiom in axioms
        for item in walk(axiom)
        if isinstance(item, AnonymousIndividual)
    }
    assert actual == expected
    assert axioms == originals
    assert all(
        actual_axiom is original
        for actual_axiom, original in zip(axioms, originals, strict=True)
    )


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


def test_nested_anonymous_composition_retains_an_inner_bridge(tmp_path: Path) -> None:
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

    nested_wire = encode_snapshot(nested)
    flat_wire = encode_snapshot(flat)
    decoded_nested = decode_snapshot(nested_wire)
    decoded_flat = decode_snapshot(flat_wire)
    assert tuple(decoded_nested.iter_axioms()) == tuple(decoded_flat.iter_axioms())
    assert decoded_nested.structural_fingerprint == decoded_flat.structural_fingerprint
    assert encode_snapshot(decoded_nested) == nested_wire
    assert encode_snapshot(decoded_flat) == flat_wire

    nested_path = tmp_path / "nested-anonymous.pyocore"
    flat_path = tmp_path / "flat-anonymous.pyocore"
    nested_path.write_bytes(nested_wire)
    flat_path.write_bytes(flat_wire)
    mapped_nested = open_snapshot(nested_path)
    mapped_flat = open_snapshot(flat_path)
    assert isinstance(mapped_nested, MappedOntologySnapshot)
    assert isinstance(mapped_flat, MappedOntologySnapshot)
    try:
        assert tuple(mapped_nested.iter_axioms()) == tuple(mapped_flat.iter_axioms())
        assert mapped_nested.structural_fingerprint == mapped_flat.structural_fingerprint
        assert encode_snapshot(mapped_nested) == nested_wire
        assert encode_snapshot(mapped_flat) == flat_wire
    finally:
        mapped_nested.close()
        mapped_flat.close()


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
