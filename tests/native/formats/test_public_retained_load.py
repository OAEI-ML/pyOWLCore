from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import pytest

from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    EncodedStructuralView,
    ImportPolicy,
    LoadOptions,
    load_snapshot,
)
from pyowl_core.backends import native
from pyowl_core.model import canonical_bytes
from tests.native.encoded_views._independent import decode_root_canonical_bytes
from tests.native.foundation._support import NativeTestExtension, load_extension

SOURCE = (
    b"Ontology(<urn:retained-load> "
    b"Declaration(Class(<urn:retained-load:C>)) "
    b"SubClassOf(<urn:retained-load:C> <urn:retained-load:D>))"
)


@pytest.fixture(scope="module", autouse=True)
def extension() -> NativeTestExtension:
    selected = load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available or "parse-functional-v1" not in result.features:
        pytest.skip(result.reason or "native Functional parser capability is unavailable")
    if not hasattr(selected, "_retain_structural_snapshot_v2"):
        pytest.skip("selected native artifact lacks the retained-load test hook")
    return selected


def _options(backend: BackendPreference) -> LoadOptions:
    return LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.IGNORE,
        backend=backend,
        collect_provenance=False,
    )


def test_public_forced_native_load_publishes_real_typed_owner_without_scalar_fallback(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    monkeypatch.setenv("PYOWL_CORE_TEST_RETAINED_NATIVE_LOAD", "1")
    reference = load_snapshot(SOURCE, options=_options(BackendPreference.PYTHON))
    selected = load_snapshot(SOURCE, options=_options(BackendPreference.NATIVE))

    assert selected.capabilities.backend == "native"
    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint

    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    assert type(raw_owner) is cast(Any, extension)._NativeSnapshotHandle
    before = cast(Any, raw_owner)._publication_counters_v2()
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


def test_retained_load_stays_unadvertised_and_ineligible_options_keep_existing_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYOWL_CORE_TEST_RETAINED_NATIVE_LOAD", raising=False)
    unadvertised = load_snapshot(SOURCE, options=_options(BackendPreference.NATIVE))
    assert unadvertised.capabilities.backend == "python"

    monkeypatch.setenv("PYOWL_CORE_TEST_RETAINED_NATIVE_LOAD", "1")
    provenance = load_snapshot(
        SOURCE,
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.NATIVE,
            collect_provenance=True,
        ),
    )
    assert provenance.capabilities.backend == "python"
