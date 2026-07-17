from __future__ import annotations

from pyowl_core import (
    IRI,
    BackendPreference,
    Class,
    Declaration,
    ImportPolicy,
    LoadOptions,
    ParseLimits,
    load_snapshot,
)


def declaration(name: str) -> Declaration:
    return Declaration(Class(IRI(f"urn:test#{name}")))


def snapshot(identity: str, *names: str, limits: ParseLimits | None = None):
    body = " ".join(f"Declaration(Class(:{name}))" for name in names)
    source = f"Prefix(:=<urn:test#>) Ontology(<urn:{identity}> {body})".encode()
    return load_snapshot(
        source,
        options=LoadOptions(
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
            limits=limits or ParseLimits(),
        ),
    )
