"""Deterministic bounded byte mutations shared by ordinary CI fuzz lanes."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Mutation:
    id: str
    data: bytes


def mutations(seed: bytes, *, maximum: int = 128) -> tuple[Mutation, ...]:
    if not isinstance(seed, bytes):
        raise TypeError("seed must be bytes")
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
        raise ValueError("maximum must be a positive integer")
    unique: dict[bytes, Mutation] = {}
    for item in _candidates(seed):
        unique.setdefault(item.data, item)
        if len(unique) == maximum:
            break
    return tuple(unique.values())


def _candidates(seed: bytes) -> Iterator[Mutation]:
    yield Mutation("empty", b"")
    for offset in _sample_offsets(len(seed), 32):
        yield Mutation(f"truncate-{offset}", seed[:offset])
    for offset in _sample_offsets(len(seed), 32):
        for mask in (0x01, 0x80):
            changed = bytearray(seed)
            changed[offset] ^= mask
            yield Mutation(f"xor-{offset}-{mask:02x}", bytes(changed))
    for offset in _sample_offsets(len(seed), 16):
        yield Mutation(f"delete-{offset}", seed[:offset] + seed[offset + 1 :])
        yield Mutation(f"insert-nul-{offset}", seed[:offset] + b"\0" + seed[offset:])
        yield Mutation(f"insert-ff-{offset}", seed[:offset] + b"\xff" + seed[offset:])


def _sample_offsets(length: int, count: int) -> tuple[int, ...]:
    if length < 1:
        return ()
    if length <= count:
        return tuple(range(length))
    return tuple(sorted({index * (length - 1) // (count - 1) for index in range(count)}))


__all__ = ["Mutation", "mutations"]
