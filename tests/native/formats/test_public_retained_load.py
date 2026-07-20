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
        collect_provenance=False,
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
            collect_provenance=True,
        ),
        LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.NATIVE,
            preserve_source_map=True,
            collect_provenance=False,
        ),
        LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.NATIVE,
            collect_provenance=False,
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
            collect_provenance=False,
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
        attestation: object,
        config: object,
        cancel: object,
    ) -> object:
        nonlocal calls
        calls += 1
        return retain((((b"",), (), ()),), attestation, config, cancel)

    monkeypatch.setattr(cast(Any, extension), "_retain_structural_snapshot_v2", fail)
    with pytest.raises(BackendProtocolError) as raised:
        load_snapshot(SOURCE, options=_options(BackendPreference.NATIVE))
    assert raised.value.code == "NATIVE_EXCEPTION"
    assert calls == 1


def test_isolated_installed_artifact_public_load_has_python_parity() -> None:
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
    assert observed["ingestion_features"] == []
    assert observed["encoded_view_schemas"] == {}
    if environment.get("PYOWL_CORE_TEST_NATIVE_LIBRARY") == "1":
        assert not Path(observed["package_file"]).is_relative_to(ROOT / "src")
