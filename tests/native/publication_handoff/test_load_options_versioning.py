from __future__ import annotations

import hashlib

import pytest

from pyowl_core.backends.native_handoff import (
    NATIVE_LOAD_OPTION_FIELDS_V1,
    NATIVE_PARSE_LIMIT_FIELDS_V1,
    _load_options_bytes_v1,
    _sequence_bytes,
)
from pyowl_core.backends.native_handoff_v2 import (
    NATIVE_LOAD_OPTION_FIELDS_V2,
    _load_options_bytes_v2,
)
from pyowl_core.config import LoadOptions
from pyowl_core.exceptions import BackendProtocolError


def _option_values(options: LoadOptions, fields: tuple[str, ...]) -> tuple[object, ...]:
    values: list[object] = []
    for name in fields:
        value = getattr(options, name)
        if name == "limits":
            values.append(
                tuple(getattr(value, limit_name) for limit_name in NATIVE_PARSE_LIMIT_FIELDS_V1)
            )
        else:
            values.append(value)
    return tuple(values)


def test_v1_option_bytes_remain_frozen_and_reject_partial_mode() -> None:
    options = LoadOptions()
    expected = b"pyowl-core:native-load-options:v1\x00" + _sequence_bytes(
        _option_values(options, NATIVE_LOAD_OPTION_FIELDS_V1)
    )
    assert _load_options_bytes_v1(options) == expected

    with pytest.raises(BackendProtocolError) as caught:
        _load_options_bytes_v1(LoadOptions(allow_partial_rdf_mapping=True))
    assert caught.value.code == "NATIVE_ATTESTATION_OPTIONS"


def test_v2_option_digest_attests_the_partial_mapping_switch() -> None:
    strict = LoadOptions()
    partial = LoadOptions(allow_partial_rdf_mapping=True)
    strict_bytes = _load_options_bytes_v2(strict)
    partial_bytes = _load_options_bytes_v2(partial)

    assert (
        *NATIVE_LOAD_OPTION_FIELDS_V1,
        "allow_partial_rdf_mapping",
    ) == NATIVE_LOAD_OPTION_FIELDS_V2
    assert strict_bytes == b"pyowl-core:native-load-options:v2\x00" + _sequence_bytes(
        _option_values(strict, NATIVE_LOAD_OPTION_FIELDS_V2)
    )
    assert strict_bytes != _load_options_bytes_v1(strict)
    assert partial_bytes != strict_bytes
    assert hashlib.sha256(partial_bytes).digest() != hashlib.sha256(strict_bytes).digest()
