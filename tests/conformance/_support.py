from __future__ import annotations

import os

import pyowl_core.extensions.swrl as swrl
import pyowl_core.model as m
from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    OntologyDocument,
    OntologySnapshot,
    load_snapshot,
    parse_document,
    render_document,
)
from tests.generated.model.fixtures import model_fixtures
from tests.roundtrip.test_every_constructor import _source_document


def every_constructor_document(*, include_swrl: bool = False) -> OntologyDocument:
    source = _source_document()
    document = parse_document(
        render_document(source, format=DocumentFormat.FUNCTIONAL),
        format=DocumentFormat.FUNCTIONAL,
        document_iri=source.document_iri,
        options=LoadOptions(backend=BackendPreference.PYTHON),
    )
    if not include_swrl:
        return document
    rule = model_fixtures()[swrl.SWRLRule]
    assert isinstance(rule, swrl.SWRLRule)
    return OntologyDocument(
        document.ontology_id,
        document.document_iri,
        document.direct_imports,
        document.ontology_annotations,
        document.axioms,
        m.CanonicalSet((rule,)),
        document.provenance,
        source_map=document.source_map,
        origin_index=document.origin_index,
        rdf_mapping_report=document.rdf_mapping_report,
    )


def python_snapshot(document: OntologyDocument) -> OntologySnapshot:
    return load_snapshot(
        document,
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            imports=ImportPolicy.IGNORE,
        ),
    )


def native_requested() -> bool:
    return bool(os.environ.get("PYOWL_CORE_TEST_NATIVE_LIBRARY"))


__all__ = ["every_constructor_document", "native_requested", "python_snapshot"]
