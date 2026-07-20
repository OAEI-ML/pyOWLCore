from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pyowl_core
from tests.native.encoded_views import _independent as independent_decoder
from tests.native.encoded_views._support import scalar_root_bytes


def test_mapped_composite_reads_anonymous_lineage_from_columns_without_materializing(
    tmp_path: Path,
) -> None:
    first_source = pyowl_core.load_snapshot(
        b"Ontology(<urn:mapped-member:left> ClassAssertion(<urn:C> _:shared))"
    )
    second_source = pyowl_core.load_snapshot(
        b"Ontology(<urn:mapped-member:right> ClassAssertion(<urn:C> _:shared))"
    )
    first_path = tmp_path / "first.pyocore"
    second_path = tmp_path / "second.pyocore"
    first_path.write_bytes(pyowl_core.encode_snapshot(first_source))
    second_path.write_bytes(pyowl_core.encode_snapshot(second_source))
    first = pyowl_core.open_snapshot(first_path, mmap=True, verify=True)
    second = pyowl_core.open_snapshot(second_path, mmap=True, verify=True)
    composite = pyowl_core.compose_views(first, second, roles=("left", "right"))
    expected = scalar_root_bytes(
        pyowl_core.compose_views(first_source, second_source, roles=("left", "right"))
    )

    encoded = composite.view(pyowl_core.EncodedStructuralView)
    decoded = independent_decoder._decode_segmented_root_canonical_bytes(encoded)
    observed = tuple((root.root_kind, root.canonical) for root in decoded.roots)

    assert observed == expected
    assert cast(Any, first)._mapped_state.decoded is None
    assert cast(Any, second)._mapped_state.decoded is None
    assert decoded.proof.scalar_traversal_calls == 0
    assert decoded.proof.referenced_buffer_copy_bytes == 0
    assert any(cast(Any, value).owner is first for value in decoded.proof.retained_views)
    assert any(cast(Any, value).owner is second for value in decoded.proof.retained_views)
