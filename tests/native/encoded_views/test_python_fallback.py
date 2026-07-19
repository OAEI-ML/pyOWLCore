from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

import pyowl_core.model as m
from pyowl_core.backends.native_views import (
    ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1,
    ENCODED_STRUCTURAL_SCHEMA_NAME_V1,
    produce_encoded_structural_view_v1,
)
from pyowl_core.document.snapshot import AxiomScope
from pyowl_core.exceptions import ResourceLimitError
from tests.native.encoded_views._independent import decode_root_canonical_bytes
from tests.native.encoded_views._support import (
    complete_constructor_snapshot,
    scalar_root_bytes,
)


def _unsigned(data: memoryview, width: int) -> tuple[int, ...]:
    return tuple(
        int.from_bytes(data[offset : offset + width], "little")
        for offset in range(0, len(data), width)
    )


def test_python_fallback_covers_every_constructor_and_independent_decoder() -> None:
    snapshot = complete_constructor_snapshot()
    encoded = produce_encoded_structural_view_v1(snapshot)

    assert encoded.owner is snapshot
    assert encoded.schema_name == ENCODED_STRUCTURAL_SCHEMA_NAME_V1
    assert encoded.schema_version == 1
    assert encoded.model_schema == 1
    assert encoded.descriptor
    assert ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1.hex() == (
        "29bf111466b3946d4765c29c0d4742ab3ec7b355fdaa5be1ca18d15ebc3b452a"
    )
    assert len(encoded.segments) == 1
    direct = encoded.segments[0]
    assert direct.role == 1
    assert direct.owner is snapshot
    assert direct.source is None
    assert direct.posting_mode == 0
    assert bytes(direct.root_ids) == b""
    assert direct.member_token is None
    assert decode_root_canonical_bytes(encoded.buffers) == scalar_root_bytes(snapshot)

    observed_tags = set(_unsigned(encoded.buffers["node_tags"], 2))
    assert observed_tags == {spec.tag for spec in m.CONSTRUCTOR_SPECS}
    for _kind, canonical in decode_root_canonical_bytes(encoded.buffers):
        assert m.canonical_bytes(m.decode_canonical(canonical)) == canonical


def test_buffers_are_deterministic_readonly_contiguous_and_owner_retaining() -> None:
    snapshot = complete_constructor_snapshot()
    first = produce_encoded_structural_view_v1(snapshot)
    second = produce_encoded_structural_view_v1(snapshot)

    assert first.owner is snapshot
    assert second.owner is snapshot
    assert first.structural_fingerprint == second.structural_fingerprint
    assert tuple(first.buffers) == tuple(second.buffers)
    for name in first.buffers:
        left = first.buffers[name]
        right = second.buffers[name]
        assert bytes(left) == bytes(right)
        assert left.readonly
        assert left.c_contiguous
        assert left.ndim == left.itemsize == 1
        assert left.format == "B"
        assert left.shape == (len(left),)
        assert left.strides == (1,)
        assert type(left.obj) is bytes
        if left:
            with pytest.raises(TypeError):
                left[0] = 0
    with pytest.raises(TypeError):
        cast(Any, first.buffers)["hostile"] = memoryview(b"")


def test_scope_and_document_selection_use_public_scalar_semantics() -> None:
    snapshot = complete_constructor_snapshot()
    root = produce_encoded_structural_view_v1(snapshot, scope=AxiomScope.ROOT)
    document = produce_encoded_structural_view_v1(
        snapshot,
        scope=AxiomScope.DOCUMENT,
        document_key=snapshot.root_document_key,
    )
    closure = produce_encoded_structural_view_v1(snapshot)

    assert root.scope is AxiomScope.ROOT
    assert root.document_key is None
    assert document.scope is AxiomScope.DOCUMENT
    assert document.document_key == snapshot.root_document_key
    assert decode_root_canonical_bytes(root.buffers) == scalar_root_bytes(snapshot)
    assert decode_root_canonical_bytes(document.buffers) == scalar_root_bytes(snapshot)
    assert decode_root_canonical_bytes(closure.buffers) == scalar_root_bytes(snapshot)

    with pytest.raises(ValueError, match="requires document_key"):
        produce_encoded_structural_view_v1(snapshot, scope=AxiomScope.DOCUMENT)
    with pytest.raises(ValueError, match="valid only"):
        produce_encoded_structural_view_v1(
            snapshot,
            scope=AxiomScope.CLOSURE,
            document_key=snapshot.root_document_key,
        )


def test_multiplicity_order_and_structural_identity_survive_columns() -> None:
    snapshot = complete_constructor_snapshot()
    encoded = produce_encoded_structural_view_v1(snapshot)
    independent = decode_root_canonical_bytes(encoded.buffers)
    expected = scalar_root_bytes(snapshot)

    assert independent == expected
    # The exhaustive fixture includes ordered property/argument tuples, canonical
    # sets, repeated references, and a cardinality larger than u64.
    roots = tuple(m.decode_canonical(payload) for _kind, payload in independent)
    assert any(
        isinstance(node, m.ObjectMaxCardinality) and node.cardinality == 2**80
        for root in roots
        for node in m.walk(root)
    )
    assert any(
        isinstance(node, m.ObjectPropertyChain) and len(node.properties) == 3
        for root in roots
        for node in m.walk(root)
    )


def test_producer_uses_owner_limits_and_only_allows_tightening() -> None:
    snapshot = complete_constructor_snapshot()
    tight_limits = replace(snapshot.load_options.limits, max_index_bytes=1)
    tight_owner = replace(
        snapshot,
        load_options=replace(snapshot.load_options, limits=tight_limits),
    )

    with pytest.raises(ResourceLimitError) as limited:
        produce_encoded_structural_view_v1(tight_owner)
    assert limited.value.limit == "max_index_bytes"

    with pytest.raises(ResourceLimitError) as limited:
        produce_encoded_structural_view_v1(snapshot, limits=tight_limits)
    assert limited.value.limit == "max_index_bytes"
