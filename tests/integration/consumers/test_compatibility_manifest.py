from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import pyowl_core
from pyowl_core.adapters import AdapterRequirement, negotiate_view

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "reports" / "integration" / "consumer-compatibility.json"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
CORE_IMPLEMENTATION_COMMIT = "af9bdb0b9178766b5f15806fb6a2f00b05e00e22"
CORE_RELEASE_EVIDENCE_COMMIT = "15992ca5b19f795da7870ec183727100758b08d9"
CONSUMER_COMMITS = {
    "exact-om": "d172cfa355a5d2683fc47824a5d8f2ed24cf9125",
    "oaei-bioml-eval": "04573c09dd0e62825c3fa7c5b2490b43d5a22874",
    "pyelk": "a909cfcea341834ab6d6598f80445a697b338f13",
    "pyhermit": "04bd8163b532f623044d7391706ff728d1aed4b1",
    "projector": "53a23e2d385696e2be042568ade0d178580c6de4",
}
CONSUMER_TESTS = {
    "exact-om": {
        "selection": (
            "tests/ontology_stack_provenance_test.py "
            "tests/reasoner_adapters_test.py "
            "tests/shared_owl_stack_test.py "
            "tests/owl_stack_scale_test.py "
            "tests/owl_public_boundary_test.py"
        ),
        "result": "82 passed, 1 expected NativeBackendFallbackWarning",
    },
    "oaei-bioml-eval": {
        "selection": (
            "tests/test_coherence.py tests/test_native_reasoners.py tests/test_java_free_runtime.py"
        ),
        "result": "95 tests, OK",
    },
    "pyelk": {
        "selection": (
            "tests/unit/core/test_core_contract.py tests/unit/indexing/test_literals.py "
            "tests/integration/test_shared_snapshot_input.py "
            "tests/unit/inputs/test_policy_cache.py"
        ),
        "result": "32 passed",
    },
    "pyhermit": {
        "selection": (
            "tests/unit/core/test_core_contract.py "
            "tests/integration/shared_snapshot/test_inputs.py "
            "tests/unit/datatypes/test_language_tags.py "
            "tests/unit/datatypes/test_literal_identities.py"
        ),
        "result": "85 passed",
    },
    "projector": {
        "selection": "tests/test_consumer_conformance.py",
        "result": "13 passed",
    },
}


def _snapshot(iri: str) -> pyowl_core.OntologySnapshot:
    return pyowl_core.load_snapshot(
        f"Ontology(<{iri}> Declaration(Class(<{iri}#A>)))".encode(),
        options=pyowl_core.LoadOptions(backend=pyowl_core.BackendPreference.PYTHON),
    )


def test_recorded_consumer_contracts_negotiate_with_the_implementation_checkpoint() -> None:
    payload = cast(dict[str, Any], json.loads(MANIFEST.read_text(encoding="utf-8")))
    assert payload["schema"] == "pyowl-core.consumer-compatibility/2"
    assert payload["recorded_date"] == "2026-07-26"
    core = cast(dict[str, Any], payload["core"])
    assert core["package_version"] == pyowl_core.__version__
    assert tuple(core["api_version"]) == pyowl_core.API_VERSION
    assert core["model_schema"] == pyowl_core.MODEL_SCHEMA_VERSION
    assert tuple(core["wire_format"]) == pyowl_core.WIRE_FORMAT_VERSION
    assert core["adapter_protocol"] == pyowl_core.ADAPTER_PROTOCOL_VERSION
    assert core["implementation_commit"] == CORE_IMPLEMENTATION_COMMIT
    assert HEX40.fullmatch(core["adapter_contract_commit"])
    assert core["release_evidence"] == {
        "commit": CORE_RELEASE_EVIDENCE_COMMIT,
        "implementation_commit": CORE_IMPLEMENTATION_COMMIT,
        "classification": "behavior-preserving-release-evidence-only",
        "runtime_source_changed": False,
        "changed_paths": [
            ".github/workflows/ci.yml",
            "benchmarks/tests/test_harness_acceptance.py",
            "reports/release/0.1.0.dev0/build-provenance.json",
            "tests/packaging/test_supply_chain.py",
            "tests/packaging/test_workflows.py",
            "tools/benchmark/harness.py",
            "tools/packaging/supply_chain.py",
        ],
    }

    snapshot = _snapshot("urn:manifest:one")
    second = _snapshot("urn:manifest:two")
    composite = pyowl_core.compose_views(snapshot, second, roles=("source", "target"))
    consumers = cast(list[dict[str, Any]], payload["consumers"])
    assert [item["id"] for item in consumers] == list(CONSUMER_COMMITS)
    for item in consumers:
        assert item["commit"] == CONSUMER_COMMITS[item["id"]]
        assert item["core_requirement"] == ">=0.1,<0.2"
        test = cast(dict[str, Any], item["test"])
        assert set(test) == {
            "core_implementation_commit",
            "result",
            "selection",
            "status",
            "tested_commit_tree",
        }
        assert test["core_implementation_commit"] == CORE_IMPLEMENTATION_COMMIT
        assert test["status"] == "pass"
        assert test["tested_commit_tree"] is True
        assert test["selection"] == CONSUMER_TESTS[item["id"]]["selection"]
        assert test["result"] == CONSUMER_TESTS[item["id"]]["result"]
        requirement = AdapterRequirement(
            consumer=item["package"],
            consumer_version=item["package_version"],
            consumer_api=item["consumer_api"],
            required_features=frozenset(item["required_features"]),
        )
        view = composite if item["view_kind"] == "composite" else snapshot
        report = negotiate_view(view, requirement)
        assert report.compatible, (item["id"], report.to_dict())


def test_compatibility_evidence_is_path_free_and_records_no_runtime_coupling() -> None:
    text = MANIFEST.read_text(encoding="utf-8")
    payload = cast(dict[str, Any], json.loads(text))
    assert "/Users/" not in text
    assert "private/tmp" not in text
    audits = cast(dict[str, Any], payload["static_audits"])
    assert audits["consumer_runtime_private_core_imports"] == 0
    assert audits["consumer_runtime_java_imports"] == 0
    assert audits["consumer_runtime_pickle_handoffs"] == 0
    assert audits["core_runtime_consumer_imports"] == 0
