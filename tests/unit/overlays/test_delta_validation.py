from __future__ import annotations

from dataclasses import replace

import pytest

from pyowl_core import (
    CanonicalSet,
    DeltaBaseMismatchError,
    DeltaError,
    DeltaPolicy,
    OntologyDelta,
    apply_delta,
)

from .conftest import annotation, declaration, snapshot


def test_delta_is_canonical_immutable_and_rejects_conflicts() -> None:
    first = declaration("A")
    second = declaration("B")
    metadata = {"agent": "repair"}
    delta = OntologyDelta(add_axioms={second, first}, metadata=metadata)
    metadata["agent"] = "mutated"

    assert isinstance(delta.add_axioms, CanonicalSet)
    assert tuple(delta.add_axioms) == tuple(CanonicalSet((first, second)))
    assert delta.metadata == {"agent": "repair"}
    assert delta.entry_count == 2
    assert not delta.is_empty
    assert len(delta.provenance_digest) == 32

    with pytest.raises(DeltaError) as conflict:
        OntologyDelta(add_axioms={first}, remove_axioms={first})
    assert conflict.value.code == "DELTA_AXIOM_CONFLICT"

    note = annotation("note")
    with pytest.raises(DeltaError) as annotation_conflict:
        OntologyDelta(
            add_ontology_annotations={note},
            remove_ontology_annotations={note},
        )
    assert annotation_conflict.value.code == "DELTA_ANNOTATION_CONFLICT"


def test_metadata_is_bounded_and_excluded_from_semantic_delta_bytes() -> None:
    axiom = declaration("A")
    first = OntologyDelta(add_axioms={axiom}, metadata={"run": "one"})
    second = OntologyDelta(add_axioms={axiom}, metadata={"run": "two"})

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.provenance_digest != second.provenance_digest

    with pytest.raises(DeltaError) as too_many:
        OntologyDelta(metadata={f"k{i}": "v" for i in range(65)})
    assert too_many.value.code == "DELTA_METADATA_LIMIT"
    with pytest.raises(DeltaError) as too_large:
        OntologyDelta(metadata={"k": "v" * 4097})
    assert too_large.value.code == "DELTA_METADATA_LIMIT"
    with pytest.raises(DeltaError) as invalid_text:
        OntologyDelta(metadata={"k": "\ud800"})
    assert invalid_text.value.code == "DELTA_METADATA_ENCODING"


def test_strict_and_idempotent_application_have_stable_outcomes() -> None:
    base = snapshot("A")
    existing = declaration("A")
    absent = declaration("B")

    with pytest.raises(DeltaError) as duplicate:
        apply_delta(base, OntologyDelta(add_axioms={existing}))
    assert duplicate.value.code == "DELTA_ADD_EXISTS"
    with pytest.raises(DeltaError) as missing:
        apply_delta(base, OntologyDelta(remove_axioms={absent}))
    assert missing.value.code == "DELTA_REMOVE_ABSENT"

    replay = apply_delta(
        base,
        OntologyDelta(
            add_axioms={existing},
            remove_axioms={absent},
            policy=DeltaPolicy.IDEMPOTENT,
        ),
    )
    assert replay.delta.is_empty
    assert replay.no_op_add_axioms == CanonicalSet((existing,))
    assert replay.no_op_remove_axioms == CanonicalSet((absent,))
    assert {item.code for item in replay.report.diagnostics} >= {
        "DELTA_IDEMPOTENT_ADD_NOOP",
        "DELTA_IDEMPOTENT_REMOVE_NOOP",
    }


def test_expected_base_mismatch_precedes_membership_work(monkeypatch: pytest.MonkeyPatch) -> None:
    base = snapshot("A")
    wrong = replace(base.structural_fingerprint, digest=b"x" * 32)
    calls = 0
    original = type(base).contains

    def counted(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(base), "contains", counted)
    with pytest.raises(DeltaBaseMismatchError) as mismatch:
        apply_delta(
            base,
            OntologyDelta(
                add_axioms={declaration("B")},
                expected_base_fingerprint=wrong,
            ),
        )
    assert mismatch.value.code == "DELTA_BASE_MISMATCH"
    assert calls == 0


def test_annotation_edits_follow_strict_and_idempotent_rules() -> None:
    base = snapshot("A")
    note = annotation("note")
    added = apply_delta(base, OntologyDelta(add_ontology_annotations={note}))
    assert note in added.ontology_annotations()
    assert added.structural_fingerprint != base.structural_fingerprint
    assert added.logical_fingerprint == base.logical_fingerprint
    materialized = added.materialize()
    assert materialized.structural_fingerprint == added.structural_fingerprint
    assert materialized.origin_index.origins_for(note) == added.origins_for(note)

    with pytest.raises(DeltaError) as duplicate:
        apply_delta(added, OntologyDelta(add_ontology_annotations={note}))
    assert duplicate.value.code == "DELTA_ANNOTATION_ADD_EXISTS"

    removed = apply_delta(added, OntologyDelta(remove_ontology_annotations={note}))
    assert note not in removed.ontology_annotations()
    assert tuple(removed.iter_axioms()) == tuple(base.iter_axioms())
    assert removed.structural_fingerprint == base.structural_fingerprint
    assert removed.logical_fingerprint == base.logical_fingerprint
    assert removed.signature_fingerprint == base.signature_fingerprint
