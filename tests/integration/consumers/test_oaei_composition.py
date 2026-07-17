from __future__ import annotations

from pathlib import Path

import pyowl_core
from pyowl_core.adapters import OperationCounters

from ._support import FixtureConsumer, InstrumentedCore, load_options


def _snapshot(iri: str, classes: tuple[str, ...]) -> pyowl_core.OntologySnapshot:
    declarations = " ".join(f"Declaration(Class(<{value}>))" for value in classes)
    return pyowl_core.load_snapshot(
        f"Ontology(<{iri}> {declarations})".encode(),
        document_iri=f"{iri}:document",
        options=load_options(),
    )


def _bridges(pairs: tuple[tuple[str, str], ...]) -> pyowl_core.OntologyDelta:
    return pyowl_core.OntologyDelta(
        add_axioms=pyowl_core.CanonicalSet(
            pyowl_core.EquivalentClasses(
                pyowl_core.CanonicalSet(
                    (
                        pyowl_core.Class(pyowl_core.IRI(source)),
                        pyowl_core.Class(pyowl_core.IRI(target)),
                    )
                )
            )
            for source, target in pairs
        )
    )


def test_source_target_bridge_composition_and_batched_trials_are_zero_reparse(
    operation_counters: OperationCounters,
) -> None:
    source = _snapshot("urn:oaei:source", ("urn:source#A", "urn:source#B"))
    target = _snapshot("urn:oaei:target", ("urn:target#A", "urn:target#B"))
    acquired = operation_counters.snapshot()
    assert acquired.parser == 2
    source_before = tuple(source.iter_axioms())
    target_before = tuple(target.iter_axioms())

    deltas = (
        _bridges((("urn:source#A", "urn:target#A"),)),
        _bridges(
            (
                ("urn:source#A", "urn:target#A"),
                ("urn:source#B", "urn:target#B"),
            )
        ),
    )
    digests = []
    for delta in deltas:
        composite = pyowl_core.compose_views(
            source,
            target,
            delta=delta,
            roles=("source", "target"),
        )
        assert tuple(member.view for member in composite.members) == (source, target)
        assert tuple(member.role for member in composite.members) == ("source", "target")
        assert pyowl_core.coerce_snapshot(composite) is composite
        assert composite.requested_delta is delta
        assert composite.delta == delta
        direct = FixtureConsumer()(composite)
        materialized = FixtureConsumer()(composite.materialize())
        assert direct.result_sha256 == materialized.result_sha256
        digests.append(direct.result_sha256)
        assert operation_counters.snapshot() == acquired
    assert digests[0] != digests[1]
    assert tuple(source.iter_axioms()) == source_before
    assert tuple(target.iter_axioms()) == target_before


def test_composite_standalone_in_process_and_wire_results_match(
    operation_counters: OperationCounters,
    tmp_path: Path,
) -> None:
    source = _snapshot("urn:oaei:source", ("urn:source#A",))
    target = _snapshot("urn:oaei:target", ("urn:target#A",))
    composite = pyowl_core.compose_views(
        source,
        target,
        delta=_bridges((("urn:source#A", "urn:target#A"),)),
        roles=("source", "target"),
    )
    direct = FixtureConsumer()(composite)
    counts_before_wire = operation_counters.snapshot()
    core = InstrumentedCore(operation_counters)
    payload = core.encode(composite)
    decoded = core.decode(payload)
    path = tmp_path / "oaei.pyocore"
    path.write_bytes(payload)
    mapped = core.open(path)
    try:
        worker_counts = operation_counters.snapshot()
        decoded_result = FixtureConsumer()(decoded)
        mapped_result = FixtureConsumer()(mapped)
        assert operation_counters.snapshot() == worker_counts
        assert direct.result_sha256 == decoded_result.result_sha256 == mapped_result.result_sha256
        assert direct.cache_key == decoded_result.cache_key == mapped_result.cache_key
        assert {"wire-v1", "wire-verified"} <= decoded.capabilities.features
        assert {"wire-v1", "wire-verified", "mmap-snapshot"} <= mapped.capabilities.features
    finally:
        mapped.close()
    delta = operation_counters.snapshot() - counts_before_wire
    assert delta.parser == 0
    assert delta.resolver == 0
    assert delta.wire_encode == 1
    assert delta.wire_decode == 1
    assert delta.mmap_open == 1
