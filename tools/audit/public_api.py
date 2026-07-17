"""Compare curated runtime exports with the reviewed snapshot."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence
from pathlib import Path

from .common import run_cli


def audit_public_api(root: Path) -> list[str]:
    snapshot = root / "tools" / "audit" / "public-api-v0.txt"
    if not snapshot.is_file():
        return ["public-api: missing tools/audit/public-api-v0.txt"]
    expected = {
        line.strip()
        for line in snapshot.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    source = str(root / "src")
    sys.path.insert(0, source)
    try:
        module = importlib.import_module("pyowl_core")
        actual = set(module.__all__)
        missing_attributes = sorted(name for name in actual if not hasattr(module, name))
    except Exception as error:
        return [f"public-api: import failed: {error}"]
    finally:
        sys.path.remove(source)
    violations: list[str] = []
    if expected != actual:
        added = sorted(actual - expected)
        removed = sorted(expected - actual)
        violations.append(f"public-api: snapshot mismatch; added={added!r}, removed={removed!r}")
    if missing_attributes:
        violations.append(f"public-api: missing exported attributes: {missing_attributes!r}")
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(audit_public_api, argv)


if __name__ == "__main__":
    raise SystemExit(main())
