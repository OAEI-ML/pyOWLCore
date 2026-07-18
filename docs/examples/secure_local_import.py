"""Executable offline import-resolution example using an exact local mapping."""

from __future__ import annotations

from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    MappingResolver,
    OntologySnapshot,
    load_snapshot,
)

ROOT = b"""\
Ontology(<urn:example:root>
    Import(<urn:example:child>)
    Declaration(Class(<urn:example#RootClass>))
)
"""
CHILD = b"""\
Ontology(<urn:example:child>
    Declaration(Class(<urn:example#ChildClass>))
)
"""


def demonstrate() -> OntologySnapshot:
    """Resolve one allowlisted in-memory import with network access disabled."""

    snapshot = load_snapshot(
        ROOT,
        document_iri="urn:example:root-document",
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.RESOLVE_LOCAL,
            backend=BackendPreference.PYTHON,
            offline=True,
        ),
        resolver=MappingResolver({"urn:example:child": CHILD}),
    )
    assert snapshot.is_complete
    assert len(snapshot.documents) == 2
    assert len(snapshot.import_manifest.edges) == 1
    return snapshot


if __name__ == "__main__":
    demonstrate()
