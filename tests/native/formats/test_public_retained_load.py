from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from pyowl_core import (
    AcquisitionCache,
    AxiomScope,
    BackendPreference,
    CancellationSource,
    DocumentFormat,
    EncodedStructuralView,
    ImportPolicy,
    ImportStatus,
    LoadOptions,
    MappingResolver,
    OperationCancelledError,
    ParsedDocumentCache,
    ParseError,
    ParseLimits,
    SnapshotLoader,
    UnresolvedImportWarning,
    encode_snapshot,
    load_snapshot,
)
from pyowl_core.backends import native, native_handoff_v2, native_ingestion
from pyowl_core.exceptions import BackendProtocolError, ResourceLimitError
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
    assert selected.root.origin_index == reference.root.origin_index
    assert selected.origin_index == reference.origin_index

    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    assert type(raw_owner) is cast(Any, extension)._NativeSnapshotHandle
    before = cast(Any, raw_owner)._publication_counters_v2()
    assert before.parser_bytes == len(SOURCE)
    assert before.retained_origin_rows == 3 * sum(
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


def test_anonymous_re_scope_retains_distinct_raw_and_effective_native_owners(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    source = b"Ontology(<urn:retained-anonymous> ClassAssertion(<urn:C> _:person))"
    reference = load_snapshot(source, options=_options(BackendPreference.PYTHON))
    def unexpected(*_arguments: object, **_keywords: object) -> object:
        raise AssertionError("anonymous retained load crossed the complete model decoder")

    monkeypatch.setattr(native, "_decode_parsed_functional", unexpected)
    selected = load_snapshot(source, options=_options(BackendPreference.NATIVE))
    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    owner = object.__getattribute__(handle, "_owner_v2")
    before_native = cast(Any, owner)._publication_counters_v2()
    before_python = cast(Any, selected)._native_python_counters()

    assert selected.capabilities.backend == "native"
    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert before_native.retained_axiom_rows == 3
    assert before_native.retained_origin_rows == 3
    assert before_native.page_requests == 0
    assert before_python.model_rows_materialized == 0
    assert before_python.auxiliary_rows_decoded == 0

    raw_axioms = tuple(canonical_bytes(value) for value in selected.root.axioms)
    reference_raw_axioms = tuple(canonical_bytes(value) for value in reference.root.axioms)
    effective_axioms = tuple(canonical_bytes(value) for value in selected.iter_axioms())
    reference_effective_axioms = tuple(canonical_bytes(value) for value in reference.iter_axioms())
    assert raw_axioms == reference_raw_axioms
    assert effective_axioms == reference_effective_axioms
    assert raw_axioms != effective_axioms
    assert tuple(selected.root.origin_index.entries) == tuple(reference.root.origin_index.entries)
    assert selected.origin_index == reference.origin_index

    reference_wire = encode_snapshot(reference)
    before_wire_native = cast(Any, owner)._publication_counters_v2()
    before_wire_python = cast(Any, selected)._native_python_counters()
    wire_error = AssertionError("anonymous retained wire crossed scalar traversal")
    with (
        patch.object(type(selected), "iter_axioms", side_effect=wire_error),
        patch.object(type(selected), "iter_extensions", side_effect=wire_error),
        patch.object(type(selected), "ontology_annotations", side_effect=wire_error),
        patch.object(type(selected), "signature", side_effect=wire_error),
    ):
        selected_wire = encode_snapshot(selected)
    after_wire_native = cast(Any, owner)._publication_counters_v2()
    after_wire_python = cast(Any, selected)._native_python_counters()
    assert selected_wire == reference_wire
    assert (
        after_wire_native.encoded_view_requests
        == before_wire_native.encoded_view_requests + 3
    )
    assert after_wire_python == before_wire_python

    after_native = after_wire_native
    after_python = after_wire_python
    assert after_native.axiom_rows_emitted > before_native.axiom_rows_emitted
    assert after_native.origin_rows_emitted > before_native.origin_rows_emitted
    assert after_python.model_rows_materialized > before_python.model_rows_materialized
    assert after_python.auxiliary_rows_decoded > before_python.auxiliary_rows_decoded


def test_anonymous_retained_occurrences_preserve_source_maps_and_duplicates(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    source = (
        b"Prefix(ex:=<urn:retained-anonymous-source:>) "
        b"Ontology(<urn:retained-anonymous-source> "
        b"ClassAssertion(ex:C _:left) ClassAssertion(ex:C _:left) "
        b"ObjectPropertyAssertion(ex:p _:left _:right))"
    )

    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=backend,
            collect_provenance=True,
            preserve_source_map=True,
        )

    reference = load_snapshot(source, options=options(BackendPreference.PYTHON))

    def unexpected(*_arguments: object, **_keywords: object) -> object:
        raise AssertionError("anonymous retained load crossed the complete model decoder")

    monkeypatch.setattr(native, "_decode_parsed_functional", unexpected)
    selected = load_snapshot(source, options=options(BackendPreference.NATIVE))
    ingestion = cast(Any, selected)._native_ingestion_counters_v2()

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert tuple(selected.root.origin_index.entries) == tuple(reference.root.origin_index.entries)
    assert tuple(
        (item.occurrence, item.span)
        for occurrences in selected.root.origin_index.entries.values()
        for item in occurrences
    ) == tuple(
        (item.occurrence, item.span)
        for occurrences in reference.root.origin_index.entries.values()
        for item in occurrences
    )
    assert selected.origin_index == reference.origin_index
    assert selected.root.source_map == reference.root.source_map
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert ingestion.parser_result_bytes_scanned == 0
    assert ingestion.eager_structural_objects_materialized == 0
    assert ingestion.structural_occurrence_rows_scanned == 3
    assert ingestion.structural_root_rows_published == 2
    assert sum(len(rows) for rows in selected.root.origin_index.entries.values()) == 3


def test_functional_anonymous_scoping_accounts_temporary_workspace() -> None:
    source = b"Ontology(<urn:retained-anonymous> ClassAssertion(<urn:C> _:person))"
    with pytest.raises(ResourceLimitError, match="max_temporary_bytes"):
        load_snapshot(
            source,
            options=LoadOptions(
                format=DocumentFormat.FUNCTIONAL,
                imports=ImportPolicy.IGNORE,
                backend=BackendPreference.NATIVE,
                collect_provenance=False,
                limits=replace(
                    ParseLimits(),
                    max_temporary_bytes=len(source),
                ),
            ),
        )


def test_functional_source_map_stays_in_parser_owned_storage_until_access(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    source = (
        b"Prefix(ex:=<urn:retained-source:>) Ontology(<urn:retained-source> "
        b"Declaration(Class(ex:C)) Declaration(Class(ex:C)) "
        b"SubClassOf(ex:C <http://www.w3.org/2002/07/owl#Thing>))"
    )

    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=backend,
            collect_provenance=False,
            preserve_source_map=True,
        )

    reference = load_snapshot(source, options=options(BackendPreference.PYTHON))

    def unexpected(*_arguments: object, **_keywords: object) -> object:
        raise AssertionError("eligible source-map load crossed the complete model decoder")

    monkeypatch.setattr(native, "_decode_parsed_functional", unexpected)
    selected = load_snapshot(source, options=options(BackendPreference.NATIVE))
    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    before_native = cast(Any, raw_owner)._publication_counters_v2()
    before_python = cast(Any, selected)._native_python_counters()

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.root.source_map is not None
    assert reference.root.source_map is not None
    assert handle.attestation.capability_bits == 15
    assert before_native.retained_source_map_rows == 3
    assert before_native.retained_source_prefix_rows == 5
    assert before_native.retained_source_bytes > 0
    assert before_native.source_map_rows_emitted == 0
    assert before_native.source_prefix_rows_emitted == 0
    assert before_python.auxiliary_rows_decoded == 0

    assert selected.root.source_map == reference.root.source_map
    after_native = cast(Any, raw_owner)._publication_counters_v2()
    after_python = cast(Any, selected)._native_python_counters()
    assert after_native.source_map_rows_emitted > before_native.source_map_rows_emitted
    assert after_native.source_prefix_rows_emitted > before_native.source_prefix_rows_emitted
    assert after_python.auxiliary_rows_decoded > before_python.auxiliary_rows_decoded
    assert encode_snapshot(selected) == encode_snapshot(reference)


def test_language_tagged_source_map_stays_owner_first_with_exact_lexical_rows(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    source = (
        b"Ontology(<urn:retained-language> "
        b"DataPropertyRange(<urn:p> DataOneOf("
        b'"v10"@EN "v09"@eN "v08"@En "v07"@en "v06"@EN "v05"@eN '
        b'"v04"@En "v03"@en "v02"@EN "v01"@eN "v00"@En)) '
        b'DataPropertyRange(<urn:q> DataOneOf("same"@PT-br "same"@pt-BR)))'
    )
    options = LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.NATIVE,
        collect_provenance=False,
        preserve_source_map=True,
    )
    reference = load_snapshot(
        source,
        options=replace(options, backend=BackendPreference.PYTHON),
    )

    def unexpected(*_arguments: object, **_keywords: object) -> object:
        raise AssertionError("language source map crossed the complete model decoder")

    monkeypatch.setattr(native, "_decode_parsed_functional", unexpected)
    selected = load_snapshot(source, options=options)
    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    before_native = cast(Any, raw_owner)._publication_counters_v2()

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.capabilities.backend == "native"
    assert handle.attestation.capability_bits == 15
    assert before_native.retained_source_map_rows == 14
    assert before_native.source_map_rows_emitted == 0
    assert selected.root.source_map == reference.root.source_map
    assert encode_snapshot(selected) == encode_snapshot(reference)

    assert selected.root.source_map is not None
    first_axiom = next(reference.iter_axioms())
    lexical = selected.root.source_map.occurrences_for(first_axiom)[0].lexical
    assert lexical == {
        "language-tag": "EN",
        "language-tag:2": "eN",
        "language-tag:3": "En",
        "language-tag:4": "en",
        "language-tag:5": "EN",
        "language-tag:6": "eN",
        "language-tag:7": "En",
        "language-tag:8": "en",
        "language-tag:9": "EN",
        "language-tag:10": "eN",
        "language-tag:11": "En",
    }
    after_native = cast(Any, raw_owner)._publication_counters_v2()
    assert after_native.source_map_rows_emitted > before_native.source_map_rows_emitted


def test_language_source_map_limit_counts_literal_rows() -> None:
    source = b'Ontology(AnnotationAssertion(<urn:p> <urn:s> "hi"@EN))'
    options = LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.NATIVE,
        collect_provenance=False,
        preserve_source_map=True,
        limits=replace(ParseLimits(), max_source_map_entries=1),
    )

    with pytest.raises(ResourceLimitError):
        load_snapshot(source, options=options)


def test_native_source_map_limit_fails_before_publication() -> None:
    options = LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.NATIVE,
        collect_provenance=False,
        preserve_source_map=True,
        limits=replace(ParseLimits(), max_source_map_entries=2),
    )
    with pytest.raises(ResourceLimitError):
        load_snapshot(SOURCE, options=options)


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

    source_mapped = load_snapshot(
        SOURCE,
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.NATIVE,
            preserve_source_map=True,
            collect_provenance=True,
        ),
    )
    assert source_mapped.capabilities.backend == "native"

    ineligible = load_snapshot(
        SOURCE,
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.NATIVE,
            collect_provenance=True,
            validate_owl2_dl=True,
        ),
    )
    assert ineligible.capabilities.backend == "python"

    imported = load_snapshot(
        b"Ontology(<urn:retained-root> Import(<urn:retained-child>))",
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.RESOLVE_LOCAL,
            backend=BackendPreference.NATIVE,
            preserve_source_map=True,
            collect_provenance=True,
            validate_owl2_dl=True,
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


@pytest.mark.parametrize("cancel_phase", ("entry", "canonical-row"))
def test_resolver_built_closure_cancellation_precedes_owner_publication(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
    cancel_phase: str,
) -> None:
    root = (
        b"Ontology(<urn:retained-cancel:root> Import(<urn:retained-cancel:child>) "
        b"Declaration(Class(<urn:retained-cancel:Root>)))"
    )
    child = (
        b"Ontology(<urn:retained-cancel:child> "
        b"Declaration(Class(<urn:retained-cancel:Child>)))"
    )
    options = LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.RESOLVE_LOCAL,
        backend=BackendPreference.NATIVE,
        collect_provenance=True,
        preserve_source_map=True,
    )
    real_retain = native_ingestion.retain_native_snapshot_v2
    with patch.object(
        native_ingestion,
        "retain_native_snapshot_v2",
        side_effect=lambda snapshot, **_keywords: snapshot,
    ):
        unpublished = load_snapshot(
            root,
            options=options,
            resolver=MappingResolver({"urn:retained-cancel:child": child}),
        )
    assert len(unpublished.documents) == 2
    assert all(document.provenance.backend == "native" for document in unpublished.documents)

    owner_calls = 0

    def unexpected(*_arguments: object, **_keywords: object) -> object:
        nonlocal owner_calls
        owner_calls += 1
        raise AssertionError("cancelled closure reached retained-owner construction")

    monkeypatch.setattr(cast(Any, extension), "_retain_structural_snapshot_v2", unexpected)
    cancellation = CancellationSource()
    if cancel_phase == "entry":
        cancellation.cancel("resolver-built closure cancelled at publication entry")
        with pytest.raises(OperationCancelledError, match="publication entry"):
            real_retain(unpublished, cancellation_token=cancellation.token)
    else:
        import pyowl_core.model as model_module

        canonical_rows = 0
        real_canonical_bytes = model_module.canonical_bytes

        def cancel_after_row(value: Any, *, limits: object | None = None) -> bytes:
            nonlocal canonical_rows
            encoded = real_canonical_bytes(value, limits=limits)
            canonical_rows += 1
            cancellation.cancel("resolver-built closure cancelled during canonical rows")
            return encoded

        with (
            patch.object(model_module, "canonical_bytes", side_effect=cancel_after_row),
            pytest.raises(OperationCancelledError, match="canonical rows"),
        ):
            real_retain(unpublished, cancellation_token=cancellation.token)
        assert canonical_rows == 1

    assert owner_calls == 0


def test_resolver_built_closure_partial_parse_failure_publishes_no_owner(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    owner_calls = 0

    def unexpected(*_arguments: object, **_keywords: object) -> object:
        nonlocal owner_calls
        owner_calls += 1
        raise AssertionError("partially parsed closure reached retained-owner construction")

    monkeypatch.setattr(cast(Any, extension), "_retain_structural_snapshot_v2", unexpected)
    monkeypatch.setattr(
        cast(Any, extension),
        "_merge_parsed_structural_snapshot_v2",
        unexpected,
    )
    parse_retained = cast(Any, extension)._parse_functional_retained_v2
    root = (
        b"Ontology(<urn:retained-partial:root> "
        b"Import(<urn:retained-partial:good>) "
        b"Import(<urn:retained-partial:malformed>) "
        b"Declaration(Class(<urn:retained-partial:Root>)))"
    )
    sources = {
        "urn:retained-partial:good": (
            b"Ontology(<urn:retained-partial:good> "
            b"Declaration(Class(<urn:retained-partial:Good>)))"
        ),
        "urn:retained-partial:malformed": (
            b"Ontology(<urn:retained-partial:malformed> this is not valid)"
        ),
    }
    parse_barrier = threading.Barrier(len(sources))
    thread_ids: set[int] = set()
    thread_lock = threading.Lock()

    def capture_parallel_parse(
        request: object,
        *arguments: object,
        **keywords: object,
    ) -> object:
        assert isinstance(request, bytes)
        matches = tuple(source for source in sources.values() if request.endswith(source))
        if matches:
            assert len(matches) == 1
            with thread_lock:
                thread_ids.add(threading.get_ident())
            parse_barrier.wait(timeout=5)
        return parse_retained(request, *arguments, **keywords)

    monkeypatch.setattr(
        cast(Any, extension),
        "_parse_functional_retained_v2",
        capture_parallel_parse,
    )
    resolver = MappingResolver(sources)

    with pytest.raises(ParseError):
        SnapshotLoader(
            acquisition_cache=AcquisitionCache(),
            document_cache=ParsedDocumentCache(),
        ).load(
            root,
            options=LoadOptions(
                format=DocumentFormat.FUNCTIONAL,
                imports=ImportPolicy.RESOLVE_LOCAL,
                backend=BackendPreference.NATIVE,
                limits=replace(ParseLimits(), max_concurrent_fetches=2),
            ),
            resolver=resolver,
        )

    assert len(thread_ids) == len(sources) == 2
    assert owner_calls == 0


def test_parallel_resolved_functional_parse_cancellation_publishes_no_owner(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    root = (
        b"Ontology(<urn:retained-parallel-cancel:root> "
        b"Import(<urn:retained-parallel-cancel:left>) "
        b"Import(<urn:retained-parallel-cancel:right>) "
        b"Declaration(Class(<urn:retained-parallel-cancel:Root>)))"
    )
    children = {
        "urn:retained-parallel-cancel:left": (
            b"Ontology(<urn:retained-parallel-cancel:left> "
            b"Declaration(Class(<urn:retained-parallel-cancel:Left>)))"
        ),
        "urn:retained-parallel-cancel:right": (
            b"Ontology(<urn:retained-parallel-cancel:right> "
            b"Declaration(Class(<urn:retained-parallel-cancel:Right>)))"
        ),
    }
    child_sources = frozenset(children.values())
    parse_barrier = threading.Barrier(len(child_sources))
    thread_ids: set[int] = set()
    thread_lock = threading.Lock()
    cancellation = CancellationSource()
    parse_retained = cast(Any, extension)._parse_functional_retained_v2

    def cancel_parallel_parse(
        request: object,
        *arguments: object,
        **keywords: object,
    ) -> object:
        assert isinstance(request, bytes)
        matches = tuple(source for source in child_sources if request.endswith(source))
        if matches:
            assert len(matches) == 1
            with thread_lock:
                thread_ids.add(threading.get_ident())
            parse_barrier.wait(timeout=5)
            cancellation.cancel("parallel import parse cancelled")
        return parse_retained(request, *arguments, **keywords)

    owner_calls = 0

    def unexpected_owner(*_arguments: object, **_keywords: object) -> object:
        nonlocal owner_calls
        owner_calls += 1
        raise AssertionError("cancelled parallel parse reached owner publication")

    monkeypatch.setattr(
        cast(Any, extension),
        "_parse_functional_retained_v2",
        cancel_parallel_parse,
    )
    monkeypatch.setattr(
        cast(Any, extension),
        "_retain_structural_snapshot_v2",
        unexpected_owner,
    )
    monkeypatch.setattr(
        cast(Any, extension),
        "_merge_parsed_structural_snapshot_v2",
        unexpected_owner,
    )

    with pytest.raises(OperationCancelledError, match="parallel import parse cancelled"):
        SnapshotLoader(
            acquisition_cache=AcquisitionCache(),
            document_cache=ParsedDocumentCache(),
        ).load(
            root,
            options=LoadOptions(
                format=DocumentFormat.FUNCTIONAL,
                imports=ImportPolicy.RESOLVE_LOCAL,
                backend=BackendPreference.NATIVE,
                limits=replace(ParseLimits(), max_concurrent_fetches=2),
            ),
            resolver=MappingResolver(children),
            cancellation_token=cancellation.token,
        )

    assert len(thread_ids) == len(child_sources) == 2
    assert owner_calls == 0


@pytest.mark.parametrize("collect_provenance", (False, True))
@pytest.mark.parametrize("preserve_source_map", (False, True))
@pytest.mark.parametrize(
    "policy",
    (ImportPolicy.RESOLVE_LOCAL, ImportPolicy.RESOLVE_STRICT),
)
def test_resolved_functional_diamond_retains_one_native_closure_owner(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
    collect_provenance: bool,
    preserve_source_map: bool,
    policy: ImportPolicy,
) -> None:
    root = (
        b"Ontology(<urn:retained-closure:root> "
        b"Import(<urn:retained-closure:left>) "
        b"Import(<urn:retained-closure:right>) "
        b"Declaration(Class(<urn:retained-closure:Root>)))"
    )
    sources: dict[str, Any] = {
        "urn:retained-closure:left": (
            b"Ontology(<urn:retained-closure:left> "
            b"Import(<urn:retained-closure:leaf>) "
            b"Declaration(Class(<urn:retained-closure:Left>)))"
        ),
        "urn:retained-closure:right": (
            b"Ontology(<urn:retained-closure:right> "
            b"Import(<urn:retained-closure:leaf>) "
            b"Declaration(Class(<urn:retained-closure:Right>)))"
        ),
        "urn:retained-closure:leaf": (
            b"Ontology(<urn:retained-closure:leaf> Declaration(Class(<urn:retained-closure:Leaf>)))"
        ),
    }

    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=policy,
            backend=backend,
            collect_provenance=collect_provenance,
            preserve_source_map=preserve_source_map,
            limits=replace(ParseLimits(), max_concurrent_fetches=2),
        )

    reference = load_snapshot(
        root,
        options=options(BackendPreference.PYTHON),
        resolver=MappingResolver(sources),
    )
    sequential = SnapshotLoader(
        acquisition_cache=AcquisitionCache(),
        document_cache=ParsedDocumentCache(),
    ).load(
        root,
        options=replace(
            options(BackendPreference.NATIVE),
            limits=replace(ParseLimits(), max_concurrent_fetches=1),
        ),
        resolver=MappingResolver(sources),
    )
    parse_retained = cast(Any, extension)._parse_functional_retained_v2
    parsed_sources: list[bytes] = []
    materialized_sources: list[bytes] = []
    expected_sources = (root, *sources.values())
    parallel_sources = frozenset(
        (sources["urn:retained-closure:left"], sources["urn:retained-closure:right"])
    )
    parallel_barrier = threading.Barrier(len(parallel_sources))
    parallel_thread_ids: set[int] = set()
    parallel_thread_lock = threading.Lock()

    def record_source(request: object) -> bytes:
        assert isinstance(request, bytes)
        matches = tuple(source for source in expected_sources if request.endswith(source))
        assert len(matches) == 1
        parsed_sources.append(matches[0])
        return matches[0]

    def capture_retained_parse(
        request: object,
        *arguments: object,
        **keywords: object,
    ) -> object:
        source = record_source(request)
        if keywords.get("materialize_document") is True:
            materialized_sources.append(source)
        if source in parallel_sources:
            with parallel_thread_lock:
                parallel_thread_ids.add(threading.get_ident())
            parallel_barrier.wait(timeout=5)
        return parse_retained(request, *arguments, **keywords)

    monkeypatch.setattr(
        cast(Any, extension),
        "_parse_functional_retained_v2",
        capture_retained_parse,
    )
    merge_retained = cast(Any, extension)._merge_parsed_structural_snapshot_v2
    captured: dict[str, object] = {}

    def capture(
        parsed_documents: object,
        origins: object,
        attestation: object,
        config: object,
        cancel: object,
        **keywords: object,
    ) -> object:
        captured["parsed_documents"] = parsed_documents
        captured["origins"] = origins
        captured.update(keywords)
        return merge_retained(
            parsed_documents,
            origins,
            attestation,
            config,
            cancel,
            **keywords,
        )

    def unexpected_structural(*_arguments: object, **_keywords: object) -> object:
        raise AssertionError("parser-built closure crossed Python structural retention")

    monkeypatch.setattr(
        cast(Any, extension),
        "_merge_parsed_structural_snapshot_v2",
        capture,
    )
    monkeypatch.setattr(
        cast(Any, extension),
        "_retain_structural_snapshot_v2",
        unexpected_structural,
    )
    selected = SnapshotLoader(
        acquisition_cache=AcquisitionCache(),
        document_cache=ParsedDocumentCache(),
    ).load(
        root,
        options=options(BackendPreference.NATIVE),
        resolver=MappingResolver(sources),
    )

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.capabilities.backend == "native"
    assert not selected.capabilities.encoded_view_schemas
    assert len(selected.documents) == len(reference.documents) == 4
    assert len(selected.import_manifest.edges) == 4
    assert selected.import_manifest == reference.import_manifest
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.origin_index == reference.origin_index
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert selected.import_manifest == sequential.import_manifest
    assert selected.structural_fingerprint == sequential.structural_fingerprint
    assert selected.logical_fingerprint == sequential.logical_fingerprint
    assert selected.signature_fingerprint == sequential.signature_fingerprint
    assert selected.origin_index == sequential.origin_index
    assert encode_snapshot(selected) == encode_snapshot(sequential)
    assert selected.report.acquisition_cache_hits == 1
    assert selected.report.document_cache_hits == 1
    assert len(parsed_sources) == len(expected_sources) == 4
    assert all(parsed_sources.count(source) == 1 for source in expected_sources)
    assert len(materialized_sources) == len(sources) == 3
    assert root not in materialized_sources
    assert all(materialized_sources.count(source) == 1 for source in sources.values())
    assert len(parallel_thread_ids) == len(parallel_sources) == 2

    parsed_documents = cast(tuple[object, ...], captured["parsed_documents"])
    assert len(parsed_documents) == 4
    assert all(
        type(value) is cast(Any, extension)._NativeParsedStructuralStorageV2
        for value in parsed_documents
    )
    reference_origin_rows = sum(
        len(occurrences) for occurrences in reference.origin_index.entries.values()
    )
    if collect_provenance:
        captured_origins = cast(tuple[tuple[bytes, ...], ...], captured["origins"])
        assert len(captured_origins) == 4
        assert sum(len(rows) for rows in captured_origins) == reference_origin_rows
    else:
        assert captured["origins"] is None
    if preserve_source_map:
        captured_source_maps = cast(
            tuple[tuple[tuple[bytes, ...], tuple[bytes, ...]], ...],
            captured["source_maps"],
        )
        assert len(captured_source_maps) == 4
        assert sum(len(entries) for entries, _prefixes in captured_source_maps) == sum(
            len(occurrences)
            for document in reference.documents
            for occurrences in cast(Any, document.source_map).entries.values()
        )
        assert sum(len(prefixes) for _entries, prefixes in captured_source_maps) == sum(
            len(cast(Any, document.source_map).prefixes) for document in reference.documents
        )
    else:
        assert captured["source_maps"] is None
    assert captured["effective_origins"] is None
    assert captured["effective_document_ordinals"] == ((0,), (1,), (2,), (3,))
    assert captured["closure_document_ordinals"] == (0, 1, 2, 3)

    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    counters = cast(Any, raw_owner)._publication_counters_v2()
    assert counters.retained_document_tables == 4
    assert counters.canonical_input_rows == 4
    assert counters.retained_origin_rows == (2 * reference_origin_rows if collect_provenance else 0)
    expected_source_rows = (
        sum(
            len(occurrences)
            for document in reference.documents
            for occurrences in cast(Any, document.source_map).entries.values()
        )
        if preserve_source_map
        else 0
    )
    expected_prefix_rows = (
        sum(len(cast(Any, document.source_map).prefixes) for document in reference.documents)
        if preserve_source_map
        else 0
    )
    assert counters.retained_source_map_rows == expected_source_rows
    assert counters.retained_source_prefix_rows == expected_prefix_rows
    assert counters.source_map_rows_emitted == 0
    assert counters.source_prefix_rows_emitted == 0
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0
    assert handle.attestation.capability_bits == (
        7 | (8 if preserve_source_map else 0) | (16 if collect_provenance else 0)
    )
    if preserve_source_map:
        assert tuple(document.source_map for document in selected.documents) == tuple(
            document.source_map for document in reference.documents
        )
        after_source_maps = cast(Any, raw_owner)._publication_counters_v2()
        assert after_source_maps.source_map_rows_emitted > counters.source_map_rows_emitted
        assert after_source_maps.source_prefix_rows_emitted > counters.source_prefix_rows_emitted

    scalar_error = AssertionError("closure encoded view crossed scalar traversal")
    with (
        patch.object(type(selected), "iter_axioms", side_effect=scalar_error),
        patch.object(type(selected), "iter_extensions", side_effect=scalar_error),
        patch.object(type(selected), "ontology_annotations", side_effect=scalar_error),
        patch.object(type(selected), "signature", side_effect=scalar_error),
    ):
        encoded = selected.view(EncodedStructuralView)
    expected = tuple((2, canonical_bytes(value)) for value in reference.iter_axioms())
    assert decode_root_canonical_bytes(encoded.buffers) == expected
    assert encoded.owner is selected
    assert len({id(value.obj) for value in encoded.buffers.values()}) == 1
    selected.close()
    assert selected.closed
    assert decode_root_canonical_bytes(encoded.buffers) == expected


@pytest.mark.parametrize("collect_provenance", (False, True))
@pytest.mark.parametrize("preserve_source_map", (False, True))
def test_record_unresolved_mixed_closure_retains_resolved_documents_and_diagnostics(
    extension: NativeTestExtension,
    collect_provenance: bool,
    preserve_source_map: bool,
) -> None:
    root = (
        b"Ontology(<urn:retained-record:root> "
        b"Import(<urn:retained-record:child>) "
        b"Import(<urn:retained-record:missing>) "
        b"Declaration(Class(<urn:retained-record:Root>)))"
    )
    child = (
        b"Ontology(<urn:retained-record:child> "
        b"Declaration(Class(<urn:retained-record:Child>)))"
    )

    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.RECORD_UNRESOLVED,
            backend=backend,
            collect_provenance=collect_provenance,
            preserve_source_map=preserve_source_map,
        )

    resolver = MappingResolver({"urn:retained-record:child": child})
    with pytest.warns(UnresolvedImportWarning):
        reference = load_snapshot(
            root,
            options=options(BackendPreference.PYTHON),
            resolver=resolver,
        )
    with pytest.warns(UnresolvedImportWarning):
        selected = load_snapshot(
            root,
            options=options(BackendPreference.NATIVE),
            resolver=resolver,
        )

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert len(selected.documents) == len(reference.documents) == 2
    assert selected.import_manifest == reference.import_manifest
    assert {edge.status for edge in selected.import_manifest.edges} == {
        ImportStatus.RESOLVED,
        ImportStatus.UNRESOLVED,
    }
    unresolved = next(
        edge
        for edge in selected.import_manifest.edges
        if edge.status is ImportStatus.UNRESOLVED
    )
    assert unresolved.import_iri.value == "urn:retained-record:missing"
    assert unresolved.resolver_name == "mapping"
    assert unresolved.diagnostic is not None
    assert unresolved.diagnostic.code == "UNRESOLVED_IMPORT"
    assert selected.report.diagnostics == reference.report.diagnostics
    assert selected.report.resolution_attempts == reference.report.resolution_attempts == 2
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.origin_index == reference.origin_index
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert tuple(document.source_map for document in selected.documents) == tuple(
        document.source_map for document in reference.documents
    )

    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    counters = cast(Any, raw_owner)._publication_counters_v2()
    assert counters.retained_document_tables == 2
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0
    assert handle.attestation.capability_bits == (
        7 | (8 if preserve_source_map else 0) | (16 if collect_provenance else 0)
    )
    assert extension.INGESTION_FEATURES == ()


@pytest.mark.parametrize("collect_provenance", (False, True))
@pytest.mark.parametrize("preserve_source_map", (False, True))
def test_large_auto_resolver_built_closure_retains_one_native_owner(
    extension: NativeTestExtension,
    collect_provenance: bool,
    preserve_source_map: bool,
) -> None:
    padding = b" " * (256 * 1024)
    root = (
        b"Ontology(<urn:retained-auto-closure:root> "
        b"Import(<urn:retained-auto-closure:child>) "
        + padding
        + b"Declaration(Class(<urn:retained-auto-closure:Root>)))"
    )
    child = (
        b"Ontology(<urn:retained-auto-closure:child> "
        + padding
        + b"Declaration(Class(<urn:retained-auto-closure:Child>)))"
    )

    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.RESOLVE_LOCAL,
            backend=backend,
            collect_provenance=collect_provenance,
            preserve_source_map=preserve_source_map,
        )

    resolver = MappingResolver({"urn:retained-auto-closure:child": child})
    reference = load_snapshot(
        root,
        options=options(BackendPreference.PYTHON),
        resolver=resolver,
    )
    selected = load_snapshot(
        root,
        options=options(BackendPreference.AUTO),
        resolver=resolver,
    )

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert all(document.provenance.backend == "native" for document in selected.documents)
    assert len(selected.documents) == len(reference.documents) == 2
    assert selected.import_manifest == reference.import_manifest
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.origin_index == reference.origin_index
    assert tuple(document.source_map for document in selected.documents) == tuple(
        document.source_map for document in reference.documents
    )
    assert encode_snapshot(selected) == encode_snapshot(reference)

    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    counters = cast(Any, raw_owner)._publication_counters_v2()
    assert counters.retained_document_tables == 2
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0
    assert handle.attestation.capability_bits == (
        7 | (8 if preserve_source_map else 0) | (16 if collect_provenance else 0)
    )
    assert extension.INGESTION_FEATURES == ()


@pytest.mark.parametrize("small_document", ("root", "child"))
def test_mixed_size_auto_closure_skips_owner_construction(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
    small_document: str,
) -> None:
    padding = b" " * (256 * 1024)
    root_padding = b"" if small_document == "root" else padding
    child_padding = b"" if small_document == "child" else padding
    root = (
        b"Ontology(<urn:retained-auto-mixed:root> "
        b"Import(<urn:retained-auto-mixed:child>) "
        + root_padding
        + b"Declaration(Class(<urn:retained-auto-mixed:Root>)))"
    )
    child = (
        b"Ontology(<urn:retained-auto-mixed:child> "
        + child_padding
        + b"Declaration(Class(<urn:retained-auto-mixed:Child>)))"
    )
    resolver = MappingResolver({"urn:retained-auto-mixed:child": child})

    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.RESOLVE_LOCAL,
            backend=backend,
            collect_provenance=True,
            preserve_source_map=True,
        )

    reference = load_snapshot(
        root,
        options=options(BackendPreference.PYTHON),
        resolver=resolver,
    )
    owner_calls = 0

    def unexpected(*_arguments: object, **_keywords: object) -> object:
        nonlocal owner_calls
        owner_calls += 1
        raise AssertionError("mixed AUTO closure reached retained-owner construction")

    monkeypatch.setattr(cast(Any, extension), "_retain_structural_snapshot_v2", unexpected)
    selected = load_snapshot(
        root,
        options=options(BackendPreference.AUTO),
        resolver=resolver,
    )

    assert type(selected).__name__ == "OntologySnapshot"
    assert {document.provenance.backend for document in selected.documents} == {
        "native",
        "python",
    }
    assert selected.import_manifest == reference.import_manifest
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.origin_index == reference.origin_index
    assert tuple(document.source_map for document in selected.documents) == tuple(
        document.source_map for document in reference.documents
    )
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert owner_calls == 0


@pytest.mark.parametrize("collect_provenance", (False, True))
@pytest.mark.parametrize("preserve_source_map", (False, True))
@pytest.mark.parametrize(
    "policy",
    (ImportPolicy.RESOLVE_LOCAL, ImportPolicy.RESOLVE_STRICT),
)
def test_resolved_functional_cycle_retains_distinct_anonymous_document_scopes(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
    collect_provenance: bool,
    preserve_source_map: bool,
    policy: ImportPolicy,
) -> None:
    root = (
        b"Ontology(<urn:retained-cycle:a> Import(<urn:retained-cycle:b>) "
        b"ClassAssertion(<urn:retained-cycle:C> _:person))"
    )
    child = (
        b"Ontology(<urn:retained-cycle:b> Import(<urn:retained-cycle:a>) "
        b"ClassAssertion(<urn:retained-cycle:C> _:person))"
    )
    sources: dict[str, Any] = {
        "urn:retained-cycle:a": root,
        "urn:retained-cycle:b": child,
    }

    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=policy,
            backend=backend,
            collect_provenance=collect_provenance,
            preserve_source_map=preserve_source_map,
        )

    reference = load_snapshot(
        root,
        options=options(BackendPreference.PYTHON),
        resolver=MappingResolver(sources),
    )
    merge_retained = cast(Any, extension)._merge_parsed_structural_snapshot_v2
    captured: dict[str, object] = {}

    def capture(
        parsed_documents: object,
        origins: object,
        attestation: object,
        config: object,
        cancel: object,
        **keywords: object,
    ) -> object:
        captured["parsed_documents"] = parsed_documents
        captured["origins"] = origins
        captured.update(keywords)
        return merge_retained(
            parsed_documents,
            origins,
            attestation,
            config,
            cancel,
            **keywords,
        )

    def unexpected_structural(*_arguments: object, **_keywords: object) -> object:
        raise AssertionError("anonymous parser owners crossed Python structural retention")

    monkeypatch.setattr(
        cast(Any, extension),
        "_merge_parsed_structural_snapshot_v2",
        capture,
    )
    monkeypatch.setattr(
        cast(Any, extension),
        "_retain_structural_snapshot_v2",
        unexpected_structural,
    )
    selected = load_snapshot(
        root,
        options=options(BackendPreference.NATIVE),
        resolver=MappingResolver(sources),
    )

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert len(selected.documents) == len(reference.documents) == 2
    assert len(selected.import_manifest.edges) == 2
    assert all(edge.status is ImportStatus.RESOLVED for edge in selected.import_manifest.edges)
    assert selected.import_manifest == reference.import_manifest
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.origin_index == reference.origin_index
    assert encode_snapshot(selected) == encode_snapshot(reference)
    parsed_documents = cast(tuple[object, ...], captured["parsed_documents"])
    assert len(parsed_documents) == 2
    assert all(
        type(value) is cast(Any, extension)._NativeParsedStructuralStorageV2
        for value in parsed_documents
    )
    assert captured["effective_document_ordinals"] == ((0,), (1,))
    assert captured["closure_document_ordinals"] == (0, 1)
    assert captured["anonymous_scope_targets"] == (None, None)
    if preserve_source_map:
        source_maps = cast(
            tuple[tuple[tuple[bytes, ...], tuple[bytes, ...]], ...],
            captured["source_maps"],
        )
        assert len(source_maps) == 2
    else:
        assert captured["source_maps"] is None

    raw_rows = tuple(
        tuple(canonical_bytes(value) for value in document.axioms)
        for document in selected.documents
    )
    assert raw_rows[0] != raw_rows[1]
    if collect_provenance:
        raw_origins = cast(tuple[tuple[bytes, ...], ...], captured["origins"])
        effective_origins = cast(
            tuple[tuple[bytes, ...], ...],
            captured["effective_origins"],
        )
        assert len(raw_origins) == len(effective_origins) == 2
        effective_origin_count = sum(len(rows) for rows in effective_origins)
        assert effective_origin_count == sum(
            len(occurrences) for occurrences in reference.origin_index.entries.values()
        )
        handle = cast(Any, selected)._native_snapshot_state.owner.handle
        raw_owner = object.__getattribute__(handle, "_owner_v2")
        counters = cast(Any, raw_owner)._publication_counters_v2()
        assert counters.retained_origin_rows == (
            sum(len(rows) for rows in raw_origins) + 2 * effective_origin_count
        )
    else:
        assert captured["origins"] is None
        assert captured["effective_origins"] is None
    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    before_source_maps = cast(Any, raw_owner)._publication_counters_v2()
    assert before_source_maps.canonical_input_rows == 2
    assert before_source_maps.publication_structural_rows_copied == 0
    assert before_source_maps.publication_structural_bytes_copied == 0
    expected_source_rows = (
        sum(
            len(occurrences)
            for document in reference.documents
            for occurrences in cast(Any, document.source_map).entries.values()
        )
        if preserve_source_map
        else 0
    )
    expected_prefix_rows = (
        sum(len(cast(Any, document.source_map).prefixes) for document in reference.documents)
        if preserve_source_map
        else 0
    )
    assert before_source_maps.retained_source_map_rows == expected_source_rows
    assert before_source_maps.retained_source_prefix_rows == expected_prefix_rows
    assert before_source_maps.source_map_rows_emitted == 0
    assert before_source_maps.source_prefix_rows_emitted == 0
    assert handle.attestation.capability_bits == (
        7 | (8 if preserve_source_map else 0) | (16 if collect_provenance else 0)
    )
    if preserve_source_map:
        selected_source_maps = tuple(document.source_map for document in selected.documents)
        reference_source_maps = tuple(document.source_map for document in reference.documents)
        assert selected_source_maps == reference_source_maps
        after_source_maps = cast(Any, raw_owner)._publication_counters_v2()
        assert (
            after_source_maps.source_map_rows_emitted > before_source_maps.source_map_rows_emitted
        )
        assert (
            after_source_maps.source_prefix_rows_emitted
            > before_source_maps.source_prefix_rows_emitted
        )
    effective_rows = tuple(
        tuple(
            canonical_bytes(value)
            for value in selected.iter_axioms(
                scope=AxiomScope.DOCUMENT,
                document_key=record.document_key,
            )
        )
        for record in selected.import_manifest.documents
    )
    assert effective_rows[0] != effective_rows[1]
    assert len(tuple(selected.iter_axioms())) == 2


def test_repeated_anonymous_fingerprint_group_is_rescoped_inside_native_composition(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    root = (
        b"Ontology(<urn:retained-repeat:root> "
        b"Import(<urn:retained-repeat:first>) "
        b"Import(<urn:retained-repeat:second>) "
        b"Import(<urn:retained-repeat:third>) "
        b"Declaration(Class(<urn:retained-repeat:Root>)))"
    )
    first = b"Ontology(ClassAssertion(<urn:retained-repeat:C> _:person))"
    second = b"Ontology(  ClassAssertion(<urn:retained-repeat:C> _:person)  )"
    third = b"Ontology(\nClassAssertion(<urn:retained-repeat:C> _:person)\n)"
    sources = {
        "urn:retained-repeat:first": first,
        "urn:retained-repeat:second": second,
        "urn:retained-repeat:third": third,
    }

    def selected_options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.RESOLVE_STRICT,
            backend=backend,
            collect_provenance=True,
            preserve_source_map=True,
        )

    reference = SnapshotLoader(
        acquisition_cache=AcquisitionCache(),
        document_cache=ParsedDocumentCache(),
    ).load(
        root,
        options=selected_options(BackendPreference.PYTHON),
        resolver=MappingResolver(sources),
    )
    merge_retained = cast(Any, extension)._merge_parsed_structural_snapshot_v2
    captured: dict[str, object] = {}

    def capture(
        parsed_documents: object,
        origins: object,
        attestation: object,
        config: object,
        cancel: object,
        **keywords: object,
    ) -> object:
        captured["parsed_documents"] = parsed_documents
        captured.update(keywords)
        return merge_retained(
            parsed_documents,
            origins,
            attestation,
            config,
            cancel,
            **keywords,
        )

    def unexpected_structural(*_arguments: object, **_keywords: object) -> object:
        raise AssertionError("repeated anonymous owners crossed Python structural retention")

    monkeypatch.setattr(
        cast(Any, extension),
        "_merge_parsed_structural_snapshot_v2",
        capture,
    )
    monkeypatch.setattr(
        cast(Any, extension),
        "_retain_structural_snapshot_v2",
        unexpected_structural,
    )
    selected = SnapshotLoader(
        acquisition_cache=AcquisitionCache(),
        document_cache=ParsedDocumentCache(),
    ).load(
        root,
        options=selected_options(BackendPreference.NATIVE),
        resolver=MappingResolver(sources),
    )

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.import_manifest == reference.import_manifest
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.origin_index == reference.origin_index
    assert tuple(document.source_map for document in selected.documents) == tuple(
        document.source_map for document in reference.documents
    )
    assert encode_snapshot(selected) == encode_snapshot(reference)
    parsed_documents = cast(tuple[object, ...], captured["parsed_documents"])
    assert len(parsed_documents) == 4
    scope_targets = cast(tuple[bytes | None, ...], captured["anonymous_scope_targets"])
    assert len(scope_targets) == 4
    assert sum(target is not None for target in scope_targets) == 2
    assert all(target is None or len(target) == 32 for target in scope_targets)

    anonymous_records = tuple(
        (record, document)
        for record, document in zip(
            selected.import_manifest.documents,
            selected.documents,
            strict=True,
        )
        if document.ontology_id.ontology_iri is None
    )
    assert len(anonymous_records) == 3
    assert len({record.document_fingerprint for record, _document in anonymous_records}) == 1
    assert len({record.source_sha256 for record, _document in anonymous_records}) == 3
    raw_rows = {
        tuple(canonical_bytes(value) for value in document.axioms)
        for _record, document in anonymous_records
    }
    assert len(raw_rows) == 1
    effective_rows = tuple(
        tuple(
            canonical_bytes(value)
            for value in selected.iter_axioms(
                scope=AxiomScope.DOCUMENT,
                document_key=record.document_key,
            )
        )
        for record, _document in anonymous_records
    )
    assert len(set(effective_rows)) == 3
    assert len(tuple(selected.iter_axioms())) == 4


def test_eligible_owner_construction_failure_propagates_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    calls = 0
    finalize = cast(Any, extension)._finalize_parsed_structural_snapshot_v2

    def fail(
        parsed: object,
        prepared_summary: object,
        attestation: object,
        cancel: object,
    ) -> object:
        nonlocal calls
        calls += 1
        tampered = replace(cast(Any, attestation), root_table_sha256=b"\x00" * 32)
        return finalize(parsed, prepared_summary, tampered, cancel)

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
    assert observed["summary_fingerprint_parity"] is True
    assert observed["summary_inventory_parity"] is True
    assert observed["summary_node_count_parity"] is True
    assert observed["summary_root_count_parity"] is True
    assert observed["summary_zero_work"] is True
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
    assert observed["decoded_root_parity"] is True
    assert observed["overlay_root_parity"] is True
    assert observed["overlay_owner_identity"] is True
    assert observed["overlay_scalar_traversal_calls"] == 0
    assert observed["overlay_referenced_copy_bytes"] == 0
    assert observed["composite_root_parity"] is True
    assert observed["composite_owner_identity"] is True
    assert observed["composite_scalar_traversal_calls"] == 0
    assert observed["composite_referenced_copy_bytes"] == 0
    assert observed["segmented_left_model_rows"] == 0
    assert observed["segmented_left_page_requests"] == 0
    assert observed["segmented_left_rows_emitted"] == 0
    assert observed["segmented_right_model_rows"] == 0
    assert observed["segmented_right_page_requests"] == 0
    assert observed["segmented_right_rows_emitted"] == 0
    assert observed["hostile_descriptor_code"] == "ENCODED_VIEW_DESCRIPTOR"
    assert observed["syntax_error_code"] == "FORMAT_SYNTAX"
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
    assert observed["right_selected_closed"] is True
    assert observed["auto_backend"] == "native"
    assert observed["auto_retained_parity"] is True
    assert observed["auto_ignored_manifest_parity"] is True
    assert observed["auto_ingestion_eager_structural_objects"] == 0
    assert observed["auto_parser_bytes"] == observed["auto_source_bytes"] == 262281
    assert observed["auto_direct_survives_owner_close"] is True
    assert observed["auto_closed"] is True
    assert observed["anonymous_retained_parity"] is True
    assert observed["anonymous_snapshot_type"] == "_NativeOntologySnapshot"
    assert observed["anonymous_parser_result_bytes"] == 0
    assert observed["unresolved_retained_parity"] is True
    assert observed["unresolved_snapshot_type"] == "_NativeOntologySnapshot"
    assert observed["empty_closure_parity"] == {
        "resolve_local": True,
        "resolve_strict": True,
    }
    assert observed["empty_closure_snapshot_types"] == {
        "resolve_local": "_NativeOntologySnapshot",
        "resolve_strict": "_NativeOntologySnapshot",
    }
    assert observed["empty_closure_parser_result_bytes"] == {
        "resolve_local": 0,
        "resolve_strict": 0,
    }
    assert observed["ingestion_features"] == []
    assert observed["view_features"] == []
    assert observed["encoded_view_schemas"] == {}
    assert observed["wire_model_rows_materialized"] == 0
    assert observed["wire_encoded_view_requests"] == 1
    assert observed["wire_page_requests"] == 1
    assert observed["wire_rows_emitted"] == observed["retained_origin_rows"]
    if environment.get("PYOWL_CORE_TEST_NATIVE_LIBRARY") == "1":
        assert not Path(observed["package_file"]).is_relative_to(ROOT / "src")
