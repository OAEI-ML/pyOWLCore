"""Prove that importing pyowl_core is quiet, lazy, and side-effect free."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import warnings
from pathlib import Path
from typing import Any

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_BLOCKED_EVENT_PREFIXES = ("socket.", "subprocess.")
_BLOCKED_EVENTS = {"os.posix_spawn", "os.spawn", "os.system"}
_FORBIDDEN_MODULE_PREFIXES = ("deeponto", "jpype", "mowl")


def run_import_probe(package: str = "pyowl_core") -> dict[str, Any]:
    """Import once under an audit hook and return deterministic findings."""

    violations: list[str] = []
    observed: list[str] = []
    probe_active = True

    def audit(event: str, args: tuple[object, ...]) -> None:
        if not probe_active:
            return
        if event.startswith(_BLOCKED_EVENT_PREFIXES) or event in _BLOCKED_EVENTS:
            observed.append(event)
            raise RuntimeError(f"blocked import side effect: {event}")
        if event == "open" and len(args) >= 3:
            flags = args[2]
            if isinstance(flags, int) and flags & _WRITE_FLAGS:
                path = os.fspath(args[0]) if isinstance(args[0], (str, bytes, os.PathLike)) else "?"
                observed.append(f"open-write:{path!r}")
                raise RuntimeError(f"blocked import filesystem write: {path!r}")

    sys.dont_write_bytecode = True
    sys.addaudithook(audit)
    before = set(sys.modules)
    caught: list[warnings.WarningMessage]
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            module = importlib.import_module(package)
    except Exception as error:
        violations.append(f"import failed: {type(error).__name__}: {error}")
        module = None
        caught = []
    finally:
        probe_active = False
    for warning in caught:
        violations.append(f"import warning: {warning.category.__name__}: {warning.message}")
    imported = sorted(set(sys.modules) - before)
    for name in imported:
        if name == f"{package}._native":
            violations.append("import eagerly loaded the optional native extension")
        if name.casefold().startswith(_FORBIDDEN_MODULE_PREFIXES):
            violations.append(f"import loaded forbidden integration module {name}")
    violations.extend(f"import attempted side effect {event}" for event in observed)
    return {
        "schema": 1,
        "package": package,
        "version": getattr(module, "__version__", None),
        "ok": not violations,
        "violations": sorted(set(violations)),
        "native_extension_loaded": f"{package}._native" in sys.modules,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", default="pyowl_core")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = run_import_probe(args.package)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return int(not report["ok"])


if __name__ == "__main__":
    raise SystemExit(main())
