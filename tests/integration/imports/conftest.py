from __future__ import annotations

from collections.abc import Iterable

from pyowl_core import BackendPreference, ImportPolicy, LoadOptions, ParseLimits


def functional(
    ontology_iri: str | None,
    *,
    imports: Iterable[str] = (),
    body: Iterable[str] = (),
    whitespace: str = " ",
) -> bytes:
    identity = "" if ontology_iri is None else f"<{ontology_iri}>"
    components = [*(f"Import(<{item}>)" for item in imports), *body]
    content = whitespace.join(components)
    return (f"Prefix(:=<urn:test#>){whitespace}Ontology({identity}{whitespace}{content})").encode()


def load_options(
    policy: ImportPolicy,
    *,
    offline: bool = True,
    limits: ParseLimits | None = None,
    validate_owl2_dl: bool = False,
) -> LoadOptions:
    return LoadOptions(
        imports=policy,
        backend=BackendPreference.PYTHON,
        offline=offline,
        limits=limits or ParseLimits(),
        validate_owl2_dl=validate_owl2_dl,
    )
