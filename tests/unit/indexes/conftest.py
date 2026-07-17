from __future__ import annotations

from collections.abc import Iterable

from pyowl_core import (
    BackendPreference,
    ImportPolicy,
    LoadOptions,
    ParseLimits,
    load_snapshot,
)


def functional(
    body: Iterable[str],
    *,
    ontology_iri: str = "urn:index:test",
    imports: Iterable[str] = (),
) -> bytes:
    components = [*(f"Import(<{value}>)" for value in imports), *body]
    return (
        "Prefix(:=<urn:index#>) "
        "Prefix(rdfs:=<http://www.w3.org/2000/01/rdf-schema#>) "
        f"Ontology(<{ontology_iri}> {' '.join(components)})"
    ).encode()


def snapshot(
    *body: str,
    limits: ParseLimits | None = None,
    ontology_iri: str = "urn:index:test",
):
    return load_snapshot(
        functional(body, ontology_iri=ontology_iri),
        options=LoadOptions(
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
            limits=limits or ParseLimits(),
        ),
    )
