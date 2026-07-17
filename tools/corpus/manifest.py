"""Validate and render the immutable WP09 corpus provenance lock."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the Python 3.10 lane
    import tomli as tomllib  # type: ignore[import-not-found, unused-ignore]

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "tests" / "data"
PROVENANCE = DATA / "PROVENANCE.toml"
LOCK = ROOT / "reports" / "conformance" / "corpus-lock.json"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_IGNORED = frozenset({"PROVENANCE.toml", "deviations.toml", "README.md"})
_REQUIRED_FIELDS = frozenset(
    {
        "id",
        "path",
        "category",
        "origin_url",
        "upstream_revision",
        "retrieved_at",
        "sha256",
        "license_expression",
        "upstream_terms",
        "transformation",
        "redistribution",
    }
)


class ManifestError(ValueError):
    """A provenance ledger or retained corpus violates the fail-closed policy."""


@dataclass(frozen=True, slots=True)
class CorpusArtifact:
    id: str
    path: str
    category: str
    origin_url: str
    upstream_revision: str
    retrieved_at: str
    sha256: str
    license_expression: str
    upstream_terms: str
    transformation: str
    redistribution: str

    @property
    def absolute_path(self) -> Path:
        return DATA / self.path

    def lock_record(self) -> dict[str, str | int]:
        return {
            "category": self.category,
            "id": self.id,
            "license_expression": self.license_expression,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.absolute_path.stat().st_size,
            "upstream_revision": self.upstream_revision,
        }


def _load_toml(path: Path) -> Mapping[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ManifestError(f"cannot read provenance ledger: {error}") from error
    if not isinstance(value, Mapping):
        raise ManifestError("provenance root must be a table")
    return value


def _text(record: Mapping[str, object], name: str, artifact_id: str) -> str:
    value = record.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{artifact_id}: {name} must be nonempty text")
    return value


def load_manifest(path: Path = PROVENANCE) -> tuple[CorpusArtifact, ...]:
    raw = _load_toml(path)
    if raw.get("schema") != 1:
        raise ManifestError("unsupported provenance schema")
    records = raw.get("artifact")
    if not isinstance(records, list) or not records:
        raise ManifestError("provenance ledger must contain artifacts")
    artifacts: list[CorpusArtifact] = []
    ids: set[str] = set()
    paths: set[str] = set()
    for index, value in enumerate(records):
        if not isinstance(value, Mapping):
            raise ManifestError(f"artifact {index} must be a table")
        extra = set(value) - _REQUIRED_FIELDS
        missing = _REQUIRED_FIELDS - set(value)
        if extra or missing:
            raise ManifestError(
                f"artifact {index} fields differ: missing={sorted(missing)}, extra={sorted(extra)}"
            )
        artifact_id = _text(value, "id", f"artifact-{index}")
        artifact = CorpusArtifact(
            artifact_id,
            _text(value, "path", artifact_id),
            _text(value, "category", artifact_id),
            _text(value, "origin_url", artifact_id),
            _text(value, "upstream_revision", artifact_id),
            _text(value, "retrieved_at", artifact_id),
            _text(value, "sha256", artifact_id),
            _text(value, "license_expression", artifact_id),
            _text(value, "upstream_terms", artifact_id),
            _text(value, "transformation", artifact_id),
            _text(value, "redistribution", artifact_id),
        )
        if artifact.id in ids or artifact.path in paths:
            raise ManifestError(f"duplicate provenance id/path: {artifact.id}/{artifact.path}")
        ids.add(artifact.id)
        paths.add(artifact.path)
        artifacts.append(artifact)
    return tuple(sorted(artifacts, key=lambda artifact: artifact.id))


def validate_manifest(path: Path = PROVENANCE) -> tuple[CorpusArtifact, ...]:
    artifacts = load_manifest(path)
    registered = {artifact.path for artifact in artifacts}
    retained = {
        candidate.relative_to(DATA).as_posix()
        for candidate in DATA.rglob("*")
        if candidate.is_file() and candidate.name not in _IGNORED
    }
    if registered != retained:
        raise ManifestError(
            "provenance file set differs: "
            f"unregistered={sorted(retained - registered)}, missing={sorted(registered - retained)}"
        )
    for artifact in artifacts:
        candidate = artifact.absolute_path
        try:
            relative = candidate.resolve(strict=True).relative_to(DATA.resolve(strict=True))
        except (OSError, ValueError) as error:
            raise ManifestError(f"{artifact.id}: path escapes tests/data") from error
        if relative.as_posix() != artifact.path or candidate.is_symlink():
            raise ManifestError(f"{artifact.id}: path is noncanonical or a symlink")
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if not _HASH.fullmatch(artifact.sha256) or digest != artifact.sha256:
            raise ManifestError(f"{artifact.id}: SHA-256 mismatch")
        if artifact.redistribution != "allowed":
            raise ManifestError(f"{artifact.id}: retained bytes are not redistributable")
        if not artifact.origin_url.startswith("https://") or not artifact.upstream_terms.startswith(
            "https://"
        ):
            raise ManifestError(f"{artifact.id}: provenance URLs must use HTTPS")
    return artifacts


def render_lock(artifacts: Sequence[CorpusArtifact] | None = None) -> str:
    selected = validate_manifest() if artifacts is None else tuple(artifacts)
    document = {
        "artifact_count": len(selected),
        "artifacts": [artifact.lock_record() for artifact in selected],
        "provenance_sha256": hashlib.sha256(PROVENANCE.read_bytes()).hexdigest(),
        "schema": 1,
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="compare the checked-in lock")
    args = parser.parse_args(argv)
    rendered = render_lock()
    if args.check:
        if not LOCK.is_file() or LOCK.read_text(encoding="utf-8") != rendered:
            print(f"stale corpus lock: {LOCK}")
            return 1
        return 0
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CorpusArtifact",
    "ManifestError",
    "load_manifest",
    "main",
    "render_lock",
    "validate_manifest",
]
