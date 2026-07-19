from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import fields, replace
from threading import Barrier
from typing import cast

import pytest

from pyowl_core.backends import native_handoff_v2 as handoff_v2
from pyowl_core.backends.native_handoff_v2 import (
    _REGISTERED_OWNER_TYPES_V2,
    NativeDocumentHandleV2,
    NativeFacadeCollectionV2,
    NativeFacadeContainsRequestV2,
    NativeFacadeCountersV2,
    NativeFacadePageRequestV2,
    NativeFacadePageV2,
    NativeFacadeScopeV2,
    NativeOriginRowV2,
    NativeSignatureKindV2,
    NativeSnapshotAttestationV2,
    NativeSnapshotHandleV2,
    _seal_native_snapshot_owner_v2,
    encode_native_auxiliary_row_v2,
)
from pyowl_core.config import LoadOptions
from pyowl_core.exceptions import BackendProtocolError, ClosedSnapshotError, ResourceLimitError
from pyowl_core.model import IRI, Class, Declaration, SubClassOf, canonical_bytes

from ._support import publication_fields
from ._support_v2 import attestation, fixture_max_row_bytes, publication

_FIXTURE_ROW_BOUND = fixture_max_row_bytes()


class _MaliciousOwner:
    def __init__(self, page: NativeFacadePageV2) -> None:
        self.selected_attestation = attestation()
        self.page = page
        self.closed = False
        self.attestation_calls = 0
        self.contains_calls = 0
        self.page_calls = 0

    def _publication_attestation_v2(self) -> NativeSnapshotAttestationV2:
        self.attestation_calls += 1
        return self.selected_attestation

    def _publication_closed_v2(self) -> bool:
        return self.closed

    def _publication_close_v2(self) -> None:
        self.closed = True

    def _publication_page_v2(self, _request: NativeFacadePageRequestV2) -> NativeFacadePageV2:
        self.page_calls += 1
        return self.page

    def _publication_contains_v2(self, _request: NativeFacadeContainsRequestV2) -> bool:
        self.contains_calls += 1
        return False

    def _publication_counters_v2(self) -> NativeFacadeCountersV2:
        return NativeFacadeCountersV2()


@contextmanager
def _malicious_handle(
    page: NativeFacadePageV2,
) -> Iterator[tuple[NativeSnapshotHandleV2, _MaliciousOwner]]:
    _REGISTERED_OWNER_TYPES_V2.add(_MaliciousOwner)
    owner = _MaliciousOwner(page)
    try:
        yield _seal_native_snapshot_owner_v2(owner), owner
    finally:
        _REGISTERED_OWNER_TYPES_V2.discard(_MaliciousOwner)


def _unchecked_page(**changes: object) -> NativeFacadePageV2:
    axiom = canonical_bytes(Declaration(Class(IRI("urn:malicious:A"))))
    values: dict[str, object] = {
        "collection": NativeFacadeCollectionV2.AXIOMS,
        "scope": NativeFacadeScopeV2.DOCUMENT,
        "document_ordinal": 0,
        "start": 0,
        "max_rows": 2,
        "max_bytes": 1024,
        "max_row_bytes": _FIXTURE_ROW_BOUND,
        "signature_kind": NativeSignatureKindV2.ALL,
        "include_builtins": True,
        "digest_filter": None,
        "total_count": 1,
        "next_cursor": None,
        "terminal": True,
        "page_bytes": len(axiom),
        "rows": (axiom,),
    }
    values.update(changes)
    page = object.__new__(NativeFacadePageV2)
    for item in fields(NativeFacadePageV2):
        object.__setattr__(
            page,
            item.name,
            values[item.name] if item.init else (),
        )
    return page


def _request() -> NativeFacadePageRequestV2:
    return NativeFacadePageRequestV2(
        collection=NativeFacadeCollectionV2.AXIOMS,
        scope=NativeFacadeScopeV2.DOCUMENT,
        document_ordinal=0,
        start=0,
        max_rows=2,
        max_bytes=1024,
        max_row_bytes=_FIXTURE_ROW_BOUND,
    )


@pytest.mark.parametrize(
    ("page", "message"),
    (
        (_unchecked_page(total_count=2), "terminal page"),
        (
            _unchecked_page(
                collection=NativeFacadeCollectionV2.ORIGIN_ENTRIES,
                rows=(b"bad",),
                page_bytes=3,
            ),
            "truncated V2 auxiliary row",
        ),
        (
            _unchecked_page(
                collection=NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS,
            ),
            "wrong category",
        ),
    ),
)
def test_handle_reconstructs_and_rejects_malicious_exact_page_objects(
    page: NativeFacadePageV2,
    message: str,
) -> None:
    with (
        _malicious_handle(page) as (handle, _owner),
        pytest.raises(
            BackendProtocolError,
            match="invalid page response",
        ) as raised,
    ):
        handle._facade_page_v2(_request())
    assert raised.value.code == "NATIVE_PAGE_RESPONSE"
    assert message in str(raised.value.__cause__)


def test_handle_rejects_duplicate_and_out_of_order_structural_rows() -> None:
    first = canonical_bytes(Declaration(Class(IRI("urn:malicious:A"))))
    second = canonical_bytes(Declaration(Class(IRI("urn:malicious:B"))))
    ordered = tuple(sorted((first, second)))
    for rows in ((ordered[0], ordered[0]), tuple(reversed(ordered))):
        page = _unchecked_page(
            total_count=2,
            rows=rows,
            page_bytes=sum(map(len, rows)),
        )
        with (
            _malicious_handle(page) as (handle, _owner),
            pytest.raises(BackendProtocolError) as raised,
        ):
            handle._facade_page_v2(_request())
        assert raised.value.code == "NATIVE_PAGE_RESPONSE"
        assert "ascending unique" in str(raised.value.__cause__)


def test_handle_rejects_valid_page_with_wrong_echoed_coordinates() -> None:
    page = NativeFacadePageV2(
        collection=NativeFacadeCollectionV2.AXIOMS,
        scope=NativeFacadeScopeV2.DOCUMENT,
        document_ordinal=0,
        start=1,
        max_rows=2,
        max_bytes=1024,
        max_row_bytes=_FIXTURE_ROW_BOUND,
        signature_kind=NativeSignatureKindV2.ALL,
        include_builtins=True,
        total_count=1,
        next_cursor=None,
        terminal=True,
        page_bytes=0,
        rows=(),
    )
    with (
        _malicious_handle(page) as (handle, _owner),
        pytest.raises(BackendProtocolError, match="echo exact"),
    ):
        handle._facade_page_v2(_request())


def test_document_ordinal_is_rejected_before_calling_owner() -> None:
    with _malicious_handle(_unchecked_page()) as (handle, owner):
        request = NativeFacadePageRequestV2(
            collection=NativeFacadeCollectionV2.AXIOMS,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=1,
            start=0,
            max_rows=1,
            max_bytes=1024,
            max_row_bytes=_FIXTURE_ROW_BOUND,
        )
        with pytest.raises(BackendProtocolError, match="ordinal is out of bounds"):
            handle._facade_page_v2(request)
        assert owner.page_calls == 0


def test_publication_bound_is_rejected_before_calling_owner() -> None:
    with _malicious_handle(_unchecked_page()) as (handle, owner):
        request = NativeFacadePageRequestV2(
            collection=NativeFacadeCollectionV2.AXIOMS,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            start=0,
            max_rows=1,
            max_bytes=1024,
            max_row_bytes=_FIXTURE_ROW_BOUND + 1,
        )
        with pytest.raises(BackendProtocolError, match="row bound"):
            handle._facade_page_v2(request)
        assert owner.page_calls == 0


def test_corrupted_exact_requests_are_reconstructed_before_any_owner_call() -> None:
    with _malicious_handle(_unchecked_page()) as (handle, owner):
        page_request = _request()
        object.__setattr__(page_request, "max_rows", 0)
        with pytest.raises(ValueError, match="max_rows"):
            handle._facade_page_v2(page_request)
        assert owner.attestation_calls == owner.page_calls == 0

        contains_request = NativeFacadeContainsRequestV2(
            collection=NativeFacadeCollectionV2.AXIOMS,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            canonical=canonical_bytes(Declaration(Class(IRI("urn:malicious:A")))),
            max_row_bytes=_FIXTURE_ROW_BOUND,
        )

        class EvilBytes(bytes):
            def __len__(self) -> int:
                raise AssertionError("hostile canonical bytes were inspected")

        object.__setattr__(contains_request, "canonical", EvilBytes(contains_request.canonical))
        with pytest.raises(TypeError, match="exact bytes"):
            handle._facade_contains_v2(contains_request)
        assert owner.attestation_calls == owner.contains_calls == 0


def test_owner_structural_page_is_decoded_under_attested_parse_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = publication_fields()
    options = cast(LoadOptions, values["load_options"])
    values["load_options"] = replace(
        options,
        limits=replace(options.limits, max_terms=3),
    )
    published = publication(values=values)
    hostile = canonical_bytes(
        SubClassOf(
            Class(IRI("urn:malicious:sub")),
            Class(IRI("urn:malicious:super")),
        )
    )

    def hostile_page(
        _owner: object,
        request: NativeFacadePageRequestV2,
    ) -> NativeFacadePageV2:
        return handoff_v2._unchecked_owner_page_v2(
            request,
            total_count=1,
            next_cursor=None,
            terminal=True,
            rows=(hostile,),
        )

    monkeypatch.setattr(
        handoff_v2._GeneratedNativeSnapshotOwnerV2,
        "_publication_page_v2",
        hostile_page,
    )
    with pytest.raises(BackendProtocolError) as raised:
        published.handle._facade_page_v2(
            NativeFacadePageRequestV2(
                collection=NativeFacadeCollectionV2.AXIOMS,
                scope=NativeFacadeScopeV2.DOCUMENT,
                document_ordinal=0,
                start=0,
                max_rows=1,
                max_bytes=published.max_facade_row_bytes,
                max_row_bytes=published.max_facade_row_bytes,
            )
        )
    assert raised.value.code == "NATIVE_PAGE_RESPONSE"
    row_error = raised.value.__cause__
    assert isinstance(row_error, ValueError)
    assert isinstance(row_error.__cause__, ResourceLimitError)
    assert row_error.__cause__.limit == "max_terms"


def test_contains_boundary_decodes_once_under_publication_limits_before_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = publication_fields()
    options = cast(LoadOptions, values["load_options"])
    values["load_options"] = replace(
        options,
        limits=replace(options.limits, max_terms=3),
    )
    published = publication(values=values)
    request = NativeFacadeContainsRequestV2(
        collection=NativeFacadeCollectionV2.AXIOMS,
        scope=NativeFacadeScopeV2.DOCUMENT,
        document_ordinal=0,
        canonical=canonical_bytes(
            SubClassOf(
                Class(IRI("urn:malicious:contains-sub")),
                Class(IRI("urn:malicious:contains-super")),
            )
        ),
        max_row_bytes=published.max_facade_row_bytes,
    )
    original_decode = cast(
        Callable[..., object],
        vars(handoff_v2)["decode_canonical"],
    )
    decode_calls = 0
    owner_calls = 0

    def counted_decode(*args: object, **kwargs: object) -> object:
        nonlocal decode_calls
        decode_calls += 1
        return original_decode(*args, **kwargs)

    def counted_owner(
        _owner: object,
        _request: NativeFacadeContainsRequestV2,
    ) -> bool:
        nonlocal owner_calls
        owner_calls += 1
        return False

    monkeypatch.setattr(handoff_v2, "decode_canonical", counted_decode)
    monkeypatch.setattr(
        handoff_v2._GeneratedNativeSnapshotOwnerV2,
        "_publication_contains_v2",
        counted_owner,
    )
    with pytest.raises(ValueError, match="valid model row") as raised:
        published.handle._facade_contains_v2(request)
    assert isinstance(raised.value.__cause__, ResourceLimitError)
    assert raised.value.__cause__.limit == "max_terms"
    assert decode_calls == 1
    assert owner_calls == 0


def test_capability_gating_happens_before_owner_call() -> None:
    page = _unchecked_page(collection=NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES)
    with _malicious_handle(page) as (handle, owner):
        owner.selected_attestation = replace(owner.selected_attestation, capability_bits=7)
        request = NativeFacadePageRequestV2(
            collection=NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            start=0,
            max_rows=1,
            max_bytes=1024,
            max_row_bytes=_FIXTURE_ROW_BOUND,
        )
        with pytest.raises(BackendProtocolError, match="not retained"):
            handle._facade_page_v2(request)
        assert owner.page_calls == 0


def test_handle_pins_total_across_pages() -> None:
    first = _unchecked_page(total_count=2, terminal=False, next_cursor=1)
    with _malicious_handle(first) as (handle, owner):
        handle._facade_page_v2(_request())
        second_row = canonical_bytes(Declaration(Class(IRI("urn:malicious:B"))))
        owner.page = _unchecked_page(
            start=1,
            total_count=3,
            terminal=False,
            next_cursor=2,
            rows=(second_row,),
            page_bytes=len(second_row),
        )
        with pytest.raises(BackendProtocolError, match="total changed"):
            handle._facade_page_v2(
                NativeFacadePageRequestV2(
                    collection=NativeFacadeCollectionV2.AXIOMS,
                    scope=NativeFacadeScopeV2.DOCUMENT,
                    document_ordinal=0,
                    start=1,
                    max_rows=2,
                    max_bytes=1024,
                    max_row_bytes=_FIXTURE_ROW_BOUND,
                )
            )


def test_handle_validates_contiguous_cross_page_order() -> None:
    rows = tuple(
        sorted(
            canonical_bytes(Declaration(Class(IRI(f"urn:malicious:{name}")))) for name in ("A", "B")
        )
    )
    first = _unchecked_page(
        total_count=2,
        terminal=False,
        next_cursor=1,
        rows=(rows[1],),
        page_bytes=len(rows[1]),
    )
    with _malicious_handle(first) as (handle, owner):
        handle._facade_page_v2(_request())
        owner.page = _unchecked_page(
            start=1,
            total_count=2,
            rows=(rows[0],),
            page_bytes=len(rows[0]),
        )
        with pytest.raises(BackendProtocolError, match="boundary"):
            handle._facade_page_v2(
                NativeFacadePageRequestV2(
                    collection=NativeFacadeCollectionV2.AXIOMS,
                    scope=NativeFacadeScopeV2.DOCUMENT,
                    document_ordinal=0,
                    start=1,
                    max_rows=2,
                    max_bytes=1024,
                    max_row_bytes=_FIXTURE_ROW_BOUND,
                )
            )


def test_snapshot_owner_rejects_noncanonical_effective_origin_cross_page_order() -> None:
    digest = b"o" * 32

    def origin(occurrence: int) -> bytes:
        _collection, encoded = encode_native_auxiliary_row_v2(
            NativeOriginRowV2(
                digest=digest,
                document_key="document.ofn",
                occurrence=occurrence,
                span=None,
            ),
            max_row_bytes=_FIXTURE_ROW_BOUND,
        )
        return encoded

    first_row = origin(2)
    second_row = origin(1)
    first = _unchecked_page(
        collection=NativeFacadeCollectionV2.ORIGIN_ENTRIES,
        max_rows=1,
        total_count=2,
        terminal=False,
        next_cursor=1,
        rows=(first_row,),
        page_bytes=len(first_row),
    )
    with _malicious_handle(first) as (handle, owner):
        handle._facade_page_v2(
            NativeFacadePageRequestV2(
                collection=NativeFacadeCollectionV2.ORIGIN_ENTRIES,
                scope=NativeFacadeScopeV2.DOCUMENT,
                document_ordinal=0,
                start=0,
                max_rows=1,
                max_bytes=1024,
                max_row_bytes=_FIXTURE_ROW_BOUND,
            )
        )
        owner.page = _unchecked_page(
            collection=NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            start=1,
            max_rows=1,
            total_count=2,
            rows=(second_row,),
            page_bytes=len(second_row),
        )
        with pytest.raises(BackendProtocolError, match="boundary") as raised:
            handle._facade_page_v2(
                NativeFacadePageRequestV2(
                    collection=NativeFacadeCollectionV2.ORIGIN_ENTRIES,
                    scope=NativeFacadeScopeV2.DOCUMENT,
                    document_ordinal=0,
                    start=1,
                    max_rows=1,
                    max_bytes=1024,
                    max_row_bytes=_FIXTURE_ROW_BOUND,
                )
            )
        assert raised.value.code == "NATIVE_PAGE_ORDER"


def test_publication_binding_validates_known_closure_total() -> None:
    value = publication()
    page = _unchecked_page(
        scope=NativeFacadeScopeV2.CLOSURE,
        document_ordinal=None,
        total_count=2,
        terminal=False,
        next_cursor=1,
    )
    request = NativeFacadePageRequestV2(
        collection=NativeFacadeCollectionV2.AXIOMS,
        scope=NativeFacadeScopeV2.CLOSURE,
        document_ordinal=None,
        start=0,
        max_rows=2,
        max_bytes=1024,
        max_row_bytes=_FIXTURE_ROW_BOUND,
    )
    with _malicious_handle(page) as (handle, _owner):
        handle._bind_publication_v2(
            value.documents,
            value.report,
            value.load_options,
            value.owl2_dl_report_summary,
            value.facade_cardinality_summary,
        )
        with pytest.raises(BackendProtocolError, match="publication metadata"):
            handle._facade_page_v2(request)


def test_generated_owner_serializes_close_against_concurrent_pages() -> None:
    value = publication()
    request = _request()

    def read() -> bool:
        try:
            return bool(value.handle._facade_page_v2(request).rows)
        except ClosedSnapshotError as error:
            assert error.code == "CLOSED_SNAPSHOT"
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(read) for _ in range(32)]
        value.handle.close()
        results = tuple(future.result() for future in futures)
    assert all(isinstance(item, bool) for item in results)
    assert value.handle.closed
    with pytest.raises(ClosedSnapshotError, match="closed"):
        value.handle._facade_page_v2(request)


def test_snapshot_close_and_document_fork_are_linearized() -> None:
    value = publication()
    barrier = Barrier(2)

    def fork_document() -> NativeDocumentHandleV2 | None:
        barrier.wait()
        try:
            return value.handle._facade_document_v2(0)
        except ClosedSnapshotError:
            return None

    def close_snapshot() -> None:
        barrier.wait()
        value.handle.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        forked = executor.submit(fork_document)
        closed = executor.submit(close_snapshot)
        document = forked.result()
        closed.result()
    assert value.handle.closed
    if document is not None:
        assert document._facade_page_v2(_request()).total_count == 1
        document.close()


def test_document_close_does_not_close_snapshot_or_sibling_owner() -> None:
    value = publication()
    first = value.handle._facade_document_v2(0)
    second = value.handle._facade_document_v2(0)
    first.close()
    assert first.closed
    assert not second.closed
    assert not value.handle.closed
    assert second._facade_page_v2(_request()).total_count == 1
    assert value.handle._facade_page_v2(_request()).total_count == 1


def test_pid_change_resets_process_epoch_state_and_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = publication()
    request = _request()
    value.handle._facade_page_v2(request)
    assert value.handle._facade_counters_v2().page_requests == 1
    original_pid = handoff_v2.os.getpid()  # type: ignore[attr-defined]
    monkeypatch.setattr(
        handoff_v2.os,  # type: ignore[attr-defined]
        "getpid",
        lambda: original_pid + 1,
    )
    value.handle._facade_page_v2(request)
    counters = value.handle._facade_counters_v2()
    assert counters.fork_reinitializations == 1
    assert counters.page_requests == counters.pages_returned == 1


def test_pid_change_resets_locks_but_preserves_closed_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = publication()
    value.handle.close()
    original_pid = handoff_v2.os.getpid()  # type: ignore[attr-defined]
    monkeypatch.setattr(
        handoff_v2.os,  # type: ignore[attr-defined]
        "getpid",
        lambda: original_pid + 1,
    )
    with pytest.raises(ClosedSnapshotError):
        value.handle._facade_page_v2(_request())
    assert value.handle.closed
    assert value.handle._facade_counters_v2().fork_reinitializations == 1


def test_malicious_counter_object_is_revalidated() -> None:
    page = _unchecked_page()
    with _malicious_handle(page) as (handle, owner):
        invalid = object.__new__(NativeFacadeCountersV2)
        for item in fields(NativeFacadeCountersV2):
            object.__setattr__(invalid, item.name, 0)
        object.__setattr__(invalid, "rows_emitted", 1)
        owner._publication_counters_v2 = lambda: invalid  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="per-collection"):
            handle._facade_counters_v2()
