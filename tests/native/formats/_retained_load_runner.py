from __future__ import annotations

import gc
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any, cast

from tests.native.foundation._support import load_extension


def main() -> None:
    extension = load_extension()

    import pyowl_core
    from pyowl_core import (
        BackendPreference,
        DocumentFormat,
        EncodedStructuralView,
        ImportPolicy,
        LoadOptions,
        SnapshotInUseError,
        decode_snapshot,
        encode_snapshot,
        load_snapshot,
        open_snapshot,
    )
    from pyowl_core.backends import native
    from pyowl_core.model import canonical_bytes
    from tests.native.encoded_views._independent import decode_root_canonical_bytes
    from tools.wire_reference import read_wire

    native._reset_probe_cache_for_tests()
    probe = native.probe(refresh=True)
    if not probe.available or "parse-functional-v1" not in probe.features:
        raise RuntimeError(probe.reason or "native Functional parser is unavailable")
    if not hasattr(extension, "_retain_structural_snapshot_v2"):
        raise RuntimeError("native artifact lacks the retained-owner constructor")

    source = (
        b"Ontology(<urn:retained-installed> "
        b"Declaration(Class(<urn:retained-installed:C>)) "
        b"Declaration(Class(<urn:retained-installed:D>)) "
        b"SubClassOf(<urn:retained-installed:C> <urn:retained-installed:D>))"
    )

    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=backend,
            collect_provenance=True,
        )

    reference = load_snapshot(source, options=options(BackendPreference.PYTHON))
    selected = load_snapshot(source, options=options(BackendPreference.NATIVE))
    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    if type(raw_owner) is not cast(Any, extension)._NativeSnapshotHandle:
        raise AssertionError("public load did not retain the exact Rust owner")

    reference_origin_rows = sum(len(rows) for rows in reference.origin_index.entries.values())
    retained_origin_rows = sum(len(rows) for rows in selected.origin_index.entries.values())
    origin_parity = selected.origin_index == reference.origin_index
    expected_roots = tuple((2, canonical_bytes(value)) for value in reference.iter_axioms())
    before_native = raw_owner._publication_counters_v2()
    before_python = cast(Any, selected)._native_python_counters()
    direct = selected.view(EncodedStructuralView)
    after_direct_native = raw_owner._publication_counters_v2()
    after_direct_python = cast(Any, selected)._native_python_counters()
    direct_roots = decode_root_canonical_bytes(direct.buffers)
    if direct_roots != expected_roots:
        raise AssertionError("retained direct columns disagree with scalar roots")
    if direct.owner is not selected:
        raise AssertionError("retained direct columns lost public owner identity")
    if len({id(value.obj) for value in direct.buffers.values()}) != 1:
        raise AssertionError("retained direct columns do not share one exporter")
    if not all(type(value.obj) is bytes for value in direct.buffers.values()):
        raise AssertionError("retained direct columns are not backed by immutable bytes")
    if after_direct_native.page_requests != before_native.page_requests:
        raise AssertionError("retained direct columns crossed scalar facade paging")
    if after_direct_native.rows_emitted != before_native.rows_emitted:
        raise AssertionError("retained direct columns emitted scalar facade rows")
    if after_direct_python.model_rows_materialized != before_python.model_rows_materialized:
        raise AssertionError("retained direct columns materialized Python model rows")

    reference_wire = encode_snapshot(reference)
    retained_wire = encode_snapshot(selected)
    reference_image = read_wire(reference_wire)
    retained_image = read_wire(retained_wire)
    wire_differing_sections = sorted(
        kind
        for kind in set(reference_image.sections) | set(retained_image.sections)
        if reference_image.sections.get(kind) != retained_image.sections.get(kind)
    )
    if wire_differing_sections not in ([], [13, 14]):
        raise AssertionError("retained snapshot has an unexpected wire divergence")
    after_wire_native = raw_owner._publication_counters_v2()
    after_wire_python = cast(Any, selected)._native_python_counters()
    if after_wire_python.model_rows_materialized != after_direct_python.model_rows_materialized:
        raise AssertionError("wire handoff materialized Python model rows")
    if after_wire_native.encoded_view_requests - after_direct_native.encoded_view_requests != 1:
        raise AssertionError("wire handoff did not reuse one direct-column publication")
    if after_wire_native.page_requests - after_direct_native.page_requests != 1:
        raise AssertionError("wire handoff did not use exactly one retained origin page")
    if after_wire_native.rows_emitted - after_direct_native.rows_emitted != retained_origin_rows:
        raise AssertionError("wire handoff emitted rows outside the retained origin page")
    if after_wire_native.publication_structural_rows_copied != 0:
        raise AssertionError("wire handoff copied structural publication rows")
    if after_wire_native.publication_structural_bytes_copied != 0:
        raise AssertionError("wire handoff copied structural publication bytes")

    decoded = decode_snapshot(retained_wire)
    decoded_parity = (
        decoded.structural_fingerprint == reference.structural_fingerprint
        and decoded.logical_fingerprint == reference.logical_fingerprint
        and decoded.signature_fingerprint == reference.signature_fingerprint
        and encode_snapshot(decoded) == retained_wire
    )
    if not decoded_parity:
        raise AssertionError("decoded retained wire differs from Python reference")

    without_provenance_reference = load_snapshot(
        source,
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
            collect_provenance=False,
        ),
    )
    without_provenance = load_snapshot(
        source,
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.NATIVE,
            collect_provenance=False,
        ),
    )
    without_provenance_handle = cast(Any, without_provenance)._native_snapshot_state.owner.handle
    without_provenance_owner = object.__getattribute__(without_provenance_handle, "_owner_v2")
    without_provenance_counters = without_provenance_owner._publication_counters_v2()
    without_provenance_parity = (
        without_provenance.capabilities.backend == "native"
        and without_provenance.origin_index == without_provenance_reference.origin_index
        and without_provenance.root.origin_index is None
        and without_provenance_handle.attestation.capability_bits == 7
        and without_provenance_counters.parser_bytes == len(source)
        and without_provenance_counters.retained_origin_rows == 0
        and encode_snapshot(without_provenance) == encode_snapshot(without_provenance_reference)
    )
    if not without_provenance_parity:
        raise AssertionError("provenance-disabled retained load differs from Python reference")

    with tempfile.TemporaryDirectory(prefix="pyowl-core-retained-wire-") as temporary:
        path = Path(temporary) / "retained.pyocore"
        path.write_bytes(retained_wire)
        mapped = open_snapshot(path, mmap=True, verify=True)
        if not isinstance(mapped, pyowl_core.MappedOntologySnapshot):
            raise AssertionError("mmap=True did not publish a mapped snapshot")
        if mapped._mapped_state.decoded is not None:
            raise AssertionError("mapped snapshot materialized during publication")
        mapped_fingerprint_parity = (
            mapped.structural_fingerprint == reference.structural_fingerprint
            and mapped.logical_fingerprint == reference.logical_fingerprint
            and mapped.signature_fingerprint == reference.signature_fingerprint
        )
        mapped_encoded = mapped.view(EncodedStructuralView)
        mapped_roots = decode_root_canonical_bytes(mapped_encoded.buffers)
        exporters = tuple(value.obj for value in mapped_encoded.buffers.values())
        mapped_one_exporter = bool(exporters) and all(value is exporters[0] for value in exporters)
        mapped_readonly = all(value.readonly for value in mapped_encoded.buffers.values())
        mapped_lazy = mapped._mapped_state.decoded is None
        mapped_owner_identity = mapped_encoded.owner is mapped
        mapped_close_blocked = False
        try:
            mapped.close()
        except SnapshotInUseError:
            mapped_close_blocked = True
        if not mapped_close_blocked:
            raise AssertionError("live mmap columns did not block owner close")
        del mapped_encoded
        gc.collect()
        mapped.close()
        mapped_closed = mapped.closed

    selected.close()
    direct_survives_owner_close = decode_root_canonical_bytes(direct.buffers) == expected_roots

    print(
        json.dumps(
            {
                "backend": selected.capabilities.backend,
                "decoded_parity": decoded_parity,
                "direct_encoded_view_requests": (
                    after_direct_native.encoded_view_requests - before_native.encoded_view_requests
                ),
                "direct_owner_identity": direct.owner is selected,
                "direct_root_parity": direct_roots == expected_roots,
                "direct_survives_owner_close": direct_survives_owner_close,
                "encoded_view_schemas": dict(selected.capabilities.encoded_view_schemas),
                "fingerprint_parity": (
                    selected.structural_fingerprint == reference.structural_fingerprint
                    and selected.logical_fingerprint == reference.logical_fingerprint
                    and selected.signature_fingerprint == reference.signature_fingerprint
                ),
                "ingestion_features": list(extension.INGESTION_FEATURES),
                "mapped_closed": mapped_closed,
                "mapped_close_blocked": mapped_close_blocked,
                "mapped_fingerprint_parity": mapped_fingerprint_parity,
                "mapped_lazy": mapped_lazy,
                "mapped_one_exporter": mapped_one_exporter,
                "mapped_owner_identity": mapped_owner_identity,
                "mapped_readonly": mapped_readonly,
                "mapped_root_parity": mapped_roots == expected_roots,
                "mapped_exporter_type": type(exporters[0]).__name__ if exporters else None,
                "origin_parity": origin_parity,
                "package_file": pyowl_core.__file__,
                "parser_bytes": before_native.parser_bytes,
                "phase_timings": dict(selected.report.timings),
                "provenance_disabled_parity": without_provenance_parity,
                "provenance_disabled_retained_origin_rows": (
                    without_provenance_counters.retained_origin_rows
                ),
                "reference_origin_rows": reference_origin_rows,
                "retained_origin_rows": retained_origin_rows,
                "selected_closed": selected.closed,
                "snapshot_type": type(selected).__name__,
                "view_features": list(extension.VIEW_FEATURES),
                "wire_model_rows_materialized": (
                    after_wire_python.model_rows_materialized
                    - after_direct_python.model_rows_materialized
                ),
                "wire_encoded_view_requests": (
                    after_wire_native.encoded_view_requests
                    - after_direct_native.encoded_view_requests
                ),
                "wire_page_requests": (
                    after_wire_native.page_requests - after_direct_native.page_requests
                ),
                "wire_differing_sections": wire_differing_sections,
                "wire_python_parity": retained_wire == reference_wire,
                "wire_rows_emitted": (
                    after_wire_native.rows_emitted - after_direct_native.rows_emitted
                ),
                "wire_python_sha256": hashlib.sha256(reference_wire).hexdigest(),
                "wire_sha256": hashlib.sha256(retained_wire).hexdigest(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
