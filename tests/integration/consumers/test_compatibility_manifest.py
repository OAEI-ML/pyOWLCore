from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import pyowl_core
from pyowl_core.adapters import AdapterRequirement, negotiate_capabilities, negotiate_view

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "reports" / "integration" / "consumer-compatibility.json"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
CORE_FINAL_COMMIT = "4fe32971780e38d2d83932bb93b8c2195bdfcc5f"
CORE_RUNTIME_COMMIT = "005c3ccad129757b3a9be125dc064b812b607ef5"
CORE_RUNTIME_TREE = "d4f3f29f6594b59f3d45a4811c38fb761a7028b9"
CORE_DIRECT_SAFETY_COMMIT = "a81665241ae86036a3fbe0325f7bcf43660f3a12"
CORE_PERFORMANCE_EVIDENCE_COMMIT = "4fe32971780e38d2d83932bb93b8c2195bdfcc5f"
CORE_ADAPTER_CONTRACT_COMMIT = "75132daaf8f665b6f72dbbd7c9fcf30ef23e1eb7"
STRUCTURAL_SCHEMA_DESCRIPTOR = "9ad29db6a7e616f65cea2957bc5ba8d1f9b99ef0eb1fe1432c09be25786267b5"
STRUCTURAL_SCHEMA_REQUIREMENT = {"pyowl-core/structural-columns": 1}
CONSUMER_IDENTITIES = {
    "exact-om": {
        "final_commit": "74b48779f1a3ca3e85614d50186ecf40a7f6db65",
        "runtime_commit": "ab4b76644f6ed58894d0920e47de713ba1ffb358",
        "role": "compatibility-consumer",
    },
    "oaei-bioml-eval": {
        "final_commit": "94713d5068ce78d90f42e7fb100c7631b6490924",
        "runtime_commit": "94713d5068ce78d90f42e7fb100c7631b6490924",
        "role": "compatibility-consumer",
    },
    "pyelk": {
        "final_commit": "70302fcd6abc27d703eeb8f59027fc1392f4709b",
        "runtime_commit": "bc75f4be609626f231cdc91af800f52bae46c766",
        "role": "encoded-native-compiler",
    },
    "pyhermit": {
        "final_commit": "af8f7fc669b28dfc15728c84c78f9094787d288b",
        "runtime_commit": "f0d4ebb270f3521b848cd2a858761afd66e72ae2",
        "role": "encoded-native-compiler",
    },
    "projector": {
        "final_commit": "9f19db3de54b7bdffe45498479edadd72af37218",
        "runtime_commit": "46b066f698cc790aceae4f8eaf50212934e94708",
        "role": "encoded-native-compiler",
    },
}
CONSUMER_TESTS = {
    "exact-om": {
        "selection": (
            "tests/ontology_backend_test.py "
            "tests/ontology_parity_test.py "
            "tests/ontology_stack_provenance_test.py "
            "tests/reasoner_adapters_test.py "
            "tests/shared_owl_stack_test.py "
            "tests/owl_stack_scale_test.py "
            "tests/owl_public_boundary_test.py "
            "tests/ontology_compatibility_test.py "
            "tests/ontology_public_handoff_test.py"
        ),
        "result": "107 passed, 1 expected NativeBackendFallbackWarning",
    },
    "oaei-bioml-eval": {
        "selection": "python -m unittest discover -s tests; strict installed owner matrix",
        "result": (
            "Ran 238 tests, OK (skipped=13); 4 formats / 20 owners / 40 reasoner runs, "
            "semantic identity true"
        ),
    },
    "pyelk": {
        "selection": (
            "tests/packaging/test_supply_chain.py "
            "tests/integration/test_shared_snapshot_input.py "
            "tests/unit/core/test_core_contract.py "
            "tests/unit/reasoning/test_contracts.py"
        ),
        "result": "63 passed",
    },
    "pyhermit": {
        "selection": (
            "final-core Python/core/encoded/public parity; release/workflow and fail-closed "
            "contracts; focused Rust parity"
        ),
        "result": (
            "53 passed; 20 release/workflow plus 3 fail-closed passed; focused Rust parity passed"
        ),
    },
    "projector": {
        "selection": "tests/test_release_tooling.py tests/test_consumer_conformance.py",
        "result": "50 passed",
    },
}


def _snapshot(iri: str) -> pyowl_core.OntologySnapshot:
    return pyowl_core.load_snapshot(
        f"Ontology(<{iri}> Declaration(Class(<{iri}#A>)))".encode(),
        options=pyowl_core.LoadOptions(backend=pyowl_core.BackendPreference.PYTHON),
    )


def _requirement(item: dict[str, Any]) -> AdapterRequirement:
    return AdapterRequirement(
        consumer=item["package"],
        consumer_version=item["package_version"],
        consumer_api=item["consumer_api"],
        required_features=frozenset(item["required_features"]),
        required_encoded_view_schemas=item["required_encoded_view_schemas"],
    )


def test_recorded_consumer_contracts_negotiate_with_the_implementation_checkpoint() -> None:
    payload = cast(dict[str, Any], json.loads(MANIFEST.read_text(encoding="utf-8")))
    assert payload["schema"] == "pyowl-core.consumer-compatibility/4"
    assert payload["recorded_date"] == "2026-07-29"
    core = cast(dict[str, Any], payload["core"])
    # This is exact historical evidence for the 0.1.0 runtime. Patch releases
    # remain compatible through the recorded >=0.1,<0.2 consumer constraints.
    assert core["package_version"] == "0.1.0"
    assert pyowl_core.__version__ == "0.1.1"
    assert tuple(core["api_version"]) == pyowl_core.API_VERSION
    assert core["model_schema"] == pyowl_core.MODEL_SCHEMA_VERSION
    assert tuple(core["wire_format"]) == pyowl_core.WIRE_FORMAT_VERSION
    assert core["adapter_protocol"] == pyowl_core.ADAPTER_PROTOCOL_VERSION
    assert core["final_commit"] == CORE_FINAL_COMMIT
    assert core["runtime_commit"] == CORE_RUNTIME_COMMIT
    assert core["runtime_tree"] == CORE_RUNTIME_TREE
    assert core["direct_safety_commit"] == CORE_DIRECT_SAFETY_COMMIT
    assert core["performance_evidence_commit"] == CORE_PERFORMANCE_EVIDENCE_COMMIT
    assert core["adapter_contract_commit"] == CORE_ADAPTER_CONTRACT_COMMIT
    assert core["encoded_view_descriptor_sha256"] == STRUCTURAL_SCHEMA_DESCRIPTOR
    assert core["encoded_view_schemas"] == STRUCTURAL_SCHEMA_REQUIREMENT
    assert HEX40.fullmatch(core["runtime_tree"])
    assert HEX64.fullmatch(core["encoded_view_descriptor_sha256"])
    for field in (
        "final_commit",
        "runtime_commit",
        "direct_safety_commit",
        "performance_evidence_commit",
        "adapter_contract_commit",
    ):
        assert HEX40.fullmatch(core[field])

    snapshot = _snapshot("urn:manifest:one")
    second = _snapshot("urn:manifest:two")
    composite = pyowl_core.compose_views(snapshot, second, roles=("source", "target"))
    consumers = cast(list[dict[str, Any]], payload["consumers"])
    assert [item["id"] for item in consumers] == list(CONSUMER_IDENTITIES)
    for item in consumers:
        identity = CONSUMER_IDENTITIES[item["id"]]
        assert item["final_commit"] == identity["final_commit"]
        assert item["runtime_commit"] == identity["runtime_commit"]
        assert item["role"] == identity["role"]
        assert HEX40.fullmatch(item["final_commit"])
        assert HEX40.fullmatch(item["runtime_commit"])
        assert item["core_requirement"] == ">=0.1,<0.2"
        assert item["required_encoded_view_schemas"] == STRUCTURAL_SCHEMA_REQUIREMENT
        test = cast(dict[str, Any], item["test"])
        assert set(test) == {
            "consumer_final_commit",
            "consumer_runtime_commit",
            "core_runtime_commit",
            "result",
            "selection",
            "status",
            "tested_commit_tree",
        }
        assert test["core_runtime_commit"] == CORE_RUNTIME_COMMIT
        assert test["consumer_final_commit"] == item["final_commit"]
        assert test["consumer_runtime_commit"] == item["runtime_commit"]
        assert test["status"] == "pass"
        assert test["tested_commit_tree"] is True
        assert test["selection"] == CONSUMER_TESTS[item["id"]]["selection"]
        assert test["result"] == CONSUMER_TESTS[item["id"]]["result"]
        view = composite if item["view_kind"] == "composite" else snapshot
        report = negotiate_view(view, _requirement(item))
        assert report.compatible, (item["id"], report.to_dict())


def test_recorded_consumers_fail_closed_without_structural_columns_v1() -> None:
    payload = cast(dict[str, Any], json.loads(MANIFEST.read_text(encoding="utf-8")))
    snapshot = _snapshot("urn:manifest:missing-schema")
    capabilities = snapshot.capabilities
    without_encoded_views = pyowl_core.CoreCapabilities(
        adapter_protocol=capabilities.adapter_protocol,
        model_schema=capabilities.model_schema,
        wire_format=capabilities.wire_format,
        features=capabilities.features,
        encoded_view_schemas={},
        backend=capabilities.backend,
    )

    for item in cast(list[dict[str, Any]], payload["consumers"]):
        report = negotiate_capabilities(without_encoded_views, _requirement(item))
        assert not report.compatible
        encoded_issues = [
            (issue.code, issue.field)
            for issue in report.issues
            if issue.field.startswith("encoded_view:")
        ]
        assert encoded_issues == [
            (
                "MISSING_ENCODED_VIEW",
                "encoded_view:pyowl-core/structural-columns",
            )
        ]


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
