from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields
from types import SimpleNamespace
from typing import Protocol, cast

import pytest

from pyowl_core.backends.native_handoff_v2 import (
    NativeFacadeCollectionV2,
    NativeFacadeContainsRequestV2,
    NativeFacadePageRequestV2,
    NativeFacadeScopeV2,
    NativeSignatureKindV2,
    NativeSnapshotPublicationV2,
    _seal_native_snapshot_owner_v2,
    freeze_native_snapshot_publication_v2,
)
from pyowl_core.document.native_storage import ontology_snapshot_from_native_publication_v2
from pyowl_core.model import IRI, Class, Declaration, canonical_bytes
from tests.native.foundation._support import load_extension

from ._support import publication_fields
from ._support_v2 import (
    fingerprint_evidence,
    fingerprint_preimages,
    fixture_collections,
    publication,
)


class _Closable(Protocol):
    @property
    def closed(self) -> bool: ...

    def close(self) -> None: ...


def _rust_publication() -> tuple[NativeSnapshotPublicationV2, object]:
    extension = load_extension()
    fixture = getattr(extension, "_publication_fixture_v2", None)
    if not callable(fixture):
        pytest.skip("selected native artifact lacks the V2 publication test hook")
    create = cast(Callable[..., object], fixture)
    base_values = publication_fields()
    collections = fixture_collections()
    preimages = fingerprint_preimages(base_values)
    evidence = fingerprint_evidence(base_values, preimages)
    expected = publication(collections, values=base_values, preimages=preimages)
    owner = create(
        expected.handle.attestation,
        collections,
        documents=expected.documents,
        report=expected.report,
        root_document_key=expected.root_document_key,
        load_options=expected.load_options,
        capability_bits=expected.capability_bits,
        fingerprint_evidence=evidence,
        fingerprint_preimages=preimages,
        facade_cardinality_summary=expected.facade_cardinality_summary,
        owl2_dl_report_summary=expected.owl2_dl_report_summary,
    )
    values = {item.name: getattr(expected, item.name) for item in fields(expected)}
    values["handle"] = _seal_native_snapshot_owner_v2(owner)
    return freeze_native_snapshot_publication_v2(values), owner


def _page_request(value: NativeSnapshotPublicationV2) -> NativeFacadePageRequestV2:
    return NativeFacadePageRequestV2(
        collection=NativeFacadeCollectionV2.AXIOMS,
        scope=NativeFacadeScopeV2.CLOSURE,
        document_ordinal=None,
        start=0,
        max_rows=1,
        max_bytes=value.max_facade_row_bytes,
        max_row_bytes=value.max_facade_row_bytes,
    )


def test_rebuilt_extension_owner_matches_attestation_pages_lifecycle_and_facade() -> None:
    value, _owner = _rust_publication()
    expected_axiom = Declaration(Class(IRI("urn:handoff:Class")))
    expected_row = canonical_bytes(expected_axiom)

    assert value.handle.attestation.ledger_sha256 == value.ledger_sha256
    page = value.handle._facade_page_v2(_page_request(value))
    assert page.rows == (expected_row,)
    assert value.handle._facade_contains_v2(
        NativeFacadeContainsRequestV2(
            collection=NativeFacadeCollectionV2.AXIOMS,
            scope=NativeFacadeScopeV2.CLOSURE,
            document_ordinal=None,
            canonical=expected_row,
            max_row_bytes=value.max_facade_row_bytes,
        )
    )
    document_handle = value.handle._facade_document_v2(0)
    document_page = document_handle._facade_page_v2(
        NativeFacadePageRequestV2(
            collection=NativeFacadeCollectionV2.AXIOMS,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            start=0,
            max_rows=1,
            max_bytes=value.max_facade_row_bytes,
            max_row_bytes=value.max_facade_row_bytes,
        )
    )
    assert document_page.rows == (expected_row,)
    document_handle.close()
    assert document_handle.closed and not value.handle.closed

    snapshot = ontology_snapshot_from_native_publication_v2(value)
    snapshot_lifecycle = cast(_Closable, snapshot)
    assert tuple(snapshot.iter_axioms()) == (expected_axiom,)
    assert snapshot.contains(expected_axiom)
    public_document = snapshot.document(snapshot.root_document_key)
    document_lifecycle = cast(_Closable, public_document)
    assert tuple(public_document.iter_axioms()) == (expected_axiom,)
    document_lifecycle.close()
    assert document_lifecycle.closed and not snapshot_lifecycle.closed
    assert snapshot.contains(expected_axiom)
    counters = value.handle._facade_counters_v2()
    assert counters.page_requests >= 2
    assert counters.contains_requests >= 3
    assert counters.contains_hits == counters.contains_requests
    snapshot_lifecycle.close()
    assert snapshot_lifecycle.closed


def test_direct_private_owner_rejects_mutated_exact_request_fields() -> None:
    value, owner = _rust_publication()

    def rejected(name: str, changed: object) -> None:
        request = _page_request(value)
        object.__setattr__(request, name, changed)
        with pytest.raises((TypeError, ValueError)):
            owner._publication_page_v2(request)  # type: ignore[attr-defined]

    rejected("start", True)
    rejected("max_rows", 0)
    rejected("max_rows", 65)
    rejected("max_bytes", 0)
    rejected("max_bytes", 8 * 1024 * 1024 + 1)
    rejected("scope", NativeFacadeScopeV2.DOCUMENT)
    rejected("document_ordinal", 0)
    rejected("signature_kind", NativeSignatureKindV2.CLASS)
    rejected("include_builtins", False)

    spoofed_collection = _page_request(value)
    object.__setattr__(
        spoofed_collection,
        "collection",
        SimpleNamespace(value=NativeFacadeCollectionV2.AXIOMS.value),
    )
    with pytest.raises(TypeError):
        owner._publication_page_v2(spoofed_collection)  # type: ignore[attr-defined]

    unavailable_source_map = _page_request(value)
    object.__setattr__(
        unavailable_source_map,
        "collection",
        NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES,
    )
    object.__setattr__(unavailable_source_map, "scope", NativeFacadeScopeV2.DOCUMENT)
    object.__setattr__(unavailable_source_map, "document_ordinal", 0)
    with pytest.raises(ValueError, match="capabilit"):
        owner._publication_page_v2(unavailable_source_map)  # type: ignore[attr-defined]

    unavailable_owl2_dl = _page_request(value)
    object.__setattr__(
        unavailable_owl2_dl,
        "collection",
        NativeFacadeCollectionV2.OWL2_DL_ISSUES,
    )
    with pytest.raises(ValueError, match="report"):
        owner._publication_page_v2(unavailable_owl2_dl)  # type: ignore[attr-defined]

    canonical = canonical_bytes(Declaration(Class(IRI("urn:handoff:Class"))))
    mutated = NativeFacadeContainsRequestV2(
        collection=NativeFacadeCollectionV2.AXIOMS,
        scope=NativeFacadeScopeV2.CLOSURE,
        document_ordinal=None,
        canonical=canonical,
        max_row_bytes=value.max_facade_row_bytes,
    )
    object.__setattr__(mutated, "canonical", canonical[:-1] + bytes((canonical[-1] ^ 1,)))
    with pytest.raises(ValueError, match="diverge"):
        owner._publication_contains_v2(mutated)  # type: ignore[attr-defined]

    counters = value.handle._facade_counters_v2()
    assert counters.page_requests == counters.contains_requests == 0
