"""Fail-closed performance baseline comparison with the WP10 release thresholds."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

MEDIAN_LIMIT = 1.10
RSS_LIMIT = 1.10
TAIL_LIMIT = 1.15


class RegressionDataError(ValueError):
    """A baseline/candidate report is incomplete or not comparable."""


@dataclass(frozen=True, slots=True)
class Finding:
    scenario: str
    metric: str
    baseline: float
    candidate: float
    ratio: float
    limit: float
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, str | float | bool]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Comparison:
    baseline_commit: str
    candidate_commit: str
    findings: tuple[Finding, ...]

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.findings)

    def as_dict(self) -> dict[str, str | bool | list[dict[str, str | float | bool]]]:
        return {
            "schema": "pyowl-core/performance-comparison/v1",
            "baseline_commit": self.baseline_commit,
            "candidate_commit": self.candidate_commit,
            "passed": self.passed,
            "findings": [item.as_dict() for item in self.findings],
        }


def compare_reports(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> Comparison:
    """Compare equivalent successful scenarios and reject semantic drift/missing gates."""

    _validate_report(baseline, "baseline")
    _validate_report(candidate, "candidate")
    baseline_environment = _mapping(baseline.get("environment"), "baseline.environment")
    candidate_environment = _mapping(candidate.get("environment"), "candidate.environment")
    baseline_key = _string(
        baseline_environment.get("comparison_key"), "baseline.environment.comparison_key"
    )
    candidate_key = _string(
        candidate_environment.get("comparison_key"), "candidate.environment.comparison_key"
    )
    if baseline_key != candidate_key:
        raise RegressionDataError(
            "machine/runtime comparison key changed; explicit baseline review is required"
        )
    baseline_manifest = _string(baseline.get("corpus_manifest_sha256"), "manifest hash")
    candidate_manifest = _string(candidate.get("corpus_manifest_sha256"), "manifest hash")
    if baseline_manifest != candidate_manifest:
        raise RegressionDataError(
            "corpus manifest hash changed; explicit baseline review is required"
        )
    baseline_rows = _scenario_map(baseline, "baseline")
    candidate_rows = _scenario_map(candidate, "candidate")
    if set(baseline_rows) != set(candidate_rows):
        missing = sorted(set(baseline_rows) - set(candidate_rows))
        added = sorted(set(candidate_rows) - set(baseline_rows))
        raise RegressionDataError(f"scenario set changed; missing={missing}, added={added}")
    findings: list[Finding] = []
    for key in sorted(baseline_rows):
        before = baseline_rows[key]
        after = candidate_rows[key]
        before_status = _string(before.get("status"), f"{key}.status")
        after_status = _string(after.get("status"), f"{key}.status")
        required = _boolean(before.get("required", True), f"{key}.required")
        if before_status != after_status:
            raise RegressionDataError(
                f"{key}: status changed from {before_status} to {after_status}"
            )
        if before_status != "ok":
            if required:
                raise RegressionDataError(f"{key}: required scenario is not runnable")
            continue
        if _output_fingerprint(before, key) != _output_fingerprint(after, key):
            raise RegressionDataError(f"{key}: validated output fingerprint changed")
        kind = _string(before.get("kind"), f"{key}.kind")
        findings.append(_ratio_finding(key, "wall_ns.median", before, after, MEDIAN_LIMIT))
        findings.append(_ratio_finding(key, "rss_peak_bytes.median", before, after, RSS_LIMIT))
        if kind in {"query", "mmap"}:
            findings.append(_ratio_finding(key, "wall_ns.p95", before, after, TAIL_LIMIT))
    return Comparison(
        _commit(baseline),
        _commit(candidate),
        tuple(findings),
    )


def load_report(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RegressionDataError(f"cannot read report {path}: {error}") from error
    if not isinstance(payload, Mapping):
        raise RegressionDataError("performance report root must be an object")
    return cast(Mapping[str, Any], payload)


def render_markdown(comparison: Comparison) -> str:
    state = "PASS" if comparison.passed else "FAIL"
    lines = [
        "# Performance regression comparison",
        "",
        f"Status: **{state}**",
        "",
        f"Baseline commit: `{comparison.baseline_commit}`",
        "",
        f"Candidate commit: `{comparison.candidate_commit}`",
        "",
        "| scenario | metric | baseline | candidate | ratio | limit | result |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in comparison.findings:
        result = "pass" if item.passed else "FAIL"
        lines.append(
            f"| {item.scenario} | {item.metric} | {item.baseline:.3f} | "
            f"{item.candidate:.3f} | {item.ratio:.4f} | {item.limit:.2f} | {result} |"
        )
    lines.append("")
    return "\n".join(lines)


def _ratio_finding(
    key: str,
    metric: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    limit: float,
) -> Finding:
    baseline_value = _metric(before, metric, key)
    candidate_value = _metric(after, metric, key)
    if baseline_value == 0:
        ratio = 1.0 if candidate_value == 0 else float("inf")
    else:
        ratio = candidate_value / baseline_value
    passed = ratio <= limit
    return Finding(
        key,
        metric,
        baseline_value,
        candidate_value,
        ratio,
        limit,
        passed,
        "within threshold" if passed else "candidate exceeds release threshold",
    )


def _metric(row: Mapping[str, Any], dotted: str, key: str) -> float:
    metrics = _mapping(row.get("metrics"), f"{key}.metrics")
    group_name, statistic = dotted.split(".", 1)
    group = _mapping(metrics.get(group_name), f"{key}.{group_name}")
    value = group.get(statistic)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise RegressionDataError(f"{key}.{dotted} must be non-negative numeric")
    return float(value)


def _output_fingerprint(row: Mapping[str, Any], key: str) -> str:
    output = _mapping(row.get("output"), f"{key}.output")
    return _string(output.get("fingerprint"), f"{key}.output.fingerprint")


def _scenario_map(report: Mapping[str, Any], label: str) -> dict[str, Mapping[str, Any]]:
    rows = report.get("scenarios")
    if not isinstance(rows, list):
        raise RegressionDataError(f"{label}.scenarios must be an array")
    retained: dict[str, Mapping[str, Any]] = {}
    for value in rows:
        row = _mapping(value, f"{label} scenario")
        key = _string(row.get("id"), "scenario.id")
        if key in retained:
            raise RegressionDataError(f"duplicate scenario id: {key}")
        retained[key] = row
    if not retained:
        raise RegressionDataError(f"{label} has no scenarios")
    return retained


def _validate_report(report: Mapping[str, Any], label: str) -> None:
    if report.get("schema") != "pyowl-core/performance-run/v1":
        raise RegressionDataError(f"{label}: unsupported report schema")
    _commit(report)
    _mapping(report.get("environment"), f"{label}.environment")
    methodology = _mapping(report.get("methodology"), f"{label}.methodology")
    for field in ("cache_state", "warmups", "repetitions", "safety_defaults"):
        if field not in methodology:
            raise RegressionDataError(f"{label}.methodology.{field} is required")


def _commit(report: Mapping[str, Any]) -> str:
    environment = _mapping(report.get("environment"), "environment")
    return _string(environment.get("git_commit"), "environment.git_commit")


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RegressionDataError(f"{field} must be an object")
    return cast(Mapping[str, Any], value)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RegressionDataError(f"{field} must be a nonempty string")
    return value


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise RegressionDataError(f"{field} must be boolean")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--markdown", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        comparison = compare_reports(load_report(args.baseline), load_report(args.candidate))
        payload = json.dumps(comparison.as_dict(), indent=2, sort_keys=True) + "\n"
        markdown = render_markdown(comparison)
        if args.json is None:
            print(payload, end="")
        else:
            args.json.write_text(payload, encoding="utf-8")
        if args.markdown is not None:
            args.markdown.write_text(markdown, encoding="utf-8")
        return 0 if comparison.passed else 1
    except RegressionDataError as error:
        print(f"performance comparison error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MEDIAN_LIMIT",
    "RSS_LIMIT",
    "TAIL_LIMIT",
    "Comparison",
    "Finding",
    "RegressionDataError",
    "compare_reports",
    "load_report",
    "render_markdown",
]
