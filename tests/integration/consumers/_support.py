from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any

import pyowl_core
from pyowl_core.adapters import (
    AdapterRequirement,
    CacheScope,
    ConsumerCacheKey,
    ConsumerObservation,
    OperationCounters,
    UnsupportedDisposition,
    UnsupportedFeature,
    UnsupportedFeatureReport,
    require_compatible_view,
    semantic_result_digest,
)
from pyowl_core.model import canonical_bytes

FIXTURE_DOCUMENT_IRI = "urn:pyowl-core:wp11:fixture-document"
FIXTURE_SOURCE = b"""Prefix(:=<urn:pyowl-core:wp11#>)
Ontology(<urn:pyowl-core:wp11>
 Declaration(Class(:A))
 Declaration(Class(:B))
 Declaration(ObjectProperty(:related))
 Declaration(DataProperty(:score))
 Declaration(AnnotationProperty(:label))
 SubClassOf(:A :B)
 AnnotationAssertion(:label :A \"Canonical name\"@EN-gb)
 DataPropertyRange(:score <http://www.w3.org/2001/XMLSchema#string>)
)
"""

OPTIONS_SHA256 = bytes.fromhex("1f0d8dbebf41c1b6f99c84cc19b4e5d152b1e10cc02049a27dc1eea940ef713d")

REQUIREMENT = AdapterRequirement(
    consumer="wp11-fixture-consumer",
    consumer_version="1.0.0",
    consumer_api="consumer-view/1",
    required_features=frozenset(
        {
            "document-boundaries",
            "document-scoped-anonymous",
            "import-manifest",
            "ontology-identity-index",
            "owl2-structural",
        }
    ),
)


def load_options(
    backend: pyowl_core.BackendPreference = pyowl_core.BackendPreference.PYTHON,
    *,
    preserve_source_map: bool = False,
) -> pyowl_core.LoadOptions:
    return pyowl_core.LoadOptions(
        backend=backend,
        format=pyowl_core.DocumentFormat.FUNCTIONAL,
        imports=pyowl_core.ImportPolicy.RESOLVE_LOCAL,
        preserve_source_map=preserve_source_map,
    )


class FixtureConsumer:
    """Small consumer-boundary scanner; it owns no model, parser, or resolver."""

    def __init__(self) -> None:
        self.last_view: pyowl_core.OntologyView | None = None

    def __call__(self, source: object) -> ConsumerObservation:
        view = pyowl_core.coerce_snapshot(source)  # type: ignore[arg-type]
        require_compatible_view(view, REQUIREMENT)
        self.last_view = view
        unsupported, included = _classify_axioms(view)
        return ConsumerObservation(
            view=view,
            result_sha256=semantic_result_digest(included),
            cache_key=consumer_cache_key(view),
            unsupported=unsupported,
        )


def consumer_cache_key(view: pyowl_core.OntologyView) -> ConsumerCacheKey:
    return ConsumerCacheKey.for_view(
        view,
        consumer=REQUIREMENT.consumer,
        consumer_version=REQUIREMENT.consumer_version,
        consumer_api=REQUIREMENT.consumer_api,
        compiler_schema="wp11-fixture-ir/1",
        compatibility_id="wp11-fixture-semantics/1",
        scope=CacheScope.LOGICAL,
        semantic_options_sha256=OPTIONS_SHA256,
    )


def expected_unsupported(view: pyowl_core.OntologyView) -> UnsupportedFeatureReport:
    report, _included = _classify_axioms(view)
    return report


def _classify_axioms(
    view: pyowl_core.OntologyView,
) -> tuple[UnsupportedFeatureReport, tuple[bytes, ...]]:
    counts: Counter[tuple[str, str, UnsupportedDisposition]] = Counter()
    included: list[bytes] = []
    for axiom in view.iter_axioms():
        constructor = type(axiom).__name__
        if isinstance(axiom, pyowl_core.DataPropertyRange):
            counts[
                (
                    "FIXTURE_DATA_PROPERTY_RANGE",
                    constructor,
                    UnsupportedDisposition.UNSUPPORTED,
                )
            ] += 1
        elif isinstance(axiom, pyowl_core.AnnotationAssertion):
            counts[
                (
                    "FIXTURE_ANNOTATION_NONLOGICAL",
                    constructor,
                    UnsupportedDisposition.NONLOGICAL,
                )
            ] += 1
        else:
            included.append(canonical_bytes(axiom))
    report = UnsupportedFeatureReport(
        tuple(
            UnsupportedFeature(code, constructor, disposition, count)
            for (code, constructor, disposition), count in counts.items()
        )
    )
    return report, tuple(included)


class InstrumentedPath(os.PathLike[str]):
    __slots__ = ("_counters", "_path")

    def __init__(self, path: Path, counters: OperationCounters) -> None:
        self._path = path
        self._counters = counters

    def __fspath__(self) -> str:
        self._counters.increment("path_access")
        return os.fspath(self._path)


class InstrumentedCore:
    """Explicit wire/path instrumentation; ordinary consumer calls bypass it."""

    __slots__ = ("counters",)

    def __init__(self, counters: OperationCounters) -> None:
        self.counters = counters

    def encode(self, view: pyowl_core.OntologyView) -> bytes:
        self.counters.increment("wire_encode")
        return pyowl_core.encode_snapshot(view)

    def decode(self, payload: bytes) -> pyowl_core.OntologySnapshot:
        self.counters.increment("wire_decode")
        return pyowl_core.decode_snapshot(payload)

    def open(self, path: Path) -> pyowl_core.OntologySnapshot:
        self.counters.increment("mmap_open")
        return pyowl_core.open_snapshot(path, mmap=True, verify=True)


class CountingResolver:
    """Public resolver wrapper retaining the exact delegate result."""

    __slots__ = ("_counters", "_delegate")

    def __init__(self, delegate: pyowl_core.ImportResolver, counters: OperationCounters) -> None:
        self._delegate = delegate
        self._counters = counters

    def resolve(self, request: pyowl_core.ImportRequest) -> pyowl_core.ResolvedDocument | None:
        self._counters.increment("resolver")
        return self._delegate.resolve(request)


def result_digest(view: pyowl_core.OntologyView) -> bytes:
    return FixtureConsumer()(view).result_sha256


def supported_backends() -> tuple[pyowl_core.BackendPreference, ...]:
    from pyowl_core.backends.native import probe

    values = [pyowl_core.BackendPreference.PYTHON]
    if probe("parse-functional-v1").available:
        values.append(pyowl_core.BackendPreference.NATIVE)
    return tuple(values)


def language_assertion(view: pyowl_core.OntologyView) -> pyowl_core.AnnotationAssertion:
    values = tuple(view.iter_axioms(pyowl_core.AnnotationAssertion))
    assert len(values) == 1
    assertion = values[0]
    assert isinstance(assertion, pyowl_core.AnnotationAssertion)
    return assertion


def source_language(document: pyowl_core.OntologyDocument) -> str | None:
    assertion = next(document.iter_axioms(pyowl_core.AnnotationAssertion))
    assert isinstance(assertion, pyowl_core.AnnotationAssertion)
    assert isinstance(assertion.value, pyowl_core.Literal)
    if document.source_map is None:
        return None
    occurrences = document.source_map.occurrences_for(assertion.value)
    if not occurrences:
        return None
    return occurrences[0].lexical.get("language-tag")


def core_public_observation(view: pyowl_core.OntologyView) -> tuple[Any, ...]:
    return (
        tuple(view.iter_axioms()),
        view.signature(include_builtins=False),
        view.logical_fingerprint,
        view.signature_fingerprint,
        view.is_complete,
    )


__all__ = [
    "FIXTURE_DOCUMENT_IRI",
    "FIXTURE_SOURCE",
    "REQUIREMENT",
    "CountingResolver",
    "FixtureConsumer",
    "InstrumentedCore",
    "InstrumentedPath",
    "consumer_cache_key",
    "core_public_observation",
    "expected_unsupported",
    "language_assertion",
    "load_options",
    "result_digest",
    "source_language",
    "supported_backends",
]
