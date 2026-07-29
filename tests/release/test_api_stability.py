from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import pyowl_core
from tools.audit.public_api import audit_public_api

ROOT = Path(__file__).resolve().parents[2]
COMPATIBILITY = ROOT / "reports" / "integration" / "consumer-compatibility.json"


def test_curated_exports_match_the_reviewed_candidate_snapshot() -> None:
    assert audit_public_api(ROOT) == []
    assert len(pyowl_core.__all__) == len(set(pyowl_core.__all__))
    assert all(hasattr(pyowl_core, name) for name in pyowl_core.__all__)
    assert set(pyowl_core.model.__all__) <= set(pyowl_core.__all__)
    assert set(pyowl_core.index.__all__) <= set(pyowl_core.__all__)


def test_independent_version_domains_and_distribution_metadata_agree() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', pyproject, flags=re.MULTILINE)
    assert match is not None
    assert match.group(1) == pyowl_core.__version__
    assert pyowl_core.API_VERSION == (0, 1)
    assert pyowl_core.MODEL_SCHEMA_VERSION == 1
    assert pyowl_core.WIRE_FORMAT_VERSION == (1, 1)
    assert pyowl_core.ADAPTER_PROTOCOL_VERSION == 1
    assert (ROOT / "src" / "pyowl_core" / "py.typed").is_file()


def test_documented_consumer_handoff_matches_exact_revision_evidence() -> None:
    payload = cast(dict[str, Any], json.loads(COMPATIBILITY.read_text(encoding="utf-8")))
    documentation = (ROOT / "docs" / "compatibility.md").read_text(encoding="utf-8")
    core = cast(dict[str, Any], payload["core"])
    assert core["package_version"] in documentation
    assert core["final_commit"] in documentation
    assert core["runtime_commit"] in documentation
    assert f"`({core['api_version'][0]},{core['api_version'][1]})`" in documentation
    assert f"`({core['wire_format'][0]},{core['wire_format'][1]})`" in documentation
    for name, version in cast(dict[str, int], core["encoded_view_schemas"]).items():
        assert f"`{name}` v{version}" in documentation
    for consumer in cast(list[dict[str, Any]], payload["consumers"]):
        assert consumer["final_commit"] in documentation
        assert consumer["runtime_commit"] in documentation
        assert f"`{consumer['role']}`" in documentation
        assert consumer["package_version"] in documentation
        assert consumer["core_requirement"] in documentation
        assert consumer["required_encoded_view_schemas"] == core["encoded_view_schemas"]


def test_release_checklist_does_not_claim_external_approvals() -> None:
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")
    assert "This is a verification ledger, not a statement" in checklist
    assert "- [ ] `pyowl-core` ownership is confirmed" in checklist
    assert "- [ ] License owner approves" in checklist
    assert "- [ ] Trusted-publishing rehearsal" in checklist
    assert "- [ ] Consumer requirements are updated" in checklist
    assert "Any unchecked item is a release blocker" in checklist


def test_docs_disclose_candidate_status_and_unsupported_performance_claims() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    performance = (ROOT / "docs" / "performance.md").read_text(encoding="utf-8")
    assert "not yet a published 1.0 release" in readme
    assert "No 2x parser claim" in " ".join(performance.split())
    assert "shared-host" in performance
    assert "reference" in performance
