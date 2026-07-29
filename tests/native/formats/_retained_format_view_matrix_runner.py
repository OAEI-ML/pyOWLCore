from __future__ import annotations

import gc
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from tests.native.foundation._support import load_extension


def main() -> None:
    extension = load_extension()

    from pyowl_core import (
        IRI,
        AxiomScope,
        BackendPreference,
        BackendProtocolError,
        CancellationSource,
        DocumentFormat,
        EncodedStructuralView,
        ImportPolicy,
        LoadOptions,
        MappingResolver,
        OntologyDelta,
        OntologySyntaxError,
        OperationCancelledError,
        ParseLimits,
        ResolvedDocument,
        ResourceLimitError,
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
    from pyowl_core.backends.native_views import (
        ENCODED_STRUCTURAL_SCHEMA_NAME_V1,
        ENCODED_STRUCTURAL_SCHEMA_VERSION_V1,
    )
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
    expected_ingestion_features = tuple(
        sorted(
            {
                "parse-functional-v1",
                "parse-owlxml-v1",
                "parse-rdfxml-v1",
                "parse-turtle-v1",
            }
        )
    )
    if expected_ingestion_features != extension.INGESTION_FEATURES:
        raise AssertionError("installed native ingestion capability partition is incomplete")
    if not set(expected_ingestion_features).issubset(probe.features):
        raise AssertionError("installed native feature ledger omits an ingestion capability")
    expected_view_features = (ENCODED_STRUCTURAL_SCHEMA_NAME_V1,)
    expected_view_schemas = {
        ENCODED_STRUCTURAL_SCHEMA_NAME_V1: ENCODED_STRUCTURAL_SCHEMA_VERSION_V1,
    }
    if expected_view_features != extension.VIEW_FEATURES:
        raise AssertionError("installed native view capability partition is incomplete")
    if not set(expected_view_features).issubset(probe.features):
        raise AssertionError("installed native feature ledger omits the encoded view")

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
        *,
        load_options: LoadOptions | None = None,
        cancellation_token: object | None = None,
        document_iri: object | None = None,
        resolver: object | None = None,
    ) -> Any:
        unexpected = AssertionError(
            f"{format_value.value} forced-native matrix crossed a Python parser"
        )
        with (
            patch(
                "pyowl_core.backends.python.parser.parse_functional",
                side_effect=unexpected,
            ),
            patch(
                "pyowl_core.backends.python.parser.parse_rdfxml",
                side_effect=unexpected,
            ),
            patch(
                "pyowl_core.backends.python.parser.parse_turtle",
                side_effect=unexpected,
            ),
            patch(
                "pyowl_core.backends.python.parser.parse_owlxml",
                side_effect=unexpected,
            ),
        ):
            return load_snapshot(
                source,
                document_iri=cast(Any, document_iri),
                options=(
                    options(format_value, BackendPreference.NATIVE)
                    if load_options is None
                    else load_options
                ),
                cancellation_token=cast(Any, cancellation_token),
                resolver=cast(Any, resolver),
            )

    def forced_native_document(
        source: bytes,
        format_value: DocumentFormat,
        *,
        load_options: LoadOptions | None = None,
        document_iri: object | None = None,
    ) -> Any:
        unexpected = AssertionError(
            f"{format_value.value} forced-native document matrix crossed a Python parser"
        )
        with (
            patch(
                "pyowl_core.backends.python.parser.parse_functional",
                side_effect=unexpected,
            ),
            patch(
                "pyowl_core.backends.python.parser.parse_rdfxml",
                side_effect=unexpected,
            ),
            patch(
                "pyowl_core.backends.python.parser.parse_turtle",
                side_effect=unexpected,
            ),
            patch(
                "pyowl_core.backends.python.parser.parse_owlxml",
                side_effect=unexpected,
            ),
        ):
            return parse_document(
                source,
                document_iri=cast(Any, document_iri),
                options=(
                    options(format_value, BackendPreference.NATIVE)
                    if load_options is None
                    else load_options
                ),
            )

    observed: dict[str, object] = {}
    malformed_sources = {
        DocumentFormat.FUNCTIONAL: b"Ontology(Declaration(Class(<urn:broken>))",
        DocumentFormat.RDF_XML: (
            b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
            b'xmlns:owl="http://www.w3.org/2002/07/owl#"><owl:Ontology></rdf:RDF>'
        ),
        DocumentFormat.TURTLE: b"@prefix ex: <urn:broken:> . ex:A ex:p",
        DocumentFormat.OWL_XML: (
            b'<Ontology xmlns="http://www.w3.org/2002/07/owl#"><Declaration></Ontology>'
        ),
    }
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

        try:
            forced_native_snapshot(malformed_sources[format_value], format_value)
        except OntologySyntaxError as error:
            syntax_error_code = error.code
        else:
            raise AssertionError(f"{format_value.value} malformed source did not fail")
        try:
            forced_native_document(malformed_sources[format_value], format_value)
        except OntologySyntaxError as error:
            document_syntax_error_code = error.code
        else:
            raise AssertionError(f"{format_value.value} malformed document source did not fail")

        limited_options = replace(
            options(format_value, BackendPreference.NATIVE),
            limits=ParseLimits(max_axioms=1),
        )
        try:
            forced_native_snapshot(
                source,
                format_value,
                load_options=limited_options,
            )
        except ResourceLimitError as error:
            limit_error_code = error.code
        else:
            raise AssertionError(f"{format_value.value} axiom limit did not fail")
        try:
            forced_native_document(
                source,
                format_value,
                load_options=limited_options,
            )
        except ResourceLimitError as error:
            document_limit_error_code = error.code
        else:
            raise AssertionError(f"{format_value.value} document axiom limit did not fail")

        cancellation = CancellationSource()
        cancellation.cancel(f"{format_value.value} matrix cancellation")
        try:
            forced_native_snapshot(
                source,
                format_value,
                cancellation_token=cancellation.token,
            )
        except OperationCancelledError as error:
            cancellation_error_code = error.code
        else:
            raise AssertionError(f"{format_value.value} cancellation did not fail")
        selected = forced_native_snapshot(source, format_value)
        right_selected = forced_native_snapshot(right_source, format_value)

        if type(selected).__name__ != "_NativeOntologySnapshot":
            raise AssertionError(f"{format_value.value} did not publish a native snapshot")
        if type(right_selected).__name__ != "_NativeOntologySnapshot":
            raise AssertionError(f"{format_value.value} did not publish the right native snapshot")
        writer_parity = all(
            render_document(selected.root, format=output_format)
            == render_document(reference.root, format=output_format)
            for output_format in formats
        )
        for candidate in (reference, right_reference, selected, right_selected):
            if dict(candidate.capabilities.encoded_view_schemas) != expected_view_schemas:
                raise AssertionError(
                    f"{format_value.value} owner omitted the encoded-view capability"
                )
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
        try:
            independent_decoder.decode_segmented_root_canonical_bytes(
                replace(direct, descriptor=b"hostile"),
                expected_owner=selected,
                expected_scope=AxiomScope.CLOSURE,
                expected_document_key=None,
            )
        except BackendProtocolError as error:
            hostile_descriptor_code = error.code
        else:
            raise AssertionError(f"{format_value.value} hostile encoded descriptor did not fail")
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
            if dict(mapped.capabilities.encoded_view_schemas) != expected_view_schemas:
                raise AssertionError(
                    f"{format_value.value} mmap owner omitted the encoded-view capability"
                )
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
        if any(
            dict(candidate.capabilities.encoded_view_schemas) != expected_view_schemas
            for candidate in (overlay, scalar_composite, composite, decoded)
        ):
            raise AssertionError(
                f"{format_value.value} derived owner omitted the encoded-view capability"
            )

        observed[format_value.value] = {
            "cancellation_error_code": cancellation_error_code,
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
            "document_limit_error_code": document_limit_error_code,
            "document_syntax_error_code": document_syntax_error_code,
            "eager_structural_objects": ingestion.eager_structural_objects_materialized,
            "fingerprint_parity": fingerprint_parity,
            "hostile_descriptor_code": hostile_descriptor_code,
            "limit_error_code": limit_error_code,
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
            "syntax_error_code": syntax_error_code,
            "right_direct_owner_identity": right_direct.owner is right_selected,
            "right_direct_root_parity": right_direct_roots == expected_mapped_roots,
            "right_wire_parity": right_selected_wire == right_reference_wire,
            "wire_parity": selected_wire == reference_wire,
            "writer_parity": writer_parity,
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

    option_root_document = parse_document(
        b"Ontology(<urn:format-option-matrix:root> "
        b"Import(<urn:format-option-matrix:child>) "
        b"Declaration(Class(<urn:format-option-matrix:Root>)))",
        options=options(DocumentFormat.FUNCTIONAL, BackendPreference.PYTHON),
    )
    option_child_document = parse_document(
        b"Ontology(<urn:format-option-matrix:child> "
        b"Declaration(Class(<urn:format-option-matrix:Child>)) "
        b"SubClassOf(<urn:format-option-matrix:Child> "
        b"<urn:format-option-matrix:Root>))",
        options=options(DocumentFormat.FUNCTIONAL, BackendPreference.PYTHON),
    )
    option_pairs = ((False, False), (True, False), (False, True), (True, True))
    import_scenarios = (
        (ImportPolicy.IGNORE, False),
        (ImportPolicy.RECORD_UNRESOLVED, False),
        (ImportPolicy.RESOLVE_LOCAL, True),
        (ImportPolicy.RESOLVE_STRICT, True),
    )

    def option_resolver(
        child_source: bytes,
        format_value: DocumentFormat,
    ) -> MappingResolver:
        return MappingResolver(
            {
                "urn:format-option-matrix:child": ResolvedDocument(
                    child_source,
                    IRI(f"urn:format-option-matrix:child-source:{format_value.value}"),
                    format=format_value,
                )
            }
        )

    def option_load_options(
        format_value: DocumentFormat,
        import_policy: ImportPolicy,
        backend: BackendPreference,
        collect_provenance: bool,
        preserve_source_map: bool,
    ) -> LoadOptions:
        return LoadOptions(
            format=format_value,
            imports=import_policy,
            backend=backend,
            collect_provenance=collect_provenance,
            preserve_source_map=preserve_source_map,
        )

    option_matrix: dict[str, dict[str, object]] = {}
    for format_value in formats:
        root_source = render_document(option_root_document, format=format_value)
        child_source = render_document(option_child_document, format=format_value)
        source_iri = IRI(f"urn:format-option-matrix:source:{format_value.value}")

        cases = 0
        document_cases = 0
        document_zero_copy_cases = 0
        native_documents = 0
        native_snapshots = 0
        resolved_cases = 0
        wire_parity_cases = 0
        zero_copy_cases = 0
        for import_policy, resolve_import in import_scenarios:
            for collect_provenance, preserve_source_map in option_pairs:
                reference_document = parse_document(
                    root_source,
                    document_iri=source_iri,
                    options=option_load_options(
                        format_value,
                        import_policy,
                        BackendPreference.PYTHON,
                        collect_provenance,
                        preserve_source_map,
                    ),
                )
                selected_document = forced_native_document(
                    root_source,
                    format_value,
                    load_options=option_load_options(
                        format_value,
                        import_policy,
                        BackendPreference.NATIVE,
                        collect_provenance,
                        preserve_source_map,
                    ),
                    document_iri=source_iri,
                )
                try:
                    document_cases += 1
                    if type(selected_document).__name__ != "_NativeOntologyDocument":
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} "
                            "option case did not publish a native document"
                        )
                    native_documents += 1
                    document_owner = object.__getattribute__(
                        selected_document._native_document_state.owner.handle,
                        "_owner_v2",
                    )
                    document_counters = document_owner._publication_counters_v2()
                    if document_counters.parser_bytes != len(root_source):
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} "
                            "document did not account for the exact parsed bytes"
                        )
                    if (document_counters.retained_origin_rows > 0) is not collect_provenance:
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} "
                            "document provenance retention did not follow the option"
                        )
                    if (document_counters.retained_source_map_rows > 0) is not preserve_source_map:
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} "
                            "document source-map retention did not follow the option"
                        )
                    if (
                        document_counters.publication_structural_rows_copied != 0
                        or document_counters.publication_structural_bytes_copied != 0
                    ):
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} "
                            "document performed forbidden publication copies"
                        )
                    document_zero_copy_cases += 1
                    if selected_document != reference_document:
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} document structure differs"
                        )
                    if (
                        selected_document.document_fingerprint
                        != reference_document.document_fingerprint
                        or selected_document.direct_imports != reference_document.direct_imports
                        or selected_document.source_map != reference_document.source_map
                        or selected_document.origin_index != reference_document.origin_index
                        or selected_document.rdf_mapping_report
                        != reference_document.rdf_mapping_report
                        or selected_document.diagnostics != reference_document.diagnostics
                    ):
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} document metadata differs"
                        )
                    if selected_document.direct_imports != (IRI("urn:format-option-matrix:child"),):
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} "
                            "parse_document did not preserve its direct import"
                        )
                    if (
                        selected_document.provenance.backend != "native"
                        or selected_document.provenance.parser != "pyowl_core.backends.native"
                    ):
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} "
                            "document did not retain native provenance"
                        )
                finally:
                    selected_document.close()

                reference = load_snapshot(
                    root_source,
                    document_iri=source_iri,
                    options=option_load_options(
                        format_value,
                        import_policy,
                        BackendPreference.PYTHON,
                        collect_provenance,
                        preserve_source_map,
                    ),
                    resolver=(
                        option_resolver(child_source, format_value) if resolve_import else None
                    ),
                )
                selected = forced_native_snapshot(
                    root_source,
                    format_value,
                    load_options=option_load_options(
                        format_value,
                        import_policy,
                        BackendPreference.NATIVE,
                        collect_provenance,
                        preserve_source_map,
                    ),
                    document_iri=source_iri,
                    resolver=(
                        option_resolver(child_source, format_value) if resolve_import else None
                    ),
                )
                try:
                    cases += 1
                    if type(selected).__name__ != "_NativeOntologySnapshot":
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} "
                            "option case did not publish a native snapshot"
                        )
                    native_snapshots += 1
                    owner = object.__getattribute__(
                        selected._native_snapshot_state.owner.handle,
                        "_owner_v2",
                    )
                    counters = owner._publication_counters_v2()
                    ingestion = selected._native_ingestion_counters_v2()
                    expected_document_count = 2 if resolve_import else 1
                    expected_parser_bytes = len(root_source) + (
                        len(child_source) if resolve_import else 0
                    )
                    if len(selected.documents) != expected_document_count:
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} "
                            "published the wrong document count"
                        )
                    if not all(
                        type(item).__name__ == "_NativeOntologyDocument"
                        for item in selected.documents
                    ):
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} "
                            "published a non-native closure member"
                        )
                    if counters.parser_bytes != expected_parser_bytes:
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} "
                            "did not account for the exact parsed bytes"
                        )
                    if (counters.retained_origin_rows > 0) is not collect_provenance:
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} "
                            "provenance retention did not follow the option"
                        )
                    if (counters.retained_source_map_rows > 0) is not preserve_source_map:
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} "
                            "source-map retention did not follow the option"
                        )
                    if (
                        counters.publication_structural_rows_copied != 0
                        or counters.publication_structural_bytes_copied != 0
                        or ingestion.parser_result_bytes_scanned != 0
                        or ingestion.eager_structural_objects_materialized != 0
                    ):
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} "
                            "performed forbidden publication work"
                        )
                    zero_copy_cases += 1

                    if selected.capabilities.backend != "native":
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} "
                            "did not report the forced native backend"
                        )
                    if selected.import_manifest != reference.import_manifest:
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} import manifests differ"
                        )
                    if selected.diagnostics != reference.diagnostics:
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} "
                            "snapshot diagnostics differ"
                        )
                    if tuple(item.diagnostics for item in selected.documents) != tuple(
                        item.diagnostics for item in reference.documents
                    ):
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} "
                            "document diagnostics differ"
                        )
                    if tuple(item.source_map for item in selected.documents) != tuple(
                        item.source_map for item in reference.documents
                    ):
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} source maps differ"
                        )
                    if tuple(item.rdf_mapping_report for item in selected.documents) != tuple(
                        item.rdf_mapping_report for item in reference.documents
                    ):
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} RDF mapping reports differ"
                        )
                    if selected.origin_index != reference.origin_index:
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} origin indexes differ"
                        )
                    if (
                        selected.structural_fingerprint != reference.structural_fingerprint
                        or selected.logical_fingerprint != reference.logical_fingerprint
                        or selected.signature_fingerprint != reference.signature_fingerprint
                    ):
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} fingerprints differ"
                        )
                    if encode_snapshot(selected) != encode_snapshot(reference):
                        raise AssertionError(
                            f"{format_value.value} {import_policy.value} wire bytes differ"
                        )
                    wire_parity_cases += 1
                    resolved_cases += int(resolve_import)
                finally:
                    selected.close()

        option_matrix[format_value.value] = {
            "cases": cases,
            "document_cases": document_cases,
            "document_zero_copy_cases": document_zero_copy_cases,
            "import_policies": [policy.value for policy, _ in import_scenarios],
            "native_documents": native_documents,
            "native_snapshots": native_snapshots,
            "option_pairs": [
                {
                    "collect_provenance": collect_provenance,
                    "preserve_source_map": preserve_source_map,
                }
                for collect_provenance, preserve_source_map in option_pairs
            ],
            "resolved_cases": resolved_cases,
            "wire_parity_cases": wire_parity_cases,
            "zero_copy_cases": zero_copy_cases,
        }
    observed["option_matrix"] = option_matrix

    observed["capabilities"] = {
        "ingestion_features": list(extension.INGESTION_FEATURES),
        "probe_contains_ingestion_partition": set(extension.INGESTION_FEATURES).issubset(
            probe.features
        ),
        "encoded_view_schemas": expected_view_schemas,
        "probe_contains_view_partition": set(extension.VIEW_FEATURES).issubset(probe.features),
        "view_features": list(extension.VIEW_FEATURES),
    }

    mixed_root = b"""\
<Ontology xmlns="http://www.w3.org/2002/07/owl#" ontologyIRI="urn:matrix:mixed-root">
  <Import>urn:matrix:child:functional</Import>
  <Import>urn:matrix:child:rdfxml</Import>
  <Import>urn:matrix:child:turtle</Import>
  <Declaration><Class IRI="urn:matrix:Root"/></Declaration>
</Ontology>
"""
    mixed_children = {
        "urn:matrix:child:functional": ResolvedDocument(
            b"""\
Ontology(<urn:matrix:child:functional>
  Declaration(Class(<urn:matrix:FunctionalChild>))
  SubClassOf(<urn:matrix:FunctionalChild> <urn:matrix:Root>)
)
""",
            IRI("urn:matrix:source:functional"),
            format=DocumentFormat.FUNCTIONAL,
        ),
        "urn:matrix:child:rdfxml": ResolvedDocument(
            b"""\
<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
    xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="urn:matrix:child:rdfxml"/>
  <owl:Class rdf:about="urn:matrix:RdfXmlChild">
    <rdfs:subClassOf rdf:resource="urn:matrix:Root"/>
  </owl:Class>
</rdf:RDF>
""",
            IRI("urn:matrix:source:rdfxml"),
            format=DocumentFormat.RDF_XML,
        ),
        "urn:matrix:child:turtle": ResolvedDocument(
            b"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<urn:matrix:child:turtle> a owl:Ontology .
<urn:matrix:TurtleChild> a owl:Class ;
    rdfs:subClassOf <urn:matrix:Root> .
""",
            IRI("urn:matrix:source:turtle"),
            format=DocumentFormat.TURTLE,
        ),
    }
    mixed_options = LoadOptions(
        format=DocumentFormat.OWL_XML,
        imports=ImportPolicy.RESOLVE_STRICT,
        backend=BackendPreference.PYTHON,
        collect_provenance=True,
        preserve_source_map=True,
    )
    reference_mixed = load_snapshot(
        mixed_root,
        document_iri=IRI("urn:matrix:source:root"),
        options=mixed_options,
        resolver=MappingResolver(cast(Any, mixed_children)),
    )
    selected_mixed = forced_native_snapshot(
        mixed_root,
        DocumentFormat.OWL_XML,
        load_options=replace(mixed_options, backend=BackendPreference.NATIVE),
        document_iri=IRI("urn:matrix:source:root"),
        resolver=MappingResolver(cast(Any, mixed_children)),
    )
    try:
        mixed_owner = object.__getattribute__(
            selected_mixed._native_snapshot_state.owner.handle,
            "_owner_v2",
        )
        mixed_before_native = mixed_owner._publication_counters_v2()
        mixed_before_python = selected_mixed._native_python_counters()
        scalar_error = AssertionError("mixed-format encoded publication crossed scalar traversal")
        with (
            patch.object(
                type(selected_mixed),
                "ontology_annotations",
                side_effect=scalar_error,
            ),
            patch.object(type(selected_mixed), "iter_axioms", side_effect=scalar_error),
            patch.object(
                type(selected_mixed),
                "iter_extensions",
                side_effect=scalar_error,
            ),
            patch.object(type(selected_mixed), "signature", side_effect=scalar_error),
        ):
            mixed_encoded = selected_mixed.view(EncodedStructuralView)
        mixed_decoded = independent_decoder.decode_segmented_root_canonical_bytes(
            mixed_encoded,
            expected_owner=selected_mixed,
            expected_scope=AxiomScope.CLOSURE,
            expected_document_key=None,
        )
        mixed_after_native = mixed_owner._publication_counters_v2()
        mixed_after_python = selected_mixed._native_python_counters()
        expected_mixed_roots = scalar_roots(reference_mixed)
        observed_mixed_roots = tuple(
            (root.root_kind, root.canonical) for root in mixed_decoded.roots
        )
        selected_formats = tuple(
            sorted(document.provenance.format.value for document in selected_mixed.documents)
        )
        expected_formats = tuple(sorted(format_value.value for format_value in formats))
        observed["mixed_closure"] = {
            "all_documents_native": all(
                type(document).__name__ == "_NativeOntologyDocument"
                for document in selected_mixed.documents
            ),
            "document_count": len(selected_mixed.documents),
            "encoded_owner_identity": mixed_encoded.owner is selected_mixed,
            "encoded_root_parity": observed_mixed_roots == expected_mixed_roots,
            "fingerprint_parity": (
                selected_mixed.structural_fingerprint == reference_mixed.structural_fingerprint
                and selected_mixed.logical_fingerprint == reference_mixed.logical_fingerprint
                and selected_mixed.signature_fingerprint == reference_mixed.signature_fingerprint
            ),
            "format_coverage": selected_formats == expected_formats,
            "manifest_parity": selected_mixed.import_manifest == reference_mixed.import_manifest,
            "model_row_delta": (
                mixed_after_python.model_rows_materialized
                - mixed_before_python.model_rows_materialized
            ),
            "origin_parity": selected_mixed.origin_index == reference_mixed.origin_index,
            "page_request_delta": (
                mixed_after_native.page_requests - mixed_before_native.page_requests
            ),
            "parser_bytes": mixed_after_native.parser_bytes,
            "publication_structural_bytes_copied": (
                mixed_after_native.publication_structural_bytes_copied
            ),
            "publication_structural_rows_copied": (
                mixed_after_native.publication_structural_rows_copied
            ),
            "referenced_buffer_copy_bytes": (mixed_decoded.proof.referenced_buffer_copy_bytes),
            "rows_emitted_delta": (
                mixed_after_native.rows_emitted - mixed_before_native.rows_emitted
            ),
            "scalar_traversal_calls": mixed_decoded.proof.scalar_traversal_calls,
            "source_map_parity": tuple(document.source_map for document in selected_mixed.documents)
            == tuple(document.source_map for document in reference_mixed.documents),
            "source_bytes": len(mixed_root)
            + sum(len(cast(bytes, document.source)) for document in mixed_children.values()),
            "wire_parity": encode_snapshot(selected_mixed) == encode_snapshot(reference_mixed),
        }
    finally:
        selected_mixed.close()

    print(json.dumps(observed, sort_keys=True))


if __name__ == "__main__":
    main()
