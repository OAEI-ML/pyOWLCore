from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import pytest

from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    EntityKind,
    ImportPolicy,
    LoadOptions,
    SignatureView,
    canonical_bytes,
    load_snapshot,
)
from pyowl_core.backends import native
from pyowl_core.exceptions import BackendProtocolError
from tests.native.foundation._support import NativeTestExtension, load_extension

SOURCE = b"""\
Prefix(:=<urn:retained-signature#>)
Prefix(rdfs:=<http://www.w3.org/2000/01/rdf-schema#>)
Ontology(<urn:retained-signature>
  Declaration(Class(:A))
  Declaration(ObjectProperty(:p))
  SubClassOf(Annotation(rdfs:label "edge") :A
    ObjectSomeValuesFrom(:p <http://www.w3.org/2002/07/owl#Thing>))
  EquivalentClasses(:A :B)
  AnnotationAssertion(rdfs:comment :Only "annotation only")
  AnnotationAssertion(rdfs:label :A "label")
)
"""


@pytest.fixture(scope="module", autouse=True)
def extension() -> NativeTestExtension:
    selected = load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available or "parse-functional-v1" not in result.features:
        pytest.skip(result.reason or "native Functional parser capability is unavailable")
    if not hasattr(selected, "_retained_signature_index_v1"):
        pytest.skip("selected native artifact lacks retained signature ownership")
    return selected


def _snapshot(backend: BackendPreference) -> object:
    return load_snapshot(
        SOURCE,
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=backend,
            collect_provenance=False,
        ),
    )


def _rows(view: SignatureView) -> tuple[bytes, ...]:
    return tuple(canonical_bytes(value) for value in view.iter())


def test_retained_signature_counts_avoid_structural_materialization_and_match_all_options(
    extension: NativeTestExtension,
) -> None:
    reference = cast(Any, _snapshot(BackendPreference.PYTHON))
    selected = cast(Any, _snapshot(BackendPreference.NATIVE))
    raw_owner = selected._native_snapshot_state.owner.handle._owner_v2
    before_owner = raw_owner._publication_counters_v2()
    before_python = selected._native_python_counters()
    unexpected = AssertionError("retained signature crossed scalar structural traversal")
    with (
        patch.object(type(selected), "iter_axioms", side_effect=unexpected),
        patch.object(type(selected), "iter_extensions", side_effect=unexpected),
        patch.object(type(selected), "ontology_annotations", side_effect=unexpected),
    ):
        index = selected.view(SignatureView)
    reference_index = reference.view(SignatureView)
    after_owner = raw_owner._publication_counters_v2()
    after_python = selected._native_python_counters()

    owner = cast(Any, index)._native_owner
    assert type(owner) is cast(Any, extension)._NativeRetainedSignatureIndexV1
    (
        root_table_sha256,
        effective_root_table_sha256,
        referenced,
        nonannotation,
        declarations,
        counters,
    ) = owner._layout_v1()
    attestation = selected._native_snapshot_state.owner.handle._attestation_v2()
    assert root_table_sha256 == attestation.root_table_sha256
    assert effective_root_table_sha256 == attestation.effective_root_table_sha256
    assert counters["structural_root_rows"] == (
        len(reference.root.ontology_annotations)
        + len(reference.root.axioms)
        + len(reference.root.extension_components)
    )
    assert counters["entity_rows"] == len(referenced) == len(tuple(reference_index.iter()))
    assert len(nonannotation) == len(declarations) == len(referenced)
    assert counters["referenced_links"] == sum(referenced)
    assert counters["nonannotation_links"] == sum(nonannotation)
    assert counters["declaration_links"] == sum(declarations) == 2
    assert counters["retained_buffer_bytes"] >= 3 * 8 * len(referenced)
    assert counters["peak_owned_bytes"] >= counters["retained_buffer_bytes"]
    assert counters["complete_root_encode_calls"] == 0
    assert after_owner.page_requests > before_owner.page_requests
    assert after_python.model_rows_materialized - before_python.model_rows_materialized == len(
        referenced
    )
    assert index.report.tables == reference_index.report.tables
    assert _rows(index) == _rows(reference_index)
    for entity in reference_index.iter():
        assert index.reference_count(entity) == reference_index.reference_count(entity)
        assert index.declaration_count(entity) == reference_index.declaration_count(entity)

    option_sets = (
        {"kind": EntityKind.CLASS},
        {"declared_only": True},
        {"include_builtins": False},
        {"include_annotation_only": False},
        {
            "kind": EntityKind.CLASS,
            "declared_only": True,
            "include_builtins": False,
            "include_annotation_only": False,
        },
    )
    for options in option_sets:
        selected_view = selected.view(SignatureView, **options)
        reference_view = reference.view(SignatureView, **options)
        assert type(cast(Any, selected_view)._native_owner) is type(owner)
        assert selected_view.report.tables == reference_view.report.tables
        assert _rows(selected_view) == _rows(reference_view)

    selected.close()
    assert selected.closed
    assert owner._layout_v1()[2] == referenced
    assert _rows(index) == _rows(reference_index)


def test_foreign_retained_signature_owner_fails_closed(
    extension: NativeTestExtension,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = cast(Any, _snapshot(BackendPreference.NATIVE))
    foreign = cast(
        Any,
        load_snapshot(
            b"Ontology(<urn:foreign-signature> Declaration(Class(<urn:foreign-signature:A>)))",
            options=LoadOptions(
                format=DocumentFormat.FUNCTIONAL,
                imports=ImportPolicy.IGNORE,
                backend=BackendPreference.NATIVE,
                collect_provenance=False,
            ),
        ),
    )
    operation = cast(Any, extension)._retained_signature_index_v1
    foreign_raw_owner = foreign._native_snapshot_state.owner.handle._owner_v2
    foreign_owner = operation(
        foreign_raw_owner,
        "closure",
        None,
        native._encode_config(foreign.load_options.limits, None, verify=False),
        None,
    )

    def substitute_owner(*_args: object) -> object:
        return foreign_owner

    monkeypatch.setattr(
        cast(Any, extension),
        "_retained_signature_index_v1",
        substitute_owner,
    )
    unexpected = AssertionError("invalid retained signature reached structural fallback")
    with (
        patch.object(type(selected), "iter_axioms", side_effect=unexpected),
        pytest.raises(BackendProtocolError) as raised,
    ):
        selected.view(SignatureView)
    assert raised.value.code == "NATIVE_INDEX_RESULT"


def test_python_signature_view_never_acquires_a_native_owner() -> None:
    selected = cast(Any, _snapshot(BackendPreference.PYTHON))

    index = selected.view(SignatureView)

    assert cast(Any, index)._native_owner is None


def test_empty_retained_signature_is_canonical(extension: NativeTestExtension) -> None:
    selected = cast(
        Any,
        load_snapshot(
            b"Ontology(<urn:empty-retained-signature>)",
            options=LoadOptions(
                format=DocumentFormat.FUNCTIONAL,
                imports=ImportPolicy.IGNORE,
                backend=BackendPreference.NATIVE,
                collect_provenance=False,
            ),
        ),
    )

    index = selected.view(SignatureView)
    owner = cast(Any, index)._native_owner
    assert type(owner) is cast(Any, extension)._NativeRetainedSignatureIndexV1
    _, _, referenced, nonannotation, declarations, counters = owner._layout_v1()
    assert referenced == nonannotation == declarations == ()
    assert counters["structural_root_rows"] == counters["entity_rows"] == 0
    assert tuple(index.iter()) == ()
