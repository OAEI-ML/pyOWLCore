from __future__ import annotations

import hashlib

from pyowl_core import (
    AxiomScope,
    BackendPreference,
    ImportPolicy,
    LoadOptions,
    load_snapshot,
)
from pyowl_core.document.fingerprint import (
    StructuralContext,
    document_fingerprint_bytes,
    document_fingerprint_bytes_v1,
    document_fingerprint_v1,
    logical_fingerprint_bytes,
    logical_fingerprint_bytes_v1,
    logical_fingerprint_v1,
    signature_fingerprint_bytes,
    signature_fingerprint_bytes_v1,
    signature_fingerprint_v1,
    snapshot_structural_fingerprint_v1,
    structural_context_bytes_v1,
    structural_context_bytes_v2,
)

_SOURCE = b"Prefix(:=<urn:v#>) Ontology(<urn:vectors> Declaration(Class(:A)) SubClassOf(:A :B))"


def _snapshot():  # type: ignore[no-untyped-def]
    return load_snapshot(
        _SOURCE,
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            imports=ImportPolicy.IGNORE,
        ),
    )


def test_model_schema_v2_fingerprint_vectors_are_exact() -> None:
    snapshot = _snapshot()

    assert snapshot.root_document_key == (
        "d2:6c358dd7587a145a5c7220e25fa63e3b9339cab39fb310e33dd09ff49750bf92"
    )
    assert snapshot.root.document_fingerprint.schema == 2
    assert snapshot.root.document_fingerprint.hex == (
        "3a51ced38897d015d525cd4828586907182e84447ccf96ee84247e0e0c818b35"
    )
    assert snapshot.structural_fingerprint.schema == 2
    assert snapshot.structural_fingerprint.hex == (
        "0fc2e870415fa0591073fb01623be9ba4766d73807a7a620f359583139f87597"
    )
    assert snapshot.logical_fingerprint.schema == 2
    assert snapshot.logical_fingerprint.hex == (
        "7fe77d7c2663d20005f26a8b0526b5f81210445505ae3bac8d25567c475bbbcb"
    )
    assert snapshot.signature_fingerprint.schema == 2
    assert snapshot.signature_fingerprint.hex == (
        "ca512102d4eb7811000f6213e14073e2c51fd580f65573f66052c159faee886f"
    )


def test_frozen_v1_domains_remain_explicit_and_never_alias_v2() -> None:
    snapshot = _snapshot()
    document = snapshot.root
    document_v1 = document_fingerprint_v1(document)
    logical_v1 = logical_fingerprint_v1(snapshot.iter_axioms(), snapshot.iter_extensions())
    signature_v1 = signature_fingerprint_v1(snapshot.signature())

    assert document_fingerprint_bytes_v1(document).startswith(
        b"pyowl-core:document-fingerprint:v1\x00"
    )
    assert document_fingerprint_bytes(document).startswith(
        b"pyowl-core:document-fingerprint:v2\x00"
    )
    assert logical_fingerprint_bytes_v1(
        snapshot.iter_axioms(), snapshot.iter_extensions()
    ).startswith(b"pyowl-core:snapshot-logical:v1\x00")
    assert logical_fingerprint_bytes(snapshot.iter_axioms(), snapshot.iter_extensions()).startswith(
        b"pyowl-core:snapshot-logical:v2\x00"
    )
    assert signature_fingerprint_bytes_v1(snapshot.signature()).startswith(
        b"pyowl-core:snapshot-signature:v1\x00"
    )
    assert signature_fingerprint_bytes(snapshot.signature()).startswith(
        b"pyowl-core:snapshot-signature:v2\x00"
    )
    assert (document_v1.schema, document_v1.hex) == (
        1,
        "d5bfbf069634b411d10081da5aa4c7ba39ff32d60f038fccc3bb4a8ae8688cac",
    )
    assert (logical_v1.schema, logical_v1.hex) == (
        1,
        "0398e11ed57dea8793cea98d312321e7c6014e8b4d92a086d67110e25c022333",
    )
    assert (signature_v1.schema, signature_v1.hex) == (
        1,
        "cc7591dfd8085ea3785df73dd2529660273646dec7ff4bd7922e43c42f6e18ff",
    )
    assert document_v1.digest != document.document_fingerprint.digest
    assert logical_v1.digest != snapshot.logical_fingerprint.digest
    assert signature_v1.digest != snapshot.signature_fingerprint.digest


def test_snapshot_and_context_domains_have_frozen_cross_language_vectors() -> None:
    snapshot = _snapshot()
    documents = tuple(
        (
            record.document_key,
            snapshot.ontology_annotations(
                scope=AxiomScope.DOCUMENT,
                document_key=record.document_key,
            ),
            tuple(
                snapshot.iter_axioms(
                    scope=AxiomScope.DOCUMENT,
                    document_key=record.document_key,
                )
            ),
            tuple(
                snapshot.iter_extensions(
                    scope=AxiomScope.DOCUMENT,
                    document_key=record.document_key,
                )
            ),
        )
        for record in snapshot.import_manifest.documents
    )
    structural_v1 = snapshot_structural_fingerprint_v1(
        snapshot.import_manifest,
        documents,
    )
    context = StructuralContext.overlay(snapshot.structural_fingerprint)

    assert (structural_v1.schema, structural_v1.hex) == (
        1,
        "f425c71f3f0b5b0bceb8fa73049573664deaa1643f26f277212c2b75755b14dd",
    )
    assert hashlib.sha256(structural_context_bytes_v1(context)).hexdigest() == (
        "6aba82ab3c1e772792a6c30f7c799493ec5ee047bf231710b72ddd2a0fa424f3"
    )
    assert hashlib.sha256(structural_context_bytes_v2(context)).hexdigest() == (
        "2beb5a66708cbbf5d72cfee83c816396805422632dc9fa84b484d2352bd9ba13"
    )
