"""Check repository licensing and external test-fixture provenance."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from .common import run_cli

_IGNORED_FIXTURES = {"PROVENANCE.toml", "deviations.toml", "README.md", ".gitkeep"}


def audit_provenance(root: Path) -> list[str]:
    violations: list[str] = []
    for name in ("LICENSE", "NOTICE"):
        path = root / name
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            violations.append(f"provenance: missing or empty {name}")
    data = root / "tests" / "data"
    if not data.is_dir():
        return violations
    fixtures = [
        path.relative_to(data).as_posix()
        for path in sorted(data.rglob("*"))
        if path.is_file() and path.name not in _IGNORED_FIXTURES
    ]
    if not fixtures:
        return violations
    ledger = data / "PROVENANCE.toml"
    if not ledger.is_file():
        violations.append("provenance: tests/data/PROVENANCE.toml is required")
        return violations
    text = ledger.read_text(encoding="utf-8")
    recorded = set(re.findall(r"(?m)^\s*path\s*=\s*['\"]([^'\"]+)['\"]\s*$", text))
    for fixture in fixtures:
        if fixture not in recorded:
            violations.append(f"provenance: unrecorded fixture: tests/data/{fixture}")
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(audit_provenance, argv)


if __name__ == "__main__":
    raise SystemExit(main())
