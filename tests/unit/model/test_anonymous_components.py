from __future__ import annotations

import os
import random
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from unittest.mock import patch

import pytest

import pyowl_core.model as m
from pyowl_core import (
    BackendPreference,
    LoadOptions,
    ParseLimits,
    ResourceLimitError,
    parse_document,
)
from pyowl_core.document import OntologyID
from pyowl_core.document.document import (
    _blank_component_summary,
    _canonicalize_blank_components,
    _partition_blank_graph,
    freeze_document_anonymous,
    provisional_anonymous,
)
from pyowl_core.model.anonymous import (
    _bind_component_blank_nodes,
    _canonical_component_manifest,
)

ONTOLOGY_ID = OntologyID(m.IRI("urn:test:anonymous-components"))
CLASS_C = m.Class(m.IRI("urn:test:C"))
CLASS_D = m.Class(m.IRI("urn:test:D"))
PROPERTY_P = m.ObjectProperty(m.IRI("urn:test:p"))
ROOT = Path(__file__).resolve().parents[3]


def _blank(label: str) -> m.AnonymousIndividual:
    return provisional_anonymous(label)


def _freeze(
    roots: Iterable[m.AxiomNode],
    *,
    limits: ParseLimits | None = None,
) -> m.CanonicalSet[m.AxiomNode]:
    _, _, axioms, _ = freeze_document_anonymous(
        ONTOLOGY_ID,
        (),
        (),
        roots,
        (),
        limits=limits,
    )
    return axioms


def _anonymous_values(axioms: m.CanonicalSet[m.AxiomNode]) -> set[m.AnonymousIndividual]:
    values: set[m.AnonymousIndividual] = set()
    for axiom in axioms:
        if isinstance(axiom, m.ClassAssertion) and isinstance(
            axiom.individual, m.AnonymousIndividual
        ):
            values.add(axiom.individual)
        elif isinstance(axiom, m.ObjectPropertyAssertion):
            if isinstance(axiom.source, m.AnonymousIndividual):
                values.add(axiom.source)
            if isinstance(axiom.target, m.AnonymousIndividual):
                values.add(axiom.target)
        elif isinstance(axiom, m.SameIndividual):
            values.update(
                value for value in axiom.individuals if isinstance(value, m.AnonymousIndividual)
            )
    return values


def test_component_manifest_sorts_complete_graphs_and_retains_multiplicity() -> None:
    manifest = _canonical_component_manifest((b"beta", b"alpha", b"alpha"))

    assert manifest == (b"pyowl-core:blank-component-manifest:v2\x00\x02\x05alpha\x02\x04beta\x01")


def test_schema_two_component_scope_and_key_framing_has_frozen_vectors() -> None:
    canonicalization = m.alpha_canonicalize_blank_nodes(
        (m.BlankNodeArc("source", "edge", "target", b"payload"),),
        b"P" * 32,
    )
    manifest = _canonical_component_manifest(
        (canonicalization.canonical_graph, canonicalization.canonical_graph)
    )
    scope = m.canonical_document_scope(b"ontology-key", canonical_graph=manifest)

    assert scope.hex() == "8d1c3f2c1068788506082f3c597048d2d934dfad530d6c864d708bb5b8c0c0a9"
    expected = (
        (
            "b22b8089f9ef61ea37d482f355e714bae2c0cf3dd3e6738bfb70c7d3c69c8c73",
            "193b7a5768e93a443b8fca99d386985997d3a1dd0b83f84d76bd1725a4f5236b",
        ),
        (
            "71d226d29151745d047295d1573b190764da7c9ae38af5678fae7e05c2855a3e",
            "bdac8436947fc21f5b4aa132835300787003bba6914114624f69578a80751373",
        ),
    )
    for ordinal, keys in enumerate(expected):
        rebound = _bind_component_blank_nodes(
            canonicalization,
            scope,
            occurrence_ordinal=ordinal,
        )
        assert tuple(binding.individual.local_key.hex() for binding in rebound.bindings) == keys


def test_repeated_isomorphic_components_remain_distinct_and_label_invariant() -> None:
    first = _freeze(
        [
            m.ClassAssertion(CLASS_C, _blank("left")),
            m.ClassAssertion(CLASS_C, _blank("right")),
        ],
        limits=ParseLimits(max_canonical_work=9),
    )
    renamed_and_reversed = _freeze(
        [
            m.ClassAssertion(CLASS_C, _blank("zulu")),
            m.ClassAssertion(CLASS_C, _blank("alpha")),
        ][::-1],
        limits=ParseLimits(max_canonical_work=9),
    )

    assert first == renamed_and_reversed
    assert len(first) == 2
    individuals = _anonymous_values(first)
    assert len(individuals) == 2
    assert len({value.local_key for value in individuals}) == 2
    assert len({value.document_scope for value in individuals}) == 1


def test_component_solver_reuses_phase_one_order_and_scales_at_fixed_size() -> None:
    roots = [m.ClassAssertion(CLASS_C, _blank(f"node-{index}")) for index in range(64)]

    with patch(
        "pyowl_core.document.document.alpha_canonicalize_blank_nodes",
        wraps=m.alpha_canonicalize_blank_nodes,
    ) as canonicalize:
        result = _freeze(roots, limits=ParseLimits(max_canonical_work=9))

    assert len(result) == 64
    assert canonicalize.call_count == 64


def test_document_global_term_limit_is_not_reset_per_component() -> None:
    roots = [
        m.ClassAssertion(CLASS_C, _blank("left")),
        m.ClassAssertion(CLASS_C, _blank("right")),
    ]

    with pytest.raises(ResourceLimitError) as captured:
        _freeze(roots, limits=ParseLimits(max_terms=3))

    assert captured.value.limit == "max_terms"
    assert captured.value.observed == 4
    assert captured.value.allowed == 3


def test_one_oversized_component_reports_complete_structural_details() -> None:
    root = m.SameIndividual(m.CanonicalSet(_blank(label) for label in "abcd"))

    with pytest.raises(ResourceLimitError) as captured:
        _freeze([root], limits=ParseLimits(max_canonical_work=23))

    error = captured.value
    assert (error.limit, error.observed, error.allowed) == ("max_canonical_work", 24, 23)
    assert dict(error.details) == {
        "component_count": 1,
        "largest_component_arcs": 10,
        "largest_component_labels": 4,
        "refinement_rounds": 0,
        "work_term": "setup",
    }


def test_partition_summary_measures_interleaved_root_activity() -> None:
    roots: tuple[m.StructuralNode, ...] = (
        m.ClassAssertion(CLASS_C, _blank("shared")),
        m.ClassAssertion(CLASS_C, _blank("independent")),
        m.ClassAssertion(CLASS_D, _blank("shared")),
    )

    summary = _blank_component_summary(roots)

    assert summary.component_count == 2
    assert summary.largest_component_labels == 1
    assert summary.largest_component_arcs == 2
    assert summary.largest_component_roots == 2
    assert summary.maximum_root_interval_span == 3
    assert summary.maximum_open_root_intervals == 2
    assert summary.total_labels == 2
    assert summary.total_arcs == 3


def test_late_connecting_root_merges_prior_component_payloads() -> None:
    left = _blank("left")
    right = _blank("right")
    roots: tuple[m.StructuralNode, ...] = (
        m.ClassAssertion(CLASS_C, left),
        m.ClassAssertion(CLASS_D, right),
        m.SameIndividual(m.CanonicalSet((left, right))),
    )

    components, summary = _partition_blank_graph(roots)

    assert len(components) == 1
    assert components[0].labels == ("left", "right")
    assert components[0].root_indexes == (0, 1, 2)
    assert len(components[0].arcs) == 5
    assert summary.component_count == 1
    assert summary.largest_component_roots == 3


def test_success_telemetry_exposes_component_and_aggregate_phase_work() -> None:
    roots: tuple[m.StructuralNode, ...] = (
        m.ClassAssertion(CLASS_C, _blank("left")),
        m.ClassAssertion(CLASS_C, _blank("right")),
    )
    components, partition = _partition_blank_graph(roots)

    _, summary = _canonicalize_blank_components(
        components,
        partition,
        limits=ParseLimits(max_canonical_work=9),
    )

    assert len(summary.components) == 2
    assert summary.total_setup_work == 6
    assert summary.total_refinement_work == 8
    assert summary.total_candidate_order_work == 4
    assert summary.total_canonical_work == 18
    assert summary.largest_component_work == 9
    assert summary.maximum_refinement_rounds == 1
    assert summary.total_permutations_examined == 2
    for component in summary.components:
        assert (component.label_count, component.arc_count, component.root_count) == (1, 1, 1)
        assert (component.setup_work, component.refinement_work) == (3, 4)
        assert component.candidate_order_work == 2
        assert component.canonical_work == 9


def test_duplicate_roots_keep_normal_canonical_set_semantics() -> None:
    root = m.ClassAssertion(CLASS_C, _blank("same"))

    assert _freeze([root, root]) == _freeze([root])


def test_random_label_root_and_component_permutations_preserve_bytes() -> None:
    def build(seed: int) -> m.CanonicalSet[m.AxiomNode]:
        generator = random.Random(seed)
        source_labels = [f"seed-{seed}-blank-{index}" for index in range(5)]
        generator.shuffle(source_labels)
        roots: list[m.AxiomNode] = [
            m.ObjectPropertyAssertion(
                PROPERTY_P,
                _blank(source_labels[0]),
                _blank(source_labels[1]),
            ),
            m.ObjectPropertyAssertion(
                PROPERTY_P,
                _blank(source_labels[2]),
                _blank(source_labels[3]),
            ),
            m.ClassAssertion(CLASS_C, _blank(source_labels[4])),
        ]
        generator.shuffle(roots)
        return _freeze(roots)

    baseline = build(0)
    assert len(baseline) == 3
    assert len(_anonymous_values(baseline)) == 5
    for seed in range(1, 32):
        candidate = build(seed)
        assert candidate == baseline
        assert tuple(m.canonical_bytes(item) for item in candidate) == tuple(
            m.canonical_bytes(item) for item in baseline
        )


def test_symmetric_component_permutations_preserve_distinct_occurrence_slots() -> None:
    def build(labels: tuple[str, str, str, str], reverse: bool) -> m.CanonicalSet[m.AxiomNode]:
        roots: list[m.AxiomNode] = [
            m.SameIndividual(m.CanonicalSet((_blank(labels[0]), _blank(labels[1])))),
            m.SameIndividual(m.CanonicalSet((_blank(labels[2]), _blank(labels[3])))),
        ]
        if reverse:
            roots.reverse()
        return _freeze(roots)

    first = build(("a", "b", "c", "d"), False)
    renamed = build(("z", "w", "y", "x"), True)

    assert first == renamed
    assert len(first) == 2
    assert len(_anonymous_values(first)) == 4


def test_repeated_multi_root_components_are_complete_and_order_invariant() -> None:
    first = _freeze(
        [
            m.ClassAssertion(CLASS_C, _blank("a")),
            m.ClassAssertion(CLASS_C, _blank("c")),
            m.ObjectPropertyAssertion(PROPERTY_P, _blank("a"), _blank("b")),
            m.ObjectPropertyAssertion(PROPERTY_P, _blank("c"), _blank("d")),
        ]
    )
    permuted = _freeze(
        [
            m.ObjectPropertyAssertion(PROPERTY_P, _blank("w"), _blank("x")),
            m.ClassAssertion(CLASS_C, _blank("y")),
            m.ObjectPropertyAssertion(PROPERTY_P, _blank("y"), _blank("z")),
            m.ClassAssertion(CLASS_C, _blank("w")),
        ]
    )

    assert first == permuted
    assert len(first) == 4
    assert len(_anonymous_values(first)) == 4


def test_document_fingerprint_is_invariant_to_component_order_and_labels() -> None:
    first = b"""\
Ontology(<urn:test:anonymous-components>
 Declaration(Class(<urn:test:C>))
 Declaration(ObjectProperty(<urn:test:p>))
 ObjectPropertyAssertion(<urn:test:p> _:a _:b)
 ObjectPropertyAssertion(<urn:test:p> _:c _:d)
 ClassAssertion(<urn:test:C> _:e)
)
"""
    permuted = b"""\
Ontology(<urn:test:anonymous-components>
 ClassAssertion(<urn:test:C> _:middle)
 ObjectPropertyAssertion(<urn:test:p> _:zulu _:yankee)
 Declaration(ObjectProperty(<urn:test:p>))
 ObjectPropertyAssertion(<urn:test:p> _:bravo _:alpha)
 Declaration(Class(<urn:test:C>))
)
"""
    options = LoadOptions(backend=BackendPreference.PYTHON)

    first_document = parse_document(first, format="functional", options=options)
    permuted_document = parse_document(permuted, format="functional", options=options)

    assert first_document == permuted_document
    assert first_document.document_fingerprint == permuted_document.document_fingerprint


def test_component_canonicalization_is_python_hash_seed_independent() -> None:
    script = """
from pyowl_core import BackendPreference, LoadOptions, parse_document
source = b'''Ontology(<urn:test:anonymous-components>
 Declaration(Class(<urn:test:C>))
 Declaration(ObjectProperty(<urn:test:p>))
 ObjectPropertyAssertion(<urn:test:p> _:a _:b)
 ObjectPropertyAssertion(<urn:test:p> _:c _:d)
 ClassAssertion(<urn:test:C> _:e))'''
document = parse_document(
    source,
    format="functional",
    options=LoadOptions(backend=BackendPreference.PYTHON),
)
print(document.document_fingerprint.hex)
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

    assert len(outputs) == 1
