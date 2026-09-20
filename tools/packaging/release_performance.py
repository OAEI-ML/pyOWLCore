"""Validate the owner-approved, version-scoped native evidence exception."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

SCOPED_VERSION = "0.2.1"
SCOPED_APPROVAL = "owner-approved-scoped-validation"
REQUIRED_EVIDENCE = {
    "benchmark-policy.md",
    "owner-release-authorization.md",
    "consumer-stack.json",
    "consumer-stack-tests.log",
    "static-evidence.md",
    "native-optimization/artifacts.json",
    "native-optimization/completed-summary.json",
    "native-optimization/job.json",
    "native-optimization/source.oracle.json",
    "native-optimization/target.oracle.json",
}


def validate_policy(policy_path: Path, *, version: str) -> tuple[dict[str, str], dict[str, bytes]]:
    """Check the explicit 0.2.1 policy and preserve the exact verified bytes."""

    if version != SCOPED_VERSION:
        raise ValueError("other versions require the full Native performance workflow")
    if policy_path.is_symlink():
        raise ValueError("performance policy must not be a symlink")
    policy_bytes = policy_path.read_bytes()
    policy = json.loads(policy_bytes)
    if not isinstance(policy, dict) or set(policy) != {"schema", "version", "approval", "evidence"}:
        raise ValueError("unexpected performance policy fields")
    if (
        type(policy["schema"]) is not int
        or policy["schema"] != 1
        or policy["version"] != version
        or policy["approval"] != SCOPED_APPROVAL
    ):
        raise ValueError("performance policy is not the approved 0.2.1 exception")
    evidence = policy["evidence"]
    if not isinstance(evidence, dict) or not REQUIRED_EVIDENCE.issubset(evidence):
        raise ValueError(
            "policy must bind owner approval, benchmark scope, and validation evidence"
        )
    blobs = {"performance-policy.json": policy_bytes}
    for name, expected in evidence.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            raise ValueError("evidence paths and hashes must be strings")
        relative = PurePosixPath(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in name
            or str(relative) != name
            or name in {".", "evidence.json", "performance-policy.json"}
            or re.fullmatch(r"[0-9a-f]{64}", expected) is None
        ):
            raise ValueError(f"invalid evidence binding: {name}")
        path = policy_path.parent
        for part in relative.parts:
            path = path / part
            if path.is_symlink():
                raise ValueError(f"evidence must not traverse a symlink: {name}")
        if not path.is_file():
            raise ValueError(f"missing evidence file: {name}")
        blob = path.read_bytes()
        if hashlib.sha256(blob).hexdigest() != expected:
            raise ValueError(f"evidence checksum mismatch: {name}")
        blobs[name] = blob
    return evidence, blobs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-report", type=Path, required=True)
    args = parser.parse_args()
    if re.fullmatch(r"[0-9a-f]{40}", args.source_revision) is None:
        parser.error("source revision must be a full lowercase Git SHA")
    evidence, blobs = validate_policy(args.policy, version=args.version)
    candidate_bytes = args.candidate_report.read_bytes()
    candidate = json.loads(candidate_bytes)
    if candidate["version"] != args.version or candidate["source_revision"] != args.source_revision:
        parser.error("candidate report does not match the signed source and version")
    payload = {
        "candidate_release_report_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
        "schema": "pyowl-core/scoped-performance-evidence/v1",
        "version": args.version,
        "approval": SCOPED_APPROVAL,
        "source_revision": args.source_revision,
        "policy_sha256": hashlib.sha256(blobs["performance-policy.json"]).hexdigest(),
        "evidence": evidence,
        "full_comparator_study": "not-executed-by-this-policy",
    }
    args.output.parent.mkdir(parents=True, exist_ok=False)
    for name, blob in blobs.items():
        destination = args.output.parent / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(blob)
    output = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    args.output.write_bytes(output)
    print(hashlib.sha256(output).hexdigest())


if __name__ == "__main__":
    main()
