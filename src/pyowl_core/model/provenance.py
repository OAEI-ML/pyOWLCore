"""Nonstructural source-lexical provenance hooks for model values."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .base import StructuralNode


@dataclass(frozen=True, slots=True)
class LexicalToken:
    kind: str
    spelling: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("lexical token kind must be a nonempty string")
        if not isinstance(self.spelling, str):
            raise TypeError("lexical token spelling must be a string")


@dataclass(frozen=True, slots=True)
class LexicalRecord:
    structural_digest: bytes
    tokens: tuple[LexicalToken, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.structural_digest, bytes) or len(self.structural_digest) != 32:
            raise ValueError("structural_digest must be exactly 32 bytes")
        tokens = tuple(self.tokens)
        if not all(isinstance(token, LexicalToken) for token in tokens):
            raise TypeError("tokens must contain LexicalToken values")
        object.__setattr__(self, "tokens", tokens)


@dataclass(frozen=True, slots=True)
class LexicalProvenance:
    records: tuple[LexicalRecord, ...] = ()

    def __post_init__(self) -> None:
        records = tuple(self.records)
        if not all(isinstance(record, LexicalRecord) for record in records):
            raise TypeError("records must contain LexicalRecord values")
        object.__setattr__(self, "records", records)

    def tokens_for(self, value: StructuralNode) -> tuple[LexicalToken, ...]:
        from .canonical import structural_digest

        digest = structural_digest(value)
        return tuple(
            token
            for record in self.records
            if record.structural_digest == digest
            for token in record.tokens
        )


class LexicalProvenanceBuilder:
    """Document-scoped mutable collector; frozen values never point back to it."""

    __slots__ = ("_tokens",)

    def __init__(self) -> None:
        self._tokens: dict[bytes, list[LexicalToken]] = {}

    def attach(self, value: StructuralNode, kind: str, spelling: str) -> None:
        from .canonical import structural_digest

        if not isinstance(value, StructuralNode):
            raise TypeError("value must be a StructuralNode")
        token = LexicalToken(kind, spelling)
        self._tokens.setdefault(structural_digest(value), []).append(token)

    def extend(self, value: StructuralNode, tokens: Iterable[LexicalToken]) -> None:
        for token in tokens:
            if not isinstance(token, LexicalToken):
                raise TypeError("tokens must contain LexicalToken values")
            self.attach(value, token.kind, token.spelling)

    def freeze(self) -> LexicalProvenance:
        records = tuple(
            LexicalRecord(digest, tuple(tokens)) for digest, tokens in sorted(self._tokens.items())
        )
        return LexicalProvenance(records)


@dataclass(frozen=True, slots=True)
class RescopeRecord:
    old_scope: bytes
    new_scope: bytes
    old_local_key: bytes
    new_local_key: bytes

    def __post_init__(self) -> None:
        for field, value in (("old_scope", self.old_scope), ("new_scope", self.new_scope)):
            if not isinstance(value, bytes) or len(value) != 32:
                raise ValueError(f"{field} must be exactly 32 bytes")
        for field, value in (
            ("old_local_key", self.old_local_key),
            ("new_local_key", self.new_local_key),
        ):
            if not isinstance(value, bytes) or not value:
                raise ValueError(f"{field} must be nonempty bytes")


__all__ = [
    "LexicalProvenance",
    "LexicalProvenanceBuilder",
    "LexicalRecord",
    "LexicalToken",
    "RescopeRecord",
]
