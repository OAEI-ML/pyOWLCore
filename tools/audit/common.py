"""Shared audit helpers."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path

Audit = Callable[[Path], list[str]]


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def run_cli(audit: Audit, argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=repository_root())
    args = parser.parse_args(argv)
    violations = audit(args.root.resolve())
    for violation in violations:
        print(violation)
    return bool(violations)
