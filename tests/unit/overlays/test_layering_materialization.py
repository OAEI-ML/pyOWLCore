from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from pyowl_core import (
    AxiomScope,
    DeltaPolicy,
    OntologyDelta,
    OntologyOverlay,
    OntologySnapshot,
    OntologyView,
    OverlayPerformanceWarning,
    ParseLimits,
    ResourceLimitError,
    apply_delta,
    coerce_snapshot,
)

from .conftest import declaration, snapshot


def test_overlay_queries_keep_base_identity_and_apply_delta_to_closure_only() -> None:
    base = snapshot("A", "B")
    added = declaration("C")
    removed = declaration("A")
    overlay = apply_delta(
        base,
        OntologyDelta(add_axioms={added}, remove_axioms={removed}),
    )

    assert overlay.base is base
    assert isinstance(overlay, OntologyView)
    assert not isinstance(overlay, OntologySnapshot)
    assert overlay.contains(added)
    assert not overlay.contains(removed)
    assert tuple(overlay.iter_axioms()) == tuple(sorted({declaration("B"), added}))
    assert not overlay.contains(added, scope=AxiomScope.ROOT)
    assert overlay.contains(removed, scope=AxiomScope.ROOT)
    assert tuple(overlay.iter_axioms(scope=AxiomScope.ROOT)) == tuple(
        base.iter_axioms(scope=AxiomScope.ROOT)
    )
    assert tuple(
        overlay.iter_axioms(
            scope=AxiomScope.DOCUMENT,
            document_key=base.root_document_key,
        )
    ) == tuple(
        base.iter_axioms(
            scope=AxiomScope.DOCUMENT,
            document_key=base.root_document_key,
        )
    )
    assert coerce_snapshot(overlay) is overlay
    assert overlay.view(OntologyOverlay) is overlay


def test_compaction_and_materialization_preserve_every_fingerprint() -> None:
    base = snapshot("A", "B")
    first = apply_delta(base, OntologyDelta(add_axioms={declaration("C")}))
    second = apply_delta(first, OntologyDelta(remove_axioms={declaration("A")}))
    layered = apply_delta(second, OntologyDelta(add_axioms={declaration("D")}))
    compacted = layered.compact()
    materialized = layered.materialize()

    assert layered.depth == 3
    assert compacted.depth == 1
    assert compacted.base is base
    assert tuple(compacted.iter_axioms()) == tuple(layered.iter_axioms())
    for axiom in layered.iter_axioms():
        assert compacted.origins_for(axiom) == layered.origins_for(axiom)
        assert materialized.origin_index.origins_for(axiom) == layered.origins_for(axiom)
    for candidate in (compacted, materialized):
        assert candidate.structural_fingerprint == layered.structural_fingerprint
        assert candidate.logical_fingerprint == layered.logical_fingerprint
        assert candidate.signature_fingerprint == layered.signature_fingerprint
    assert isinstance(materialized, OntologySnapshot)
    assert materialized.structural_context == layered.structural_context


def test_compaction_retains_accumulated_idempotent_no_op_provenance() -> None:
    base = snapshot("A")
    first = apply_delta(
        base,
        OntologyDelta(add_axioms={declaration("A")}, policy=DeltaPolicy.IDEMPOTENT),
    )
    second = apply_delta(
        first,
        OntologyDelta(remove_axioms={declaration("missing")}, policy=DeltaPolicy.IDEMPOTENT),
    )
    compacted = second.compact()

    assert compacted.no_op_add_axioms == first.no_op_add_axioms
    assert compacted.no_op_remove_axioms == second.no_op_remove_axioms
    assert {item.code for item in compacted.report.diagnostics} >= {
        "DELTA_IDEMPOTENT_ADD_NOOP",
        "DELTA_IDEMPOTENT_REMOVE_NOOP",
    }


def test_equal_effective_histories_have_equal_semantic_fingerprints() -> None:
    base = snapshot("A")
    x = declaration("X")
    y = declaration("Y")
    direct = apply_delta(base, OntologyDelta(add_axioms={y}))
    history = apply_delta(base, OntologyDelta(add_axioms={x}))
    history = apply_delta(history, OntologyDelta(add_axioms={y}))
    history = apply_delta(history, OntologyDelta(remove_axioms={x}))

    assert tuple(direct.iter_axioms()) == tuple(history.iter_axioms())
    assert direct.structural_fingerprint == history.structural_fingerprint
    assert direct.logical_fingerprint == history.logical_fingerprint
    assert direct.signature_fingerprint == history.signature_fingerprint
    assert direct.edit_chain_digest != history.edit_chain_digest


@pytest.mark.filterwarnings("ignore:overlay delta exceeds ten percent")
def test_randomized_histories_match_independent_set_and_compaction() -> None:
    rng = random.Random(0x0A_E1)
    base_names = {f"A{i}" for i in range(12)}
    base = snapshot(*sorted(base_names))
    effective = set(base_names)
    view: OntologyView = base
    for _ in range(80):
        name = f"A{rng.randrange(24)}"
        if name in effective:
            delta = OntologyDelta(remove_axioms={declaration(name)})
            effective.remove(name)
        else:
            delta = OntologyDelta(add_axioms={declaration(name)})
            effective.add(name)
        view = apply_delta(view, delta)
        if isinstance(view, OntologyOverlay) and view.depth == 8:
            view = view.compact()
    assert {item.entity.iri.value.rsplit("#", 1)[-1] for item in view.iter_axioms()} == effective
    assert isinstance(view, OntologyOverlay)
    compacted = view.compact()
    materialized = view.materialize()
    assert tuple(compacted.iter_axioms()) == tuple(materialized.iter_axioms())
    assert compacted.structural_fingerprint == materialized.structural_fingerprint
    assert compacted.logical_fingerprint == materialized.logical_fingerprint
    assert compacted.signature_fingerprint == materialized.signature_fingerprint


def test_depth_limit_and_soft_recommendation_are_explicit() -> None:
    limited = snapshot("A", limits=replace(ParseLimits(), max_overlay_depth=2))
    first = apply_delta(limited, OntologyDelta(add_axioms={declaration("B")}))
    second = apply_delta(first, OntologyDelta(add_axioms={declaration("C")}))
    with pytest.raises(ResourceLimitError) as depth:
        apply_delta(second, OntologyDelta(add_axioms={declaration("D")}))
    assert depth.value.limit == "max_overlay_depth"

    delta_limited = snapshot(
        "A",
        limits=replace(ParseLimits(), max_delta_entries=1),
    )
    with pytest.raises(ResourceLimitError) as delta_entries:
        apply_delta(
            delta_limited,
            OntologyDelta(add_axioms={declaration("B"), declaration("C")}),
        )
    assert delta_entries.value.limit == "max_delta_entries"
    one_entry = apply_delta(
        delta_limited,
        OntologyDelta(add_axioms={declaration("B")}),
    )
    with pytest.raises(ResourceLimitError) as cumulative_entries:
        apply_delta(one_entry, OntologyDelta(add_axioms={declaration("C")}))
    assert cumulative_entries.value.limit == "max_delta_entries"

    wide = snapshot(*(f"A{index}" for index in range(20)))
    with pytest.warns(OverlayPerformanceWarning, match="ten percent"):
        apply_delta(
            wide,
            OntologyDelta(add_axioms={declaration("X"), declaration("Y"), declaration("Z")}),
        )

    deep = snapshot("A", limits=replace(ParseLimits(), max_overlay_depth=40))
    current = deep
    with pytest.warns(OverlayPerformanceWarning):
        for index in range(32):
            current = apply_delta(
                current,
                OntologyDelta(add_axioms={declaration(f"D{index}")}),
            )
    assert current.depth == 32


def test_concurrent_lazy_publication_is_stable() -> None:
    overlay = apply_delta(snapshot("A", "B"), OntologyDelta(add_axioms={declaration("C")}))

    def read(_index: int):
        return (
            overlay.structural_fingerprint,
            overlay.logical_fingerprint,
            overlay.signature_fingerprint,
            overlay.report,
            overlay.origin_index,
            tuple(overlay.iter_axioms()),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        values = tuple(pool.map(read, range(64)))
    assert all(item[:3] == values[0][:3] for item in values)
    assert all(item[3] is values[0][3] for item in values)
    assert all(item[4] is values[0][4] for item in values)
    assert all(item[5] == values[0][5] for item in values)


def test_idempotent_empty_histories_share_semantic_fingerprints() -> None:
    base = snapshot("A")
    first = apply_delta(base, OntologyDelta(policy=DeltaPolicy.IDEMPOTENT))
    second = apply_delta(
        base,
        OntologyDelta(add_axioms={declaration("A")}, policy=DeltaPolicy.IDEMPOTENT),
    )
    cancelled = apply_delta(base, OntologyDelta(add_axioms={declaration("B")}))
    cancelled = apply_delta(cancelled, OntologyDelta(remove_axioms={declaration("B")}))
    for candidate in (first, second, cancelled, cancelled.materialize()):
        assert candidate.structural_fingerprint == base.structural_fingerprint
        assert candidate.logical_fingerprint == base.logical_fingerprint
        assert candidate.signature_fingerprint == base.signature_fingerprint
