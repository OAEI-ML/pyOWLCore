"""Allowlisted, strategy-driven local directory import resolver."""

from __future__ import annotations

import hashlib
import os
import stat
from enum import Enum
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pyowl_core.exceptions import AccessDeniedError, ResourceLimitError
from pyowl_core.model import IRI

from .base import (
    ImportRequest,
    ResolutionMode,
    ResolvedDocument,
    ResolverOutcome,
    framed_text,
)


class DirectoryNamingStrategy(str, Enum):
    BASENAME = "basename"
    RELATIVE = "relative"
    SHA256 = "sha256"


class DirectoryResolver:
    """Map import IRIs below one real directory without path escape."""

    __slots__ = ("_allow_symlinks", "_iri_prefix", "_root", "_strategy", "_suffix")
    name = "directory"
    network_capable = False

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        strategy: DirectoryNamingStrategy | str = DirectoryNamingStrategy.BASENAME,
        iri_prefix: IRI | str | None = None,
        suffix: str = ".owl",
        allow_symlinks: bool = False,
    ) -> None:
        selected = (
            strategy
            if isinstance(strategy, DirectoryNamingStrategy)
            else DirectoryNamingStrategy(strategy)
        )
        resolved_root = Path(root).expanduser().resolve(strict=True)
        if not resolved_root.is_dir():
            raise ValueError("root must identify a directory")
        if not isinstance(suffix, str) or not suffix or "/" in suffix or "\\" in suffix:
            raise ValueError("suffix must be a nonempty filename suffix")
        if not isinstance(allow_symlinks, bool):
            raise TypeError("allow_symlinks must be bool")
        prefix = (
            None
            if iri_prefix is None
            else (iri_prefix.value if isinstance(iri_prefix, IRI) else iri_prefix)
        )
        if selected is DirectoryNamingStrategy.RELATIVE and not prefix:
            raise ValueError("relative strategy requires iri_prefix")
        self._root = resolved_root
        self._strategy = selected
        self._iri_prefix = prefix
        self._suffix = suffix
        self._allow_symlinks = allow_symlinks

    def resolve(self, request: ImportRequest) -> ResolvedDocument | None:
        return self.resolve_outcome(request, mode=ResolutionMode.LOCAL_ONLY).resolved

    def resolve_outcome(self, request: ImportRequest, *, mode: ResolutionMode) -> ResolverOutcome:
        del mode
        candidate = self._candidate(request.import_iri)
        if candidate is None or not candidate.exists():
            return ResolverOutcome.missing(self.name)
        if not self._allow_symlinks and _contains_symlink(self._root, candidate):
            raise AccessDeniedError(
                "directory resolver rejected a symlink", code="IMPORT_PATH_SYMLINK"
            )
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self._root)
        except (OSError, ValueError) as error:
            raise AccessDeniedError(
                "directory resolver path escapes its allowlisted root",
                code="IMPORT_PATH_ESCAPE",
            ) from error
        data = _read_regular(resolved, request)
        return ResolverOutcome.success(
            self.name,
            ResolvedDocument(
                data,
                IRI(resolved.as_uri()),
                provenance={
                    "resolver": self.name,
                    "locator": resolved.relative_to(self._root).as_posix(),
                },
            ),
        )

    def configuration_bytes(self) -> bytes:
        # The machine-local root is acquisition location, not structural configuration.
        return (
            b"directory:v1"
            + framed_text(self._strategy.value)
            + framed_text(self._iri_prefix or "")
            + framed_text(self._suffix)
            + bytes((int(self._allow_symlinks),))
        )

    def _candidate(self, iri: IRI) -> Path | None:
        if self._strategy is DirectoryNamingStrategy.SHA256:
            name = hashlib.sha256(iri.value.encode("utf-8")).hexdigest() + self._suffix
            return self._root / name
        if self._strategy is DirectoryNamingStrategy.BASENAME:
            split = urlsplit(iri.value)
            raw = split.path.rsplit("/", 1)[-1]
            if not raw:
                return None
            safe_name = _safe_segment(raw)
            return None if safe_name is None else self._root / safe_name
        prefix = self._iri_prefix
        if prefix is None or not iri.value.startswith(prefix):
            return None
        tail = iri.value[len(prefix) :].lstrip("/")
        if not tail:
            return None
        segments = tuple(_safe_segment(part) for part in tail.split("/"))
        if any(part is None for part in segments):
            raise AccessDeniedError(
                "directory resolver rejected an unsafe relative IRI",
                code="IMPORT_PATH_ESCAPE",
            )
        return self._root.joinpath(*(part for part in segments if part is not None))


def _safe_segment(raw: str) -> str | None:
    try:
        decoded = unquote(raw, errors="strict")
    except UnicodeError:
        return None
    if decoded in {"", ".", ".."} or any(character in decoded for character in "/\\\x00"):
        return None
    return decoded


def _contains_symlink(root: Path, candidate: Path) -> bool:
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _read_regular(path: Path, request: ImportRequest) -> bytes:
    maximum = request.limits.max_source_bytes
    chunks: list[bytes] = []
    total = 0
    with path.open("rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise AccessDeniedError(
                "directory resolver target is not a regular file",
                code="IMPORT_PATH_NOT_REGULAR",
            )
        if metadata.st_size > maximum:
            raise ResourceLimitError(
                "resource limit max_source_bytes exceeded",
                limit="max_source_bytes",
                observed=metadata.st_size,
                allowed=maximum,
            )
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            request.limits.enforce("max_source_bytes", total)
            chunks.append(chunk)
    return b"".join(chunks)


__all__ = ["DirectoryNamingStrategy", "DirectoryResolver"]
