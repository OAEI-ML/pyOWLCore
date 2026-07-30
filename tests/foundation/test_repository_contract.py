from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.audit.check_all import audit_all

ROOT = Path(__file__).resolve().parents[2]


class RepositoryContractTests(unittest.TestCase):
    def test_repository_audits_pass(self) -> None:
        self.assertEqual(audit_all(ROOT), [])

    def test_import_is_silent_and_does_not_write_or_load_java_bridges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = """
import json
import pathlib
import sys
import warnings
before = sorted(path.name for path in pathlib.Path.cwd().iterdir())
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    import pyowl_core
after = sorted(path.name for path in pathlib.Path.cwd().iterdir())
print(json.dumps({
    "before": before,
    "after": after,
    "warnings": [str(item.message) for item in caught],
    "forbidden": sorted(
        name
        for name in sys.modules
        if name.split(".")[0] in {"jpype", "deeponto", "mowl"}
    ),
    "version": pyowl_core.__version__,
}))
"""
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(ROOT / "src")
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=directory,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            evidence = json.loads(result.stdout)
            self.assertEqual(evidence["before"], evidence["after"])
            self.assertEqual(evidence["warnings"], [])
            self.assertEqual(evidence["forbidden"], [])
            self.assertEqual(evidence["version"], "0.1.1")

    def test_foundation_values_are_hash_seed_deterministic(self) -> None:
        script = """
import json
from pyowl_core import Diagnostic, LoadOptions, Severity
diagnostic = Diagnostic(
    code="TEST",
    severity=Severity.INFO,
    message="ok",
    details={"z": 1, "a": 2},
)
options = LoadOptions()
payload = {
    "diagnostic": diagnostic.to_dict(),
    "options": [options.imports.value, options.backend.value, options.offline],
}
print(json.dumps(payload, sort_keys=True))
"""
        outputs: list[str] = []
        for seed in ("1", "987654"):
            environment = dict(os.environ)
            environment.update(
                PYTHONPATH=str(ROOT / "src"),
                PYTHONDONTWRITEBYTECODE="1",
                PYTHONHASHSEED=seed,
            )
            outputs.append(
                subprocess.run(
                    [sys.executable, "-c", script],
                    cwd=ROOT,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )
        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
