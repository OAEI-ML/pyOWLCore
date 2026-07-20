from __future__ import annotations

from itertools import product
from typing import Any, cast
from unittest.mock import patch

import pytest

from pyowl_core import (
    AxiomScope,
    BackendPreference,
    Declaration,
    DeclarationIndex,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    canonical_bytes,
    load_snapshot,
)
from pyowl_core.backends import native
from pyowl_core.index import AxiomTypeIndex
from tests.native.foundation._support import NativeTestExtension, load_extension


def _source() -> bytes:
    declarations = " ".join(f"Declaration(Class(:C{ordinal}))" for ordinal in range(70))
    references = " ".join(f"SubClassOf(:C{ordinal} :U{ordinal})" for ordinal in range(70))
    return (
        "Prefix(:=<urn:retained-declarations#>) "
        "Prefix(rdfs:=<http://www.w3.org/2000/01/rdf-schema#>) "
        "Ontology(<urn:retained-declarations> "
        f"{declarations} {references} "
        "SubClassOf(:C0 <http://www.w3.org/2002/07/owl#Thing>) "
        'AnnotationAssertion(rdfs:label :Only "annotation only"))'
    ).encode()


def _snapshot(backend: BackendPreference) -> object:
    return load_snapshot(
        _source(),
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=backend,
            collect_provenance=False,
        ),
    )


@pytest.fixture(scope="module", autouse=True)
def extension() -> NativeTestExtension:
    selected = load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available or "index-axiom-types-v1" not in result.features:
        pytest.skip(result.reason or "native axiom index capability is unavailable")
    if not hasattr(selected, "_retained_axiom_type_index_v1"):
        pytest.skip("selected native artifact lacks retained axiom ownership")
    return selected


def _entity_rows(index: DeclarationIndex) -> tuple[bytes, ...]:
    return tuple(canonical_bytes(value) for value in index.entities())


def _undeclared_rows(index: DeclarationIndex) -> tuple[bytes, ...]:
    return tuple(canonical_bytes(value) for value in index.undeclared_entities())


def test_native_declarations_page_only_declaration_postings_and_match_all_options(
    extension: NativeTestExtension,
) -> None:
    reference = cast(Any, _snapshot(BackendPreference.PYTHON))
    selected = cast(Any, _snapshot(BackendPreference.NATIVE))
    reference_index = reference.view(DeclarationIndex)
    expected_declarations = tuple(reference.iter_axioms(Declaration))
    before_python = selected._native_python_counters()
    unexpected = AssertionError("declaration index crossed structural axiom iteration")

    with patch.object(type(selected), "iter_axioms", side_effect=unexpected):
        index = selected.view(DeclarationIndex)

    after_build = selected._native_python_counters()
    axiom_index = selected.view(AxiomTypeIndex, include_origins=False)
    owner = cast(Any, axiom_index)._native_owner
    assert type(owner) is cast(Any, extension)._NativeRetainedAxiomTypeIndexV1
    assert owner._layout_v1()[-1]["axiom_rows"] > len(expected_declarations)
    assert owner._layout_v1()[-1]["complete_root_encode_calls"] == len(expected_declarations)
    assert after_build.model_rows_materialized - before_python.model_rows_materialized == len(
        expected_declarations
    )
    assert index.report.tables == reference_index.report.tables
    assert _entity_rows(index) == _entity_rows(reference_index)
    for entity in reference_index.entities():
        assert tuple(index.declarations(entity)) == tuple(reference_index.declarations(entity))

    for include_builtins, include_annotation_only in product((False, True), repeat=2):
        options = {
            "include_builtins": include_builtins,
            "include_annotation_only": include_annotation_only,
        }
        with patch.object(type(selected), "iter_axioms", side_effect=unexpected):
            selected_view = selected.view(DeclarationIndex, **options)
            selected_undeclared = _undeclared_rows(selected_view)
        reference_view = reference.view(DeclarationIndex, **options)
        assert selected_view.report.tables == reference_view.report.tables
        assert _entity_rows(selected_view) == _entity_rows(reference_view)
        assert selected_undeclared == _undeclared_rows(reference_view)

    document_options = {
        "scope": AxiomScope.DOCUMENT,
        "document_key": selected.root_document_key,
        "include_builtins": False,
        "include_annotation_only": False,
    }
    with patch.object(type(selected), "iter_axioms", side_effect=unexpected):
        selected_document = selected.view(DeclarationIndex, **document_options)
    reference_document = reference.view(DeclarationIndex, **document_options)
    assert selected_document.report.tables == reference_document.report.tables
    assert _entity_rows(selected_document) == _entity_rows(reference_document)

    retained_entities = tuple(index.entities())
    selected.close()
    assert selected.closed
    assert tuple(index.entities()) == retained_entities
    for entity in retained_entities:
        assert tuple(index.declarations(entity)) == tuple(reference_index.declarations(entity))


def test_python_declarations_do_not_acquire_a_retained_axiom_owner() -> None:
    selected = cast(Any, _snapshot(BackendPreference.PYTHON))

    selected.view(DeclarationIndex)
    axiom_index = selected.view(AxiomTypeIndex, include_origins=False)

    assert cast(Any, axiom_index)._native_owner is None
