"""Delta-debug one retained hostile input while preserving its failure class."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path

from pyowl_core import PyOWLCoreError, decode_snapshot, parse_document

Predicate = Callable[[bytes], bool]


def minimize(data: bytes, predicate: Predicate) -> bytes:
    """Return a deterministic 1-minimal subsequence accepted by ``predicate``."""

    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if not callable(predicate):
        raise TypeError("predicate must be callable")
    if not predicate(data):
        raise ValueError("the initial input does not preserve the requested failure")
    current = data
    granularity = 2
    while len(current) >= 2:
        width = max(1, (len(current) + granularity - 1) // granularity)
        reduced = False
        for start in range(0, len(current), width):
            candidate = current[:start] + current[start + width :]
            if predicate(candidate):
                current = candidate
                granularity = max(2, granularity - 1)
                reduced = True
                break
        if not reduced:
            if granularity >= len(current):
                break
            granularity = min(len(current), granularity * 2)
    return current


def _failure_predicate(kind: str, format: str | None, baseline: bytes) -> Predicate:
    def operation(data: bytes) -> object:
        if kind == "wire":
            return decode_snapshot(data)
        return parse_document(data, format=format, document_iri="urn:pyowl-core:fuzz")

    try:
        operation(baseline)
    except PyOWLCoreError as error:
        expected = type(error), error.code
    else:
        raise ValueError("input does not produce a pyowl-core failure")

    def preserves(data: bytes) -> bool:
        try:
            operation(data)
        except PyOWLCoreError as error:
            return (type(error), error.code) == expected
        return False

    return preserves


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("parser", "wire"))
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--format", choices=("functional", "owlxml", "rdfxml", "turtle"))
    args = parser.parse_args(argv)
    if args.kind == "parser" and args.format is None:
        parser.error("parser minimization requires --format")
    data = args.input.read_bytes()
    result = minimize(data, _failure_predicate(args.kind, args.format, data))
    args.output.write_bytes(result)
    print(f"minimized {len(data)} -> {len(result)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["Predicate", "main", "minimize"]
