"""Generate the exhaustive constructor-to-verification evidence ledger."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "reports" / "conformance" / "constructor-coverage.json"

_EVIDENCE = {
    "canonical": (
        "tests/conformance/test_constructor_coverage.py::"
        "test_every_registered_fixture_has_canonical_visitor_and_reference_evidence"
    ),
    "formats": (
        "tests/conformance/test_constructor_coverage.py::"
        "test_every_constructor_crosses_all_required_formats"
    ),
    "generative": "tests/conformance/test_metamorphic.py",
    "native": (
        "tests/conformance/test_differential.py::"
        "test_functional_python_native_and_independent_wire_cross_product"
    ),
    "reference_index": "pyowl_core.index.common.iter_structural_occurrences/FIELD_ROLE_TABLE",
    "signature_visitor": "pyowl_core.model.visitor.walk/signature",
    "validation": "tests/unit/model/test_validation.py and constructor __post_init__",
    "wire": (
        "tests/conformance/test_constructor_coverage.py::"
        "test_every_constructor_crosses_canonical_wire_and_independent_reader"
    ),
}


def build_coverage() -> dict[str, object]:
    source = str(ROOT / "src")
    sys.path.insert(0, source)
    try:
        from pyowl_core.model.registry import CONSTRUCTOR_SPECS

        rows = []
        for spec in CONSTRUCTOR_SPECS:
            rows.append(
                {
                    "category": spec.category,
                    "constructor": spec.constructor.__name__,
                    "evidence": sorted(_EVIDENCE),
                    "fields": list(spec.fields),
                    "model_fixture": (
                        "tests.generated.model.fixtures.model_fixtures"
                        f"[{spec.constructor.__name__}]"
                    ),
                    "production": spec.production,
                    "schema_tag": spec.tag,
                    "schema_tag_name": spec.tag_name,
                }
            )
    finally:
        sys.path.remove(source)
    return {
        "constructor_count": len(rows),
        "evidence_catalog": _EVIDENCE,
        "model_schema": 2,
        "required_evidence_columns": sorted(_EVIDENCE),
        "rows": rows,
        "schema": 1,
    }


def render_coverage() -> str:
    return json.dumps(build_coverage(), indent=2, sort_keys=True) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    rendered = render_coverage()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != rendered:
            print(f"stale constructor coverage: {OUTPUT}", file=sys.stderr)
            return 1
        return 0
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_coverage", "main", "render_coverage"]
