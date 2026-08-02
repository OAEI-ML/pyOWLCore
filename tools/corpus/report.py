"""Build the deterministic WP09 conformance evidence summary."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the Python 3.10 lane
    import tomli as tomllib  # type: ignore[import-not-found, unused-ignore]

from tools.corpus.coverage import build_coverage
from tools.corpus.differential import core_comparison
from tools.corpus.manifest import PROVENANCE, validate_manifest

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "tests" / "data"
OUTPUT = ROOT / "reports" / "conformance" / "summary.json"


def _toml(name: str) -> Mapping[str, Any]:
    path = DATA / name
    with path.open("rb") as stream:
        value = tomllib.load(stream)
    if not isinstance(value, Mapping) or value.get("schema") != 1:
        raise ValueError(f"{name} must contain a schema-1 TOML table")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_report() -> dict[str, object]:
    artifacts = validate_manifest()
    coverage = build_coverage()
    deviations = _toml("deviations.toml")
    decisions = _toml("errata.toml")
    oracles = _toml("external-oracles.toml")
    registered_deviations = deviations.get("deviation", [])
    if not isinstance(registered_deviations, list):
        raise ValueError("deviations.toml deviation entries must be an array")
    java_oracle = oracles.get("java_oracle")
    if not isinstance(java_oracle, Mapping) or java_oracle.get("enabled") is not False:
        raise ValueError("the normal conformance lane must keep the Java oracle disabled")
    return {
        "constructor_coverage": {
            "constructors": coverage["constructor_count"],
            "required_evidence": coverage["required_evidence_columns"],
        },
        "corpus": {
            "artifacts": len(artifacts),
            "categories": sorted({artifact.category for artifact in artifacts}),
            "provenance_sha256": _sha256(PROVENANCE),
        },
        "deviations": {
            "count": len(registered_deviations),
            "registry_sha256": _sha256(DATA / "deviations.toml"),
        },
        "differential": core_comparison(),
        "errata": {
            "covered_decisions": len(decisions.get("decision", [])),
            "ledger_sha256": _sha256(DATA / "errata.toml"),
        },
        "external_oracles": {
            "java_enabled": False,
            "lock_sha256": _sha256(DATA / "external-oracles.toml"),
            "normal_lane_requires_external_oracle": False,
        },
        "model_schema": 2,
        "python": ["3.10", "3.12"],
        "schema": 1,
        "status": "no-unexplained-deviations",
    }


def render_report() -> str:
    return json.dumps(build_report(), indent=2, sort_keys=True) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    rendered = render_report()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != rendered:
            print(f"stale conformance report: {OUTPUT}")
            return 1
        return 0
    if args.write:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(rendered, encoding="utf-8")
        return 0
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_report", "main", "render_report"]
