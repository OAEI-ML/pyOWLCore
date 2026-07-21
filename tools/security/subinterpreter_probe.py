"""Run the fail-closed native policy in disposable CPython subinterpreters."""

from __future__ import annotations

import argparse
import importlib
import json
from collections.abc import Sequence
from typing import Protocol, cast


class _LegacyInterpreters(Protocol):
    def create(self) -> int: ...

    def run_string(self, interpreter: int, statement: str) -> None: ...

    def destroy(self, interpreter: int) -> None: ...


class _Interpreter(Protocol):
    def exec(self, statement: str) -> None: ...

    def close(self) -> None: ...


class _PublicInterpreters(Protocol):
    def create(self) -> _Interpreter: ...

_FALLBACK_PROBE = r"""
import importlib
import sys
import warnings

from pyowl_core import BackendPreference, DocumentFormat, LoadOptions, parse_document
from pyowl_core.backends import native

original_import_module = importlib.import_module

def guarded_import(name, package=None):
    if name == "pyowl_core._native":
        raise AssertionError("subinterpreter policy attempted to import the extension")
    return original_import_module(name, package)

importlib.import_module = guarded_import
probe = native.probe(refresh=True)
assert probe.available is False
assert probe.reason == "native extension is not approved in subinterpreters"
assert "pyowl_core._native" not in sys.modules

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    document = parse_document(
        b"Ontology(Declaration(Class(<urn:lifecycle:C>)))",
        format=DocumentFormat.FUNCTIONAL,
        options=LoadOptions(backend=BackendPreference.AUTO),
    )
assert len(document.axioms) == 1
assert "pyowl_core._native" not in sys.modules
"""


def _run(statement: str, repetitions: int) -> str:
    try:
        selected = importlib.import_module("concurrent.interpreters")
    except ImportError:
        legacy = cast(
            _LegacyInterpreters,
            importlib.import_module("_xxsubinterpreters"),
        )

        for _index in range(repetitions):
            legacy_id = legacy.create()
            try:
                legacy.run_string(legacy_id, statement)
            finally:
                legacy.destroy(legacy_id)
        return "_xxsubinterpreters"

    interpreters = cast(_PublicInterpreters, selected)
    for _index in range(repetitions):
        public_interpreter = interpreters.create()
        try:
            public_interpreter.exec(statement)
        finally:
            public_interpreter.close()
    return "concurrent.interpreters"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=8)
    parser.add_argument("--preflight-ssl", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.repetitions < 1:
        parser.error("--repetitions must be positive")

    mode = "stdlib-ssl-preflight" if arguments.preflight_ssl else "python-fallback"
    statement = "import ssl" if arguments.preflight_ssl else _FALLBACK_PROBE
    api = _run(statement, arguments.repetitions)
    print(
        json.dumps(
            {
                "schema": "pyowl-core.subinterpreter-probe/1",
                "status": "passed",
                "mode": mode,
                "api": api,
                "interpreters_created": arguments.repetitions,
                "interpreters_destroyed": arguments.repetitions,
                "documents_parsed": 0 if arguments.preflight_ssl else arguments.repetitions,
                "native_extension_import_attempts": 0,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through subprocesses
    raise SystemExit(main())
