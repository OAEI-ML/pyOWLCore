from __future__ import annotations

from typing import Any, cast

import pytest

from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    UnresolvedImportWarning,
    load_snapshot,
)
from pyowl_core.backends import native
from pyowl_core.exceptions import BackendProtocolError
from pyowl_core.index import OntologyIdentityIndex
from tests.native.foundation._support import NativeTestExtension, load_extension

SOURCE = (
    b"Ontology(<urn:retained-identity> Import(<urn:retained-identity:missing>) "
    b"Declaration(Class(<urn:retained-identity:C>)))"
)


@pytest.fixture(scope="module", autouse=True)
def extension() -> NativeTestExtension:
    selected = load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available or "parse-functional-v1" not in result.features:
        pytest.skip(result.reason or "native Functional parser capability is unavailable")
    if not hasattr(selected, "_retained_ontology_identity_index_v1"):
        pytest.skip("selected native artifact lacks retained identity ownership")
    return selected


def _options(backend: BackendPreference) -> LoadOptions:
    return LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.RECORD_UNRESOLVED,
        backend=backend,
        collect_provenance=False,
    )


def _snapshot(backend: BackendPreference) -> object:
    with pytest.warns(UnresolvedImportWarning):
        return load_snapshot(SOURCE, options=_options(backend))


def test_retained_identity_index_owns_exact_attested_metadata_without_root_work(
    extension: NativeTestExtension,
) -> None:
    reference = cast(Any, _snapshot(BackendPreference.PYTHON))
    selected = cast(Any, _snapshot(BackendPreference.NATIVE))
    handle = selected._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    attestation = handle._attestation_v2()
    before_owner = raw_owner._publication_counters_v2()
    before_python = selected._native_python_counters()

    index = selected.view(OntologyIdentityIndex)
    reference_index = reference.view(OntologyIdentityIndex)

    after_owner = raw_owner._publication_counters_v2()
    after_python = selected._native_python_counters()
    native_owner = cast(Any, index)._native_owner
    assert type(native_owner) is cast(Any, extension)._NativeRetainedOntologyIdentityIndexV1
    root_key, metadata_digest, diagnostic_digest, report_digest, counters = (
        native_owner._layout_v1()
    )
    assert root_key == selected.root_document_key == attestation.root_document_key
    assert metadata_digest == attestation.metadata_manifest_sha256
    assert diagnostic_digest == attestation.diagnostics_manifest_sha256
    assert report_digest == attestation.report_sha256
    assert counters == {
        "document_count": len(selected.import_manifest.documents),
        "import_edge_count": len(selected.import_manifest.edges),
        "diagnostic_count": attestation.diagnostic_count,
        "retained_owner_bytes": before_owner.retained_owner_bytes,
        "complete_root_encode_calls": 0,
    }
    assert after_owner == before_owner
    assert after_python == before_python
    assert index.documents == reference_index.documents
    assert index.import_manifest_digest == reference_index.import_manifest_digest
    assert index.loader_diagnostics_digest == reference_index.loader_diagnostics_digest
    assert index.is_complete is reference_index.is_complete is False

    selected.close()
    assert selected.closed
    assert native_owner._layout_v1()[0] == root_key
    assert index.documents == reference_index.documents


def test_foreign_retained_identity_owner_fails_closed(
    extension: NativeTestExtension,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = cast(Any, _snapshot(BackendPreference.NATIVE))
    foreign = load_snapshot(
        b"Ontology(<urn:foreign-identity> Declaration(Class(<urn:foreign-identity:C>)))",
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.NATIVE,
            collect_provenance=False,
        ),
    )
    operation = cast(Any, extension)._retained_ontology_identity_index_v1
    foreign_handle = cast(Any, foreign)._native_snapshot_state.owner.handle
    foreign_raw_owner = object.__getattribute__(foreign_handle, "_owner_v2")
    foreign_index_owner = operation(foreign_raw_owner)

    def substitute_owner(_raw_owner: object) -> object:
        return foreign_index_owner

    monkeypatch.setattr(
        cast(Any, extension),
        "_retained_ontology_identity_index_v1",
        substitute_owner,
    )
    with pytest.raises(BackendProtocolError) as raised:
        selected.view(OntologyIdentityIndex)
    assert raised.value.code == "NATIVE_INDEX_RESULT"


def test_python_identity_index_never_acquires_a_native_owner() -> None:
    selected = cast(Any, _snapshot(BackendPreference.PYTHON))

    index = selected.view(OntologyIdentityIndex)

    assert cast(Any, index)._native_owner is None
