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
PLATFORM_AUDIT = (ROOT / "tools" / "packaging" / "platform_audit.py").read_text(
    encoding="utf-8"
)
ACTION = re.compile(r"(?m)^\s*-?\s*uses:\s+([^\s#]+)")


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
    for workflow in (CI, WHEELS, RELEASE, NATIVE_SAFETY):
        actions = ACTION.findall(workflow)
        assert actions
        for action in actions:
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", action), action


def test_inline_workflow_python_is_syntactically_valid() -> None:
    snippets = _inline_python(WHEELS) + _inline_python(RELEASE)
    assert len(snippets) >= 8
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
    assert "MACOSX_DEPLOYMENT_TARGET: \"13.0\"" in WHEELS


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
    assert NATIVE_SAFETY.count("timeout-minutes:") == 3
    assert "continue-on-error" not in NATIVE_SAFETY


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
        '.cibw-target-$PYOWL_CORE_BUILD_PASS',
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
    assert "wheels_run_id" in RELEASE
    assert "run-id: ${{ inputs.wheels_run_id }}" in RELEASE
    assert 'run["name"] == "Wheels"' in RELEASE
    assert 'run["conclusion"] == "success"' in RELEASE
    assert 'run["head_sha"] == os.environ["SOURCE_SHA"]' in RELEASE
    assert "git verify-tag" in RELEASE
    assert 'test "$source_sha" = "$GITHUB_SHA"' in RELEASE
    assert 'version="$(python -m tools.packaging.release_tag "$TAG")"' in RELEASE
    assert 'tag["verification"]["verified"] is True' in RELEASE
    assert 'report["release_ready"] is True' in RELEASE
    assert "sha256sum --check" in RELEASE
    assert "python -m build" not in RELEASE
    assert "packages-dir: candidate/dist/" in RELEASE


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
    assert 'assert actual == expected' in promotion_body
    assert 'candidate/SHA256SUMS candidate/dist/*' in promotion_body
    assert RELEASE[public_index:].count("pypi-attestations verify pypi") == 1


def test_checked_gate_manifest_records_real_blockers() -> None:
    path = ROOT / "reports" / "release" / "0.1.0.dev0" / "gates.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == 1
    gates = payload["gates"]
    assert len(gates) == 12
    assert {gate["status"] for gate in gates.values()} == {"blocked"}
    evidence = " ".join(gate["evidence"] for gate in gates.values())
    for phrase in ("PyPI", "legal", "reference-machine", "signed release tag"):
        assert phrase in evidence
