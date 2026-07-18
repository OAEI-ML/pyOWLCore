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
