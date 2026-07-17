"""Shared immutable value mechanics for the OWL 2 structural model."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Set
from functools import total_ordering
from typing import Generic, TypeVar, cast

from pyowl_core.exceptions import StructuralConstraintError

T = TypeVar("T", bound="StructuralNode")


def _canonical_bytes(value: StructuralNode) -> bytes:
    # A local import keeps the leaf value definitions independent of the
    # registry while still giving every value one equality/hash implementation.
    from .canonical import canonical_bytes

    return canonical_bytes(value)


@total_ordering
class StructuralNode:
    """Base for values whose equality is their canonical structural bytes."""

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, StructuralNode):
            return NotImplemented
        if self is other:
            return True
        return _canonical_bytes(self) == _canonical_bytes(other)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, StructuralNode):
            return NotImplemented
        return _canonical_bytes(self) < _canonical_bytes(other)

    def __hash__(self) -> int:
        digest = hashlib.sha256(b"pyowl-core:python-hash:v1\x00" + _canonical_bytes(self)).digest()
        value = int.from_bytes(digest[:8], "big", signed=True)
        return -2 if value == -1 else value

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self)


class CanonicalSet(Set[T], Generic[T]):
    """Duplicate-free immutable set with canonical deterministic iteration."""

    __slots__ = ("_cached_hash", "_items")

    def __init__(self, values: Iterable[T] = ()) -> None:
        unique: dict[bytes, T] = {}
        for value in values:
            if not isinstance(value, StructuralNode):
                raise TypeError("CanonicalSet values must be structural nodes")
            unique[_canonical_bytes(value)] = value
        self._items = tuple(unique[key] for key in sorted(unique))
        self._cached_hash: int | None = None

    def __contains__(self, value: object) -> bool:
        if not isinstance(value, StructuralNode):
            return False
        target = _canonical_bytes(value)
        lower = 0
        upper = len(self._items)
        while lower < upper:
            middle = (lower + upper) // 2
            candidate = _canonical_bytes(self._items[middle])
            if candidate < target:
                lower = middle + 1
            else:
                upper = middle
        return lower < len(self._items) and _canonical_bytes(self._items[lower]) == target

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CanonicalSet):
            return False
        return self._items == other._items

    def __iter__(self) -> Iterator[T]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __hash__(self) -> int:
        cached = self._cached_hash
        if cached is None:
            hasher = hashlib.sha256(b"pyowl-core:canonical-set:v1\x00")
            for item in self._items:
                encoded = _canonical_bytes(item)
                hasher.update(len(encoded).to_bytes(8, "big"))
                hasher.update(encoded)
            cached = int.from_bytes(hasher.digest()[:8], "big", signed=True)
            self._cached_hash = -2 if cached == -1 else cached
        return cached

    def __repr__(self) -> str:
        return f"CanonicalSet({self._items!r})"

    def as_tuple(self) -> tuple[T, ...]:
        return self._items


def canonical_set(
    values: Iterable[T],
    expected: type[StructuralNode] | tuple[type[StructuralNode], ...],
    field: str,
    *,
    minimum: int = 0,
    flatten: type[StructuralNode] | None = None,
    flatten_field: str = "operands",
) -> CanonicalSet[T]:
    expanded: list[T] = []
    for value in values:
        if not isinstance(value, expected):
            raise TypeError(f"{field} contains {type(value).__name__}; expected structural value")
        if flatten is not None and type(value) is flatten:
            nested = getattr(value, flatten_field)
            if not isinstance(nested, CanonicalSet):
                raise StructuralConstraintError(f"{field} has invalid nested canonical set")
            expanded.extend(cast(Iterable[T], nested))
        else:
            expanded.append(value)
    result = CanonicalSet(expanded)
    if len(result) < minimum:
        raise StructuralConstraintError(
            f"{field} requires at least {minimum} distinct value(s); got {len(result)}"
        )
    return result


def structural_tuple(
    values: Iterable[T],
    expected: type[StructuralNode] | tuple[type[StructuralNode], ...],
    field: str,
    *,
    minimum: int = 0,
) -> tuple[T, ...]:
    result = tuple(values)
    for value in result:
        if not isinstance(value, expected):
            raise TypeError(f"{field} contains {type(value).__name__}; expected structural value")
    if len(result) < minimum:
        raise StructuralConstraintError(
            f"{field} requires at least {minimum} value(s); got {len(result)}"
        )
    return result


def require_node(
    value: object,
    expected: type[T] | tuple[type[StructuralNode], ...],
    field: str,
) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{field} must be a structural {expected!r}")


def require_nonnegative_integer(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value < 0:
        raise StructuralConstraintError(f"{field} must be nonnegative")


def require_text(value: object, field: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if nonempty and not value:
        raise StructuralConstraintError(f"{field} must be nonempty")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise StructuralConstraintError(f"{field} contains an unpaired Unicode surrogate")


__all__ = [
    "CanonicalSet",
    "StructuralNode",
    "canonical_set",
    "require_node",
    "require_nonnegative_integer",
    "require_text",
    "structural_tuple",
]
