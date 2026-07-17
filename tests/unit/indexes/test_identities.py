from __future__ import annotations

from pyowl_core import (
    OntologyDelta,
    OntologyDocumentIdentity,
    OntologyIdentityIndex,
    ViewBuildStrategy,
    apply_delta,
    compose_views,
)

from .conftest import snapshot


def test_snapshot_identity_index_exposes_exact_manifest_documents_and_digests() -> None:
    source = snapshot("Declaration(Class(:A))", ontology_iri="urn:identity:root")
    first = source.view(OntologyIdentityIndex)
    second = source.view(OntologyIdentityIndex)

    assert first is second
    assert first.documents == tuple(
        OntologyDocumentIdentity(record.document_key, record.ontology_id)
        for record in source.import_manifest.documents
    )
    assert first.document_keys == tuple(record.document_key for record in first.documents)
    assert len(first.import_manifest_digest) == 32
    assert len(first.loader_diagnostics_digest) == 32
    assert first.is_complete is source.is_complete
    assert first.report.strategy is ViewBuildStrategy.FULL_BUILD


def test_overlay_reuses_base_identity_and_composite_namespaces_member_keys() -> None:
    first = snapshot(ontology_iri="urn:identity:first")
    second = snapshot(ontology_iri="urn:identity:second")
    base_identity = first.view(OntologyIdentityIndex)
    overlay = apply_delta(first, OntologyDelta())
    overlay_identity = overlay.view(OntologyIdentityIndex)

    assert overlay_identity.documents is base_identity.documents
    assert overlay_identity.import_manifest_digest == base_identity.import_manifest_digest
    assert overlay_identity.loader_diagnostics_digest == base_identity.loader_diagnostics_digest
    assert overlay_identity.report.strategy is ViewBuildStrategy.PATCHED

    composite = compose_views(first, second, roles=("first", "second"))
    identity = composite.view(OntologyIdentityIndex)
    assert identity.report.strategy is ViewBuildStrategy.MERGED
    assert len(identity.documents) == 2
    assert len(set(identity.document_keys)) == 2
    assert all(key.startswith("member:") for key in identity.document_keys)
    assert {item.ontology_id for item in identity.documents} == {
        first.root.ontology_id,
        second.root.ontology_id,
    }
    assert identity.is_complete


def test_composite_identity_is_deterministic_for_an_equal_request() -> None:
    first = snapshot(ontology_iri="urn:identity:deterministic-a")
    second = snapshot(ontology_iri="urn:identity:deterministic-b")
    left = compose_views(first, second).view(OntologyIdentityIndex)
    right = compose_views(first, second).view(OntologyIdentityIndex)

    assert left.documents == right.documents
    assert left.import_manifest_digest == right.import_manifest_digest
    assert left.loader_diagnostics_digest == right.loader_diagnostics_digest
