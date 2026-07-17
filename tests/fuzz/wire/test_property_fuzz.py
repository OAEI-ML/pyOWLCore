from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from pyowl_core import WireError, decode_snapshot, encode_snapshot
from tests.unit.wire.conftest import snapshot


def test_every_compact_wire_truncation_is_rejected() -> None:
    encoded = encode_snapshot(snapshot())
    for length in range(len(encoded)):
        try:
            decode_snapshot(encoded[:length])
        except WireError:
            continue
        raise AssertionError(f"wire truncation at {length} bytes was accepted")


@settings(max_examples=96, deadline=None, derandomize=True)
@given(st.binary(min_size=0, max_size=2_048))
def test_arbitrary_wire_bytes_never_escape_the_wire_error_boundary(data: bytes) -> None:
    try:
        decoded = decode_snapshot(data)
    except WireError as error:
        assert error.code.startswith("WIRE_")
    else:
        assert encode_snapshot(decoded) == data
