from __future__ import annotations

import struct
from typing import Any, cast
from unittest.mock import patch

import pytest

from pyowl_core import (
    IRI,
    BackendPreference,
    CanonicalSet,
    Class,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    OntologyDelta,
    OntologySnapshot,
    ParseLimits,
    SubClassOf,
    apply_delta,
    canonical_bytes,
    compose_views,
    load_snapshot,
)
from pyowl_core.backends import native
from pyowl_core.cancellation import CancellationSource
from pyowl_core.exceptions import BackendProtocolError, OperationCancelledError, ResourceLimitError
from pyowl_core.index import AxiomTypeIndex, ViewBuildStrategy
from pyowl_core.model import AXIOM_TYPES
from tests.native.foundation._support import NativeTestExtension, load_extension


def _source(prefix: str) -> bytes:
    return (
        f"Prefix(:=<urn:{prefix}#>) Ontology(<urn:{prefix}> "
        "Declaration(Class(:A)) Declaration(Class(:B)) "
        "Declaration(ObjectProperty(:p)) "
        "SubClassOf(:A :B) EquivalentClasses(:A :B) "
        "ObjectPropertyDomain(:p :A) ObjectPropertyRange(:p :B))"
    ).encode()


def _snapshot(prefix: str, backend: BackendPreference) -> OntologySnapshot:
    return load_snapshot(
        _source(prefix),
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=backend,
        ),
    )


@pytest.fixture(scope="module", autouse=True)
def extension() -> NativeTestExtension:
    selected = load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available or "index-axiom-types-v1" not in result.features:
        pytest.skip(result.reason or "native axiom index capability is unavailable")
    return selected


def _rows(index: AxiomTypeIndex) -> tuple[bytes, ...]:
    return tuple(canonical_bytes(value) for value in index.iter_all())


def test_full_build_routes_once_and_matches_python_exactly(
    extension: NativeTestExtension,
) -> None:
    python_snapshot = _snapshot("partition", BackendPreference.PYTHON)
    native_snapshot = cast(Any, _snapshot("partition", BackendPreference.NATIVE))
    before_python = native_snapshot._native_python_counters()
    unexpected = AssertionError("retained index crossed coarse canonical rows")
    with (
        patch("pyowl_core.backends.native.partition_axioms", side_effect=unexpected),
        patch.object(type(native_snapshot), "iter_axioms", side_effect=unexpected),
    ):
        selected = native_snapshot.view(AxiomTypeIndex)
    reference = python_snapshot.view(AxiomTypeIndex)
    after_build = native_snapshot._native_python_counters()
    owner = cast(Any, selected)._native_owner
    assert type(owner) is cast(Any, extension)._NativeRetainedAxiomTypeIndexV1
    _, _, _, _, postings, counters = owner._layout_v1()
    attestation = native_snapshot._native_snapshot_state.owner.handle._attestation_v2()
    assert owner._binding_v1() == (
        attestation.root_table_sha256,
        attestation.effective_root_table_sha256,
    )
    expected_rows = tuple(reference.iter_all())
    assert owner._canonical_sizes_v1() == tuple(
        len(canonical_bytes(value)) for value in expected_rows
    )
    assert counters["axiom_rows"] == len(tuple(reference.iter_all()))
    assert counters["complete_root_encode_calls"] == 0
    assert postings == tuple(range(len(expected_rows)))
    assert after_build.model_rows_materialized == before_python.model_rows_materialized
    assert selected.report.strategy is ViewBuildStrategy.FULL_BUILD
    assert selected.report.row_count == reference.report.row_count
    assert selected.report.tables == reference.report.tables

    selected_count = selected.count(SubClassOf)
    assert selected_count == reference.count(SubClassOf)
    assert owner._layout_v1()[-1]["complete_root_encode_calls"] == 0
    assert (
        native_snapshot._native_python_counters().model_rows_materialized
        == before_python.model_rows_materialized
    )
    assert selected.tuple(SubClassOf, limit=0) == ()
    assert owner._layout_v1()[-1]["complete_root_encode_calls"] == 0
    assert selected.tuple(SubClassOf, limit=1) == reference.tuple(SubClassOf, limit=1)
    assert owner._layout_v1()[-1]["complete_root_encode_calls"] == 1
    subclass_rows = selected.tuple(SubClassOf)
    assert subclass_rows == reference.tuple(SubClassOf)
    assert owner._layout_v1()[-1]["complete_root_encode_calls"] == selected_count + 1
    assert (
        native_snapshot._native_python_counters().model_rows_materialized
        - before_python.model_rows_materialized
        == selected_count
    )
    assert _rows(selected) == _rows(reference)
    for constructor in AXIOM_TYPES:
        assert selected.tuple(constructor) == reference.tuple(constructor)

    before_close_calls = owner._layout_v1()[-1]["complete_root_encode_calls"]
    native_snapshot.close()
    assert native_snapshot.closed
    assert selected.tuple(SubClassOf) == reference.tuple(SubClassOf)
    assert owner._layout_v1()[-1]["complete_root_encode_calls"] == (
        before_close_calls + selected_count
    )


def test_retained_owner_protocol_failure_does_not_fall_back(
    extension: NativeTestExtension,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = cast(Any, _snapshot("invalid-retained-owner", BackendPreference.NATIVE))
    foreign = cast(Any, _snapshot("foreign-retained-owner", BackendPreference.NATIVE))
    operation = cast(Any, extension)._retained_axiom_type_index_v1
    foreign_raw_owner = object.__getattribute__(
        foreign._native_snapshot_state.owner.handle,
        "_owner_v2",
    )
    foreign_owner = operation(
        foreign_raw_owner,
        "closure",
        None,
        native._encode_config(foreign.load_options.limits, None, verify=False),
        None,
    )

    def wrong_owner(*_args: object) -> object:
        return foreign_owner

    monkeypatch.setattr(
        cast(Any, extension),
        "_retained_axiom_type_index_v1",
        wrong_owner,
    )
    unexpected = AssertionError("invalid retained owner reached the coarse bridge")
    with (
        patch("pyowl_core.backends.native.partition_axioms", side_effect=unexpected),
        pytest.raises(BackendProtocolError) as raised,
    ):
        snapshot.view(AxiomTypeIndex)
    assert raised.value.code == "NATIVE_INDEX_RESULT"


def test_auto_keeps_small_index_builds_on_python() -> None:
    snapshot = _snapshot("auto-small", BackendPreference.AUTO)
    with patch("pyowl_core.backends.native.partition_axioms") as partition:
        index = snapshot.view(AxiomTypeIndex)
    partition.assert_not_called()
    assert index.report.strategy is ViewBuildStrategy.FULL_BUILD


def test_auto_routes_large_closure_index_to_native() -> None:
    members = " ".join(f"Declaration(Class(<urn:auto-index:C{index}>))" for index in range(4_100))
    snapshot = load_snapshot(
        f"Ontology({members})".encode(),
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.AUTO,
        ),
    )
    with patch(
        "pyowl_core.backends.native.partition_axioms",
        wraps=native.partition_axioms,
    ) as partition:
        index = snapshot.view(AxiomTypeIndex)
    partition.assert_called_once()
    assert index.report.row_count == 4_100


def test_overlay_and_composite_reuse_native_parent_postings() -> None:
    source = _snapshot("source", BackendPreference.NATIVE)
    target = _snapshot("target", BackendPreference.NATIVE)
    source_index = source.view(AxiomTypeIndex)
    removed = next(source.iter_axioms(SubClassOf))
    added = SubClassOf(Class(IRI("urn:source#B")), Class(IRI("urn:source#C")))
    overlay = apply_delta(
        source,
        OntologyDelta(
            add_axioms=CanonicalSet((added,)),
            remove_axioms=CanonicalSet((removed,)),
        ),
    )
    overlay_index = overlay.view(AxiomTypeIndex)
    assert overlay_index.report.strategy is ViewBuildStrategy.PATCHED
    assert overlay_index.report.shared_row_count == source_index.report.total_row_count
    assert _rows(overlay_index) == tuple(canonical_bytes(value) for value in overlay.iter_axioms())

    bridge = SubClassOf(Class(IRI("urn:source#B")), Class(IRI("urn:target#A")))
    composite = compose_views(
        source,
        target,
        roles=("source", "target"),
        delta=OntologyDelta(add_axioms=CanonicalSet((bridge,))),
    )
    composite_index = composite.view(AxiomTypeIndex)
    assert composite_index.report.strategy is ViewBuildStrategy.MERGED
    assert composite_index.report.shared_row_count > 0
    assert _rows(composite_index) == tuple(
        canonical_bytes(value) for value in composite.iter_axioms()
    )


def test_partition_limits_and_cancellation_are_typed() -> None:
    axioms = tuple(_snapshot("bounded", BackendPreference.NATIVE).iter_axioms())
    with pytest.raises(ResourceLimitError):
        native.partition_axioms(
            axioms,
            limits=ParseLimits(max_index_rows=len(axioms) - 1),
        )
    with pytest.raises(ResourceLimitError):
        native.partition_axioms(
            axioms,
            limits=ParseLimits(max_index_bytes=64),
        )
    cancellation = CancellationSource()
    cancellation.cancel("test cancellation")
    with pytest.raises(OperationCancelledError):
        native.partition_axioms(axioms, cancellation_token=cancellation.token)


def test_empty_partition_is_canonical() -> None:
    result = native.partition_axioms(())
    assert result.postings == {}
    assert result.canonical_sizes == ()


def test_all_coarse_source_and_result_truncations_are_rejected(
    extension: NativeTestExtension,
) -> None:
    axiom = next(_snapshot("hostile", BackendPreference.NATIVE).iter_axioms())
    encoded = canonical_bytes(axiom)
    source = struct.pack("<8sHHQ", b"PYNIDXS1", 1, 0, 1)
    source += struct.pack("<Q", len(encoded)) + encoded
    request = b"PYNIDXQ1" + native._encode_config(
        ParseLimits(),
        None,
        verify=False,
    )
    build = cast(Any, extension).build_index
    for length in range(len(source)):
        with pytest.raises(extension._NativeError):
            build(source[:length], request)
    result = build(source, request)
    for length in range(len(result)):
        with pytest.raises(BackendProtocolError):
            native._decode_axiom_partition(result[:length], (axiom,), ParseLimits())
