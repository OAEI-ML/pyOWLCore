from __future__ import annotations

from dataclasses import replace

import pytest

import pyowl_core
from pyowl_core.adapters import CacheScope, ConsumerCacheKey, compare_cache_keys

OPTIONS = b"o" * 32


def snapshot() -> pyowl_core.OntologySnapshot:
    return pyowl_core.load_snapshot(
        b"Ontology(<urn:cache> Declaration(Class(<urn:cache#A>)))",
        options=pyowl_core.LoadOptions(backend=pyowl_core.BackendPreference.PYTHON),
    )


def key(
    view: pyowl_core.OntologyView,
    *,
    scope: CacheScope = CacheScope.LOGICAL,
) -> ConsumerCacheKey:
    return ConsumerCacheKey.for_view(
        view,
        consumer="fixture-consumer",
        consumer_version="1.2.3",
        consumer_api="fixture/2",
        compiler_schema="fixture-ir/7",
        compatibility_id="fixture-semantics/4",
        scope=scope,
        semantic_options_sha256=OPTIONS,
    )


def test_cache_key_binds_core_consumer_fingerprints_schemas_and_options() -> None:
    view = snapshot()
    semantic = key(view)
    structural = key(view, scope=CacheScope.STRUCTURAL)

    assert semantic.primary_fingerprint is view.logical_fingerprint
    assert structural.primary_fingerprint is view.structural_fingerprint
    assert semantic.signature_fingerprint is view.signature_fingerprint
    assert semantic.core_model_schema == pyowl_core.MODEL_SCHEMA_VERSION
    assert semantic.core_wire_format == pyowl_core.WIRE_FORMAT_VERSION
    assert semantic.core_adapter_protocol == pyowl_core.ADAPTER_PROTOCOL_VERSION
    assert semantic.hex == semantic.hex
    assert len(semantic.canonical_bytes) > 100
    assert b"path" not in semantic.canonical_bytes.lower()
    assert semantic.hex != structural.hex


def test_cache_comparison_is_exhaustive_and_fail_closed() -> None:
    expected = key(snapshot())
    actual = replace(
        expected,
        consumer_version="9.0",
        compiler_schema="old-ir/1",
        compatibility_id="old-semantics",
        core_package_version="0.1.0",
        semantic_options_sha256=b"x" * 32,
    )
    report = compare_cache_keys(actual, expected)

    assert not report.compatible
    assert {issue.field for issue in report.issues} == {
        "consumer_version",
        "compiler_schema",
        "compatibility_id",
        "core_package_version",
        "semantic_options_sha256",
    }
    with pytest.raises(pyowl_core.AdapterCompatibilityError) as caught:
        report.raise_for_errors()
    assert caught.value.code == "ADAPTER_CACHE_KEY_MISMATCH"
    assert caught.value.diagnostic is not None
    assert caught.value.diagnostic.details["issue_count"] == 5


def test_equal_wire_views_have_equal_keys_and_content_changes_do_not() -> None:
    direct = snapshot()
    decoded = pyowl_core.decode_snapshot(pyowl_core.encode_snapshot(direct))
    changed = pyowl_core.apply_delta(
        direct,
        pyowl_core.OntologyDelta(
            add_axioms=pyowl_core.CanonicalSet(
                (pyowl_core.Declaration(pyowl_core.Class(pyowl_core.IRI("urn:cache#B"))),)
            )
        ),
    )

    assert key(direct) == key(decoded)
    assert key(direct).hex == key(decoded).hex
    assert key(direct) != key(changed)
    assert key(direct, scope=CacheScope.STRUCTURAL) != key(changed, scope=CacheScope.STRUCTURAL)


@pytest.mark.parametrize("options", (b"", b"x" * 31, bytearray(b"x" * 32)))
def test_cache_key_rejects_non_digest_options(options: object) -> None:
    with pytest.raises(ValueError):
        ConsumerCacheKey.for_view(
            snapshot(),
            consumer="fixture",
            consumer_version="1",
            consumer_api="1",
            compiler_schema="1",
            compatibility_id="1",
            scope=CacheScope.LOGICAL,
            semantic_options_sha256=options,  # type: ignore[arg-type]
        )
