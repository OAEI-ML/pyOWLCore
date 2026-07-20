from __future__ import annotations

import hashlib

import pytest

import pyowl_core
from pyowl_core.backends.native_views import (
    ENCODED_STRUCTURAL_DESCRIPTOR_V1,
    EncodedStructuralViewV1,
)
from pyowl_core.document.native_storage import ontology_snapshot_from_native_publication_v2
from pyowl_core.exceptions import ResourceLimitError
from pyowl_core.index import IndexCachePolicy, index_cache_report
from tests.native.encoded_views._independent import decode_root_canonical_bytes
from tests.native.encoded_views._support import (
    complete_constructor_snapshot,
    scalar_root_bytes,
)
from tests.native.publication_handoff._support_v2 import publication

_TOP_LEVEL_DESCRIPTOR_DIGEST: bytes = pyowl_core.ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1
_REQUEST_TYPE_DESCRIPTOR_DIGEST: bytes = pyowl_core.EncodedStructuralView.DESCRIPTOR_SHA256


def test_frozen_descriptor_digest_is_available_without_building_a_view() -> None:
    assert _TOP_LEVEL_DESCRIPTOR_DIGEST is _REQUEST_TYPE_DESCRIPTOR_DIGEST
    assert type(_TOP_LEVEL_DESCRIPTOR_DIGEST) is bytes
    assert len(_TOP_LEVEL_DESCRIPTOR_DIGEST) == 32
    assert (
        hashlib.sha256(ENCODED_STRUCTURAL_DESCRIPTOR_V1).digest()
        == _TOP_LEVEL_DESCRIPTOR_DIGEST
    )


def test_public_request_type_routes_through_cached_snapshot_view_boundary() -> None:
    snapshot = complete_constructor_snapshot()

    encoded = snapshot.view(pyowl_core.EncodedStructuralView, schema_version=1)

    assert pyowl_core.EncodedStructuralView is EncodedStructuralViewV1
    assert encoded.owner is snapshot
    assert encoded is snapshot.view(pyowl_core.EncodedStructuralView, schema_version=1)
    assert decode_root_canonical_bytes(encoded.buffers) == scalar_root_bytes(snapshot)
    assert encoded.schema_name not in snapshot.capabilities.encoded_view_schemas

    with pytest.raises(ValueError, match="schema_version"):
        snapshot.view(pyowl_core.EncodedStructuralView, schema_version=2)


def test_retained_v2_snapshot_uses_the_same_public_fallback_boundary() -> None:
    snapshot = ontology_snapshot_from_native_publication_v2(publication())

    encoded = snapshot.view(pyowl_core.EncodedStructuralView, schema_version=1)

    assert encoded.owner is snapshot
    assert encoded is snapshot.view(pyowl_core.EncodedStructuralView, schema_version=1)
    assert decode_root_canonical_bytes(encoded.buffers) == scalar_root_bytes(snapshot)
    assert encoded.schema_name not in snapshot.capabilities.encoded_view_schemas


def test_tight_cache_policy_fails_before_retained_scalar_traversal() -> None:
    published = publication()
    snapshot = ontology_snapshot_from_native_publication_v2(published)
    before = published.handle._facade_counters_v2()
    before_python = snapshot._native_python_counters()  # type: ignore[attr-defined]

    with pytest.raises(ResourceLimitError) as failure:
        snapshot.view(
            pyowl_core.EncodedStructuralView,
            schema_version=1,
            cache_policy=IndexCachePolicy(max_bytes=8),
        )

    after = published.handle._facade_counters_v2()
    after_python = snapshot._native_python_counters()  # type: ignore[attr-defined]
    report = index_cache_report(snapshot)
    assert failure.value.limit == "max_index_bytes"
    assert after.page_requests == before.page_requests
    assert after_python.model_rows_materialized == before_python.model_rows_materialized
    assert report.retained_entries == 0
    assert report.reserved_bytes == 0
