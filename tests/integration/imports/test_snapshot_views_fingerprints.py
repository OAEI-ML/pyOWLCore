from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pyowl_core import (
    IRI,
    AxiomScope,
    ClassAssertion,
    EntityKind,
    ImportPolicy,
    MappingResolver,
    OntologyView,
    ResolvedDocument,
    SubClassOf,
    load_snapshot,
    structural_digest,
)

from .conftest import functional, load_options

ROOT = Path(__file__).resolve().parents[3]


def test_root_document_identity_and_scope_iteration() -> None:
    root_document = load_snapshot(
        functional(
            "urn:root",
            body=(
                "Declaration(Class(:A))",
                "Declaration(Class(:B))",
                "SubClassOf(:A :B)",
            ),
        ),
        options=load_options(ImportPolicy.IGNORE),
    ).root
    snapshot = load_snapshot(
        root_document,
        options=load_options(ImportPolicy.IGNORE),
    )

    assert snapshot.root is root_document
    assert snapshot.document(snapshot.root_document_key) is root_document
    assert isinstance(snapshot, OntologyView)
    assert len(tuple(snapshot.iter_documents())) == 1
    assert tuple(snapshot.iter_axioms(scope=AxiomScope.ROOT)) == tuple(snapshot.iter_axioms())
    assert len(tuple(snapshot.iter_axioms(SubClassOf))) == 1
    with pytest.raises(ValueError):
        tuple(snapshot.iter_axioms(scope=AxiomScope.DOCUMENT))
    with pytest.raises(ValueError):
        tuple(snapshot.iter_axioms(scope=AxiomScope.CLOSURE, document_key="bad"))


def test_anonymous_individuals_are_standardized_apart_across_equal_documents() -> None:
    first = functional(
        None,
        body=("Declaration(Class(:C))", "ClassAssertion(:C _:same)"),
        whitespace=" ",
    )
    second = functional(
        None,
        body=("Declaration(Class(:C))", "ClassAssertion(:C _:same)"),
        whitespace="\n",
    )
    snapshot = load_snapshot(
        functional("urn:root", imports=("urn:first", "urn:second")),
        options=load_options(ImportPolicy.RESOLVE_LOCAL),
        resolver=MappingResolver(
            {
                "urn:first": ResolvedDocument(first, document_iri=IRI("urn:doc:first")),
                "urn:second": ResolvedDocument(second, document_iri=IRI("urn:doc:second")),
            }
        ),
    )
    assertions = tuple(snapshot.iter_axioms(ClassAssertion))
    assert len(assertions) == 2
    individuals = {item.individual for item in assertions}
    assert len(individuals) == 2
    assert len({item.document_scope for item in individuals}) == 2
    assert all(len(snapshot.origin_index.origins_for(item)) == 1 for item in assertions)


def test_duplicate_named_axioms_collapse_and_retain_all_origins() -> None:
    body = (
        "Declaration(Class(:A))",
        "Declaration(Class(:B))",
        "SubClassOf(:A :B)",
    )
    snapshot = load_snapshot(
        functional("urn:root", imports=("urn:left", "urn:right")),
        options=load_options(ImportPolicy.RESOLVE_LOCAL),
        resolver=MappingResolver(
            {
                "urn:left": functional("urn:left", body=body),
                "urn:right": functional("urn:right", body=body),
            }
        ),
    )
    subclass = tuple(snapshot.iter_axioms(SubClassOf))
    assert len(subclass) == 1
    origins = snapshot.origin_index.origins_for(subclass[0])
    assert len(origins) == 2
    assert len({item.document_key for item in origins}) == 2


def test_structural_logical_and_signature_fingerprint_boundaries() -> None:
    base_body = (
        "Declaration(Class(:A))",
        "Declaration(Class(:B))",
        "SubClassOf(:A :B)",
    )
    annotated_body = (
        'Annotation(<urn:label> "ontology annotation")',
        *base_body,
    )
    plain = load_snapshot(
        functional("urn:root", body=base_body),
        options=load_options(ImportPolicy.IGNORE),
    )
    annotated = load_snapshot(
        functional("urn:root", body=annotated_body),
        options=load_options(ImportPolicy.IGNORE),
    )
    local_policy = load_snapshot(
        functional("urn:root", body=base_body),
        options=load_options(ImportPolicy.RESOLVE_LOCAL),
    )

    assert plain.structural_fingerprint != annotated.structural_fingerprint
    assert plain.logical_fingerprint == annotated.logical_fingerprint
    assert plain.structural_fingerprint != local_policy.structural_fingerprint
    assert plain.logical_fingerprint == local_policy.logical_fingerprint
    assert plain.report.structural_fingerprint == plain.structural_fingerprint
    assert plain.report.logical_fingerprint == plain.logical_fingerprint
    assert annotated.signature(EntityKind.ANNOTATION_PROPERTY)


def test_acquisition_locators_do_not_change_snapshot_fingerprints() -> None:
    root = functional("urn:root", imports=("urn:child",))
    child = functional("urn:child", body=("Declaration(Class(:C))",))
    first = load_snapshot(
        root,
        options=load_options(ImportPolicy.RESOLVE_LOCAL),
        resolver=MappingResolver(
            {
                "urn:child": ResolvedDocument(
                    child,
                    document_iri=IRI("urn:canonical"),
                    provenance={"locator": "one.owl"},
                )
            }
        ),
    )
    second = load_snapshot(
        root,
        options=load_options(ImportPolicy.RESOLVE_LOCAL),
        resolver=MappingResolver(
            {
                "urn:child": ResolvedDocument(
                    child,
                    document_iri=IRI("urn:canonical"),
                    provenance={"locator": "two.owl"},
                )
            }
        ),
    )
    assert first.import_manifest == second.import_manifest
    assert first.structural_fingerprint == second.structural_fingerprint
    assert first.logical_fingerprint == second.logical_fingerprint


def test_signature_filters_membership_and_origin_digest_consistency() -> None:
    snapshot = load_snapshot(
        functional(
            "urn:root",
            body=(
                "Declaration(Class(:A))",
                "Declaration(Class(:B))",
                "SubClassOf(:A :B)",
            ),
        ),
        options=load_options(ImportPolicy.IGNORE),
    )
    axiom = next(snapshot.iter_axioms(SubClassOf))
    assert snapshot.contains(axiom)
    assert len(snapshot.signature(EntityKind.CLASS)) == 2
    assert snapshot.signature_fingerprint.hex == snapshot.report.signature_fingerprint.hex
    assert len(snapshot.origin_index.entries[structural_digest(axiom)]) == 1


def test_manifest_and_fingerprints_are_hash_seed_independent() -> None:
    script = r"""
import json
from pyowl_core import BackendPreference, ImportPolicy, LoadOptions, MappingResolver, load_snapshot
root = b"Ontology(<urn:root> Import(<urn:b>) Import(<urn:a>))"
resolver = MappingResolver({
    "urn:b": b"Ontology(<urn:b> Declaration(Class(<urn:B>)))",
    "urn:a": b"Ontology(<urn:a> Declaration(Class(<urn:A>)))",
})
snapshot = load_snapshot(root, options=LoadOptions(
    imports=ImportPolicy.RESOLVE_LOCAL,
    backend=BackendPreference.PYTHON,
), resolver=resolver)
print(json.dumps({
    "documents": [item.document_key for item in snapshot.import_manifest.documents],
    "edges": [(item.importing_document_key, item.import_iri.value, item.resolved_document_key)
              for item in snapshot.import_manifest.edges],
    "structural": snapshot.structural_fingerprint.hex,
    "logical": snapshot.logical_fingerprint.hex,
    "signature": snapshot.signature_fingerprint.hex,
}, sort_keys=True))
"""
    outputs: list[dict[str, object]] = []
    for seed in ("1", "987654"):
        environment = dict(os.environ)
        environment.update(
            PYTHONPATH=str(ROOT / "src"),
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONHASHSEED=seed,
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(json.loads(result.stdout))
    assert outputs[0] == outputs[1]
