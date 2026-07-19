from __future__ import annotations

import copy
import pickle
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Protocol, cast

import pytest

from pyowl_core.document.native_storage import (
    ontology_snapshot_from_native_publication_v2,
)
from pyowl_core.document.provenance import OriginIndex, SourceMap
from pyowl_core.exceptions import ClosedSnapshotError
from pyowl_core.model.validation import OWL2DLReport, RoleAnalysis, StructuralReport

from .test_cache_and_queries import _fixture_axiom, _source_map_publication
from .test_lazy_reports import _owl_publication


class _Closable(Protocol):
    def close(self) -> None: ...


def _close(value: object) -> None:
    cast(_Closable, value).close()


def _assert_copy_pickle_contract(value: object, operation: str) -> None:
    if operation == "copy":
        assert copy.copy(value) is value
    elif operation == "deepcopy":
        assert copy.deepcopy(value) is value
    else:
        assert operation == "pickle"
        with pytest.raises(TypeError, match="cannot be pickled"):
            pickle.dumps(value)


def _lazy_mapping_value(name: str) -> tuple[object, object, object]:
    snapshot = ontology_snapshot_from_native_publication_v2(_source_map_publication())
    document = snapshot.root
    source_map = cast(SourceMap, document.source_map)
    origin_index = snapshot.origin_index
    values: dict[str, object] = {
        "source-map": source_map,
        "source-entries": source_map.entries,
        "source-prefixes": source_map.prefixes,
        "origin-index": origin_index,
        "origin-entries": origin_index.entries,
    }
    return values[name], document, snapshot


@pytest.mark.parametrize(
    "name",
    (
        "source-map",
        "source-entries",
        "source-prefixes",
        "origin-index",
        "origin-entries",
    ),
)
@pytest.mark.parametrize("operation", ("copy", "deepcopy", "pickle"))
@pytest.mark.parametrize("closed", (False, True), ids=("open", "closed"))
def test_lazy_mapping_facades_preserve_copy_and_pickle_lifecycle(
    name: str,
    operation: str,
    *,
    closed: bool,
) -> None:
    value, document, snapshot = _lazy_mapping_value(name)
    try:
        if closed:
            _close(document if name.startswith("source") else snapshot)
        _assert_copy_pickle_contract(value, operation)
    finally:
        _close(document)
        _close(snapshot)


@pytest.mark.parametrize("name", ("source-map", "origin-index"))
def test_lazy_mapping_dataclasses_reject_replace(name: str) -> None:
    value, document, snapshot = _lazy_mapping_value(name)
    try:
        with pytest.raises(TypeError, match="cannot be replaced"):
            replace(cast(Any, value))
    finally:
        _close(document)
        _close(snapshot)


def test_lazy_source_and_origin_values_match_eager_values_and_fail_closed() -> None:
    published = _source_map_publication()
    snapshot = ontology_snapshot_from_native_publication_v2(published)
    document = snapshot.root
    source_map = cast(SourceMap, document.source_map)
    origin_index = snapshot.origin_index

    before_repr = published.handle._facade_counters_v2()
    source_iterator = iter(source_map.entries)
    prefix_iterator = iter(source_map.prefixes)
    origin_iterator = iter(origin_index.entries)
    assert "native source mapping" in repr(source_map.entries)
    assert "native prefix mapping" in repr(source_map.prefixes)
    assert "native origin mapping" in repr(origin_index.entries)
    after_repr = published.handle._facade_counters_v2()
    assert after_repr.page_requests == before_repr.page_requests

    eager_source = SourceMap(
        dict(source_map.entries.items()),
        dict(source_map.prefixes.items()),
    )
    eager_origin = OriginIndex(dict(origin_index.entries.items()))
    assert source_map == eager_source
    assert eager_source == source_map
    assert hash(source_map) == hash(eager_source)
    assert origin_index == eager_origin
    assert eager_origin == origin_index
    assert hash(origin_index) == hash(eager_origin)

    digest = next(iter(eager_origin.entries))
    _close(document)
    _close(snapshot)

    for iterator in (source_iterator, prefix_iterator, origin_iterator):
        with pytest.raises(ClosedSnapshotError):
            next(iterator)
    operations: tuple[Callable[[], object], ...] = (
        lambda: len(source_map.entries),
        lambda: len(source_map.prefixes),
        lambda: source_map.prefixes["ex"],
        lambda: source_map.occurrences_for(_fixture_axiom()),
        lambda: hash(source_map.entries),
        lambda: hash(source_map.prefixes),
        lambda: hash(source_map),
        lambda: source_map == eager_source,
        lambda: len(origin_index.entries),
        lambda: origin_index.entries[digest],
        lambda: origin_index.origins_for(_fixture_axiom()),
        lambda: hash(origin_index.entries),
        lambda: hash(origin_index),
        lambda: origin_index == eager_origin,
    )
    for operation in operations:
        with pytest.raises(ClosedSnapshotError):
            operation()

    assert "native source mapping" in repr(source_map.entries)
    assert "native prefix mapping" in repr(source_map.prefixes)
    assert "native origin mapping" in repr(origin_index.entries)


@pytest.mark.parametrize("name", ("structural", "roles"))
def test_nested_owl_report_facades_preserve_lifecycle_contract(name: str) -> None:
    published, _property_a, _property_b = _owl_publication()
    snapshot = ontology_snapshot_from_native_publication_v2(published)
    report = cast(OWL2DLReport, snapshot.owl2_dl_report)
    value = report.structural if name == "structural" else report.roles

    assert copy.copy(value) is value
    assert copy.deepcopy(value) is value
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(value)
    with pytest.raises(TypeError, match="cannot be replaced"):
        if name == "structural":
            replace(cast(StructuralReport, value), complete=False)
        else:
            replace(cast(RoleAnalysis, value), hierarchy=())

    _close(snapshot)
    assert copy.copy(value) is value
    assert copy.deepcopy(value) is value
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(value)
