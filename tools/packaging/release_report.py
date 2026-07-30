"""Generate a checksum-bound release decision report from inspected artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .artifact_inspector import InspectionResult, inspect_artifact
from .platform_audit import APPROVED_LANES

GateStatus = Literal["passed", "blocked", "failed"]
SUPPORTED_CPYTHON_TAGS = ("cp310", "cp311", "cp312", "cp313", "cp314")
REQUIRED_RELEASE_GATES = (
    "advisory_scan",
    "consumer_matrix",
    "legal_review",
    "name_control",
    "platform_artifact_audit",
    "project_urls",
    "reference_performance",
    "release_owner_approval",
    "signatures",
    "source_tag_verified",
    "testpypi_rehearsal",
    "trusted_publishing",
)


@dataclass(frozen=True, slots=True)
class ReleaseGate:
    """One externally evidenced release decision."""

    status: GateStatus
    evidence: str


def parse_gate(value: str) -> tuple[str, ReleaseGate]:
    """Parse ``NAME=STATUS:EVIDENCE`` without accepting empty evidence."""

    try:
        name, decision = value.split("=", 1)
        status, evidence = decision.split(":", 1)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "gate must use NAME=passed|blocked|failed:EVIDENCE"
        ) from error
    if name not in REQUIRED_RELEASE_GATES:
        raise argparse.ArgumentTypeError(f"unknown release gate {name!r}")
    if status not in {"passed", "blocked", "failed"}:
        raise argparse.ArgumentTypeError(f"invalid gate status {status!r}")
    if not evidence.strip():
        raise argparse.ArgumentTypeError("gate evidence must not be empty")
    return name, ReleaseGate(status=status, evidence=evidence.strip())  # type: ignore[arg-type]


def load_gate_file(path: Path) -> dict[str, ReleaseGate]:
    """Load an auditable JSON gate manifest without accepting unknown fields."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid release gate JSON: {error}") from error
    if not isinstance(payload, dict) or set(payload) != {"schema", "gates"}:
        raise ValueError("release gate file must contain exactly schema and gates")
    if payload["schema"] != 1 or not isinstance(payload["gates"], dict):
        raise ValueError("release gate file must use schema 1 and an object of gates")
    rendered: dict[str, ReleaseGate] = {}
    for name, value in payload["gates"].items():
        if name not in REQUIRED_RELEASE_GATES:
            raise ValueError(f"unknown release gate {name!r}")
        if not isinstance(value, dict) or set(value) != {"status", "evidence"}:
            raise ValueError(f"release gate {name!r} must contain status and evidence")
        status = value["status"]
        evidence = value["evidence"]
        if status not in {"passed", "blocked", "failed"}:
            raise ValueError(f"invalid status for release gate {name!r}")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError(f"release gate {name!r} has no evidence")
        rendered[name] = ReleaseGate(status=status, evidence=evidence.strip())
    return rendered


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_paths(directory: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for path in directory.iterdir():
        if path.suffix != ".whl" and not path.name.endswith(".tar.gz"):
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"artifact path must be a regular non-symlink file: {path.name}")
        paths.append(path)
    return tuple(sorted(paths, key=lambda path: path.name))


def expected_artifact_filenames(version: str) -> frozenset[str]:
    """Return the exact pure, native, and source artifact matrix for a release."""

    native = {
        f"pyowl_core-{version}-{python_tag}-{python_tag}-{platform_tag}.whl"
        for python_tag in SUPPORTED_CPYTHON_TAGS
        for _, _, platform_tag in APPROVED_LANES.values()
    }
    return frozenset(
        {
            f"pyowl_core-{version}-py3-none-any.whl",
            f"pyowl_core-{version}.tar.gz",
            *native,
        }
    )


def build_release_report(
    artifact_dir: Path,
    *,
    source_revision: str,
    gates: dict[str, ReleaseGate],
    expected_version: str = "0.1.0",
) -> dict[str, object]:
    """Inspect and bind an artifact set to explicit internal/external gates."""

    if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        raise ValueError("source revision must be a full lowercase 40-character Git SHA")
    paths = _artifact_paths(artifact_dir)
    if not paths:
        raise ValueError("artifact directory contains no wheel or sdist")

    artifacts: list[dict[str, object]] = []
    results: list[InspectionResult] = []
    for path in paths:
        initial_stat = path.lstat()
        if not stat.S_ISREG(initial_stat.st_mode):
            raise ValueError(f"artifact path must remain a regular file: {path.name}")
        initial_sha256 = _sha256(path)
        result = inspect_artifact(path, expected_version=expected_version)
        final_sha256 = _sha256(path)
        final_stat = path.lstat()
        initial_identity = (
            initial_stat.st_dev,
            initial_stat.st_ino,
            initial_stat.st_size,
            initial_stat.st_mtime_ns,
        )
        final_identity = (
            final_stat.st_dev,
            final_stat.st_ino,
            final_stat.st_size,
            final_stat.st_mtime_ns,
        )
        if (
            not stat.S_ISREG(final_stat.st_mode)
            or initial_sha256 != final_sha256
            or initial_identity != final_identity
        ):
            raise ValueError(f"artifact changed during inspection: {path.name}")
        results.append(result)
        artifacts.append(
            {
                "filename": path.name,
                "sha256": final_sha256,
                "bytes": final_stat.st_size,
                "kind": result.kind,
                "variant": result.variant,
                "inspection_ok": result.ok,
                "errors": list(result.errors),
                "release_blockers": list(result.release_blockers),
                "deferred_platform_checks": list(result.deferred_platform_checks),
                "legal_payload_sha256": result.legal_payload_sha256,
            }
        )

    set_errors: list[str] = []
    pure_count = sum(result.variant == "pure" for result in results)
    sdist_count = sum(result.variant == "sdist" for result in results)
    native_count = sum(result.variant == "native" for result in results)
    expected_filenames = expected_artifact_filenames(expected_version)
    actual_filenames = {path.name for path in paths}
    for filename in sorted(expected_filenames - actual_filenames):
        set_errors.append(f"artifact set is missing required artifact: {filename}")
    for filename in sorted(actual_filenames - expected_filenames):
        set_errors.append(f"artifact set contains unexpected artifact: {filename}")
    if pure_count != 1:
        set_errors.append(f"artifact set must contain exactly one pure wheel; found {pure_count}")
    if sdist_count != 1:
        set_errors.append(f"artifact set must contain exactly one sdist; found {sdist_count}")
    expected_native_count = len(SUPPORTED_CPYTHON_TAGS) * len(APPROVED_LANES)
    if native_count != expected_native_count:
        set_errors.append(
            "artifact set must contain exactly "
            f"{expected_native_count} native wheels; found {native_count}"
        )
    if pure_count == 1:
        pure_result = next(result for result in results if result.variant == "pure")
        pure_fingerprint = pure_result.non_native_payload_sha256
        if pure_fingerprint is None:
            set_errors.append("artifact set pure wheel has no non-native payload fingerprint")
        else:
            for result in results:
                if result.variant != "native":
                    continue
                if result.non_native_payload_sha256 is None:
                    set_errors.append(
                        f"artifact has no non-native payload fingerprint: {Path(result.path).name}"
                    )
                elif result.non_native_payload_sha256 != pure_fingerprint:
                    set_errors.append(
                        "artifact non-native payload differs from pure wheel: "
                        f"{Path(result.path).name}"
                    )
    legal_baseline = next(
        (
            result.legal_payload_sha256
            for result in results
            if result.variant == "sdist" and result.legal_payload_sha256 is not None
        ),
        None,
    )
    for result in results:
        if result.legal_payload_sha256 is None:
            set_errors.append(
                f"artifact has no legal payload fingerprint: {Path(result.path).name}"
            )
        elif legal_baseline is None:
            legal_baseline = result.legal_payload_sha256
        elif result.legal_payload_sha256 != legal_baseline:
            set_errors.append(
                f"artifact legal payload differs across artifact set: {Path(result.path).name}"
            )

    blockers = list(set_errors)
    for result in results:
        blockers.extend(f"{Path(result.path).name}: {error}" for error in result.errors)
        blockers.extend(
            f"{Path(result.path).name}: {blocker}" for blocker in result.release_blockers
        )
    platform_gate = gates.get("platform_artifact_audit")
    if platform_gate is None or platform_gate.status != "passed":
        for result in results:
            blockers.extend(
                f"{Path(result.path).name}: {check}" for check in result.deferred_platform_checks
            )

    rendered_gates: dict[str, dict[str, str]] = {}
    for name in REQUIRED_RELEASE_GATES:
        gate = gates.get(name)
        if gate is None:
            blockers.append(f"release gate has no evidence: {name}")
            rendered_gates[name] = {"status": "blocked", "evidence": "not supplied"}
        else:
            rendered_gates[name] = {"status": gate.status, "evidence": gate.evidence}
            if gate.status != "passed":
                blockers.append(f"release gate {name} is {gate.status}: {gate.evidence}")

    unique_blockers = sorted(set(blockers))
    return {
        "schema": 1,
        "distribution": "pyowl-core",
        "version": expected_version,
        "source_revision": source_revision,
        "artifacts": artifacts,
        "artifact_counts": {
            "pure_wheels": pure_count,
            "native_wheels": native_count,
            "sdists": sdist_count,
        },
        "gates": rendered_gates,
        "release_ready": not unique_blockers,
        "blockers": unique_blockers,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_dir", type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--expected-version", default="0.1.0")
    parser.add_argument("--gate-file", type=Path)
    parser.add_argument("--gate", action="append", default=[], type=parse_gate)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args(argv)
    try:
        gates = {} if args.gate_file is None else load_gate_file(args.gate_file)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    for name, gate in args.gate:
        if name in gates:
            parser.error(f"duplicate release gate {name!r}")
        gates[name] = gate
    try:
        report = build_release_report(
            args.artifact_dir.resolve(),
            source_revision=args.source_revision,
            gates=gates,
            expected_version=args.expected_version,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"release report generated: {args.output}")
    artifact_rows = report["artifacts"]
    assert isinstance(artifact_rows, list)
    inspection_failed = any(
        not bool(artifact["inspection_ok"])
        for artifact in artifact_rows
        if isinstance(artifact, dict)
    )
    return int(inspection_failed or (args.require_ready and not bool(report["release_ready"])))


if __name__ == "__main__":
    raise SystemExit(main())
