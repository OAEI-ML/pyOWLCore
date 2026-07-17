from __future__ import annotations

import random

import pytest

from pyowl_core import WireError, decode_snapshot, encode_snapshot
from tests.unit.wire.conftest import snapshot


def test_deterministic_single_bit_mutation_smoke_never_returns_partial_snapshot() -> None:
    encoded = encode_snapshot(snapshot("A"))
    randomizer = random.Random(0x50594F434F5245)
    offsets = randomizer.sample(range(len(encoded)), min(256, len(encoded)))
    for offset in offsets:
        changed = bytearray(encoded)
        changed[offset] ^= 1 << randomizer.randrange(8)
        with pytest.raises(WireError):
            decode_snapshot(changed)
