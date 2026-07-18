from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRACEABILITY = ROOT / "docs" / "spec-traceability.md"


def test_every_normative_spec_has_an_explicit_traceability_entry() -> None:
    text = TRACEABILITY.read_text(encoding="utf-8")
    expected = {
        "SPEC.md",
        "adapters.md",
        "architecture.md",
        "contracts.md",
        "indexes-views.md",
        "model.md",
        "native-backend.md",
        "packaging.md",
        "parsing-imports.md",
        "performance.md",
        "references.md",
        "security.md",
        "snapshots-overlays.md",
        "verification.md",
        "wire-format.md",
    }
    assert {name for name in expected if f"../specs/{name}" not in text} == set()


def test_completed_workpackages_link_to_retained_handoff_evidence() -> None:
    text = TRACEABILITY.read_text(encoding="utf-8")
    for number in range(11):
        report = ROOT / "reports" / "workpackages" / f"WP{number:02}.md"
        assert report.is_file()
        assert f"../reports/workpackages/{report.name}" in text
    assert "../reports/integration/WP11.md" in text
    for number in (12, 13):
        report = ROOT / "reports" / "workpackages" / f"WP{number:02}.md"
        assert report.is_file()
        assert f"../reports/workpackages/{report.name}" in text


def test_wp12_and_wp13_handoffs_do_not_claim_external_completion() -> None:
    wp12 = (ROOT / "reports" / "workpackages" / "WP12.md").read_text(encoding="utf-8")
    wp13 = (ROOT / "reports" / "workpackages" / "WP13.md").read_text(encoding="utf-8")
    for blocker in (
        "PyPI and TestPyPI control",
        "Legal review",
        "Trusted Publisher",
        "reference-machine",
    ):
        assert blocker in wp12
    assert "not release-ready" in wp12
    assert "1.0 acceptance gate remains blocked" in wp13
    assert ">=0.1,<0.2" in wp13
    for report in (wp12, wp13):
        assert "## Implementation revisions and owned-file inventory" in report
        assert "## Executable verification matrix" in report
        assert "PYTHONPATH=src" in report


def test_compatibility_separates_support_from_executed_evidence() -> None:
    compatibility = (ROOT / "docs" / "compatibility.md").read_text(encoding="utf-8")
    for interpreter in (
        "CPython 3.10",
        "CPython 3.11",
        "CPython 3.12",
        "CPython 3.13",
        "CPython 3.14",
        "PyPy 3.10",
    ):
        assert interpreter in compatibility
    assert "selected-revision hosted result pending" in compatibility
    assert "pure wheel only" in compatibility


def test_traceability_keeps_external_release_blockers_explicit() -> None:
    text = TRACEABILITY.read_text(encoding="utf-8")
    for blocker in (
        "PyPI/TestPyPI control",
        "legal approval",
        "trusted publication",
        "external signatures",
        "approved reference machine",
        "consumer-owner approval",
    ):
        assert blocker in text
