from __future__ import annotations

import gc
import importlib.util
from dataclasses import replace

import pytest

from pyowl_core import (
    AxiomScope,
    ClosedSnapshotError,
    EntityKind,
    OntologyDelta,
    OntologyView,
    PythonParser,
    SnapshotInUseError,
    apply_delta,
    coerce_snapshot,
    compose_views,
)

from .conftest import declaration, snapshot

_HAS_TRACEMALLOC = importlib.util.find_spec("_tracemalloc") is not None


class _Lease:
    def __init__(self, owner: _InstrumentedView) -> None:
        self.owner = owner

    def __del__(self) -> None:
        self.owner.dependents -= 1


class _InstrumentedView:
    def __init__(self, base, *, axiom_count: int = 1_000_000) -> None:  # type: ignore[no-untyped-def]
        self.base = base
        self.axiom_count = axiom_count
        self.iter_calls = 0
        self.contains_calls = 0
        self.report_calls = 0
        self.failed_iterations = 0
        self.closed = False
        self.dependents = 0

    def _check(self) -> None:
        if self.closed:
            raise ClosedSnapshotError("instrumented mapped view is closed")

    @property
    def capabilities(self):  # type: ignore[no-untyped-def]
        self._check()
        return self.base.capabilities

    @property
    def is_complete(self) -> bool:
        self._check()
        return self.base.is_complete

    @property
    def origin_index(self):  # type: ignore[no-untyped-def]
        self._check()
        return self.base.origin_index

    @property
    def structural_fingerprint(self):  # type: ignore[no-untyped-def]
        self._check()
        return self.base.structural_fingerprint

    @property
    def logical_fingerprint(self):  # type: ignore[no-untyped-def]
        self._check()
        return self.base.logical_fingerprint

    @property
    def signature_fingerprint(self):  # type: ignore[no-untyped-def]
        self._check()
        return self.base.signature_fingerprint

    @property
    def report(self):  # type: ignore[no-untyped-def]
        self._check()
        self.report_calls += 1
        return replace(self.base.report, effective_axiom_count=self.axiom_count)

    def _check_open(self) -> None:
        self._check()

    def iter_axioms(self, axiom_type=None, *, scope=AxiomScope.CLOSURE, document_key=None):  # type: ignore[no-untyped-def]
        self._check()
        self.iter_calls += 1
        if self.failed_iterations:
            self.failed_iterations -= 1
            raise RuntimeError("injected iterator failure")
        yield from self.base.iter_axioms(axiom_type, scope=scope, document_key=document_key)

    def iter_extensions(self, namespace=None, *, scope=AxiomScope.CLOSURE, document_key=None):  # type: ignore[no-untyped-def]
        self._check()
        yield from self.base.iter_extensions(namespace, scope=scope, document_key=document_key)

    def ontology_annotations(self, *, scope=AxiomScope.CLOSURE, document_key=None):  # type: ignore[no-untyped-def]
        self._check()
        return self.base.ontology_annotations(scope=scope, document_key=document_key)

    def contains(self, axiom, *, scope=AxiomScope.CLOSURE, document_key=None):  # type: ignore[no-untyped-def]
        self._check()
        self.contains_calls += 1
        return self.base.contains(axiom, scope=scope, document_key=document_key)

    def signature(
        self,
        kind: EntityKind | None = None,
        *,
        scope=AxiomScope.CLOSURE,
        document_key=None,
        include_builtins=True,
    ):  # type: ignore[no-untyped-def]
        self._check()
        return self.base.signature(
            kind,
            scope=scope,
            document_key=document_key,
            include_builtins=include_builtins,
        )

    def view(self, view_type, /, **options):  # type: ignore[no-untyped-def]
        self._check()
        if options:
            raise TypeError
        if isinstance(self, view_type):
            return self
        raise LookupError

    def _retain_dependent(self):  # type: ignore[no-untyped-def]
        self._check()
        self.dependents += 1
        return _Lease(self)

    def close(self) -> None:
        if self.dependents:
            raise SnapshotInUseError("mapped view still has dependent ontology views")
        self.closed = True


@pytest.mark.skipif(not _HAS_TRACEMALLOC, reason="interpreter does not provide _tracemalloc")
def test_million_axiom_overlay_creation_is_delta_sized_and_does_not_iterate() -> None:
    import tracemalloc

    arena = _InstrumentedView(snapshot("A"))
    assert isinstance(arena, OntologyView)
    arena.report_calls = 0
    tracemalloc.start()
    before, _peak = tracemalloc.get_traced_memory()
    overlay = apply_delta(arena, OntologyDelta(add_axioms={declaration("B")}))
    after, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert overlay.base is arena
    assert arena.iter_calls == 0
    assert arena.contains_calls == 1
    assert arena.report_calls == 0
    assert after - before < 256_000
    assert peak - before < 512_000


@pytest.mark.skipif(not _HAS_TRACEMALLOC, reason="interpreter does not provide _tracemalloc")
def test_two_million_axiom_composition_retains_arenas_without_iteration() -> None:
    import tracemalloc

    left = _InstrumentedView(snapshot("A"))
    right = _InstrumentedView(snapshot("B"))
    tracemalloc.start()
    before, _peak = tracemalloc.get_traced_memory()
    composite = compose_views(left, right, roles=("source", "target"))
    after, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert composite.members[0].view is left
    assert composite.members[1].view is right
    assert left.iter_calls == right.iter_calls == 0
    assert left.contains_calls == right.contains_calls == 0
    assert left.report_calls == right.report_calls == 0
    assert after - before < 256_000
    assert peak - before < 512_000


def test_layering_composition_and_coercion_do_not_force_reports_or_iteration() -> None:
    arena = _InstrumentedView(snapshot("A"))
    other = _InstrumentedView(snapshot("Z"))
    first = apply_delta(arena, OntologyDelta(add_axioms={declaration("B")}))
    second = apply_delta(first, OntologyDelta(add_axioms={declaration("C")}))
    composite = compose_views(second, other)

    assert coerce_snapshot(second) is second
    assert coerce_snapshot(composite) is composite
    assert arena.iter_calls == other.iter_calls == 0
    assert arena.report_calls == other.report_calls == 0


def test_in_process_handoff_never_invokes_the_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    left = snapshot("A")
    right = snapshot("B")

    def unexpected_parse(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("handoff reparsed an already-loaded ontology")

    monkeypatch.setattr(PythonParser, "parse", unexpected_parse)
    overlay = apply_delta(left, OntologyDelta(add_axioms={declaration("C")}))
    composite = compose_views(overlay, right)
    assert coerce_snapshot(overlay) is overlay
    assert coerce_snapshot(composite) is composite


def test_dependent_view_token_prevents_explicit_base_close() -> None:
    arena = _InstrumentedView(snapshot("A"))
    overlay = apply_delta(arena, OntologyDelta(add_axioms={declaration("B")}))
    assert arena.dependents == 1
    with pytest.raises(SnapshotInUseError):
        arena.close()

    del overlay
    gc.collect()
    assert arena.dependents == 0
    arena.close()
    with pytest.raises(ClosedSnapshotError):
        apply_delta(arena, OntologyDelta(add_axioms={declaration("C")}))


def test_composite_and_overlay_chain_retain_every_member_lease() -> None:
    left = _InstrumentedView(snapshot("A"))
    right = _InstrumentedView(snapshot("B"))
    composite = compose_views(left, right)
    overlay = apply_delta(composite, OntologyDelta(add_axioms={declaration("C")}))
    assert left.dependents == right.dependents == 1

    del composite
    gc.collect()
    assert left.dependents == right.dependents == 1
    del overlay
    gc.collect()
    assert left.dependents == right.dependents == 0


def test_failed_lazy_fingerprint_build_is_retryable() -> None:
    arena = _InstrumentedView(snapshot("A"))
    overlay = apply_delta(arena, OntologyDelta(add_axioms={declaration("B")}))
    arena.failed_iterations = 1

    with pytest.raises(RuntimeError, match="injected"):
        _fingerprint = overlay.structural_fingerprint
    assert overlay._fingerprint_cache is None
    assert overlay.structural_fingerprint.digest
