"""Immutable one-document acquisition, source-map, and RDF mapping evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from pyowl_core._immutable import FrozenMap, freeze_mapping
from pyowl_core.config import DocumentFormat
from pyowl_core.diagnostics import Diagnostic, SourceSpan
from pyowl_core.model import IRI


class DigestKind(str, Enum):
    EXACT_BYTES = "exact-bytes"
    NORMALIZED_TEXT = "normalized-text"


class DetectionBasis(str, Enum):
    EXPLICIT = "explicit"
    MEDIA_TYPE = "media-type"
    CONTENT = "content"
    EXTENSION = "extension"


@dataclass(frozen=True, slots=True)
class DocumentProvenance:
    source_sha256: bytes
    digest_kind: DigestKind
    byte_length: int
    decoded_codepoint_length: int
    document_iri: IRI | None
    acquisition_locator: str | None
    format: DocumentFormat
    detection_basis: DetectionBasis
    media_type: str | None = None
    expected_sha256: bytes | None = None
    parser: str = "pyowl_core.backends.python"
    backend: str = "python"
    api_version: tuple[int, int] = (0, 2)
    model_schema: int = 2

    def __post_init__(self) -> None:
        if not isinstance(self.source_sha256, bytes) or len(self.source_sha256) != 32:
            raise ValueError("source_sha256 must be exactly 32 bytes")
        if not isinstance(self.digest_kind, DigestKind):
            raise TypeError("digest_kind must be DigestKind")
        for name in ("byte_length", "decoded_codepoint_length"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.acquisition_locator is not None and not isinstance(self.acquisition_locator, str):
            raise TypeError("acquisition_locator must be str or None")
        if self.document_iri is not None and not isinstance(self.document_iri, IRI):
            raise TypeError("document_iri must be IRI or None")
        if not isinstance(self.format, DocumentFormat):
            raise TypeError("format must be DocumentFormat")
        if not isinstance(self.detection_basis, DetectionBasis):
            raise TypeError("detection_basis must be DetectionBasis")
        if self.expected_sha256 is not None and (
            not isinstance(self.expected_sha256, bytes) or len(self.expected_sha256) != 32
        ):
            raise ValueError("expected_sha256 must be exactly 32 bytes or None")


@dataclass(frozen=True, slots=True, order=True)
class SourceOccurrence:
    occurrence: int
    span: SourceSpan | None = None
    lexical: Mapping[str, str] = field(default_factory=FrozenMap, compare=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.occurrence, bool)
            or not isinstance(self.occurrence, int)
            or self.occurrence < 0
        ):
            raise ValueError("occurrence must be a nonnegative integer")
        if self.span is not None and not isinstance(self.span, SourceSpan):
            raise TypeError("span must be SourceSpan or None")
        lexical: dict[str, str] = {}
        for key, value in self.lexical.items():
            if not isinstance(key, str) or not key or not isinstance(value, str):
                raise TypeError("lexical details must map nonempty strings to strings")
            lexical[key] = value
        object.__setattr__(self, "lexical", freeze_mapping(lexical))


@dataclass(frozen=True, slots=True)
class SourceMap:
    """Bounded mapping from structural digests to source occurrences."""

    entries: Mapping[bytes, tuple[SourceOccurrence, ...]] = field(default_factory=FrozenMap)
    prefixes: Mapping[str, str] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        entries: dict[bytes, tuple[SourceOccurrence, ...]] = {}
        for digest, occurrences in self.entries.items():
            if not isinstance(digest, bytes) or len(digest) != 32:
                raise ValueError("source-map keys must be 32-byte structural digests")
            frozen = tuple(occurrences)
            if not all(isinstance(item, SourceOccurrence) for item in frozen):
                raise TypeError("source-map entries must contain SourceOccurrence values")
            entries[digest] = frozen
        prefixes: dict[str, str] = {}
        for prefix, iri in self.prefixes.items():
            if not isinstance(prefix, str) or not isinstance(iri, str):
                raise TypeError("prefixes must map strings to strings")
            prefixes[prefix] = iri
        object.__setattr__(self, "entries", freeze_mapping(entries))
        object.__setattr__(self, "prefixes", freeze_mapping(prefixes))

    def occurrences_for(self, value: object) -> tuple[SourceOccurrence, ...]:
        from pyowl_core.model import StructuralNode, structural_digest

        if not isinstance(value, StructuralNode):
            raise TypeError("value must be a StructuralNode")
        return self.entries.get(structural_digest(value), ())


@dataclass(frozen=True, slots=True, order=True)
class OriginOccurrence:
    document_key: str
    occurrence: int
    span: SourceSpan | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.document_key, str) or not self.document_key:
            raise ValueError("document_key must be a nonempty string")
        if (
            isinstance(self.occurrence, bool)
            or not isinstance(self.occurrence, int)
            or self.occurrence < 0
        ):
            raise ValueError("occurrence must be a nonnegative integer")
        if self.span is not None and not isinstance(self.span, SourceSpan):
            raise TypeError("span must be SourceSpan or None")


@dataclass(frozen=True, slots=True)
class OriginIndex:
    entries: Mapping[bytes, tuple[OriginOccurrence, ...]] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        entries: dict[bytes, tuple[OriginOccurrence, ...]] = {}
        for digest, occurrences in self.entries.items():
            if not isinstance(digest, bytes) or len(digest) != 32:
                raise ValueError("origin keys must be 32-byte structural digests")
            frozen = tuple(occurrences)
            if not all(isinstance(item, OriginOccurrence) for item in frozen):
                raise TypeError("origin entries must contain OriginOccurrence values")
            entries[digest] = frozen
        object.__setattr__(self, "entries", freeze_mapping(entries))

    def origins_for(self, value: object) -> tuple[OriginOccurrence, ...]:
        from pyowl_core.model import StructuralNode, structural_digest

        if not isinstance(value, StructuralNode):
            raise TypeError("value must be a StructuralNode")
        return self.entries.get(structural_digest(value), ())


@dataclass(frozen=True, slots=True, order=True)
class RDFTripleEvidence:
    subject: str
    predicate: str
    object: str
    object_kind: str = "literal"

    def __post_init__(self) -> None:
        for name in ("subject", "predicate", "object"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a nonempty string")
        if self.object_kind not in {"iri", "blank", "literal"}:
            raise ValueError("object_kind must be 'iri', 'blank', or 'literal'")


@dataclass(frozen=True, slots=True)
class RDFMappingReport:
    conformant: bool
    consumed_triples: int
    total_triples: int
    unconsumed: tuple[RDFTripleEvidence, ...] = ()
    rule_ids: tuple[str, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def dropped_triples(self) -> int:
        """Return the exact number of graph statements omitted by mapping."""

        return self.total_triples - self.consumed_triples

    def __post_init__(self) -> None:
        for name in ("consumed_triples", "total_triples"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.consumed_triples > self.total_triples:
            raise ValueError("consumed_triples cannot exceed total_triples")
        if self.conformant != (self.consumed_triples == self.total_triples):
            raise ValueError("conformant must agree with the mapping counts")
        unconsumed = tuple(self.unconsumed)
        rules = tuple(self.rule_ids)
        diagnostics = tuple(self.diagnostics)
        if not all(isinstance(item, RDFTripleEvidence) for item in unconsumed):
            raise TypeError("unconsumed must contain RDFTripleEvidence")
        if not all(isinstance(item, str) and item for item in rules):
            raise TypeError("rule_ids must contain nonempty strings")
        if not all(isinstance(item, Diagnostic) for item in diagnostics):
            raise TypeError("diagnostics must contain Diagnostic values")
        object.__setattr__(self, "unconsumed", unconsumed)
        object.__setattr__(self, "rule_ids", tuple(sorted(set(rules))))
        object.__setattr__(self, "diagnostics", diagnostics)


class SourceMapBuilder:
    __slots__ = ("_entries", "_prefixes")

    def __init__(self, prefixes: Mapping[str, str] | None = None) -> None:
        self._entries: dict[bytes, list[SourceOccurrence]] = {}
        self._prefixes = dict(prefixes or {})

    def add(
        self,
        value: object,
        occurrence: int,
        span: SourceSpan | None = None,
        lexical: Mapping[str, str] | None = None,
    ) -> None:
        from pyowl_core.model import StructuralNode, structural_digest

        if not isinstance(value, StructuralNode):
            raise TypeError("value must be a StructuralNode")
        item = SourceOccurrence(occurrence, span, lexical or {})
        self._entries.setdefault(structural_digest(value), []).append(item)

    def add_digest(
        self,
        digest: bytes,
        occurrence: int,
        span: SourceSpan | None = None,
        lexical: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(digest, bytes) or len(digest) != 32:
            raise ValueError("digest must be exactly 32 bytes")
        self._entries.setdefault(digest, []).append(
            SourceOccurrence(occurrence, span, lexical or {})
        )

    def freeze(self) -> SourceMap:
        return SourceMap(
            {key: tuple(values) for key, values in self._entries.items()},
            self._prefixes,
        )


class OriginIndexBuilder:
    __slots__ = ("_document_key", "_entries")

    def __init__(self, document_key: str) -> None:
        self._document_key = document_key
        self._entries: dict[bytes, list[OriginOccurrence]] = {}

    def add(self, value: object, occurrence: int, span: SourceSpan | None = None) -> None:
        from pyowl_core.model import StructuralNode, structural_digest

        if not isinstance(value, StructuralNode):
            raise TypeError("value must be a StructuralNode")
        self._entries.setdefault(structural_digest(value), []).append(
            OriginOccurrence(self._document_key, occurrence, span)
        )

    def add_digest(self, digest: bytes, occurrence: int, span: SourceSpan | None = None) -> None:
        if not isinstance(digest, bytes) or len(digest) != 32:
            raise ValueError("digest must be exactly 32 bytes")
        self._entries.setdefault(digest, []).append(
            OriginOccurrence(self._document_key, occurrence, span)
        )

    def freeze(self) -> OriginIndex:
        return OriginIndex({key: tuple(values) for key, values in self._entries.items()})


__all__ = [
    "DetectionBasis",
    "DigestKind",
    "DocumentProvenance",
    "OriginIndex",
    "OriginIndexBuilder",
    "OriginOccurrence",
    "RDFMappingReport",
    "RDFTripleEvidence",
    "SourceMap",
    "SourceMapBuilder",
    "SourceOccurrence",
]
