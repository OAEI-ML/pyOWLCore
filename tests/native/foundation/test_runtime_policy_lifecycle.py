from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import sysconfig
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[3]


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    python_path = [str(ROOT / "src"), str(ROOT)]
    inherited = environment.get("PYTHONPATH")
    if inherited:
        python_path.append(inherited)
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    return environment


def _run_isolated(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script],
        cwd=ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _subinterpreter_worker(statement: str, *, repetitions: int) -> str:
    return f"""
try:
    from concurrent import interpreters
except ImportError:
    import _xxsubinterpreters as interpreters

    def run_once(source):
        interpreter = interpreters.create()
        try:
            interpreters.run_string(interpreter, source)
        finally:
            interpreters.destroy(interpreter)
else:
    def run_once(source):
        interpreter = interpreters.create()
        try:
            interpreter.exec(source)
        finally:
            interpreter.close()

for _index in range({repetitions}):
    run_once({statement!r})
print("SUBINTERPRETER_LIFECYCLE_OK", flush=True)
"""


def _has_subinterpreter_api() -> bool:
    for name in ("concurrent.interpreters", "_xxsubinterpreters"):
        try:
            if importlib.util.find_spec(name) is not None:
                return True
        except (ImportError, ModuleNotFoundError):
            continue
    return False


@pytest.mark.parametrize(("current", "expected"), ((3, 4), (0, 0)))
def test_runtime_identity_prefers_public_interpreter_api(
    monkeypatch: pytest.MonkeyPatch,
    current: int,
    expected: int,
) -> None:
    from pyowl_core.backends import native

    public_api = SimpleNamespace(
        get_current=lambda: SimpleNamespace(id=current),
        get_main=lambda: SimpleNamespace(id=0),
    )
    original = importlib.import_module

    def selected(name: str, package: str | None = None) -> object:
        if name == "concurrent.interpreters":
            return public_api
        return original(name, package)

    monkeypatch.setattr(importlib, "import_module", selected)
    assert native._interpreter_id() == expected


def test_subinterpreter_repeatedly_selects_complete_python_fallback() -> None:
    if not _has_subinterpreter_api():
        pytest.skip("CPython runtime does not expose a subinterpreter test API")

    # Some older CPython patch releases abort while finalizing stdlib ``ssl`` in
    # a subinterpreter. Isolate that runtime defect before importing pyowl-core,
    # which exposes the HTTP resolver and therefore imports ``ssl``.
    preflight = _run_isolated(_subinterpreter_worker("import ssl", repetitions=1))
    if preflight.returncode != 0:
        pytest.skip(
            "this CPython build cannot safely finalize its own ssl module in a "
            "subinterpreter"
        )

    statement = r"""
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
    completed = _run_isolated(_subinterpreter_worker(statement, repetitions=8))

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "SUBINTERPRETER_LIFECYCLE_OK"


@pytest.mark.skipif(
    not bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
    reason="requires free-threaded CPython",
)
def test_free_threaded_runtime_selects_complete_python_fallback() -> None:
    script = r"""
import importlib
import sys
import warnings

from pyowl_core import BackendPreference, DocumentFormat, LoadOptions, parse_document
from pyowl_core.backends import native

original_import_module = importlib.import_module

def guarded_import(name, package=None):
    if name == "pyowl_core._native":
        raise AssertionError("free-threaded policy attempted to import the extension")
    return original_import_module(name, package)

importlib.import_module = guarded_import
probe = native.probe(refresh=True)
assert probe.available is False
assert probe.reason == "native extension is not approved for free-threaded CPython"
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
print("FREE_THREADED_FALLBACK_OK", flush=True)
"""
    completed = _run_isolated(script)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "FREE_THREADED_FALLBACK_OK"
