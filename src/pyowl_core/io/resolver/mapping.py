"""Exact, immutable import-IRI mappings with explicit alias handling."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import BinaryIO, TypeAlias

from pyowl_core.exceptions import ImportCycleError
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

MappingTarget: TypeAlias = (
    ResolvedDocument | IRI | bytes | bytearray | memoryview | str | os.PathLike[str] | BinaryIO
)


@dataclass(frozen=True, slots=True)
class _Entry:
    target: MappingTarget


class MappingResolver:
    """Resolve exact import IRIs from an immutable application mapping."""

    __slots__ = ("_entries",)
    name = "mapping"
    network_capable = False

    def __init__(self, mappings: Mapping[IRI | str, MappingTarget]) -> None:
        if not isinstance(mappings, Mapping):
            raise TypeError("mappings must be a mapping")
        entries: dict[IRI, _Entry] = {}
        for key, target in mappings.items():
            iri = key if isinstance(key, IRI) else IRI(key)
            _validate_target(target)
            entries[iri] = _Entry(target)
        self._entries = tuple(sorted(entries.items(), key=lambda item: item[0].canonical_bytes()))

    def resolve(self, request: ImportRequest) -> ResolvedDocument | None:
        return self.resolve_outcome(request, mode=ResolutionMode.LOCAL_ONLY).resolved

    def resolve_outcome(self, request: ImportRequest, *, mode: ResolutionMode) -> ResolverOutcome:
        del mode
        entries = dict(self._entries)
        current = request.import_iri
        visited: list[IRI] = []
        attempts: list[ResolutionAttempt] = []
        for _ in range(request.limits.max_catalog_rewrites + 1):
            if current in visited:
                raise ImportCycleError(
                    "mapping resolver alias cycle",
                    code="IMPORT_ALIAS_CYCLE",
                ) from None
            visited.append(current)
            entry = entries.get(current)
            if entry is None:
                attempts.append(ResolutionAttempt(self.name, ResolutionKind.NOT_FOUND))
                return ResolverOutcome.missing(self.name, attempts=tuple(attempts))
            target = entry.target
            if isinstance(target, IRI):
                attempts.append(
                    ResolutionAttempt(self.name, ResolutionKind.NOT_FOUND, "IMPORT_ALIAS")
                )
                current = target
                continue
            resolved = _as_resolved(current, target)
            attempts.append(ResolutionAttempt(self.name, ResolutionKind.RESOLVED))
            return ResolverOutcome.success(self.name, resolved, attempts=tuple(attempts))
        raise ImportCycleError(
            "mapping resolver rewrite limit exceeded",
            code="IMPORT_ALIAS_LIMIT",
        )

    def configuration_bytes(self) -> bytes:
        pieces = [b"mapping:v1", encode_varint(len(self._entries))]
        for iri, entry in self._entries:
            pieces.append(framed_text(iri.value))
            pieces.append(_target_configuration(entry.target))
        return b"".join(pieces)


def _validate_target(target: MappingTarget) -> None:
    if isinstance(target, (ResolvedDocument, IRI, bytes, bytearray, memoryview, str, os.PathLike)):
        return
    if callable(getattr(target, "read", None)):
        return
    raise TypeError("mapping targets must be a resolved document, alias, bytes, path, or BinaryIO")


def _as_resolved(import_iri: IRI, target: MappingTarget) -> ResolvedDocument:
    if isinstance(target, ResolvedDocument):
        return target
    if isinstance(target, IRI):
        raise TypeError("an alias target cannot be converted to a document")
    if isinstance(target, (bytearray, memoryview)):
        source: bytes | str | os.PathLike[str] | BinaryIO = bytes(target)
    else:
        source = target
    return ResolvedDocument(source, import_iri, provenance={"resolver": "mapping"})


def _target_configuration(target: MappingTarget) -> bytes:
    if isinstance(target, IRI):
        return b"A" + framed_text(target.value)
    if isinstance(target, ResolvedDocument):
        digest = target.expected_sha256
        return (
            b"R"
            + framed_text(target.document_iri.value)
            + (b"0" if target.format is None else b"1" + framed_text(target.format.value))
            + (b"0" if digest is None else b"1" + digest)
            + _source_configuration(target.source)
        )
    return b"S" + _source_configuration(target)


def _source_configuration(source: object) -> bytes:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return b"B" + hashlib.sha256(bytes(source)).digest()
    if isinstance(source, (str, os.PathLike)):
        # The locator is configuration, but credentials are never accepted in a path.
        return b"P" + framed_text(os.fspath(source))
    return b"T" + framed_text(type(source).__qualname__)


__all__ = ["MappingResolver", "MappingTarget"]
