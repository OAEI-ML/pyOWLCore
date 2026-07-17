from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

import pyowl_core.model as m
from pyowl_core import (
    IRI,
    BackendPreference,
    ImportPolicy,
    LoadOptions,
    MappingResolver,
    ParseLimits,
    ResolvedDocument,
    encode_snapshot,
    load_snapshot,
)


def _document(index: int, edges: set[tuple[int, int]]) -> bytes:
    imports = " ".join(
        f"Import(<urn:generated:{target}>)"
        for source, target in sorted(edges)
        if source == index
    )
    return (
        f"Ontology(<urn:generated:{index}> {imports} "
        f"Declaration(Class(<urn:generated#C{index}>)))"
    ).encode()


def _reachable(edges: set[tuple[int, int]]) -> set[int]:
    result = {0}
    frontier = [0]
    while frontier:
        source = frontier.pop()
        for left, right in edges:
            if left == source and right not in result:
                result.add(right)
                frontier.append(right)
    return result


@st.composite
def _import_graph(draw: st.DrawFn) -> tuple[int, set[tuple[int, int]]]:
    count = draw(st.integers(min_value=1, max_value=7))
    edges = draw(
        st.sets(
            st.tuples(
                st.integers(min_value=0, max_value=count - 1),
                st.integers(min_value=0, max_value=count - 1),
            ),
            max_size=min(18, count * count),
        )
    )
    return count, edges


@settings(max_examples=40, deadline=None, derandomize=True)
@given(_import_graph())
def test_generated_import_graphs_preserve_cycles_and_ignore_mapping_order(
    graph: tuple[int, set[tuple[int, int]]],
) -> None:
    count, edges = graph
    documents = {f"urn:generated:{index}": _document(index, edges) for index in range(count)}
    options = LoadOptions(
        imports=ImportPolicy.RESOLVE_STRICT,
        backend=BackendPreference.PYTHON,
        limits=ParseLimits(max_concurrent_fetches=4),
    )
    first = load_snapshot(
        documents["urn:generated:0"],
        options=options,
        resolver=MappingResolver(
            {
                IRI(key): ResolvedDocument(value, IRI(key))
                for key, value in documents.items()
            }
        ),
    )
    second = load_snapshot(
        documents["urn:generated:0"],
        options=options,
        resolver=MappingResolver(
            {
                IRI(key): ResolvedDocument(value, IRI(key))
                for key, value in reversed(tuple(documents.items()))
            }
        ),
    )
    reachable = _reachable(edges)
    assert first.is_complete and second.is_complete
    assert len(first.documents) == len(reachable)
    assert len(first.import_manifest.edges) == sum(
        source in reachable for source, _target in edges
    )
    assert len(tuple(first.iter_axioms(m.Declaration))) == len(reachable)
    assert first.structural_fingerprint == second.structural_fingerprint
    assert first.logical_fingerprint == second.logical_fingerprint
    assert encode_snapshot(first) == encode_snapshot(second)
