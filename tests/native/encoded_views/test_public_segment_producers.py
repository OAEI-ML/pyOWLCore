from __future__ import annotations

import pytest

from pyowl_core import (
    IRI,
    AxiomScope,
    BackendPreference,
    CanonicalSet,
    Class,
    Declaration,
    ImportPolicy,
    LoadOptions,
    OntologyDelta,
    ParseLimits,
    SubClassOf,
    apply_delta,
    canonical_bytes,
    compose_views,
    load_snapshot,
)
from pyowl_core.backends.native_views import (
    EncodedStructuralViewV2,
    produce_encoded_structural_view_v2,
)

from ._independent import decode_segmented_root_canonical_bytes
from ._support import scalar_root_bytes


def _snapshot(identity: str, *body: str):  # type: ignore[no-untyped-def]
    source = f"Prefix(:=<urn:segments#>) Ontology(<urn:{identity}> {' '.join(body)})"
    return load_snapshot(
        source.encode(),
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            imports=ImportPolicy.IGNORE,
        ),
    )


def _decoded_pairs(view: EncodedStructuralViewV2) -> tuple[tuple[int, bytes], ...]:
    decoded = decode_segmented_root_canonical_bytes(
        view,
        expected_owner=view.owner,
        expected_scope=view.scope,
        expected_document_key=view.document_key,
    )
    return tuple((root.root_kind, root.canonical) for root in decoded.roots)


def test_public_overlay_references_anchor_columns_and_publishes_only_cumulative_delta() -> None:
    base = _snapshot(
        "overlay-base",
        "Declaration(Class(:A))",
        "Declaration(Class(:B))",
    )
    first = apply_delta(
        base,
        OntologyDelta(
            add_axioms=CanonicalSet((Declaration(Class(IRI("urn:segments#C"))),)),
        ),
    )
    overlay = apply_delta(
        first,
        OntologyDelta(
            add_axioms=CanonicalSet((Declaration(Class(IRI("urn:segments#D"))),)),
            remove_axioms=CanonicalSet((Declaration(Class(IRI("urn:segments#A"))),)),
        ),
    )
    base_view = base.view(EncodedStructuralViewV2)
    encoded = overlay.view(EncodedStructuralViewV2)

    assert tuple(segment.role for segment in encoded.segments) == (2, 3)
    assert encoded.segments[0].posting_mode == 2
    assert bytes(encoded.segments[0].root_ids) == (1).to_bytes(4, "little")
    assert encoded.segments[0].owner is base
    assert encoded.segments[0].source is not None
    assert all(
        encoded.segments[0].source.buffers[name].obj is base_view.buffers[name].obj
        for name in base_view.buffers
    )
    assert len(encoded.buffers["root_ids"]) // 4 == 2
    assert _decoded_pairs(encoded) == scalar_root_bytes(overlay)


def test_segment_matching_does_not_sum_independently_bounded_canonical_rows() -> None:
    base = _snapshot(
        "overlay-row-work",
        *(f"Declaration(Class(:C{index}))" for index in range(12)),
    )
    removed = Declaration(Class(IRI("urn:segments#C11")))
    overlay = apply_delta(
        base,
        OntologyDelta(remove_axioms=CanonicalSet((removed,))),
    )
    base_rows = scalar_root_bytes(base)
    row_limit = max(len(payload) for _kind, payload in base_rows)

    encoded = produce_encoded_structural_view_v2(
        overlay,
        limits=ParseLimits(max_canonical_work=row_limit),
    )

    assert sum(len(payload) for _kind, payload in base_rows) > row_limit
    assert tuple(segment.role for segment in encoded.segments) == (2,)
    assert _decoded_pairs(encoded) == scalar_root_bytes(overlay)


def test_public_composite_reuses_member_columns_with_postings_and_local_bridge() -> None:
    left = _snapshot(
        "composite-left",
        "Declaration(Class(:A))",
        "Declaration(Class(:B))",
    )
    right = _snapshot("composite-right", "Declaration(Class(:D))")
    removed = Declaration(Class(IRI("urn:segments#A")))
    bridge = SubClassOf(
        Class(IRI("urn:segments#B")),
        Class(IRI("urn:segments#D")),
    )
    left_view = left.view(EncodedStructuralViewV2)
    right_view = right.view(EncodedStructuralViewV2)
    composite = compose_views(
        left,
        right,
        delta=OntologyDelta(
            add_axioms=CanonicalSet((bridge,)),
            remove_axioms=CanonicalSet((removed,)),
        ),
    )
    encoded = composite.view(EncodedStructuralViewV2)

    assert tuple(segment.role for segment in encoded.segments) == (4, 4, 5)
    members = encoded.segments[:2]
    assert [segment.member_token for segment in members] == sorted(
        segment.member_token for segment in members
    )
    assert {segment.posting_mode for segment in members} == {0, 2}
    assert len(encoded.buffers["root_ids"]) // 4 == 1
    source_exporters = {
        id(view.buffers[name].obj) for view in (left_view, right_view) for name in view.buffers
    }
    retained_exporters = {
        id(segment.source.buffers[name].obj)
        for segment in members
        if segment.source is not None
        for name in segment.source.buffers
    }
    assert retained_exporters == source_exporters
    assert _decoded_pairs(encoded) == scalar_root_bytes(composite)
    assert canonical_bytes(bridge) in {value for _kind, value in _decoded_pairs(encoded)}


def test_public_composite_publishes_explicit_anonymous_scope_maps() -> None:
    left = _snapshot("anonymous", "ClassAssertion(:A _:x)")
    right = _snapshot("anonymous", "ClassAssertion(:A _:x)")
    composite = compose_views(left, right)
    encoded = composite.view(EncodedStructuralViewV2)

    assert tuple(segment.role for segment in encoded.segments) == (4, 4)
    assert all(len(segment.anonymous_scope_map) == 64 for segment in encoded.segments)
    assert len(encoded.buffers["root_ids"]) == 0
    assert _decoded_pairs(encoded) == scalar_root_bytes(composite)


def test_overlay_root_and_document_selections_reference_base_without_delta_rows() -> None:
    base = _snapshot("scoped", "Declaration(Class(:A))")
    overlay = apply_delta(
        base,
        OntologyDelta(
            add_axioms=CanonicalSet((Declaration(Class(IRI("urn:segments#B"))),)),
        ),
    )
    for scope, document_key in (
        (AxiomScope.ROOT, None),
        (AxiomScope.DOCUMENT, base.root_document_key),
    ):
        encoded = overlay.view(
            EncodedStructuralViewV2,
            scope=scope,
            document_key=document_key,
        )
        assert tuple(segment.role for segment in encoded.segments) == (2,)
        assert len(encoded.buffers["root_ids"]) == 0
        assert _decoded_pairs(encoded) == tuple(
            sorted(
                (2, canonical_bytes(value))
                for value in overlay.iter_axioms(scope=scope, document_key=document_key)
            )
        )


def test_materialized_segments_is_explicit_and_never_changes_the_segmented_default() -> None:
    left = _snapshot("materialized-left", "Declaration(Class(:A))")
    right = _snapshot("materialized-right", "Declaration(Class(:B))")
    composite = compose_views(left, right)

    segmented = composite.view(EncodedStructuralViewV2)
    materialized = composite.view(
        EncodedStructuralViewV2,
        materialize_segments=True,
    )

    assert segmented is composite.view(EncodedStructuralViewV2)
    assert materialized is composite.view(
        EncodedStructuralViewV2,
        materialize_segments=True,
    )
    assert segmented is not materialized
    assert tuple(segment.role for segment in segmented.segments) == (4, 4)
    assert len(segmented.buffers["root_ids"]) == 0
    assert tuple(segment.role for segment in materialized.segments) == (1,)
    assert len(materialized.buffers["root_ids"]) // 4 == 2
    assert _decoded_pairs(segmented) == scalar_root_bytes(composite)
    assert _decoded_pairs(materialized) == scalar_root_bytes(composite)
    with pytest.raises(TypeError, match="materialize_segments must be bool"):
        composite.view(EncodedStructuralViewV2, materialize_segments=1)
