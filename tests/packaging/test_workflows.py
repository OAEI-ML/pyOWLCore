from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
WHEELS = (WORKFLOWS / "wheels.yml").read_text(encoding="utf-8")
RELEASE = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
CI = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
NATIVE_SAFETY = (WORKFLOWS / "native-safety.yml").read_text(encoding="utf-8")
NATIVE_PERFORMANCE = (WORKFLOWS / "native-performance.yml").read_text(encoding="utf-8")
PLATFORM_AUDIT = (ROOT / "tools" / "packaging" / "platform_audit.py").read_text(encoding="utf-8")
DIRECT_RUNNER_BUILD = (
    ROOT / "tools" / "benchmark" / "comparators" / "build_direct_runner.py"
).read_text(encoding="utf-8")
ACTION = re.compile(r"(?m)^\s*-?\s*uses:\s+([^\s#]+)")
CONTAINER_IMAGE = re.compile(r"(?m)^\s*container:\s+([^\s#]+)")


def _inline_python(workflow: str) -> tuple[str, ...]:
    lines = workflow.splitlines()
    snippets: list[str] = []
    position = 0
    while position < len(lines):
        line = lines[position]
        if "python" not in line or not line.rstrip().endswith("<<'PY'"):
            position += 1
            continue
        indentation = len(line) - len(line.lstrip())
        body: list[str] = []
        position += 1
        while position < len(lines) and lines[position].strip() != "PY":
            body.append(lines[position][indentation:])
            position += 1
        assert position < len(lines), "unterminated workflow Python heredoc"
        snippets.append("\n".join(body) + "\n")
        position += 1
    return tuple(snippets)


def test_every_external_action_is_pinned_to_a_full_commit() -> None:
    for workflow in (CI, WHEELS, RELEASE, NATIVE_SAFETY, NATIVE_PERFORMANCE):
        actions = ACTION.findall(workflow)
        assert actions
        for action in actions:
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", action), action


def test_ci_container_images_are_pinned_to_exact_manifests() -> None:
    assert CONTAINER_IMAGE.findall(CI) == [
        "python:3.10-slim@sha256:e8d6cdadc17ce7146e1bb286e6093d58c8cf582659a558ad51cd103829655e72"
    ]


def test_inline_workflow_python_is_syntactically_valid() -> None:
    snippets = (
        _inline_python(WHEELS)
        + _inline_python(RELEASE)
        + _inline_python(NATIVE_SAFETY)
        + _inline_python(NATIVE_PERFORMANCE)
    )
    assert len(snippets) >= 9
    for index, snippet in enumerate(snippets):
        compile(snippet, f"workflow-inline-{index}.py", "exec")


def test_wheel_matrix_covers_supported_runtime_and_platforms() -> None:
    for version in ("3.10", "3.11", "3.12", "3.13", "3.14", "pypy3.10"):
        assert version in WHEELS
    for runner in (
        "ubuntu-24.04",
        "ubuntu-24.04-arm",
        "macos-15-intel",
        "macos-15",
        "windows-latest",
    ):
        assert runner in WHEELS
    assert "rustup toolchain install 1.83.0" in WHEELS
    assert "--default-toolchain 1.83.0" in WHEELS
    assert "pypa/cibuildwheel@294735312765b09d24a2fbec22660ce817587d55" in WHEELS
    assert 'MACOSX_DEPLOYMENT_TARGET: "13.0"' in WHEELS


def test_native_safety_workflow_is_pinned_bounded_and_fail_closed() -> None:
    for requirement in (
        "nightly-2026-07-14",
        "sanitizer: [address, thread]",
        "-Zsanitizer=${{ matrix.sanitizer }}",
        "--component rust-src",
        "test -Zbuild-std",
        "--target x86_64-unknown-linux-gnu",
        "--component miri",
        "tests/miri/native/Cargo.toml --locked",
        "process-allocator:",
        "--features process-allocator-test",
        "--test process_allocator_failure",
        'python: "3.14t"',
        "pytest==9.1.1",
        "--features test-hooks",
        "test_allocation_failure.py",
        "test_runtime_policy_lifecycle.py",
        "test_process_lifecycle.py",
        "test_rust_process_lifecycle.py",
        "PYOWL_CORE_TEST_HOOKS_REQUIRED",
        "-k free_threaded",
        "cargo-fuzz --version 0.12.0 --locked",
        "--sanitizer address functional",
        "--sanitizer address wire",
        "-max_total_time=60",
        "-timeout=10",
        "-rss_limit_mb=2048",
        "if: failure()",
        "tests/fuzz/native/artifacts/",
    ):
        assert requirement in NATIVE_SAFETY
    assert NATIVE_SAFETY.count("timeout-minutes:") == 6
    assert "continue-on-error" not in NATIVE_SAFETY
    linkage_start = NATIVE_SAFETY.index("\n  direct-runner-linkage:")
    linkage_end = NATIVE_SAFETY.index("\n  fuzz:", linkage_start)
    linkage = NATIVE_SAFETY[linkage_start:linkage_end]
    for requirement in (
        "ubuntu-24.04",
        "macos-15-intel",
        "windows-latest",
        'python-version: "3.12.3"',
        "rustup toolchain install 1.97.1",
        "python -m tools.benchmark.comparators.build_direct_runner",
        "tools.benchmark.comparators.linkage_audit",
        "--expected-runner-sha256",
        "dumpbin.exe",
        "if: always()",
        "if-no-files-found: error",
    ):
        assert requirement in linkage
    assert linkage.count("timeout-minutes:") == 1
    assert "cargo +1.97.1 build --locked --release" not in linkage
    assert "PYO3_PYTHON" not in linkage
    assert "--allow-partial" not in linkage
    for requirement in (
        "CARGO_ENCODED_RUSTFLAGS",
        "--remap-path-prefix=",
        "link-arg=-Wl,-no_uuid",
        "RUSTC_WRAPPER",
        "reproducible_rustc.py",
    ):
        assert requirement in DIRECT_RUNNER_BUILD


def test_wheel_workflow_is_build_once_fail_closed_and_audited() -> None:
    for requirement in (
        "cmp candidate/direct/*.whl candidate/rebuilt/*.whl",
        "assert len(native) == 25",
        "assert len(tuple(output.iterdir())) == 27",
        "tools.packaging.artifact_inspector",
        "tools.packaging.import_probe",
        "tools.packaging.supply_chain",
        "tools.packaging.release_report",
        "tools.packaging.platform_audit audit",
        "tools.packaging.platform_audit verify-set",
        "cargo audit --deny warnings",
        "cmp candidate/sdist-a/*.tar.gz candidate/sdist-b/*.tar.gz",
        "assert first == second",
        ".cibw-target-$PYOWL_CORE_BUILD_PASS",
        "PYOWL_CORE_BUILD_PASS: candidate",
        "PYOWL_CORE_BUILD_PASS: rebuild",
        "python -m pytest -q -p no:cacheprovider {project}/tests",
        "PYOWL_CORE_TEST_NATIVE_LIBRARY=1",
        "CIBW_TEST_ENVIRONMENT",
        "assert len(results) == 29",
        "cp314t-pure-fallback",
        "pypy310-pure",
        "--no-index",
        "*-py3-none-any.whl",
    ):
        assert requirement in WHEELS
    assert "PYOWL_CORE_BUILD_NATIVE=0" in WHEELS
    assert "PYOWL_CORE_BUILD_NATIVE=1" in WHEELS
    assert "abi3" not in WHEELS.casefold()
    assert "free-thread" not in WHEELS.casefold()
    for command in (
        '("auditwheel", "show"',
        '("delocate-listdeps", "--all"',
        '("delvewheel", "show"',
        '("readelf", "-d"',
        '("otool", "-L"',
        '("dumpbin", "/DEPENDENTS"',
    ):
        assert command in PLATFORM_AUDIT


def test_release_consumes_verified_artifacts_and_never_rebuilds() -> None:
    assert 'test "$GITHUB_REPOSITORY" = "OAEI-ML/pyOWLCore"' in RELEASE
    assert "wheels_run_id" in RELEASE
    assert "performance_run_id" in RELEASE
    assert "run-id: ${{ inputs.wheels_run_id }}" in RELEASE
    assert "run-id: ${{ inputs.performance_run_id }}" in RELEASE
    assert 'run["name"] == "Wheels"' in RELEASE
    assert 'run["name"] == "Native performance"' in RELEASE
    assert 'run["conclusion"] == "success"' in RELEASE
    assert 'run["head_sha"] == os.environ["SOURCE_SHA"]' in RELEASE
    assert 'run["event"] == "workflow_dispatch"' in RELEASE
    assert 'run["path"] == ".github/workflows/native-performance.yml"' in RELEASE
    assert 'evidence["wheels_run_id"] == os.environ["WHEELS_RUN_ID"]' in RELEASE
    assert 'evidence["performance_run_id"] == os.environ["PERFORMANCE_RUN_ID"]' in RELEASE
    assert 'evidence["selected_wheel"]["sha256"] == selected["sha256"]' in RELEASE
    assert 'payload["gates"]["reference_performance"]' in RELEASE
    assert "git verify-tag" in RELEASE
    assert 'test "$source_sha" = "$GITHUB_SHA"' in RELEASE
    assert 'version="$(python -m tools.packaging.release_tag "$TAG")"' in RELEASE
    assert 'tag["verification"]["verified"] is True' in RELEASE
    assert 'report["release_ready"] is True' in RELEASE
    assert "sha256sum --check" in RELEASE
    assert "python -m build" not in RELEASE
    assert "packages-dir: candidate/dist/" in RELEASE
    performance_verification = RELEASE.index(
        "Verify authenticated reference-performance evidence"
    )
    performance_gate = RELEASE.index('payload["gates"]["reference_performance"]')
    regenerated_report = RELEASE.index(
        "python -m tools.packaging.release_report",
        performance_gate,
    )
    assert performance_verification < performance_gate < regenerated_report
    assert 'gates[name]["status"] == "blocked" for name in allowed_staged' in RELEASE
    assert "candidate/reference-performance" in RELEASE


def test_native_performance_is_guarded_complete_and_fail_closed() -> None:
    for requirement in (
        "workflow_dispatch:",
        "wheels_run_id:",
        "group: native-performance-${{ github.sha }}",
        "cancel-in-progress: false",
        "actions: read",
        "contents: read",
        "name: native-performance",
        "self-hosted",
        "shared-darwin25-x86_64",
        "timeout-minutes:",
        'run["name"] == "Wheels"',
        'run["conclusion"] == "success"',
        'run["head_sha"] == os.environ["GITHUB_SHA"]',
        'run["head_repository"]["full_name"] == os.environ["GITHUB_REPOSITORY"]',
        "release-candidate-${{ github.sha }}",
        "run-id: ${{ inputs.wheels_run_id }}",
        "pyowl_core-*-cp312-cp312-macosx_13_0_x86_64.whl",
        "python -m tools.benchmark.comparators.build_direct_runner",
        "oaei-bioml-doid-2024",
        "oaei-bioml-ncit-2024",
        "hpo-base-2026-06-23",
        "pyowl-python-common",
        "pyowl-native-wheel-common",
        "pyowl-direct-rust-common",
        "horned-owl-raw",
        "horned-owl-common",
        "py-horned-common",
        "owlapi-common",
        "PYOWL_CORE_PY_HORNED_VENV_BIN",
        (
            "PYOWL_CORE_PY_HORNED_RUNNER: ${{ github.workspace }}/"
            "benchmarks/comparators/runners/py_horned_common.py"
        ),
        'py_horned_runner.is_relative_to(Path.cwd().resolve())',
        "py_horned_runner.read_bytes()",
        'export PATH="$PY_HORNED_VENV_BIN:$PATH"',
        "--process-mode fresh-process",
        "--process-mode steady-process",
        "--input-mode resident-bytes",
        "--input-mode file",
        "--warmups 2",
        "--repetitions 20",
        "--seed 180643",
        'report["contract_valid"] is True',
        'report["comparative_complete"] is True',
        'completion["passed"] is True',
        'ratio_gates["configured"] is True',
        'ratio_gates["passed"] is True',
        'machine["approval"] == "approved"',
        'machine_evidence["matches"] is True',
        'environment["git_commit"] == os.environ["GITHUB_SHA"]',
        'environment["git_dirty"] is False',
        '"schema": "pyowl-core/native-performance-evidence/v1"',
        '"source_revision": os.environ["GITHUB_SHA"]',
        '"wheels_run_id": os.environ["WHEELS_RUN_ID"]',
        '"performance_run_id": os.environ["GITHUB_RUN_ID"]',
        "native-performance-${{ github.sha }}",
        "if-no-files-found: error",
    ):
        assert requirement in NATIVE_PERFORMANCE
    assert "\n  push:" not in NATIVE_PERFORMANCE
    assert "\n  pull_request:" not in NATIVE_PERFORMANCE
    assert "\n  schedule:" not in NATIVE_PERFORMANCE
    assert "--allow-partial" not in NATIVE_PERFORMANCE
    assert "continue-on-error" not in NATIVE_PERFORMANCE
    assert "${{ vars.PYOWL_CORE_PY_HORNED_RUNNER }}" not in NATIVE_PERFORMANCE


def test_release_uses_protected_oidc_environments_without_tokens() -> None:
    assert "name: testpypi" in RELEASE
    assert "name: pypi" in RELEASE
    assert RELEASE.count("id-token: write") == 2
    assert RELEASE.count("pypa/gh-action-pypi-publish@") == 2
    assert "repository-url: https://test.pypi.org/legacy/" in RELEASE
    assert "attestations: true" in RELEASE
    assert "secrets." not in RELEASE
    assert "api-token" not in RELEASE


def test_release_signs_final_report_and_verifies_index_attestations() -> None:
    for requirement in (
        "pypi-attestations==0.0.29",
        "pypi-attestations verify pypi",
        "testpypi-provenance",
        "hash/PEP 740 verified all 27 TestPyPI files",
        "actions/attest-build-provenance@0f67c3f4856b2e3261c31976d6725780e5e4c373",
        "candidate/release-report.json",
        "gh attestation verify",
        "release-attestation.sigstore.json",
        "urljoin(index_url",
        "TestPyPI evidence did not converge",
        "PyPI evidence did not converge",
        "candidate/pypi-files.json",
        "candidate/pypi-attestation-verification.txt",
    ):
        assert requirement in RELEASE
    close_gate = RELEASE.index('gates["gates"]["signatures"]')
    pypi_verification = RELEASE.index("Verify every TestPyPI file hash and PEP 740")
    final_report = RELEASE.index("Close the rehearsal/signature gates")
    signing = RELEASE.index("Sign the final report and immutable distribution set")
    assert pypi_verification < final_report < close_gate < signing
    assert "signed source tag plus checksum-bound immutable candidate" not in RELEASE
    promotion = RELEASE.index("Reverify promotion-ready report and checksums")
    publish = RELEASE.index("Publish the identical files to PyPI")
    public_index = RELEASE.index("Verify public index hashes, provenance")
    assert promotion < publish < public_index
    promotion_body = RELEASE[promotion:publish]
    assert "assert actual == expected" in promotion_body
    assert "candidate/SHA256SUMS candidate/dist/*" in promotion_body
    assert RELEASE[public_index:].count("pypi-attestations verify pypi") == 1


def test_checked_gate_manifest_records_owner_authorized_closures() -> None:
    path = ROOT / "reports" / "release" / "0.1.0" / "gates.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == 1
    gates = payload["gates"]
    assert len(gates) == 12
    assert {gate["status"] for gate in gates.values()} == {"passed"}
    evidence = " ".join(gate["evidence"] for gate in gates.values())
    for phrase in ("PyPI", "legal", "DOID", "owner"):
        assert phrase in evidence
