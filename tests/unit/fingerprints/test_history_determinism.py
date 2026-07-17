from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from pathlib import Path

from pyowl_core import (
    IRI,
    BackendPreference,
    Class,
    Declaration,
    ImportPolicy,
    LoadOptions,
    OntologyDelta,
    OntologyOverlay,
    apply_delta,
    load_snapshot,
)


def _declaration(index: int) -> Declaration:
    return Declaration(Class(IRI(f"urn:test#C{index}")))


def _base():  # type: ignore[no-untyped-def]
    return load_snapshot(
        b"Prefix(:=<urn:test#>) Ontology(<urn:root> Declaration(Class(:C0)))",
        options=LoadOptions(
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
        ),
    )


def test_randomized_edit_histories_equal_materialized_full_fingerprints() -> None:
    rng = random.Random(404)
    base = _base()
    current = base
    present = {0}
    for _ in range(120):
        index = rng.randrange(30)
        if index in present:
            delta = OntologyDelta(remove_axioms={_declaration(index)})
            present.remove(index)
        else:
            delta = OntologyDelta(add_axioms={_declaration(index)})
            present.add(index)
        current = apply_delta(current, delta)
        if current.depth == 12:
            current = current.compact()
    assert isinstance(current, OntologyOverlay)
    materialized = current.materialize()
    assert tuple(current.iter_axioms()) == tuple(materialized.iter_axioms())
    assert current.structural_fingerprint == materialized.structural_fingerprint
    assert current.logical_fingerprint == materialized.logical_fingerprint
    assert current.signature_fingerprint == materialized.signature_fingerprint


def test_overlay_and_composite_fingerprints_ignore_python_hash_seed() -> None:
    root = Path(__file__).resolve().parents[3]
    script = r"""
import json
from pyowl_core import *
options = LoadOptions(imports=ImportPolicy.IGNORE, backend=BackendPreference.PYTHON)
first = load_snapshot(
    b"Prefix(:=<urn:t#>) Ontology(<urn:a> Declaration(Class(:A)))",
    options=options,
)
second = load_snapshot(
    b"Prefix(:=<urn:t#>) Ontology(<urn:b> Declaration(Class(:B)))",
    options=options,
)
bridge = Declaration(Class(IRI("urn:t#Bridge")))
overlay = apply_delta(first, OntologyDelta(add_axioms=frozenset({bridge})))
composite = compose_views(overlay, second, roles=("source", "target"))
print(json.dumps({
    "overlay": [
        overlay.structural_fingerprint.hex,
        overlay.logical_fingerprint.hex,
        overlay.signature_fingerprint.hex,
    ],
    "composite": [
        composite.structural_fingerprint.hex,
        composite.logical_fingerprint.hex,
        composite.signature_fingerprint.hex,
    ],
}, sort_keys=True))
"""
    outputs = []
    for seed in ("1", "7", "99991"):
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = seed
        environment["PYTHONPATH"] = str(root / "src")
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(json.loads(completed.stdout))
    assert outputs[0] == outputs[1] == outputs[2]
