from __future__ import annotations

from typing import Any, cast

import pytest

from pyowl_core import IRI, Class, Declaration, ParseLimits
from pyowl_core.backends import native
from pyowl_core.exceptions import ClosedSnapshotError
from pyowl_core.model import canonical_bytes
from tests.native.encoded_views._independent import decode_root_canonical_bytes
from tests.native.foundation._support import NativeTestExtension, load_extension


class _Scope(str):
    pass


@pytest.fixture(scope="module")
def extension() -> NativeTestExtension:
    selected = load_extension()
    required = (
        "_encoded_structural_fixture_v1",
        "_encoded_structural_columns_v1",
        "_encoded_structural_document_columns_v1",
    )
    if any(not hasattr(selected, name) for name in required):
        pytest.skip("selected native artifact lacks the WP17 direct-column hooks")
    return selected


def _config() -> bytes:
    return cast(bytes, cast(Any, native)._encode_config(ParseLimits(), None, verify=False))


def _assert_direct_buffers(buffers: object, counters: object) -> None:
    selected = cast(dict[str, memoryview], buffers)
    observed = cast(dict[str, int], counters)
    assert tuple(selected) == (
        "root_kinds",
        "root_ids",
        "node_tags",
        "node_field_offsets",
        "field_kinds",
        "field_values",
        "field_lengths",
        "item_kinds",
        "item_values",
        "item_lengths",
        "scalar_bytes",
    )
    exporters = {id(value.obj) for value in selected.values()}
    assert len(exporters) == 1
    for value in selected.values():
        assert type(value) is memoryview
        assert value.readonly
        assert value.c_contiguous
        assert value.ndim == value.itemsize == 1
        assert value.format == "B"
        assert value.shape == (len(value),)
        assert value.strides == (1,)
        assert type(value.obj) is bytes
    assert sum(map(len, selected.values())) == observed["retained_buffer_bytes"]
    assert observed["python_bridge_copy_bytes"] == 0
    assert observed["complete_root_encode_calls"] == 0


def test_direct_snapshot_and_document_columns_share_one_python_owner(
    extension: NativeTestExtension,
) -> None:
    selected = cast(Any, extension)
    handle = selected._encoded_structural_fixture_v1()
    before = handle._publication_counters_v2()

    buffers, counters = selected._encoded_structural_columns_v1(
        handle, "closure", None, _config()
    )
    _assert_direct_buffers(buffers, counters)
    expected = canonical_bytes(Declaration(Class(IRI("urn:encoded-view:fixture"))))
    assert decode_root_canonical_bytes(buffers) == ((2, expected),)

    document = handle._publication_document_v2(0)
    document_buffers, document_counters = selected._encoded_structural_document_columns_v1(
        document, _config()
    )
    _assert_direct_buffers(document_buffers, document_counters)
    assert decode_root_canonical_bytes(document_buffers) == ((2, expected),)
    assert handle._publication_counters_v2().encoded_view_requests == (
        before.encoded_view_requests + 2
    )

    handle._publication_close_v2()
    assert decode_root_canonical_bytes(buffers) == ((2, expected),)


def test_direct_columns_fail_closed_for_coordinates_and_lifecycle(
    extension: NativeTestExtension,
) -> None:
    selected = cast(Any, extension)
    handle = selected._encoded_structural_fixture_v1()
    before = handle._publication_counters_v2().encoded_view_requests

    with pytest.raises(ValueError, match="scope and document ordinal disagree"):
        selected._encoded_structural_columns_v1(handle, "closure", 0, _config())
    with pytest.raises(TypeError, match="exact str"):
        selected._encoded_structural_columns_v1(handle, _Scope("closure"), None, _config())
    assert handle._publication_counters_v2().encoded_view_requests == before

    handle._publication_close_v2()
    with pytest.raises(ClosedSnapshotError):
        selected._encoded_structural_columns_v1(handle, "closure", None, _config())
