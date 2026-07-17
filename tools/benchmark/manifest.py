"""Fail-closed corpus manifest validation and explicit offline preparation."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib  # type: ignore[import-not-found, unused-ignore]

from pyowl_core import DocumentFormat

from .synthetic import (
    SyntheticCounts,
    adversarial_deep_functional,
    annotation_list_turtle,
    equivalent_counts,
    equivalent_source,
    import_diamond,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "benchmarks" / "corpora.toml"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REQUIRED_TIERS = frozenset(
    {"tiny", "small", "medium", "large", "composite", "synthetic", "adversarial"}
)
_REQUIRED_FAMILIES = frozenset(
    {
        "constructors",
        "biomedical",
        "imports",
        "annotation-list-heavy",
        "oaei-composite",
        "synthetic",
        "adversarial",
    }
)
_SOURCES = frozenset({"generated", "url", "archive-member"})
_REDISTRIBUTION = frozenset({"generated", "allowed", "manifest-only"})


class ManifestError(ValueError):
    """The benchmark corpus lock is incomplete, unsafe, or inconsistent."""


@dataclass(frozen=True, slots=True)
class CorpusCounts:
    bytes: int
    triples: int
    axioms: int
    entities: int
    imports: int
    basis: str

    def __post_init__(self) -> None:
        for name in ("bytes", "triples", "axioms", "entities", "imports"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ManifestError(f"count {name} must be a non-negative integer")
        if not self.basis:
            raise ManifestError("count basis must be nonempty")


@dataclass(frozen=True, slots=True)
class Corpus:
    id: str
    tier: str
    families: tuple[str, ...]
    source: str
    format: DocumentFormat
    revision: str
    sha256: str
    counts: CorpusCounts
    license: str
    license_url: str
    acquired: str
    redistribution: str
    url: str | None = None
    artifact_sha256: str | None = None
    artifact_bytes: int | None = None
    archive_member: str | None = None
    mapping_member: str | None = None
    mapping_sha256: str | None = None
    mapping_bytes: int | None = None
    mapping_rows: int | None = None
    generator: str | None = None
    generator_size: int | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if not _valid_id(self.id):
            raise ManifestError(f"invalid corpus id: {self.id!r}")
        if self.tier not in _REQUIRED_TIERS:
            raise ManifestError(f"{self.id}: unknown tier {self.tier!r}")
        if not self.families or any(not _valid_id(value) for value in self.families):
            raise ManifestError(f"{self.id}: families must be nonempty identifiers")
        if len(set(self.families)) != len(self.families):
            raise ManifestError(f"{self.id}: duplicate family")
        if self.source not in _SOURCES:
            raise ManifestError(f"{self.id}: unknown source kind")
        if not self.revision:
            raise ManifestError(f"{self.id}: revision must be nonempty")
        _validate_digest(self.sha256, f"{self.id}.sha256")
        if not self.license or not self.license_url:
            raise ManifestError(f"{self.id}: license and license_url are required")
        _validate_https(self.license_url, f"{self.id}.license_url")
        if self.redistribution not in _REDISTRIBUTION:
            raise ManifestError(f"{self.id}: unknown redistribution policy")
        _validate_date(self.acquired, f"{self.id}.acquired")
        if self.source == "generated":
            if self.url is not None or self.archive_member is not None:
                raise ManifestError(f"{self.id}: generated corpus cannot have a URL/member")
            if self.generator is None or self.generator_size is None:
                raise ManifestError(f"{self.id}: generator and generator_size are required")
            if self.redistribution != "generated":
                raise ManifestError(f"{self.id}: generated corpus must use generated policy")
        else:
            if self.url is None:
                raise ManifestError(f"{self.id}: external corpus URL is required")
            _validate_https(self.url, f"{self.id}.url")
            if self.generator is not None or self.generator_size is not None:
                raise ManifestError(f"{self.id}: external corpus cannot specify a generator")
        if self.source == "archive-member":
            if self.archive_member is None or self.artifact_sha256 is None:
                raise ManifestError(f"{self.id}: archive member and artifact digest are required")
            if self.artifact_bytes is None or self.artifact_bytes < 1:
                raise ManifestError(f"{self.id}: positive artifact_bytes is required")
            _validate_digest(self.artifact_sha256, f"{self.id}.artifact_sha256")
            _validate_member(self.archive_member, self.id)
        elif any(
            value is not None
            for value in (self.archive_member, self.artifact_sha256, self.artifact_bytes)
        ):
            raise ManifestError(f"{self.id}: archive metadata requires archive-member source")
        mapping_values = (
            self.mapping_member,
            self.mapping_sha256,
            self.mapping_bytes,
            self.mapping_rows,
        )
        if any(value is not None for value in mapping_values):
            if self.source != "archive-member" or any(value is None for value in mapping_values):
                raise ManifestError(f"{self.id}: complete archive mapping metadata is required")
            assert self.mapping_member is not None
            assert self.mapping_sha256 is not None
            assert self.mapping_bytes is not None
            assert self.mapping_rows is not None
            _validate_member(self.mapping_member, self.id)
            _validate_digest(self.mapping_sha256, f"{self.id}.mapping_sha256")
            if self.mapping_bytes < 1 or self.mapping_rows < 1:
                raise ManifestError(f"{self.id}: mapping bytes/rows must be positive")

    @property
    def filename(self) -> str:
        suffix = {
            DocumentFormat.FUNCTIONAL: ".ofn",
            DocumentFormat.OWL_XML: ".owx",
            DocumentFormat.TURTLE: ".ttl",
            DocumentFormat.RDF_XML: ".rdf",
        }[self.format]
        return f"{self.id}{suffix}"


@dataclass(frozen=True, slots=True)
class CorpusManifest:
    schema: int
    project: str
    corpora: tuple[Corpus, ...]

    def __post_init__(self) -> None:
        if self.schema != 1:
            raise ManifestError("unsupported corpus manifest schema")
        if self.project != "pyowl-core":
            raise ManifestError("manifest project must be pyowl-core")
        ids = tuple(item.id for item in self.corpora)
        if len(set(ids)) != len(ids):
            raise ManifestError("corpus ids must be unique")
        tiers = {item.tier for item in self.corpora}
        missing_tiers = sorted(_REQUIRED_TIERS - tiers)
        if missing_tiers:
            raise ManifestError(f"missing corpus tiers: {', '.join(missing_tiers)}")
        families = {family for item in self.corpora for family in item.families}
        missing_families = sorted(_REQUIRED_FAMILIES - families)
        if missing_families:
            raise ManifestError(f"missing corpus families: {', '.join(missing_families)}")
        required_formats = set(DocumentFormat)
        generated_formats = {item.format for item in self.corpora if item.source == "generated"}
        missing_formats = sorted(value.value for value in required_formats - generated_formats)
        if missing_formats:
            raise ManifestError(
                "generated representative inputs missing formats: " + ", ".join(missing_formats)
            )

    def by_id(self, corpus_id: str) -> Corpus:
        for corpus in self.corpora:
            if corpus.id == corpus_id:
                return corpus
        raise ManifestError(f"unknown corpus id: {corpus_id}")


def load_manifest(path: Path = DEFAULT_MANIFEST) -> CorpusManifest:
    """Load and completely validate a corpus manifest."""

    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ManifestError(f"cannot read corpus manifest: {error}") from error
    if not isinstance(payload, Mapping):
        raise ManifestError("manifest root must be a table")
    root = cast(Mapping[str, Any], payload)
    rows = root.get("corpus")
    if not isinstance(rows, list):
        raise ManifestError("manifest must contain [[corpus]] rows")
    corpora = tuple(_parse_corpus(_mapping(row, "corpus row")) for row in rows)
    return CorpusManifest(
        schema=_integer(root.get("schema"), "schema"),
        project=_string(root.get("project"), "project"),
        corpora=corpora,
    )


def generated_bytes(corpus: Corpus) -> bytes:
    """Materialize a deterministic generated corpus entry."""

    if corpus.source != "generated" or corpus.generator is None:
        raise ManifestError(f"{corpus.id}: not a generated corpus")
    size = cast(int, corpus.generator_size)
    if corpus.generator == "equivalent-chain":
        return equivalent_source(corpus.format, size)
    if corpus.generator == "annotation-list":
        return annotation_list_turtle(size)
    if corpus.generator == "adversarial-deep":
        return adversarial_deep_functional(size)
    if corpus.generator == "import-diamond-root":
        root, _mapping_values = import_diamond()
        return root
    raise ManifestError(f"{corpus.id}: unknown generator {corpus.generator!r}")


def verify_generated(corpus: Corpus) -> None:
    """Verify a generated entry's byte lock and exact declarative counts."""

    payload = generated_bytes(corpus)
    _verify_payload(corpus, payload)
    size = cast(int, corpus.generator_size)
    if corpus.generator == "equivalent-chain":
        expected = equivalent_counts(corpus.format, size)
    elif corpus.generator == "annotation-list":
        expected = _synthetic_counts(payload, 4 * size + 5, 2 * size + 2, size + 3, 0)
    elif corpus.generator == "adversarial-deep":
        expected = _synthetic_counts(payload, 2 * size + 2, 1, 2, 0)
    elif corpus.generator == "import-diamond-root":
        expected = _synthetic_counts(payload, 4, 1, 1, 2)
    else:
        raise ManifestError(f"{corpus.id}: unknown generator {corpus.generator!r}")
    observed = CorpusCounts(
        expected.bytes,
        expected.triples,
        expected.axioms,
        expected.entities,
        expected.imports,
        corpus.counts.basis,
    )
    if observed != corpus.counts:
        raise ManifestError(f"{corpus.id}: generated counts do not match generator")


def prepare_corpus(
    corpus: Corpus,
    cache_dir: Path,
    *,
    timeout_seconds: float = 60.0,
    max_download_bytes: int = 512 * 1024 * 1024,
    max_member_bytes: int = 512 * 1024 * 1024,
) -> Path:
    """Explicitly prepare one pinned input; timed benchmarks never call this function."""

    mapping_payload: bytes | None = None
    if corpus.source == "generated":
        payload = generated_bytes(corpus)
    else:
        assert corpus.url is not None
        artifact_limit = max_download_bytes
        if corpus.artifact_bytes is not None:
            artifact_limit = min(artifact_limit, corpus.artifact_bytes)
        artifact = _download(corpus.url, timeout_seconds, artifact_limit)
        if corpus.source == "archive-member":
            assert corpus.artifact_sha256 is not None
            assert corpus.artifact_bytes is not None
            _verify_exact(
                artifact,
                corpus.artifact_bytes,
                corpus.artifact_sha256,
                f"{corpus.id} archive",
            )
            payload = _extract_member(corpus, artifact, max_member_bytes)
            if corpus.mapping_member is not None:
                assert corpus.mapping_bytes is not None
                assert corpus.mapping_sha256 is not None
                mapping_payload = _extract_named_member(
                    corpus,
                    artifact,
                    corpus.mapping_member,
                    corpus.mapping_bytes,
                    corpus.mapping_sha256,
                    max_member_bytes,
                )
                if len(mapping_payload.splitlines()) != corpus.mapping_rows:
                    raise ManifestError(f"{corpus.id}: mapping row count mismatch")
        else:
            payload = artifact
    _verify_payload(corpus, payload)
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / corpus.filename
    _atomic_write(destination, payload)
    if mapping_payload is not None:
        _atomic_write(cache_dir / f"{corpus.id}.mappings.tsv", mapping_payload)
    return destination


def verify_prepared(corpus: Corpus, path: Path) -> None:
    """Verify a prepared corpus before any timed phase."""

    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ManifestError(f"{corpus.id}: cannot read prepared input: {error}") from error
    _verify_payload(corpus, payload)


def manifest_fingerprint(path: Path = DEFAULT_MANIFEST) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_corpus(row: Mapping[str, Any]) -> Corpus:
    counts = CorpusCounts(
        bytes=_integer(row.get("bytes"), "bytes"),
        triples=_integer(row.get("triples"), "triples"),
        axioms=_integer(row.get("axioms"), "axioms"),
        entities=_integer(row.get("entities"), "entities"),
        imports=_integer(row.get("imports"), "imports"),
        basis=_string(row.get("count_basis"), "count_basis"),
    )
    return Corpus(
        id=_string(row.get("id"), "id"),
        tier=_string(row.get("tier"), "tier"),
        families=_string_tuple(row.get("families"), "families"),
        source=_string(row.get("source"), "source"),
        format=_format(row.get("format")),
        revision=_string(row.get("revision"), "revision"),
        sha256=_string(row.get("sha256"), "sha256"),
        counts=counts,
        license=_string(row.get("license"), "license"),
        license_url=_string(row.get("license_url"), "license_url"),
        acquired=_string(row.get("acquired"), "acquired"),
        redistribution=_string(row.get("redistribution"), "redistribution"),
        url=_optional_string(row.get("url"), "url"),
        artifact_sha256=_optional_string(row.get("artifact_sha256"), "artifact_sha256"),
        artifact_bytes=_optional_integer(row.get("artifact_bytes"), "artifact_bytes"),
        archive_member=_optional_string(row.get("archive_member"), "archive_member"),
        mapping_member=_optional_string(row.get("mapping_member"), "mapping_member"),
        mapping_sha256=_optional_string(row.get("mapping_sha256"), "mapping_sha256"),
        mapping_bytes=_optional_integer(row.get("mapping_bytes"), "mapping_bytes"),
        mapping_rows=_optional_integer(row.get("mapping_rows"), "mapping_rows"),
        generator=_optional_string(row.get("generator"), "generator"),
        generator_size=_optional_integer(row.get("generator_size"), "generator_size"),
        notes=_string(row.get("notes", ""), "notes", allow_empty=True),
    )


def _synthetic_counts(
    payload: bytes,
    triples: int,
    axioms: int,
    entities: int,
    imports: int,
) -> SyntheticCounts:
    return SyntheticCounts(len(payload), triples, axioms, entities, imports)


def _download(url: str, timeout_seconds: float, limit: int) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "pyowl-core-benchmark-preparer/1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            final = response.geturl()
            _validate_https(final, "redirect URL")
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) > limit:
                raise ManifestError("download exceeds configured byte limit")
            retained = bytearray()
            while True:
                chunk = response.read(min(1024 * 1024, limit + 1 - len(retained)))
                if not chunk:
                    break
                retained.extend(chunk)
                if len(retained) > limit:
                    raise ManifestError("download exceeds configured byte limit")
            return bytes(retained)
    except (OSError, ValueError) as error:
        if isinstance(error, ManifestError):
            raise
        raise ManifestError(f"download failed: {error}") from error


def _extract_member(corpus: Corpus, artifact: bytes, limit: int) -> bytes:
    assert corpus.archive_member is not None
    return _extract_named_member(
        corpus,
        artifact,
        corpus.archive_member,
        corpus.counts.bytes,
        corpus.sha256,
        limit,
    )


def _extract_named_member(
    corpus: Corpus,
    artifact: bytes,
    member: str,
    expected_bytes: int,
    expected_sha256: str,
    limit: int,
) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
            for info in archive.infolist():
                _validate_member(info.filename, corpus.id)
            try:
                selected = archive.getinfo(member)
            except KeyError as error:
                raise ManifestError(f"{corpus.id}: archive member is absent") from error
            if selected.is_dir() or selected.file_size != expected_bytes:
                raise ManifestError(f"{corpus.id}: archive member size mismatch")
            if selected.file_size > limit:
                raise ManifestError(f"{corpus.id}: archive member exceeds expansion limit")
            payload = archive.read(selected)
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise ManifestError(f"{corpus.id}: invalid archive: {error}") from error
    _verify_exact(payload, expected_bytes, expected_sha256, f"{corpus.id}:{member}")
    return payload


def _verify_payload(corpus: Corpus, payload: bytes) -> None:
    _verify_exact(payload, corpus.counts.bytes, corpus.sha256, corpus.id)


def _verify_exact(payload: bytes, size: int, digest: str, label: str) -> None:
    if len(payload) != size:
        raise ManifestError(f"{label}: expected {size} bytes, got {len(payload)}")
    observed = hashlib.sha256(payload).hexdigest()
    if observed != digest:
        raise ManifestError(f"{label}: SHA-256 mismatch: expected {digest}, got {observed}")


def _atomic_write(destination: Path, payload: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _validate_digest(value: str, field: str) -> None:
    if not _SHA256.fullmatch(value):
        raise ManifestError(f"{field} must be lowercase SHA-256")


def _validate_https(value: str, field: str) -> None:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ManifestError(f"{field} must be credential-free HTTPS")
    if parsed.fragment:
        raise ManifestError(f"{field} must not contain a fragment")


def _validate_member(value: str, corpus_id: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or not path.parts:
        raise ManifestError(f"{corpus_id}: unsafe archive member path")


def _validate_date(value: str, field: str) -> None:
    if not re.fullmatch(r"20\d\d-\d\d-\d\d", value):
        raise ManifestError(f"{field} must be YYYY-MM-DD")


def _valid_id(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*", value))


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{field} must be a table")
    return cast(Mapping[str, Any], value)


def _string(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ManifestError(f"{field} must be a {'nonempty ' if not allow_empty else ''}string")
    return value


def _optional_string(value: object, field: str) -> str | None:
    return None if value is None else _string(value, field)


def _integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ManifestError(f"{field} must be an integer")
    return value


def _optional_integer(value: object, field: str) -> int | None:
    return None if value is None else _integer(value, field)


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ManifestError(f"{field} must be an array")
    return tuple(_string(item, field) for item in value)


def _format(value: object) -> DocumentFormat:
    selected = _string(value, "format")
    try:
        return DocumentFormat(selected)
    except ValueError as error:
        raise ManifestError(f"unsupported document format: {selected}") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=ROOT / "benchmarks" / "results" / "corpora",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--list", action="store_true")
    action.add_argument("--prepare", metavar="CORPUS_ID")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        if args.list:
            for corpus in manifest.corpora:
                print(f"{corpus.id}\t{corpus.tier}\t{corpus.source}\t{corpus.counts.bytes}")
            return 0
        if args.check:
            for corpus in manifest.corpora:
                if corpus.source == "generated":
                    verify_generated(corpus)
            print(
                f"corpus manifest OK: {len(manifest.corpora)} entries, "
                f"sha256={manifest_fingerprint(args.manifest)}"
            )
            return 0
        selected = manifest.by_id(cast(str, args.prepare))
        destination = prepare_corpus(selected, args.cache_dir)
        print(f"prepared {selected.id}: {destination}")
        return 0
    except ManifestError as error:
        print(f"benchmark manifest error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_MANIFEST",
    "Corpus",
    "CorpusCounts",
    "CorpusManifest",
    "ManifestError",
    "generated_bytes",
    "load_manifest",
    "manifest_fingerprint",
    "prepare_corpus",
    "verify_generated",
    "verify_prepared",
]
