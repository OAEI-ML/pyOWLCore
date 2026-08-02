from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest

from pyowl_core.backends.native_views import (
    ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1,
    ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V2,
    ENCODED_STRUCTURAL_DESCRIPTOR_V1,
    ENCODED_STRUCTURAL_DESCRIPTOR_V2,
    ENCODED_STRUCTURAL_MODEL_SCHEMA_V1,
    ENCODED_STRUCTURAL_MODEL_SCHEMA_V2,
    ENCODED_STRUCTURAL_SCHEMA_NAME_V1,
    ENCODED_STRUCTURAL_SCHEMA_NAME_V2,
    ENCODED_STRUCTURAL_SCHEMA_VERSION_V1,
    ENCODED_STRUCTURAL_SCHEMA_VERSION_V2,
)
from tests.native.foundation._support import NativeTestExtension, load_extension


@pytest.fixture(scope="module")
def extension() -> NativeTestExtension:
    selected = load_extension()
    required = ("_encoded_view_schema_v1", "_encoded_view_schema_v2")
    if any(not hasattr(selected, name) for name in required):
        pytest.skip("selected native artifact lacks the encoded-schema-2 test hooks")
    return selected


def _schema_hook(extension: NativeTestExtension) -> Callable[..., object]:
    return cast(Callable[..., object], cast(Any, extension)._encoded_view_schema_v2)


def test_native_registration_matches_the_frozen_python_descriptor(
    extension: NativeTestExtension,
) -> None:
    observed = cast(
        tuple[str, int, int, bytes, bytes, str, bool],
        _schema_hook(extension)(
            ENCODED_STRUCTURAL_SCHEMA_NAME_V2,
            ENCODED_STRUCTURAL_SCHEMA_VERSION_V2,
            ENCODED_STRUCTURAL_MODEL_SCHEMA_V2,
            ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V2,
        ),
    )

    assert observed == (
        ENCODED_STRUCTURAL_SCHEMA_NAME_V2,
        ENCODED_STRUCTURAL_SCHEMA_VERSION_V2,
        ENCODED_STRUCTURAL_MODEL_SCHEMA_V2,
        ENCODED_STRUCTURAL_DESCRIPTOR_V2,
        ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V2,
        "frozen-advertised",
        True,
    )
    assert extension.VIEW_FEATURES == (ENCODED_STRUCTURAL_SCHEMA_NAME_V2,)
    assert ENCODED_STRUCTURAL_SCHEMA_NAME_V2 in extension.FEATURES


def test_native_schema_v1_registration_fails_closed(
    extension: NativeTestExtension,
) -> None:
    hook = cast(Callable[..., object], cast(Any, extension)._encoded_view_schema_v1)
    with pytest.raises(extension._NativeError) as raised:
        hook(
            ENCODED_STRUCTURAL_SCHEMA_NAME_V1,
            ENCODED_STRUCTURAL_SCHEMA_VERSION_V1,
            ENCODED_STRUCTURAL_MODEL_SCHEMA_V1,
            ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1,
        )
    assert raised.value.args == (
        "NATIVE_WIRE_VERSION",
        "encoded structural schema 1 is frozen and not valid for model schema 2",
    )
    assert ENCODED_STRUCTURAL_DESCRIPTOR_V1 != ENCODED_STRUCTURAL_DESCRIPTOR_V2


def test_native_registration_rejects_a_descriptor_digest_mismatch(
    extension: NativeTestExtension,
) -> None:
    wrong_digest = bytes([ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V2[0] ^ 0xFF]) + (
        ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V2[1:]
    )
    with pytest.raises(extension._NativeError) as raised:
        _schema_hook(extension)(
            ENCODED_STRUCTURAL_SCHEMA_NAME_V2,
            ENCODED_STRUCTURAL_SCHEMA_VERSION_V2,
            ENCODED_STRUCTURAL_MODEL_SCHEMA_V2,
            wrong_digest,
        )
    assert raised.value.args == (
        "NATIVE_PROTOCOL",
        "native encoded-view schema registration mismatch",
    )


def test_native_registration_rejects_nonexact_metadata(
    extension: NativeTestExtension,
) -> None:
    hook = _schema_hook(extension)
    values: tuple[tuple[Any, ...], ...] = (
        (
            "pyowl-core/not-the-frozen-schema",
            ENCODED_STRUCTURAL_SCHEMA_VERSION_V2,
            ENCODED_STRUCTURAL_MODEL_SCHEMA_V2,
            ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V2,
        ),
        (
            ENCODED_STRUCTURAL_SCHEMA_NAME_V2,
            ENCODED_STRUCTURAL_SCHEMA_VERSION_V2 + 1,
            ENCODED_STRUCTURAL_MODEL_SCHEMA_V2,
            ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V2,
        ),
        (
            ENCODED_STRUCTURAL_SCHEMA_NAME_V2,
            ENCODED_STRUCTURAL_SCHEMA_VERSION_V2,
            ENCODED_STRUCTURAL_MODEL_SCHEMA_V2 + 1,
            ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V2,
        ),
    )
    for arguments in values:
        with pytest.raises(extension._NativeError):
            hook(*arguments)
