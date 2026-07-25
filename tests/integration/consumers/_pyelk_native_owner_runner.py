from __future__ import annotations

import gc
import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import replace
from inspect import getattr_static
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from unittest.mock import PropertyMock, patch

from tests.native.foundation._support import load_extension


def _load_pyelk_extension(path: Path) -> ModuleType:
    importlib.import_module("pyelk")

    destination = Path(tempfile.mkdtemp(prefix="pyowl-core-pyelk-native-")) / "_native.so"
    shutil.copy2(path.resolve(), destination)
    spec = importlib.util.spec_from_file_location("pyelk._native", destination)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load pyELK native artifact {destination}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pyelk._native"] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    core_extension = load_extension()
    from pyowl_core import (
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
        render_document,
    )
    from pyowl_core.backends import native

    native._reset_probe_cache_for_tests()
    probe = native.probe(refresh=True)
    if not probe.available or "parse-functional-v1" not in probe.features:
        raise RuntimeError(probe.reason or "native Functional parser is unavailable")
    if not hasattr(core_extension, "_parse_rdfxml_retained_v2"):
        raise RuntimeError("native artifact lacks the guarded retained RDF/XML parser")
    pyelk_native = _load_pyelk_extension(Path(os.environ["PYOWL_CORE_TEST_PYELK_NATIVE_LIBRARY"]))
    from pyelk import Reasoner, ReasonerConfig  # type: ignore[import-not-found]
    from pyelk.indexing.encoded import (  # type: ignore[import-not-found]
        ENCODED_SCHEMA_NAME,
        ENCODED_SCHEMA_VERSION,
    )

    if cast(Any, pyelk_native).encoded_view_schemas():
        raise AssertionError("pyELK fixture advertised an unfinished encoded input capability")

    functional_source = b"""Prefix(:=<urn:core-pyelk#>) Ontology(<urn:core-pyelk>
      Declaration(Class(:A))
      Declaration(Class(:B))
      Declaration(ObjectProperty(:p))
      SubClassOf(:A :B)
      SubClassOf(:A ObjectSomeValuesFrom(:p :B))
      ObjectPropertyRange(:p :B)
    )"""
    right_functional_source = b"""Prefix(:=<urn:core-pyelk#>)
    Ontology(<urn:core-pyelk-right>
      Declaration(Class(:B))
      Declaration(Class(:C))
      SubClassOf(:B :C)
    )"""

    def options(
        format: DocumentFormat,
        backend: BackendPreference,
    ) -> LoadOptions:
        return LoadOptions(
            format=format,
            imports=ImportPolicy.IGNORE,
            backend=backend,
            collect_provenance=True,
        )

    def as_rdfxml(source: bytes) -> bytes:
        seed = load_snapshot(
            source,
            options=options(DocumentFormat.FUNCTIONAL, BackendPreference.PYTHON),
        )
        try:
            return render_document(seed.root, format=DocumentFormat.RDF_XML)
        finally:
            close_seed = getattr(seed, "close", None)
            if callable(close_seed):
                close_seed()

    rdfxml_source = as_rdfxml(functional_source)
    right_rdfxml_source = as_rdfxml(right_functional_source)

    def exercise(
        source: bytes,
        format: DocumentFormat,
        *,
        guarded_candidate: bool,
    ) -> dict[str, object]:
        reference = load_snapshot(
            source,
            options=options(format, BackendPreference.PYTHON),
        )
        expected = Reasoner(
            reference,
            ReasonerConfig(backend="python", unsupported="error"),
        )
        with ExitStack() as stack:
            if guarded_candidate:
                stack.enter_context(
                    patch(
                        "pyowl_core.backends.parser._NativeBackendDriver.select",
                        autospec=True,
                        return_value="native",
                    )
                )
                stack.enter_context(
                    patch(
                        "pyowl_core.backends.python.parser.parse_rdfxml",
                        side_effect=AssertionError(
                            "guarded RDF/XML pyELK handoff crossed the Python parser"
                        ),
                    )
                )
            selected = load_snapshot(
                source,
                options=options(format, BackendPreference.NATIVE),
            )
        try:
            if selected.capabilities.backend != "native":
                raise AssertionError("public forced-native load did not retain the V2 owner")
            if selected.capabilities.encoded_view_schemas:
                raise AssertionError(
                    "retained-load fixture advertised an unfinished view capability"
                )
            if (
                selected.structural_fingerprint != reference.structural_fingerprint
                or selected.logical_fingerprint != reference.logical_fingerprint
                or selected.signature_fingerprint != reference.signature_fingerprint
            ):
                raise AssertionError("retained public load fingerprint parity failed")

            handle = cast(Any, selected)._native_snapshot_state.owner.handle
            raw_owner = object.__getattribute__(handle, "_owner_v2")
            if type(raw_owner) is not cast(Any, core_extension)._NativeSnapshotHandle:
                raise AssertionError("public load did not retain the exact Rust snapshot owner")
            before = cast(Any, raw_owner)._publication_counters_v2()
            state = cast(Any, selected)._native_snapshot_state
            advertised = replace(
                state.capabilities,
                encoded_view_schemas={ENCODED_SCHEMA_NAME: ENCODED_SCHEMA_VERSION},
            )
            scalar_error = AssertionError("pyELK handoff crossed scalar ontology traversal")
            with (
                ExitStack() as reasoner_stack,
                patch.object(state, "capabilities", advertised),
                patch.object(
                    pyelk_native,
                    "encoded_view_schemas",
                    return_value={ENCODED_SCHEMA_NAME: ENCODED_SCHEMA_VERSION},
                ),
                patch.object(type(selected), "iter_axioms", side_effect=scalar_error),
                patch.object(type(selected), "iter_extensions", side_effect=scalar_error),
                patch.object(type(selected), "ontology_annotations", side_effect=scalar_error),
                patch.object(type(selected), "signature", side_effect=scalar_error),
                patch(
                    "pyelk.api._compile_ontology_with_materialization_count",
                    side_effect=scalar_error,
                ),
            ):
                actual = reasoner_stack.enter_context(
                    Reasoner(
                        selected,
                        ReasonerConfig(backend="rust", workers=1, unsupported="error"),
                    )
                )
                if actual.ontology is not selected:
                    raise AssertionError("public pyELK facade changed the retained owner identity")
                session = cast(Any, actual)._session
                handoff = session._encoded_owner
                if handoff is None:
                    raise AssertionError("public pyELK facade did not retain its encoded handoff")
                encoded = handoff.encoded_view
                if not isinstance(encoded, EncodedStructuralView):
                    raise AssertionError("public pyELK facade retained the wrong encoded view type")
                exporters = {id(value.obj) for value in encoded.buffers.values()}
                if handoff.owner is not selected or encoded.owner is not selected:
                    raise AssertionError("encoded handoff changed the exact public owner")
                if len(encoded.buffers) != 11 or len(exporters) != 1:
                    raise AssertionError("encoded handoff is not eleven slices over one exporter")
                if not all(type(value.obj) is bytes for value in encoded.buffers.values()):
                    raise AssertionError(
                        "encoded handoff exporter is not the direct immutable bytes owner"
                    )
                actual_results = (
                    actual.is_consistent(),
                    actual.classify(),
                    actual.classify_object_properties(),
                    actual.realize(),
                )
                expected_results = (
                    expected.is_consistent(),
                    expected.classify(),
                    expected.classify_object_properties(),
                    expected.realize(),
                )
                if actual_results != expected_results:
                    raise AssertionError("public pyELK encoded and scalar results diverge")

                after = cast(Any, raw_owner)._publication_counters_v2()
                if after.encoded_view_requests != before.encoded_view_requests + 1:
                    raise AssertionError(
                        "encoded handoff did not use exactly one native view export"
                    )
                if (
                    after.page_requests != before.page_requests
                    or after.rows_emitted != before.rows_emitted
                ):
                    raise AssertionError("encoded handoff materialized scalar facade rows")

                actual_diagnostics = actual.diagnostics()
                expected_diagnostics = expected.diagnostics()
                expected_digest = expected_diagnostics["compiler_digest"]
                if actual_diagnostics["compiler_digest"] != expected_digest:
                    raise AssertionError(
                        "public pyELK encoded compiler digest diverges from scalar compilation"
                    )
                if actual_diagnostics["ingestion_path"] != "encoded-native":
                    raise AssertionError("public pyELK facade did not select encoded-native")
                if actual_diagnostics["materialized_scalar_rows"] != 0:
                    raise AssertionError("public pyELK facade materialized scalar compiler rows")
                if actual_diagnostics["encoded_zero_copy_buffers"] != 11:
                    raise AssertionError("pyELK did not borrow all eleven direct buffers")
                if actual_diagnostics["encoded_detached_buffer_count"] != 11:
                    raise AssertionError("pyELK did not detach the shared exporter")
                if actual_diagnostics["encoded_staging_copy_bytes"] != 0:
                    raise AssertionError("pyELK staged the direct encoded handoff")
                result = {
                    "compiler_digest": expected_digest,
                    "encoded_buffers": len(encoded.buffers),
                    "encoded_exporters": len(exporters),
                    "parser_bytes": after.parser_bytes,
                    "public_operations": len(actual_results),
                    "scalar_facade_rows": after.rows_emitted - before.rows_emitted,
                }
            if selected.capabilities.encoded_view_schemas:
                raise AssertionError("test-local core capability leaked into the public snapshot")
            if cast(Any, pyelk_native).encoded_view_schemas():
                raise AssertionError("test-local pyELK capability leaked into the native module")
            return result
        finally:
            expected.close()
            for snapshot in (selected, reference):
                close_snapshot = getattr(snapshot, "close", None)
                if callable(close_snapshot):
                    close_snapshot()

    def exercise_owner(
        candidate: Any,
        expected_view: Any,
        *,
        native_bases: tuple[Any, ...],
        expected_segments: int,
        expected_references: int,
        expected_detached_buffers: int,
        expected_fingerprint_accesses: dict[str, int],
        expected_view_request_deltas: tuple[int, ...],
    ) -> dict[str, object]:
        if len(expected_view_request_deltas) != len(native_bases):
            raise AssertionError(
                "public pyELK owner matrix requires one export delta per native base"
            )
        expected = Reasoner(
            expected_view,
            ReasonerConfig(backend="python", unsupported="error"),
        )
        owner_evidence = []
        for base in native_bases:
            handle = base._native_snapshot_state.owner.handle
            raw_owner = object.__getattribute__(handle, "_owner_v2")
            owner_evidence.append(
                (
                    base,
                    raw_owner,
                    raw_owner._publication_counters_v2(),
                    base._native_python_counters(),
                    base._native_ingestion_counters_v2(),
                )
            )
        advertised = replace(
            candidate.capabilities,
            encoded_view_schemas={ENCODED_SCHEMA_NAME: ENCODED_SCHEMA_VERSION},
        )
        scalar_error = AssertionError("public pyELK owner matrix crossed scalar traversal")
        fingerprint_names = (
            "structural_fingerprint",
            "logical_fingerprint",
            "signature_fingerprint",
        )
        fingerprint_accesses = {name: 0 for name in fingerprint_names}
        result: dict[str, object]
        try:
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(
                        type(candidate),
                        "capabilities",
                        new_callable=PropertyMock,
                        return_value=advertised,
                    )
                )
                stack.enter_context(
                    patch.object(
                        pyelk_native,
                        "encoded_view_schemas",
                        return_value={ENCODED_SCHEMA_NAME: ENCODED_SCHEMA_VERSION},
                    )
                )
                scalar_view_types = {type(base) for base in native_bases}
                scalar_view_types.add(type(candidate))
                for view_type in scalar_view_types:
                    for method in (
                        "iter_axioms",
                        "iter_extensions",
                        "ontology_annotations",
                        "signature",
                    ):
                        stack.enter_context(
                            patch.object(view_type, method, side_effect=scalar_error)
                        )
                candidate_type = type(candidate)
                for name in fingerprint_names:
                    descriptor = getattr_static(candidate_type, name)
                    if not isinstance(descriptor, property):
                        raise AssertionError(f"public pyELK owner matrix cannot guard {name}")

                    def observe_fingerprint(
                        *,
                        field: str = name,
                        original: property = descriptor,
                    ) -> object:
                        fingerprint_accesses[field] += 1
                        return original.__get__(candidate, candidate_type)

                    stack.enter_context(
                        patch.object(
                            candidate_type,
                            name,
                            new_callable=PropertyMock,
                            side_effect=observe_fingerprint,
                        )
                    )
                stack.enter_context(
                    patch(
                        "pyelk.api._compile_ontology_with_materialization_count",
                        side_effect=scalar_error,
                    )
                )
                actual = stack.enter_context(
                    Reasoner(
                        candidate,
                        ReasonerConfig(backend="rust", workers=1, unsupported="error"),
                    )
                )
                if actual.ontology is not candidate:
                    raise AssertionError("public pyELK matrix changed the candidate identity")
                session = cast(Any, actual)._session
                handoff = session._encoded_owner
                if handoff is None or handoff.owner is not candidate:
                    raise AssertionError("public pyELK matrix lost its encoded owner")
                encoded = handoff.encoded_view
                if not isinstance(encoded, EncodedStructuralView) or encoded.owner is not candidate:
                    raise AssertionError("public pyELK matrix retained the wrong encoded view")
                if set(handoff.buffers) != set(encoded.buffers):
                    raise AssertionError("public pyELK matrix changed the encoded buffer mapping")
                for name, value in encoded.buffers.items():
                    negotiated = handoff.buffers[name]
                    if (
                        negotiated.nbytes != value.nbytes
                        or negotiated.readonly is not True
                        or negotiated.format != "B"
                        or negotiated.obj is not value.obj
                    ):
                        raise AssertionError(f"public pyELK matrix changed encoded buffer {name!r}")
                if handoff.buffer_count != len(encoded.buffers) or handoff.buffer_bytes != sum(
                    value.nbytes for value in encoded.buffers.values()
                ):
                    raise AssertionError(
                        "public pyELK matrix changed the negotiated top-level buffers"
                    )

                referenced_sources = tuple(
                    segment.source for segment in encoded.segments if segment.source is not None
                )
                if expected_references:
                    if len(referenced_sources) != expected_references:
                        raise AssertionError(
                            "public pyELK matrix exposed the wrong referenced source count"
                        )
                    if {id(source.owner) for source in referenced_sources} != {
                        id(base) for base in native_bases
                    }:
                        raise AssertionError(
                            "public pyELK matrix changed referenced source ownership"
                        )
                    buffer_sources = referenced_sources
                else:
                    if referenced_sources:
                        raise AssertionError(
                            "public pyELK matrix exposed a referenced direct source"
                        )
                    buffer_sources = (encoded,)
                expected_buffer_names = set(encoded.buffers)
                for source in buffer_sources:
                    if set(source.buffers) != expected_buffer_names:
                        raise AssertionError("public pyELK matrix changed a source buffer mapping")
                    if any(
                        value.readonly is not True or value.format != "B"
                        for value in source.buffers.values()
                    ):
                        raise AssertionError(
                            "public pyELK matrix exposed a mutable or non-byte source buffer"
                        )
                expected_buffer_count = sum(len(source.buffers) for source in buffer_sources)
                expected_buffer_bytes = sum(
                    value.nbytes for source in buffer_sources for value in source.buffers.values()
                )

                actual_results = (
                    actual.is_consistent(),
                    actual.classify(),
                    actual.classify_object_properties(),
                    actual.realize(),
                )
                expected_results = (
                    expected.is_consistent(),
                    expected.classify(),
                    expected.classify_object_properties(),
                    expected.realize(),
                )
                if actual_results != expected_results:
                    raise AssertionError("public pyELK owner-matrix results diverge")
                diagnostics = actual.diagnostics()
                expected_digest = expected.diagnostics()["compiler_digest"]
                if diagnostics["compiler_digest"] != expected_digest:
                    raise AssertionError("public pyELK owner-matrix compiler digest diverges")
                if diagnostics["ingestion_path"] != "encoded-native":
                    raise AssertionError("public pyELK owner matrix did not use encoded-native")
                if diagnostics["materialized_scalar_rows"] != 0:
                    raise AssertionError("public pyELK owner matrix materialized scalar rows")
                if diagnostics["encoded_segment_count"] != expected_segments:
                    raise AssertionError(
                        "public pyELK owner matrix used "
                        f"{diagnostics['encoded_segment_count']} segments, "
                        f"expected {expected_segments}"
                    )
                if diagnostics["encoded_referenced_view_count"] != expected_references:
                    raise AssertionError(
                        "public pyELK owner matrix retained "
                        f"{diagnostics['encoded_referenced_view_count']} referenced views, "
                        f"expected {expected_references}"
                    )
                if diagnostics["encoded_staging_copy_bytes"] != 0:
                    raise AssertionError("public pyELK owner matrix staged encoded buffers")
                if diagnostics["encoded_private_ir_bytes"] != 0:
                    raise AssertionError("public pyELK owner matrix retained private compiler IR")
                if diagnostics["encoded_buffer_count"] != expected_buffer_count:
                    raise AssertionError("public pyELK owner matrix changed the buffer count")
                if diagnostics["encoded_buffer_bytes"] != expected_buffer_bytes:
                    raise AssertionError("public pyELK owner matrix changed the buffer byte total")
                if diagnostics["encoded_zero_copy_buffers"] != expected_buffer_count:
                    raise AssertionError("public pyELK owner matrix copied encoded buffers")
                if diagnostics["encoded_detached_buffer_count"] != expected_detached_buffers:
                    raise AssertionError(
                        "public pyELK owner matrix changed detached exporter ownership"
                    )
                expected_indexed_buffers = expected_buffer_count - expected_detached_buffers
                if diagnostics["encoded_indexed_buffer_count"] != expected_indexed_buffers:
                    raise AssertionError(
                        "public pyELK owner matrix changed indexed exporter ownership"
                    )
                result = {
                    "compiler_digest": expected_digest,
                    "encoded_buffer_bytes": diagnostics["encoded_buffer_bytes"],
                    "encoded_buffers": expected_buffer_count,
                    "detached_buffers": diagnostics["encoded_detached_buffer_count"],
                    "indexed_buffers": diagnostics["encoded_indexed_buffer_count"],
                    "public_operations": len(actual_results),
                    "referenced_views": diagnostics["encoded_referenced_view_count"],
                    "segments": diagnostics["encoded_segment_count"],
                    "staging_copy_bytes": diagnostics["encoded_staging_copy_bytes"],
                    "zero_copy_buffers": diagnostics["encoded_zero_copy_buffers"],
                }
        finally:
            expected.close()

        if fingerprint_accesses != expected_fingerprint_accesses:
            raise AssertionError(
                "public pyELK owner matrix changed bounded fingerprint access: "
                f"{fingerprint_accesses!r} != {expected_fingerprint_accesses!r}"
            )
        request_deltas = []
        for (
            (base, raw_owner, before_native, before_python, before_ingestion),
            expected_request_delta,
        ) in zip(
            owner_evidence,
            expected_view_request_deltas,
            strict=True,
        ):
            after_native = raw_owner._publication_counters_v2()
            expected_native = replace(
                before_native,
                encoded_view_requests=(
                    before_native.encoded_view_requests + expected_request_delta
                ),
            )
            if after_native != expected_native:
                raise AssertionError(
                    "public pyELK owner matrix changed native counters outside "
                    "the exact encoded-view export delta"
                )
            if base._native_python_counters() != before_python:
                raise AssertionError("public pyELK owner matrix materialized Python model rows")
            if base._native_ingestion_counters_v2() != before_ingestion:
                raise AssertionError("public pyELK owner matrix repeated native ingestion")
            request_deltas.append(
                after_native.encoded_view_requests - before_native.encoded_view_requests
            )
        result["encoded_view_request_deltas"] = request_deltas
        result["fingerprint_accesses"] = dict(fingerprint_accesses)
        if candidate.capabilities.encoded_view_schemas:
            raise AssertionError("test-local owner capability leaked after public pyELK use")
        if cast(Any, pyelk_native).encoded_view_schemas():
            raise AssertionError("test-local pyELK capability leaked after owner-matrix use")
        return result

    def native_snapshot(source: bytes, format: DocumentFormat) -> Any:
        with ExitStack() as stack:
            if format is DocumentFormat.RDF_XML:
                stack.enter_context(
                    patch(
                        "pyowl_core.backends.parser._NativeBackendDriver.select",
                        autospec=True,
                        return_value="native",
                    )
                )
                stack.enter_context(
                    patch(
                        "pyowl_core.backends.python.parser.parse_rdfxml",
                        side_effect=AssertionError(
                            "guarded RDF/XML owner matrix crossed the Python parser"
                        ),
                    )
                )
            return load_snapshot(
                source,
                options=options(format, BackendPreference.NATIVE),
            )

    def exercise_owner_matrix(
        source: bytes,
        right_source: bytes,
        format: DocumentFormat,
    ) -> dict[str, dict[str, object]]:
        reference = load_snapshot(
            source,
            options=options(format, BackendPreference.PYTHON),
        )
        right_reference = load_snapshot(
            right_source,
            options=options(format, BackendPreference.PYTHON),
        )
        direct = native_snapshot(source, format)
        right = native_snapshot(right_source, format)
        if direct.capabilities.backend != "native" or right.capabilities.backend != "native":
            raise AssertionError("owner matrix did not retain both native inputs")

        direct_fingerprint_accesses = {
            "structural_fingerprint": 0,
            "logical_fingerprint": 1,
            "signature_fingerprint": 1,
        }
        observed_owners = {
            "direct": exercise_owner(
                direct,
                reference,
                native_bases=(direct,),
                expected_segments=1,
                expected_references=0,
                expected_detached_buffers=11,
                expected_fingerprint_accesses=direct_fingerprint_accesses,
                expected_view_request_deltas=(1,),
            )
        }
        payload = encode_snapshot(direct)
        decoded = decode_snapshot(payload)
        # A decoded scalar snapshot owns its fallback columns. Prime that core producer
        # before the consumer-only scalar fence, then require pyELK to reuse the cached view.
        decoded_encoded = decoded.view(
            EncodedStructuralView,
            schema_version=ENCODED_SCHEMA_VERSION,
        )
        if decoded_encoded.owner is not decoded:
            raise AssertionError("decoded owner matrix primed the wrong encoded owner")
        del decoded_encoded
        with tempfile.TemporaryDirectory(prefix="pyowl-core-pyelk-mapped-") as directory:
            path = Path(directory) / "retained.pyocore"
            path.write_bytes(payload)
            mapped = open_snapshot(path, mmap=True, verify=True)
            overlay = apply_delta(direct, OntologyDelta())
            composite = compose_views(direct, right, roles=("left", "right"))
            expected_overlay = apply_delta(reference, OntologyDelta())
            expected_composite = compose_views(
                reference,
                right_reference,
                roles=("left", "right"),
            )
            try:
                observed_owners.update(
                    {
                        "decoded": exercise_owner(
                            decoded,
                            reference,
                            native_bases=(direct,),
                            expected_segments=1,
                            expected_references=0,
                            expected_detached_buffers=11,
                            expected_fingerprint_accesses=direct_fingerprint_accesses,
                            expected_view_request_deltas=(0,),
                        ),
                        "mmap": exercise_owner(
                            mapped,
                            reference,
                            native_bases=(direct,),
                            expected_segments=1,
                            expected_references=0,
                            expected_detached_buffers=0,
                            expected_fingerprint_accesses=direct_fingerprint_accesses,
                            expected_view_request_deltas=(0,),
                        ),
                        "overlay": exercise_owner(
                            overlay,
                            expected_overlay,
                            native_bases=(direct,),
                            expected_segments=2,
                            expected_references=1,
                            expected_detached_buffers=11,
                            expected_fingerprint_accesses=direct_fingerprint_accesses,
                            expected_view_request_deltas=(0,),
                        ),
                        "composite": exercise_owner(
                            composite,
                            expected_composite,
                            native_bases=(direct, right),
                            expected_segments=4,
                            expected_references=2,
                            expected_detached_buffers=22,
                            expected_fingerprint_accesses={
                                "structural_fingerprint": 0,
                                "logical_fingerprint": 0,
                                "signature_fingerprint": 0,
                            },
                            expected_view_request_deltas=(0, 1),
                        ),
                    }
                )
            finally:
                del overlay, composite, expected_overlay, expected_composite
                gc.collect()
                cast(Any, mapped).close()
        for snapshot in (decoded, right, direct, right_reference, reference):
            close_snapshot = getattr(snapshot, "close", None)
            if callable(close_snapshot):
                close_snapshot()
        return observed_owners

    observed = {
        "functional": exercise(
            functional_source,
            DocumentFormat.FUNCTIONAL,
            guarded_candidate=False,
        ),
        "rdfxml": exercise(
            rdfxml_source,
            DocumentFormat.RDF_XML,
            guarded_candidate=True,
        ),
    }
    if observed["functional"]["compiler_digest"] != observed["rdfxml"]["compiler_digest"]:
        raise AssertionError("format-equivalent retained owners produced different pyELK compilers")
    owner_matrix = {
        "functional": exercise_owner_matrix(
            functional_source,
            right_functional_source,
            DocumentFormat.FUNCTIONAL,
        ),
        "rdfxml": exercise_owner_matrix(
            rdfxml_source,
            right_rdfxml_source,
            DocumentFormat.RDF_XML,
        ),
    }

    def owner_digest(format_name: str, owner_name: str) -> str:
        value = owner_matrix[format_name][owner_name]["compiler_digest"]
        if not isinstance(value, str):
            raise AssertionError("public pyELK owner matrix returned a non-text digest")
        return value

    semantic_owners = ("direct", "decoded", "mmap", "overlay")
    for format_name in ("functional", "rdfxml"):
        semantic_digests = {owner_digest(format_name, owner_name) for owner_name in semantic_owners}
        if semantic_digests != {observed[format_name]["compiler_digest"]}:
            raise AssertionError("direct, decoded, mmap, no-op overlay, and scalar digests diverge")
    for owner_name in (*semantic_owners, "composite"):
        if owner_digest("functional", owner_name) != owner_digest("rdfxml", owner_name):
            raise AssertionError(f"format-equivalent {owner_name} owner digests diverge")
    print(json.dumps({"formats": observed, "owners": owner_matrix}, sort_keys=True))


if __name__ == "__main__":
    main()
