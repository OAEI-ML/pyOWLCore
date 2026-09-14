from __future__ import annotations

from typing import Any

import pytest

from pyowl_core import (
    IRI,
    AnnotationAssertionIndex,
    AnnotationProperty,
    AxiomScope,
    BackendPreference,
    BackendProtocolError,
    ImportPolicy,
    Literal,
    LoadOptions,
    ParseLimits,
    ResourceLimitError,
    load_snapshot,
)
from pyowl_core.model import canonical_bytes, structural_digest
from pyowl_core.model.axioms import AnnotationAssertion
from tests.native.foundation._support import load_extension

SOURCE = b"""Ontology(<urn:annotations>
 Declaration(Class(<urn:A>)) Declaration(Class(<urn:B>))
 SubClassOf(<urn:A> <urn:B>)
 AnnotationAssertion(<urn:label> <urn:A> "A"@en)
 AnnotationAssertion(Annotation(<urn:note> "nested") <urn:label> <urn:A> "A"@en)
 AnnotationAssertion(<urn:label> <urn:B> "B"^^<http://www.w3.org/2001/XMLSchema#string>)
 AnnotationAssertion(<urn:link> <urn:A> <urn:B>)
 AnnotationAssertion(<urn:flag> <urn:B> "false")
 AnnotationAssertion(<urn:link> _:anonymous <urn:A>)
)"""


@pytest.fixture(scope="module", autouse=True)
def extension() -> Any:
    selected = load_extension()
    assert getattr(selected, "NATIVE_ANNOTATION_COLUMNS_API_VERSION", None) == 1
    return selected


def owner(backend: BackendPreference = BackendPreference.NATIVE, **kwargs: Any) -> Any:
    return load_snapshot(
        SOURCE, options=LoadOptions(backend=backend, imports=ImportPolicy.IGNORE, **kwargs)
    )


def strict(source: Any, **options: Any) -> Any:
    return source.view(
        AnnotationAssertionIndex, require_native_pipeline=True, include_origins=False, **options
    )


def forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("native annotation query entered whole-ontology Python iteration")


def test_selected_columns_preserve_assertion_and_literal_identity(monkeypatch: Any) -> None:
    reference = owner(BackendPreference.PYTHON)
    expected = sorted(reference.iter_axioms(AnnotationAssertion), key=canonical_bytes)
    source = owner()
    monkeypatch.setattr(type(source), "iter_axioms", forbidden)
    monkeypatch.setattr(type(source), "iter_extensions", forbidden)
    index = strict(source)
    pages = list(index.iter_columns(max_rows=2, max_bytes=4096))
    assert all(len(page.subjects) <= 2 for page in pages)
    assert [row for page in pages for row in page.canonical_assertion_bytes] == [
        canonical_bytes(row) for row in expected
    ]
    assert [row for page in pages for row in page.assertion_digests] == [
        structural_digest(row) for row in expected
    ]
    assert [row for page in pages for row in page.values] == [row.value for row in expected]
    assert sum(page.report["published_rows"] for page in pages) == len(expected)
    assert index.report.row_count == len(expected)
    assert (
        source.view(AnnotationAssertionIndex, require_native_pipeline=True, include_origins=False)
        is index
    )
    assert AnnotationAssertionIndex.supports_native_columns(source)


def test_filter_before_scalar_wrapping_and_empty_filter(monkeypatch: Any) -> None:
    from pyowl_core.backends import annotation_columns

    source = owner()
    index = strict(source)
    decoded = []
    original = annotation_columns.decode_canonical

    def capture(data: bytes) -> Any:
        decoded.append(data)
        return original(data)

    monkeypatch.setattr(annotation_columns, "decode_canonical", capture)
    pages = list(
        index.iter_columns(
            subjects=[IRI("urn:B")], properties=[AnnotationProperty(IRI("urn:flag"))]
        )
    )
    assert len(pages) == 1 and len(pages[0].values) == 1
    assert isinstance(pages[0].values[0], Literal)
    assert pages[0].values[0].lexical_form == "false"
    assert len(decoded) == 3  # Only requested scalar result wrappers, no axiom reconstruction.
    assert list(index.iter_columns(properties=[])) == []
    assert list(index.iter_columns(subjects=[])) == []
    assert len(decoded) == 3


def test_page_survives_owner_close_but_new_requests_fail() -> None:
    source = owner()
    index = strict(source)
    pages = index.iter_columns(max_rows=1)
    first = next(pages)
    source.close()
    assert first.canonical_assertion_bytes and first.values
    from pyowl_core import ClosedSnapshotError

    with pytest.raises(ClosedSnapshotError):
        next(pages)


def test_indivisible_oversized_row_and_native_index_budget() -> None:
    with pytest.raises(ResourceLimitError):
        list(strict(owner()).iter_columns(max_bytes=1))
    from pyowl_core.backends.native_validation import _native_owner
    from pyowl_core.backends.native_views import _invoke_native_column_operation_v2

    source = owner()
    raw, extension = _native_owner(source)
    with pytest.raises(ResourceLimitError):
        _invoke_native_column_operation_v2(
            extension,
            extension._annotation_columns_v1,
            (raw, "closure", None),
            ParseLimits(max_index_rows=1),
            None,
        )


def test_strict_unsupported_owners_and_nested_fail_before_iteration(monkeypatch: Any) -> None:
    source = owner(BackendPreference.PYTHON)
    monkeypatch.setattr(type(source), "iter_axioms", forbidden)
    assert not AnnotationAssertionIndex.supports_native_columns(source)
    with pytest.raises(BackendProtocolError, match="native annotation"):
        strict(source)
    with pytest.raises(BackendProtocolError, match="native annotation"):
        strict(owner(), include_nested=True)


def test_root_columns_and_selected_origins() -> None:
    source = owner()
    index = source.view(
        AnnotationAssertionIndex, require_native_pipeline=True, scope=AxiomScope.ROOT
    )
    rows = list(index.iter_columns(properties=[AnnotationProperty(IRI("urn:label"))]))
    assert sum(len(page.values) for page in rows) == 3
    for page in rows:
        assert page.origins == tuple(
            source.origin_index.entries.get(digest, ()) for digest in page.assertion_digests
        )


def test_default_index_does_not_read_unrequested_nested_roots(monkeypatch: Any) -> None:
    source = owner(BackendPreference.PYTHON)
    original = type(source).iter_axioms
    calls = []

    def typed_only(self: Any, axiom_type: Any = None, **kwargs: Any) -> Any:
        calls.append(axiom_type)
        assert axiom_type is AnnotationAssertion
        return original(self, axiom_type, **kwargs)

    monkeypatch.setattr(type(source), "iter_axioms", typed_only)
    monkeypatch.setattr(type(source), "iter_extensions", forbidden)
    index = source.view(AnnotationAssertionIndex, include_origins=False)
    assert tuple(index.values(IRI("urn:B"), AnnotationProperty(IRI("urn:flag"))))
    assert calls == [AnnotationAssertion]


def test_cancelled_request_does_not_publish_rows() -> None:
    from pyowl_core import CancellationSource, OperationCancelledError

    index = strict(owner())
    source = CancellationSource()
    source.cancel()
    token = source.token
    with pytest.raises(OperationCancelledError):
        list(index.iter_columns(cancellation_token=token))


def test_imported_scope_selection_keeps_native_annotation_identity(tmp_path: Any) -> None:
    from pyowl_core import MappingResolver, ResolvedDocument

    imported = tmp_path / "annotations.ofn"
    imported.write_text(
        'Ontology(<urn:imported> AnnotationAssertion(<urn:label> <urn:B> "imported"))'
    )
    root = tmp_path / "root.ofn"
    root.write_text(
        f"Ontology(<urn:root> Import(<{imported.as_uri()}>) "
        'AnnotationAssertion(<urn:label> <urn:A> "root"))'
    )
    source = load_snapshot(
        root,
        options=LoadOptions(backend=BackendPreference.NATIVE),
        resolver=MappingResolver(
            {imported.as_uri(): ResolvedDocument(imported.read_bytes(), IRI(imported.as_uri()))}
        ),
    )
    root_pages = list(strict(source, scope=AxiomScope.ROOT).iter_columns())
    closure_pages = list(strict(source).iter_columns())
    assert {value.lexical_form for page in root_pages for value in page.values} == {"root"}
    assert {value.lexical_form for page in closure_pages for value in page.values} == {
        "root",
        "imported",
    }


def test_native_overlay_add_remove_and_layers_without_base_traversal(monkeypatch: Any) -> None:
    from pyowl_core import CanonicalSet, OntologyDelta, apply_delta

    source = owner()
    removed = next(source.iter_axioms(AnnotationAssertion))
    added = AnnotationAssertion(AnnotationProperty(IRI("urn:label")), IRI("urn:C"), IRI("urn:new"))
    first = apply_delta(
        source,
        OntologyDelta(add_axioms=CanonicalSet((added,)), remove_axioms=CanonicalSet((removed,))),
    )
    second = apply_delta(
        first,
        OntologyDelta(add_axioms=CanonicalSet((removed,)), remove_axioms=CanonicalSet((added,))),
    )
    expected = sorted(canonical_bytes(row) for row in first.iter_axioms(AnnotationAssertion))
    original = sorted(canonical_bytes(row) for row in source.iter_axioms(AnnotationAssertion))
    monkeypatch.setattr(type(source), "iter_axioms", forbidden)
    monkeypatch.setattr(type(first), "iter_axioms", forbidden)
    monkeypatch.setattr(type(first), "materialize", forbidden)
    index = strict(first)
    assert index._ontology is first
    assert [
        row for page in index.iter_columns(max_rows=1) for row in page.canonical_assertion_bytes
    ] == expected
    assert [
        row for page in strict(second).iter_columns() for row in page.canonical_assertion_bytes
    ] == original
    assert [
        row
        for page in strict(first, scope=AxiomScope.ROOT).iter_columns()
        for row in page.canonical_assertion_bytes
    ] == original
    origins = first.view(AnnotationAssertionIndex, require_native_pipeline=True).iter_columns(
        subjects=[IRI("urn:C")]
    )
    assert next(origins).origins == (first.origins_for(added),)


def test_selected_query_work_ignores_unrelated_annotation_rows() -> None:
    for unrelated in (0, 1000):
        rows = " ".join(
            f'AnnotationAssertion(<urn:other> <urn:U{i}> "unrelated")' for i in range(unrelated)
        )
        source = load_snapshot(
            (
                'Ontology(<urn:scale> AnnotationAssertion(<urn:selected> <urn:A> "chosen") '
                + rows
                + ")"
            ).encode(),
            options=LoadOptions(backend=BackendPreference.NATIVE, imports=ImportPolicy.IGNORE),
        )
        index = strict(source)
        for _ in range(3):
            pages = list(index.iter_columns(properties=[AnnotationProperty(IRI("urn:selected"))]))
            assert len(pages) == 1
            assert pages[0].report["scanned_annotation_rows"] == 1
            assert pages[0].report["published_rows"] == 1
            assert pages[0].report["index_build_scanned_roots"] == unrelated + 1
