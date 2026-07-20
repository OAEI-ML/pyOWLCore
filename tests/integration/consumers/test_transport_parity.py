from __future__ import annotations

import io
from pathlib import Path

import pytest

import pyowl_core
from pyowl_core.adapters import OperationCounters, SnapshotProviderProbe

from ._support import (
    FIXTURE_DOCUMENT_IRI,
    FIXTURE_SOURCE,
    REQUIREMENT,
    CountingResolver,
    FixtureConsumer,
    InstrumentedCore,
    InstrumentedPath,
    consumer_cache_key,
    core_public_observation,
    expected_unsupported,
    load_options,
    supported_backends,
)


@pytest.mark.parametrize("backend", supported_backends(), ids=lambda item: item.value)
def test_standalone_python_native_and_in_process_forms_match(
    backend: pyowl_core.BackendPreference,
    operation_counters: OperationCounters,
    tmp_path: Path,
) -> None:
    path = tmp_path / "consumer.ofn"
    path.write_bytes(FIXTURE_SOURCE)
    counted_path = InstrumentedPath(path, operation_counters)
    options = load_options(backend)
    sources: tuple[object, ...] = (
        FIXTURE_SOURCE,
        bytearray(FIXTURE_SOURCE),
        memoryview(FIXTURE_SOURCE),
        io.BytesIO(FIXTURE_SOURCE),
        io.StringIO(FIXTURE_SOURCE.decode("utf-8")),
        counted_path,
    )
    observations = []
    result_digests = []
    for source in sources:
        before = operation_counters.snapshot()
        view = pyowl_core.coerce_snapshot(
            source,  # type: ignore[arg-type]
            document_iri=FIXTURE_DOCUMENT_IRI,
            options=options,
        )
        delta = operation_counters.snapshot() - before
        expected_python_parses = int(backend is pyowl_core.BackendPreference.PYTHON)
        assert delta.parser == expected_python_parses
        consumer = FixtureConsumer()
        consumer_before = operation_counters.snapshot()
        observation = consumer(view)
        assert operation_counters.snapshot() == consumer_before
        assert consumer.last_view is view
        observations.append(core_public_observation(view))
        result_digests.append(observation.result_sha256)
    assert all(item == observations[0] for item in observations)
    assert len(set(result_digests)) == 1
    assert operation_counters.snapshot().path_access > 0

    snapshot = pyowl_core.load_snapshot(
        FIXTURE_SOURCE,
        document_iri=FIXTURE_DOCUMENT_IRI,
        options=options,
    )
    duplicate = pyowl_core.load_snapshot(
        FIXTURE_SOURCE,
        document_iri=FIXTURE_DOCUMENT_IRI,
        options=options,
    )
    shared: tuple[pyowl_core.OntologyView | SnapshotProviderProbe, ...] = (
        snapshot,
        pyowl_core.apply_delta(snapshot, pyowl_core.OntologyDelta()),
        pyowl_core.compose_views(snapshot, duplicate, roles=("source", "target")),
        SnapshotProviderProbe(snapshot),
    )
    for source in shared:
        before = operation_counters.snapshot()
        observation = FixtureConsumer()(source)
        assert operation_counters.snapshot() == before
        assert observation.result_sha256 == result_digests[0]
        if isinstance(source, SnapshotProviderProbe):
            assert source.provider_calls == 1
            assert source.source_accesses == 0


def test_direct_decoded_and_mmap_views_have_equal_consumer_results_and_keys(
    operation_counters: OperationCounters,
    tmp_path: Path,
) -> None:
    core = InstrumentedCore(operation_counters)
    direct = pyowl_core.load_snapshot(
        FIXTURE_SOURCE,
        document_iri=FIXTURE_DOCUMENT_IRI,
        options=load_options(),
    )
    payload = core.encode(direct)
    decoded = core.decode(payload)
    path = tmp_path / "consumer.pyocore"
    path.write_bytes(payload)
    mapped = core.open(path)
    try:
        acquired_counts = operation_counters.snapshot()
        observations = tuple(FixtureConsumer()(view) for view in (direct, decoded, mapped))
        assert operation_counters.snapshot() == acquired_counts
        assert len({item.result_sha256 for item in observations}) == 1
        assert len({item.cache_key for item in observations}) == 1
        assert observations[0].cache_key == consumer_cache_key(direct)
        assert all(item.unsupported == expected_unsupported(direct) for item in observations)
        assert {view.capabilities.backend for view in (direct, decoded, mapped)} == {"python"}
        assert "wire-verified" not in direct.capabilities.features
        assert {"wire-v1", "wire-verified"} <= decoded.capabilities.features
        assert {"wire-v1", "wire-verified", "mmap-snapshot"} <= mapped.capabilities.features
    finally:
        mapped.close()
    counts = operation_counters.snapshot()
    assert counts.parser == 1
    assert counts.wire_encode == 1
    assert counts.wire_decode == 1
    assert counts.mmap_open == 1


def test_import_resolution_occurs_once_before_consumer_handoff(
    operation_counters: OperationCounters,
) -> None:
    root = b"Ontology(<urn:root> Import(<urn:imported>) Declaration(Class(<urn:root#A>)))"
    imported = b"Ontology(<urn:imported> Declaration(Class(<urn:imported#B>)))"
    delegate = pyowl_core.MappingResolver(
        {
            "urn:imported": pyowl_core.ResolvedDocument(
                imported,
                pyowl_core.IRI("urn:imported-document"),
                pyowl_core.DocumentFormat.FUNCTIONAL,
            )
        }
    )
    resolver = CountingResolver(delegate, operation_counters)
    snapshot = pyowl_core.load_snapshot(
        root,
        document_iri="urn:root-document",
        options=load_options(),
        resolver=resolver,
    )
    acquired = operation_counters.snapshot()

    observation = FixtureConsumer()(snapshot)

    assert operation_counters.snapshot() == acquired
    assert acquired.parser == 2
    assert acquired.resolver == 1
    assert observation.view is snapshot
    assert snapshot.report.document_count == 2
    assert snapshot.is_complete


def test_conformance_report_accepts_exact_expected_cache_and_unsupported_report() -> None:
    from pyowl_core.adapters import verify_consumer_handoff

    snapshot = pyowl_core.load_snapshot(
        FIXTURE_SOURCE,
        document_iri=FIXTURE_DOCUMENT_IRI,
        options=load_options(),
    )
    report = verify_consumer_handoff(
        snapshot,
        FixtureConsumer(),
        requirement=REQUIREMENT,
        expected_cache_key=consumer_cache_key(snapshot),
        expected_unsupported=expected_unsupported(snapshot),
    )
    assert report.passed
