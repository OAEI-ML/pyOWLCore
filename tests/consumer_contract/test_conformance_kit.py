from __future__ import annotations

import hashlib
import os

import pytest

import pyowl_core
from pyowl_core.adapters import (
    STRUCTURAL_CONSUMER_FEATURES,
    AdapterRequirement,
    CacheScope,
    ConsumerCacheKey,
    ConsumerObservation,
    OperationCounters,
    SnapshotProviderProbe,
    UnsupportedDisposition,
    UnsupportedFeature,
    UnsupportedFeatureReport,
    capture_view_contract,
    semantic_result_digest,
    verify_consumer_handoff,
)
from pyowl_core.model import canonical_bytes

OPTIONS = hashlib.sha256(b"fixture-options").digest()


def snapshot() -> pyowl_core.OntologySnapshot:
    return pyowl_core.load_snapshot(
        b"Ontology(<urn:handoff> Declaration(Class(<urn:handoff#A>)))",
        options=pyowl_core.LoadOptions(backend=pyowl_core.BackendPreference.PYTHON),
    )


def requirement() -> AdapterRequirement:
    return AdapterRequirement(
        consumer="fixture-consumer",
        consumer_version="1.0",
        consumer_api="fixture/1",
        required_features=STRUCTURAL_CONSUMER_FEATURES,
    )


def cache_key(view: pyowl_core.OntologyView) -> ConsumerCacheKey:
    return ConsumerCacheKey.for_view(
        view,
        consumer="fixture-consumer",
        consumer_version="1.0",
        consumer_api="fixture/1",
        compiler_schema="fixture-ir/1",
        compatibility_id="fixture-semantics/1",
        scope=CacheScope.LOGICAL,
        semantic_options_sha256=OPTIONS,
    )


def observation(source: object) -> ConsumerObservation:
    view = pyowl_core.coerce_snapshot(source)  # type: ignore[arg-type]
    return ConsumerObservation(
        view=view,
        result_sha256=semantic_result_digest(canonical_bytes(item) for item in view.iter_axioms()),
        cache_key=cache_key(view),
        unsupported=UnsupportedFeatureReport(),
    )


def test_provider_probe_rejects_every_source_recovery_surface() -> None:
    probe = SnapshotProviderProbe(snapshot())

    assert probe.owl_snapshot() is probe.snapshot
    with pytest.raises(pyowl_core.AdapterCompatibilityError):
        os.fspath(probe)
    with pytest.raises(pyowl_core.AdapterCompatibilityError):
        probe.read()
    with pytest.raises(pyowl_core.AdapterCompatibilityError):
        _ = probe.origin
    assert probe.provider_calls == 1
    assert probe.source_accesses == 3


def test_handoff_verifies_identity_zero_work_cache_and_immutable_contract() -> None:
    view = snapshot()
    counters = OperationCounters()
    before = capture_view_contract(view)

    report = verify_consumer_handoff(
        view,
        observation,
        requirement=requirement(),
        expected_cache_key=cache_key(view),
        counters=counters,
    )

    assert report.passed
    assert report.provider_calls == 1
    assert report.source_accesses == 0
    assert report.before == report.after == before
    assert report.operation_delta.to_dict() == {
        "parser": 0,
        "resolver": 0,
        "wire_encode": 0,
        "wire_decode": 0,
        "mmap_open": 0,
        "path_access": 0,
    }


def test_forbidden_parser_counter_is_a_hard_failure() -> None:
    view = snapshot()
    counters = OperationCounters()

    def parsing_adapter(source: object) -> ConsumerObservation:
        counters.increment("parser")
        return observation(source)

    with pytest.raises(pyowl_core.AdapterCompatibilityError) as caught:
        verify_consumer_handoff(
            view,
            parsing_adapter,
            requirement=requirement(),
            expected_cache_key=cache_key(view),
            counters=counters,
        )
    assert caught.value.code == "ADAPTER_CONFORMANCE"
    assert caught.value.diagnostic is not None
    assert caught.value.diagnostic.details["field"] == "operation_counters"


def test_unsupported_report_must_match_exhaustively() -> None:
    view = snapshot()
    expected = UnsupportedFeatureReport(
        (
            UnsupportedFeature(
                "FIXTURE_UNSUPPORTED",
                "DataPropertyRange",
                UnsupportedDisposition.UNSUPPORTED,
                2,
            ),
        )
    )

    with pytest.raises(pyowl_core.AdapterCompatibilityError) as caught:
        verify_consumer_handoff(
            view,
            observation,
            requirement=requirement(),
            expected_cache_key=cache_key(view),
            expected_unsupported=expected,
        )
    assert caught.value.diagnostic is not None
    assert caught.value.diagnostic.details["field"] == "unsupported_features"


def test_unsupported_report_is_canonical_and_rejects_duplicate_decisions() -> None:
    item = UnsupportedFeature(
        "FIXTURE_IGNORED",
        "AnnotationAssertion",
        UnsupportedDisposition.NONLOGICAL,
    )
    assert UnsupportedFeatureReport((item,)).digest == UnsupportedFeatureReport((item,)).digest
    with pytest.raises(ValueError, match="duplicate"):
        UnsupportedFeatureReport((item, item))
