from __future__ import annotations

from typing import Any

import pytest

from pyowl_core import (
    IRI,
    AnnotationAssertion,
    AnnotationProperty,
    AxiomScope,
    AxiomTypeIndex,
    BackendPreference,
    BackendProtocolError,
    CanonicalSet,
    Class,
    ClassAssertion,
    DataProperty,
    DataPropertyAssertion,
    ImportPolicy,
    LoadOptions,
    NamedIndividual,
    OntologyDelta,
    SubClassOf,
    apply_delta,
    load_snapshot,
)
from pyowl_core.index.axiom_types import AxiomCategory
from pyowl_core.model import canonical_bytes, walk
from tests.native.foundation._support import load_extension

SOURCE = b"""Ontology(<urn:typed>
 Declaration(Class(<urn:A>)) Declaration(NamedIndividual(<urn:A>))
 Declaration(ObjectProperty(<urn:p>))
 SubClassOf(<urn:A> <urn:B>)
 ClassAssertion(ObjectIntersectionOf(<urn:A> <urn:B>) <urn:i>)
 ClassAssertion(<urn:A> <urn:j>) ClassAssertion(<urn:B> <urn:A>)
 DataPropertyAssertion(<urn:p> <urn:i> "value")
 DataPropertyAssertion(<urn:p> <urn:j> "other")
 AnnotationAssertion(<urn:label> <urn:A> "A")
)"""


@pytest.fixture(scope="module", autouse=True)
def extension() -> Any:
    selected = load_extension()
    assert selected.NATIVE_AXIOM_INDEX_API_VERSION == 1
    return selected


def owner(backend: BackendPreference = BackendPreference.NATIVE, source: bytes = SOURCE) -> Any:
    return load_snapshot(source, options=LoadOptions(backend=backend, imports=ImportPolicy.IGNORE))


def strict(source: Any, **kwargs: Any) -> Any:
    return source.view(
        AxiomTypeIndex, require_native_pipeline=True, include_origins=False, **kwargs
    )


def forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("strict typed index traversed scalar ontology")


def test_native_types_entities_categories_and_exact_membership(monkeypatch: Any) -> None:
    reference = owner(BackendPreference.PYTHON).view(AxiomTypeIndex, include_origins=False)
    source = owner()
    monkeypatch.setattr(type(source), "iter_axioms", forbidden)
    index = strict(source)
    assert index.supports_native()
    for kind in (ClassAssertion, DataPropertyAssertion, SubClassOf, AnnotationAssertion):
        expected = list(reference.iter(kind))
        assert list(index.iter(kind)) == expected
        assert index.count(kind) == len(expected)
        for entity in (
            Class(IRI("urn:A")),
            NamedIndividual(IRI("urn:A")),
            NamedIndividual(IRI("urn:i")),
            DataProperty(IRI("urn:p")),
        ):
            selected = [row for row in expected if any(node == entity for node in walk(row))]
            assert list(index.iter(kind, referencing=entity)) == selected
            assert index.count(kind, referencing=entity) == len(selected)
            assert index.tuple(kind, referencing=entity) == tuple(selected)
    for category in AxiomCategory:
        assert list(index.iter_category(category)) == list(reference.iter_category(category))
        assert index.count_category(category) == reference.count_category(category)
    assert list(index.iter_all()) == list(reference.iter_all())
    axiom = next(index.iter(SubClassOf))
    assert index.posting(axiom).axiom == axiom
    assert index.posting(SubClassOf(Class(IRI("urn:absent")), Class(IRI("urn:B")))) is None
    assert index.native_report["has_object_data_property_punning"] is True


def test_requested_entity_rows_only_are_wrapped_and_pagination_is_canonical(
    monkeypatch: Any,
) -> None:
    from pyowl_core.backends import axiom_index

    unrelated = " ".join(f"ClassAssertion(<urn:A> <urn:U{i}>)" for i in range(200))
    source = owner(
        source=(
            "Ontology(<urn:many> ClassAssertion(<urn:A> <urn:selected>) " + unrelated + ")"
        ).encode()
    )
    index = strict(source)
    captured = []
    decode = axiom_index.decode_canonical

    def record(data: bytes) -> Any:
        captured.append(data)
        return decode(data)

    monkeypatch.setattr(axiom_index, "decode_canonical", record)
    for _ in range(3):
        assert (
            len(list(index.iter(ClassAssertion, referencing=NamedIndividual(IRI("urn:selected")))))
            == 1
        )
    assert len(captured) == 3
    assert index.native_report["requested_rows"] == 3
    rows = list(index.iter_all())
    assert len(rows) == 201
    assert [canonical_bytes(row) for row in rows] == sorted(
        set(canonical_bytes(row) for row in rows)
    )
    assert not index.native_report["has_object_data_property_punning"]


def test_annotation_overlay_nonannotation_reuse_is_explicit(monkeypatch: Any) -> None:
    source = owner()
    overlay = apply_delta(
        source,
        OntologyDelta(
            add_axioms=CanonicalSet(
                (
                    AnnotationAssertion(
                        AnnotationProperty(IRI("urn:note")), IRI("urn:A"), IRI("urn:value")
                    ),
                )
            )
        ),
    )
    expected = list(strict(source).iter(ClassAssertion))
    monkeypatch.setattr(type(overlay), "iter_axioms", forbidden)
    monkeypatch.setattr(type(source), "iter_axioms", forbidden)
    index = strict(overlay)
    assert index._ontology is overlay
    assert list(index.iter(ClassAssertion)) == expected
    with pytest.raises(BackendProtocolError, match="annotation overlay"):
        list(index.iter(AnnotationAssertion))
    assert strict(overlay, scope=AxiomScope.ROOT).count(AnnotationAssertion) == 1


def test_strict_typed_rejection_limits_cancellation_and_owner_close(monkeypatch: Any) -> None:
    from pyowl_core import (
        CancellationSource,
        ClosedSnapshotError,
        OperationCancelledError,
        ParseLimits,
        ResourceLimitError,
    )
    from pyowl_core.backends.native_validation import _native_owner
    from pyowl_core.backends.native_views import _invoke_native_column_operation_v2

    source = owner(BackendPreference.PYTHON)
    monkeypatch.setattr(type(source), "iter_axioms", forbidden)
    with pytest.raises(BackendProtocolError, match="native compact"):
        strict(source)
    source = owner()
    raw, extension = _native_owner(source)
    with pytest.raises(ResourceLimitError):
        _invoke_native_column_operation_v2(
            extension,
            extension._axiom_index_v1,
            (raw, "closure", None),
            ParseLimits(max_index_rows=1),
            None,
        )
    cancelled = CancellationSource()
    cancelled.cancel()
    with pytest.raises(OperationCancelledError):
        source.view(
            AxiomTypeIndex, require_native_pipeline=True, cancellation_token=cancelled.token
        )
    index = strict(source)
    row = next(index.iter(ClassAssertion))
    source.close()
    assert row
    with pytest.raises(ClosedSnapshotError):
        index.count(ClassAssertion)


def test_imported_scope_and_declaration_only_property_punning(tmp_path: Any) -> None:
    from pyowl_core import MappingResolver, ResolvedDocument

    imported = tmp_path / "import.ofn"
    imported.write_text(
        "Ontology(<urn:imported> ClassAssertion(<urn:B> <urn:i>) "
        "Declaration(DataProperty(<urn:pun>)))"
    )
    root = tmp_path / "root.ofn"
    root.write_text(
        f"Ontology(<urn:root> Import(<{imported.as_uri()}>) "
        "ClassAssertion(<urn:A> <urn:i>) Declaration(ObjectProperty(<urn:pun>)))"
    )
    source = load_snapshot(
        root,
        options=LoadOptions(backend=BackendPreference.NATIVE),
        resolver=MappingResolver(
            {imported.as_uri(): ResolvedDocument(imported.read_bytes(), IRI(imported.as_uri()))}
        ),
    )
    closure = strict(source)
    selected = strict(source, scope=AxiomScope.ROOT)
    assert closure.count(ClassAssertion, referencing=NamedIndividual(IRI("urn:i"))) == 2
    assert selected.count(ClassAssertion, referencing=NamedIndividual(IRI("urn:i"))) == 1
    assert closure.native_report["has_object_data_property_punning"] is True
    assert selected.native_report["has_object_data_property_punning"] is False
    assert list(closure.iter(ClassAssertion)) == sorted(
        closure.iter(ClassAssertion), key=canonical_bytes
    )


def test_logical_overlay_and_native_memory_budget_fail_closed(monkeypatch: Any) -> None:
    from pyowl_core import ParseLimits, ResourceLimitError
    from pyowl_core.backends.native_validation import _native_owner
    from pyowl_core.backends.native_views import _invoke_native_column_operation_v2

    source = owner()
    overlay = apply_delta(
        source,
        OntologyDelta(
            add_axioms=CanonicalSet((SubClassOf(Class(IRI("urn:B")), Class(IRI("urn:C"))),))
        ),
    )
    monkeypatch.setattr(type(overlay), "iter_axioms", forbidden)
    with pytest.raises(BackendProtocolError, match="changes in overlays"):
        strict(overlay)
    raw, extension = _native_owner(source)
    with pytest.raises(ResourceLimitError):
        _invoke_native_column_operation_v2(
            extension,
            extension._axiom_index_v1,
            (raw, "closure", None),
            ParseLimits(max_index_bytes=1),
            None,
        )
    with pytest.raises(ResourceLimitError):
        _invoke_native_column_operation_v2(
            extension,
            extension._axiom_index_v1,
            (raw, "closure", None),
            ParseLimits(max_memory_bytes=1),
            None,
        )


def test_default_index_does_not_claim_strict_native_diagnostics() -> None:
    index = owner(BackendPreference.PYTHON).view(AxiomTypeIndex)
    with pytest.raises(BackendProtocolError, match="require_native_pipeline=True"):
        _ = index.native_report
    assert index.count(ClassAssertion) == 3
