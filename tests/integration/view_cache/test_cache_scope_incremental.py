from __future__ import annotations

import gc
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from pyowl_core import (
    IRI,
    AnnotationAssertionIndex,
    AssertedClassHierarchyView,
    AssertedPropertyHierarchyView,
    AxiomScope,
    AxiomTypeIndex,
    BackendPreference,
    CacheRetention,
    CancellationSource,
    CanonicalSet,
    Class,
    Declaration,
    DeclarationIndex,
    EntityReferenceIndex,
    ExpressionOccurrenceIndex,
    ImportPolicy,
    IndexCachePolicy,
    LoadOptions,
    MappingResolver,
    OntologyDelta,
    OperationCancelledError,
    ParseLimits,
    PropertyDomainRangeView,
    ReentrancyError,
    ResolvedDocument,
    ResourceLimitError,
    SignatureView,
    SubClassOf,
    ViewBuildStrategy,
    apply_delta,
    compose_views,
    configure_index_cache,
    index_cache_report,
    load_snapshot,
)
from pyowl_core.index.cache import IndexBuildBudget


def _source(identity: str, body: str, imports: str = "") -> bytes:
    return (f"Prefix(:=<urn:cache#>) Ontology(<urn:{identity}> {imports} {body})").encode()


def _snapshot(body: str, *, limits: ParseLimits | None = None):
    return load_snapshot(
        _source("cache", body),
        options=LoadOptions(
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
            limits=limits or ParseLimits(),
        ),
    )


def test_scope_options_are_canonical_and_overlay_composite_preserve_wp04_scope() -> None:
    root = _source("root", "Declaration(Class(:Root))", "Import(<urn:imported>)")
    imported = _source("imported", "Declaration(Class(:Imported))")
    ontology = load_snapshot(
        root,
        options=LoadOptions(
            imports=ImportPolicy.RESOLVE_LOCAL,
            backend=BackendPreference.PYTHON,
        ),
        resolver=MappingResolver(
            {
                "urn:imported": ResolvedDocument(
                    imported,
                    document_iri=IRI("urn:document:imported"),
                )
            }
        ),
    )
    closure = ontology.view(AxiomTypeIndex, scope="closure")
    assert closure is ontology.view(AxiomTypeIndex, scope=AxiomScope.CLOSURE)
    assert closure.count(Declaration) == 2
    root_index = ontology.view(AxiomTypeIndex, scope=AxiomScope.ROOT)
    assert root_index.count(Declaration) == 1
    imported_key = next(
        record.document_key
        for record in ontology.import_manifest.documents
        if record.document_key != ontology.root_document_key
    )
    document_index = ontology.view(
        AxiomTypeIndex,
        scope=AxiomScope.DOCUMENT,
        document_key=imported_key,
    )
    assert document_index.count(Declaration) == 1
    overlay = apply_delta(
        ontology,
        OntologyDelta(add_axioms=CanonicalSet((Declaration(Class(IRI("urn:cache#Added"))),))),
    )
    assert overlay.view(AxiomTypeIndex, scope=AxiomScope.ROOT).count(Declaration) == 1
    other = _snapshot("Declaration(Class(:Other))")
    composite = compose_views(ontology, other)
    with pytest.raises(ValueError):
        composite.view(AxiomTypeIndex, scope=AxiomScope.ROOT)
    with pytest.raises(TypeError, match="unknown"):
        ontology.view(AxiomTypeIndex, not_an_option=True)


@dataclass(frozen=True, slots=True)
class _CustomOptions:
    value: int = 0


class _CustomView:
    SCHEMA_NAME = "tests/custom-view"
    SCHEMA_VERSION = 1
    OPTIONS_TYPE = _CustomOptions
    builds = 0
    fail_once = False
    barrier: threading.Barrier | None = None
    lock = threading.Lock()

    def __init__(self, value: int) -> None:
        self.value = value

    @classmethod
    def _build(
        cls,
        ontology: object,
        options: object,
        budget: IndexBuildBudget,
        cancellation_token: object,
        started: float,
    ) -> _CustomView:
        assert isinstance(options, _CustomOptions)
        with cls.lock:
            cls.builds += 1
            should_fail = cls.fail_once
            cls.fail_once = False
        if cls.barrier is not None:
            cls.barrier.wait(timeout=2)
        budget.add("custom", bytes_=300)
        if should_fail:
            raise RuntimeError("injected")
        time.sleep(0.01)
        return cls(options.value)


class _CancellableView:
    SCHEMA_NAME = "tests/cancellable-view"
    SCHEMA_VERSION = 1
    OPTIONS_TYPE = _CustomOptions
    started = threading.Event()

    @classmethod
    def _build(
        cls,
        ontology: object,
        options: object,
        budget: IndexBuildBudget,
        cancellation_token: object,
        started: float,
    ) -> _CancellableView:
        cls.started.set()
        for _index in range(100_000):
            budget.add("rows", bytes_=1)
        return cls()


class _ReentrantView:
    SCHEMA_NAME = "tests/reentrant-view"
    SCHEMA_VERSION = 1
    OPTIONS_TYPE = _CustomOptions
    recurse = True

    @classmethod
    def _build(
        cls,
        ontology: object,
        options: object,
        budget: IndexBuildBudget,
        cancellation_token: object,
        started: float,
    ) -> _ReentrantView:
        if cls.recurse:
            ontology.view(cls)  # type: ignore[attr-defined]
        budget.add("view", bytes_=64)
        return cls()


def test_once_cache_concurrency_failure_retry_and_distinct_builds() -> None:
    ontology = _snapshot("Declaration(Class(:A))")
    _CustomView.builds = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = tuple(pool.map(lambda _item: ontology.view(_CustomView, value=1), range(16)))
    assert len({id(value) for value in values}) == 1
    assert _CustomView.builds == 1
    _CustomView.fail_once = True
    with pytest.raises(RuntimeError, match="injected"):
        ontology.view(_CustomView, value=2)
    recovered = ontology.view(_CustomView, value=2)
    assert recovered.value == 2
    assert _CustomView.builds == 3

    _CustomView.barrier = threading.Barrier(2)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(ontology.view, _CustomView, value=3)
            second = pool.submit(ontology.view, _CustomView, value=4)
            assert {first.result().value, second.result().value} == {3, 4}
    finally:
        _CustomView.barrier = None


def test_cancellation_and_limits_publish_nothing_and_remain_retryable() -> None:
    cancelled = _snapshot("Declaration(Class(:A))")
    source = CancellationSource()
    source.cancel("stop")
    with pytest.raises(OperationCancelledError):
        cancelled.view(_CustomView, value=91, cancellation_token=source.token)
    before = _CustomView.builds
    assert cancelled.view(_CustomView, value=91).value == 91
    assert _CustomView.builds == before + 1

    limited = _snapshot(
        "Declaration(Class(:A))",
        limits=ParseLimits(max_index_bytes=128),
    )
    with pytest.raises(ResourceLimitError) as caught:
        limited.view(_CustomView, value=1)
    assert caught.value.limit == "max_index_bytes"
    assert index_cache_report(limited).retained_entries == 0

    row_limited = _snapshot(
        "Declaration(Class(:A)) Declaration(Class(:B))",
        limits=ParseLimits(max_index_rows=1),
    )
    with pytest.raises(ResourceLimitError) as row_error:
        row_limited.view(AxiomTypeIndex)
    assert row_error.value.limit == "max_index_rows"

    patch_limited = _snapshot(
        "Declaration(Class(:A)) Declaration(Class(:B))",
        limits=ParseLimits(max_index_rows=2),
    )
    patch_limited.view(AxiomTypeIndex)
    too_large = apply_delta(
        patch_limited,
        OntologyDelta(add_axioms=CanonicalSet((Declaration(Class(IRI("urn:cache#C"))),))),
    )
    with pytest.raises(ResourceLimitError) as patch_error:
        too_large.view(AxiomTypeIndex)
    assert patch_error.value.limit == "max_index_rows"


def test_mid_build_cancellation_and_reentrant_failure_are_retryable() -> None:
    ontology = _snapshot("Declaration(Class(:A))")
    source = CancellationSource()
    _CancellableView.started.clear()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            ontology.view,
            _CancellableView,
            cancellation_token=source.token,
        )
        assert _CancellableView.started.wait(timeout=2)
        source.cancel("mid-build")
        with pytest.raises(OperationCancelledError):
            future.result(timeout=3)
    assert index_cache_report(ontology).reserved_bytes == 0
    assert isinstance(ontology.view(_CancellableView), _CancellableView)

    _ReentrantView.recurse = True
    with pytest.raises(ReentrancyError, match="dependency cycle"):
        ontology.view(_ReentrantView)
    _ReentrantView.recurse = False
    assert isinstance(ontology.view(_ReentrantView), _ReentrantView)


def test_weak_identity_registry_survives_lru_eviction_while_caller_holds_view() -> None:
    ontology = _snapshot("Declaration(Class(:A))")
    configure_index_cache(
        ontology,
        IndexCachePolicy(max_bytes=400, retention=CacheRetention.STRONG),
    )
    _CustomView.builds = 0
    first = ontology.view(_CustomView, value=1)
    first_ref = weakref.ref(first)
    ontology.view(_CustomView, value=2)
    report = index_cache_report(ontology)
    assert report.evictions >= 1
    assert report.live_identities == 2
    assert ontology.view(_CustomView, value=1) is first
    del first
    gc.collect()
    # A subsequent report cleans dead weak identities; a retained/promoted view
    # may remain alive, but never produces a second identity while held.
    assert first_ref() is not None or index_cache_report(ontology).live_identities >= 1


_BUILTINS = (
    SignatureView,
    AxiomTypeIndex,
    EntityReferenceIndex,
    DeclarationIndex,
    AnnotationAssertionIndex,
    AssertedClassHierarchyView,
    AssertedPropertyHierarchyView,
    PropertyDomainRangeView,
    ExpressionOccurrenceIndex,
)


@pytest.mark.parametrize("view_type", _BUILTINS)
def test_small_delta_and_composition_build_only_patch_member_metadata(
    view_type: type[object],
) -> None:
    body = " ".join(f"Declaration(Class(:C{index}))" for index in range(250))
    base = _snapshot(body)
    base_index = base.view(view_type)
    addition = SubClassOf(
        Class(IRI("urn:cache#C0")),
        Class(IRI("urn:cache#Added")),
    )
    overlay = apply_delta(base, OntologyDelta(add_axioms=CanonicalSet((addition,))))
    patched = overlay.view(view_type)
    assert patched.report.strategy is ViewBuildStrategy.PATCHED
    assert patched.report.own_bytes <= 1024
    assert patched.report.shared_bytes >= base_index.report.own_bytes

    other_body = " ".join(f"Declaration(Class(:D{index}))" for index in range(250))
    other = _snapshot(other_body)
    other_index = other.view(view_type)
    composite = compose_views(base, other)
    merged = composite.view(view_type)
    assert merged.report.strategy is ViewBuildStrategy.MERGED
    assert merged.report.own_bytes <= 1024
    assert merged.report.shared_bytes >= (
        base_index.report.own_bytes + other_index.report.own_bytes
    )
