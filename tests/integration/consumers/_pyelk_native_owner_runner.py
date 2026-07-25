from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from unittest.mock import patch

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
        load_snapshot,
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

    seed = load_snapshot(
        functional_source,
        options=options(DocumentFormat.FUNCTIONAL, BackendPreference.PYTHON),
    )
    try:
        rdfxml_source = render_document(seed.root, format=DocumentFormat.RDF_XML)
    finally:
        close_seed = getattr(seed, "close", None)
        if callable(close_seed):
            close_seed()

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
    print(json.dumps({"formats": observed}, sort_keys=True))


if __name__ == "__main__":
    main()
