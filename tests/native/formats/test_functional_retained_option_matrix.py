from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import pytest

from pyowl_core import (
    IRI,
    BackendPreference,
    ImportPolicy,
    LoadOptions,
    encode_snapshot,
    load_snapshot,
)
from pyowl_core.backends import native
from tests.native.foundation._support import NativeTestExtension, load_extension

SOURCE = (
    b"Prefix(ex:=<urn:functional:matrix:>) "
    b"Ontology(<urn:functional:matrix> "
    + (b" " * (256 * 1024))
    + b"Declaration(Class(ex:A)) Declaration(Class(ex:B)) "
    b"SubClassOf(ex:A ex:B))"
)
DOCUMENT_IRI = IRI("urn:functional:matrix:document")


@pytest.fixture(scope="module", autouse=True)
def extension() -> NativeTestExtension:
    selected = load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available or "parse-functional-v1" not in result.features:
        pytest.skip(result.reason or "native Functional parser capability is unavailable")
    if not hasattr(selected, "_parse_functional_retained_v2"):
        pytest.skip("selected native artifact lacks retained Functional parsing")
    return selected


@pytest.mark.parametrize(
    ("collect_provenance", "preserve_source_map"),
    ((False, False), (True, False), (False, True), (True, True)),
)
@pytest.mark.parametrize("imports", tuple(ImportPolicy))
@pytest.mark.parametrize(
    "preference",
    (BackendPreference.NATIVE, BackendPreference.AUTO),
)
@pytest.mark.parametrize("document_iri", (None, DOCUMENT_IRI), ids=("no-iri", "iri"))
def test_detected_functional_retained_public_option_matrix(
    extension: NativeTestExtension,
    document_iri: IRI | None,
    preference: BackendPreference,
    imports: ImportPolicy,
    collect_provenance: bool,
    preserve_source_map: bool,
) -> None:
    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            imports=imports,
            backend=backend,
            collect_provenance=collect_provenance,
            preserve_source_map=preserve_source_map,
        )

    reference = load_snapshot(
        SOURCE,
        document_iri=document_iri,
        options=options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("retained Functional matrix decoded a complete Python model")
    with patch(
        "pyowl_core.backends.native._decode_parsed_functional",
        side_effect=unexpected,
    ):
        selected = cast(
            Any,
            load_snapshot(
                SOURCE,
                document_iri=document_iri,
                options=options(preference),
            ),
        )

    owner = selected._native_snapshot_state.owner.handle._owner_v2
    counters = owner._publication_counters_v2()
    ingestion = selected._native_ingestion_counters_v2()
    assert type(owner) is cast(Any, extension)._NativeSnapshotHandle
    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.capabilities.backend == "native"
    assert selected.root == reference.root
    assert selected.root.source_map == reference.root.source_map
    assert selected.root.origin_index == reference.root.origin_index
    assert selected.import_manifest == reference.import_manifest
    assert selected.origin_index == reference.origin_index
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert counters.parser_bytes == len(SOURCE)
    assert (counters.retained_origin_rows > 0) is collect_provenance
    assert (counters.retained_source_map_rows > 0) is preserve_source_map
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0
    assert ingestion.parser_result_bytes_scanned == 0
    assert ingestion.eager_structural_objects_materialized == 0
