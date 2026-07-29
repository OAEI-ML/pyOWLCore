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
CORE_FINAL_COMMIT = "9251059e10ab1c4474d58d7c3d61b63c0ae3d23c"
CORE_RUNTIME_COMMIT = "21503cf5a35c22c1fa35653c13df958df4fca100"
STRUCTURAL_SCHEMA_REQUIREMENT = {"pyowl-core/structural-columns": 1}
CONSUMER_IDENTITIES = {
    "exact-om": {
        "final_commit": "abba717bd5b3f186678bd6f3e88bf73066c2ae49",
        "runtime_commit": "ab4b76644f6ed58894d0920e47de713ba1ffb358",
        "role": "compatibility-consumer",
    },
    "oaei-bioml-eval": {
        "final_commit": "e5d1affaf66600b09b8d771c2bb691a10cfda852",
        "runtime_commit": "fd75aedbf9f5ed4351d3f6d634a6e07721d21778",
        "role": "compatibility-consumer",
    },
    "pyelk": {
        "final_commit": "faf7a995bd4b44964d7e5a56007ae484df79d597",
        "runtime_commit": "bc75f4be609626f231cdc91af800f52bae46c766",
        "role": "encoded-native-compiler",
    },
    "pyhermit": {
        "final_commit": "f0d4ebb270f3521b848cd2a858761afd66e72ae2",
        "runtime_commit": "f0d4ebb270f3521b848cd2a858761afd66e72ae2",
        "role": "encoded-native-compiler",
    },
    "projector": {
        "final_commit": "8f599fb00708703f3bdbdbbf2d0064bc2935167c",
        "runtime_commit": "46b066f698cc790aceae4f8eaf50212934e94708",
        "role": "encoded-native-compiler",
    },
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
        "result": "86 passed",
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
    assert payload["schema"] == "pyowl-core.consumer-compatibility/3"
    assert payload["recorded_date"] == "2026-07-28"
    core = cast(dict[str, Any], payload["core"])
    assert core["package_version"] == pyowl_core.__version__
    assert tuple(core["api_version"]) == pyowl_core.API_VERSION
    assert core["model_schema"] == pyowl_core.MODEL_SCHEMA_VERSION
    assert tuple(core["wire_format"]) == pyowl_core.WIRE_FORMAT_VERSION
    assert core["adapter_protocol"] == pyowl_core.ADAPTER_PROTOCOL_VERSION
    assert core["final_commit"] == CORE_FINAL_COMMIT
    assert core["runtime_commit"] == CORE_RUNTIME_COMMIT
    assert core["encoded_view_schemas"] == STRUCTURAL_SCHEMA_REQUIREMENT
    assert HEX40.fullmatch(core["adapter_contract_commit"])

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
