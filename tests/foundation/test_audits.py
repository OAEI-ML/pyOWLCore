from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.audit.architecture import audit_architecture
from tools.audit.java import audit_java
from tools.audit.provenance import audit_provenance


class AuditFixtureTests(unittest.TestCase):
    def test_architecture_audit_catches_consumer_and_layer_imports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "src" / "pyowl_core" / "model"
            model.mkdir(parents=True)
            (model / "bad.py").write_text(
                "import exact_om\nfrom ..io import parser\n",
                encoding="utf-8",
            )
            violations = audit_architecture(root)
            self.assertTrue(any("reverse dependency" in item for item in violations))
            self.assertTrue(any("model layer" in item for item in violations))

    def test_java_audit_catches_artifacts_archives_and_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src" / "pyowl_core").mkdir(parents=True)
            (root / "pyproject.toml").write_text("[project]\ndependencies = []\n", encoding="utf-8")
            (root / "bad.jar").write_bytes(b"not a real jar")
            archive = root / "artifact.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("nested/Bad.class", b"bytecode")
                output.writestr(
                    "source/pyproject.toml",
                    '[project]\ndependencies = ["JPype1>=1"]\n',
                )
            violations = audit_java(root)
            self.assertTrue(any("dependency in archive" in item for item in violations))
            self.assertTrue(any("bad.jar" in item for item in violations))
            self.assertTrue(any("Bad.class" in item for item in violations))

    def test_provenance_audit_requires_every_external_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "LICENSE").write_text("license\n", encoding="utf-8")
            (root / "NOTICE").write_text("notice\n", encoding="utf-8")
            data = root / "tests" / "data"
            data.mkdir(parents=True)
            (data / "external.owl").write_text("Ontology()\n", encoding="utf-8")
            violations = audit_provenance(root)
            self.assertTrue(any("PROVENANCE.toml" in item for item in violations))
            (data / "PROVENANCE.toml").write_text(
                '[[artifact]]\npath = "external.owl"\n', encoding="utf-8"
            )
            self.assertEqual(audit_provenance(root), [])


if __name__ == "__main__":
    unittest.main()
