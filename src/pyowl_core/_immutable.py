"""Small immutable containers used by the public foundation contracts."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Generic, TypeVar

_K = TypeVar("_K")
_V = TypeVar("_V")


def _deterministic_key(value: object) -> tuple[str, str]:
    """Order byte keys by their bytes while retaining a total generic fallback."""

    encoded = value.hex() if isinstance(value, bytes) else repr(value)
    return type(value).__qualname__, encoded


class FrozenMap(Mapping[_K, _V], Generic[_K, _V]):
    """A deterministic, recursively caller-isolated read-only mapping.

    Values are expected to be immutable. The class copies the incoming mapping,
    so later caller mutation cannot alter a frozen public value.
    """

    __slots__ = ("_data", "_hash", "_items")

    def __init__(
        self,
        values: Mapping[_K, _V] | Iterable[tuple[_K, _V]] | None = None,
    ) -> None:
        data = dict(values or ())
        items = tuple(
            sorted(
                data.items(),
                key=lambda item: _deterministic_key(item[0]),
            )
        )
        self._data = data
        self._items = items
        self._hash: int | None = None

    def __getitem__(self, key: _K) -> _V:
        return self._data[key]

    def __iter__(self) -> Iterator[_K]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __hash__(self) -> int:
        cached = self._hash
        if cached is None:
            cached = hash(self._items)
            self._hash = cached
        return cached

    def __repr__(self) -> str:
        body = ", ".join(f"{key!r}: {value!r}" for key, value in self._items)
        return f"FrozenMap({{{body}}})"

    def __reduce__(self) -> tuple[type[FrozenMap[_K, _V]], tuple[dict[_K, _V]]]:
        return type(self), (dict(self._items),)


def freeze_mapping(
    values: Mapping[_K, _V] | Iterable[tuple[_K, _V]] | None = None,
) -> FrozenMap[_K, _V]:
    if isinstance(values, FrozenMap):
        return values
    return FrozenMap(values)
