from __future__ import annotations

from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest

import pyowl_core.backends.native_views as native_views_module
from pyowl_core import (
    IRI,
    AxiomScope,
    BackendPreference,
    CancellationSource,
    Class,
    Declaration,
    ImportPolicy,
    LoadOptions,
    OntologyView,
    OperationCancelledError,
    ParseLimits,
    ResourceLimitError,
    load_snapshot,
)
from pyowl_core.backends.native_handoff_v2 import (
    NativeFacadeScopeV2,
    _seal_native_snapshot_owner_v2,
)
from pyowl_core.backends.native_views import EncodedStructuralViewV2
from pyowl_core.index.cache import create_index_cache, request_index_view
from pyowl_core.model import canonical_bytes
from tests.native.encoded_views._independent import decode_root_canonical_bytes
from tests.native.foundation._support import NativeTestExtension, load_extension


@pytest.fixture(scope="module")
def extension() -> NativeTestExtension:
    selected = load_extension()
    required = (
        "_encoded_structural_fixture_v2",
        "_encoded_structural_columns_v2",
    )
    if any(not hasattr(selected, name) for name in required):
        pytest.skip("selected native artifact lacks the WP17 direct-column hooks")
    return selected


class _NativeViewProxy:
    """Public OntologyView shell around the typed native test owner."""

    def __init__(self, raw_owner: object) -> None:
        self.limits = ParseLimits()
        self._index_cache = create_index_cache(self.limits)
        handle = _seal_native_snapshot_owner_v2(raw_owner)
        self._native_snapshot_state = SimpleNamespace(owner=SimpleNamespace(handle=handle))
        self.scalar_calls = 0
        self._fallback = load_snapshot(
            b"Ontology(<urn:encoded-view:test> Declaration(Class(<urn:encoded-view:fixture>)))",
            options=LoadOptions(
                backend=BackendPreference.PYTHON,
                imports=ImportPolicy.IGNORE,
            ),
        )

    @property
    def capabilities(self):  # type: ignore[no-untyped-def]
        return self._fallback.capabilities

    @property
    def origin_index(self):  # type: ignore[no-untyped-def]
        return self._fallback.origin_index

    @property
    def is_complete(self) -> bool:
        return self._fallback.is_complete

    @property
    def structural_fingerprint(self):  # type: ignore[no-untyped-def]
        return self._fallback.structural_fingerprint

    @property
    def logical_fingerprint(self):  # type: ignore[no-untyped-def]
        return self._fallback.logical_fingerprint

    @property
    def signature_fingerprint(self):  # type: ignore[no-untyped-def]
        return self._fallback.signature_fingerprint

    @property
    def report(self):  # type: ignore[no-untyped-def]
        return self._fallback.report

    def _forbid_scalar(self) -> NoReturn:
        self.scalar_calls += 1
        raise AssertionError("native direct publication crossed scalar traversal")

    def iter_axioms(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        del args, kwargs
        self._forbid_scalar()

    def iter_extensions(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        del args, kwargs
        self._forbid_scalar()

    def ontology_annotations(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        del args, kwargs
        self._forbid_scalar()

    def contains(self, *args: object, **kwargs: object) -> bool:
        del args, kwargs
        self._forbid_scalar()

    def signature(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        del args, kwargs
        self._forbid_scalar()

    def view(self, view_type: type[object], /, **options: object):  # type: ignore[no-untyped-def]
        return request_index_view(self, view_type, options)

    def _native_scope(
        self,
        scope: AxiomScope,
        document_key: str | None,
    ) -> tuple[NativeFacadeScopeV2, int | None]:
        if scope is AxiomScope.CLOSURE:
            assert document_key is None
            return NativeFacadeScopeV2.CLOSURE, None
        return NativeFacadeScopeV2.DOCUMENT, 0


def _proxy(extension: NativeTestExtension) -> tuple[_NativeViewProxy, object]:
    raw_owner = cast(Any, extension)._encoded_structural_fixture_v2()
    return _NativeViewProxy(raw_owner), raw_owner


def test_public_view_uses_native_direct_columns_without_scalar_callbacks(
    extension: NativeTestExtension,
) -> None:
    owner, raw_owner = _proxy(extension)
    assert isinstance(owner, OntologyView)
    before = cast(Any, raw_owner)._publication_counters_v2()

    closure = owner.view(EncodedStructuralViewV2)
    root = owner.view(EncodedStructuralViewV2, scope=AxiomScope.ROOT)
    document = owner.view(
        EncodedStructuralViewV2,
        scope=AxiomScope.DOCUMENT,
        document_key="d1:test",
    )
    after = cast(Any, raw_owner)._publication_counters_v2()
    expected = canonical_bytes(Declaration(Class(IRI("urn:encoded-view:fixture"))))

    assert owner.scalar_calls == 0
    assert after.encoded_view_requests == before.encoded_view_requests + 3
    for encoded in (closure, root, document):
        assert encoded.owner is owner
        assert tuple(segment.role for segment in encoded.segments) == (1,)
        assert decode_root_canonical_bytes(encoded.buffers) == ((2, expected),)
        exporters = {id(value.obj) for value in encoded.buffers.values()}
        assert len(exporters) == 1
        assert all(type(value.obj) is bytes for value in encoded.buffers.values())

    cast(Any, raw_owner)._publication_close_v2()
    assert decode_root_canonical_bytes(closure.buffers) == ((2, expected),)


def test_direct_column_validation_never_decodes_structural_roots(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    owner, _raw_owner = _proxy(extension)

    def unexpected(*_arguments: object, **_keywords: object) -> object:
        raise AssertionError("direct column validation decoded a structural root")

    monkeypatch.setattr(native_views_module, "decode_canonical", unexpected)

    encoded = owner.view(EncodedStructuralViewV2)

    assert encoded.owner is owner
    assert owner.scalar_calls == 0


def test_public_native_direct_limits_and_cancellation_fail_without_fallback(
    extension: NativeTestExtension,
) -> None:
    limited_owner, limited_raw = _proxy(extension)
    limited_before = cast(Any, limited_raw)._publication_counters_v2()
    with pytest.raises(ResourceLimitError) as limited:
        limited_owner.view(
            EncodedStructuralViewV2,
            limits=ParseLimits(max_index_bytes=1),
        )
    assert limited.value.code == "NATIVE_WIRE_LIMIT"
    assert limited_owner.scalar_calls == 0
    assert (
        cast(Any, limited_raw)._publication_counters_v2().encoded_view_requests
        == limited_before.encoded_view_requests
    )

    cancelled_owner, cancelled_raw = _proxy(extension)
    source = CancellationSource()
    source.cancel("direct view cancelled")
    cancelled_before = cast(Any, cancelled_raw)._publication_counters_v2()
    with pytest.raises(OperationCancelledError, match="direct view cancelled"):
        cancelled_owner.view(
            EncodedStructuralViewV2,
            cancellation_token=source.token,
        )
    assert cancelled_owner.scalar_calls == 0
    assert (
        cast(Any, cancelled_raw)._publication_counters_v2().encoded_view_requests
        == cancelled_before.encoded_view_requests
    )
