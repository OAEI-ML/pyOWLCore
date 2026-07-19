from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pyowl_core.backends import native
from pyowl_core.backends.native_ingestion import require_ingestion_binding
from pyowl_core.backends.native_views import require_view_binding
from pyowl_core.exceptions import BackendProtocolError

from ._support import load_extension


def _metadata_extension(
    *,
    ingestion: object = (),
    views: object = (),
    extra_features: tuple[str, ...] = (),
) -> SimpleNamespace:
    features = tuple(sorted((*native._FOUNDATION_FEATURE_LEDGER, *extra_features)))
    return SimpleNamespace(
        ABI_VERSION=1,
        MODEL_SCHEMA_VERSION=1,
        WIRE_FORMAT_VERSION=(1, 1),
        FEATURES=features,
        INGESTION_FEATURES=ingestion,
        VIEW_FEATURES=views,
    )


def test_successor_binding_partitions_start_empty() -> None:
    extension = load_extension()
    assert extension.INGESTION_FEATURES == ()
    assert extension.VIEW_FEATURES == ()
    assert set(extension.INGESTION_FEATURES).isdisjoint(extension.VIEW_FEATURES)
    assert set(extension.INGESTION_FEATURES) <= set(extension.FEATURES)
    assert set(extension.VIEW_FEATURES) <= set(extension.FEATURES)


@pytest.mark.parametrize(
    ("require_binding", "extension", "code"),
    (
        (
            require_ingestion_binding,
            SimpleNamespace(INGESTION_FEATURES=(), VIEW_FEATURES=("view-v1",)),
            "NATIVE_INGESTION_REGISTRATION",
        ),
        (
            require_view_binding,
            SimpleNamespace(INGESTION_FEATURES=("ingest-v1",), VIEW_FEATURES=()),
            "NATIVE_VIEW_REGISTRATION",
        ),
    ),
)
def test_python_binding_seams_reject_cross_partition_capabilities(
    require_binding: Callable[[str], object],
    extension: object,
    code: str,
) -> None:
    with (
        patch("pyowl_core.backends.native.require", return_value=extension),
        pytest.raises(BackendProtocolError) as captured,
    ):
        require_binding("shared-v1")
    assert captured.value.code == code


def test_python_binding_seams_return_exact_partition_owner() -> None:
    ingestion = SimpleNamespace(INGESTION_FEATURES=("ingest-v1",), VIEW_FEATURES=())
    view = SimpleNamespace(INGESTION_FEATURES=(), VIEW_FEATURES=("view-v1",))
    with patch("pyowl_core.backends.native.require", return_value=ingestion):
        assert require_ingestion_binding("ingest-v1") is ingestion
    with patch("pyowl_core.backends.native.require", return_value=view):
        assert require_view_binding("view-v1") is view


@pytest.mark.parametrize(
    "extension",
    (
        _metadata_extension(extra_features=("orphan-v1",)),
        _metadata_extension(
            ingestion=("ingest-v1",),
            views=("ingest-v1",),
            extra_features=("ingest-v1",),
        ),
        _metadata_extension(
            ingestion=("safe-rust",),
        ),
        _metadata_extension(
            ingestion=("z-v1", "a-v1"),
            extra_features=("a-v1", "z-v1"),
        ),
        _metadata_extension(
            ingestion=["ingest-v1"],
            extra_features=("ingest-v1",),
        ),
        _metadata_extension(
            ingestion=("ingest-λ",),
            extra_features=("ingest-λ",),
        ),
    ),
)
def test_native_metadata_rejects_malformed_successor_partitions(
    extension: SimpleNamespace,
) -> None:
    with pytest.raises(ValueError):
        native._validate_metadata(extension)


def test_native_metadata_accepts_exhaustive_disjoint_successor_partitions() -> None:
    extension = _metadata_extension(
        ingestion=("ingest-v1",),
        views=("view-v1",),
        extra_features=("ingest-v1", "view-v1"),
    )
    assert native._validate_metadata(extension) == extension.FEATURES
