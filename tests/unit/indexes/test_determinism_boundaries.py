from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_index_results_are_hash_seed_deterministic() -> None:
    script = r'''
import json
from pyowl_core import (
    AxiomTypeIndex, BackendPreference, EntityReferenceIndex, ImportPolicy,
    LoadOptions, SignatureView, canonical_bytes, load_snapshot,
)
source = b"""Prefix(:=<urn:seed#>) Ontology(<urn:seed>
Declaration(Class(:B)) Declaration(Class(:A))
SubClassOf(:A ObjectSomeValuesFrom(:p :B))
)"""
view = load_snapshot(
    source,
    options=LoadOptions(imports=ImportPolicy.IGNORE, backend=BackendPreference.PYTHON),
)
references = view.view(EntityReferenceIndex)
payload = {
    "signature": [canonical_bytes(value).hex() for value in view.view(SignatureView).iter()],
    "axioms": [canonical_bytes(value).hex() for value in view.view(AxiomTypeIndex).iter_all()],
    "references": [
        [
            canonical_bytes(key).hex(),
            [
                [
                    canonical_bytes(item.container).hex(),
                    [
                        [
                            step.field_id.constructor_tag,
                            step.field_id.field_ordinal,
                            step.item_index,
                        ]
                        for step in item.constructor_path
                    ],
                    item.role.value,
                ]
                for item in references.iter(key)
            ],
        ]
        for key in references
    ],
}
print(json.dumps(payload, sort_keys=True))
'''
    outputs: list[str] = []
    for seed in ("1", "987654"):
        environment = dict(os.environ)
        environment.update(
            PYTHONPATH=str(ROOT / "src"),
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONHASHSEED=seed,
        )
        outputs.append(
            subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
    assert outputs[0] == outputs[1]


def test_core_indexes_do_not_import_consumer_private_ir() -> None:
    forbidden = ("pyelk", "pyhermit", "owl2vec", "exact_om", "deeponto", "jpype")
    for path in sorted((ROOT / "src" / "pyowl_core" / "index").glob("*.py")):
        source = path.read_text(encoding="utf-8").lower()
        assert not any(value in source for value in forbidden), path
