from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import cast

import pytest

from pyowl_core import IRI, ImportPolicy, OntologyDocument, parse_document
from pyowl_core.document import imports as imports_module
from pyowl_core.document import snapshot as snapshot_module
from pyowl_core.document.imports import DocumentRecord
from pyowl_core.model import StructuralNode, walk

from .conftest import functional, load_options


class _Roots:
    def __init__(
        self,
        name: str,
        values: tuple[StructuralNode, ...],
        events: list[tuple[str, StructuralNode]],
    ) -> None:
        self._name = name
        self._values = values
        self._events = events

    def __iter__(self) -> Iterator[StructuralNode]:
        for value in self._values:
            self._events.append((f"yield:{self._name}", value))
            yield value


def _streaming_document(
    events: list[tuple[str, StructuralNode]],
) -> OntologyDocument:
    return cast(
        OntologyDocument,
        SimpleNamespace(
            ontology_annotations=_Roots(
                "annotations",
                (IRI("urn:test:annotation:1"), IRI("urn:test:annotation:2")),
                events,
            ),
            axioms=_Roots(
                "axioms",
                (IRI("urn:test:axiom:1"), IRI("urn:test:axiom:2")),
                events,
            ),
            extension_components=_Roots(
                "extensions",
                (IRI("urn:test:extension:1"),),
                events,
            ),
            origin_index=object(),
        ),
    )


def _tracked_walk(
    events: list[tuple[str, StructuralNode]],
    root: StructuralNode,
) -> Iterator[StructuralNode]:
    events.append(("walk", root))
    yield root


def _expected_events() -> list[tuple[str, StructuralNode]]:
    return [
        ("yield:annotations", IRI("urn:test:annotation:1")),
        ("walk", IRI("urn:test:annotation:1")),
        ("yield:annotations", IRI("urn:test:annotation:2")),
        ("walk", IRI("urn:test:annotation:2")),
        ("yield:axioms", IRI("urn:test:axiom:1")),
        ("walk", IRI("urn:test:axiom:1")),
        ("yield:axioms", IRI("urn:test:axiom:2")),
        ("walk", IRI("urn:test:axiom:2")),
        ("yield:extensions", IRI("urn:test:extension:1")),
        ("walk", IRI("urn:test:extension:1")),
    ]


def test_document_term_count_preserves_structural_walk_parity() -> None:
    document = parse_document(
        functional(
            "urn:root",
            body=(
                "Annotation(<urn:test#p> \"value\")",
                "Declaration(Class(:A))",
                "SubClassOf(:A ObjectIntersectionOf(:B :C))",
            ),
        ),
        options=load_options(ImportPolicy.IGNORE),
    )
    expected = sum(
        1
        for collection in (
            document.ontology_annotations,
            document.axioms,
            document.extension_components,
        )
        for root in collection
        for _ in walk(root)
    )

    assert imports_module._document_terms(document) == expected


def test_document_term_count_never_buffers_native_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, StructuralNode]] = []
    document = _streaming_document(events)
    monkeypatch.setattr(
        imports_module,
        "walk",
        lambda root: _tracked_walk(events, root),
    )

    assert imports_module._document_terms(document) == 5
    assert events == _expected_events()


def test_scope_scan_never_buffers_native_roots_and_preserves_collections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, StructuralNode]] = []
    document = _streaming_document(events)
    record = cast(
        DocumentRecord,
        SimpleNamespace(
            document_key="d1:test",
            source_sha256=b"\x01" * 32,
            document_fingerprint=SimpleNamespace(digest=b"\x02" * 32),
        ),
    )
    monkeypatch.setattr(
        snapshot_module,
        "_walk",
        lambda root: _tracked_walk(events, root),
    )

    scoped = snapshot_module._scope_documents((record,), (document,))

    assert events == _expected_events()
    assert scoped["d1:test"].annotations is document.ontology_annotations
    assert scoped["d1:test"].axioms is document.axioms
    assert scoped["d1:test"].extensions is document.extension_components
    assert scoped["d1:test"].identity_preserved
