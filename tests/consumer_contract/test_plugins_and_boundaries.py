from __future__ import annotations

import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

import pyowl_core
from pyowl_core.adapters import discover_plugin_metadata

ROOT = Path(__file__).resolve().parents[2]


def test_metadata_discovery_never_loads_plugin_code(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = metadata.EntryPoint(
        name="fixture",
        value="definitely_absent_consumer_plugin:Factory",
        group="pyowl_core.views",
    )
    monkeypatch.setattr(metadata, "entry_points", lambda: metadata.EntryPoints((entry,)))

    records = discover_plugin_metadata("pyowl_core.views")

    assert [record.to_dict() for record in records] == [
        {
            "group": "pyowl_core.views",
            "name": "fixture",
            "value": "definitely_absent_consumer_plugin:Factory",
            "module": "definitely_absent_consumer_plugin",
            "attribute": "Factory",
            "distribution": None,
            "distribution_version": None,
        }
    ]
    assert "definitely_absent_consumer_plugin" not in sys.modules


def test_duplicate_plugin_names_fail_without_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = metadata.EntryPoints(
        (
            metadata.EntryPoint("same", "absent_one:Factory", "pyowl_core.parsers"),
            metadata.EntryPoint("same", "absent_two:Factory", "pyowl_core.parsers"),
        )
    )
    monkeypatch.setattr(metadata, "entry_points", lambda: entries)

    with pytest.raises(pyowl_core.AdapterCompatibilityError) as caught:
        discover_plugin_metadata("pyowl_core.parsers")
    assert caught.value.code == "ADAPTER_PLUGIN_COLLISION"
    assert "absent_one" not in sys.modules
    assert "absent_two" not in sys.modules


def test_adapter_import_has_no_discovery_consumer_native_java_or_io_side_effects() -> None:
    script = """
import importlib.metadata
import pathlib
import sys

def forbidden(*args, **kwargs):
    raise AssertionError("adapter import attempted discovery or filesystem I/O")

importlib.metadata.entry_points = forbidden
pathlib.Path.open = forbidden
import pyowl_core.adapters

for name in (
    "exact",
    "oaei_bioml_eval",
    "pyelk",
    "pyhermit",
    "pyowl2vec_star_projector",
    "jpype",
    "pyowl_core._native",
):
    assert name not in sys.modules, name
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    subprocess.run([sys.executable, "-c", script], check=True, env=environment)


def test_adapter_runtime_sources_have_no_consumer_private_java_or_pickle_imports() -> None:
    source = ROOT / "src" / "pyowl_core" / "adapters"
    text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(source.glob("*.py")))
    forbidden = (
        "import exact",
        "import oaei_bioml_eval",
        "import pyelk",
        "import pyhermit",
        "import pyowl2vec_star_projector",
        "pyowl_core._native",
        "pyowl_core.backends",
        "import jpype",
        "import pickle",
    )
    assert all(value not in text for value in forbidden)
