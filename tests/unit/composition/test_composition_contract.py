from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from itertools import permutations

import pytest

from pyowl_core import (
    IRI,
    Annotation,
    AnnotationProperty,
    AxiomScope,
    BackendPreference,
    DeltaBaseMismatchError,
    DeltaError,
    DeltaPolicy,
    LoadOptions,
    OntologyComposite,
    OntologyDelta,
    OntologySnapshot,
    OntologyView,
    OptionConflictError,
    ParseLimits,
    ResourceLimitError,
    coerce_snapshot,
    compose_views,
)

from .conftest import declaration, snapshot


class _Provider:
    def __init__(self, value: OntologyView) -> None:
        self.value = value
        self.calls = 0

    def owl_snapshot(self) -> OntologyView:
        self.calls += 1
        return self.value


def test_union_bridge_identity_and_explicit_materialization() -> None:
    source = snapshot("source", "A")
    target = snapshot("target", "B")
    bridge = declaration("Bridge")
    composite = compose_views(
        source,
        target,
        delta=OntologyDelta(add_axioms={bridge}),
        roles=("source", "target"),
    )
    provider = _Provider(composite)

    assert isinstance(composite, OntologyView)
    assert not isinstance(composite, OntologySnapshot)
    assert composite.members[0].view is source
    assert composite.members[1].view is target
    assert tuple(composite.iter_axioms()) == tuple(
        sorted({declaration("A"), declaration("B"), bridge})
    )
    assert composite.contains(bridge)
    assert coerce_snapshot(composite) is composite
    assert coerce_snapshot(provider) is composite
    assert provider.calls == 1
    assert composite.view(OntologyComposite) is composite

    materialized = composite.materialize()
    assert isinstance(materialized, OntologySnapshot)
    assert tuple(materialized.iter_axioms()) == tuple(composite.iter_axioms())
    assert materialized.structural_fingerprint == composite.structural_fingerprint
    assert materialized.logical_fingerprint == composite.logical_fingerprint
    assert materialized.signature_fingerprint == composite.signature_fingerprint


def test_member_order_and_roles_are_provenance_not_semantic_identity() -> None:
    source = snapshot("source", "A")
    target = snapshot("target", "B")
    first = compose_views(source, target, roles=("source", "target"))
    second = compose_views(target, source, roles=("right", "left"))

    assert tuple(first.iter_axioms()) == tuple(second.iter_axioms())
    assert first.structural_fingerprint == second.structural_fingerprint
    assert first.logical_fingerprint == second.logical_fingerprint
    assert first.signature_fingerprint == second.signature_fingerprint
    assert first.composition_provenance_digest != second.composition_provenance_digest

    third = snapshot("third", "C")
    permutations_of_members = tuple(
        compose_views(*order) for order in permutations((source, target, third))
    )
    assert all(
        candidate.structural_fingerprint == permutations_of_members[0].structural_fingerprint
        for candidate in permutations_of_members
    )


def test_randomized_compositions_match_set_union_and_materialization() -> None:
    rng = random.Random(0xC04)
    universe = tuple(f"C{index}" for index in range(10))
    for case in range(16):
        member_names = tuple(
            tuple(name for name in universe if rng.random() < 0.35) for _ in range(3)
        )
        members = tuple(
            snapshot(f"random-{case}-{index}", *names) for index, names in enumerate(member_names)
        )
        expected = set().union(*(set(names) for names in member_names))
        bridge = f"Bridge{case}"
        removal = min(expected) if expected else None
        delta = OntologyDelta(
            add_axioms={declaration(bridge)},
            remove_axioms=() if removal is None else {declaration(removal)},
        )
        if removal is not None:
            expected.remove(removal)
        expected.add(bridge)
        composite = compose_views(*members, delta=delta)
        reversed_composite = compose_views(*reversed(members), delta=delta)
        materialized = composite.materialize()

        assert {
            item.entity.iri.value.rsplit("#", 1)[-1] for item in composite.iter_axioms()
        } == expected
        assert composite.structural_fingerprint == reversed_composite.structural_fingerprint
        assert composite.logical_fingerprint == reversed_composite.logical_fingerprint
        assert materialized.structural_fingerprint == composite.structural_fingerprint
        assert materialized.logical_fingerprint == composite.logical_fingerprint


def test_duplicate_axioms_collapse_and_retain_every_member_origin() -> None:
    first = snapshot("same", "A")
    second = snapshot("same", "A")
    composite = compose_views(first, second, roles=("left", "right"))
    axiom = declaration("A")

    assert tuple(composite.iter_axioms()) == (axiom,)
    origins = composite.origins_for(axiom)
    assert len(origins) == 2
    assert len({origin.document_key for origin in origins}) == 2
    assert set(composite.member_roles.values()) == {"left", "right"}


def test_composite_exposes_only_its_well_defined_closure_scope() -> None:
    first = snapshot("same", "A")
    second = snapshot("same", "A")
    composite = compose_views(first, second)
    assert first.root_document_key == second.root_document_key

    with pytest.raises(ValueError, match="CLOSURE"):
        tuple(composite.iter_axioms(scope=AxiomScope.ROOT))
    with pytest.raises(ValueError, match="CLOSURE"):
        tuple(
            composite.iter_axioms(
                scope=AxiomScope.DOCUMENT,
                document_key=first.root_document_key,
            )
        )
    with pytest.raises(ValueError, match="document_key"):
        tuple(composite.iter_axioms(document_key=first.root_document_key))


def test_nested_composites_flatten_structurally_and_overlap_is_rejected() -> None:
    first = snapshot("one", "A")
    second = snapshot("two", "B")
    third = snapshot("three", "C")
    nested = compose_views(first, second)
    outer = compose_views(nested, third)
    flat = compose_views(first, second, third)

    assert len(outer.members) == 3
    assert tuple(outer.iter_axioms()) == tuple(flat.iter_axioms())
    assert outer.structural_fingerprint == flat.structural_fingerprint
    assert outer.logical_fingerprint == flat.logical_fingerprint
    assert outer.signature_fingerprint == flat.signature_fingerprint

    with pytest.raises(DeltaError) as overlap:
        compose_views(nested, first)
    assert overlap.value.code == "COMPOSITION_CYCLE"


def test_composite_intersects_encoded_view_schema_capabilities() -> None:
    first = snapshot("one", "A")
    second = snapshot("two", "B")
    third = snapshot("three", "C")
    object.__setattr__(
        first,
        "_capabilities",
        replace(
            first.capabilities,
            encoded_view_schemas={
                "urn:schema:common": 3,
                "urn:schema:first-only": 1,
            },
        ),
    )
    object.__setattr__(
        second,
        "_capabilities",
        replace(
            second.capabilities,
            encoded_view_schemas={
                "urn:schema:common": 1,
                "urn:schema:second-only": 2,
            },
        ),
    )
    object.__setattr__(
        third,
        "_capabilities",
        replace(
            third.capabilities,
            encoded_view_schemas={
                "urn:schema:common": 2,
                "urn:schema:third-only": 1,
            },
        ),
    )

    direct = compose_views(first, second, third)
    nested = compose_views(compose_views(first, second), third)

    assert dict(direct.capabilities.encoded_view_schemas) == {"urn:schema:common": 1}
    assert nested.capabilities.encoded_view_schemas == (direct.capabilities.encoded_view_schemas)


def test_shape_role_self_and_member_limits_are_checked_before_iteration() -> None:
    first = snapshot("one", "A")
    second = snapshot("two", "B")
    with pytest.raises(ValueError):
        compose_views(first)
    with pytest.raises(ValueError):
        compose_views(first, second, roles=("only",))
    duplicate_roles = compose_views(first, second, roles=("member", "member"))
    assert tuple(duplicate_roles.member_roles.values()) == ("member", "member")
    with pytest.raises(ValueError, match="nonempty"):
        compose_views(first, second, roles=("", "member"))
    with pytest.raises(ValueError, match="UTF-8"):
        compose_views(first, second, roles=("\ud800", "member"))
    with pytest.raises(DeltaError) as self_reference:
        compose_views(first, first)
    assert self_reference.value.code == "COMPOSITION_SELF_REFERENCE"

    broad_limit = replace(ParseLimits(), max_composite_members=8)
    tight_limit = replace(ParseLimits(), max_composite_members=2)
    limited_first = snapshot("limited-one", "A", limits=broad_limit)
    limited_second = snapshot("limited-two", "B", limits=tight_limit)
    limited_third = snapshot("limited-three", "C", limits=broad_limit)
    with pytest.raises(ResourceLimitError) as member_limit:
        compose_views(limited_first, limited_second, limited_third)
    assert member_limit.value.limit == "max_composite_members"


def test_bridge_strict_idempotent_removal_and_base_binding() -> None:
    first = snapshot("one", "A")
    second = snapshot("two", "B")
    base = compose_views(first, second)
    existing = declaration("A")
    absent = declaration("C")

    with pytest.raises(DeltaError) as duplicate:
        compose_views(first, second, delta=OntologyDelta(add_axioms={existing}))
    assert duplicate.value.code == "DELTA_ADD_EXISTS"
    with pytest.raises(DeltaError) as missing:
        compose_views(first, second, delta=OntologyDelta(remove_axioms={absent}))
    assert missing.value.code == "DELTA_REMOVE_ABSENT"

    replay = compose_views(
        first,
        second,
        delta=OntologyDelta(
            add_axioms={existing},
            remove_axioms={absent},
            policy=DeltaPolicy.IDEMPOTENT,
        ),
    )
    assert replay.delta.is_empty
    assert replay.structural_fingerprint == base.structural_fingerprint

    bound = compose_views(
        first,
        second,
        delta=OntologyDelta(
            add_axioms={absent},
            expected_base_fingerprint=base.structural_fingerprint,
        ),
    )
    assert bound.contains(absent)
    wrong = replace(base.structural_fingerprint, digest=b"w" * 32)
    with pytest.raises(DeltaBaseMismatchError):
        compose_views(
            first,
            second,
            delta=OntologyDelta(
                add_axioms={absent},
                expected_base_fingerprint=wrong,
            ),
        )

    duplicate_first = snapshot("duplicate", "A")
    duplicate_second = snapshot("duplicate", "A")
    removed_everywhere = compose_views(
        duplicate_first,
        duplicate_second,
        delta=OntologyDelta(remove_axioms={existing}),
    )
    assert not removed_everywhere.contains(existing)
    assert removed_everywhere.origins_for(existing) == ()

    note = Annotation(
        AnnotationProperty(IRI("urn:test#label")),
        IRI("urn:test#note"),
    )
    annotated = compose_views(
        first,
        second,
        delta=OntologyDelta(add_ontology_annotations={note}),
    )
    assert note in annotated.ontology_annotations()
    assert annotated.origins_for(note)


def test_concurrent_composite_caches_publish_one_immutable_result() -> None:
    composite = compose_views(
        snapshot("source", "A"),
        snapshot("target", "B"),
        delta=OntologyDelta(add_axioms={declaration("Bridge")}),
    )

    def read(_index: int):
        return (
            composite.structural_fingerprint,
            composite.logical_fingerprint,
            composite.signature_fingerprint,
            composite.report,
            composite.origin_index,
            tuple(composite.iter_axioms()),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        values = tuple(pool.map(read, range(64)))
    assert all(item[:3] == values[0][:3] for item in values)
    assert all(item[3] is values[0][3] for item in values)
    assert all(item[4] is values[0][4] for item in values)
    assert all(item[5] == values[0][5] for item in values)


def test_coercion_option_checks_apply_to_every_composite_leaf() -> None:
    first = snapshot("one", "A")
    second = snapshot("two", "B")
    composite = compose_views(first, second)
    assert coerce_snapshot(composite, options=first.load_options) is composite
    with pytest.raises(OptionConflictError) as conflict:
        coerce_snapshot(composite, options=LoadOptions())
    assert getattr(conflict.value, "code", None) == "VIEW_IMPORT_OPTION_CONFLICT"
    with pytest.raises(OptionConflictError) as resolver_conflict:
        coerce_snapshot(composite, resolver=object())  # type: ignore[arg-type]
    assert resolver_conflict.value.code == "VIEW_RESOLVER_CONFLICT"
    with pytest.raises(OptionConflictError) as backend_conflict:
        coerce_snapshot(
            composite,
            options=replace(first.load_options, backend=BackendPreference.NATIVE),
        )
    assert backend_conflict.value.code == "VIEW_BACKEND_CONFLICT"
    with pytest.raises(OptionConflictError) as source_map_conflict:
        coerce_snapshot(
            composite,
            options=replace(first.load_options, preserve_source_map=True),
        )
    assert source_map_conflict.value.code == "VIEW_SOURCE_MAP_CONFLICT"
