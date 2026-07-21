from __future__ import annotations

import gc
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

import pyowl_core.model as model
from pyowl_core import (
    BackendPreference,
    MappedOntologySnapshot,
    OntologyView,
    ParseLimits,
    encode_snapshot,
    open_snapshot,
)
from pyowl_core.backends import native
from pyowl_core.backends.native_ingestion import _publish_structural_snapshot_v2
from pyowl_core.backends.native_views import produce_encoded_structural_view_v1
from pyowl_core.cancellation import CancellationToken
from pyowl_core.document.provenance import OriginIndex
from pyowl_core.exceptions import ResourceLimitError, SnapshotInUseError
from pyowl_core.wire import codec as wire_codec
from pyowl_core.wire.codec import InspectedWire
from tests.conformance._support import python_snapshot
from tests.native.encoded_views._independent import decode_root_canonical_bytes
from tests.native.encoded_views._support import (
    complete_constructor_snapshot,
    scalar_root_bytes,
)
from tests.native.foundation._support import NativeTestExtension, load_extension


@pytest.fixture(scope="module", autouse=True)  # type: ignore[untyped-decorator]
def extension() -> NativeTestExtension:
    selected = load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available:
        if os.environ.get("PYOWL_CORE_TEST_HOOKS_REQUIRED") == "1":
            pytest.fail(result.reason or "required native extension is unavailable")
        pytest.skip(result.reason or "native extension is unavailable")
    return selected


def _roots(selected: OntologyView) -> tuple[model.StructuralNode, ...]:
    return (
        *selected.ontology_annotations(),
        *selected.iter_axioms(),
        *selected.iter_extensions(),
    )


def _reachable_tags(roots: tuple[model.StructuralNode, ...]) -> set[int]:
    return {
        model.constructor_spec(node).tag
        for root in roots
        for node in model.walk(root)
    }


def test_every_constructor_mapped_owner_matches_scalar_native_and_wire(
    tmp_path: Path,
) -> None:
    source = complete_constructor_snapshot()
    source_roots = _roots(source)
    expected_tags = {spec.tag for spec in model.CONSTRUCTOR_SPECS}
    assert _reachable_tags(source_roots) == expected_tags

    python_wire = encode_snapshot(source)
    native_wire = native.encode_snapshot(source)
    assert native_wire == python_wire
    assert native.roundtrip_wire(native_wire) == native_wire

    path = tmp_path / "every-constructor.pyocore"
    path.write_bytes(native_wire)
    opened = open_snapshot(path)
    assert isinstance(opened, MappedOntologySnapshot)
    encoded = None
    try:
        assert opened._mapped_state.decoded is None
        assert opened._mapped_state.inspected.materialized_model_cache is None
        assert opened.root_document_key == source.root_document_key
        assert opened.structural_context == source.structural_context
        assert opened.structural_fingerprint == source.structural_fingerprint
        assert opened.logical_fingerprint == source.logical_fingerprint
        assert opened.signature_fingerprint == source.signature_fingerprint
        assert opened.diagnostics == source.diagnostics == ()
        assert opened.report.document_count == source.report.document_count
        assert opened.report.effective_axiom_count == source.report.effective_axiom_count
        assert encode_snapshot(opened) == native_wire
        assert opened._mapped_state.decoded is None

        encoded = produce_encoded_structural_view_v1(opened)
        assert encoded.owner is opened
        assert decode_root_canonical_bytes(encoded.buffers) == scalar_root_bytes(source)
        assert opened._mapped_state.decoded is None

        materialized = opened.materialize()
        mapped_roots = _roots(materialized)
        assert _reachable_tags(mapped_roots) == expected_tags
        assert tuple(model.canonical_bytes(value) for value in mapped_roots) == tuple(
            model.canonical_bytes(value) for value in source_roots
        )
        assert tuple(hash(value) for value in mapped_roots) == tuple(
            hash(value) for value in source_roots
        )
        assert materialized.root.ontology_id == source.root.ontology_id
        assert materialized.root.direct_imports == source.root.direct_imports
        assert materialized.import_manifest.policy == source.import_manifest.policy
        assert materialized.import_manifest.offline is source.import_manifest.offline
        assert materialized.import_manifest.edges == source.import_manifest.edges
        assert tuple(
            (
                record.document_key,
                record.ontology_id,
                record.document_fingerprint,
                record.status,
            )
            for record in materialized.import_manifest.documents
        ) == tuple(
            (
                record.document_key,
                record.ontology_id,
                record.document_fingerprint,
                record.status,
            )
            for record in source.import_manifest.documents
        )
        assert hash(opened) == hash(source)
        assert encode_snapshot(materialized) == native_wire
    finally:
        encoded = None
        gc.collect()
        assert opened._mapped_state.dependents == 0
        opened.close()


def test_every_constructor_scoped_retained_owner_uses_native_wire_and_mapping(
    extension: NativeTestExtension,
    tmp_path: Path,
) -> None:
    fixture = complete_constructor_snapshot()
    base = python_snapshot(replace(fixture.root, origin_index=OriginIndex()))
    source = replace(
        base,
        load_options=replace(base.load_options, backend=BackendPreference.NATIVE),
    )
    expected_roots = _roots(source)
    expected_tags = {spec.tag for spec in model.CONSTRUCTOR_SPECS}
    assert _reachable_tags(expected_roots) == expected_tags
    expected_wire = encode_snapshot(source)

    retained = _publish_structural_snapshot_v2(
        source,
        cast(Any, extension),
        None,
        None,
    )
    selected = cast(Any, retained)
    assert type(retained).__name__ == "_NativeOntologySnapshot"
    assert not selected._native_wire_structural_aliases_v1()
    handle = selected._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    before_native = raw_owner._publication_counters_v2()
    before_python = selected._native_python_counters()

    scalar_error = AssertionError("scoped retained wire crossed scalar traversal")
    with (
        patch.object(type(retained), "iter_axioms", side_effect=scalar_error),
        patch.object(type(retained), "iter_extensions", side_effect=scalar_error),
        patch.object(type(retained), "ontology_annotations", side_effect=scalar_error),
        patch.object(type(retained), "signature", side_effect=scalar_error),
    ):
        retained_wire = encode_snapshot(retained)

    after_native = raw_owner._publication_counters_v2()
    after_python = selected._native_python_counters()
    assert retained_wire == expected_wire
    assert after_native.encoded_view_requests == before_native.encoded_view_requests + 3
    assert after_python == before_python

    path = tmp_path / "retained-every-constructor.pyocore"
    path.write_bytes(retained_wire)
    opened = open_snapshot(path)
    assert isinstance(opened, MappedOntologySnapshot)
    encoded = None
    try:
        assert opened._mapped_state.decoded is None
        assert encode_snapshot(opened) == retained_wire
        assert opened._mapped_state.decoded is None

        encoded = produce_encoded_structural_view_v1(opened)
        assert encoded.owner is opened
        assert decode_root_canonical_bytes(encoded.buffers) == scalar_root_bytes(source)
        assert opened._mapped_state.decoded is None

        materialized = opened.materialize()
        mapped_roots = _roots(materialized)
        assert _reachable_tags(mapped_roots) == expected_tags
        assert tuple(model.canonical_bytes(value) for value in mapped_roots) == tuple(
            model.canonical_bytes(value) for value in expected_roots
        )
        assert tuple(hash(value) for value in mapped_roots) == tuple(
            hash(value) for value in expected_roots
        )
        assert materialized.structural_fingerprint == source.structural_fingerprint
        assert materialized.logical_fingerprint == source.logical_fingerprint
        assert materialized.signature_fingerprint == source.signature_fingerprint
        assert materialized.diagnostics == source.diagnostics
    finally:
        encoded = None
        gc.collect()
        opened.close()
        selected.close()


def test_mapped_wire_fast_path_honors_limits_without_materialization(
    tmp_path: Path,
) -> None:
    wire = encode_snapshot(complete_constructor_snapshot())
    path = tmp_path / "limited.pyocore"
    path.write_bytes(wire)
    opened = open_snapshot(path)
    assert isinstance(opened, MappedOntologySnapshot)
    try:
        with pytest.raises(ResourceLimitError) as wire_limited:
            encode_snapshot(
                opened,
                limits=replace(opened.limits, max_wire_bytes=len(wire) - 1),
            )
        assert wire_limited.value.limit == "max_wire_bytes"
        with pytest.raises(ResourceLimitError) as limited:
            encode_snapshot(
                opened,
                limits=replace(opened.limits, max_temporary_bytes=len(wire) - 1),
            )
        assert limited.value.limit == "max_temporary_bytes"
        assert opened._mapped_state.decoded is None
    finally:
        opened.close()


def test_unverified_mapping_rebuilds_instead_of_copying_untrusted_digest(
    tmp_path: Path,
) -> None:
    wire = encode_snapshot(complete_constructor_snapshot())
    untrusted = bytearray(wire)
    untrusted[56:88] = bytes(32)
    path = tmp_path / "unverified.pyocore"
    path.write_bytes(untrusted)
    opened = open_snapshot(path, verify=False)
    assert isinstance(opened, MappedOntologySnapshot)
    try:
        assert "wire-verified" not in opened.capabilities.features
        assert opened._mapped_state.decoded is None
        assert encode_snapshot(opened) == wire
        assert opened._mapped_state.decoded is not None
    finally:
        opened.close()


def test_mapped_wire_copy_lease_fences_concurrent_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = encode_snapshot(complete_constructor_snapshot())
    path = tmp_path / "concurrent.pyocore"
    path.write_bytes(wire)
    opened = open_snapshot(path)
    assert isinstance(opened, MappedOntologySnapshot)
    started = threading.Event()
    proceed = threading.Event()
    original_validate = wire_codec.validate_bytes

    def blocked_validate(
        data: bytes | bytearray | memoryview,
        *,
        limits: ParseLimits,
        verify: bool,
        cancellation_token: CancellationToken | None = None,
        lazy_model_validation: bool = False,
    ) -> InspectedWire:
        started.set()
        if not proceed.wait(timeout=5):
            raise AssertionError("timed out waiting to release mapped wire validation")
        return original_validate(
            data,
            limits=limits,
            verify=verify,
            cancellation_token=cancellation_token,
            lazy_model_validation=lazy_model_validation,
        )

    monkeypatch.setattr(wire_codec, "validate_bytes", blocked_validate)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(encode_snapshot, opened)
            assert started.wait(timeout=5)
            with pytest.raises(SnapshotInUseError):
                opened.close()
            proceed.set()
            assert future.result(timeout=5) == wire
        assert opened._mapped_state.dependents == 0
        assert opened._mapped_state.decoded is None
    finally:
        proceed.set()
        opened.close()
