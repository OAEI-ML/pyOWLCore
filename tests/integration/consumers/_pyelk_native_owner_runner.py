from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
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
    )
    from pyowl_core.backends import native

    native._reset_probe_cache_for_tests()
    probe = native.probe(refresh=True)
    if not probe.available or "parse-functional-v1" not in probe.features:
        raise RuntimeError(probe.reason or "native Functional parser is unavailable")
    pyelk_native = _load_pyelk_extension(
        Path(os.environ["PYOWL_CORE_TEST_PYELK_NATIVE_LIBRARY"])
    )
    from pyelk.indexing.compiler import compile_ontology  # type: ignore[import-not-found]
    from pyelk.indexing.summary import compiler_digest  # type: ignore[import-not-found]

    source = b"""Prefix(:=<urn:core-pyelk#>) Ontology(<urn:core-pyelk>
      Declaration(Class(:A))
      Declaration(Class(:B))
      Declaration(ObjectProperty(:p))
      SubClassOf(:A :B)
      SubClassOf(:A ObjectSomeValuesFrom(:p :B))
      ObjectPropertyRange(:p :B)
    )"""

    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=backend,
            collect_provenance=False,
        )

    reference = load_snapshot(source, options=options(BackendPreference.PYTHON))
    selected = load_snapshot(source, options=options(BackendPreference.NATIVE))
    if selected.capabilities.backend != "native":
        raise AssertionError("public forced-native load did not retain the V2 owner")
    if selected.capabilities.encoded_view_schemas:
        raise AssertionError("retained-load fixture advertised an unfinished view capability")
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
    scalar_error = AssertionError("pyELK handoff crossed scalar ontology traversal")
    with (
        patch.object(type(selected), "iter_axioms", side_effect=scalar_error),
        patch.object(type(selected), "iter_extensions", side_effect=scalar_error),
        patch.object(type(selected), "ontology_annotations", side_effect=scalar_error),
        patch.object(type(selected), "signature", side_effect=scalar_error),
    ):
        encoded = selected.view(EncodedStructuralView)
        direct = pyelk_native.create_session_from_encoded(encoded, 1, "error")
    after = cast(Any, raw_owner)._publication_counters_v2()

    compiled = compile_ontology(reference, unsupported="error")
    scalar = pyelk_native.create_session(compiled.encode(), 1)
    try:
        exporters = {id(value.obj) for value in encoded.buffers.values()}
        if encoded.owner is not selected:
            raise AssertionError("encoded handoff changed the exact public owner")
        if len(encoded.buffers) != 11 or len(exporters) != 1:
            raise AssertionError("encoded handoff is not eleven slices over one exporter")
        if not all(type(value.obj) is bytes for value in encoded.buffers.values()):
            raise AssertionError("encoded handoff exporter is not the direct immutable bytes owner")
        if after.encoded_view_requests != before.encoded_view_requests + 1:
            raise AssertionError("encoded handoff did not use exactly one native view export")
        if after.page_requests != before.page_requests or after.rows_emitted != before.rows_emitted:
            raise AssertionError("encoded handoff materialized scalar facade rows")

        direct_diagnostics = direct.diagnostics()
        scalar_diagnostics = scalar.diagnostics()
        expected_digest = compiler_digest(compiled).hex()
        if direct_diagnostics["compiler_digest"] != expected_digest:
            raise AssertionError("pyELK direct compiler digest diverges from scalar compilation")
        if scalar_diagnostics["compiler_digest"] != expected_digest:
            raise AssertionError("pyELK scalar compiler digest diverges from its source IR")
        if direct_diagnostics["encoded_zero_copy_buffers"] != 11:
            raise AssertionError("pyELK did not borrow all eleven direct buffers")
        if direct_diagnostics["encoded_detached_buffer_count"] != 11:
            raise AssertionError("pyELK did not detach the shared exporter")
        if direct_diagnostics["encoded_staging_copy_bytes"] != 0:
            raise AssertionError("pyELK staged the direct encoded handoff")
        if direct.debug_snapshot(realize=True) != scalar.debug_snapshot(realize=True):
            raise AssertionError("pyELK direct and scalar results diverge")
        print(
            json.dumps(
                {
                    "compiler_digest": expected_digest,
                    "encoded_buffers": len(encoded.buffers),
                    "encoded_exporters": len(exporters),
                    "scalar_facade_rows": after.rows_emitted - before.rows_emitted,
                },
                sort_keys=True,
            )
        )
    finally:
        direct.close()
        scalar.close()


if __name__ == "__main__":
    main()
