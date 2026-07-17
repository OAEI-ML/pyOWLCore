from __future__ import annotations

from typing import cast

from hypothesis import given, settings
from hypothesis import strategies as st

import pyowl_core.model as m
from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    OntologyDelta,
    OntologySnapshot,
    apply_delta,
    compose_views,
    decode_snapshot,
    encode_snapshot,
    load_snapshot,
    parse_document,
)

SETTINGS = settings(max_examples=24, deadline=None, derandomize=True)
NAMES = st.lists(
    st.integers(min_value=0, max_value=10_000),
    min_size=2,
    max_size=8,
    unique=True,
)


def _class(index: int) -> m.Class:
    return m.Class(m.IRI(f"https://example.org/generated#C{index}"))


def _declaration(index: int) -> m.Declaration:
    return m.Declaration(_class(index))


def _snapshot(source: bytes, *, document_iri: str) -> OntologySnapshot:
    return load_snapshot(
        source,
        document_iri=document_iri,
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
        ),
    )


@SETTINGS
@given(NAMES)
def test_unordered_operands_are_permutation_invariant_and_chains_are_not(
    names: list[int],
) -> None:
    classes = tuple(_class(index) for index in names)
    equivalent = m.EquivalentClasses(m.CanonicalSet(classes))
    permuted = m.EquivalentClasses(m.CanonicalSet(reversed(classes)))
    assert equivalent == permuted
    assert equivalent.canonical_bytes() == permuted.canonical_bytes()

    properties = tuple(
        m.ObjectProperty(m.IRI(f"https://example.org/generated#p{index}"))
        for index in names
    )
    chain = m.ObjectPropertyChain(properties)
    reversed_chain = m.ObjectPropertyChain(tuple(reversed(properties)))
    assert chain != reversed_chain
    assert chain.canonical_bytes() != reversed_chain.canonical_bytes()


@SETTINGS
@given(NAMES)
def test_layout_source_order_and_duplicate_axioms_do_not_change_identity_or_wire(
    names: list[int],
) -> None:
    declarations = [
        f"Declaration(Class(<https://example.org/generated#C{value}>))" for value in names
    ]
    forward = (
        "Ontology(<https://example.org/generated/o> " + " ".join(declarations) + ")"
    ).encode()
    reverse = (
        "Ontology(\n  <https://example.org/generated/o>\n  "
        + "\n  ".join((*reversed(declarations), declarations[0]))
        + "\n)"
    ).encode()
    first = parse_document(
        forward,
        format=DocumentFormat.FUNCTIONAL,
        document_iri="https://example.org/generated/o",
        options=LoadOptions(backend=BackendPreference.PYTHON),
    )
    second = parse_document(
        reverse,
        format=DocumentFormat.FUNCTIONAL,
        document_iri="https://example.org/generated/o",
        options=LoadOptions(backend=BackendPreference.PYTHON),
    )
    assert first == second
    assert first.document_fingerprint == second.document_fingerprint
    first_snapshot = load_snapshot(
        first,
        options=LoadOptions(backend=BackendPreference.PYTHON, imports=ImportPolicy.IGNORE),
    )
    second_snapshot = load_snapshot(
        second,
        options=LoadOptions(backend=BackendPreference.PYTHON, imports=ImportPolicy.IGNORE),
    )
    assert first_snapshot.structural_fingerprint == second_snapshot.structural_fingerprint
    assert encode_snapshot(first_snapshot) == encode_snapshot(second_snapshot)


@SETTINGS
@given(
    remove=st.sets(st.integers(min_value=0, max_value=5), max_size=5),
    add=st.sets(st.integers(min_value=6, max_value=12), max_size=6),
)
def test_overlay_effective_content_matches_independent_materialization_and_wire(
    remove: set[int],
    add: set[int],
) -> None:
    base_source = (
        "Ontology(<https://example.org/generated/base> "
        + " ".join(
            f"Declaration(Class(<https://example.org/generated#C{index}>))"
            for index in range(6)
        )
        + ")"
    ).encode()
    base = _snapshot(base_source, document_iri="https://example.org/generated/base")
    delta = OntologyDelta(
        add_axioms=m.CanonicalSet(_declaration(index) for index in add),
        remove_axioms=m.CanonicalSet(_declaration(index) for index in remove),
    )
    overlay = apply_delta(base, delta)
    materialized = overlay.materialize()
    expected = {
        *(_declaration(index) for index in range(6) if index not in remove),
        *(_declaration(index) for index in add),
    }
    assert set(overlay.iter_axioms()) == expected
    assert set(materialized.iter_axioms()) == expected
    assert overlay.structural_fingerprint == materialized.structural_fingerprint
    assert overlay.logical_fingerprint == materialized.logical_fingerprint
    assert overlay.signature_fingerprint == materialized.signature_fingerprint
    overlay_wire = encode_snapshot(overlay)
    decoded = decode_snapshot(overlay_wire)
    assert decoded.structural_fingerprint == overlay.structural_fingerprint
    assert set(decoded.iter_axioms()) == expected


@SETTINGS
@given(
    left=st.integers(min_value=0, max_value=1000),
    right=st.integers(min_value=1001, max_value=2000),
)
def test_composite_member_permutation_preserves_semantic_identity(
    left: int,
    right: int,
) -> None:
    first = _snapshot(
        f"Ontology(Declaration(Class(<https://example.org/generated#C{left}>)))".encode(),
        document_iri=f"https://example.org/generated/left/{left}",
    )
    second = _snapshot(
        f"Ontology(Declaration(Class(<https://example.org/generated#C{right}>)))".encode(),
        document_iri=f"https://example.org/generated/right/{right}",
    )
    forward = compose_views(first, second)
    reverse = compose_views(second, first)
    assert tuple(sorted(forward.iter_axioms(), key=m.canonical_bytes)) == tuple(
        sorted(reverse.iter_axioms(), key=m.canonical_bytes)
    )
    assert forward.structural_fingerprint == reverse.structural_fingerprint
    assert forward.logical_fingerprint == reverse.logical_fingerprint
    assert forward.signature_fingerprint == reverse.signature_fingerprint
    assert encode_snapshot(forward) == encode_snapshot(reverse)


@SETTINGS
@given(st.integers(min_value=0, max_value=20), st.sampled_from(("EN", "en", "eN")))
def test_language_case_and_wire_roundtrip_are_canonical(index: int, language: str) -> None:
    source = (
        "Ontology(AnnotationAssertion(<https://example.org/p> "
        f"<https://example.org/s{index}> \"value\"@{language}))"
    ).encode()
    snapshot = _snapshot(source, document_iri=f"https://example.org/generated/lang/{index}")
    encoded = encode_snapshot(snapshot)
    decoded = decode_snapshot(encoded)
    assert encode_snapshot(decoded) == encoded
    assertion = cast(m.AnnotationAssertion, next(decoded.iter_axioms(m.AnnotationAssertion)))
    assert isinstance(assertion.value, m.Literal)
    assert assertion.value.language == "en"
