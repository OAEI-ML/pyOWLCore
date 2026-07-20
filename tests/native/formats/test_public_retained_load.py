from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    EncodedStructuralView,
    ImportPolicy,
    LoadOptions,
    MappingResolver,
    encode_snapshot,
    load_snapshot,
)
from pyowl_core.backends import native
from pyowl_core.exceptions import BackendProtocolError
from pyowl_core.model import canonical_bytes
from tests.native.encoded_views._independent import decode_root_canonical_bytes
from tests.native.foundation._support import NativeTestExtension, load_extension

SOURCE = (
    b"Ontology(<urn:retained-load> "
    b"Declaration(Class(<urn:retained-load:C>)) "
    b"Declaration(Class(<urn:retained-load:D>)) "
    b"SubClassOf(<urn:retained-load:C> <urn:retained-load:D>))"
)
ROOT = Path(__file__).parents[3]
RUNNER = Path(__file__).with_name("_retained_load_runner.py")


@pytest.fixture(scope="module", autouse=True)
def extension() -> NativeTestExtension:
    selected = load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available or "parse-functional-v1" not in result.features:
        pytest.skip(result.reason or "native Functional parser capability is unavailable")
    if not hasattr(selected, "_retain_structural_snapshot_v2"):
        pytest.skip("selected native artifact lacks the retained-owner constructor")
    return selected


def _options(backend: BackendPreference) -> LoadOptions:
    return LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.IGNORE,
        backend=backend,
        collect_provenance=True,
    )


def test_public_forced_native_load_publishes_real_typed_owner_without_scalar_fallback(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(SOURCE, options=_options(BackendPreference.PYTHON))
    selected = load_snapshot(SOURCE, options=_options(BackendPreference.NATIVE))

    assert selected.capabilities.backend == "native"
    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.origin_index == reference.origin_index

    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    assert type(raw_owner) is cast(Any, extension)._NativeSnapshotHandle
    before = cast(Any, raw_owner)._publication_counters_v2()
    assert before.retained_origin_rows == 2 * sum(
        len(rows) for rows in reference.origin_index.entries.values()
    )
    assert before.retained_origin_bytes > 0
    assert before.publication_structural_rows_copied == 0
    assert before.publication_structural_bytes_copied == 0

    scalar_error = AssertionError("encoded consumer crossed scalar traversal")
    with (
        patch.object(type(selected), "iter_axioms", side_effect=scalar_error),
        patch.object(type(selected), "iter_extensions", side_effect=scalar_error),
        patch.object(type(selected), "ontology_annotations", side_effect=scalar_error),
        patch.object(type(selected), "signature", side_effect=scalar_error),
    ):
        encoded = selected.view(EncodedStructuralView)

    after = cast(Any, raw_owner)._publication_counters_v2()
    expected = tuple((2, canonical_bytes(value)) for value in reference.iter_axioms())
    assert encoded.owner is selected
    assert len(encoded.buffers) == 11
    assert decode_root_canonical_bytes(encoded.buffers) == expected
    assert len({id(value.obj) for value in encoded.buffers.values()}) == 1
    assert all(type(value.obj) is bytes for value in encoded.buffers.values())
    assert after.encoded_view_requests == before.encoded_view_requests + 1
    assert after.page_requests == before.page_requests
    assert after.rows_emitted == before.rows_emitted


def test_retained_wire_reuses_columns_and_pages_origins_once(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(SOURCE, options=_options(BackendPreference.PYTHON))
    selected = load_snapshot(SOURCE, options=_options(BackendPreference.NATIVE))
    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    assert type(raw_owner) is cast(Any, extension)._NativeSnapshotHandle
    before_native = cast(Any, raw_owner)._publication_counters_v2()
    before_python = cast(Any, selected)._native_python_counters()

    scalar_error = AssertionError("wire consumer crossed scalar traversal")
    with (
        patch.object(type(selected), "iter_axioms", side_effect=scalar_error),
        patch.object(type(selected), "iter_extensions", side_effect=scalar_error),
        patch.object(type(selected), "ontology_annotations", side_effect=scalar_error),
        patch.object(type(selected), "signature", side_effect=scalar_error),
    ):
        retained_wire = encode_snapshot(selected)

    after_native = cast(Any, raw_owner)._publication_counters_v2()
    after_python = cast(Any, selected)._native_python_counters()
    assert retained_wire == encode_snapshot(reference)
    assert after_python.model_rows_materialized == before_python.model_rows_materialized
    assert after_native.encoded_view_requests == before_native.encoded_view_requests + 1
    assert after_native.page_requests == before_native.page_requests + 1
    assert after_native.rows_emitted == before_native.rows_emitted + sum(
        len(rows) for rows in reference.origin_index.entries.values()
    )


def test_attested_wire_source_fails_closed_without_direct_columns(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    selected = load_snapshot(SOURCE, options=_options(BackendPreference.NATIVE))
    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    before_native = cast(Any, raw_owner)._publication_counters_v2()
    before_python = cast(Any, selected)._native_python_counters()
    monkeypatch.setattr(cast(Any, extension), "_encoded_structural_columns_v1", None)

    scalar_error = AssertionError("failed wire source crossed scalar traversal")
    with (
        patch.object(type(selected), "iter_axioms", side_effect=scalar_error),
        patch.object(type(selected), "iter_extensions", side_effect=scalar_error),
        patch.object(type(selected), "ontology_annotations", side_effect=scalar_error),
        pytest.raises(BackendProtocolError) as raised,
    ):
        encode_snapshot(selected)

    after_native = cast(Any, raw_owner)._publication_counters_v2()
    after_python = cast(Any, selected)._native_python_counters()
    assert raised.value.code == "NATIVE_WIRE_SOURCE"
    assert after_native.page_requests == before_native.page_requests
    assert after_native.rows_emitted == before_native.rows_emitted
    assert after_python.model_rows_materialized == before_python.model_rows_materialized


def test_empty_provenance_enabled_load_retains_zero_origin_rows(
    extension: NativeTestExtension,
) -> None:
    source = b"Ontology(<urn:retained-empty>)"
    reference = load_snapshot(source, options=_options(BackendPreference.PYTHON))
    selected = load_snapshot(source, options=_options(BackendPreference.NATIVE))

    assert selected.capabilities.backend == "native"
    assert selected.origin_index == reference.origin_index
    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    assert type(raw_owner) is cast(Any, extension)._NativeSnapshotHandle
    before_native = cast(Any, raw_owner)._publication_counters_v2()
    before_python = cast(Any, selected)._native_python_counters()
    assert encode_snapshot(selected) == encode_snapshot(reference)
    counters = cast(Any, raw_owner)._publication_counters_v2()
    python_counters = cast(Any, selected)._native_python_counters()
    assert counters.retained_origin_rows == 0
    assert counters.retained_origin_bytes == 0
    assert counters.page_requests == before_native.page_requests
    assert counters.rows_emitted == before_native.rows_emitted
    assert python_counters.model_rows_materialized == before_python.model_rows_materialized


def test_retained_load_stays_unadvertised_and_ineligible_shape_skips_owner_construction(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    calls = 0

    def unexpected(*_arguments: object) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("ineligible load crossed retained-owner construction")

    monkeypatch.setattr(cast(Any, extension), "_retain_structural_snapshot_v2", unexpected)

    for options in (
        LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.NATIVE,
            collect_provenance=False,
        ),
        LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.NATIVE,
            preserve_source_map=True,
            collect_provenance=True,
        ),
        LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.NATIVE,
            collect_provenance=True,
            validate_owl2_dl=True,
        ),
    ):
        ineligible = load_snapshot(SOURCE, options=options)
        assert ineligible.capabilities.backend == "python"

    anonymous = load_snapshot(
        b"Ontology(<urn:retained-anonymous> ClassAssertion(<urn:C> _:person))",
        options=_options(BackendPreference.NATIVE),
    )
    assert anonymous.capabilities.backend == "python"

    imported = load_snapshot(
        b"Ontology(<urn:retained-root> Import(<urn:retained-child>))",
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.RESOLVE_LOCAL,
            backend=BackendPreference.NATIVE,
            collect_provenance=True,
        ),
        resolver=MappingResolver(
            {
                "urn:retained-child": (
                    b"Ontology(<urn:retained-child> Declaration(Class(<urn:Child>)))"
                )
            }
        ),
    )
    assert len(imported.documents) == 2
    assert imported.capabilities.backend == "python"

    assert calls == 0
    assert extension.INGESTION_FEATURES == ()
    assert "retained-structural-snapshot-v2" not in extension.FEATURES
    assert not imported.capabilities.encoded_view_schemas


def test_eligible_owner_construction_failure_propagates_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    calls = 0
    retain = cast(Any, extension)._retain_structural_snapshot_v2

    def fail(
        _documents: object,
        _origins: object,
        attestation: object,
        config: object,
        cancel: object,
    ) -> object:
        nonlocal calls
        calls += 1
        return retain((((b"",), (), ()),), (), attestation, config, cancel)

    monkeypatch.setattr(cast(Any, extension), "_retain_structural_snapshot_v2", fail)
    with pytest.raises(BackendProtocolError) as raised:
        load_snapshot(SOURCE, options=_options(BackendPreference.NATIVE))
    assert raised.value.code == "NATIVE_EXCEPTION"
    assert calls == 1


def test_isolated_installed_artifact_crosses_direct_wire_and_mmap_owners() -> None:
    environment = dict(os.environ)
    inherited = environment.get("PYTHONPATH", "")
    paths = [str(ROOT)]
    if environment.get("PYOWL_CORE_TEST_NATIVE_LIBRARY") != "1":
        paths.insert(0, str(ROOT / "src"))
    if inherited:
        paths.append(inherited)
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    completed = subprocess.run(
        [sys.executable, str(RUNNER)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    observed = json.loads(completed.stdout)
    assert observed["backend"] == "native"
    assert observed["snapshot_type"] == "_NativeOntologySnapshot"
    assert observed["fingerprint_parity"] is True
    assert observed["direct_root_parity"] is True
    assert observed["direct_owner_identity"] is True
    assert observed["direct_encoded_view_requests"] == 1
    assert observed["decoded_parity"] is True
    assert len(observed["wire_sha256"]) == 64
    assert len(observed["wire_python_sha256"]) == 64
    assert observed["wire_python_parity"] is True
    assert observed["wire_differing_sections"] == []
    assert observed["origin_parity"] is True
    assert observed["retained_origin_rows"] == observed["reference_origin_rows"]
    assert observed["mapped_root_parity"] is True
    assert observed["mapped_fingerprint_parity"] is True
    assert observed["mapped_owner_identity"] is True
    assert observed["mapped_one_exporter"] is True
    assert observed["mapped_exporter_type"] == "mmap"
    assert observed["mapped_readonly"] is True
    assert observed["mapped_lazy"] is True
    assert observed["mapped_close_blocked"] is True
    assert observed["mapped_closed"] is True
    assert observed["direct_survives_owner_close"] is True
    assert observed["selected_closed"] is True
    assert observed["ingestion_features"] == []
    assert observed["view_features"] == []
    assert observed["encoded_view_schemas"] == {}
    assert observed["wire_model_rows_materialized"] == 0
    assert observed["wire_encoded_view_requests"] == 1
    assert observed["wire_page_requests"] == 1
    assert observed["wire_rows_emitted"] == observed["retained_origin_rows"]
    if environment.get("PYOWL_CORE_TEST_NATIVE_LIBRARY") == "1":
        assert not Path(observed["package_file"]).is_relative_to(ROOT / "src")
