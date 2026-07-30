from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest

from tools.benchmark.comparators.dependency_audit import (
    SCHEMA,
    ArtifactExpectation,
    audit_dependency_exclusion,
    main,
)

_PYPROJECT = """\
[build-system]
requires = ["setuptools>=77", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "pyowl-core"
version = "0.1.0"
requires-python = ">=3.10"
license = "Apache-2.0"
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=8"]

[tool.setuptools]
package-dir = {"" = "src"}

[tool.setuptools.packages.find]
where = ["src"]
"""

_MANIFEST = """\
recursive-include src *.py *.pyi
recursive-include tools *.py
prune tools/benchmark/comparators
recursive-include benchmarks *.toml
prune benchmarks/comparators
recursive-include tests *.py
prune tests/benchmark/comparators
recursive-include reports *.md *.json *.txt
recursive-exclude reports/performance/redesign-baseline dependency-audit-*.json
"""

_METADATA = b"""Metadata-Version: 2.4
Name: pyowl-core
Version: 0.1.0
Requires-Python: >=3.10
License-Expression: Apache-2.0
Provides-Extra: dev
Requires-Dist: pytest>=8; extra == "dev"

fixture
"""

_LICENSE_FILES = {
    "LICENSE": b"Apache License 2.0",
    "NOTICE": b"pyowl-core",
    "THIRD_PARTY_LICENSES/LLVM-exception.txt": b"LLVM exception",
    "THIRD_PARTY_LICENSES/README.md": b"license inventory",
    "THIRD_PARTY_LICENSES/Unicode-3.0.txt": b"Unicode License v3",
    "THIRD_PARTY_LICENSES/W3C-RDF-tests-BSD-3-Clause.txt": b"W3C test license",
    "THIRD_PARTY_LICENSES/inventory.toml": b"schema = 1",
}


def test_clean_source_passes_while_missing_artifacts_are_not_run(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)

    first = audit_dependency_exclusion(root)
    second = audit_dependency_exclusion(root)

    assert first == second
    assert first["schema"] == SCHEMA
    assert first["status"] == "not-run"
    assert all(row["status"] == "pass" for row in _source_checks(first))
    assert _artifact_check(first) == {
        "status": "not-run",
        "reason": "no built wheel or sdist was supplied",
        "findings": [],
        "artifacts": [],
        "platform_linkage": {
            "status": "not-run",
            "reason": "static archive inspection does not perform a platform linkage audit",
        },
    }
    json.dumps(first, sort_keys=True)


def test_source_identity_has_a_reproducible_preimage_and_changes_with_inputs(
    tmp_path: Path,
) -> None:
    root = _clean_repository(tmp_path)

    first = _source_identity(audit_dependency_exclusion(root))
    rows = cast(list[dict[str, object]], first["inputs"])
    canonical_inputs = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    expected = hashlib.sha256(
        cast(str, first["domain"]).encode("utf-8") + b"\0" + canonical_inputs
    ).hexdigest()

    assert first["status"] == "pass"
    assert first["sha256"] == expected
    assert first["input_count"] == len(rows)
    assert first["input_bytes"] == sum(cast(int, row["bytes"]) for row in rows)
    pyproject = next(row for row in rows if row["path"] == "pyproject.toml")
    assert pyproject["checks"] == ["dependency-metadata", "package-payload-manifests"]

    (root / "src" / "pyowl_core" / "__init__.py").write_text(
        'VERSION = "changed"\n',
        encoding="utf-8",
    )
    changed = _source_identity(audit_dependency_exclusion(root))

    assert changed["sha256"] != first["sha256"]


def test_detached_evidence_is_not_a_source_identity_input(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    before = _source_identity(audit_dependency_exclusion(root))
    evidence = root / "reports" / "performance" / "redesign-baseline" / "dependency-audit-x.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text('{"self":"first"}\n', encoding="utf-8")

    first = _source_identity(audit_dependency_exclusion(root))
    evidence.write_text('{"self":"changed"}\n', encoding="utf-8")
    second = _source_identity(audit_dependency_exclusion(root))

    assert first["sha256"] == before["sha256"] == second["sha256"]
    paths = {row["path"] for row in cast(list[dict[str, object]], first["inputs"])}
    assert evidence.relative_to(root).as_posix() not in paths


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is unavailable")
def test_source_identity_records_git_revision_without_evidence_self_reference(
    tmp_path: Path,
) -> None:
    root = _clean_repository(tmp_path)
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "Dependency Audit Test")
    _git(root, "config", "user.email", "audit@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", ".")
    _git(root, "commit", "--quiet", "-m", "fixture")

    clean = cast(dict[str, object], _source_identity(audit_dependency_exclusion(root))["git"])
    revision = cast(str, clean["revision"])
    assert clean == {
        "available": True,
        "revision": revision,
        "dirty": False,
        "dirty_scope": "repository-excluding-detached-audit-evidence",
        "inspected_inputs_dirty": False,
    }

    detached = root / "reports" / "performance" / "redesign-baseline" / "dependency-audit-x.json"
    detached.parent.mkdir(parents=True)
    detached.write_text("{}\n", encoding="utf-8")
    still_clean = cast(dict[str, object], _source_identity(audit_dependency_exclusion(root))["git"])
    assert still_clean["dirty"] is False
    assert still_clean["inspected_inputs_dirty"] is False

    uninspected_path = root / "README.md"
    uninspected_path.write_text("provenance-only change\n", encoding="utf-8")
    uninspected = cast(dict[str, object], _source_identity(audit_dependency_exclusion(root))["git"])
    assert uninspected["dirty"] is True
    assert uninspected["inspected_inputs_dirty"] is False
    uninspected_path.unlink()

    (root / "src" / "pyowl_core" / "__init__.py").write_text("changed = True\n", encoding="utf-8")
    dirty = cast(dict[str, object], _source_identity(audit_dependency_exclusion(root))["git"])
    assert dirty["revision"] == revision
    assert dirty["dirty"] is True
    assert dirty["inspected_inputs_dirty"] is True


def test_source_identity_rejects_a_source_symlink_that_escapes_the_root(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path / "source")
    outside = tmp_path / "outside.py"
    outside.write_text("SAFE = True\n", encoding="utf-8")
    (root / "src" / "pyowl_core" / "escaped.py").symlink_to(outside)

    report = audit_dependency_exclusion(root)
    identity = _source_identity(report)

    assert report["status"] == "fail"
    assert identity["status"] == "fail"
    assert identity["sha256"] is None
    assert any(
        "cannot bind input: ValueError" in row for row in cast(list[str], identity["findings"])
    )


def test_runtime_build_and_test_dependency_metadata_leaks_fail(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    (root / "pyproject.toml").write_text(
        _PYPROJECT.replace("dependencies = []", 'dependencies = ["py-horned-owl==1.4.0"]'),
        encoding="utf-8",
    )
    (root / "native" / "Cargo.toml").write_text(
        '[package]\nname = "fixture"\nversion = "0.0.0"\n'
        '[dependencies]\nowl-engine = { package = "horned-owl", version = "1.4.0" }\n',
        encoding="utf-8",
    )
    (root / "requirements-test.txt").write_text("JPype1>=1.5\n", encoding="utf-8")
    (root / "setup.cfg").write_text(
        "[options]\ninstall_requires =\n    owlapi>=5.5\n",
        encoding="utf-8",
    )

    check = _check(audit_dependency_exclusion(root), "dependency-metadata")

    assert check["status"] == "fail"
    findings = cast(list[str], check["findings"])
    assert any(
        "runtime:pyproject.toml: forbidden dependency py-horned-owl" in row for row in findings
    )
    assert any("native/Cargo.toml: forbidden dependency horned-owl" in row for row in findings)
    assert any("requirements-test.txt: forbidden dependency jpype1" in row for row in findings)
    assert any("setup.cfg: forbidden dependency owlapi" in row for row in findings)


def test_runtime_build_and_ordinary_test_source_imports_fail(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    (root / "src" / "pyowl_core" / "leak.py").write_text(
        "import py_horned_owl\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_leak.py").write_text(
        "from jpype import startJVM\n",
        encoding="utf-8",
    )
    (root / "pyowl_build.py").write_text(
        'import subprocess\nsubprocess.run(["java", "-version"], check=True)\n',
        encoding="utf-8",
    )

    check = _check(audit_dependency_exclusion(root), "source-imports")

    assert check["status"] == "fail"
    findings = cast(list[str], check["findings"])
    assert any(
        "runtime:src/pyowl_core/leak.py: forbidden import py_horned_owl" in row for row in findings
    )
    assert any(
        "ordinary-test:tests/test_leak.py: forbidden import jpype" in row for row in findings
    )
    assert any("build:pyowl_build.py: forbidden Java command" in row for row in findings)


def test_source_scan_resolves_simple_importlib_and_subprocess_aliases(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    (root / "src" / "pyowl_core" / "aliases.py").write_text(
        "from importlib import import_module as load\n"
        "import subprocess as sp\n"
        'load("py_horned_owl")\n'
        'sp.run(["java", "-version"])\n',
        encoding="utf-8",
    )

    check = _check(audit_dependency_exclusion(root), "source-imports")
    findings = cast(list[str], check["findings"])

    assert check["status"] == "fail"
    assert "source:runtime:src/pyowl_core/aliases.py: forbidden import py_horned_owl" in findings
    assert "source:runtime:src/pyowl_core/aliases.py: forbidden Java command" in findings


def test_source_scan_fails_closed_on_python_parse_errors(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    (root / "src" / "pyowl_core" / "broken.py").write_text(
        "def broken(:\n",
        encoding="utf-8",
    )

    check = _check(audit_dependency_exclusion(root), "source-imports")

    assert check["status"] == "fail"
    assert check["findings"] == [
        "source:runtime:src/pyowl_core/broken.py: cannot parse Python source"
    ]


def test_comparator_adapter_sources_are_allowed_when_payload_is_excluded(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    comparator = root / "tools" / "benchmark" / "comparators"
    comparator.mkdir(parents=True)
    (comparator / "horned_runner.py").write_text("import py_horned_owl\n", encoding="utf-8")

    report = audit_dependency_exclusion(root)

    assert all(row["status"] == "pass" for row in _source_checks(report))
    assert report["status"] == "not-run"


def test_payload_manifest_must_exclude_each_comparator_path_after_broad_include(
    tmp_path: Path,
) -> None:
    root = _clean_repository(tmp_path)
    (root / "MANIFEST.in").write_text(
        _MANIFEST.replace("prune tools/benchmark/comparators\n", ""),
        encoding="utf-8",
    )

    check = _check(audit_dependency_exclusion(root), "package-payload-manifests")

    assert check["status"] == "fail"
    assert check["findings"] == [
        "payload: MANIFEST.in does not exclude comparator path tools/benchmark/comparators"
    ]


def test_payload_manifest_must_detach_audit_json_after_retaining_other_reports(
    tmp_path: Path,
) -> None:
    root = _clean_repository(tmp_path)
    (root / "MANIFEST.in").write_text(
        _MANIFEST.replace(
            "recursive-exclude reports/performance/redesign-baseline dependency-audit-*.json\n",
            "",
        ),
        encoding="utf-8",
    )

    check = _check(audit_dependency_exclusion(root), "package-payload-manifests")

    assert check["status"] == "fail"
    assert check["findings"] == ["payload: MANIFEST.in does not detach dependency-audit evidence"]


def test_repository_manifest_retains_reports_but_detaches_audit_json() -> None:
    manifest = (Path(__file__).parents[3] / "MANIFEST.in").read_text(encoding="utf-8")

    assert "recursive-include reports *.md *.json *.txt\n" in manifest
    assert (
        "recursive-exclude reports/performance/redesign-baseline dependency-audit-*.json\n"
    ) in manifest


@pytest.mark.parametrize("kind", ("wheel", "sdist"))
def test_supplied_clean_artifact_can_pass(kind: str, tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    artifact = _wheel(tmp_path, {}) if kind == "wheel" else _sdist(tmp_path, {})

    report = audit_dependency_exclusion(root, (artifact,))

    assert report["status"] == "pass"
    artifact_check = _artifact_check(report)
    assert artifact_check["status"] == "pass"
    rows = cast(list[dict[str, object]], artifact_check["artifacts"])
    assert rows[0]["kind"] == kind
    assert rows[0]["status"] == "pass"
    assert rows[0]["findings"] == []
    assert artifact_check["platform_linkage"] == {
        "status": "not-run",
        "reason": "static archive inspection does not perform a platform linkage audit",
    }


@pytest.mark.parametrize("member", ("WHEEL", "RECORD"))
def test_wheel_requires_complete_dist_info_structure(member: str, tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    dist_info = "pyowl_core-0.1.0.dist-info"
    artifact = _wheel(tmp_path, {}, omit=frozenset({f"{dist_info}/{member}"}))

    row = _artifact_rows(audit_dependency_exclusion(root, (artifact,)))[0]

    assert row["status"] == "fail"
    assert any(member in value for value in cast(list[str], row["findings"]))


def test_wheel_requires_matching_metadata_and_record_membership(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    dist_info = "pyowl_core-0.1.0.dist-info"
    wrong_metadata = _METADATA.replace(b"Name: pyowl-core", b"Name: unrelated")
    artifact = _wheel(tmp_path, {f"{dist_info}/METADATA": wrong_metadata})
    with zipfile.ZipFile(artifact, "a") as archive:
        archive.writestr("unrecorded.txt", b"not in RECORD")

    row = _artifact_rows(audit_dependency_exclusion(root, (artifact,)))[0]
    findings = cast(list[str], row["findings"])

    assert row["status"] == "fail"
    assert any("metadata: Name" in value for value in findings)
    assert any("RECORD member set" in value for value in findings)


def test_sdist_requires_matching_pyproject_identity(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    source_root = "pyowl_core-0.1.0"
    artifact = _sdist(
        tmp_path,
        {
            f"{source_root}/pyproject.toml": _PYPROJECT.replace(
                'name = "pyowl-core"', 'name = "unrelated"'
            ).encode()
        },
    )

    row = _artifact_rows(audit_dependency_exclusion(root, (artifact,)))[0]

    assert row["status"] == "fail"
    assert "sdist: pyproject identity differs from project source" in cast(
        list[str], row["findings"]
    )


@pytest.mark.parametrize(
    "relative",
    ("PKG-INFO", "pyproject.toml", "src/pyowl_core/__init__.py"),
)
def test_sdist_requires_identity_and_project_layout(relative: str, tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    source_root = "pyowl_core-0.1.0"
    artifact = _sdist(tmp_path, {}, omit=frozenset({f"{source_root}/{relative}"}))

    row = _artifact_rows(audit_dependency_exclusion(root, (artifact,)))[0]

    assert row["status"] == "fail"
    assert any(relative in value for value in cast(list[str], row["findings"]))


def test_expected_kind_and_sha256_are_bound_to_supplied_artifact(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    artifact = _wheel(tmp_path, {})
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    passing = audit_dependency_exclusion(
        root,
        (ArtifactExpectation(artifact, kind="wheel", sha256=digest),),
    )
    wrong_digest = audit_dependency_exclusion(
        root,
        (ArtifactExpectation(artifact, kind="wheel", sha256="0" * 64),),
    )
    wrong_kind = audit_dependency_exclusion(
        root,
        (ArtifactExpectation(artifact, kind="sdist", sha256=digest),),
    )

    assert passing["status"] == "pass"
    passing_row = _artifact_rows(passing)[0]
    assert passing_row["sha256_bound"] is True
    assert passing_row["expected_sha256"] == digest
    assert wrong_digest["status"] == "fail"
    assert wrong_kind["status"] == "fail"


def test_duplicate_artifact_paths_and_content_are_ambiguous(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    artifact = _wheel(tmp_path, {})

    report = audit_dependency_exclusion(root, (artifact, artifact))

    assert report["status"] == "fail"
    findings = cast(list[str], _artifact_check(report)["findings"])
    assert any("duplicate path" in value for value in findings)
    assert any("duplicate content SHA-256" in value for value in findings)


def test_duplicate_archive_members_fail_before_structure_validation(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    artifact = _wheel(tmp_path, {})
    with (
        pytest.warns(UserWarning, match="Duplicate name"),
        zipfile.ZipFile(artifact, "a") as archive,
    ):
        archive.writestr("pyowl_core/__init__.py", b"duplicate")

    row = _artifact_rows(audit_dependency_exclusion(root, (artifact,)))[0]

    assert row["status"] == "fail"
    assert "artifact: duplicate archive member name" in cast(list[str], row["findings"])


def test_native_library_marker_scan_is_recorded_and_fail_closed(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    native_name = "pyowl_core/_native.cpython-310-x86_64-linux-gnu.so"
    clean = _wheel(tmp_path, {}, variant="native")
    clean_row = _artifact_rows(audit_dependency_exclusion(root, (clean,)))[0]
    assert clean_row["status"] == "pass"
    clean_evidence = cast(dict[str, Any], clean_row["dynamic_library_markers"])
    assert clean_evidence["status"] == "pass"
    assert cast(list[dict[str, Any]], clean_evidence["libraries"])[0]["markers"] == []

    marked = _wheel(
        tmp_path,
        {native_name: b"prefix JNI_CreateJavaVM suffix"},
        variant="native",
    )
    marked_row = _artifact_rows(audit_dependency_exclusion(root, (marked,)))[0]
    marked_evidence = cast(dict[str, Any], marked_row["dynamic_library_markers"])

    assert marked_row["status"] == "fail"
    assert marked_evidence["status"] == "fail"
    assert cast(list[dict[str, Any]], marked_evidence["libraries"])[0]["markers"] == [
        "JNI_CreateJavaVM"
    ]


def test_artifacts_reject_java_members_and_comparator_payloads(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    artifact = _wheel(
        tmp_path,
        {
            "vendor/owlapi.jar": b"jar",
            "vendor/Ontology.class": b"class",
            "tools/benchmark/comparators/runner.py": b"pass\n",
        },
    )

    row = _artifact_rows(audit_dependency_exclusion(root, (artifact,)))[0]

    assert row["status"] == "fail"
    findings = cast(list[str], row["findings"])
    assert "artifact: forbidden Java member vendor/Ontology.class" in findings
    assert "artifact: forbidden Java member vendor/owlapi.jar" in findings
    assert "artifact: forbidden comparator path tools/benchmark/comparators/runner.py" in findings


@pytest.mark.parametrize("kind", ("wheel", "sdist"))
def test_artifacts_scan_packaged_python_and_stub_payloads(kind: str, tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    package = "pyowl_core" if kind == "wheel" else "pyowl_core-0.1.0/src/pyowl_core"
    additions = {
        f"{package}/dynamic.py": (
            b'from importlib import import_module as load\nload("py_horned_owl")\n'
        ),
        f"{package}/runner.py": (b'import subprocess as sp\nsp.run(["java", "-version"])\n'),
        f"{package}/leak.pyi": b"import jpype\n",
    }
    artifact = _wheel(tmp_path, additions) if kind == "wheel" else _sdist(tmp_path, additions)

    row = _artifact_rows(audit_dependency_exclusion(root, (artifact,)))[0]
    findings = cast(list[str], row["findings"])

    assert row["status"] == "fail"
    assert any("dynamic.py: forbidden import py_horned_owl" in value for value in findings)
    assert any("runner.py: forbidden Java command" in value for value in findings)
    assert any("leak.pyi: forbidden import jpype" in value for value in findings)


def test_artifact_scan_does_not_treat_relative_local_modules_as_dependencies(
    tmp_path: Path,
) -> None:
    root = _clean_repository(tmp_path)
    artifact = _wheel(
        tmp_path,
        {"pyowl_core/audit.py": b"from .java import audit_java\n"},
    )

    row = _artifact_rows(audit_dependency_exclusion(root, (artifact,)))[0]

    assert row["status"] == "pass"
    assert row["findings"] == []


def test_artifact_python_scan_fails_closed_on_parse_and_size_limits(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    artifact = _wheel(
        tmp_path,
        {
            "pyowl_core/broken.py": b"def broken(:\n",
            "pyowl_core/oversized.py": b"#" + b"x" * (4 * 1024**2),
        },
    )

    row = _artifact_rows(audit_dependency_exclusion(root, (artifact,)))[0]
    findings = cast(list[str], row["findings"])

    assert row["status"] == "fail"
    assert any("broken.py: cannot parse Python source" in value for value in findings)
    assert (
        "artifact: Python source was not inspected within byte limits pyowl_core/oversized.py"
        in findings
    )


def test_sdist_rejects_comparator_path_and_locked_horned_dependency(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    artifact = _sdist(
        tmp_path,
        {
            "pyowl_core-0.1.0/benchmarks/comparators/comparators.toml": b"schema = 1\n",
            "pyowl_core-0.1.0/native/Cargo.lock": (
                b'version = 4\n[[package]]\nname = "horned-owl"\nversion = "1.4.0"\n'
            ),
        },
    )

    row = _artifact_rows(audit_dependency_exclusion(root, (artifact,)))[0]

    assert row["status"] == "fail"
    findings = cast(list[str], row["findings"])
    assert any("forbidden comparator path" in value for value in findings)
    assert any("forbidden dependency horned-owl" in value for value in findings)


def test_wheel_metadata_rejects_py_horned_runtime_dependency(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path)
    artifact = _wheel(
        tmp_path,
        {
            "pyowl_core-0.1.0.dist-info/METADATA": (
                b"Metadata-Version: 2.4\nName: pyowl-core\nRequires-Dist: py-horned-owl==1.4.0\n"
            )
        },
    )

    row = _artifact_rows(audit_dependency_exclusion(root, (artifact,)))[0]

    assert row["status"] == "fail"
    findings = cast(list[str], row["findings"])
    assert any("forbidden dependency py-horned-owl" in value for value in findings)


def test_cli_requires_explicit_partial_opt_in_for_not_run_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _clean_repository(tmp_path)

    assert main(("--root", str(root))) == 1
    first_output = capsys.readouterr().out
    assert main(("--root", str(root), "--allow-partial")) == 0

    output = capsys.readouterr().out
    payload = cast(dict[str, Any], json.loads(output))
    assert payload["schema"] == SCHEMA
    assert payload["status"] == "not-run"
    assert output == json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    assert first_output == output


def test_cli_can_write_canonical_evidence(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path / "source")
    output = (
        root / "reports" / "performance" / "redesign-baseline" / "dependency-audit-fixture.json"
    )
    output.parent.mkdir(parents=True)

    assert main(("--root", str(root), "--allow-partial", "--output", str(output))) == 0
    payload = cast(dict[str, Any], json.loads(output.read_text(encoding="utf-8")))
    first = output.read_bytes()
    assert main(("--root", str(root), "--allow-partial", "--output", str(output))) == 0

    assert payload["schema"] == SCHEMA
    assert output.read_bytes() == first
    assert output.read_text(encoding="utf-8") == (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    )


def _clean_repository(root: Path) -> Path:
    (root / "src" / "pyowl_core").mkdir(parents=True)
    (root / "native" / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (root / "MANIFEST.in").write_text(_MANIFEST, encoding="utf-8")
    (root / "setup.py").write_text("from setuptools import setup\nsetup()\n", encoding="utf-8")
    (root / "pyowl_build.py").write_text("BUILD_NATIVE = False\n", encoding="utf-8")
    (root / "src" / "pyowl_core" / "__init__.py").write_text("", encoding="utf-8")
    (root / "native" / "Cargo.toml").write_text(
        '[package]\nname = "fixture"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )
    (root / "native" / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
    (root / "native" / "src" / "lib.rs").write_text("", encoding="utf-8")
    (root / "tests" / "test_smoke.py").write_text("import pyowl_core\n", encoding="utf-8")
    return root


def _record(entries: dict[str, bytes], record_name: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, payload in entries.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
        writer.writerow((name, "sha256=" + digest.decode("ascii"), len(payload)))
    writer.writerow((record_name, "", ""))
    return output.getvalue().encode("utf-8")


def _wheel(
    root: Path,
    additions: dict[str, bytes],
    *,
    variant: str = "pure",
    omit: frozenset[str] = frozenset(),
) -> Path:
    dist_info = "pyowl_core-0.1.0.dist-info"
    tag = "py3-none-any" if variant == "pure" else "cp310-cp310-manylinux_2_28_x86_64"
    entries = {
        "pyowl_core/__init__.py": b'__version__ = "0.1.0"\n',
        f"{dist_info}/METADATA": _METADATA,
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: pyowl-core-test-fixture\n"
            f"Root-Is-Purelib: {'true' if variant == 'pure' else 'false'}\n"
            f"Tag: {tag}\n"
        ).encode(),
    }
    for name, payload in _LICENSE_FILES.items():
        entries[f"{dist_info}/licenses/{name}"] = payload
    if variant == "native":
        entries["pyowl_core/_native.cpython-310-x86_64-linux-gnu.so"] = b"native-fixture"
    entries.update(additions)
    for name in omit:
        entries.pop(name, None)
    record_name = f"{dist_info}/RECORD"
    if record_name not in omit:
        entries[record_name] = _record(entries, record_name)
    path = root / f"pyowl_core-0.1.0-{tag}.whl"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(2025, 1, 1, 0, 0, 0))
            info.external_attr = (0o100755 if name.endswith(".so") else 0o100644) << 16
            archive.writestr(info, payload)
    return path


def _sdist(
    root: Path,
    additions: dict[str, bytes],
    *,
    omit: frozenset[str] = frozenset(),
) -> Path:
    source_root = "pyowl_core-0.1.0"
    entries = {
        f"{source_root}/PKG-INFO": _METADATA,
        f"{source_root}/pyproject.toml": _PYPROJECT.encode(),
        f"{source_root}/setup.py": b"from setuptools import setup\nsetup()\n",
        f"{source_root}/src/pyowl_core/__init__.py": b'__version__ = "0.1.0"\n',
        f"{source_root}/native/Cargo.lock": b"version = 4\n",
        f"{source_root}/native/Cargo.toml": (b'[package]\nname = "fixture"\nversion = "0.0.0"\n'),
        f"{source_root}/native/src/lib.rs": b"",
    }
    for name, payload in _LICENSE_FILES.items():
        entries[f"{source_root}/{name}"] = payload
    entries.update(additions)
    for name in omit:
        entries.pop(name, None)
    path = root / "pyowl_core-0.1.0.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(payload))
    return path


def _source_checks(report: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], report["source_checks"])


def _source_identity(report: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], report["source_identity"])


def _check(report: dict[str, object], identifier: str) -> dict[str, object]:
    return next(row for row in _source_checks(report) if row["id"] == identifier)


def _artifact_check(report: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], report["artifact_check"])


def _artifact_rows(report: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], _artifact_check(report)["artifacts"])


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        capture_output=True,
        check=True,
    )
