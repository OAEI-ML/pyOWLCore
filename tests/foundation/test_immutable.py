from __future__ import annotations

from pyowl_core._immutable import FrozenMap


def test_frozen_map_orders_byte_keys_by_unsigned_byte_value() -> None:
    values = FrozenMap(
        {
            b"\xd2": "last",
            b"\x56": "first",
            b"\x83": "third",
            b"\x7d": "second",
        }
    )

    assert tuple(values) == (b"\x56", b"\x7d", b"\x83", b"\xd2")
