from __future__ import annotations

import hashlib
import os
import random
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

import pyowl_core.model as m
from pyowl_core.model.anonymous import (  # type: ignore[attr-defined]
    AlphaCanonicalization,
    _bind_component_blank_nodes,
    _canonical_component_manifest,
)
from tests.reference.anonymous_v2 import ReferenceArc, canonicalize_document

ROOT = Path(__file__).resolve().parents[3]
ONTOLOGY_KEY = b"ontology-key"


def _labels(arcs: Iterable[ReferenceArc]) -> tuple[str, ...]:
    values: set[str] = set()
    for arc in arcs:
        values.add(arc.source)
        if arc.target is not None:
            values.add(arc.target)
    return tuple(sorted(values))


def _production_document(
    components: Iterable[Iterable[ReferenceArc]],
) -> tuple[bytes, bytes, tuple[bytes, ...], tuple[bytes, ...]]:
    """Adapt abstract oracle fixtures to the production component primitives."""

    solved: list[tuple[tuple[str, ...], AlphaCanonicalization]] = []
    for component in components:
        arcs = tuple(component)
        production = m.alpha_canonicalize_blank_nodes(
            tuple(
                m.BlankNodeArc(arc.source, arc.role, arc.target, arc.payload)
                for arc in arcs
            ),
            b"P" * 32,
            labels=_labels(arcs),
        )
        solved.append((_labels(arcs), production))
    manifest = _canonical_component_manifest(
        canonicalization.canonical_graph for _, canonicalization in solved
    )
    scope = m.canonical_document_scope(ONTOLOGY_KEY, canonical_graph=manifest)
    classes: dict[bytes, list[tuple[tuple[str, ...], AlphaCanonicalization]]] = {}
    for labels, canonicalization in solved:
        classes.setdefault(canonicalization.canonical_graph, []).append(
            (labels, canonicalization)
        )
    keys: list[bytes] = []
    for graph in sorted(classes):
        for ordinal, (_, canonicalization) in enumerate(
            sorted(classes[graph], key=lambda item: item[0])
        ):
            rebound = _bind_component_blank_nodes(
                canonicalization,
                scope,
                occurrence_ordinal=ordinal,
            )
            keys.extend(binding.individual.local_key for binding in rebound.bindings)
    graphs = tuple(sorted(item.canonical_graph for _, item in solved))
    return scope, manifest, graphs, tuple(keys)


def _flatten(components: Iterable[Iterable[ReferenceArc]]) -> tuple[ReferenceArc, ...]:
    return tuple(arc for component in components for arc in component)


def _fixture_components() -> tuple[tuple[ReferenceArc, ...], ...]:
    return (
        (
            ReferenceArc("a0", "peer", "a1", b"same"),
            ReferenceArc("a1", "peer", "a0", b"same"),
        ),
        (
            ReferenceArc("b0", "peer", "b1", b"same"),
            ReferenceArc("b1", "peer", "b0", b"same"),
        ),
        (ReferenceArc("c0", "solo", payload=b"different"),),
        (
            ReferenceArc("d0", "chain", "d1", b"left"),
            ReferenceArc("d1", "chain", "d2", b"right"),
            ReferenceArc("d0", "mark", payload=b"asymmetric"),
        ),
    )


def _permuted_components(seed: int) -> tuple[tuple[ReferenceArc, ...], ...]:
    generator = random.Random(seed)
    components = _fixture_components()
    original_labels = sorted({label for component in components for label in _labels(component)})
    replacements = [f"renamed-{seed}-{index}" for index in range(len(original_labels))]
    generator.shuffle(replacements)
    renamed = dict(zip(original_labels, replacements, strict=True))
    result: list[tuple[ReferenceArc, ...]] = []
    for component in components:
        arcs = [
            ReferenceArc(
                renamed[arc.source],
                arc.role,
                None if arc.target is None else renamed[arc.target],
                arc.payload,
            )
            for arc in component
        ]
        generator.shuffle(arcs)
        result.append(tuple(arcs))
    generator.shuffle(result)
    return tuple(result)


def test_independent_schema_two_vectors_match_production_exactly() -> None:
    components = (
        (ReferenceArc("source", "edge", "target", b"payload"),),
        (ReferenceArc("left", "edge", "right", b"payload"),),
    )
    reference = canonicalize_document(ONTOLOGY_KEY, _flatten(components))

    assert hashlib.sha256(reference.component_graphs[0]).hexdigest() == (
        "1de4ac688ddd20beab2427e8c57314e8682571095ef746c01ac4d22ae1e9491d"
    )
    assert hashlib.sha256(reference.component_manifest).hexdigest() == (
        "84f1694e2ced904e5b64088da7e5dd63840c60c3057c35ce4d41aeb29665d156"
    )
    assert reference.document_scope.hex() == (
        "8d1c3f2c1068788506082f3c597048d2d934dfad530d6c864d708bb5b8c0c0a9"
    )
    assert tuple(binding.local_key.hex() for binding in reference.bindings) == (
        "b22b8089f9ef61ea37d482f355e714bae2c0cf3dd3e6738bfb70c7d3c69c8c73",
        "193b7a5768e93a443b8fca99d386985997d3a1dd0b83f84d76bd1725a4f5236b",
        "71d226d29151745d047295d1573b190764da7c9ae38af5678fae7e05c2855a3e",
        "bdac8436947fc21f5b4aa132835300787003bba6914114624f69578a80751373",
    )
    assert _production_document(components) == (
        reference.document_scope,
        reference.component_manifest,
        reference.component_graphs,
        tuple(binding.local_key for binding in reference.bindings),
    )


def test_independent_oracle_preserves_multiplicity_without_key_collapse() -> None:
    components = _fixture_components()
    reference = canonicalize_document(ONTOLOGY_KEY, _flatten(components))
    graph_counts = Counter(reference.component_graphs)
    keys = tuple(binding.local_key for binding in reference.bindings)

    assert sorted(graph_counts.values()) == [1, 1, 2]
    assert len(keys) == 8
    assert len(set(keys)) == len(keys)
    repeated_graph = next(graph for graph, count in graph_counts.items() if count == 2)
    repeated = [
        binding for binding in reference.bindings if binding.component_graph == repeated_graph
    ]
    assert {binding.occurrence_ordinal for binding in repeated} == {0, 1}
    assert _production_document(components) == (
        reference.document_scope,
        reference.component_manifest,
        reference.component_graphs,
        keys,
    )


def test_independent_oracle_keeps_true_duplicate_root_semantics() -> None:
    root = ReferenceArc("only", "class-assertion", payload=b"C")
    single = canonicalize_document(ONTOLOGY_KEY, (root,))
    duplicate = canonicalize_document(ONTOLOGY_KEY, (root, root))

    assert duplicate == single
    assert _production_document(((root, root),)) == (
        single.document_scope,
        single.component_manifest,
        single.component_graphs,
        tuple(binding.local_key for binding in single.bindings),
    )


def test_label_root_and_component_permutations_match_independent_oracle() -> None:
    baseline = canonicalize_document(
        ONTOLOGY_KEY,
        _flatten(_permuted_components(0)),
    )
    baseline_contract = (
        baseline.document_scope,
        baseline.component_manifest,
        baseline.component_graphs,
        tuple(binding.local_key for binding in baseline.bindings),
    )

    for seed in range(1, 64):
        components = _permuted_components(seed)
        candidate = canonicalize_document(ONTOLOGY_KEY, _flatten(components))
        candidate_contract = (
            candidate.document_scope,
            candidate.component_manifest,
            candidate.component_graphs,
            tuple(binding.local_key for binding in candidate.bindings),
        )
        assert candidate_contract == baseline_contract
        assert _production_document(components) == baseline_contract


def test_independent_and_production_oracles_are_hash_seed_invariant() -> None:
    script = """
import hashlib
from tests.reference.anonymous_v2 import canonicalize_document
from tests.unit.model.test_anonymous_components_v2 import (
    ONTOLOGY_KEY,
    _flatten,
    _production_document,
)
from tests.reference.anonymous_v2 import ReferenceArc

components = (
    {
        ReferenceArc("a0", "peer", "a1", b"same"),
        ReferenceArc("a1", "peer", "a0", b"same"),
    },
    {
        ReferenceArc("b0", "peer", "b1", b"same"),
        ReferenceArc("b1", "peer", "b0", b"same"),
    },
    {ReferenceArc("c0", "solo", payload=b"different")},
)
reference = canonicalize_document(ONTOLOGY_KEY, set(_flatten(components)))
production = _production_document(components)
payload = (
    reference.document_scope
    + reference.component_manifest
    + b"".join(reference.component_graphs)
    + b"".join(binding.local_key for binding in reference.bindings)
    + b"".join(production[0:2])
    + b"".join(production[2])
    + b"".join(production[3])
)
print(hashlib.sha256(payload).hexdigest())
"""
    outputs: set[str] = set()
    for seed in ("0", "1", "8675309"):
        environment = dict(os.environ)
        environment.update(
            PYTHONHASHSEED=seed,
            PYTHONPATH=str(ROOT / "src"),
            PYTHONDONTWRITEBYTECODE="1",
        )
        outputs.add(
            subprocess.check_output(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=environment,
                text=True,
            ).strip()
        )

    assert outputs == {
        "fc637d1abf9daaa34fdae45b4921719f844367cb73d29a99b969630093ed135e"
    }
