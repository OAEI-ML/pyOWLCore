from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from pyowl_core.backends import native_handoff_v2 as handoff_v2
from pyowl_core.backends.native_handoff import (
    NativeDocumentPublicationV1,
    NativeLoadReportPublicationV1,
)
from pyowl_core.backends.native_handoff_v2 import (
    NativeFacadeCollectionV2,
    NativeFacadePageRequestV2,
    NativeFacadePageV2,
    NativeFacadeScopeV2,
    NativeOriginRowV2,
    NativeSignatureKindV2,
    NativeSourceMapRowV2,
    decode_native_auxiliary_row_v2,
    encode_native_auxiliary_row_v2,
)
from pyowl_core.config import LoadOptions
from pyowl_core.model import IRI, Class, Declaration, canonical_bytes, structural_digest

from ._support import publication_fields
from ._support_v2 import FixtureKey, fixture_collections, publication, source_load_row_budget


def _raw_digest_group_fixture(
    collection: NativeFacadeCollectionV2,
    *,
    row_count: int,
) -> tuple[dict[str, object], dict[FixtureKey, tuple[bytes, ...]], bytes, tuple[bytes, ...]]:
    values = publication_fields()
    effective = dict(fixture_collections())
    raw = dict(effective)
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    options = cast(LoadOptions, values["load_options"])
    origin_key = (
        NativeFacadeCollectionV2.ORIGIN_ENTRIES,
        NativeFacadeScopeV2.DOCUMENT,
        0,
        NativeSignatureKindV2.ALL,
        True,
    )
    digest = cast(
        NativeOriginRowV2,
        decode_native_auxiliary_row_v2(
            NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            raw[origin_key][0],
            max_row_bytes=source_load_row_budget(values),
        ),
    ).digest
    occurrences = (9, 1, 1, 7, 0)
    rows: list[bytes] = []
    for index in range(row_count):
        occurrence = occurrences[index % len(occurrences)]
        value: NativeSourceMapRowV2 | NativeOriginRowV2
        if collection is NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES:
            value = NativeSourceMapRowV2(
                digest=digest,
                occurrence=occurrence,
                span=None,
                lexical=(("syntax", "same"),),
            )
        else:
            value = NativeOriginRowV2(
                digest=digest,
                document_key=documents[0].document_key,
                occurrence=occurrence,
                span=None,
            )
        encoded_collection, encoded = encode_native_auxiliary_row_v2(
            value,
            max_row_bytes=source_load_row_budget(values),
        )
        assert encoded_collection is collection
        rows.append(encoded)
    selected_rows = tuple(rows)
    raw[
        (
            collection,
            NativeFacadeScopeV2.DOCUMENT,
            0,
            NativeSignatureKindV2.ALL,
            True,
        )
    ] = selected_rows
    if collection is NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES:
        values["documents"] = (replace(documents[0], source_map_entry_count=row_count),)
        values["load_options"] = replace(options, preserve_source_map=True)
        values["capability_bits"] = cast(int, values["capability_bits"]) | 8
    else:
        values["documents"] = (replace(documents[0], origin_entry_count=row_count),)
    return values, raw, digest, selected_rows


def _raw_document_pages(
    *,
    collection: NativeFacadeCollectionV2,
    row_count: int,
    max_rows: int,
) -> tuple[tuple[bytes, ...], int]:
    values, raw, digest, expected = _raw_digest_group_fixture(
        collection,
        row_count=row_count,
    )
    published = publication(
        fixture_collections(),
        values=values,
        raw_document_collections=raw,
    )
    handle = published.handle._facade_document_v2(0)
    gathered: list[bytes] = []
    start = 0
    while True:
        page = handle._facade_page_v2(
            NativeFacadePageRequestV2(
                collection=collection,
                scope=NativeFacadeScopeV2.DOCUMENT,
                document_ordinal=0,
                start=start,
                max_rows=max_rows,
                max_bytes=published.max_facade_row_bytes * max_rows,
                max_row_bytes=published.max_facade_row_bytes,
                digest_filter=digest,
            )
        )
        gathered.extend(page.rows)
        if page.terminal:
            break
        assert page.next_cursor is not None
        start = page.next_cursor
    assert tuple(gathered) == expected
    return tuple(gathered), published.max_facade_row_bytes


def _two_origin_groups() -> tuple[
    dict[str, object],
    dict[FixtureKey, tuple[bytes, ...]],
    bytes,
    bytes,
]:
    values = publication_fields()
    collections = dict(fixture_collections())
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    report = cast(NativeLoadReportPublicationV1, values["report"])
    first_axiom = collections[
        (
            NativeFacadeCollectionV2.AXIOMS,
            NativeFacadeScopeV2.DOCUMENT,
            0,
            NativeSignatureKindV2.ALL,
            True,
        )
    ][0]
    second_value = Declaration(Class(IRI("urn:digest-filter:second")))
    second_axiom = canonical_bytes(second_value)
    axiom_rows = tuple(sorted((first_axiom, second_axiom)))
    for scope, ordinal in (
        (NativeFacadeScopeV2.DOCUMENT, 0),
        (NativeFacadeScopeV2.CLOSURE, None),
    ):
        collections[
            (
                NativeFacadeCollectionV2.AXIOMS,
                scope,
                ordinal,
                NativeSignatureKindV2.ALL,
                True,
            )
        ] = axiom_rows

    origin_key = (
        NativeFacadeCollectionV2.ORIGIN_ENTRIES,
        NativeFacadeScopeV2.DOCUMENT,
        0,
        NativeSignatureKindV2.ALL,
        True,
    )
    first_origin = collections[origin_key][0]
    first_digest = cast(
        NativeOriginRowV2,
        decode_native_auxiliary_row_v2(
            NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            first_origin,
            max_row_bytes=source_load_row_budget(values),
        ),
    ).digest
    second_digest = structural_digest(second_value)
    _collection, second_origin = encode_native_auxiliary_row_v2(
        NativeOriginRowV2(
            digest=second_digest,
            document_key=documents[0].document_key,
            occurrence=0,
            span=None,
        ),
        max_row_bytes=source_load_row_budget(values),
    )
    collections[origin_key] = tuple(sorted((first_origin, second_origin)))
    collections[
        (
            NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            NativeFacadeScopeV2.CLOSURE,
            None,
            NativeSignatureKindV2.ALL,
            True,
        )
    ] = collections[origin_key]
    values["documents"] = (replace(documents[0], axiom_count=2, origin_entry_count=2),)
    values["report"] = replace(report, effective_axiom_count=2)
    return values, collections, first_digest, second_digest


def test_digest_filter_uses_group_relative_total_and_cursor() -> None:
    values, collections, first_digest, second_digest = _two_origin_groups()
    value = publication(collections, values=values)
    for digest in (first_digest, second_digest):
        page = value.handle._facade_page_v2(
            NativeFacadePageRequestV2(
                collection=NativeFacadeCollectionV2.ORIGIN_ENTRIES,
                scope=NativeFacadeScopeV2.DOCUMENT,
                document_ordinal=0,
                start=0,
                max_rows=1,
                max_bytes=value.max_facade_row_bytes,
                max_row_bytes=value.max_facade_row_bytes,
                digest_filter=digest,
            )
        )
        assert page.digest_filter == digest
        assert page.total_count == 1
        assert page.terminal and page.next_cursor is None
        assert (
            cast(
                NativeOriginRowV2,
                decode_native_auxiliary_row_v2(
                    NativeFacadeCollectionV2.ORIGIN_ENTRIES,
                    page.rows[0],
                    max_row_bytes=value.max_facade_row_bytes,
                ),
            ).digest
            == digest
        )

    missing = value.handle._facade_page_v2(
        NativeFacadePageRequestV2(
            collection=NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            start=0,
            max_rows=1,
            max_bytes=value.max_facade_row_bytes,
            max_row_bytes=value.max_facade_row_bytes,
            digest_filter=b"z" * 32,
        )
    )
    assert missing.total_count == 0 and missing.terminal and not missing.rows


def test_digest_filter_is_exact_collection_limited_and_rows_must_match() -> None:
    with pytest.raises(ValueError, match="supported only"):
        NativeFacadePageRequestV2(
            collection=NativeFacadeCollectionV2.AXIOMS,
            scope=NativeFacadeScopeV2.CLOSURE,
            document_ordinal=None,
            start=0,
            max_rows=1,
            max_bytes=1,
            max_row_bytes=1,
            digest_filter=b"x" * 32,
        )
    with pytest.raises(ValueError, match="32 bytes"):
        NativeFacadePageRequestV2(
            collection=NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            start=0,
            max_rows=1,
            max_bytes=1,
            max_row_bytes=1,
            digest_filter=b"short",
        )

    values, collections, first_digest, second_digest = _two_origin_groups()
    origin_key = (
        NativeFacadeCollectionV2.ORIGIN_ENTRIES,
        NativeFacadeScopeV2.DOCUMENT,
        0,
        NativeSignatureKindV2.ALL,
        True,
    )
    wrong_row = next(
        row
        for row in collections[origin_key]
        if cast(
            NativeOriginRowV2,
            decode_native_auxiliary_row_v2(
                NativeFacadeCollectionV2.ORIGIN_ENTRIES,
                row,
                max_row_bytes=source_load_row_budget(values),
            ),
        ).digest
        == second_digest
    )
    with pytest.raises(ValueError, match="another digest group"):
        NativeFacadePageV2(
            collection=NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            start=0,
            max_rows=1,
            max_bytes=len(wrong_row),
            max_row_bytes=len(wrong_row),
            signature_kind=NativeSignatureKindV2.ALL,
            include_builtins=True,
            digest_filter=first_digest,
            total_count=1,
            next_cursor=None,
            terminal=True,
            page_bytes=len(wrong_row),
            rows=(wrong_row,),
        )


@pytest.mark.parametrize(
    "collection",
    (
        NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES,
        NativeFacadeCollectionV2.ORIGIN_ENTRIES,
    ),
)
def test_digest_filter_uses_logarithmic_bounds_and_a_page_sized_window(
    monkeypatch: pytest.MonkeyPatch,
    collection: NativeFacadeCollectionV2,
) -> None:
    row_count = 4096
    values, raw, digest, expected = _raw_digest_group_fixture(
        collection,
        row_count=row_count,
    )
    published = publication(
        fixture_collections(),
        values=values,
        raw_document_collections=raw,
    )
    handle = published.handle._facade_document_v2(0)
    prefix_calls = 0
    bounded_windows: list[tuple[int, int]] = []
    original_prefix = handoff_v2._digest_prefix_v2
    original_bounded = handoff_v2._bounded_page_rows_v2

    def tracked_prefix(row: bytes) -> bytes:
        nonlocal prefix_calls
        prefix_calls += 1
        return original_prefix(row)

    def tracked_bounded(
        rows: tuple[bytes, ...],
        start: int,
        stop: int,
        max_bytes: int,
    ) -> tuple[bytes, ...]:
        bounded_windows.append((start, stop))
        return original_bounded(rows, start, stop, max_bytes)

    monkeypatch.setattr(handoff_v2, "_digest_prefix_v2", tracked_prefix)
    monkeypatch.setattr(handoff_v2, "_bounded_page_rows_v2", tracked_bounded)
    selected_start = row_count // 2
    page = handle._facade_page_v2(
        NativeFacadePageRequestV2(
            collection=collection,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            start=selected_start,
            max_rows=1,
            max_bytes=published.max_facade_row_bytes,
            max_row_bytes=published.max_facade_row_bytes,
            digest_filter=digest,
        )
    )

    assert page.total_count == row_count
    assert page.rows == (expected[selected_start],)
    assert prefix_calls < 64
    assert bounded_windows == [(selected_start, selected_start + 1)]


@pytest.mark.parametrize(
    "collection",
    (
        NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES,
        NativeFacadeCollectionV2.ORIGIN_ENTRIES,
    ),
)
def test_raw_digest_groups_preserve_producer_multiplicity_across_page_sizes(
    collection: NativeFacadeCollectionV2,
) -> None:
    one_row_pages, _one_bound = _raw_document_pages(
        collection=collection,
        row_count=13,
        max_rows=1,
    )
    wider_pages, _wide_bound = _raw_document_pages(
        collection=collection,
        row_count=13,
        max_rows=7,
    )

    assert wider_pages == one_row_pages
    decoded = tuple(
        cast(
            NativeSourceMapRowV2 | NativeOriginRowV2,
            decode_native_auxiliary_row_v2(
                collection,
                row,
                max_row_bytes=max(_one_bound, _wide_bound),
            ),
        )
        for row in one_row_pages
    )
    assert tuple(row.occurrence for row in decoded[:5]) == (9, 1, 1, 7, 0)
    assert one_row_pages[1] == one_row_pages[2]
