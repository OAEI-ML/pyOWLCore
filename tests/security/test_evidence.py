from __future__ import annotations

from pathlib import Path

from tools.corpus.report import OUTPUT as CONFORMANCE_REPORT
from tools.corpus.report import render_report
from tools.security.evidence import OUTPUT as SECURITY_MATRIX
from tools.security.evidence import render_matrix, validate_controls
from tools.security.minimize import minimize

ROOT = Path(__file__).parents[2]


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


def test_minimized_regression_workflow_reaches_one_minimal_subsequence() -> None:
    result = minimize(b"noise[TRIGGER]tail", lambda value: b"TRIGGER" in value)
    assert result == b"TRIGGER"
    assert all(
        b"TRIGGER" not in result[:index] + result[index + 1 :]
        for index in range(len(result))
    )
