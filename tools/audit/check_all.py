"""Run every WP00 repository audit."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .architecture import audit_architecture
from .common import repository_root
from .java import audit_java
from .metadata import audit_metadata
from .provenance import audit_provenance
from .public_api import audit_public_api


def audit_all(root: Path) -> list[str]:
    violations: list[str] = []
    for audit in (
        audit_architecture,
        audit_java,
        audit_metadata,
        audit_provenance,
        audit_public_api,
    ):
        violations.extend(audit(root))
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    if argv and len(argv) > 1:
        raise SystemExit("usage: python -m tools.audit.check_all [ROOT]")
    root = Path(argv[0]).resolve() if argv else repository_root()
    violations = audit_all(root)
    for violation in violations:
        print(violation)
    if violations:
        print(f"audit failed: {len(violations)} violation(s)")
        return 1
    print("all repository audits passed")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
