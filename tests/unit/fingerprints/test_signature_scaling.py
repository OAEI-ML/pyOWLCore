from __future__ import annotations

from unittest.mock import patch

from pyowl_core import IRI, Class, StructuralNode, SubClassOf, signature_fingerprint
from pyowl_core.model.visitor import _collect_signature


def test_duplicate_heavy_signature_collection_avoids_structural_hashing() -> None:
    left = Class(IRI("urn:signature:left"))
    right = Class(IRI("urn:signature:right"))
    roots = tuple(SubClassOf(left, right) for _ in range(1_000))

    with patch.object(
        StructuralNode,
        "__hash__",
        side_effect=AssertionError("signature collection must not structurally hash entities"),
    ):
        values = _collect_signature(roots)
        fingerprint = signature_fingerprint((*values, *values))

    assert values == tuple(sorted((left, right), key=lambda item: item.canonical_bytes()))
    assert len(fingerprint.digest) == 32
