from __future__ import annotations

import json
import re
from pathlib import Path

from tools.corpus.report import OUTPUT as CONFORMANCE_REPORT
from tools.corpus.report import render_report
from tools.security.evidence import OUTPUT as SECURITY_MATRIX
from tools.security.evidence import render_matrix, validate_controls
from tools.security.minimize import minimize

ROOT = Path(__file__).parents[2]
NATIVE_SAFETY = ROOT / "reports" / "security" / "native-safety-checkpoint.json"


def test_conformance_and_security_reports_are_reproducible() -> None:
    assert CONFORMANCE_REPORT.read_text(encoding="utf-8") == render_report()
    assert SECURITY_MATRIX.read_text(encoding="utf-8") == render_matrix()
    assert len(validate_controls()) >= 12


def test_security_process_draft_has_release_blocking_contact_fields() -> None:
    process = (ROOT / "reports" / "security" / "disclosure-draft.md").read_text(
        encoding="utf-8"
    )
    for heading in (
        "Private contact",
        "Supported versions",
        "Response targets",
        "Coordinated disclosure",
        "Pre-1.0 release gate",
    ):
        assert heading in process


def test_native_safety_checkpoint_is_exact_and_fail_closed() -> None:
    checkpoint = json.loads(NATIVE_SAFETY.read_text(encoding="utf-8"))

    assert checkpoint["schema"] == "pyowl-core.native-safety-checkpoint/1"
    assert re.fullmatch(r"[0-9a-f]{40}", checkpoint["subject_revision"])
    assert checkpoint["claim"] == "checkpoint-only"
    assert checkpoint["capability_advertised"] is False

    workflow = checkpoint["continuous_workflow"]
    assert workflow["path"] == ".github/workflows/native-safety.yml"
    assert (ROOT / workflow["path"]).is_file()
    assert workflow["status"] == "configured-not-run"
    assert workflow["reason"]

    runs = checkpoint["runs"]
    assert {run["id"] for run in runs} == {
        "address-sanitizer-native-library",
        "thread-sanitizer-native-library",
        "miri-pure-ownership",
        "functional-libfuzzer-address",
        "wire-libfuzzer-address",
    }
    assert all(run["status"] == "pass" for run in runs)
    for run in runs:
        assert run["command"]
        assert run["working_directory"]
        assert run["observations"]
        assert run["notes"]
        assert run["observations"].get("tests_failed", 0) == 0
        assert run["observations"].get("crashes", 0) == 0
        assert run["observations"].get("sanitizer_findings", 0) == 0
        assert run["observations"].get("miri_findings", 0) == 0

    release = checkpoint["release_effect"]
    assert release["security_resource_determinism"] == "not-run"
    assert release["core_release_eligible"] is False
    assert release["reason"]
    assert checkpoint["limitations"]

    for report in (
        ROOT / "reports" / "security" / "README.md",
        ROOT / "reports" / "workpackages" / "WP15.md",
        ROOT / "reports" / "workpackages" / "WP18.md",
    ):
        text = report.read_text(encoding="utf-8")
        assert "native-safety-checkpoint.json" in text


def test_minimized_regression_workflow_reaches_one_minimal_subsequence() -> None:
    result = minimize(b"noise[TRIGGER]tail", lambda value: b"TRIGGER" in value)
    assert result == b"TRIGGER"
    assert all(
        b"TRIGGER" not in result[:index] + result[index + 1 :]
        for index in range(len(result))
    )
