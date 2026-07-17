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


def _snapshot(iri: str) -> pyowl_core.OntologySnapshot:
    return pyowl_core.load_snapshot(
        f"Ontology(<{iri}> Declaration(Class(<{iri}#A>)))".encode(),
        options=pyowl_core.LoadOptions(backend=pyowl_core.BackendPreference.PYTHON),
    )


def test_recorded_consumer_contracts_negotiate_with_the_implementation_checkpoint() -> None:
    payload = cast(dict[str, Any], json.loads(MANIFEST.read_text(encoding="utf-8")))
    assert payload["schema"] == "pyowl-core.consumer-compatibility/1"
    core = cast(dict[str, Any], payload["core"])
    assert core["package_version"] == pyowl_core.__version__
    assert tuple(core["api_version"]) == pyowl_core.API_VERSION
    assert core["model_schema"] == pyowl_core.MODEL_SCHEMA_VERSION
    assert tuple(core["wire_format"]) == pyowl_core.WIRE_FORMAT_VERSION
    assert core["adapter_protocol"] == pyowl_core.ADAPTER_PROTOCOL_VERSION
    assert HEX40.fullmatch(core["implementation_commit"])
    assert HEX40.fullmatch(core["adapter_contract_commit"])

    snapshot = _snapshot("urn:manifest:one")
    second = _snapshot("urn:manifest:two")
    composite = pyowl_core.compose_views(snapshot, second, roles=("source", "target"))
    consumers = cast(list[dict[str, Any]], payload["consumers"])
    assert [item["id"] for item in consumers] == [
        "exact-om",
        "oaei-bioml-eval",
        "pyelk",
        "pyhermit",
        "projector",
    ]
    for item in consumers:
        assert HEX40.fullmatch(item["commit"])
        assert item["core_requirement"] == ">=0.1,<0.2"
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
