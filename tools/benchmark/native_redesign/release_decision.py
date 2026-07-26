"""Derive the two fail-closed WP18 decisions from exact evidence rows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, TypeVar, cast

INPUT_SCHEMA = "pyowl-core.native-redesign-release-evidence/2"
DECISION_SCHEMA = "pyowl-core.native-redesign-release-decision/2"
_LEGACY_INPUT_SCHEMA = "pyowl-core.native-redesign-release-evidence/1"

REQUIRED_CORE_GATES = (
    "semantic-differential-conformance",
    "security-resource-determinism",
    "retained-storage-no-eager-expansion",
    "encoded-view-installed-integration",
    "direct-decoded-mmap-overlay-composite-parity",
    "pure-and-native-artifact-matrix",
    "horned-direct-common-contract",
    "horned-wheel-common-contract",
    "version-schema-consistency",
    "artifact-license-sbom-java-audit",
)
REQUIRED_WORKSPACE_CONSUMERS: Mapping[str, str] = MappingProxyType(
    {
        "exact-om": "compatibility-consumer",
        "oaei-bioml-eval": "compatibility-consumer",
        "projector": "encoded-native-compiler",
        "pyelk": "encoded-native-compiler",
        "pyhermit": "encoded-native-compiler",
    }
)

_ROOT_FIELDS = frozenset({"schema", "core_revision", "core_gates", "workspace_consumers"})
_GATE_FIELDS = frozenset({"id", "status", "reason", "evidence"})
_CONSUMER_FIELDS = frozenset({"id", "role", "status", "revision", "reason", "evidence"})
_EVIDENCE_FIELDS = frozenset({"path", "sha256"})
_STATUSES = frozenset({"pass", "fail", "not-run"})
_GIT_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_StableResultT = TypeVar("_StableResultT")


class ReleaseDecisionError(ValueError):
    """Release evidence is incomplete, ambiguous, or claims an unproved pass."""


def load_release_evidence(path: Path) -> dict[str, object]:
    """Load one evidence document and verify pass evidence beside the manifest."""

    try:
        evidence_root = path.parent.resolve(strict=True)
        manifest = _read_stable_regular_file(path, label="release evidence manifest")
        payload = json.loads(manifest.decode("utf-8"))
        if path.parent.resolve(strict=True) != evidence_root:
            raise ReleaseDecisionError("release evidence manifest changed while reading")
    except ReleaseDecisionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseDecisionError(f"cannot load release evidence: {error}") from error
    if not isinstance(payload, dict):
        raise ReleaseDecisionError("release evidence root must be an object")
    return evaluate_release_decision(
        cast(dict[str, object], payload),
        evidence_root=evidence_root,
    )


def evaluate_release_decision(
    payload: Mapping[str, object],
    *,
    evidence_root: Path | None = None,
) -> dict[str, object]:
    """Validate evidence and derive core and workspace decisions independently."""

    _require_exact_fields(payload, _ROOT_FIELDS, "release evidence")
    source_schema = payload["schema"]
    if not isinstance(source_schema, str) or source_schema not in (
        INPUT_SCHEMA,
        _LEGACY_INPUT_SCHEMA,
    ):
        raise ReleaseDecisionError(f"unsupported release evidence schema: {payload['schema']!r}")
    legacy = source_schema == _LEGACY_INPUT_SCHEMA
    core_revision = _require_revision(payload["core_revision"], "core_revision")
    core_rows = _validate_rows(
        payload["core_gates"],
        expected=MappingProxyType({value: None for value in REQUIRED_CORE_GATES}),
        consumer=False,
        evidence_root=evidence_root,
        legacy=legacy,
    )
    consumer_rows = _validate_rows(
        payload["workspace_consumers"],
        expected=REQUIRED_WORKSPACE_CONSUMERS,
        consumer=True,
        evidence_root=evidence_root,
        legacy=legacy,
    )

    core_blockers = tuple(_blocker(row) for row in core_rows if row["status"] != "pass")
    consumer_blockers = tuple(
        _blocker(row, include_revision=True) for row in consumer_rows if row["status"] != "pass"
    )
    core_release_eligible = not core_blockers
    consumers_complete = not consumer_blockers
    workspace_optimization_complete = core_release_eligible and consumers_complete

    workspace_blockers: list[dict[str, object]] = list(consumer_blockers)
    if not core_release_eligible:
        workspace_blockers.insert(
            0,
            {
                "id": "core-release-eligibility",
                "status": "fail",
                "reason": "workspace completion requires core_release_eligible=true",
            },
        )

    return {
        "schema": DECISION_SCHEMA,
        "source_schema": source_schema,
        "core_revision": core_revision,
        "core_release_eligible": core_release_eligible,
        "workspace_optimization_complete": workspace_optimization_complete,
        "core": {
            "status": "pass" if core_release_eligible else "fail",
            "required_gate_count": len(REQUIRED_CORE_GATES),
            "passed_gate_count": len(REQUIRED_CORE_GATES) - len(core_blockers),
            "blockers": list(core_blockers),
        },
        "workspace": {
            "status": "pass" if workspace_optimization_complete else "fail",
            "required_consumer_count": len(REQUIRED_WORKSPACE_CONSUMERS),
            "passed_consumer_count": len(REQUIRED_WORKSPACE_CONSUMERS) - len(consumer_blockers),
            "not_run_consumers": [
                cast(str, row["id"]) for row in consumer_rows if row["status"] == "not-run"
            ],
            "blockers": workspace_blockers,
        },
        "claims": {
            "core_release_allowed": core_release_eligible,
            "multi_consumer_native_performance_allowed": workspace_optimization_complete,
        },
    }


def _validate_rows(
    value: object,
    *,
    expected: Mapping[str, str | None],
    consumer: bool,
    evidence_root: Path | None,
    legacy: bool,
) -> tuple[dict[str, object], ...]:
    label = "workspace_consumers" if consumer else "core_gates"
    if not isinstance(value, list):
        raise ReleaseDecisionError(f"{label} must be an array")
    rows: list[dict[str, object]] = []
    observed: set[str] = set()
    fields = _CONSUMER_FIELDS if consumer else _GATE_FIELDS
    for index, candidate in enumerate(value):
        if not isinstance(candidate, dict):
            raise ReleaseDecisionError(f"{label}[{index}] must be an object")
        row = cast(dict[str, object], candidate)
        _require_exact_fields(row, fields, f"{label}[{index}]")
        identifier = row["id"]
        if not isinstance(identifier, str) or identifier not in expected:
            raise ReleaseDecisionError(f"{label}[{index}] has unknown id: {identifier!r}")
        if identifier in observed:
            raise ReleaseDecisionError(f"{label} contains duplicate id: {identifier}")
        observed.add(identifier)
        _validate_status_row(
            row,
            f"{label}[{index}]",
            evidence_root=evidence_root,
            legacy=legacy,
        )
        if consumer:
            required_role = expected[identifier]
            if row["role"] != required_role:
                raise ReleaseDecisionError(
                    f"{identifier}: role must be {required_role!r}, got {row['role']!r}"
                )
            revision = row["revision"]
            if revision is not None:
                _require_revision(revision, f"{identifier}.revision")
            if row["status"] in {"pass", "fail"} and revision is None:
                raise ReleaseDecisionError(
                    f"{identifier}: {row['status']} requires an exact revision"
                )
        rows.append(dict(row))
    missing = set(expected).difference(observed)
    if missing:
        raise ReleaseDecisionError(f"{label} is missing required ids: {sorted(missing)!r}")
    return tuple(sorted(rows, key=lambda row: cast(str, row["id"])))


def _validate_status_row(
    row: Mapping[str, object],
    label: str,
    *,
    evidence_root: Path | None,
    legacy: bool,
) -> None:
    status = row["status"]
    if status not in _STATUSES:
        raise ReleaseDecisionError(f"{label}.status must be pass, fail, or not-run")
    reason = row["reason"]
    evidence = row["evidence"]
    if legacy:
        _validate_legacy_evidence(evidence, label)
        if status == "pass":
            raise ReleaseDecisionError(
                f"{label}: schema 1 cannot claim pass; use checksum-bound release evidence schema 2"
            )
    else:
        _validate_bound_evidence(
            evidence,
            label,
            verify=status == "pass",
            evidence_root=evidence_root,
        )
    if status == "pass":
        if not cast(list[object], evidence):
            raise ReleaseDecisionError(f"{label}: pass requires evidence")
        if reason is not None:
            raise ReleaseDecisionError(f"{label}: pass reason must be null")
        return
    if not isinstance(reason, str) or not reason.strip():
        raise ReleaseDecisionError(f"{label}: {status} requires a nonempty reason")


def _validate_legacy_evidence(value: object, label: str) -> None:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ReleaseDecisionError(f"{label}.evidence must be an array of nonempty strings")
    if len(set(cast(list[str], value))) != len(value):
        raise ReleaseDecisionError(f"{label}.evidence contains duplicates")


def _validate_bound_evidence(
    value: object,
    label: str,
    *,
    verify: bool,
    evidence_root: Path | None,
) -> None:
    if not isinstance(value, list):
        raise ReleaseDecisionError(f"{label}.evidence must be an array")
    paths: set[str] = set()
    for index, candidate in enumerate(value):
        item_label = f"{label}.evidence[{index}]"
        if not isinstance(candidate, dict):
            raise ReleaseDecisionError(f"{item_label} must be an object")
        item = cast(dict[str, object], candidate)
        _require_exact_fields(item, _EVIDENCE_FIELDS, item_label)
        path = item["path"]
        sha256 = item["sha256"]
        if not isinstance(path, str) or not path.strip():
            raise ReleaseDecisionError(f"{item_label}.path must be a nonempty string")
        if path in paths:
            raise ReleaseDecisionError(f"{label}.evidence contains duplicate path: {path}")
        paths.add(path)
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise ReleaseDecisionError(f"{item_label}.sha256 must be lowercase 64-hex")
        if verify:
            if evidence_root is None:
                raise ReleaseDecisionError(f"{label}: pass evidence requires an evidence root")
            _verify_evidence_file(
                root=evidence_root,
                relative_path=path,
                expected_sha256=sha256,
                label=item_label,
            )


def _verify_evidence_file(
    *,
    root: Path,
    relative_path: str,
    expected_sha256: str,
    label: str,
) -> None:
    path = Path(relative_path)
    if path.is_absolute():
        raise ReleaseDecisionError(f"{label}.path must be relative to the evidence manifest")
    try:
        resolved_root = root.resolve(strict=True)
        selected_path = resolved_root / path
        resolved_path = selected_path.resolve()
        resolved_path.relative_to(resolved_root)
    except (OSError, RuntimeError) as error:
        raise ReleaseDecisionError(f"{label}.path cannot be loaded: {error}") from error
    except ValueError as error:
        raise ReleaseDecisionError(f"{label}.path escapes the evidence root") from error
    if not resolved_root.is_dir():
        raise ReleaseDecisionError("evidence root must be a directory")
    try:
        resolved_path = selected_path.resolve(strict=True)
        resolved_path.relative_to(resolved_root)
    except (OSError, RuntimeError) as error:
        raise ReleaseDecisionError(f"{label}.path cannot be loaded: {error}") from error
    except ValueError as error:
        raise ReleaseDecisionError(f"{label}.path escapes the evidence root") from error
    actual_sha256 = _stable_regular_file_sha256(
        selected_path,
        label=f"{label}.path",
    )
    try:
        resolved_after_read = selected_path.resolve(strict=True)
        resolved_after_read.relative_to(resolved_root)
    except (OSError, RuntimeError) as error:
        raise ReleaseDecisionError(f"{label}.path cannot be loaded: {error}") from error
    except ValueError as error:
        raise ReleaseDecisionError(f"{label}.path escapes the evidence root") from error
    if resolved_after_read != resolved_path:
        raise ReleaseDecisionError(f"{label}.path changed while reading")
    if actual_sha256 != expected_sha256:
        raise ReleaseDecisionError(f"{label}.sha256 does not match {relative_path}")


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _consume_stable_regular_file(
    path: Path,
    *,
    label: str,
    consume: Callable[[BinaryIO], tuple[_StableResultT, int]],
) -> _StableResultT:
    try:
        initial = path.lstat()
    except OSError as error:
        raise ReleaseDecisionError(f"cannot inspect {label}: {error}") from error
    if not stat.S_ISREG(initial.st_mode):
        raise ReleaseDecisionError(f"{label} must be a regular non-symlink file")
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            result, consumed_bytes = consume(stream)
            completed = os.fstat(stream.fileno())
        final = path.lstat()
    except OSError as error:
        raise ReleaseDecisionError(f"cannot read {label}: {error}") from error
    identities = {
        _stat_identity(initial),
        _stat_identity(opened),
        _stat_identity(completed),
        _stat_identity(final),
    }
    if len(identities) != 1 or not stat.S_ISREG(opened.st_mode) or consumed_bytes != opened.st_size:
        raise ReleaseDecisionError(f"{label} changed while reading")
    return result


def _read_stable_regular_file(path: Path, *, label: str) -> bytes:
    def consume(stream: BinaryIO) -> tuple[bytes, int]:
        payload = stream.read()
        return payload, len(payload)

    return _consume_stable_regular_file(path, label=label, consume=consume)


def _stable_regular_file_sha256(path: Path, *, label: str) -> str:
    def consume(stream: BinaryIO) -> tuple[str, int]:
        consumed_bytes = 0
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024**2), b""):
            consumed_bytes += len(chunk)
            digest.update(chunk)
        return digest.hexdigest(), consumed_bytes

    return _consume_stable_regular_file(path, label=label, consume=consume)


def _blocker(
    row: Mapping[str, object],
    *,
    include_revision: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {
        "id": row["id"],
        "status": row["status"],
        "reason": row["reason"],
    }
    if include_revision:
        result["revision"] = row["revision"]
    return result


def _require_exact_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    observed = set(value)
    missing = expected.difference(observed)
    unknown = observed.difference(expected)
    if missing or unknown:
        parts: list[str] = []
        if missing:
            parts.append(f"missing fields {sorted(missing)!r}")
        if unknown:
            parts.append(f"unknown fields {sorted(unknown)!r}")
        raise ReleaseDecisionError(f"{label}: {'; '.join(parts)}")


def _require_revision(value: object, label: str) -> str:
    if not isinstance(value, str) or not _GIT_REVISION.fullmatch(value):
        raise ReleaseDecisionError(f"{label} must be an exact lowercase 40-hex Git revision")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = load_release_evidence(arguments.evidence)
    except ReleaseDecisionError as error:
        parser.error(str(error))
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if arguments.output is None:
        print(rendered, end="")
    else:
        arguments.output.write_text(rendered, encoding="utf-8")
    return 0 if result["core_release_eligible"] else 1


if __name__ == "__main__":  # pragma: no cover - exercised through main
    raise SystemExit(main())
