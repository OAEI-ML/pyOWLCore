from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest

from pyowl_core.backends.native_views import (
    ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1,
    ENCODED_STRUCTURAL_DESCRIPTOR_V1,
    ENCODED_STRUCTURAL_MODEL_SCHEMA_V1,
    ENCODED_STRUCTURAL_SCHEMA_NAME_V1,
    ENCODED_STRUCTURAL_SCHEMA_VERSION_V1,
)
from tests.native.foundation._support import NativeTestExtension, load_extension


@pytest.fixture(scope="module")
def extension() -> NativeTestExtension:
    selected = load_extension()
    if not hasattr(selected, "_encoded_view_schema_v1"):
        pytest.skip("selected native artifact lacks the WP17 schema test hook")
    return selected


def _schema_hook(extension: NativeTestExtension) -> Callable[..., object]:
    return cast(Callable[..., object], cast(Any, extension)._encoded_view_schema_v1)


def test_native_registration_matches_the_frozen_python_descriptor(
    extension: NativeTestExtension,
) -> None:
    observed = cast(
        tuple[str, int, int, bytes, bytes, str, bool],
        _schema_hook(extension)(
            ENCODED_STRUCTURAL_SCHEMA_NAME_V1,
            ENCODED_STRUCTURAL_SCHEMA_VERSION_V1,
            ENCODED_STRUCTURAL_MODEL_SCHEMA_V1,
            ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1,
        ),
    )

    assert observed == (
        ENCODED_STRUCTURAL_SCHEMA_NAME_V1,
        ENCODED_STRUCTURAL_SCHEMA_VERSION_V1,
        ENCODED_STRUCTURAL_MODEL_SCHEMA_V1,
        ENCODED_STRUCTURAL_DESCRIPTOR_V1,
        ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1,
        "frozen-unadvertised",
        False,
    )
    assert extension.VIEW_FEATURES == ()
    assert ENCODED_STRUCTURAL_SCHEMA_NAME_V1 not in extension.FEATURES


def test_native_registration_rejects_a_descriptor_digest_mismatch(
    extension: NativeTestExtension,
) -> None:
    wrong_digest = bytes([ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1[0] ^ 0xFF]) + (
        ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1[1:]
    )
    with pytest.raises(extension._NativeError) as raised:
        _schema_hook(extension)(
            ENCODED_STRUCTURAL_SCHEMA_NAME_V1,
            ENCODED_STRUCTURAL_SCHEMA_VERSION_V1,
            ENCODED_STRUCTURAL_MODEL_SCHEMA_V1,
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
            ENCODED_STRUCTURAL_SCHEMA_VERSION_V1,
            ENCODED_STRUCTURAL_MODEL_SCHEMA_V1,
            ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1,
        ),
        (
            ENCODED_STRUCTURAL_SCHEMA_NAME_V1,
            ENCODED_STRUCTURAL_SCHEMA_VERSION_V1 + 1,
            ENCODED_STRUCTURAL_MODEL_SCHEMA_V1,
            ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1,
        ),
        (
            ENCODED_STRUCTURAL_SCHEMA_NAME_V1,
            ENCODED_STRUCTURAL_SCHEMA_VERSION_V1,
            ENCODED_STRUCTURAL_MODEL_SCHEMA_V1 + 1,
            ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1,
        ),
    )
    for arguments in values:
        with pytest.raises(extension._NativeError):
            hook(*arguments)
