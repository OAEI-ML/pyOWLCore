"""Offline local checks; add ``--full`` for installed lint/type tools."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str]) -> int:
    print("+", " ".join(command), flush=True)
    environment = dict(os.environ)
    source = str(ROOT / "src")
    current_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = source if not current_path else source + os.pathsep + current_path
    return subprocess.run(command, cwd=ROOT, env=environment, check=False).returncode


def _syntax_check() -> int:
    failures = 0
    for base in (ROOT / "src", ROOT / "tools", ROOT / "tests"):
        for path in sorted(base.rglob("*.py")):
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError) as error:
                print(f"syntax: {path.relative_to(ROOT)}: {error}")
                failures += 1
    return int(bool(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    environment_checks = [
        [sys.executable, "-m", "tools.audit.check_all"],
        [sys.executable, "-m", "unittest", "discover", "-s", "tests/foundation", "-v"],
    ]
    status = _syntax_check()
    for command in environment_checks:
        status |= _run(command)
    if args.full:
        for module, arguments in (
            ("ruff", ["check", "."]),
            ("mypy", ["src", "tools"]),
            ("pytest", ["-q"]),
        ):
            if importlib.util.find_spec(module) is None:
                print(f"full check requires missing module: {module}")
                status = 1
            else:
                status |= _run([sys.executable, "-m", module, *arguments])
    return status


if __name__ == "__main__":
    raise SystemExit(main())
