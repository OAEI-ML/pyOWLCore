from __future__ import annotations

from pyowl_core import BackendPreference, ImportPolicy, LoadOptions, load_snapshot


def snapshot(*names: str):  # type: ignore[no-untyped-def]
    body = " ".join(f"Declaration(Class(:{name}))" for name in names)
    source = f"Prefix(:=<urn:wire#>) Ontology(<urn:wire> {body})".encode()
    return load_snapshot(
        source,
        options=LoadOptions(
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
        ),
    )
