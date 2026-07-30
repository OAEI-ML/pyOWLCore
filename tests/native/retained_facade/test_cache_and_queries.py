from __future__ import annotations

import os
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, cast

import pytest

from pyowl_core.backends.native_handoff import (
    NativeDocumentPublicationV1,
    NativeLoadReportPublicationV1,
)
from pyowl_core.backends.native_handoff_v2 import (
    NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2,
    NativeFacadeCollectionV2,
    NativeFacadeScopeV2,
    NativeSignatureKindV2,
    NativeSnapshotPublicationV2,
    NativeSourceMapRowV2,
    NativeSourcePrefixRowV2,
    encode_native_auxiliary_row_v2,
)
from pyowl_core.config import LoadOptions
from pyowl_core.diagnostics import SourceSpan
from pyowl_core.document.native_storage import (
    _deep_size,
    _NativeSharedState,
    _shallow_size,
    ontology_snapshot_from_native_publication_v2,
)
from pyowl_core.document.provenance import SourceMap
from pyowl_core.exceptions import ClosedSnapshotError
from pyowl_core.model import (
    IRI,
    Class,
    Declaration,
    EntityKind,
    canonical_bytes,
    structural_digest,
)

from ..publication_handoff._support import publication_fields
from ..publication_handoff._support_v2 import (
    FixtureKey,
    fixture_collections,
    publication,
    source_load_row_budget,
)


def _fixture_axiom() -> Declaration:
    return Declaration(Class(IRI("urn:handoff:Class")))


def _key(
    collection: NativeFacadeCollectionV2,
    scope: NativeFacadeScopeV2,
    ordinal: int | None,
    signature_kind: NativeSignatureKindV2 = NativeSignatureKindV2.ALL,
    include_builtins: bool = True,
) -> FixtureKey:
    return collection, scope, ordinal, signature_kind, include_builtins


def _large_publication(row_count: int = 257) -> NativeSnapshotPublicationV2:
    values = publication_fields()
    collections = dict(fixture_collections())
    axioms = (
        _fixture_axiom(),
        *(
            Declaration(Class(IRI(f"urn:retained-cache:{index:04d}")))
            for index in range(row_count - 1)
        ),
    )
    rows = tuple(sorted(canonical_bytes(item) for item in axioms))
    collections[
        _key(
            NativeFacadeCollectionV2.AXIOMS,
            NativeFacadeScopeV2.DOCUMENT,
            0,
        )
    ] = rows
    collections[
        _key(
            NativeFacadeCollectionV2.AXIOMS,
            NativeFacadeScopeV2.CLOSURE,
            None,
        )
    ] = rows
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    report = cast(NativeLoadReportPublicationV1, values["report"])
    values["documents"] = (replace(documents[0], axiom_count=row_count),)
    values["report"] = replace(report, effective_axiom_count=row_count)
    return publication(collections, values=values)


def _source_map_publication() -> NativeSnapshotPublicationV2:
    values = publication_fields()
    collections = dict(fixture_collections())
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    options = cast(LoadOptions, values["load_options"])
    digest = structural_digest(_fixture_axiom())
    span = SourceSpan(2, 9, 1, 3, 1, 10)
    _collection, source_row = encode_native_auxiliary_row_v2(
        NativeSourceMapRowV2(
            digest=digest,
            occurrence=4,
            span=span,
            lexical=(("token", "Class"),),
        ),
        max_row_bytes=source_load_row_budget(values),
    )
    _collection, prefix_row = encode_native_auxiliary_row_v2(
        NativeSourcePrefixRowV2(prefix="ex", iri="urn:example:"),
        max_row_bytes=source_load_row_budget(values),
    )
    collections[
        _key(
            NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES,
            NativeFacadeScopeV2.DOCUMENT,
            0,
        )
    ] = (source_row,)
    collections[
        _key(
            NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES,
            NativeFacadeScopeV2.DOCUMENT,
            0,
        )
    ] = (prefix_row,)
    values["documents"] = (
        replace(documents[0], source_map_entry_count=1),
    )
    values["load_options"] = replace(options, preserve_source_map=True)
    values["capability_bits"] = cast(int, values["capability_bits"]) | 8
    return publication(collections, values=values)


def test_buffered_page_iterators_observe_close_before_each_yield() -> None:
    document_snapshot = ontology_snapshot_from_native_publication_v2(_large_publication(3))
    document = document_snapshot.root
    document_values = iter(document.axioms)
    next(document_values)
    document.close()  # type: ignore[attr-defined]
    with pytest.raises(ClosedSnapshotError):
        next(document_values)

    snapshot = ontology_snapshot_from_native_publication_v2(_large_publication(3))
    closure_values = snapshot.iter_axioms()
    next(closure_values)
    snapshot.close()  # type: ignore[attr-defined]
    with pytest.raises(ClosedSnapshotError):
        next(closure_values)

    encoded_snapshot = ontology_snapshot_from_native_publication_v2(_large_publication(3))
    encoded_document = encoded_snapshot.root
    encoded_values = encoded_document.axioms._ref.iter_encoded()  # type: ignore[attr-defined]
    next(encoded_values)
    encoded_document.close()  # type: ignore[attr-defined]
    with pytest.raises(ClosedSnapshotError):
        next(encoded_values)


def _signature_publication() -> NativeSnapshotPublicationV2:
    values = publication_fields()
    collections = dict(fixture_collections())
    entity = Class(IRI("urn:handoff:Class"))
    row = canonical_bytes(entity)
    for scope, ordinal in (
        (NativeFacadeScopeV2.DOCUMENT, 0),
        (NativeFacadeScopeV2.CLOSURE, None),
    ):
        for kind in (NativeSignatureKindV2.ALL, NativeSignatureKindV2.CLASS):
            for include_builtins in (False, True):
                collections[
                    _key(
                        NativeFacadeCollectionV2.SIGNATURE,
                        scope,
                        ordinal,
                        kind,
                        include_builtins,
                    )
                ] = (row,)
    return publication(collections, values=values)


def test_cache_is_strong_shared_and_bounded_by_entry_count() -> None:
    small = ontology_snapshot_from_native_publication_v2(publication())
    published = _large_publication()
    snapshot = ontology_snapshot_from_native_publication_v2(published)

    small_count = small._native_python_counters().publication_objects  # type: ignore[attr-defined]
    large_before = snapshot._native_python_counters()  # type: ignore[attr-defined]
    assert large_before.publication_objects == small_count
    assert large_before.model_rows_materialized == 0
    assert published.handle._facade_counters_v2().page_requests == 0

    materialized = tuple(snapshot.root.axioms)
    assert len(materialized) == 257
    counters = snapshot._native_python_counters()  # type: ignore[attr-defined]
    assert counters.model_rows_materialized == 257
    assert counters.cache_misses == 257
    assert counters.cache_evictions >= 1
    assert counters.cache_current_entries <= NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2[
        "max_facade_cache_entries"
    ]
    assert counters.cache_current_bytes <= NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2[
        "max_facade_cache_bytes"
    ]
    assert published.handle._facade_counters_v2().page_requests == 5


def test_cache_byte_bound_covers_the_complete_retained_graph() -> None:
    values = publication_fields()
    options = cast(LoadOptions, values["load_options"])
    values["load_options"] = replace(
        options,
        limits=replace(options.limits, max_temporary_bytes=1_024),
    )
    state = _NativeSharedState(publication(values=values))

    for index in range(8):
        decoded = Class(IRI(f"urn:tight-cache:{index}"))
        state.consume(
            NativeFacadeCollectionV2.SIGNATURE,
            canonical_bytes(decoded),
            decoded,
        )

    counters = state.counters()
    actual_retained = _deep_size(state._cache)
    assert counters.cache_current_entries > 0
    assert actual_retained <= counters.cache_current_bytes
    assert counters.cache_current_bytes <= 1_024


def test_cache_drops_an_empty_grown_container_after_candidate_eviction() -> None:
    decoded = Class(IRI("urn:tight-cache:evicted"))
    encoded = canonical_bytes(decoded)
    key = ("model", encoded)
    visited: set[int] = set()
    admission_bytes = (
        _deep_size(key, visited)
        + _deep_size((decoded, 1), visited)
        + _shallow_size(OrderedDict())
    )
    values = publication_fields()
    options = cast(LoadOptions, values["load_options"])
    values["load_options"] = replace(
        options,
        limits=replace(options.limits, max_temporary_bytes=admission_bytes),
    )
    state = _NativeSharedState(publication(values=values))

    assert (
        state.consume(NativeFacadeCollectionV2.SIGNATURE, encoded, decoded) is decoded
    )

    counters = state.counters()
    assert counters.cache_evictions == 1
    assert counters.cache_current_entries == 0
    assert counters.cache_current_bytes == 0
    assert state._cache is None


def test_source_map_rows_and_prefixes_stay_lazy_and_match_eager_value_semantics() -> None:
    published = _source_map_publication()
    snapshot = ontology_snapshot_from_native_publication_v2(published)
    source_map = snapshot.root.source_map
    assert source_map is not None

    before = snapshot._native_python_counters()  # type: ignore[attr-defined]
    assert before.auxiliary_rows_decoded == 0
    assert published.handle._facade_counters_v2().page_requests == 0
    assert "<native source mapping" in repr(source_map.entries)
    assert "<native prefix mapping" in repr(source_map.prefixes)
    assert published.handle._facade_counters_v2().page_requests == 0

    occurrences = source_map.occurrences_for(_fixture_axiom())
    assert len(occurrences) == 1
    assert occurrences[0].occurrence == 4
    assert occurrences[0].span == SourceSpan(2, 9, 1, 3, 1, 10)
    assert occurrences[0].lexical == {"token": "Class"}
    assert source_map.prefixes["ex"] == "urn:example:"
    eager = SourceMap(
        dict(source_map.entries.items()),
        dict(source_map.prefixes.items()),
    )
    assert source_map == eager
    assert eager == source_map
    assert hash(source_map) == hash(eager)
    assert snapshot._native_python_counters().auxiliary_rows_decoded >= 2  # type: ignore[attr-defined]


def test_signature_projection_uses_native_pages_and_shared_model_cache() -> None:
    snapshot = ontology_snapshot_from_native_publication_v2(_signature_publication())
    entity = Class(IRI("urn:handoff:Class"))

    document_values = snapshot.root.signature()
    closure_values = snapshot.signature()
    assert document_values == closure_values == (entity,)
    assert document_values[0] is closure_values[0]
    assert snapshot.signature(EntityKind.CLASS, include_builtins=False) == (entity,)
    assert snapshot.signature(EntityKind.DATATYPE) == ()
    counters = snapshot._native_python_counters()  # type: ignore[attr-defined]
    assert counters.model_rows_materialized == 1
    assert counters.cache_hits >= 2


def test_concurrent_readers_share_immutable_values_safely() -> None:
    snapshot = ontology_snapshot_from_native_publication_v2(publication())

    def read_snapshot(_index: int) -> tuple[Declaration, ...]:
        return cast(tuple[Declaration, ...], tuple(snapshot.iter_axioms()))

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(read_snapshot, range(32)))

    expected = (_fixture_axiom(),)
    assert all(result == expected for result in results)
    first = results[0][0]
    assert all(result[0] is first for result in results)
    counters = snapshot._native_python_counters()  # type: ignore[attr-defined]
    assert counters.model_rows_materialized == 1
    assert counters.cache_hits == 31


def test_pid_change_resets_only_process_local_cache_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyowl_core.document import native_storage

    snapshot = ontology_snapshot_from_native_publication_v2(publication())
    assert tuple(snapshot.root.axioms) == (_fixture_axiom(),)
    before = snapshot._native_python_counters()  # type: ignore[attr-defined]
    assert before.cache_current_entries == 1
    inherited_materializations = before.model_rows_materialized
    current_pid = os.getpid()

    monkeypatch.setattr(
        cast(Any, native_storage).os,
        "getpid",
        lambda: current_pid + 1,
    )
    reset = snapshot._native_python_counters()  # type: ignore[attr-defined]
    assert reset.cache_current_entries == 0
    assert reset.cache_current_bytes == 0
    assert reset.cache_peak_bytes == 0
    assert reset.cache_hits == reset.cache_misses == reset.cache_evictions == 0
    assert reset.model_rows_materialized == inherited_materializations

    assert tuple(snapshot.root.axioms) == (_fixture_axiom(),)
    rebuilt = snapshot._native_python_counters()  # type: ignore[attr-defined]
    assert rebuilt.cache_current_entries == 1
    assert rebuilt.cache_misses == 1
    assert rebuilt.model_rows_materialized == inherited_materializations + 1
