from __future__ import annotations

import gc
import mmap
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

import pyowl_core
from pyowl_core.backends import native
from tests.native.encoded_views import _independent as independent_decoder
from tests.native.encoded_views._independent import decode_root_canonical_bytes
from tests.native.encoded_views._support import scalar_root_bytes
from tests.native.foundation._support import NativeTestExtension, load_extension

LEFT_SOURCE = (
    b"Ontology(<urn:wp18:left> "
    b"Declaration(Class(<urn:wp18:A>)) "
    b"Declaration(Class(<urn:wp18:B>)) "
    b"SubClassOf(<urn:wp18:A> <urn:wp18:B>))"
)
RIGHT_SOURCE = (
    b"Ontology(<urn:wp18:right> "
    b"Declaration(Class(<urn:wp18:C>)) "
    b"Declaration(Class(<urn:wp18:D>)) "
    b"SubClassOf(<urn:wp18:C> <urn:wp18:D>))"
)


def _rdfxml_source(identity: str, sub_class: str, super_class: str) -> bytes:
    return f"""<?xml version="1.0"?>
<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
    xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="urn:wp18:{identity}"/>
  <owl:Class rdf:about="urn:wp18:{sub_class}">
    <rdfs:subClassOf rdf:resource="urn:wp18:{super_class}"/>
  </owl:Class>
  <owl:Class rdf:about="urn:wp18:{super_class}"/>
</rdf:RDF>
""".encode()


@pytest.fixture(scope="module", autouse=True)
def extension() -> NativeTestExtension:
    selected = load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available or "parse-functional-v1" not in result.features:
        pytest.skip(result.reason or "native Functional parser capability is unavailable")
    if not hasattr(selected, "_parse_functional_retained_v2"):
        pytest.skip("selected native artifact lacks retained Functional publication")
    return selected


@pytest.mark.parametrize(
    ("format", "left_source", "right_source"),
    (
        pytest.param(
            pyowl_core.DocumentFormat.FUNCTIONAL,
            LEFT_SOURCE,
            RIGHT_SOURCE,
            id="functional",
        ),
        pytest.param(
            pyowl_core.DocumentFormat.RDF_XML,
            _rdfxml_source("left", "A", "B"),
            _rdfxml_source("right", "C", "D"),
            id="rdfxml",
        ),
    ),
)
def test_retained_direct_wire_mmap_overlay_and_composite_matrix_avoids_scalar_base(
    tmp_path: Path,
    extension: NativeTestExtension,
    format: pyowl_core.DocumentFormat,
    left_source: bytes,
    right_source: bytes,
) -> None:
    if format is pyowl_core.DocumentFormat.RDF_XML and not hasattr(
        extension, "_parse_rdfxml_retained_v2"
    ):
        pytest.skip("selected native artifact lacks retained RDF/XML publication")
    python_options = _options(pyowl_core.BackendPreference.PYTHON, format)
    native_options = _options(pyowl_core.BackendPreference.NATIVE, format)
    left_reference = pyowl_core.load_snapshot(left_source, options=python_options)
    right_reference = pyowl_core.load_snapshot(right_source, options=python_options)
    if format is pyowl_core.DocumentFormat.RDF_XML:
        unexpected = AssertionError("guarded RDF/XML matrix crossed the Python parser")
        with (
            patch(
                "pyowl_core.backends.parser._NativeBackendDriver.select",
                autospec=True,
                return_value="native",
            ),
            patch(
                "pyowl_core.backends.python.parser.parse_rdfxml",
                side_effect=unexpected,
            ),
        ):
            left = pyowl_core.load_snapshot(left_source, options=native_options)
            right = pyowl_core.load_snapshot(right_source, options=native_options)
    else:
        left = pyowl_core.load_snapshot(left_source, options=native_options)
        right = pyowl_core.load_snapshot(right_source, options=native_options)
    assert type(left).__name__ == type(right).__name__ == "_NativeOntologySnapshot"

    left_owner = _raw_owner(left)
    right_owner = _raw_owner(right)
    left_before = cast(Any, left_owner)._publication_counters_v2()
    right_before = cast(Any, right_owner)._publication_counters_v2()
    left_python_before = cast(Any, left)._native_python_counters()
    right_python_before = cast(Any, right)._native_python_counters()

    left_direct = left.view(pyowl_core.EncodedStructuralView)
    right_direct = right.view(pyowl_core.EncodedStructuralView)
    overlay = pyowl_core.apply_delta(left, pyowl_core.OntologyDelta())
    composite = pyowl_core.compose_views(left, right, roles=("left", "right"))
    left_before_segmented_views = cast(Any, left_owner)._publication_counters_v2()
    right_before_segmented_views = cast(Any, right_owner)._publication_counters_v2()
    expected_overlay = scalar_root_bytes(
        pyowl_core.apply_delta(left_reference, pyowl_core.OntologyDelta())
    )
    expected_composite = scalar_root_bytes(
        pyowl_core.compose_views(left_reference, right_reference, roles=("left", "right"))
    )

    scalar_error = AssertionError("WP18 encoded matrix crossed native scalar traversal")
    with (
        patch.object(type(left), "iter_axioms", side_effect=scalar_error),
        patch.object(type(left), "iter_extensions", side_effect=scalar_error),
        patch.object(type(left), "ontology_annotations", side_effect=scalar_error),
        patch.object(type(left), "signature", side_effect=scalar_error),
    ):
        overlay_encoded = overlay.view(pyowl_core.EncodedStructuralView)
        left_after_overlay = cast(Any, left_owner)._publication_counters_v2()
        right_after_overlay = cast(Any, right_owner)._publication_counters_v2()
        composite_encoded = composite.view(pyowl_core.EncodedStructuralView)
        left_after_views = cast(Any, left_owner)._publication_counters_v2()
        right_after_views = cast(Any, right_owner)._publication_counters_v2()
        payload = pyowl_core.encode_snapshot(left)

    decoded = pyowl_core.decode_snapshot(payload)
    path = tmp_path / f"retained-{format.value}-matrix.pyocore"
    path.write_bytes(payload)
    mapped = pyowl_core.open_snapshot(path, mmap=True, verify=True)
    assert isinstance(mapped, pyowl_core.MappedOntologySnapshot)
    mapped_encoded = mapped.view(pyowl_core.EncodedStructuralView)
    decoded_encoded = decoded.view(pyowl_core.EncodedStructuralView)

    assert left_direct.owner is left
    assert right_direct.owner is right
    assert decode_root_canonical_bytes(left_direct.buffers) == scalar_root_bytes(left_reference)
    assert decode_root_canonical_bytes(right_direct.buffers) == scalar_root_bytes(right_reference)
    assert decode_root_canonical_bytes(decoded_encoded.buffers) == scalar_root_bytes(left_reference)
    assert decode_root_canonical_bytes(mapped_encoded.buffers) == scalar_root_bytes(left_reference)
    assert _segmented_roots(overlay_encoded) == expected_overlay
    assert _segmented_roots(composite_encoded) == expected_composite

    overlay_proof = independent_decoder._decode_segmented_root_canonical_bytes(
        overlay_encoded
    ).proof
    composite_proof = independent_decoder._decode_segmented_root_canonical_bytes(
        composite_encoded
    ).proof
    assert overlay_proof.scalar_traversal_calls == 0
    assert composite_proof.scalar_traversal_calls == 0
    assert overlay_proof.referenced_buffer_copy_bytes == 0
    assert composite_proof.referenced_buffer_copy_bytes == 0
    assert any(cast(Any, value).owner is left for value in overlay_proof.retained_views)
    assert any(cast(Any, value).owner is left for value in composite_proof.retained_views)
    assert any(cast(Any, value).owner is right for value in composite_proof.retained_views)

    left_after = cast(Any, left_owner)._publication_counters_v2()
    right_after = cast(Any, right_owner)._publication_counters_v2()
    left_python_after = cast(Any, left)._native_python_counters()
    right_python_after = cast(Any, right)._native_python_counters()
    assert left_after_views.page_requests == left_before_segmented_views.page_requests
    assert right_after_views.page_requests == right_before_segmented_views.page_requests
    assert left_after_overlay.rows_emitted == left_before_segmented_views.rows_emitted
    assert right_after_overlay.rows_emitted == right_before_segmented_views.rows_emitted
    assert left_after_views.rows_emitted == left_before_segmented_views.rows_emitted
    assert right_after_views.rows_emitted == right_before_segmented_views.rows_emitted
    assert left_after.page_requests == left_after_views.page_requests
    assert right_after.page_requests == right_after_views.page_requests
    assert left_after.rows_emitted == left_before.rows_emitted
    assert right_after.rows_emitted == right_before.rows_emitted
    assert left_after.publication_structural_rows_copied == 0
    assert right_after.publication_structural_rows_copied == 0
    assert left_after.publication_structural_bytes_copied == 0
    assert right_after.publication_structural_bytes_copied == 0
    assert left_python_after.model_rows_materialized == left_python_before.model_rows_materialized
    assert right_python_after.model_rows_materialized == right_python_before.model_rows_materialized
    assert left_after.encoded_view_requests > left_before.encoded_view_requests
    assert right_after.encoded_view_requests > right_before.encoded_view_requests

    direct_exporters = {id(value.obj) for value in left_direct.buffers.values()}
    mapped_exporters = {id(value.obj) for value in mapped_encoded.buffers.values()}
    assert len(direct_exporters) == 1
    assert all(type(value.obj) is bytes for value in left_direct.buffers.values())
    assert len(mapped_exporters) == 1
    assert all(type(value.obj) is mmap.mmap for value in mapped_encoded.buffers.values())
    assert all(value.readonly for value in mapped_encoded.buffers.values())

    del mapped_encoded
    gc.collect()
    mapped.close()
    assert mapped.closed


def _options(
    backend: pyowl_core.BackendPreference,
    format: pyowl_core.DocumentFormat,
) -> pyowl_core.LoadOptions:
    return pyowl_core.LoadOptions(
        format=format,
        imports=pyowl_core.ImportPolicy.IGNORE,
        backend=backend,
        collect_provenance=False,
    )


def _raw_owner(snapshot: pyowl_core.OntologyView) -> object:
    handle = cast(Any, snapshot)._native_snapshot_state.owner.handle
    return cast(object, object.__getattribute__(handle, "_owner_v2"))


def _segmented_roots(view: object) -> tuple[tuple[int, bytes], ...]:
    decoded = independent_decoder._decode_segmented_root_canonical_bytes(view)
    return tuple((root.root_kind, root.canonical) for root in decoded.roots)
