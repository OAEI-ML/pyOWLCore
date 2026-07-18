"""Executable offline import-resolution example using an exact local mapping."""

from __future__ import annotations

import os

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


def demonstrate(
    backend: BackendPreference = BackendPreference.PYTHON,
) -> OntologySnapshot:
    """Resolve one allowlisted in-memory import with network access disabled."""

    snapshot = load_snapshot(
        ROOT,
        document_iri="urn:example:root-document",
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.RESOLVE_LOCAL,
            backend=backend,
            offline=True,
        ),
        resolver=MappingResolver({"urn:example:child": CHILD}),
    )
    assert snapshot.is_complete
    assert len(snapshot.documents) == 2
    assert len(snapshot.import_manifest.edges) == 1
    return snapshot


if __name__ == "__main__":
    demonstrate(BackendPreference(os.environ.get("PYOWL_CORE_DOCS_BACKEND", "python")))
