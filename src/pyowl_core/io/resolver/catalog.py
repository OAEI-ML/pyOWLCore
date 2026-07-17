"""Secure XML/JSON catalog resolver for exact and prefix mappings."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pyowl_core.config import DocumentFormat
from pyowl_core.exceptions import AccessDeniedError, ImportCycleError, ImportResolutionError
from pyowl_core.limits import ParseLimits
from pyowl_core.model import IRI, encode_varint

from .base import (
    ImportRequest,
    ResolutionAttempt,
    ResolutionKind,
    ResolutionMode,
    ResolvedDocument,
    ResolverOutcome,
    framed_text,
)

_CATALOG_MAX_BYTES = 16 * 1024**2
_FORBIDDEN_XML = (b"<!DOCTYPE", b"<!ENTITY", b"<xi:include", b"<xinclude")


@dataclass(frozen=True, slots=True)
class _Target:
    path: Path | None = None
    alias: IRI | None = None
    document_iri: IRI | None = None
    format: DocumentFormat | None = None
    expected_sha256: bytes | None = None

    def __post_init__(self) -> None:
        if (self.path is None) == (self.alias is None):
            raise ValueError("catalog target must contain exactly one path or alias")


@dataclass(frozen=True, slots=True)
class _Rewrite:
    prefix: str
    replacement: str
    base: Path


class CatalogResolver:
    """Resolve OASIS-style XML or documented JSON catalog entries locally."""

    __slots__ = ("_catalog_digest", "_exact", "_rewrites")
    name = "catalog"
    network_capable = False

    def __init__(
        self,
        catalog: str | os.PathLike[str] | bytes,
        *,
        base_dir: str | os.PathLike[str] | None = None,
        limits: ParseLimits | None = None,
    ) -> None:
        selected_limits = ParseLimits() if limits is None else limits
        if not isinstance(selected_limits, ParseLimits):
            raise TypeError("limits must be ParseLimits or None")
        exact: dict[IRI, _Target] = {}
        rewrites: list[_Rewrite] = []
        digests: list[bytes] = []
        if isinstance(catalog, bytes):
            if len(catalog) > min(_CATALOG_MAX_BYTES, selected_limits.max_source_bytes):
                raise ImportResolutionError(
                    "catalog exceeds its source limit", code="CATALOG_SOURCE_LIMIT"
                )
            base = Path.cwd() if base_dir is None else Path(base_dir).resolve(strict=True)
            _parse_catalog_bytes(
                catalog,
                base,
                exact,
                rewrites,
                selected_limits,
                (),
                digests,
            )
        else:
            path = Path(catalog).expanduser().resolve(strict=True)
            _load_catalog_path(
                path,
                exact,
                rewrites,
                selected_limits,
                (),
                digests,
            )
        self._exact = tuple(sorted(exact.items(), key=lambda item: item[0].canonical_bytes()))
        self._rewrites = tuple(
            sorted(rewrites, key=lambda item: (-len(item.prefix), item.prefix, item.replacement))
        )
        hasher = hashlib.sha256(b"pyowl-core:catalog-content:v1\x00")
        for digest in sorted(digests):
            hasher.update(digest)
        self._catalog_digest = hasher.digest()

    def resolve(self, request: ImportRequest) -> ResolvedDocument | None:
        return self.resolve_outcome(request, mode=ResolutionMode.LOCAL_ONLY).resolved

    def resolve_outcome(self, request: ImportRequest, *, mode: ResolutionMode) -> ResolverOutcome:
        del mode
        exact = dict(self._exact)
        current = request.import_iri
        visited: list[IRI] = []
        attempts: list[ResolutionAttempt] = []
        for _ in range(request.limits.max_catalog_rewrites + 1):
            if current in visited:
                raise ImportCycleError(
                    "catalog alias cycle",
                    code="IMPORT_ALIAS_CYCLE",
                )
            visited.append(current)
            target = exact.get(current)
            if target is None:
                target = self._rewrite_target(current)
            if target is None:
                attempts.append(ResolutionAttempt(self.name, ResolutionKind.NOT_FOUND))
                return ResolverOutcome.missing(self.name, attempts=tuple(attempts))
            if target.alias is not None:
                attempts.append(
                    ResolutionAttempt(self.name, ResolutionKind.NOT_FOUND, "IMPORT_ALIAS")
                )
                current = target.alias
                continue
            path = target.path
            if path is None:
                raise AssertionError("catalog target has no path")
            if not path.exists():
                attempts.append(ResolutionAttempt(self.name, ResolutionKind.NOT_FOUND))
                return ResolverOutcome.missing(self.name, attempts=tuple(attempts))
            try:
                canonical = path.resolve(strict=True)
            except OSError as error:
                raise AccessDeniedError(
                    "catalog target cannot be opened", code="CATALOG_TARGET_DENIED"
                ) from error
            attempts.append(ResolutionAttempt(self.name, ResolutionKind.RESOLVED))
            return ResolverOutcome.success(
                self.name,
                ResolvedDocument(
                    canonical,
                    target.document_iri or IRI(canonical.as_uri()),
                    target.format,
                    target.expected_sha256,
                    {
                        "resolver": self.name,
                        "catalog_digest": self._catalog_digest.hex(),
                        "locator": canonical.name,
                    },
                ),
                attempts=tuple(attempts),
            )
        raise ImportCycleError(
            "catalog rewrite limit exceeded",
            code="IMPORT_ALIAS_LIMIT",
        )

    def configuration_bytes(self) -> bytes:
        pieces = [b"catalog:v1", self._catalog_digest, encode_varint(len(self._exact))]
        for iri, target in self._exact:
            pieces.extend((framed_text(iri.value), _target_bytes(target)))
        pieces.append(encode_varint(len(self._rewrites)))
        for rewrite in self._rewrites:
            pieces.extend((framed_text(rewrite.prefix), framed_text(rewrite.replacement)))
        return b"".join(pieces)

    def _rewrite_target(self, iri: IRI) -> _Target | None:
        for rewrite in self._rewrites:
            if not iri.value.startswith(rewrite.prefix):
                continue
            replacement = rewrite.replacement + iri.value[len(rewrite.prefix) :]
            return _parse_target(replacement, rewrite.base)
        return None


def _load_catalog_path(
    path: Path,
    exact: dict[IRI, _Target],
    rewrites: list[_Rewrite],
    limits: ParseLimits,
    stack: tuple[Path, ...],
    digests: list[bytes],
) -> None:
    canonical = path.resolve(strict=True)
    if canonical in stack:
        raise ImportCycleError("catalog include cycle", code="CATALOG_INCLUDE_CYCLE")
    if len(stack) >= limits.max_catalog_rewrites:
        raise ImportCycleError("catalog include limit exceeded", code="CATALOG_INCLUDE_LIMIT")
    data = _read_catalog(canonical, limits)
    _parse_catalog_bytes(
        data,
        canonical.parent,
        exact,
        rewrites,
        limits,
        (*stack, canonical),
        digests,
    )


def _parse_catalog_bytes(
    data: bytes,
    base: Path,
    exact: dict[IRI, _Target],
    rewrites: list[_Rewrite],
    limits: ParseLimits,
    stack: tuple[Path, ...],
    digests: list[bytes],
) -> None:
    digests.append(hashlib.sha256(data).digest())
    stripped = data.lstrip()
    if stripped.startswith((b"{", b"[")):
        _parse_json(data, base, exact, rewrites, limits, stack, digests)
        return
    lowered = data.lower()
    if any(marker.lower() in lowered for marker in _FORBIDDEN_XML):
        raise AccessDeniedError(
            "catalog XML contains a forbidden DTD, entity, or include",
            code="CATALOG_XML_FORBIDDEN",
        )
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise ImportResolutionError("malformed XML catalog", code="CATALOG_MALFORMED") from error
    for count, element in enumerate(root.iter(), 1):
        limits.enforce("max_terms", count)
        name = element.tag.rsplit("}", 1)[-1]
        if name == "uri":
            source = element.attrib.get("name")
            target = element.attrib.get("uri")
            if source and target:
                exact[IRI(source)] = _parse_target(target, base)
        elif name == "rewriteURI":
            prefix = element.attrib.get("uriStartString")
            replacement = element.attrib.get("rewritePrefix")
            if prefix is not None and replacement is not None:
                rewrites.append(_Rewrite(prefix, replacement, base))
        elif name == "nextCatalog":
            nested = element.attrib.get("catalog")
            if nested:
                _load_catalog_path(
                    _local_path(nested, base), exact, rewrites, limits, stack, digests
                )


def _read_catalog(path: Path, limits: ParseLimits) -> bytes:
    maximum = min(_CATALOG_MAX_BYTES, limits.max_source_bytes)
    chunks: list[bytes] = []
    total = 0
    with path.open("rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise AccessDeniedError(
                "catalog source is not a regular file",
                code="CATALOG_NOT_REGULAR",
            )
        if metadata.st_size > maximum:
            raise ImportResolutionError(
                "catalog exceeds its source limit",
                code="CATALOG_SOURCE_LIMIT",
            )
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise ImportResolutionError(
                    "catalog exceeds its source limit",
                    code="CATALOG_SOURCE_LIMIT",
                )
            chunks.append(chunk)
    return b"".join(chunks)


def _parse_json(
    data: bytes,
    base: Path,
    exact: dict[IRI, _Target],
    rewrites: list[_Rewrite],
    limits: ParseLimits,
    stack: tuple[Path, ...],
    digests: list[bytes],
) -> None:
    try:
        decoded = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ImportResolutionError("malformed JSON catalog", code="CATALOG_MALFORMED") from error
    if not isinstance(decoded, Mapping):
        raise ImportResolutionError("JSON catalog root must be an object", code="CATALOG_MALFORMED")
    mappings = decoded.get("mappings", {})
    if isinstance(mappings, Mapping):
        entries = tuple(mappings.items())
    elif isinstance(mappings, list):
        entries = tuple(_json_entry(item) for item in mappings)
    else:
        raise ImportResolutionError(
            "catalog mappings must be an object or list", code="CATALOG_MALFORMED"
        )
    for index, pair in enumerate(entries, 1):
        limits.enforce("max_terms", index)
        source, value = pair
        if not isinstance(source, str):
            raise ImportResolutionError(
                "catalog mapping IRI must be text", code="CATALOG_MALFORMED"
            )
        exact[IRI(source)] = _json_target(value, base)
    raw_rewrites = decoded.get("rewrites", [])
    if not isinstance(raw_rewrites, list):
        raise ImportResolutionError("catalog rewrites must be a list", code="CATALOG_MALFORMED")
    for item in raw_rewrites:
        if not isinstance(item, Mapping):
            raise ImportResolutionError(
                "catalog rewrite must be an object", code="CATALOG_MALFORMED"
            )
        prefix = item.get("prefix")
        replacement = item.get("replacement")
        if not isinstance(prefix, str) or not isinstance(replacement, str):
            raise ImportResolutionError(
                "catalog rewrite fields must be text", code="CATALOG_MALFORMED"
            )
        rewrites.append(_Rewrite(prefix, replacement, base))
    nested_catalogs = decoded.get("next_catalogs", [])
    if not isinstance(nested_catalogs, list) or not all(
        isinstance(item, str) for item in nested_catalogs
    ):
        raise ImportResolutionError("next_catalogs must contain paths", code="CATALOG_MALFORMED")
    for nested in nested_catalogs:
        _load_catalog_path(_local_path(nested, base), exact, rewrites, limits, stack, digests)


def _json_entry(value: object) -> tuple[object, object]:
    if not isinstance(value, Mapping):
        raise ImportResolutionError("catalog mapping must be an object", code="CATALOG_MALFORMED")
    source = value.get("import_iri")
    return source, value


def _json_target(value: object, base: Path) -> _Target:
    if isinstance(value, str):
        return _parse_target(value, base)
    if not isinstance(value, Mapping):
        raise ImportResolutionError(
            "catalog target must be text or an object", code="CATALOG_MALFORMED"
        )
    raw = value.get("path", value.get("uri", value.get("alias")))
    if not isinstance(raw, str):
        raise ImportResolutionError(
            "catalog target is missing path/uri/alias", code="CATALOG_MALFORMED"
        )
    target = _parse_target(raw, base, force_alias="alias" in value)
    document_iri = value.get("document_iri")
    raw_format = value.get("format")
    raw_digest = value.get("sha256")
    return _Target(
        target.path,
        target.alias,
        target.document_iri if document_iri is None else IRI(_text(document_iri, "document_iri")),
        target.format if raw_format is None else DocumentFormat(_text(raw_format, "format")),
        target.expected_sha256 if raw_digest is None else _digest(_text(raw_digest, "sha256")),
    )


def _parse_target(raw: str, base: Path, *, force_alias: bool = False) -> _Target:
    if force_alias:
        return _Target(alias=IRI(raw))
    split = urlsplit(raw)
    if split.scheme and split.scheme != "file":
        return _Target(alias=IRI(raw))
    path = _local_path(raw, base)
    return _Target(path=path, document_iri=IRI(path.absolute().as_uri()))


def _local_path(raw: str, base: Path) -> Path:
    split = urlsplit(raw)
    if split.scheme not in {"", "file"}:
        raise AccessDeniedError("catalog target is not local", code="CATALOG_NETWORK_TARGET")
    if split.scheme == "file" and split.netloc not in {"", "localhost"}:
        raise AccessDeniedError("remote file catalog target denied", code="CATALOG_FILE_HOST")
    decoded = unquote(split.path, errors="strict")
    path = Path(decoded)
    if not path.is_absolute():
        path = base / path
    candidate = path.resolve(strict=False)
    try:
        candidate.relative_to(base.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise AccessDeniedError(
            "catalog path escapes its allowlisted base",
            code="CATALOG_PATH_ESCAPE",
        ) from error
    return candidate


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ImportResolutionError(f"catalog {field} must be text", code="CATALOG_MALFORMED")
    return value


def _digest(value: str) -> bytes:
    try:
        digest = bytes.fromhex(value)
    except ValueError as error:
        raise ImportResolutionError(
            "catalog sha256 is malformed", code="CATALOG_MALFORMED"
        ) from error
    if len(digest) != 32:
        raise ImportResolutionError("catalog sha256 must be 32 bytes", code="CATALOG_MALFORMED")
    return digest


def _target_bytes(target: _Target) -> bytes:
    if target.alias is not None:
        return b"A" + framed_text(target.alias.value)
    return (
        b"P"
        + framed_text("" if target.path is None else target.path.name)
        + framed_text("" if target.document_iri is None else target.document_iri.value)
        + framed_text("" if target.format is None else target.format.value)
        + (b"0" if target.expected_sha256 is None else b"1" + target.expected_sha256)
    )


__all__ = ["CatalogResolver"]
