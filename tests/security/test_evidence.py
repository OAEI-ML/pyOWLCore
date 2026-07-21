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
NATIVE_LIFECYCLE = (
    ROOT / "reports" / "security" / "native-lifecycle-checkpoint.json"
)
NATIVE_VIEW_LIFECYCLE = (
    ROOT / "reports" / "security" / "native-view-lifecycle-checkpoint.json"
)
NATIVE_MAPPED_WIRE = (
    ROOT / "reports" / "security" / "native-mapped-wire-checkpoint.json"
)
NATIVE_ALLOCATION = (
    ROOT / "reports" / "security" / "native-allocation-checkpoint.json"
)


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


def test_native_lifecycle_checkpoint_is_exact_and_fail_closed() -> None:
    checkpoint = json.loads(NATIVE_LIFECYCLE.read_text(encoding="utf-8"))

    assert checkpoint["schema"] == "pyowl-core.native-lifecycle-checkpoint/1"
    assert re.fullmatch(r"[0-9a-f]{40}", checkpoint["subject_revision"])
    assert checkpoint["claim"] == "checkpoint-only"
    assert checkpoint["capability_advertised"] is False
    assert checkpoint["artifact"]["kind"] == "local test-hook extension"

    workflow = checkpoint["continuous_workflow"]
    assert workflow["path"] == ".github/workflows/native-safety.yml"
    assert workflow["job"] == "runtime-lifecycle"
    assert workflow["status"] == "configured-not-run"
    assert workflow["reason"]
    assert [row["python"] for row in workflow["matrix"]] == [
        "3.10",
        "3.12",
        "3.14",
        "3.14t",
    ]

    runs = {run["id"]: run for run in checkpoint["runs"]}
    assert {name: run["status"] for name, run in runs.items()} == {
        "cpython-3.12-native-owner-lifecycle": "pass",
        "cpython-3.11-subinterpreter-python-fallback": "pass",
        "cpython-3.12-subinterpreter-python-fallback": "not-run",
        "cpython-3.14-public-interpreter-api": "not-run",
        "cpython-3.14t-python-fallback": "not-run",
    }
    for run in runs.values():
        if run["status"] == "pass":
            assert run["command"]
            assert run["observations"]
            assert run["notes"]
        else:
            assert run["reason"]
            assert run["evidence"]

    assert runs["cpython-3.12-native-owner-lifecycle"]["observations"][
        "tests_failed"
    ] == 0
    fallback = runs["cpython-3.11-subinterpreter-python-fallback"]["observations"]
    assert fallback["interpreters_created"] == fallback["interpreters_destroyed"] == 8
    assert fallback["native_extension_import_attempts"] == 0
    assert (ROOT / "tools" / "security" / "subinterpreter_probe.py").is_file()

    release = checkpoint["release_effect"]
    assert release["lifecycle_matrix"] == "not-run"
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
        assert "native-lifecycle-checkpoint.json" in text


def test_native_view_lifecycle_checkpoint_is_exact_and_fail_closed() -> None:
    checkpoint = json.loads(NATIVE_VIEW_LIFECYCLE.read_text(encoding="utf-8"))

    assert checkpoint["schema"] == "pyowl-core.native-view-lifecycle-checkpoint/1"
    assert re.fullmatch(r"[0-9a-f]{40}", checkpoint["subject_revision"])
    assert checkpoint["claim"] == "checkpoint-only"
    assert checkpoint["capability_advertised"] is False
    assert checkpoint["artifact"]["kind"] == "local test-hook extension"
    assert re.fullmatch(r"[0-9a-f]{64}", checkpoint["artifact"]["sha256"])

    workflow = checkpoint["continuous_workflow"]
    assert workflow["path"] == ".github/workflows/native-safety.yml"
    assert workflow["job"] == "runtime-lifecycle"
    assert workflow["status"] == "configured-not-run"
    workflow_text = (ROOT / workflow["path"]).read_text(encoding="utf-8")
    assert "test_process_lifecycle.py" in workflow_text

    run = checkpoint["run"]
    assert run["status"] == "pass"
    assert run["command"]
    assert run["working_directory"]
    assert run["observations"]["tests_failed"] == 0
    assert run["observations"]["tests_passed"] >= 4
    assert len(run["encoded_view_cases"]) == 4
    assert run["notes"]

    release = checkpoint["release_effect"]
    assert release["local_encoded_view_lifecycle"] == "pass"
    assert release["supported_platform_lifecycle"] == "not-run"
    assert release["security_resource_determinism"] == "not-run"
    assert release["core_release_eligible"] is False
    assert release["reason"]
    assert checkpoint["limitations"]

    for report in (
        ROOT / "reports" / "security" / "README.md",
        ROOT / "reports" / "workpackages" / "WP17.md",
        ROOT / "reports" / "workpackages" / "WP18.md",
    ):
        text = report.read_text(encoding="utf-8")
        assert "native-view-lifecycle-checkpoint.json" in text


def test_native_mapped_wire_checkpoint_is_exact_and_fail_closed() -> None:
    checkpoint = json.loads(NATIVE_MAPPED_WIRE.read_text(encoding="utf-8"))

    assert checkpoint["schema"] == "pyowl-core.native-mapped-wire-checkpoint/1"
    assert re.fullmatch(r"[0-9a-f]{40}", checkpoint["subject_revision"])
    assert checkpoint["claim"] == "checkpoint-only"
    assert checkpoint["capability_advertised"] is False
    assert checkpoint["artifact"]["kind"] == "local test-hook extension"
    assert re.fullmatch(r"[0-9a-f]{64}", checkpoint["artifact"]["sha256"])

    implementation = checkpoint["implementation"]
    assert implementation["constructor_fixture_tags"] == 76
    assert "raw-document" in implementation["scoped_retained_fast_path"]
    assert len(implementation["invariants"]) >= 9

    runs = {run["id"]: run for run in checkpoint["runs"]}
    assert set(runs) == {
        "cpython-3.12-scoped-retained-wire-matrix",
        "cpython-3.12-consumer-cache-contracts",
        "rust-native-library",
        "static-quality-gates",
    }
    assert all(run["status"] == "pass" for run in runs.values())
    python_run = runs["cpython-3.12-scoped-retained-wire-matrix"]
    assert python_run["observations"]["tests_failed"] == 0
    assert python_run["observations"]["tests_passed"] >= 168
    assert python_run["observations"]["subtests_passed"] >= 2987
    consumer_run = runs["cpython-3.12-consumer-cache-contracts"]
    assert consumer_run["observations"]["tests_failed"] == 0
    assert consumer_run["observations"]["tests_passed"] >= 36
    assert runs["rust-native-library"]["observations"]["tests_failed"] == 0
    assert runs["static-quality-gates"]["commands"]

    release = checkpoint["release_effect"]
    assert release["local_mapped_wire_constructor_matrix"] == "pass"
    assert release["local_scoped_retained_wire_matrix"] == "pass"
    assert release["installed_artifact_matrix"] == "not-run"
    assert release["supported_platform_matrix"] == "not-run"
    assert release["complete_format_option_import_matrix"] == "not-run"
    assert release["security_resource_determinism"] == "not-run"
    assert release["core_release_eligible"] is False
    assert release["reason"]
    assert checkpoint["limitations"]

    for report in (
        ROOT / "reports" / "security" / "README.md",
        ROOT / "reports" / "workpackages" / "WP15.md",
        ROOT / "reports" / "workpackages" / "WP17.md",
        ROOT / "reports" / "workpackages" / "WP18.md",
    ):
        text = report.read_text(encoding="utf-8")
        assert "native-mapped-wire-checkpoint.json" in text


def test_native_allocation_checkpoint_is_exact_and_fail_closed() -> None:
    checkpoint = json.loads(NATIVE_ALLOCATION.read_text(encoding="utf-8"))

    assert checkpoint["schema"] == "pyowl-core.native-allocation-checkpoint/1"
    assert re.fullmatch(r"[0-9a-f]{40}", checkpoint["subject_revision"])
    assert checkpoint["claim"] == "checkpoint-only"
    assert checkpoint["capability_advertised"] is False
    assert checkpoint["artifact"]["kind"] == "local test-hook extension"
    assert re.fullmatch(r"[0-9a-f]{64}", checkpoint["artifact"]["sha256"])

    workflow = checkpoint["continuous_workflow"]
    assert workflow["path"] == ".github/workflows/native-safety.yml"
    assert workflow["job"] == "runtime-lifecycle"
    assert workflow["status"] == "configured-not-run"
    workflow_text = (ROOT / workflow["path"]).read_text(encoding="utf-8")
    assert "test_allocation_failure.py" in workflow_text

    sweep = checkpoint["sweep"]
    assert sweep["constructors"] == 76
    phases = {phase["name"]: phase for phase in sweep["phases"]}
    assert set(phases) == {"build", "freeze", "encode"}
    assert all(phase["allocation_checkpoints"] > 0 for phase in phases.values())
    assert all(
        phase["allocation_checkpoints"] == phase["injected_failures"]
        for phase in phases.values()
    )
    assert sweep["total_allocation_checkpoints"] == sum(
        phase["allocation_checkpoints"] for phase in phases.values()
    )
    assert sweep["total_injected_failures"] == sweep["total_allocation_checkpoints"]
    assert sweep["total_boundary_successes"] == 3 * sweep["constructors"]
    assert len(sweep["invariants"]) >= 5
    wire = sweep["wire_validation"]
    assert wire["allocation_checkpoints"] == len(wire["boundaries"]) == 5
    assert wire["injected_failures"] == wire["allocation_checkpoints"]
    assert wire["boundary_successes"] == 1
    publication = sweep["publication_facade"]
    assert publication["allocation_checkpoints"] == 57
    assert publication["injected_failures"] == publication["allocation_checkpoints"]
    assert publication["boundary_successes"] == 1
    assert publication["scope"]
    bridge = sweep["encoded_view_bridge"]
    assert bridge["allocation_checkpoints"] == 51
    assert bridge["injected_failures"] == bridge["allocation_checkpoints"]
    assert bridge["boundary_successes"] == 1
    assert bridge["scope"]
    workspace = sweep["encoded_view_workspace"]
    assert workspace["allocation_checkpoints"] == 13
    assert workspace["injected_failures"] == workspace["allocation_checkpoints"]
    assert workspace["boundary_successes"] == 1
    assert workspace["scope"]
    parser = sweep["parser_session"]
    assert parser["allocation_checkpoints"] == 38
    assert parser["injected_failures"] == parser["allocation_checkpoints"]
    assert parser["boundary_successes"] == 1
    assert parser["scope"]
    parser_bridge = sweep["parser_bridge"]
    assert parser_bridge["allocation_checkpoints"] == 13
    assert parser_bridge["injected_failures"] == parser_bridge["allocation_checkpoints"]
    assert parser_bridge["boundary_successes"] == 1
    assert parser_bridge["scope"]
    rdfxml_bridge = sweep["rdfxml_retained_bridge"]
    assert rdfxml_bridge["allocation_checkpoints"] == 9
    assert rdfxml_bridge["injected_failures"] == rdfxml_bridge["allocation_checkpoints"]
    assert rdfxml_bridge["boundary_successes"] == 1
    assert rdfxml_bridge["scope"]
    index_bridge = sweep["index_bridge"]
    assert index_bridge["allocation_checkpoints"] == 13
    assert index_bridge["injected_failures"] == index_bridge["allocation_checkpoints"]
    assert index_bridge["boundary_successes"] == 1
    assert index_bridge["scope"]
    foundation_bridge = sweep["foundation_bridge"]
    assert foundation_bridge["operations"] == 3
    assert foundation_bridge["allocation_checkpoints_per_operation"] == 13
    assert foundation_bridge["allocation_checkpoints"] == 39
    assert foundation_bridge["injected_failures"] == foundation_bridge["allocation_checkpoints"]
    assert foundation_bridge["boundary_successes"] == foundation_bridge["operations"]
    assert foundation_bridge["scope"]
    assert sweep["covered_allocation_checkpoints"] == (
        sweep["total_allocation_checkpoints"]
        + wire["allocation_checkpoints"]
        + publication["allocation_checkpoints"]
        + bridge["allocation_checkpoints"]
        + workspace["allocation_checkpoints"]
        + parser["allocation_checkpoints"]
        + parser_bridge["allocation_checkpoints"]
        + rdfxml_bridge["allocation_checkpoints"]
        + index_bridge["allocation_checkpoints"]
        + foundation_bridge["allocation_checkpoints"]
    )
    assert sweep["covered_injected_failures"] == sweep["covered_allocation_checkpoints"]
    assert sweep["covered_boundary_successes"] == (
        sweep["total_boundary_successes"]
        + wire["boundary_successes"]
        + publication["boundary_successes"]
        + bridge["boundary_successes"]
        + workspace["boundary_successes"]
        + parser["boundary_successes"]
        + parser_bridge["boundary_successes"]
        + rdfxml_bridge["boundary_successes"]
        + index_bridge["boundary_successes"]
        + foundation_bridge["boundary_successes"]
    )

    assert {run["id"]: run["status"] for run in checkpoint["runs"]} == {
        "cpython-3.12-retained-allocation-boundary": "pass",
        "rust-retained-allocation-regressions": "pass",
    }
    for run in checkpoint["runs"]:
        assert run["command"]
        assert run["working_directory"]
        assert run["observations"]["tests_failed"] == 0
        assert run["notes"]

    release = checkpoint["release_effect"]
    assert release["retained_component_allocation_failures"] == "local-pass"
    assert release["native_wire_validation_allocation_failures"] == "local-pass"
    assert release["retained_publication_allocation_failures"] == "local-pass"
    assert release["encoded_view_python_bridge_allocation_failures"] == "local-pass"
    assert release["encoded_view_rust_workspace_allocation_failures"] == "local-pass"
    assert release["native_parser_session_allocation_failures"] == "local-pass"
    assert release["native_parser_python_bridge_allocation_failures"] == "local-pass"
    assert (
        release["native_rdfxml_retained_python_bridge_allocation_failures"]
        == "local-pass"
    )
    assert release["native_index_python_bridge_allocation_failures"] == "local-pass"
    assert release["native_foundation_python_bridge_allocation_failures"] == "local-pass"
    assert release["end_to_end_allocation_failure_matrix"] == "not-run"
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
        assert "native-allocation-checkpoint.json" in text


def test_minimized_regression_workflow_reaches_one_minimal_subsequence() -> None:
    result = minimize(b"noise[TRIGGER]tail", lambda value: b"TRIGGER" in value)
    assert result == b"TRIGGER"
    assert all(
        b"TRIGGER" not in result[:index] + result[index + 1 :]
        for index in range(len(result))
    )
