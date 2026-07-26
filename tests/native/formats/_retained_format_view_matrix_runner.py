from __future__ import annotations

import gc
import json
import tempfile
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from tests.native.foundation._support import load_extension


def main() -> None:
    extension = load_extension()

    from pyowl_core import (
        AxiomScope,
        BackendPreference,
        DocumentFormat,
        EncodedStructuralView,
        ImportPolicy,
        LoadOptions,
        OntologyDelta,
        apply_delta,
        compose_views,
        decode_snapshot,
        encode_snapshot,
        load_snapshot,
        open_snapshot,
        parse_document,
        render_document,
    )
    from pyowl_core.backends import native
    from pyowl_core.model import canonical_bytes
    from tests.conformance._support import every_constructor_document
    from tests.native.encoded_views import _independent as independent_decoder
    from tests.native.encoded_views._independent import decode_root_canonical_bytes

    required_operations = (
        "_parse_functional_retained_v2",
        "_parse_rdfxml_retained_v2",
        "_parse_turtle_retained_v2",
        "_parse_owlxml_retained_v2",
    )
    missing = tuple(name for name in required_operations if not hasattr(extension, name))
    if missing:
        raise RuntimeError(f"native artifact lacks retained format operations: {missing!r}")

    native._reset_probe_cache_for_tests()
    probe = native.probe(refresh=True)
    if not probe.available:
        raise RuntimeError(probe.reason or "native backend is unavailable")

    formats = (
        DocumentFormat.FUNCTIONAL,
        DocumentFormat.RDF_XML,
        DocumentFormat.TURTLE,
        DocumentFormat.OWL_XML,
    )

    def options(
        format_value: DocumentFormat,
        backend: BackendPreference,
    ) -> LoadOptions:
        return LoadOptions(
            format=format_value,
            imports=ImportPolicy.IGNORE,
            backend=backend,
            collect_provenance=True,
            preserve_source_map=True,
        )

    def scalar_roots(view: object) -> tuple[tuple[int, bytes], ...]:
        selected = cast(Any, view)
        roots = [
            *((1, canonical_bytes(value)) for value in selected.ontology_annotations()),
            *((2, canonical_bytes(value)) for value in selected.iter_axioms()),
            *((3, canonical_bytes(value)) for value in selected.iter_extensions()),
        ]
        return tuple(sorted(roots))

    def forced_native_snapshot(
        source: bytes,
        format_value: DocumentFormat,
    ) -> Any:
        parser_name = {
            DocumentFormat.FUNCTIONAL: "parse_functional",
            DocumentFormat.RDF_XML: "parse_rdfxml",
            DocumentFormat.TURTLE: "parse_turtle",
            DocumentFormat.OWL_XML: "parse_owlxml",
        }[format_value]
        with (
            patch(
                "pyowl_core.backends.parser._NativeBackendDriver.select",
                autospec=True,
                return_value="native",
            ),
            patch(
                f"pyowl_core.backends.python.parser.{parser_name}",
                side_effect=AssertionError(
                    f"{format_value.value} forced-native matrix crossed the Python parser"
                ),
            ),
        ):
            return load_snapshot(
                source,
                options=options(format_value, BackendPreference.NATIVE),
            )

    observed: dict[str, dict[str, object]] = {}
    document = every_constructor_document()
    right_document = parse_document(
        b"Ontology(<urn:format-view-matrix:right> "
        b"Declaration(Class(<urn:format-view-matrix:R>)) "
        b"Declaration(Class(<urn:format-view-matrix:S>)) "
        b"SubClassOf(<urn:format-view-matrix:R> <urn:format-view-matrix:S>))",
        options=options(DocumentFormat.FUNCTIONAL, BackendPreference.PYTHON),
    )
    for format_value in formats:
        source = render_document(document, format=format_value)
        right_source = render_document(right_document, format=format_value)
        reference = load_snapshot(
            source,
            options=options(format_value, BackendPreference.PYTHON),
        )
        right_reference = load_snapshot(
            right_source,
            options=options(format_value, BackendPreference.PYTHON),
        )
        expected_roots = scalar_roots(reference)
        expected_mapped_roots = scalar_roots(right_reference)

        selected = forced_native_snapshot(source, format_value)
        right_selected = forced_native_snapshot(right_source, format_value)

        if type(selected).__name__ != "_NativeOntologySnapshot":
            raise AssertionError(f"{format_value.value} did not publish a native snapshot")
        if type(right_selected).__name__ != "_NativeOntologySnapshot":
            raise AssertionError(f"{format_value.value} did not publish the right native snapshot")
        handle = selected._native_snapshot_state.owner.handle
        raw_owner = object.__getattribute__(handle, "_owner_v2")
        right_handle = right_selected._native_snapshot_state.owner.handle
        right_raw_owner = object.__getattribute__(right_handle, "_owner_v2")
        before_native = raw_owner._publication_counters_v2()
        before_python = selected._native_python_counters()
        right_before_native = right_raw_owner._publication_counters_v2()
        right_before_python = right_selected._native_python_counters()
        ingestion = selected._native_ingestion_counters_v2()
        overlay = apply_delta(selected, OntologyDelta())

        scalar_error = AssertionError(
            f"{format_value.value} encoded publication crossed scalar traversal"
        )
        with (
            patch.object(type(selected), "ontology_annotations", side_effect=scalar_error),
            patch.object(type(selected), "iter_axioms", side_effect=scalar_error),
            patch.object(type(selected), "iter_extensions", side_effect=scalar_error),
            patch.object(type(selected), "signature", side_effect=scalar_error),
        ):
            direct = selected.view(EncodedStructuralView)
            right_direct = right_selected.view(EncodedStructuralView)
            overlay_encoded = overlay.view(EncodedStructuralView)

        direct_roots = decode_root_canonical_bytes(direct.buffers)
        right_direct_roots = decode_root_canonical_bytes(right_direct.buffers)
        overlay_decoded = independent_decoder.decode_segmented_root_canonical_bytes(
            overlay_encoded,
            expected_owner=overlay,
            expected_scope=AxiomScope.CLOSURE,
            expected_document_key=None,
        )
        overlay_roots = tuple((root.root_kind, root.canonical) for root in overlay_decoded.roots)
        after_segmented_native = raw_owner._publication_counters_v2()
        after_segmented_python = selected._native_python_counters()
        right_after_segmented_native = right_raw_owner._publication_counters_v2()
        right_after_segmented_python = right_selected._native_python_counters()

        scalar_composite = compose_views(
            reference,
            right_reference,
            roles=("left", "right"),
        )
        expected_composite = scalar_roots(scalar_composite)
        composite = compose_views(
            selected,
            right_selected,
            roles=("left", "right"),
        )
        before_composite_native = raw_owner._publication_counters_v2()
        before_composite_python = selected._native_python_counters()
        right_before_composite_native = right_raw_owner._publication_counters_v2()
        right_before_composite_python = right_selected._native_python_counters()
        with (
            patch.object(type(selected), "ontology_annotations", side_effect=scalar_error),
            patch.object(type(selected), "iter_axioms", side_effect=scalar_error),
            patch.object(type(selected), "iter_extensions", side_effect=scalar_error),
            patch.object(type(selected), "signature", side_effect=scalar_error),
        ):
            composite_encoded = composite.view(EncodedStructuralView)
        composite_decoded = independent_decoder.decode_segmented_root_canonical_bytes(
            composite_encoded,
            expected_owner=composite,
            expected_scope=AxiomScope.CLOSURE,
            expected_document_key=None,
        )
        composite_roots = tuple(
            (root.root_kind, root.canonical) for root in composite_decoded.roots
        )
        after_composite_native = raw_owner._publication_counters_v2()
        after_composite_python = selected._native_python_counters()
        right_after_composite_native = right_raw_owner._publication_counters_v2()
        right_after_composite_python = right_selected._native_python_counters()
        composite_owner_identity = composite_encoded.owner is composite
        composite_zero_copy = (
            composite_decoded.proof.scalar_traversal_calls == 0
            and composite_decoded.proof.referenced_buffer_copy_bytes == 0
        )

        reference_wire = encode_snapshot(reference)
        selected_wire = encode_snapshot(selected)
        right_reference_wire = encode_snapshot(right_reference)
        right_selected_wire = encode_snapshot(right_selected)
        decoded = decode_snapshot(selected_wire)
        decoded_encoded = decoded.view(EncodedStructuralView)
        decoded_roots = decode_root_canonical_bytes(decoded_encoded.buffers)

        with tempfile.TemporaryDirectory(
            prefix=f"pyowl-core-{format_value.value}-view-matrix-"
        ) as temporary:
            wire_path = Path(temporary) / "snapshot.pyocore"
            wire_path.write_bytes(selected_wire)
            mapped = open_snapshot(wire_path, mmap=True, verify=True)
            mapped_encoded = mapped.view(EncodedStructuralView)
            mapped_roots = decode_root_canonical_bytes(mapped_encoded.buffers)

            mapped_one_exporter = (
                len({id(value.obj) for value in mapped_encoded.buffers.values()}) == 1
            )
            mapped_readonly = all(value.readonly for value in mapped_encoded.buffers.values())
            mapped_owner_identity = mapped_encoded.owner is mapped

            del mapped_encoded
            gc.collect()
            cast(Any, mapped).close()

        no_segmented_scalar_work = (
            after_segmented_native.page_requests == before_native.page_requests
            and after_segmented_native.rows_emitted == before_native.rows_emitted
            and after_segmented_python.model_rows_materialized
            == before_python.model_rows_materialized
            and right_after_segmented_native.page_requests == right_before_native.page_requests
            and right_after_segmented_native.rows_emitted == right_before_native.rows_emitted
            and right_after_segmented_python.model_rows_materialized
            == right_before_python.model_rows_materialized
            and overlay_decoded.proof.scalar_traversal_calls == 0
            and overlay_decoded.proof.referenced_buffer_copy_bytes == 0
        )
        no_composite_scalar_work = (
            after_composite_native.page_requests == before_composite_native.page_requests
            and after_composite_native.rows_emitted == before_composite_native.rows_emitted
            and after_composite_python.model_rows_materialized
            == before_composite_python.model_rows_materialized
            and right_after_composite_native.page_requests
            == right_before_composite_native.page_requests
            and right_after_composite_native.rows_emitted
            == right_before_composite_native.rows_emitted
            and right_after_composite_python.model_rows_materialized
            == right_before_composite_python.model_rows_materialized
        )
        fingerprint_parity = (
            selected.structural_fingerprint == reference.structural_fingerprint
            and selected.logical_fingerprint == reference.logical_fingerprint
            and selected.signature_fingerprint == reference.signature_fingerprint
        )
        source_map_parity = tuple(item.source_map for item in selected.documents) == tuple(
            item.source_map for item in reference.documents
        )

        observed[format_value.value] = {
            "composite_model_row_deltas": [
                after_composite_python.model_rows_materialized
                - before_composite_python.model_rows_materialized,
                right_after_composite_python.model_rows_materialized
                - right_before_composite_python.model_rows_materialized,
            ],
            "composite_owner_identity": composite_owner_identity,
            "composite_page_request_deltas": [
                after_composite_native.page_requests - before_composite_native.page_requests,
                right_after_composite_native.page_requests
                - right_before_composite_native.page_requests,
            ],
            "composite_root_parity": composite_roots == expected_composite,
            "composite_rows_emitted_deltas": [
                after_composite_native.rows_emitted - before_composite_native.rows_emitted,
                right_after_composite_native.rows_emitted
                - right_before_composite_native.rows_emitted,
            ],
            "composite_zero_copy": composite_zero_copy,
            "decoded_owner_identity": decoded_encoded.owner is decoded,
            "decoded_root_parity": decoded_roots == expected_roots,
            "direct_owner_identity": direct.owner is selected,
            "direct_root_parity": direct_roots == expected_roots,
            "eager_structural_objects": ingestion.eager_structural_objects_materialized,
            "fingerprint_parity": fingerprint_parity,
            "mapped_one_exporter": mapped_one_exporter,
            "mapped_owner_identity": mapped_owner_identity,
            "mapped_readonly": mapped_readonly,
            "mapped_root_parity": mapped_roots == expected_roots,
            "no_composite_scalar_work": no_composite_scalar_work,
            "no_segmented_scalar_work": no_segmented_scalar_work,
            "overlay_owner_identity": overlay_encoded.owner is overlay,
            "overlay_root_parity": overlay_roots == expected_roots,
            "parser_bytes": before_native.parser_bytes,
            "publication_structural_bytes_copied": (
                after_composite_native.publication_structural_bytes_copied
            ),
            "publication_structural_rows_copied": (
                after_composite_native.publication_structural_rows_copied
            ),
            "source_bytes": len(source),
            "source_map_parity": source_map_parity,
            "right_direct_owner_identity": right_direct.owner is right_selected,
            "right_direct_root_parity": right_direct_roots == expected_mapped_roots,
            "right_wire_parity": right_selected_wire == right_reference_wire,
            "wire_parity": selected_wire == reference_wire,
        }

        del (
            direct,
            right_direct,
            overlay_encoded,
            overlay_decoded,
            overlay,
            composite_encoded,
            composite_decoded,
            composite,
            decoded_encoded,
        )
        gc.collect()
        selected.close()
        right_selected.close()

    print(json.dumps(observed, sort_keys=True))


if __name__ == "__main__":
    main()
