from __future__ import annotations

import json
from typing import Any, cast

from tests.native.foundation._support import load_extension


def main() -> None:
    extension = load_extension()

    import pyowl_core
    from pyowl_core import (
        BackendPreference,
        DocumentFormat,
        ImportPolicy,
        LoadOptions,
        load_snapshot,
    )
    from pyowl_core.backends import native

    native._reset_probe_cache_for_tests()
    probe = native.probe(refresh=True)
    if not probe.available or "parse-functional-v1" not in probe.features:
        raise RuntimeError(probe.reason or "native Functional parser is unavailable")
    if not hasattr(extension, "_retain_structural_snapshot_v2"):
        raise RuntimeError("native artifact lacks the retained-owner constructor")

    source = (
        b"Ontology(<urn:retained-installed> "
        b"Declaration(Class(<urn:retained-installed:C>)) "
        b"SubClassOf(<urn:retained-installed:C> <urn:retained-installed:D>))"
    )

    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=backend,
            collect_provenance=False,
        )

    reference = load_snapshot(source, options=options(BackendPreference.PYTHON))
    selected = load_snapshot(source, options=options(BackendPreference.NATIVE))
    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    if type(raw_owner) is not cast(Any, extension)._NativeSnapshotHandle:
        raise AssertionError("public load did not retain the exact Rust owner")

    print(
        json.dumps(
            {
                "backend": selected.capabilities.backend,
                "encoded_view_schemas": dict(selected.capabilities.encoded_view_schemas),
                "fingerprint_parity": (
                    selected.structural_fingerprint == reference.structural_fingerprint
                    and selected.logical_fingerprint == reference.logical_fingerprint
                    and selected.signature_fingerprint == reference.signature_fingerprint
                ),
                "ingestion_features": list(extension.INGESTION_FEATURES),
                "package_file": pyowl_core.__file__,
                "snapshot_type": type(selected).__name__,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
