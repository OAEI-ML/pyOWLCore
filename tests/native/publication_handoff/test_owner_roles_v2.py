from __future__ import annotations

from dataclasses import replace
from typing import cast

from pyowl_core.backends.native_handoff import NativeDocumentPublicationV1
from pyowl_core.backends.native_handoff_v2 import (
    NativeFacadeCollectionV2,
    NativeFacadePageRequestV2,
    NativeFacadeScopeV2,
    NativeOriginRowV2,
    NativeSignatureKindV2,
    NativeSnapshotPublicationV2,
    decode_native_auxiliary_row_v2,
    encode_native_auxiliary_row_v2,
)
from pyowl_core.model import (
    IRI,
    AnonymousIndividual,
    Class,
    ClassAssertion,
    canonical_bytes,
    decode_canonical,
    re_scope_anonymous,
    structural_digest,
)

from ._support import publication_fields
from ._support_v2 import FixtureKey, fixture_collections, publication, source_load_row_budget


def _anonymous_owner_fixture(
    *,
    raw_origin_present: bool,
) -> tuple[
    dict[str, object],
    dict[FixtureKey, tuple[bytes, ...]],
    dict[FixtureKey, tuple[bytes, ...]],
    bytes,
    bytes,
    bytes,
    bytes,
]:
    values = publication_fields()
    document = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])[0]
    raw_individual = AnonymousIndividual(b"r" * 32, b"same-local-key")
    effective_individual, _record = re_scope_anonymous(raw_individual, b"s" * 32)
    class_value = Class(IRI("urn:owner-role:AnonymousClass"))
    raw_value = ClassAssertion(class_value, raw_individual)
    effective_value = ClassAssertion(class_value, effective_individual)
    raw_axiom = canonical_bytes(raw_value)
    effective_axiom = canonical_bytes(effective_value)
    _raw_collection, raw_origin = encode_native_auxiliary_row_v2(
        NativeOriginRowV2(
            digest=structural_digest(raw_value),
            document_key=document.document_key,
            occurrence=0,
            span=None,
        ),
        max_row_bytes=source_load_row_budget(values),
    )
    _effective_collection, effective_origin = encode_native_auxiliary_row_v2(
        NativeOriginRowV2(
            digest=structural_digest(effective_value),
            document_key=document.document_key,
            occurrence=0,
            span=None,
        ),
        max_row_bytes=source_load_row_budget(values),
    )
    raw = dict(fixture_collections())
    effective = dict(fixture_collections())
    document_axiom_key: FixtureKey = (
        NativeFacadeCollectionV2.AXIOMS,
        NativeFacadeScopeV2.DOCUMENT,
        0,
        NativeSignatureKindV2.ALL,
        True,
    )
    closure_axiom_key: FixtureKey = (
        NativeFacadeCollectionV2.AXIOMS,
        NativeFacadeScopeV2.CLOSURE,
        None,
        NativeSignatureKindV2.ALL,
        True,
    )
    document_origin_key: FixtureKey = (
        NativeFacadeCollectionV2.ORIGIN_ENTRIES,
        NativeFacadeScopeV2.DOCUMENT,
        0,
        NativeSignatureKindV2.ALL,
        True,
    )
    closure_origin_key: FixtureKey = (
        NativeFacadeCollectionV2.ORIGIN_ENTRIES,
        NativeFacadeScopeV2.CLOSURE,
        None,
        NativeSignatureKindV2.ALL,
        True,
    )
    raw[document_axiom_key] = (raw_axiom,)
    raw[document_origin_key] = (raw_origin,) if raw_origin_present else ()
    effective[document_axiom_key] = (effective_axiom,)
    effective[closure_axiom_key] = (effective_axiom,)
    effective[document_origin_key] = (effective_origin,)
    effective[closure_origin_key] = (effective_origin,)
    values["documents"] = (replace(document, origin_entry_count=int(raw_origin_present)),)
    return (
        values,
        effective,
        raw,
        raw_axiom,
        effective_axiom,
        raw_origin,
        effective_origin,
    )


def _page_request(
    value: NativeSnapshotPublicationV2,
    collection: NativeFacadeCollectionV2,
) -> NativeFacadePageRequestV2:
    return NativeFacadePageRequestV2(
        collection=collection,
        scope=NativeFacadeScopeV2.DOCUMENT,
        document_ordinal=0,
        start=0,
        max_rows=8,
        max_bytes=value.max_facade_row_bytes,
        max_row_bytes=value.max_facade_row_bytes,
    )


def test_document_owner_is_raw_while_snapshot_owner_is_effective() -> None:
    values, effective, raw, raw_axiom, effective_axiom, raw_origin, effective_origin = (
        _anonymous_owner_fixture(raw_origin_present=True)
    )
    value = publication(effective, values=values, raw_document_collections=raw)
    document_handle = value.handle._facade_document_v2(0)
    assert document_handle._facade_page_v2(
        _page_request(value, NativeFacadeCollectionV2.AXIOMS)
    ).rows == (raw_axiom,)
    assert value.handle._facade_page_v2(
        _page_request(value, NativeFacadeCollectionV2.AXIOMS)
    ).rows == (effective_axiom,)
    assert document_handle._facade_page_v2(
        _page_request(value, NativeFacadeCollectionV2.ORIGIN_ENTRIES)
    ).rows == (raw_origin,)
    assert value.handle._facade_page_v2(
        _page_request(value, NativeFacadeCollectionV2.ORIGIN_ENTRIES)
    ).rows == (effective_origin,)
    assert decode_canonical(raw_axiom) != decode_canonical(effective_axiom)
    assert value.root_table_sha256 != value.effective_root_table_sha256
    assert value.provenance_manifest_sha256 != value.effective_origin_manifest_sha256
    assert (
        value.documents[0].document_fingerprint
        == cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])[
            0
        ].document_fingerprint
    )


def test_synthesized_effective_origin_count_is_bound_separately_from_raw() -> None:
    values, effective, raw, _raw_axiom, _effective_axiom, _raw_origin, effective_origin = (
        _anonymous_owner_fixture(raw_origin_present=False)
    )
    value = publication(effective, values=values, raw_document_collections=raw)
    document_handle = value.handle._facade_document_v2(0)
    raw_page = document_handle._facade_page_v2(
        _page_request(value, NativeFacadeCollectionV2.ORIGIN_ENTRIES)
    )
    effective_page = value.handle._facade_page_v2(
        _page_request(value, NativeFacadeCollectionV2.ORIGIN_ENTRIES)
    )
    assert raw_page.total_count == 0 and not raw_page.rows
    assert effective_page.total_count == 1 and effective_page.rows == (effective_origin,)
    assert value.documents[0].origin_entry_count == 0
    assert value.facade_cardinality_summary.documents[0].effective_origin_count == 1


def test_raw_origin_producer_order_and_multiplicity_survive_all_page_sizes() -> None:
    values = publication_fields()
    effective = fixture_collections()
    raw = dict(effective)
    origin_key: FixtureKey = (
        NativeFacadeCollectionV2.ORIGIN_ENTRIES,
        NativeFacadeScopeV2.DOCUMENT,
        0,
        NativeSignatureKindV2.ALL,
        True,
    )
    base = cast(
        NativeOriginRowV2,
        decode_native_auxiliary_row_v2(
            NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            effective[origin_key][0],
            max_row_bytes=source_load_row_budget(values),
        ),
    )

    def encoded(occurrence: int) -> bytes:
        _collection, row = encode_native_auxiliary_row_v2(
            NativeOriginRowV2(
                digest=base.digest,
                document_key=base.document_key,
                occurrence=occurrence,
                span=base.span,
            ),
            max_row_bytes=source_load_row_budget(values),
        )
        return row

    expected = (encoded(2), encoded(1), encoded(1))
    raw[origin_key] = expected
    document = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])[0]
    values["documents"] = (replace(document, origin_entry_count=len(expected)),)
    published = publication(
        effective,
        values=values,
        raw_document_collections=raw,
    )
    document_handle = published.handle._facade_document_v2(0)

    def collect(max_rows: int) -> tuple[bytes, ...]:
        rows: list[bytes] = []
        cursor = 0
        while True:
            page = document_handle._facade_page_v2(
                NativeFacadePageRequestV2(
                    collection=NativeFacadeCollectionV2.ORIGIN_ENTRIES,
                    scope=NativeFacadeScopeV2.DOCUMENT,
                    document_ordinal=0,
                    start=cursor,
                    max_rows=max_rows,
                    max_bytes=4096,
                    max_row_bytes=published.max_facade_row_bytes,
                )
            )
            rows.extend(page.rows)
            if page.next_cursor is None:
                return tuple(rows)
            cursor = page.next_cursor

    assert collect(1) == expected
    assert collect(8) == expected


def test_anonymous_rescoping_is_injective_for_colliding_local_keys() -> None:
    first = AnonymousIndividual(b"a" * 32, b"same")
    second = AnonymousIndividual(b"b" * 32, b"same")
    first_moved, _first_record = re_scope_anonymous(first, b"s" * 32)
    second_moved, _second_record = re_scope_anonymous(second, b"s" * 32)
    assert first_moved != second_moved
    assert first_moved.local_key != second_moved.local_key
