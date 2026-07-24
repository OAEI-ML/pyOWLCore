from __future__ import annotations

import gc
import hashlib
import json
import tempfile
import warnings
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from tests.native.foundation._support import load_extension


def main() -> None:
    extension = load_extension()

    import pyowl_core
    from pyowl_core import (
        AxiomScope,
        BackendPreference,
        BackendProtocolError,
        CancellationSource,
        DocumentFormat,
        EncodedStructuralView,
        ImportPolicy,
        LoadOptions,
        MappingResolver,
        OntologyDelta,
        OntologySyntaxError,
        OperationCancelledError,
        ParseLimits,
        ResolvedDocument,
        ResourceLimitError,
        SnapshotInUseError,
        UnresolvedImportWarning,
        apply_delta,
        compose_views,
        decode_snapshot,
        encode_snapshot,
        load_snapshot,
        open_snapshot,
    )
    from pyowl_core.backends import native
    from pyowl_core.model import canonical_bytes
    from tests.native.encoded_views import _independent as independent_decoder
    from tests.native.encoded_views._independent import decode_root_canonical_bytes
    from tools.benchmark.comparators.common_contract import build_core_common_contract
    from tools.wire_reference import read_wire

    native._reset_probe_cache_for_tests()
    probe = native.probe(refresh=True)
    if not probe.available or "parse-functional-v1" not in probe.features:
        raise RuntimeError(probe.reason or "native Functional parser is unavailable")
    if not hasattr(extension, "_retain_structural_snapshot_v2"):
        raise RuntimeError("native artifact lacks the retained-owner constructor")

    source = (
        b"Ontology(<urn:retained-installed> "
        b"Declaration(Class(<urn:retained-installed:C>)) "
        b"Declaration(Class(<urn:retained-installed:D>)) "
        b"SubClassOf(<urn:retained-installed:C> <urn:retained-installed:D>))"
    )
    right_source = (
        b"Ontology(<urn:retained-installed:right> "
        b"Declaration(Class(<urn:retained-installed:E>)) "
        b"Declaration(Class(<urn:retained-installed:F>)) "
        b"SubClassOf(<urn:retained-installed:E> <urn:retained-installed:F>))"
    )

    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=backend,
            collect_provenance=True,
        )

    def scalar_root_bytes(snapshot: object) -> tuple[tuple[int, bytes], ...]:
        selected_snapshot = cast(Any, snapshot)
        roots = [
            *(
                (1, canonical_bytes(value))
                for value in selected_snapshot.ontology_annotations()
            ),
            *((2, canonical_bytes(value)) for value in selected_snapshot.iter_axioms()),
            *((3, canonical_bytes(value)) for value in selected_snapshot.iter_extensions()),
        ]
        return tuple(sorted(roots))

    reference = load_snapshot(source, options=options(BackendPreference.PYTHON))

    def unexpected_model_decode(*_arguments: object, **_keywords: object) -> object:
        raise AssertionError("installed retained path crossed the complete model decoder")

    real_model_decode = native._decode_parsed_functional
    native._decode_parsed_functional = unexpected_model_decode  # type: ignore[assignment]
    selected = load_snapshot(source, options=options(BackendPreference.NATIVE))
    native._decode_parsed_functional = real_model_decode  # type: ignore[assignment]
    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    if type(raw_owner) is not cast(Any, extension)._NativeSnapshotHandle:
        raise AssertionError("public load did not retain the exact Rust owner")

    reference_origin_rows = sum(len(rows) for rows in reference.origin_index.entries.values())
    retained_origin_rows = sum(len(rows) for rows in selected.origin_index.entries.values())
    origin_parity = selected.origin_index == reference.origin_index
    expected_roots = tuple((2, canonical_bytes(value)) for value in reference.iter_axioms())
    before_native = raw_owner._publication_counters_v2()
    before_python = cast(Any, selected)._native_python_counters()
    ingestion = cast(Any, selected)._native_ingestion_counters_v2()
    if ingestion.eager_structural_objects_materialized != 0:
        raise AssertionError("retained publication eagerly materialized structural objects")
    if before_python.model_rows_materialized != 0:
        raise AssertionError("retained publication eagerly materialized facade model rows")
    summary_method = cast(Any, selected)._native_common_contract_summary_v1
    summary = summary_method()
    summary_zero_work = (
        raw_owner._publication_counters_v2() == before_native
        and cast(Any, selected)._native_python_counters() == before_python
    )
    if not summary_zero_work:
        raise AssertionError("retained contract summary crossed facade or owner work")
    if summary_method() is not summary:
        raise AssertionError("retained contract summary was not returned by identity")
    scalar_contract = build_core_common_contract(
        reference,
        corpus_id="retained-installed-summary",
        source_sha256=hashlib.sha256(source).hexdigest(),
        options_sha256="00" * 32,
    )
    scalar_fingerprints = cast(dict[str, dict[str, object]], scalar_contract["fingerprints"])
    summary_fingerprint_parity = all(
        evidence.preimage_bytes == scalar_fingerprints[name]["preimage_bytes"]
        and evidence.sha256.hex() == scalar_fingerprints[name]["preimage_sha256"]
        for name, evidence in (
            ("document", summary.document_fingerprint),
            ("structural", summary.structural_fingerprint),
            ("logical", summary.logical_fingerprint),
            ("signature", summary.signature_fingerprint),
        )
    )
    scalar_inventories = cast(
        dict[str, dict[str, object]],
        cast(dict[str, object], scalar_contract["ledger"])["inventories"],
    )
    summary_inventory_parity = all(
        inventory.count == scalar_inventories[name]["count"]
        and inventory.canonical_bytes == scalar_inventories[name]["canonical_bytes"]
        and inventory.transcript_bytes == scalar_inventories[name]["transcript_bytes"]
        and inventory.sha256.hex() == scalar_inventories[name]["sha256"]
        for name, inventory in (
            ("ontology_annotations", summary.ontology_annotations),
            ("axioms", summary.axioms),
            ("extensions", summary.extensions),
            ("signature", summary.signature),
        )
    )
    if not summary_fingerprint_parity or not summary_inventory_parity:
        raise AssertionError("retained contract summary differs from the scalar reference")
    direct = selected.view(EncodedStructuralView)
    after_direct_native = raw_owner._publication_counters_v2()
    after_direct_python = cast(Any, selected)._native_python_counters()
    direct_roots = decode_root_canonical_bytes(direct.buffers)
    if direct_roots != expected_roots:
        raise AssertionError("retained direct columns disagree with scalar roots")
    if direct.owner is not selected:
        raise AssertionError("retained direct columns lost public owner identity")
    if len({id(value.obj) for value in direct.buffers.values()}) != 1:
        raise AssertionError("retained direct columns do not share one exporter")
    if not all(type(value.obj) is bytes for value in direct.buffers.values()):
        raise AssertionError("retained direct columns are not backed by immutable bytes")
    if after_direct_native.page_requests != before_native.page_requests:
        raise AssertionError("retained direct columns crossed scalar facade paging")
    if after_direct_native.rows_emitted != before_native.rows_emitted:
        raise AssertionError("retained direct columns emitted scalar facade rows")
    if after_direct_python.model_rows_materialized != before_python.model_rows_materialized:
        raise AssertionError("retained direct columns materialized Python model rows")

    right_reference = load_snapshot(right_source, options=options(BackendPreference.PYTHON))
    right_selected = load_snapshot(right_source, options=options(BackendPreference.NATIVE))
    right_handle = cast(Any, right_selected)._native_snapshot_state.owner.handle
    right_owner = object.__getattribute__(right_handle, "_owner_v2")
    right_before_native = right_owner._publication_counters_v2()
    right_before_python = cast(Any, right_selected)._native_python_counters()
    overlay = apply_delta(selected, OntologyDelta())
    composite = compose_views(selected, right_selected, roles=("left", "right"))
    expected_overlay = scalar_root_bytes(apply_delta(reference, OntologyDelta()))
    expected_composite = scalar_root_bytes(
        compose_views(reference, right_reference, roles=("left", "right"))
    )
    scalar_error = AssertionError("installed encoded matrix crossed native scalar traversal")
    with (
        patch.object(type(selected), "iter_axioms", side_effect=scalar_error),
        patch.object(type(selected), "iter_extensions", side_effect=scalar_error),
        patch.object(type(selected), "ontology_annotations", side_effect=scalar_error),
        patch.object(type(selected), "signature", side_effect=scalar_error),
    ):
        overlay_encoded = overlay.view(EncodedStructuralView)
        composite_encoded = composite.view(EncodedStructuralView)
    overlay_decode = independent_decoder.decode_segmented_root_canonical_bytes(
        overlay_encoded,
        expected_owner=overlay,
        expected_scope=AxiomScope.CLOSURE,
        expected_document_key=None,
    )
    composite_decode = independent_decoder.decode_segmented_root_canonical_bytes(
        composite_encoded,
        expected_owner=composite,
        expected_scope=AxiomScope.CLOSURE,
        expected_document_key=None,
    )
    overlay_roots = tuple((root.root_kind, root.canonical) for root in overlay_decode.roots)
    composite_roots = tuple((root.root_kind, root.canonical) for root in composite_decode.roots)
    if overlay_roots != expected_overlay or composite_roots != expected_composite:
        raise AssertionError("installed segmented columns disagree with scalar roots")
    if overlay_decode.proof.scalar_traversal_calls != 0:
        raise AssertionError("installed overlay decoder crossed scalar traversal")
    if composite_decode.proof.scalar_traversal_calls != 0:
        raise AssertionError("installed composite decoder crossed scalar traversal")
    if overlay_decode.proof.referenced_buffer_copy_bytes != 0:
        raise AssertionError("installed overlay decoder copied referenced buffers")
    if composite_decode.proof.referenced_buffer_copy_bytes != 0:
        raise AssertionError("installed composite decoder copied referenced buffers")
    if not any(cast(Any, value).owner is selected for value in overlay_decode.proof.retained_views):
        raise AssertionError("installed overlay decoder did not retain its native base")
    if not any(
        cast(Any, value).owner is selected for value in composite_decode.proof.retained_views
    ):
        raise AssertionError("installed composite decoder did not retain its left native member")
    if not any(
        cast(Any, value).owner is right_selected for value in composite_decode.proof.retained_views
    ):
        raise AssertionError("installed composite decoder did not retain its right native member")
    after_segmented_native = raw_owner._publication_counters_v2()
    after_segmented_python = cast(Any, selected)._native_python_counters()
    right_after_segmented_native = right_owner._publication_counters_v2()
    right_after_segmented_python = cast(Any, right_selected)._native_python_counters()
    if after_segmented_native.page_requests != after_direct_native.page_requests:
        raise AssertionError("installed segmented publication crossed left facade paging")
    if after_segmented_native.rows_emitted != after_direct_native.rows_emitted:
        raise AssertionError("installed segmented publication emitted left facade rows")
    if right_after_segmented_native.page_requests != right_before_native.page_requests:
        raise AssertionError("installed segmented publication crossed right facade paging")
    if right_after_segmented_native.rows_emitted != right_before_native.rows_emitted:
        raise AssertionError("installed segmented publication emitted right facade rows")
    if after_segmented_python.model_rows_materialized != before_python.model_rows_materialized:
        raise AssertionError("installed segmented publication materialized left model rows")
    if (
        right_after_segmented_python.model_rows_materialized
        != right_before_python.model_rows_materialized
    ):
        raise AssertionError("installed segmented publication materialized right model rows")
    if after_segmented_native.publication_structural_rows_copied != 0:
        raise AssertionError("installed segmented publication copied left structural rows")
    if right_after_segmented_native.publication_structural_rows_copied != 0:
        raise AssertionError("installed segmented publication copied right structural rows")
    overlay_owner_identity = overlay_encoded.owner is overlay
    overlay_scalar_traversal_calls = overlay_decode.proof.scalar_traversal_calls
    overlay_referenced_copy_bytes = overlay_decode.proof.referenced_buffer_copy_bytes
    composite_owner_identity = composite_encoded.owner is composite
    composite_scalar_traversal_calls = composite_decode.proof.scalar_traversal_calls
    composite_referenced_copy_bytes = composite_decode.proof.referenced_buffer_copy_bytes

    hostile_code = None
    try:
        independent_decoder.decode_segmented_root_canonical_bytes(
            replace(direct, descriptor=b"hostile"),
            expected_owner=selected,
            expected_scope=AxiomScope.CLOSURE,
            expected_document_key=None,
        )
    except BackendProtocolError as error:
        hostile_code = error.code
    if hostile_code != "ENCODED_VIEW_DESCRIPTOR":
        raise AssertionError("installed consumer did not reject a hostile descriptor")

    syntax_error_code = None
    try:
        load_snapshot(
            b"Ontology(<urn:retained-installed:malformed> Declaration(Class(<urn:C>))",
            options=options(BackendPreference.NATIVE),
        )
    except OntologySyntaxError as error:
        syntax_error_code = error.code
    if syntax_error_code is None:
        raise AssertionError("forced native malformed input did not fail closed")

    limit_error_code = None
    try:
        load_snapshot(
            source,
            options=replace(
                options(BackendPreference.NATIVE),
                limits=ParseLimits(max_axioms=1),
            ),
        )
    except ResourceLimitError as error:
        limit_error_code = error.code
    if limit_error_code is None:
        raise AssertionError("forced native limit did not fail closed")

    reference_wire = encode_snapshot(reference)
    retained_wire = encode_snapshot(selected)
    reference_image = read_wire(reference_wire)
    retained_image = read_wire(retained_wire)
    wire_differing_sections = sorted(
        kind
        for kind in set(reference_image.sections) | set(retained_image.sections)
        if reference_image.sections.get(kind) != retained_image.sections.get(kind)
    )
    if wire_differing_sections not in ([], [13, 14]):
        raise AssertionError("retained snapshot has an unexpected wire divergence")
    after_wire_native = raw_owner._publication_counters_v2()
    after_wire_python = cast(Any, selected)._native_python_counters()
    if after_wire_python.model_rows_materialized != after_direct_python.model_rows_materialized:
        raise AssertionError("wire handoff materialized Python model rows")
    if after_wire_native.encoded_view_requests - after_direct_native.encoded_view_requests != 1:
        raise AssertionError("wire handoff did not reuse one direct-column publication")
    if after_wire_native.page_requests - after_direct_native.page_requests != 1:
        raise AssertionError("wire handoff did not use exactly one retained origin page")
    if after_wire_native.rows_emitted - after_direct_native.rows_emitted != retained_origin_rows:
        raise AssertionError("wire handoff emitted rows outside the retained origin page")
    if after_wire_native.publication_structural_rows_copied != 0:
        raise AssertionError("wire handoff copied structural publication rows")
    if after_wire_native.publication_structural_bytes_copied != 0:
        raise AssertionError("wire handoff copied structural publication bytes")

    decoded = decode_snapshot(retained_wire)
    decoded_encoded = decoded.view(EncodedStructuralView)
    decoded_roots = decode_root_canonical_bytes(decoded_encoded.buffers)
    decoded_parity = (
        decoded.structural_fingerprint == reference.structural_fingerprint
        and decoded.logical_fingerprint == reference.logical_fingerprint
        and decoded.signature_fingerprint == reference.signature_fingerprint
        and encode_snapshot(decoded) == retained_wire
    )
    if not decoded_parity:
        raise AssertionError("decoded retained wire differs from Python reference")

    without_provenance_reference = load_snapshot(
        source,
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
            collect_provenance=False,
        ),
    )
    without_provenance = load_snapshot(
        source,
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.NATIVE,
            collect_provenance=False,
        ),
    )
    without_provenance_handle = cast(Any, without_provenance)._native_snapshot_state.owner.handle
    without_provenance_owner = object.__getattribute__(without_provenance_handle, "_owner_v2")
    without_provenance_counters = without_provenance_owner._publication_counters_v2()
    without_provenance_parity = (
        without_provenance.capabilities.backend == "native"
        and without_provenance.origin_index == without_provenance_reference.origin_index
        and without_provenance.root.origin_index is None
        and without_provenance_handle.attestation.capability_bits == 7
        and without_provenance_counters.parser_bytes == len(source)
        and without_provenance_counters.retained_origin_rows == 0
        and encode_snapshot(without_provenance) == encode_snapshot(without_provenance_reference)
    )
    if not without_provenance_parity:
        raise AssertionError("provenance-disabled retained load differs from Python reference")

    anonymous_source = (
        b"Prefix(ex:=<urn:retained-installed-anonymous:>) "
        b"Ontology(<urn:retained-installed-anonymous> "
        b"ClassAssertion(ex:C _:left) ClassAssertion(ex:C _:left) "
        b"ObjectPropertyAssertion(ex:p _:left _:right))"
    )

    def anonymous_options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=backend,
            collect_provenance=True,
            preserve_source_map=True,
        )

    anonymous_reference = load_snapshot(
        anonymous_source,
        options=anonymous_options(BackendPreference.PYTHON),
    )
    anonymous_selected = load_snapshot(
        anonymous_source,
        options=anonymous_options(BackendPreference.NATIVE),
    )
    anonymous_ingestion = cast(Any, anonymous_selected)._native_ingestion_counters_v2()
    anonymous_raw_rows = tuple(
        canonical_bytes(value) for value in anonymous_selected.root.axioms
    )
    anonymous_effective_rows = tuple(
        canonical_bytes(value) for value in anonymous_selected.iter_axioms()
    )
    anonymous_raw_origin_parity = (
        set(cast(Any, anonymous_selected.root.origin_index).entries)
        == set(cast(Any, anonymous_reference.root.origin_index).entries)
        and {
            digest: tuple((item.occurrence, item.span) for item in occurrences)
            for digest, occurrences in cast(
                Any, anonymous_selected.root.origin_index
            ).entries.items()
        }
        == {
            digest: tuple((item.occurrence, item.span) for item in occurrences)
            for digest, occurrences in cast(
                Any, anonymous_reference.root.origin_index
            ).entries.items()
        }
    )
    anonymous_parity = (
        type(anonymous_selected).__name__ == "_NativeOntologySnapshot"
        and anonymous_selected.capabilities.backend == "native"
        and anonymous_selected.structural_fingerprint
        == anonymous_reference.structural_fingerprint
        and anonymous_selected.logical_fingerprint == anonymous_reference.logical_fingerprint
        and anonymous_selected.signature_fingerprint
        == anonymous_reference.signature_fingerprint
        and anonymous_raw_rows
        == tuple(canonical_bytes(value) for value in anonymous_reference.root.axioms)
        and anonymous_effective_rows
        == tuple(canonical_bytes(value) for value in anonymous_reference.iter_axioms())
        and anonymous_raw_rows != anonymous_effective_rows
        and anonymous_raw_origin_parity
        and anonymous_selected.origin_index == anonymous_reference.origin_index
        and anonymous_selected.root.source_map == anonymous_reference.root.source_map
        and encode_snapshot(anonymous_selected) == encode_snapshot(anonymous_reference)
        and anonymous_ingestion.parser_result_bytes_scanned == 0
        and anonymous_ingestion.eager_structural_objects_materialized == 0
        and anonymous_ingestion.structural_occurrence_rows_scanned == 3
        and anonymous_ingestion.structural_root_rows_published == 2
    )
    if not anonymous_parity:
        raise AssertionError("anonymous retained load differs from Python reference")
    anonymous_selected.close()

    empty_closure_parity: dict[str, bool] = {}
    empty_closure_snapshot_types: dict[str, str] = {}
    empty_closure_parser_result_bytes: dict[str, int] = {}
    for policy in (ImportPolicy.RESOLVE_LOCAL, ImportPolicy.RESOLVE_STRICT):
        empty_reference = load_snapshot(
            source,
            options=LoadOptions(
                format=DocumentFormat.FUNCTIONAL,
                imports=policy,
                backend=BackendPreference.PYTHON,
                collect_provenance=True,
            ),
        )
        empty_selected = load_snapshot(
            source,
            options=LoadOptions(
                format=DocumentFormat.FUNCTIONAL,
                imports=policy,
                backend=BackendPreference.NATIVE,
                collect_provenance=True,
            ),
        )
        empty_ingestion = cast(Any, empty_selected)._native_ingestion_counters_v2()
        parity = (
            type(empty_selected).__name__ == "_NativeOntologySnapshot"
            and empty_selected.import_manifest == empty_reference.import_manifest
            and empty_selected.import_manifest.policy is policy
            and empty_selected.import_manifest.edges == ()
            and empty_selected.report.resolution_attempts == 0
            and empty_selected.structural_fingerprint
            == empty_reference.structural_fingerprint
            and empty_selected.logical_fingerprint == empty_reference.logical_fingerprint
            and empty_selected.signature_fingerprint == empty_reference.signature_fingerprint
            and empty_selected.origin_index == empty_reference.origin_index
            and encode_snapshot(empty_selected) == encode_snapshot(empty_reference)
            and empty_ingestion.parser_result_bytes_scanned == 0
            and empty_ingestion.canonical_bytes_copied_to_python == 0
        )
        if not parity:
            raise AssertionError(f"{policy.value} empty retained closure differs from Python")
        empty_closure_parity[policy.value] = parity
        empty_closure_snapshot_types[policy.value] = type(empty_selected).__name__
        empty_closure_parser_result_bytes[policy.value] = (
            empty_ingestion.parser_result_bytes_scanned
        )
        empty_selected.close()

    closure_root = (
        b"Ontology(<urn:retained-installed-closure:root> "
        b"Import(<urn:retained-installed-closure:left>) "
        b"Import(<urn:retained-installed-closure:right>) "
        b"Declaration(Class(<urn:retained-installed-closure:Root>)))"
    )
    closure_sources = {
        "urn:retained-installed-closure:left": (
            b"Ontology(<urn:retained-installed-closure:left> "
            b"Import(<urn:retained-installed-closure:leaf>) "
            b"ClassAssertion(<urn:retained-installed-closure:C> _:person))"
        ),
        "urn:retained-installed-closure:right": (
            b"Ontology(<urn:retained-installed-closure:right> "
            b"Import(<urn:retained-installed-closure:leaf>) "
            b"ClassAssertion(<urn:retained-installed-closure:C> _:person))"
        ),
        "urn:retained-installed-closure:leaf": (
            b"Ontology(<urn:retained-installed-closure:leaf> "
            b"Declaration(Class(<urn:retained-installed-closure:Leaf>)))"
        ),
    }
    closure_matrix_cases = 0
    closure_matrix_parity = True
    closure_matrix_types: set[str] = set()
    closure_matrix_document_counts: set[int] = set()

    def closure_options(
        backend: BackendPreference,
        workers: int,
        *,
        policy: ImportPolicy,
        collect_provenance: bool,
        preserve_source_map: bool,
    ) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=policy,
            backend=backend,
            collect_provenance=collect_provenance,
            preserve_source_map=preserve_source_map,
            limits=replace(
                ParseLimits(),
                max_concurrent_fetches=workers,
            ),
        )

    for policy in (ImportPolicy.RESOLVE_LOCAL, ImportPolicy.RESOLVE_STRICT):
        for collect_provenance in (False, True):
            for preserve_source_map in (False, True):
                for document_iri in (
                    None,
                    pyowl_core.IRI("urn:retained-installed-closure:document"),
                ):
                    reference_closure = load_snapshot(
                        closure_root,
                        document_iri=document_iri,
                        options=closure_options(
                            BackendPreference.PYTHON,
                            3,
                            policy=policy,
                            collect_provenance=collect_provenance,
                            preserve_source_map=preserve_source_map,
                        ),
                        resolver=MappingResolver(closure_sources),
                    )
                    sequential_closure = load_snapshot(
                        closure_root,
                        document_iri=document_iri,
                        options=closure_options(
                            BackendPreference.NATIVE,
                            1,
                            policy=policy,
                            collect_provenance=collect_provenance,
                            preserve_source_map=preserve_source_map,
                        ),
                        resolver=MappingResolver(closure_sources),
                    )
                    parallel_closure = load_snapshot(
                        closure_root,
                        document_iri=document_iri,
                        options=closure_options(
                            BackendPreference.NATIVE,
                            3,
                            policy=policy,
                            collect_provenance=collect_provenance,
                            preserve_source_map=preserve_source_map,
                        ),
                        resolver=MappingResolver(closure_sources),
                    )
                    closure_handle = cast(
                        Any,
                        parallel_closure,
                    )._native_snapshot_state.owner.handle
                    closure_owner = object.__getattribute__(closure_handle, "_owner_v2")
                    closure_counters = closure_owner._publication_counters_v2()
                    closure_reference_wire = encode_snapshot(reference_closure)
                    closure_sequential_wire = encode_snapshot(sequential_closure)
                    closure_parallel_wire = encode_snapshot(parallel_closure)
                    case_parity = (
                        type(closure_owner) is cast(Any, extension)._NativeSnapshotHandle
                        and type(sequential_closure).__name__ == "_NativeOntologySnapshot"
                        and type(parallel_closure).__name__ == "_NativeOntologySnapshot"
                        and len(reference_closure.documents)
                        == len(sequential_closure.documents)
                        == len(parallel_closure.documents)
                        == 4
                        and reference_closure.import_manifest
                        == sequential_closure.import_manifest
                        == parallel_closure.import_manifest
                        and reference_closure.report.diagnostics
                        == sequential_closure.report.diagnostics
                        == parallel_closure.report.diagnostics
                        and reference_closure.structural_fingerprint
                        == sequential_closure.structural_fingerprint
                        == parallel_closure.structural_fingerprint
                        and reference_closure.logical_fingerprint
                        == sequential_closure.logical_fingerprint
                        == parallel_closure.logical_fingerprint
                        and reference_closure.signature_fingerprint
                        == sequential_closure.signature_fingerprint
                        == parallel_closure.signature_fingerprint
                        and reference_closure.origin_index
                        == sequential_closure.origin_index
                        == parallel_closure.origin_index
                        and tuple(
                            document.source_map
                            for document in reference_closure.documents
                        )
                        == tuple(
                            document.source_map
                            for document in sequential_closure.documents
                        )
                        == tuple(
                            document.source_map
                            for document in parallel_closure.documents
                        )
                        and closure_reference_wire
                        == closure_sequential_wire
                        == closure_parallel_wire
                        and closure_counters.retained_document_tables == 4
                        and closure_counters.publication_structural_rows_copied == 0
                        and closure_counters.publication_structural_bytes_copied == 0
                    )
                    closure_matrix_parity = closure_matrix_parity and case_parity
                    closure_matrix_cases += 1
                    closure_matrix_types.add(type(parallel_closure).__name__)
                    closure_matrix_document_counts.add(len(parallel_closure.documents))
                    sequential_closure.close()
                    parallel_closure.close()
    if not closure_matrix_parity:
        raise AssertionError("installed retained Functional closure matrix diverged")

    shared_root = (
        b"Ontology(<urn:retained-installed-shared:root> "
        b"Import(<urn:retained-installed-shared:left>) "
        b"Import(<urn:retained-installed-shared:right>) "
        b"Declaration(Class(<urn:retained-installed-shared:Root>)))"
    )
    shared_left = (
        b"Ontology(<urn:retained-installed-shared:left> "
        b"Import(<urn:retained-installed-shared:leaf-a>) "
        b"Declaration(Class(<urn:retained-installed-shared:Left>)))"
    )
    shared_right = (
        b"Ontology(<urn:retained-installed-shared:right> "
        b"Import(<urn:retained-installed-shared:leaf-b>) "
        b"Declaration(Class(<urn:retained-installed-shared:Right>)))"
    )
    shared_leaf = (
        b"Ontology(<urn:retained-installed-shared:leaf> "
        b"Declaration(Class(<urn:retained-installed-shared:Leaf>)))"
    )
    shared_leaf_digest = hashlib.sha256(shared_leaf).digest()
    shared_sources = {
        "urn:retained-installed-shared:left": shared_left,
        "urn:retained-installed-shared:right": shared_right,
        "urn:retained-installed-shared:leaf-a": ResolvedDocument(
            shared_leaf,
            pyowl_core.IRI("urn:retained-installed-shared:leaf"),
            format=DocumentFormat.FUNCTIONAL,
            expected_sha256=shared_leaf_digest,
            provenance={"locator": "installed:leaf-a"},
        ),
        "urn:retained-installed-shared:leaf-b": ResolvedDocument(
            shared_leaf,
            pyowl_core.IRI("urn:retained-installed-shared:leaf"),
            format=DocumentFormat.FUNCTIONAL,
            provenance={"locator": "installed:leaf-b"},
        ),
    }
    shared_options = LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.RESOLVE_STRICT,
        backend=BackendPreference.NATIVE,
        collect_provenance=True,
        preserve_source_map=True,
        limits=replace(ParseLimits(), max_concurrent_fetches=3),
    )
    shared_reference = load_snapshot(
        shared_root,
        options=replace(shared_options, backend=BackendPreference.PYTHON),
        resolver=MappingResolver(shared_sources),
    )
    shared_parse_counts = {
        "root": 0,
        "left": 0,
        "right": 0,
        "leaf": 0,
    }
    shared_source_by_name = {
        "root": shared_root,
        "left": shared_left,
        "right": shared_right,
        "leaf": shared_leaf,
    }
    real_shared_parse = cast(Any, extension)._parse_functional_retained_v2

    def capture_shared_parse(
        request: object,
        *arguments: object,
        **keywords: object,
    ) -> object:
        if not isinstance(request, bytes):
            raise AssertionError("installed retained parser request is not bytes")
        matches = tuple(
            name
            for name, expected_source in shared_source_by_name.items()
            if request.endswith(expected_source)
        )
        if len(matches) != 1:
            raise AssertionError("installed retained parser source was not identified exactly")
        shared_parse_counts[matches[0]] += 1
        return real_shared_parse(request, *arguments, **keywords)

    cast(Any, extension)._parse_functional_retained_v2 = capture_shared_parse
    try:
        shared_selected = load_snapshot(
            shared_root,
            options=shared_options,
            resolver=MappingResolver(shared_sources),
        )
    finally:
        cast(Any, extension)._parse_functional_retained_v2 = real_shared_parse
    shared_digest_parity = (
        type(shared_selected).__name__ == "_NativeOntologySnapshot"
        and len(shared_selected.documents) == len(shared_reference.documents) == 4
        and shared_selected.import_manifest == shared_reference.import_manifest
        and shared_selected.structural_fingerprint
        == shared_reference.structural_fingerprint
        and shared_selected.logical_fingerprint == shared_reference.logical_fingerprint
        and shared_selected.signature_fingerprint
        == shared_reference.signature_fingerprint
        and shared_selected.origin_index == shared_reference.origin_index
        and encode_snapshot(shared_selected) == encode_snapshot(shared_reference)
        and shared_parse_counts == {"root": 1, "left": 1, "right": 1, "leaf": 1}
    )
    if not shared_digest_parity:
        raise AssertionError(
            "installed shared digest did not parse exactly once: "
            f"{shared_parse_counts!r}",
        )
    shared_selected.close()

    failure_root = (
        b"Ontology(<urn:retained-installed-failure:root> "
        b"Import(<urn:retained-installed-failure:child>) "
        b"Declaration(Class(<urn:retained-installed-failure:Root>)))"
    )
    malformed_child = (
        b"Ontology(<urn:retained-installed-failure:child> "
        b"Declaration(Class(<urn:retained-installed-failure:Child>))"
    )
    cancellation_child = (
        b"Ontology(<urn:retained-installed-failure:child> "
        b"Declaration(Class(<urn:retained-installed-failure:Child>)))"
    )

    def failure_options() -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.RESOLVE_LOCAL,
            backend=BackendPreference.NATIVE,
            collect_provenance=False,
            limits=replace(ParseLimits(), max_concurrent_fetches=1),
        )

    merge_calls_after_failure = 0
    real_merge = cast(Any, extension)._merge_parsed_structural_snapshot_v2

    def unexpected_failure_merge(*_arguments: object, **_keywords: object) -> object:
        nonlocal merge_calls_after_failure
        merge_calls_after_failure += 1
        raise AssertionError("failed installed closure reached final owner publication")

    cast(Any, extension)._merge_parsed_structural_snapshot_v2 = unexpected_failure_merge
    malformed_import_code = None
    try:
        load_snapshot(
            failure_root,
            options=failure_options(),
            resolver=MappingResolver(
                {"urn:retained-installed-failure:child": malformed_child},
            ),
        )
    except OntologySyntaxError as error:
        malformed_import_code = error.code
    if malformed_import_code is None:
        raise AssertionError("malformed installed import did not fail closed")

    cancellation = CancellationSource()
    cancellation_reason = "installed Functional import parse cancelled"
    real_retained_parse = cast(Any, extension)._parse_functional_retained_v2

    def cancel_child_parse(
        request: object,
        *arguments: object,
        **keywords: object,
    ) -> object:
        if isinstance(request, bytes) and request.endswith(cancellation_child):
            cancellation.cancel(cancellation_reason)
        return real_retained_parse(request, *arguments, **keywords)

    cast(Any, extension)._parse_functional_retained_v2 = cancel_child_parse
    cancellation_error_code = None
    try:
        load_snapshot(
            failure_root,
            options=failure_options(),
            resolver=MappingResolver(
                {"urn:retained-installed-failure:child": cancellation_child},
            ),
            cancellation_token=cancellation.token,
        )
    except OperationCancelledError as error:
        cancellation_error_code = error.code
        if cancellation_reason not in str(error):
            raise AssertionError("installed cancellation lost its reason") from error
    finally:
        cast(Any, extension)._parse_functional_retained_v2 = real_retained_parse
        cast(Any, extension)._merge_parsed_structural_snapshot_v2 = real_merge
    if cancellation_error_code is None:
        raise AssertionError("installed import cancellation did not fail closed")
    if merge_calls_after_failure != 0:
        raise AssertionError("failed installed closure published a native owner")

    auto_source = (
        b"Ontology(<urn:retained-auto-installed> "
        b"Import(<urn:retained-auto-installed:ignored>) "
        + (b" " * (256 * 1024))
        + b"Declaration(Class(<urn:retained-auto-installed:C>)))"
    )
    auto_reference = load_snapshot(auto_source, options=options(BackendPreference.PYTHON))
    auto_selected = load_snapshot(auto_source, options=options(BackendPreference.AUTO))
    auto_handle = cast(Any, auto_selected)._native_snapshot_state.owner.handle
    auto_owner = object.__getattribute__(auto_handle, "_owner_v2")
    auto_before_native = auto_owner._publication_counters_v2()
    auto_before_python = cast(Any, auto_selected)._native_python_counters()
    auto_ingestion = cast(Any, auto_selected)._native_ingestion_counters_v2()
    auto_direct = auto_selected.view(EncodedStructuralView)
    auto_after_direct_native = auto_owner._publication_counters_v2()
    auto_after_direct_python = cast(Any, auto_selected)._native_python_counters()
    auto_expected_roots = tuple(
        (2, canonical_bytes(value)) for value in auto_reference.iter_axioms()
    )
    auto_wire = encode_snapshot(auto_selected)
    auto_reference_wire = encode_snapshot(auto_reference)
    auto_after_wire_python = cast(Any, auto_selected)._native_python_counters()
    auto_parity = (
        type(auto_owner) is cast(Any, extension)._NativeSnapshotHandle
        and auto_selected.capabilities.backend == "native"
        and auto_selected.structural_fingerprint == auto_reference.structural_fingerprint
        and auto_selected.logical_fingerprint == auto_reference.logical_fingerprint
        and auto_selected.signature_fingerprint == auto_reference.signature_fingerprint
        and auto_selected.origin_index == auto_reference.origin_index
        and auto_selected.import_manifest == auto_reference.import_manifest
        and len(auto_selected.import_manifest.edges) == 1
        and auto_selected.import_manifest.edges[0].status.value == "ignored"
        and auto_before_native.parser_bytes == len(auto_source)
        and auto_direct.owner is auto_selected
        and decode_root_canonical_bytes(auto_direct.buffers) == auto_expected_roots
        and len({id(value.obj) for value in auto_direct.buffers.values()}) == 1
        and auto_after_direct_native.page_requests == auto_before_native.page_requests
        and auto_after_direct_native.rows_emitted == auto_before_native.rows_emitted
        and auto_after_direct_python.model_rows_materialized
        == auto_before_python.model_rows_materialized
        and auto_after_wire_python.model_rows_materialized
        == auto_before_python.model_rows_materialized
        and auto_wire == auto_reference_wire
    )
    if not auto_parity:
        raise AssertionError("AUTO-selected retained load differs from Python reference")
    auto_selected.close()
    auto_direct_survives_owner_close = (
        decode_root_canonical_bytes(auto_direct.buffers) == auto_expected_roots
    )

    unresolved_source = (
        b"Ontology(<urn:retained-unresolved-installed> "
        b"Import(<urn:retained-unresolved-installed:missing>) "
        b"Declaration(Class(<urn:retained-unresolved-installed:C>)))"
    )

    def unresolved_options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.RECORD_UNRESOLVED,
            backend=backend,
            collect_provenance=True,
        )

    with warnings.catch_warnings(record=True) as reference_warnings:
        warnings.simplefilter("always", UnresolvedImportWarning)
        unresolved_reference = load_snapshot(
            unresolved_source,
            options=unresolved_options(BackendPreference.PYTHON),
        )
    with warnings.catch_warnings(record=True) as selected_warnings:
        warnings.simplefilter("always", UnresolvedImportWarning)
        unresolved_selected = load_snapshot(
            unresolved_source,
            options=unresolved_options(BackendPreference.NATIVE),
        )
    unresolved_ingestion = cast(Any, unresolved_selected)._native_ingestion_counters_v2()
    unresolved_parity = (
        type(unresolved_selected).__name__ == "_NativeOntologySnapshot"
        and unresolved_selected.import_manifest == unresolved_reference.import_manifest
        and unresolved_selected.report.diagnostics == unresolved_reference.report.diagnostics
        and unresolved_selected.report.resolution_attempts
        == unresolved_reference.report.resolution_attempts
        == 1
        and len(reference_warnings) == len(selected_warnings) == 1
        and encode_snapshot(unresolved_selected) == encode_snapshot(unresolved_reference)
        and unresolved_ingestion.parser_result_bytes_scanned == 0
        and unresolved_ingestion.canonical_bytes_copied_to_python == 0
        and unresolved_ingestion.fingerprint_preimage_bytes_materialized_in_python == 0
    )
    if not unresolved_parity:
        raise AssertionError("RECORD_UNRESOLVED retained load differs from Python reference")
    unresolved_selected.close()

    with tempfile.TemporaryDirectory(prefix="pyowl-core-retained-wire-") as temporary:
        path = Path(temporary) / "retained.pyocore"
        path.write_bytes(retained_wire)
        mapped = open_snapshot(path, mmap=True, verify=True)
        if not isinstance(mapped, pyowl_core.MappedOntologySnapshot):
            raise AssertionError("mmap=True did not publish a mapped snapshot")
        if mapped._mapped_state.decoded is not None:
            raise AssertionError("mapped snapshot materialized during publication")
        mapped_fingerprint_parity = (
            mapped.structural_fingerprint == reference.structural_fingerprint
            and mapped.logical_fingerprint == reference.logical_fingerprint
            and mapped.signature_fingerprint == reference.signature_fingerprint
        )
        mapped_encoded = mapped.view(EncodedStructuralView)
        mapped_roots = decode_root_canonical_bytes(mapped_encoded.buffers)
        exporters = tuple(value.obj for value in mapped_encoded.buffers.values())
        mapped_one_exporter = bool(exporters) and all(value is exporters[0] for value in exporters)
        mapped_readonly = all(value.readonly for value in mapped_encoded.buffers.values())
        mapped_lazy = mapped._mapped_state.decoded is None
        mapped_owner_identity = mapped_encoded.owner is mapped
        mapped_close_blocked = False
        try:
            mapped.close()
        except SnapshotInUseError:
            mapped_close_blocked = True
        if not mapped_close_blocked:
            raise AssertionError("live mmap columns did not block owner close")
        del mapped_encoded
        gc.collect()
        mapped.close()
        mapped_closed = mapped.closed

    del overlay_decode, composite_decode, overlay_encoded, composite_encoded, overlay, composite
    gc.collect()
    selected.close()
    direct_survives_owner_close = decode_root_canonical_bytes(direct.buffers) == expected_roots
    right_selected.close()

    print(
        json.dumps(
            {
                "auto_backend": auto_selected.capabilities.backend,
                "auto_closed": auto_selected.closed,
                "auto_direct_survives_owner_close": auto_direct_survives_owner_close,
                "auto_ignored_manifest_parity": (
                    auto_selected.import_manifest == auto_reference.import_manifest
                ),
                "auto_ingestion_eager_structural_objects": (
                    auto_ingestion.eager_structural_objects_materialized
                ),
                "auto_parser_bytes": auto_before_native.parser_bytes,
                "auto_retained_parity": auto_parity,
                "auto_source_bytes": len(auto_source),
                "anonymous_retained_parity": anonymous_parity,
                "anonymous_snapshot_type": type(anonymous_selected).__name__,
                "anonymous_parser_result_bytes": (
                    anonymous_ingestion.parser_result_bytes_scanned
                ),
                "backend": selected.capabilities.backend,
                "decoded_parity": decoded_parity,
                "decoded_root_parity": decoded_roots == expected_roots,
                "direct_encoded_view_requests": (
                    after_direct_native.encoded_view_requests - before_native.encoded_view_requests
                ),
                "direct_owner_identity": direct.owner is selected,
                "direct_root_parity": direct_roots == expected_roots,
                "direct_survives_owner_close": direct_survives_owner_close,
                "hostile_descriptor_code": hostile_code,
                "encoded_view_schemas": dict(selected.capabilities.encoded_view_schemas),
                "empty_closure_parity": empty_closure_parity,
                "empty_closure_parser_result_bytes": empty_closure_parser_result_bytes,
                "empty_closure_snapshot_types": empty_closure_snapshot_types,
                "closure_matrix_cases": closure_matrix_cases,
                "closure_matrix_document_counts": sorted(closure_matrix_document_counts),
                "closure_matrix_parity": closure_matrix_parity,
                "closure_matrix_snapshot_types": sorted(closure_matrix_types),
                "closure_cancellation_error_code": cancellation_error_code,
                "closure_failure_merge_calls": merge_calls_after_failure,
                "closure_malformed_import_code": malformed_import_code,
                "shared_digest_parse_counts": shared_parse_counts,
                "shared_digest_parity": shared_digest_parity,
                "fingerprint_parity": (
                    selected.structural_fingerprint == reference.structural_fingerprint
                    and selected.logical_fingerprint == reference.logical_fingerprint
                    and selected.signature_fingerprint == reference.signature_fingerprint
                ),
                "ingestion_features": list(extension.INGESTION_FEATURES),
                "ingestion_canonical_rows_scanned": ingestion.canonical_rows_scanned,
                "ingestion_canonical_bytes_copied_to_python": (
                    ingestion.canonical_bytes_copied_to_python
                ),
                "ingestion_eager_structural_objects": (
                    ingestion.eager_structural_objects_materialized
                ),
                "ingestion_fingerprint_preimage_bytes_in_python": (
                    ingestion.fingerprint_preimage_bytes_materialized_in_python
                ),
                "ingestion_native_origin_bytes_retained": (
                    ingestion.native_origin_bytes_retained
                ),
                "ingestion_native_origin_rows_retained": (
                    ingestion.native_origin_rows_retained
                ),
                "ingestion_native_publication_canonical_bytes": (
                    ingestion.native_publication_canonical_bytes_encoded
                ),
                "ingestion_native_publication_canonical_rows": (
                    ingestion.native_publication_canonical_rows_encoded
                ),
                "ingestion_parser_result_bytes": ingestion.parser_result_bytes_scanned,
                "ingestion_parser_summary_bytes": ingestion.parser_summary_bytes_materialized,
                "ingestion_provenance_occurrence_records": (
                    ingestion.provenance_occurrence_records_materialized
                ),
                "ingestion_structural_occurrence_rows_scanned": (
                    ingestion.structural_occurrence_rows_scanned
                ),
                "ingestion_structural_rows_published": (ingestion.structural_root_rows_published),
                "mapped_closed": mapped_closed,
                "mapped_close_blocked": mapped_close_blocked,
                "mapped_fingerprint_parity": mapped_fingerprint_parity,
                "mapped_lazy": mapped_lazy,
                "mapped_one_exporter": mapped_one_exporter,
                "mapped_owner_identity": mapped_owner_identity,
                "mapped_readonly": mapped_readonly,
                "mapped_root_parity": mapped_roots == expected_roots,
                "mapped_exporter_type": type(exporters[0]).__name__ if exporters else None,
                "origin_parity": origin_parity,
                "overlay_owner_identity": overlay_owner_identity,
                "overlay_referenced_copy_bytes": overlay_referenced_copy_bytes,
                "overlay_root_parity": overlay_roots == expected_overlay,
                "overlay_scalar_traversal_calls": overlay_scalar_traversal_calls,
                "package_file": pyowl_core.__file__,
                "parser_bytes": before_native.parser_bytes,
                "phase_timings": dict(selected.report.timings),
                "provenance_disabled_parity": without_provenance_parity,
                "provenance_disabled_retained_origin_rows": (
                    without_provenance_counters.retained_origin_rows
                ),
                "reference_origin_rows": reference_origin_rows,
                "retained_origin_rows": retained_origin_rows,
                "selected_closed": selected.closed,
                "right_selected_closed": right_selected.closed,
                "snapshot_type": type(selected).__name__,
                "composite_owner_identity": composite_owner_identity,
                "composite_referenced_copy_bytes": composite_referenced_copy_bytes,
                "composite_root_parity": composite_roots == expected_composite,
                "composite_scalar_traversal_calls": composite_scalar_traversal_calls,
                "segmented_left_model_rows": (
                    after_segmented_python.model_rows_materialized
                    - after_direct_python.model_rows_materialized
                ),
                "segmented_left_page_requests": (
                    after_segmented_native.page_requests - after_direct_native.page_requests
                ),
                "segmented_left_rows_emitted": (
                    after_segmented_native.rows_emitted - after_direct_native.rows_emitted
                ),
                "segmented_right_model_rows": (
                    right_after_segmented_python.model_rows_materialized
                    - right_before_python.model_rows_materialized
                ),
                "segmented_right_page_requests": (
                    right_after_segmented_native.page_requests - right_before_native.page_requests
                ),
                "segmented_right_rows_emitted": (
                    right_after_segmented_native.rows_emitted - right_before_native.rows_emitted
                ),
                "syntax_error_code": syntax_error_code,
                "limit_error_code": limit_error_code,
                "summary_fingerprint_parity": summary_fingerprint_parity,
                "summary_inventory_parity": summary_inventory_parity,
                "summary_node_count_parity": (
                    summary.node_count == len(direct.buffers["node_tags"]) // 2
                ),
                "summary_root_count_parity": (
                    summary.root_count == len(direct.buffers["root_ids"]) // 4
                ),
                "summary_zero_work": summary_zero_work,
                "unresolved_retained_parity": unresolved_parity,
                "unresolved_snapshot_type": type(unresolved_selected).__name__,
                "view_features": list(extension.VIEW_FEATURES),
                "wire_model_rows_materialized": (
                    after_wire_python.model_rows_materialized
                    - after_direct_python.model_rows_materialized
                ),
                "wire_encoded_view_requests": (
                    after_wire_native.encoded_view_requests
                    - after_direct_native.encoded_view_requests
                ),
                "wire_page_requests": (
                    after_wire_native.page_requests - after_direct_native.page_requests
                ),
                "wire_differing_sections": wire_differing_sections,
                "wire_python_parity": retained_wire == reference_wire,
                "wire_rows_emitted": (
                    after_wire_native.rows_emitted - after_direct_native.rows_emitted
                ),
                "wire_python_sha256": hashlib.sha256(reference_wire).hexdigest(),
                "wire_sha256": hashlib.sha256(retained_wire).hexdigest(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
