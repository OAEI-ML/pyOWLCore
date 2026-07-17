from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from pyowl_core import BackendPreference
from pyowl_core.backends import native
from tools.benchmark.harness import run_harness
from tools.benchmark.manifest import load_manifest
from tools.benchmark.profile import capture_profile
from tools.benchmark.synthetic import equivalent_source


def test_python_smoke_harness_validates_every_incremental_and_bounded_lane() -> None:
    report = run_harness(
        corpus_ids=("generated-tiny-functional",),
        backends=(BackendPreference.PYTHON,),
        warmups=0,
        repetitions=1,
    )

    assert report["schema"] == "pyowl-core/performance-run/v1"
    assert report["passed"] is True
    scenarios = {row["id"]: row for row in cast(list[dict[str, Any]], report["scenarios"])}
    assert len(scenarios) == 16
    assert all(row["status"] == "ok" for row in scenarios.values())
    assert all(len(row["samples"]) == 1 for row in scenarios.values())

    handoff = scenarios["generated-tiny-functional/python/consumer-handoff"]["output"]
    assert handoff["identity_preserved"] is True
    assert not any(handoff["counters"].values())

    diamond = scenarios["generated-import-diamond/python/load-closure"]["output"]
    assert diamond["shared_parse_count"] == 1
    assert diamond["counters"]["parser_calls"] == 4

    for phase in ("overlay-create-1", "composite-create-2"):
        output = scenarios[f"generated-tiny-functional/python/{phase}"]["output"]
        assert output["arena_identity"] is True
        assert output["separate_allocation_run"]["peak_bytes"] <= output["incremental_limit_bytes"]

    bounded = scenarios["generated-adversarial-deep/python/depth-limit"]["output"]
    cancelled = scenarios["generated-adversarial-deep/python/pre-cancel"]["output"]
    assert bounded["error_code"] == "RESOURCE_LIMIT"
    assert cancelled["error_code"] == "OPERATION_CANCELLED"


@pytest.mark.skipif(
    not native.probe("parse-functional-v1").available,
    reason="native Functional parser is unavailable",
)
def test_auto_and_native_smoke_outputs_have_exact_parity() -> None:
    report = run_harness(
        corpus_ids=("generated-tiny-functional",),
        backends=(BackendPreference.PYTHON, BackendPreference.AUTO, BackendPreference.NATIVE),
        warmups=0,
        repetitions=1,
    )
    assertions = cast(list[dict[str, Any]], report["assertions"])
    parity = [item for item in assertions if item["name"].endswith("backend-result-parity")]

    assert report["passed"] is True
    assert len(parity) >= 13
    assert all(item["passed"] is True for item in parity)


def test_profile_captures_measured_phase_and_excludes_validation() -> None:
    corpus = load_manifest().by_id("generated-tiny-functional")
    evidence = capture_profile(
        corpus,
        equivalent_source(corpus.format, 8),
        BackendPreference.PYTHON,
        "parse",
        iterations=1,
        top=8,
    )

    assert "pyowl-core measured profile v1" in evidence
    assert "phase: parse" in evidence
    assert f"corpus_sha256: {corpus.sha256}" in evidence
    assert "validation excluded from profile" in evidence
    assert "function calls" in evidence


def test_harness_does_not_prepare_or_fetch_missing_external_corpus(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="prepared corpus is absent"):
        run_harness(
            cache_dir=tmp_path,
            corpus_ids=("uberon-common-anatomy-2026-06-23",),
            backends=(BackendPreference.PYTHON,),
            warmups=0,
            repetitions=1,
        )
