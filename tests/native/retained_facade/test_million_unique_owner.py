from __future__ import annotations

import gc
import hashlib
import json
import math
import os
from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast

import pytest

from pyowl_core.backends.native_handoff import (
    NativeDiagnosticPublicationV1,
    NativeDocumentPublicationV1,
    NativeImportManifestPublicationV1,
    NativeLoadReportPublicationV1,
)
from pyowl_core.backends.native_handoff_v2 import (
    NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2,
    NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2,
    NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2,
    NativeClosureFacadeCardinalitiesV2,
    NativeDocumentFacadeCardinalitiesV2,
    NativeFacadeCardinalitySummaryV2,
    NativeSnapshotContentDigestsV2,
    NativeSnapshotPublicationV2,
    _seal_native_snapshot_owner_v2,
    freeze_native_snapshot_publication_v2,
    native_snapshot_publication_attestation_v2,
)
from pyowl_core.config import LoadOptions
from pyowl_core.document.native_storage import ontology_snapshot_from_native_publication_v2
from pyowl_core.model import IRI, Class, Declaration, canonical_bytes
from tests.native.foundation._support import load_extension

from ..publication_handoff._support import publication_fields
from ..publication_handoff._support_v2 import diagnostic_reference_sidecars

_ROW_COUNT = 1_000_000
_IRI_PREFIX = "urn:wp15:axiom:"
_RUN_MILLION = os.environ.get("PYOWL_CORE_RUN_WP15_MILLION") == "1"


def _digest(name: str) -> bytes:
    return hashlib.sha256(f"wp15-million-unique:{name}".encode()).digest()


def _publication(
    create: Callable[..., object],
    row_count: int,
) -> tuple[NativeSnapshotPublicationV2, object, int]:
    values = publication_fields()
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    report = cast(NativeLoadReportPublicationV1, values["report"])
    options = cast(LoadOptions, values["load_options"])
    values["documents"] = (
        replace(documents[0], axiom_count=row_count, origin_entry_count=0),
    )
    values["report"] = replace(report, effective_axiom_count=row_count)
    values["load_options"] = replace(options, collect_provenance=False)
    values["capability_bits"] = 7
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    report = cast(NativeLoadReportPublicationV1, values["report"])
    options = cast(LoadOptions, values["load_options"])
    document_key = documents[0].document_key
    summary = NativeFacadeCardinalitySummaryV2(
        documents=(
            NativeDocumentFacadeCardinalitiesV2(
                document_key=document_key,
                effective_annotation_count=0,
                effective_axiom_count=row_count,
                effective_extension_count=0,
                effective_origin_count=0,
                raw_source_prefix_count=0,
                rdf_unconsumed_triple_count=0,
                rdf_rule_count=0,
                rdf_diagnostic_count=0,
            ),
        ),
        closure=NativeClosureFacadeCardinalitiesV2(
            effective_annotation_count=0,
            effective_axiom_count=row_count,
            effective_extension_count=0,
            effective_origin_count=0,
        ),
    )
    sidecars = diagnostic_reference_sidecars(values)
    content = NativeSnapshotContentDigestsV2(
        root_table_sha256=_digest("roots"),
        effective_root_table_sha256=_digest("effective-roots"),
        fingerprint_inputs_sha256=_digest("fingerprints"),
        source_manifest_sha256=_digest("sources"),
        provenance_manifest_sha256=_digest("provenance"),
        effective_origin_manifest_sha256=_digest("effective-origins"),
    )
    maximum_row_bytes = len(
        canonical_bytes(Declaration(Class(IRI(f"{_IRI_PREFIX}{row_count - 1:020}"))))
    )
    attestation = native_snapshot_publication_attestation_v2(
        documents=documents,
        import_manifest=cast(NativeImportManifestPublicationV1, values["import_manifest"]),
        root_document_key=cast(str, values["root_document_key"]),
        load_options=options,
        diagnostics=cast(
            tuple[NativeDiagnosticPublicationV1, ...],
            values["diagnostics"],
        ),
        diagnostic_reference_sidecars=sidecars,
        facade_cardinality_summary=summary,
        report=report,
        capability_bits=7,
        content_digests=content,
        max_facade_row_bytes=maximum_row_bytes,
        owl2_dl_report_summary=None,
    )
    raw_owner = create(
        attestation,
        row_count,
        max_retained_bytes=1024**3,
    )
    handle = _seal_native_snapshot_owner_v2(raw_owner)
    publication_values: dict[str, object] = {
        "version": NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2,
        "ledger_sha256": NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2,
        "handle": handle,
        "documents": documents,
        "import_manifest": values["import_manifest"],
        "root_document_key": values["root_document_key"],
        "load_options": options,
        "diagnostics": values["diagnostics"],
        "diagnostic_reference_sidecars": sidecars,
        "facade_cardinality_summary": summary,
        "report": report,
        "capability_bits": 7,
        "max_facade_row_bytes": maximum_row_bytes,
        "owl2_dl_report_summary": None,
    }
    for name in (
        "root_table_sha256",
        "effective_root_table_sha256",
        "fingerprint_inputs_sha256",
        "source_manifest_sha256",
        "provenance_manifest_sha256",
        "effective_origin_manifest_sha256",
    ):
        publication_values[name] = getattr(content, name)
    return freeze_native_snapshot_publication_v2(publication_values), raw_owner, maximum_row_bytes


def test_unique_axiom_native_owner_fixture_uses_registered_bounded_pages() -> None:
    extension = load_extension()
    fixture = getattr(extension, "_unique_axiom_publication_fixture_v2", None)
    if not callable(fixture):
        pytest.skip("selected native artifact lacks the unique-axiom V2 owner fixture")
    row_count = 257
    publication, raw_owner, row_bytes = _publication(
        cast(Callable[..., object], fixture),
        row_count,
    )
    snapshot = ontology_snapshot_from_native_publication_v2(publication)

    observed = tuple(snapshot.iter_axioms())

    assert type(raw_owner) is cast(Any, extension)._NativeSnapshotHandle
    assert len(observed) == row_count
    assert observed[0] == Declaration(Class(IRI(f"{_IRI_PREFIX}{0:020}")))
    assert observed[-1] == Declaration(Class(IRI(f"{_IRI_PREFIX}{row_count - 1:020}")))
    native = cast(Any, raw_owner)._publication_counters_v2()
    python = cast(Any, snapshot)._native_python_counters()
    assert native.canonical_input_rows == row_count
    assert native.canonical_input_bytes == row_count * row_bytes
    assert native.page_requests == math.ceil(
        row_count / NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2["max_facade_page_rows"]
    )
    assert python.model_rows_materialized == row_count
    assert python.cache_current_entries <= NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2[
        "max_facade_cache_entries"
    ]


@pytest.mark.skipif(
    not _RUN_MILLION,
    reason="set PYOWL_CORE_RUN_WP15_MILLION=1 for the million-unique acceptance gate",
)
def test_million_unique_native_owner_stays_python_object_and_cache_bounded() -> None:
    extension = load_extension()
    fixture = getattr(extension, "_unique_axiom_publication_fixture_v2", None)
    if not callable(fixture):
        pytest.skip("selected native artifact lacks the million-unique V2 owner fixture")
    publication, raw_owner, row_bytes = _publication(
        cast(Callable[..., object], fixture),
        _ROW_COUNT,
    )
    assert type(raw_owner) is cast(Any, extension)._NativeSnapshotHandle
    before_native = cast(Any, raw_owner)._publication_counters_v2()

    gc.collect()
    objects_before_publication = len(gc.get_objects())
    snapshot = ontology_snapshot_from_native_publication_v2(publication)
    gc.collect()
    objects_after_publication = len(gc.get_objects())
    before_python = cast(Any, snapshot)._native_python_counters()

    assert type(snapshot).__name__ == "_NativeOntologySnapshot"
    assert len(snapshot.root.axioms) == _ROW_COUNT
    assert before_native.canonical_input_rows == _ROW_COUNT
    assert before_native.canonical_input_bytes == _ROW_COUNT * row_bytes
    assert before_native.retained_axiom_rows == 2 * _ROW_COUNT
    assert before_native.retained_root_bytes == _ROW_COUNT * row_bytes
    assert before_native.retained_owner_bytes < 256 * 1024**2
    assert before_native.peak_builder_live_bytes <= 1024**3
    assert before_native.publication_structural_rows_copied == 0
    assert before_native.page_requests == 0
    assert before_native.rows_emitted == 0
    assert before_python.model_rows_materialized == 0
    assert before_python.cache_current_entries == 0
    assert before_python.publication_objects < 256
    assert objects_after_publication - objects_before_publication < 1024

    traversed = 0
    prefix_length = len(_IRI_PREFIX)
    for expected_index, axiom in enumerate(snapshot.iter_axioms()):
        if type(axiom) is not Declaration or type(axiom.entity) is not Class:
            raise AssertionError("million-unique traversal returned the wrong axiom type")
        iri_value = axiom.entity.iri.value
        if (
            not iri_value.startswith(_IRI_PREFIX)
            or len(iri_value) != prefix_length + 20
            or int(iri_value[prefix_length:]) != expected_index
        ):
            raise AssertionError("million-unique traversal lost canonical row order")
        traversed += 1
    del axiom, iri_value
    gc.collect()

    objects_after_traversal = len(gc.get_objects())
    after_python = cast(Any, snapshot)._native_python_counters()
    after_native = cast(Any, raw_owner)._publication_counters_v2()
    max_cache_entries = NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2["max_facade_cache_entries"]
    max_cache_bytes = NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2["max_facade_cache_bytes"]
    expected_pages = math.ceil(
        _ROW_COUNT / NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2["max_facade_page_rows"]
    )

    assert traversed == _ROW_COUNT
    assert after_native.page_requests == expected_pages
    assert after_native.pages_returned == expected_pages
    assert after_native.rows_emitted == _ROW_COUNT
    assert after_native.axiom_rows_emitted == _ROW_COUNT
    assert after_native.payload_bytes_copied == _ROW_COUNT * row_bytes
    assert after_native.canonical_payload_bytes_copied == _ROW_COUNT * row_bytes
    assert after_python.model_rows_materialized == _ROW_COUNT
    assert after_python.cache_misses == _ROW_COUNT
    assert after_python.cache_hits == 0
    assert after_python.cache_evictions >= _ROW_COUNT - max_cache_entries
    assert after_python.cache_current_entries <= max_cache_entries
    assert after_python.cache_current_bytes <= max_cache_bytes
    assert after_python.cache_peak_bytes <= max_cache_bytes
    assert objects_after_traversal - objects_after_publication < 4096

    print(
        "WP15_MILLION_UNIQUE_EVIDENCE="
        + json.dumps(
            {
                "cache_current_bytes": after_python.cache_current_bytes,
                "cache_current_entries": after_python.cache_current_entries,
                "cache_evictions": after_python.cache_evictions,
                "cache_peak_bytes": after_python.cache_peak_bytes,
                "canonical_input_bytes": before_native.canonical_input_bytes,
                "gc_objects_after_publication": objects_after_publication,
                "gc_objects_after_traversal": objects_after_traversal,
                "gc_objects_before_publication": objects_before_publication,
                "model_rows_materialized": after_python.model_rows_materialized,
                "page_requests": after_native.page_requests,
                "peak_builder_live_bytes": before_native.peak_builder_live_bytes,
                "publication_objects": before_python.publication_objects,
                "retained_owner_bytes": before_native.retained_owner_bytes,
                "row_bytes": row_bytes,
                "rows_emitted": after_native.rows_emitted,
                "unique_axioms": traversed,
            },
            sort_keys=True,
        )
    )
