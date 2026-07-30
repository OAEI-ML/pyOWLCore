from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
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
    abi_version: int = 3,
    ingestion: object = (),
    views: object = (),
    extra_features: tuple[str, ...] = (),
) -> SimpleNamespace:
    features = tuple(sorted((*native._FOUNDATION_FEATURE_LEDGER, *extra_features)))
    return SimpleNamespace(
        ABI_VERSION=abi_version,
        MODEL_SCHEMA_VERSION=1,
        WIRE_FORMAT_VERSION=(1, 1),
        FEATURES=features,
        INGESTION_FEATURES=ingestion,
        VIEW_FEATURES=views,
    )


def test_successor_binding_partitions_are_exact_and_disjoint() -> None:
    extension = load_extension()
    expected_ingestion = tuple(sorted(native._INGESTION_FEATURE_LEDGER))
    expected_views = tuple(sorted(native._VIEW_FEATURE_LEDGER))
    assert expected_ingestion == extension.INGESTION_FEATURES
    assert expected_views == extension.VIEW_FEATURES
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


def test_stale_private_abi_fails_closed_before_native_code_runs() -> None:
    calls: list[str] = []
    extension = _metadata_extension(abi_version=1)
    extension.self_test = lambda: calls.append("self-test")
    extension.version = lambda: ("stale", 1)

    with patch("pyowl_core.backends.native.importlib.import_module", return_value=extension):
        runtime = native._load_runtime((123, 0))

    assert runtime.extension is None
    assert runtime.probe.available is False
    assert runtime.probe.reason == "native extension metadata is incompatible"
    assert calls == []


@pytest.mark.parametrize("operation", ("roundtrip_wire", "validate_wire"))
def test_public_wire_entry_owns_writable_input_before_capability_setup(
    operation: str,
) -> None:
    source = bytearray(b"wire input")
    original = bytes(source)
    captured: list[object] = []
    validation = object()

    def require_after_mutation(capability: str) -> SimpleNamespace:
        assert capability == "wire-v1"
        source[-1] ^= 1

        def capture(data: object, _config: object, _cancel: object) -> bytes:
            captured.append(data)
            if operation == "validate_wire":
                return b"receipt"
            assert isinstance(data, bytes)
            return data

        return SimpleNamespace(**{operation: capture})

    with (
        patch("pyowl_core.backends.native.require", side_effect=require_after_mutation),
        patch("pyowl_core.backends.native._relay", return_value=nullcontext(None)),
        patch("pyowl_core.backends.native._decode_receipt", return_value=validation),
    ):
        result = getattr(native, operation)(source)

    assert captured == [original]
    assert captured[0] is not source
    assert bytes(source) != original
    assert result == (original if operation == "roundtrip_wire" else validation)


def test_wire_entry_does_not_coerce_readonly_or_noncontiguous_inputs() -> None:
    immutable = b"immutable"
    readonly = memoryview(bytearray(b"readonly")).toreadonly()
    noncontiguous = memoryview(bytearray(b"noncontiguous"))[::2]
    invalid = object()

    try:
        for value in (immutable, readonly, noncontiguous, invalid):
            assert native._snapshot_writable_wire_input(value) is value
    finally:
        readonly.release()
        noncontiguous.release()
