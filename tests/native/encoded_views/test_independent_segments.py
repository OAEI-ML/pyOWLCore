from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from pyowl_core import (
    BackendPreference,
    CanonicalSet,
    ImportPolicy,
    LoadOptions,
    OntologyDelta,
    apply_delta,
    canonical_bytes,
    compose_views,
    load_snapshot,
)
from pyowl_core.backends import native_views
from pyowl_core.backends.native_views import (
    EncodedStructuralViewV1,
    produce_encoded_structural_view_v1,
)
from pyowl_core.document.document import Fingerprint
from pyowl_core.document.snapshot import AxiomScope, OntologySnapshot
from pyowl_core.exceptions import BackendProtocolError
from pyowl_core.model.axioms import AxiomNode
from tests.native.encoded_views import _independent as independent_decoder
from tests.native.encoded_views._independent import (
    IndependentSegmentDecode,
    IndependentSegmentError,
    decode_root_canonical_bytes,
    decode_segmented_root_canonical_bytes,
)
from tests.native.encoded_views._support import scalar_root_bytes

_BUFFER_NAMES = (
    "root_kinds",
    "root_ids",
    "node_tags",
    "node_field_offsets",
    "field_kinds",
    "field_values",
    "field_lengths",
    "item_kinds",
    "item_values",
    "item_lengths",
    "scalar_bytes",
)


@dataclass(slots=True)
class _SegmentFixture:
    role: int
    owner: object
    source: object | None
    posting_mode: int
    root_ids: memoryview
    member_token: bytes | None = None
    anonymous_scope_map: memoryview = field(default_factory=lambda: memoryview(b""))


@dataclass(slots=True)
class _ViewFixture:
    schema_name: str
    schema_version: int
    model_schema: int
    owner: object
    buffers: Mapping[str, memoryview]
    descriptor: bytes
    structural_fingerprint: Fingerprint
    segments: tuple[object, ...]
    scope: AxiomScope
    document_key: str | None


class _ScalarTraversalTrap:
    calls: int = 0

    def iter_axioms(self) -> None:
        self.calls += 1
        raise AssertionError("segmented decoder crossed the scalar traversal boundary")


def _snapshot(identity: str, *axioms: str) -> OntologySnapshot:
    body = " ".join(axioms)
    source = f"Prefix(:=<urn:segment#>) Ontology(<urn:{identity}> {body})".encode()
    return load_snapshot(
        source,
        options=LoadOptions(
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
        ),
    )


def _direct(identity: str, *axioms: str) -> EncodedStructuralViewV1:
    return produce_encoded_structural_view_v1(_snapshot(identity, *axioms))


def _digest(label: str) -> Fingerprint:
    return Fingerprint("sha256", 1, hashlib.sha256(label.encode()).digest())


def _view(
    local: EncodedStructuralViewV1,
    label: str,
    *,
    owner: object | None = None,
    segments: tuple[object, ...] | None = None,
    fingerprint: Fingerprint | None = None,
) -> _ViewFixture:
    selected_owner = local.owner if owner is None else owner
    selected_segments = (
        (
            _SegmentFixture(
                1,
                selected_owner,
                None,
                0,
                memoryview(b""),
            ),
        )
        if segments is None
        else segments
    )
    return _ViewFixture(
        local.schema_name,
        local.schema_version,
        local.model_schema,
        selected_owner,
        local.buffers,
        local.descriptor,
        _digest(label) if fingerprint is None else fingerprint,
        selected_segments,
        local.scope,
        local.document_key,
    )


def _postings(*root_ids: int) -> memoryview:
    return memoryview(b"".join(root_id.to_bytes(4, "little") for root_id in root_ids))


def _scope_map(*rows: tuple[bytes, bytes]) -> memoryview:
    return memoryview(b"".join(source + target for source, target in sorted(rows)))


def _pairs(result: IndependentSegmentDecode) -> tuple[tuple[int, bytes], ...]:
    return tuple((root.root_kind, root.canonical) for root in result.roots)


def _sorted_pairs(*groups: tuple[tuple[int, bytes], ...]) -> tuple[tuple[int, bytes], ...]:
    return tuple(sorted((item for group in groups for item in group), key=lambda item: item))


def _axiom_at(view: EncodedStructuralViewV1, root_id: int) -> AxiomNode:
    target = decode_root_canonical_bytes(view.buffers)[root_id - 1][1]
    return next(value for value in view.owner.iter_axioms() if canonical_bytes(value) == target)


def test_direct_reference_lane_matches_local_columns() -> None:
    direct = _direct(
        "direct",
        "Declaration(Class(:A))",
        "Declaration(Class(:B))",
    )
    decoded = independent_decoder._decode_segmented_root_canonical_bytes(direct)

    assert _pairs(decoded) == decode_root_canonical_bytes(direct.buffers)
    assert tuple(root.locator.local_root_id for root in decoded.roots) == (1, 2)
    assert all(root.locator.member_tokens == () for root in decoded.roots)
    assert decoded.proof.retained_views == (direct,)
    assert decoded.proof.referenced_buffer_views == ()
    assert decoded.proof.referenced_buffer_copy_bytes == 0
    assert decoded.proof.scalar_traversal_calls == 0


def test_overlay_exclusion_and_delta_match_explicit_root_result_without_base_copy() -> None:
    base_columns = _direct(
        "base",
        "Declaration(Class(:A))",
        "Declaration(Class(:B))",
    )
    trap = _ScalarTraversalTrap()
    base = _view(base_columns, "base", owner=trap)
    delta = _direct("delta", "Declaration(Class(:C))")
    top_owner = apply_delta(
        base_columns.owner,
        OntologyDelta(
            add_axioms=CanonicalSet((_axiom_at(delta, 1),)),
            remove_axioms=CanonicalSet((_axiom_at(base_columns, 1),)),
        ),
    )
    overlay = _view(
        delta,
        "overlay",
        owner=top_owner,
        segments=(
            _SegmentFixture(2, base.owner, base, 2, _postings(1)),
            _SegmentFixture(3, top_owner, None, 0, _postings()),
        ),
    )

    decoded = independent_decoder._decode_segmented_root_canonical_bytes(overlay)
    base_roots = decode_root_canonical_bytes(base.buffers)
    delta_roots = decode_root_canonical_bytes(delta.buffers)

    assert _pairs(decoded) == scalar_root_bytes(top_owner)
    assert _pairs(decoded) == _sorted_pairs(base_roots[1:], delta_roots)
    assert {root.locator.origin_fingerprint for root in decoded.roots} == {
        base.structural_fingerprint.digest,
        overlay.structural_fingerprint.digest,
    }
    expected_views = tuple(base.buffers[name] for name in _BUFFER_NAMES)
    assert len(decoded.proof.referenced_buffer_views) == len(expected_views)
    assert all(
        observed is expected
        for observed, expected in zip(
            decoded.proof.referenced_buffer_views,
            expected_views,
            strict=True,
        )
    )
    assert decoded.proof.referenced_buffer_copy_bytes == 0
    assert decoded.proof.scalar_traversal_calls == trap.calls == 0
    assert any(retained is base for retained in decoded.proof.retained_views)


def test_two_member_composite_include_all_and_bridge_are_deterministic() -> None:
    left = _direct(
        "left",
        "Declaration(Class(:A))",
        "Declaration(Class(:B))",
    )
    right = _direct("right", "Declaration(Class(:C))")
    bridge = _direct("bridge", "SubClassOf(:B :C)")
    top_owner = compose_views(
        left.owner,
        right.owner,
        delta=OntologyDelta(
            add_axioms=CanonicalSet((_axiom_at(bridge, 1),)),
            remove_axioms=CanonicalSet((_axiom_at(left, 1),)),
        ),
    )
    left_token = b"a" * 32
    right_token = b"b" * 32
    composite = _view(
        bridge,
        "composite",
        owner=top_owner,
        segments=(
            _SegmentFixture(4, left.owner, left, 2, _postings(1), left_token),
            _SegmentFixture(4, right.owner, right, 0, _postings(), right_token),
            _SegmentFixture(5, top_owner, None, 0, _postings()),
        ),
    )

    first = independent_decoder._decode_segmented_root_canonical_bytes(composite)
    second = independent_decoder._decode_segmented_root_canonical_bytes(composite)
    left_roots = decode_root_canonical_bytes(left.buffers)
    right_roots = decode_root_canonical_bytes(right.buffers)
    bridge_roots = decode_root_canonical_bytes(bridge.buffers)

    assert first == second
    assert _pairs(first) == scalar_root_bytes(top_owner)
    assert _pairs(first) == _sorted_pairs((left_roots[1],), right_roots, bridge_roots)
    by_canonical = {root.canonical: root for root in first.roots}
    assert by_canonical[left_roots[1][1]].locator.member_tokens == (left_token,)
    assert by_canonical[right_roots[0][1]].locator.member_tokens == (right_token,)
    assert by_canonical[bridge_roots[0][1]].locator.member_tokens == ()
    assert first.proof.referenced_buffer_copy_bytes == 0
    assert first.proof.scalar_traversal_calls == 0
    assert len(first.proof.referenced_buffer_views) == 2 * len(_BUFFER_NAMES)


def test_recursive_source_local_include_and_exclude_do_not_flatten_nested_base() -> None:
    base = _direct(
        "nested-base",
        "Declaration(Class(:A))",
        "Declaration(Class(:B))",
    )
    delta = _direct("nested-delta", "Declaration(Class(:C))")
    inner_owner = object()
    inner = _view(
        delta,
        "nested-overlay",
        owner=inner_owner,
        segments=(
            _SegmentFixture(2, base.owner, base, 2, _postings(1)),
            _SegmentFixture(3, inner_owner, None, 0, _postings()),
        ),
    )
    right = _direct("nested-right", "Declaration(Class(:D))")
    empty = _direct("nested-empty")
    left_token = b"a" * 32
    right_token = b"b" * 32

    def outer(mode: int) -> _ViewFixture:
        owner = object()
        return _view(
            empty,
            f"nested-outer-{mode}",
            owner=owner,
            segments=(
                _SegmentFixture(4, inner.owner, inner, mode, _postings(1), left_token),
                _SegmentFixture(4, right.owner, right, 0, _postings(), right_token),
            ),
        )

    included = independent_decoder._decode_segmented_root_canonical_bytes(outer(1))
    excluded = independent_decoder._decode_segmented_root_canonical_bytes(outer(2))
    base_roots = decode_root_canonical_bytes(base.buffers)
    delta_roots = decode_root_canonical_bytes(delta.buffers)
    right_roots = decode_root_canonical_bytes(right.buffers)

    assert _pairs(included) == _sorted_pairs(delta_roots, right_roots)
    assert _pairs(excluded) == _sorted_pairs(base_roots[1:], right_roots)
    assert len(included.proof.referenced_buffer_views) == 3 * len(_BUFFER_NAMES)
    assert len(excluded.proof.referenced_buffer_views) == 3 * len(_BUFFER_NAMES)
    assert included.proof.referenced_buffer_copy_bytes == 0
    assert excluded.proof.referenced_buffer_copy_bytes == 0


def _member_segments(
    sources: tuple[object, ...], actual: object
) -> tuple[_SegmentFixture, ...]:
    tokens = cast(tuple[bytes, ...], cast(Any, actual)._source_tokens())
    mappings = cast(tuple[Mapping[bytes, bytes], ...], cast(Any, actual)._scope_replacements())
    rows = sorted(zip(tokens, sources, mappings, strict=True), key=lambda row: row[0])
    return tuple(
        _SegmentFixture(
            4,
            cast(Any, source).owner,
            source,
            0,
            _postings(),
            token,
            _scope_map(*mapping.items()),
        )
        for token, source, mapping in rows
    )


def test_anonymous_scope_maps_match_scalar_composite_exactly() -> None:
    left = _direct("anonymous", "ClassAssertion(:A _:x)")
    right = _direct("anonymous", "ClassAssertion(:A _:x)")
    actual = compose_views(left.owner, right.owner)
    empty = _direct("anonymous-empty")
    composite = _view(
        empty,
        "anonymous-composite",
        owner=actual,
        segments=_member_segments((left, right), actual),
        fingerprint=actual.structural_fingerprint,
    )

    decoded = independent_decoder._decode_segmented_root_canonical_bytes(composite)

    assert _pairs(decoded) == scalar_root_bytes(actual)
    assert len(decoded.roots) == 2
    assert decoded.roots[0].canonical != decoded.roots[1].canonical
    identities = {
        identity
        for root in decoded.roots
        for identity in root.anonymous_identities
    }
    assert len(identities) == 2
    assert len({identity.member_tokens for identity in identities}) == 2
    assert len({identity.document_scope for identity in identities}) == 2
    assert len({identity.local_key for identity in identities}) == 1


def test_nested_anonymous_scope_maps_compose_to_scalar_identity() -> None:
    left = _direct("nested-anonymous", "ClassAssertion(:A _:x)")
    right = _direct("nested-anonymous", "ClassAssertion(:A _:x)")
    inner_bridge = _direct("inner-bridge", "Declaration(Class(:InnerBridge))")
    inner_actual = compose_views(
        left.owner,
        right.owner,
        delta=OntologyDelta(add_axioms=CanonicalSet((_axiom_at(inner_bridge, 1),))),
    )
    inner = _view(
        inner_bridge,
        "inner-anonymous-composite",
        owner=inner_actual,
        segments=(
            *_member_segments((left, right), inner_actual),
            _SegmentFixture(5, inner_actual, None, 0, _postings()),
        ),
        fingerprint=inner_actual.structural_fingerprint,
    )

    third = _direct("nested-anonymous", "ClassAssertion(:A _:x)")
    outer_bridge = _direct("outer-bridge", "Declaration(Class(:OuterBridge))")
    outer_actual = compose_views(
        inner_actual,
        third.owner,
        delta=OntologyDelta(add_axioms=CanonicalSet((_axiom_at(outer_bridge, 1),))),
    )
    outer = _view(
        outer_bridge,
        "outer-anonymous-composite",
        owner=outer_actual,
        segments=(
            *_member_segments((inner, third), outer_actual),
            _SegmentFixture(5, outer_actual, None, 0, _postings()),
        ),
        fingerprint=outer_actual.structural_fingerprint,
    )

    decoded = independent_decoder._decode_segmented_root_canonical_bytes(outer)

    assert _pairs(decoded) == scalar_root_bytes(outer_actual)
    identities = {
        identity
        for root in decoded.roots
        for identity in root.anonymous_identities
    }
    assert len(identities) == 3
    assert len({identity.document_scope for identity in identities}) == 3
    assert all(identity.member_tokens for identity in identities)


def test_structural_dedup_retains_every_source_locator() -> None:
    left = _direct("dedup", "Declaration(Class(:A))")
    right = _direct("dedup", "Declaration(Class(:A))")
    actual = compose_views(left.owner, right.owner)
    empty = _direct("dedup-empty")
    composite = _view(
        empty,
        "dedup-composite",
        owner=actual,
        segments=_member_segments((left, right), actual),
        fingerprint=actual.structural_fingerprint,
    )

    decoded = independent_decoder._decode_segmented_root_canonical_bytes(composite)

    assert _pairs(decoded) == scalar_root_bytes(actual)
    assert len(decoded.roots) == 1
    assert len(decoded.roots[0].source_locators) == 2


def test_hostile_posting_cycle_and_duplicate_locator_fail_closed() -> None:
    source = _direct("hostile", "Declaration(Class(:A))")
    empty = _direct("hostile-empty")
    top_owner = object()
    out_of_range = _view(
        empty,
        "out-of-range",
        owner=top_owner,
        segments=(
            _SegmentFixture(2, source.owner, source, 2, _postings(2)),
        ),
    )
    with pytest.raises(IndependentSegmentError) as hostile:
        independent_decoder._decode_segmented_root_canonical_bytes(out_of_range)
    assert hostile.value.code == "INDEPENDENT_SEGMENTS"

    identity_map = _view(
        empty,
        "identity-map",
        owner=top_owner,
        segments=(
            _SegmentFixture(
                2,
                source.owner,
                source,
                0,
                _postings(),
                anonymous_scope_map=_scope_map((b"s" * 32, b"s" * 32)),
            ),
        ),
    )
    with pytest.raises(IndependentSegmentError) as hostile_map:
        independent_decoder._decode_segmented_root_canonical_bytes(identity_map)
    assert hostile_map.value.code == "INDEPENDENT_SEGMENTS"

    cyclic = _view(empty, "cycle", owner=top_owner)
    cyclic.segments = (
        _SegmentFixture(2, top_owner, cyclic, 0, _postings()),
    )
    with pytest.raises(IndependentSegmentError) as cycle:
        independent_decoder._decode_segmented_root_canonical_bytes(cyclic)
    assert cycle.value.code == "INDEPENDENT_CYCLE"

    token = b"x" * 32
    duplicate = _view(
        empty,
        "duplicate",
        owner=top_owner,
        segments=(
            _SegmentFixture(4, source.owner, source, 0, _postings(), token),
            _SegmentFixture(4, source.owner, source, 0, _postings(), token),
        ),
    )
    with pytest.raises(IndependentSegmentError) as locator:
        independent_decoder._decode_segmented_root_canonical_bytes(duplicate)
    assert locator.value.code == "INDEPENDENT_LOCATOR"


def test_referenced_fingerprint_mutation_fails_before_independent_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _direct("fingerprint-source", "Declaration(Class(:A))")
    empty = _direct("fingerprint-top")
    base = _SegmentFixture(2, source.owner, source, 0, _postings())
    candidate = _view(
        empty,
        "fingerprint-top",
        owner=empty.owner,
        segments=(base,),
    )
    candidate.structural_fingerprint = cast(
        Fingerprint,
        cast(Any, native_views)._fingerprint(candidate.buffers, candidate.segments),
    )

    decoded = decode_segmented_root_canonical_bytes(
        candidate,
        expected_owner=empty.owner,
        expected_scope=empty.scope,
        expected_document_key=empty.document_key,
    )
    assert _pairs(decoded) == decode_root_canonical_bytes(source.buffers)

    object.__setattr__(
        source,
        "structural_fingerprint",
        Fingerprint("sha256", 1, b"\x11" * 32),
    )
    independent_calls: list[object] = []

    def trap_independent_decode(view: object) -> IndependentSegmentDecode:
        independent_calls.append(view)
        raise AssertionError("independent decode ran before core validation")

    monkeypatch.setattr(
        independent_decoder,
        "_decode_segmented_root_canonical_bytes",
        trap_independent_decode,
    )
    with pytest.raises(BackendProtocolError) as raised:
        decode_segmented_root_canonical_bytes(
            candidate,
            expected_owner=empty.owner,
            expected_scope=empty.scope,
            expected_document_key=empty.document_key,
        )

    assert raised.value.code == "ENCODED_VIEW_FINGERPRINT"
    assert independent_calls == []
