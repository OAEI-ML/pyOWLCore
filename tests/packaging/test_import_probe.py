from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_package_import_has_no_write_network_process_warning_or_eager_native_side_effect() -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-m", "tools.packaging.import_probe"],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    assert report == {
        "native_extension_loaded": False,
        "ok": True,
        "package": "pyowl_core",
        "schema": 1,
        "version": "0.1.0.dev0",
        "violations": [],
    }
