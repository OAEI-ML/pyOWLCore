from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    EncodedStructuralView,
    ImportPolicy,
    ImportStatus,
    LoadOptions,
    MappingResolver,
    ParseLimits,
    UnresolvedImportWarning,
    encode_snapshot,
    load_snapshot,
)
from pyowl_core.backends import native, native_handoff_v2
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
AUTO_SOURCE = (
    b"Ontology(<urn:retained-auto> Import(<urn:retained-auto:ignored>) "
    + (b" " * (256 * 1024))
    + b"Declaration(Class(<urn:retained-auto:C>)))"
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
    assert before.parser_bytes == len(SOURCE)
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

    for phase in (
        "native_syntax_parse_seconds",
        "native_result_encode_seconds",
        "native_arena_construction_seconds",
        "native_freeze_seconds",
        "native_publication_prepare_seconds",
        "root_parse_seconds",
    ):
        assert selected.report.timings[phase] >= 0


def test_parser_built_storage_bypasses_the_python_row_retention_bridge(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    def unexpected(*_arguments: object) -> object:
        raise AssertionError("parser-built storage crossed the Python row retention bridge")

    monkeypatch.setattr(cast(Any, extension), "_retain_structural_snapshot_v2", unexpected)
    selected = load_snapshot(SOURCE, options=_options(BackendPreference.NATIVE))
    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    counters = cast(Any, raw_owner)._publication_counters_v2()

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert counters.parser_bytes == len(SOURCE)
    assert counters.canonical_input_rows == 3


def test_owner_first_publication_skips_eager_structural_model_decoding(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(SOURCE, options=_options(BackendPreference.PYTHON))

    def unexpected(*_arguments: object, **_keywords: object) -> object:
        raise AssertionError("eligible retained load crossed the complete model decoder")

    monkeypatch.setattr(native, "_decode_parsed_functional", unexpected)
    selected = load_snapshot(SOURCE, options=_options(BackendPreference.NATIVE))
    ingestion = cast(Any, selected)._native_ingestion_counters_v2()
    before = cast(Any, selected)._native_python_counters()

    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert ingestion.parser_result_bytes_scanned == 0
    assert 0 < ingestion.parser_summary_bytes_materialized < 1024
    assert ingestion.canonical_rows_scanned == 4  # ontology IRI plus three root rows
    assert ingestion.structural_occurrence_rows_scanned == 3
    assert ingestion.structural_root_rows_published == 3
    assert ingestion.eager_structural_objects_materialized == 0
    assert ingestion.metadata_iri_objects_materialized == 1
    assert ingestion.provenance_occurrence_records_materialized == 0
    assert ingestion.canonical_bytes_copied_to_python == 0
    assert ingestion.fingerprint_preimage_bytes_materialized_in_python == 0
    assert ingestion.native_publication_canonical_rows_encoded == 5
    assert ingestion.native_publication_canonical_bytes_encoded > 0
    assert ingestion.native_fingerprint_temporary_bytes > 0
    assert ingestion.native_origin_rows_retained == 3
    assert ingestion.native_origin_bytes_retained > 0
    assert before.model_rows_materialized == 0

    assert tuple(selected.iter_axioms()) == tuple(reference.iter_axioms())
    after = cast(Any, selected)._native_python_counters()
    assert after.model_rows_materialized == 3
    assert cast(Any, selected)._native_ingestion_counters_v2() == ingestion


def test_record_unresolved_without_resolver_keeps_owner_first_diagnostics_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        b"Ontology(<urn:retained-unresolved> "
        b"Import(<https://example.test/child?secret=yes>) "
        b"Import(<urn:retained-unresolved:other>) "
        b"Declaration(Class(<urn:retained-unresolved:C>)))"
    )
    limits = ParseLimits()

    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.RECORD_UNRESOLVED,
            backend=backend,
            limits=limits,
            collect_provenance=True,
        )

    with pytest.warns(UnresolvedImportWarning):
        reference = load_snapshot(
            source,
            document_iri="urn:retained-unresolved:document",
            options=options(BackendPreference.PYTHON),
        )

    def unexpected(*_arguments: object, **_keywords: object) -> object:
        raise AssertionError("record-unresolved retained load crossed the complete model decoder")

    monkeypatch.setattr(native, "_decode_parsed_functional", unexpected)
    with pytest.warns(UnresolvedImportWarning):
        selected = load_snapshot(
            source,
            document_iri="urn:retained-unresolved:document",
            options=options(BackendPreference.NATIVE),
        )

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.import_manifest == reference.import_manifest
    assert selected.report.diagnostics == reference.report.diagnostics
    assert selected.report.resolution_attempts == reference.report.resolution_attempts == 2
    assert len(selected.report.diagnostics) == 2
    assert all(
        diagnostic.code == "UNRESOLVED_IMPORT"
        for diagnostic in selected.report.diagnostics
    )
    assert all(
        edge.status is ImportStatus.UNRESOLVED for edge in selected.import_manifest.edges
    )
    assert all(edge.resolver_name == "none" for edge in selected.import_manifest.edges)
    assert all(edge.diagnostic is not None for edge in selected.import_manifest.edges)
    http_edge = next(
        edge
        for edge in selected.import_manifest.edges
        if edge.import_iri.value.startswith("https:")
    )
    assert cast(Any, http_edge.diagnostic).details["import_iri"] == (
        "https://example.test/child"
    )
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.origin_index == reference.origin_index
    assert encode_snapshot(selected) == encode_snapshot(reference)

    ingestion = cast(Any, selected)._native_ingestion_counters_v2()
    assert ingestion.parser_result_bytes_scanned == 0
    assert ingestion.canonical_bytes_copied_to_python == 0
    assert ingestion.fingerprint_preimage_bytes_materialized_in_python == 0
    assert ingestion.provenance_occurrence_records_materialized == 0


def test_record_unresolved_tight_diagnostic_limit_falls_back_before_publication() -> None:
    source = (
        b"Ontology(<urn:retained-unresolved-limit> "
        b"Import(<urn:retained-unresolved-limit:a>) "
        b"Import(<urn:retained-unresolved-limit:b>) "
        b"Declaration(Class(<urn:retained-unresolved-limit:C>)))"
    )
    limits = replace(ParseLimits(), max_diagnostics=1)

    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.RECORD_UNRESOLVED,
            backend=backend,
            limits=limits,
            collect_provenance=True,
        )

    with pytest.warns(UnresolvedImportWarning):
        reference = load_snapshot(source, options=options(BackendPreference.PYTHON))
    with pytest.warns(UnresolvedImportWarning):
        selected = load_snapshot(source, options=options(BackendPreference.NATIVE))

    assert type(selected).__name__ == "OntologySnapshot"
    assert selected.import_manifest == reference.import_manifest
    assert selected.report.diagnostics == reference.report.diagnostics
    assert selected.report.diagnostics[0].code == "DIAGNOSTICS_SUPPRESSED"
    assert selected.report.diagnostics[0].details["count"] == 2
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint


@pytest.mark.parametrize(
    "policy",
    (ImportPolicy.RESOLVE_LOCAL, ImportPolicy.RESOLVE_STRICT),
)
def test_empty_resolver_backed_policy_retains_owner_first(
    policy: ImportPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=policy,
            backend=backend,
            collect_provenance=True,
        )

    reference = load_snapshot(SOURCE, options=options(BackendPreference.PYTHON))

    def unexpected(*_arguments: object, **_keywords: object) -> object:
        raise AssertionError("empty import closure crossed the complete model decoder")

    monkeypatch.setattr(native, "_decode_parsed_functional", unexpected)
    selected = load_snapshot(SOURCE, options=options(BackendPreference.NATIVE))

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.import_manifest.policy is policy
    assert selected.import_manifest.edges == ()
    assert selected.report.resolution_attempts == 0
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.origin_index == reference.origin_index
    assert encode_snapshot(selected) == encode_snapshot(reference)
    ingestion = cast(Any, selected)._native_ingestion_counters_v2()
    assert ingestion.parser_result_bytes_scanned == 0
    assert ingestion.canonical_bytes_copied_to_python == 0


def test_owner_first_fingerprint_inputs_cover_annotations_and_nested_entities(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    source = (
        b"Ontology(<urn:retained-annotated> "
        b'Annotation(<urn:label> "ontology") '
        b"Declaration(Class(<urn:retained-annotated:C>)) "
        b"SubClassOf(Annotation(<urn:note> <urn:evidence>) "
        b"<urn:retained-annotated:C> ObjectSomeValuesFrom("
        b"<urn:retained-annotated:p> <urn:retained-annotated:D>)))"
    )
    reference = load_snapshot(source, options=_options(BackendPreference.PYTHON))

    def unexpected(*_arguments: object, **_keywords: object) -> object:
        raise AssertionError("annotated retained load crossed the complete model decoder")

    monkeypatch.setattr(native, "_decode_parsed_functional", unexpected)
    selected = load_snapshot(source, options=_options(BackendPreference.NATIVE))

    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.origin_index == reference.origin_index
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert cast(Any, selected)._native_python_counters().model_rows_materialized == 0


def test_compact_publication_seed_does_not_copy_structural_rows_to_python(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    declarations = b" ".join(
        f"Declaration(Class(<urn:retained-bulk:C{index:04d}>))".encode()
        for index in range(256)
    )
    source = b"Ontology(<urn:retained-bulk> " + declarations + b")"
    reference = load_snapshot(source, options=_options(BackendPreference.PYTHON))

    def unexpected(*_arguments: object, **_keywords: object) -> object:
        raise AssertionError("compact retained load crossed the Python canonical scanner")

    from pyowl_core.backends import native_ingestion

    monkeypatch.setattr(native_ingestion, "_scan_functional_result_v2", unexpected)
    selected = load_snapshot(source, options=_options(BackendPreference.NATIVE))
    ingestion = cast(Any, selected)._native_ingestion_counters_v2()
    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    owner = object.__getattribute__(handle, "_owner_v2")
    counters = cast(Any, owner)._publication_counters_v2()

    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert ingestion.parser_result_bytes_scanned == 0
    assert ingestion.parser_summary_bytes_materialized < 1024
    assert ingestion.canonical_rows_scanned == 257
    assert ingestion.structural_occurrence_rows_scanned == 256
    assert ingestion.structural_root_rows_published == 256
    assert ingestion.canonical_bytes_copied_to_python == 0
    assert ingestion.fingerprint_preimage_bytes_materialized_in_python == 0
    assert ingestion.provenance_occurrence_records_materialized == 0
    assert ingestion.native_origin_rows_retained == 256
    assert ingestion.native_publication_canonical_rows_encoded == 512
    assert ingestion.native_publication_canonical_bytes_encoded > len(source)
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0


def test_anonymous_re_scope_uses_the_authoritative_model_fallback(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    source = b"Ontology(<urn:retained-anonymous> ClassAssertion(<urn:C> _:person))"
    decode = native._decode_parsed_functional
    calls = 0

    def counted(data: bytes, limits: object) -> object:
        nonlocal calls
        calls += 1
        return decode(data, cast(Any, limits))

    monkeypatch.setattr(native, "_decode_parsed_functional", counted)
    selected = load_snapshot(source, options=_options(BackendPreference.NATIVE))

    assert selected.capabilities.backend == "python"
    assert type(selected).__name__ == "OntologySnapshot"
    assert calls == 1


def test_parser_built_storage_deduplicates_roots_but_preserves_origin_occurrences(
    extension: NativeTestExtension,
) -> None:
    source = (
        b"Ontology(<urn:retained-duplicate> "
        b"Declaration(Class(<urn:retained-duplicate:C>)) "
        b"Declaration(Class(<urn:retained-duplicate:C>)))"
    )
    reference = load_snapshot(source, options=_options(BackendPreference.PYTHON))
    selected = load_snapshot(source, options=_options(BackendPreference.NATIVE))
    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    counters = cast(Any, raw_owner)._publication_counters_v2()

    assert selected.origin_index == reference.origin_index
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert counters.parser_bytes == len(source)
    assert counters.canonical_input_rows == 1
    assert sum(len(rows) for rows in selected.origin_index.entries.values()) == 2


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
    assert after_python == before_python
    assert after_native.encoded_view_requests == before_native.encoded_view_requests + 1
    assert after_native.page_requests == before_native.page_requests + 1
    assert after_native.rows_emitted == before_native.rows_emitted + sum(
        len(rows) for rows in reference.origin_index.entries.values()
    )


def test_native_origin_records_reuse_each_page_validation_without_materialization(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(SOURCE, options=_options(BackendPreference.PYTHON))
    selected = load_snapshot(SOURCE, options=_options(BackendPreference.NATIVE))
    records = getattr(selected, "_native_origin_records_v2", None)
    assert callable(records)
    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    assert type(raw_owner) is cast(Any, extension)._NativeSnapshotHandle
    before_native = cast(Any, raw_owner)._publication_counters_v2()
    before_python = cast(Any, selected)._native_python_counters()
    original = native_handoff_v2.decode_native_auxiliary_row_v2
    decode_calls = 0

    def observed(*arguments: object, **keywords: object) -> object:
        nonlocal decode_calls
        decode_calls += 1
        return original(*arguments, **keywords)

    monkeypatch.setattr(native_handoff_v2, "decode_native_auxiliary_row_v2", observed)
    observed_records = tuple(records())
    after_native = cast(Any, raw_owner)._publication_counters_v2()
    after_python = cast(Any, selected)._native_python_counters()
    expected = tuple(
        (digest, item.document_key, item.occurrence, item.span)
        for digest, occurrences in reference.origin_index.entries.items()
        for item in occurrences
    )

    assert observed_records == expected
    assert decode_calls == len(expected)
    assert after_native.page_requests == before_native.page_requests + 1
    assert after_native.rows_emitted == before_native.rows_emitted + len(expected)
    assert after_python == before_python


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


def test_provenance_disabled_load_retains_parser_arena_without_origin_capability(
    extension: NativeTestExtension,
) -> None:
    options = LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.NATIVE,
        collect_provenance=False,
    )
    reference = load_snapshot(
        SOURCE,
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
            collect_provenance=False,
        ),
    )
    selected = load_snapshot(SOURCE, options=options)
    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    counters = cast(Any, raw_owner)._publication_counters_v2()
    ingestion = cast(Any, selected)._native_ingestion_counters_v2()

    assert type(raw_owner) is cast(Any, extension)._NativeSnapshotHandle
    assert selected.capabilities.backend == "native"
    assert selected.origin_index == reference.origin_index
    assert reference.root.origin_index is None
    assert selected.root.origin_index is None
    assert handle.attestation.capability_bits == 7
    assert counters.parser_bytes == len(SOURCE)
    assert counters.retained_origin_rows == 0
    assert counters.retained_origin_bytes == 0
    assert ingestion.structural_occurrence_rows_scanned == 3
    assert ingestion.eager_structural_objects_materialized == 0
    assert ingestion.provenance_occurrence_records_materialized == 0
    assert ingestion.canonical_bytes_copied_to_python == 0
    assert ingestion.fingerprint_preimage_bytes_materialized_in_python == 0
    assert ingestion.native_origin_rows_retained == 0
    assert ingestion.native_origin_bytes_retained == 0
    assert encode_snapshot(selected) == encode_snapshot(reference)


def test_small_auto_load_stays_python_selected_without_retained_native_parse(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    calls = 0

    def unexpected(*_arguments: object) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("small AUTO load crossed the retained native parser")

    monkeypatch.setattr(cast(Any, extension), "_parse_functional_retained_v2", unexpected)
    selected = load_snapshot(SOURCE, options=_options(BackendPreference.AUTO))

    assert selected.capabilities.backend == "python"
    assert type(selected).__name__ == "OntologySnapshot"
    assert calls == 0


def test_large_auto_load_retains_parser_arena_with_ignored_import_metadata(
    extension: NativeTestExtension,
) -> None:
    assert len(AUTO_SOURCE) > 256 * 1024
    reference = load_snapshot(AUTO_SOURCE, options=_options(BackendPreference.PYTHON))
    selected = load_snapshot(AUTO_SOURCE, options=_options(BackendPreference.AUTO))

    assert selected.capabilities.backend == "native"
    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.import_manifest == reference.import_manifest
    assert len(selected.import_manifest.edges) == 1
    assert selected.import_manifest.edges[0].status.value == "ignored"
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.origin_index == reference.origin_index

    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    assert type(raw_owner) is cast(Any, extension)._NativeSnapshotHandle
    before_native = cast(Any, raw_owner)._publication_counters_v2()
    before_python = cast(Any, selected)._native_python_counters()
    direct = selected.view(EncodedStructuralView)
    after_direct_native = cast(Any, raw_owner)._publication_counters_v2()
    after_direct_python = cast(Any, selected)._native_python_counters()
    expected_roots = tuple((2, canonical_bytes(value)) for value in reference.iter_axioms())

    assert before_native.parser_bytes == len(AUTO_SOURCE)
    assert direct.owner is selected
    assert decode_root_canonical_bytes(direct.buffers) == expected_roots
    assert len({id(value.obj) for value in direct.buffers.values()}) == 1
    assert after_direct_native.page_requests == before_native.page_requests
    assert after_direct_native.rows_emitted == before_native.rows_emitted
    assert after_direct_python.model_rows_materialized == before_python.model_rows_materialized
    assert encode_snapshot(selected) == encode_snapshot(reference)
    after_wire_python = cast(Any, selected)._native_python_counters()
    assert after_wire_python.model_rows_materialized == before_python.model_rows_materialized
    assert extension.INGESTION_FEATURES == ()
    assert extension.VIEW_FEATURES == ()
    assert not selected.capabilities.encoded_view_schemas

    selected.close()
    assert selected.closed
    assert decode_root_canonical_bytes(direct.buffers) == expected_roots


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
    finalize = cast(Any, extension)._finalize_parsed_structural_snapshot_v2

    def fail(
        parsed: object,
        attestation: object,
        cancel: object,
    ) -> object:
        nonlocal calls
        calls += 1
        tampered = replace(cast(Any, attestation), root_table_sha256=b"\x00" * 32)
        return finalize(parsed, tampered, cancel)

    monkeypatch.setattr(cast(Any, extension), "_finalize_parsed_structural_snapshot_v2", fail)
    with pytest.raises(BackendProtocolError) as raised:
        load_snapshot(SOURCE, options=_options(BackendPreference.NATIVE))
    assert raised.value.code == "NATIVE_PROTOCOL"
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
    assert observed["ingestion_parser_result_bytes"] == 0
    assert 0 < observed["ingestion_parser_summary_bytes"] < 1024
    assert observed["ingestion_canonical_rows_scanned"] == 4
    assert observed["ingestion_structural_occurrence_rows_scanned"] == 3
    assert observed["ingestion_structural_rows_published"] == 3
    assert observed["ingestion_eager_structural_objects"] == 0
    assert observed["ingestion_provenance_occurrence_records"] == 0
    assert observed["ingestion_canonical_bytes_copied_to_python"] == 0
    assert observed["ingestion_fingerprint_preimage_bytes_in_python"] == 0
    assert observed["ingestion_native_origin_rows_retained"] == 3
    assert observed["ingestion_native_origin_bytes_retained"] > 0
    assert observed["decoded_parity"] is True
    assert len(observed["wire_sha256"]) == 64
    assert len(observed["wire_python_sha256"]) == 64
    assert observed["wire_python_parity"] is True
    assert observed["wire_differing_sections"] == []
    assert observed["origin_parity"] is True
    assert observed["parser_bytes"] == len(
        b"Ontology(<urn:retained-installed> "
        b"Declaration(Class(<urn:retained-installed:C>)) "
        b"Declaration(Class(<urn:retained-installed:D>)) "
        b"SubClassOf(<urn:retained-installed:C> <urn:retained-installed:D>))"
    )
    assert {
        "native_syntax_parse_seconds",
        "native_result_encode_seconds",
        "native_arena_construction_seconds",
        "native_freeze_seconds",
        "native_publication_prepare_seconds",
        "root_parse_seconds",
    } <= observed["phase_timings"].keys()
    assert observed["provenance_disabled_parity"] is True
    assert observed["provenance_disabled_retained_origin_rows"] == 0
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
    assert observed["auto_backend"] == "native"
    assert observed["auto_retained_parity"] is True
    assert observed["auto_ignored_manifest_parity"] is True
    assert observed["auto_ingestion_eager_structural_objects"] == 0
    assert observed["auto_parser_bytes"] == observed["auto_source_bytes"] == 262281
    assert observed["auto_direct_survives_owner_close"] is True
    assert observed["auto_closed"] is True
    assert observed["unresolved_retained_parity"] is True
    assert observed["unresolved_snapshot_type"] == "_NativeOntologySnapshot"
    assert observed["ingestion_features"] == []
    assert observed["view_features"] == []
    assert observed["encoded_view_schemas"] == {}
    assert observed["wire_model_rows_materialized"] == 0
    assert observed["wire_encoded_view_requests"] == 1
    assert observed["wire_page_requests"] == 1
    assert observed["wire_rows_emitted"] == observed["retained_origin_rows"]
    if environment.get("PYOWL_CORE_TEST_NATIVE_LIBRARY") == "1":
        assert not Path(observed["package_file"]).is_relative_to(ROOT / "src")
