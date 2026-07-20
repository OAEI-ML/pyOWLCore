from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError
from typing import Any, cast

import pytest

from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    EncodedStructuralView,
    ImportPolicy,
    LoadOptions,
    load_snapshot,
)
from pyowl_core.backends import native
from pyowl_core.exceptions import BackendProtocolError, ClosedSnapshotError
from tests.native.foundation._support import NativeTestExtension, load_extension
from tools.benchmark.comparators.common_contract import build_core_common_contract

SOURCE = (
    b"Ontology(<urn:retained-contract-summary> "
    b'Annotation(<urn:label> "summary") '
    b"Declaration(Class(<urn:retained-contract-summary:C>)) "
    b"Declaration(ObjectProperty(<urn:retained-contract-summary:p>)) "
    b"SubClassOf(<urn:retained-contract-summary:C> "
    b"ObjectSomeValuesFrom(<urn:retained-contract-summary:p> "
    b"<urn:retained-contract-summary:D>)))"
)

_PREPARED_HEADER_BYTES = 8 + 2 + 2
_PREPARED_FINGERPRINT_BYTES = 4 * (8 + 32)
_PREPARED_CONTENT_DIGEST_BYTES = 6 * 32
_FIRST_INVENTORY_OFFSET = (
    _PREPARED_HEADER_BYTES
    + _PREPARED_FINGERPRINT_BYTES
    + _PREPARED_CONTENT_DIGEST_BYTES
)


@pytest.fixture(scope="module", autouse=True)
def extension() -> NativeTestExtension:
    selected = load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available or "parse-functional-v1" not in result.features:
        pytest.skip(result.reason or "native Functional parser capability is unavailable")
    return selected


def _options(backend: BackendPreference) -> LoadOptions:
    return LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.IGNORE,
        backend=backend,
        collect_provenance=True,
    )


def test_retained_common_contract_summary_matches_scalar_reference_without_facade_work(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(SOURCE, options=_options(BackendPreference.PYTHON))
    selected = load_snapshot(SOURCE, options=_options(BackendPreference.NATIVE))
    method = cast(Any, selected)._native_common_contract_summary_v1
    handle = cast(Any, selected)._native_snapshot_state.owner.handle
    raw_owner = object.__getattribute__(handle, "_owner_v2")
    assert type(raw_owner) is cast(Any, extension)._NativeSnapshotHandle
    before_native = cast(Any, raw_owner)._publication_counters_v2()
    before_python = cast(Any, selected)._native_python_counters()

    summary = method()

    assert method() is summary
    assert cast(Any, raw_owner)._publication_counters_v2() == before_native
    assert cast(Any, selected)._native_python_counters() == before_python
    with pytest.raises(FrozenInstanceError):
        summary.root_count = 0

    expected = build_core_common_contract(
        reference,
        corpus_id="retained-contract-summary",
        source_sha256=hashlib.sha256(SOURCE).hexdigest(),
        options_sha256="00" * 32,
    )
    expected_fingerprints = cast(dict[str, dict[str, object]], expected["fingerprints"])
    for name, evidence in (
        ("document", summary.document_fingerprint),
        ("structural", summary.structural_fingerprint),
        ("logical", summary.logical_fingerprint),
        ("signature", summary.signature_fingerprint),
    ):
        expected_evidence = expected_fingerprints[name]
        assert evidence.preimage_bytes == expected_evidence["preimage_bytes"]
        assert evidence.sha256.hex() == expected_evidence["preimage_sha256"]
        assert evidence.sha256.hex() == expected_evidence["digest"]

    expected_inventories = cast(
        dict[str, dict[str, object]],
        cast(dict[str, object], expected["ledger"])["inventories"],
    )
    for name, inventory in (
        ("ontology_annotations", summary.ontology_annotations),
        ("axioms", summary.axioms),
        ("extensions", summary.extensions),
        ("signature", summary.signature),
    ):
        expected_inventory = expected_inventories[name]
        assert inventory.count == expected_inventory["count"]
        assert inventory.canonical_bytes == expected_inventory["canonical_bytes"]
        assert inventory.transcript_bytes == expected_inventory["transcript_bytes"]
        assert inventory.sha256.hex() == expected_inventory["sha256"]

    direct = selected.view(EncodedStructuralView)
    assert summary.root_count == len(direct.buffers["root_ids"]) // 4
    assert summary.node_count == len(direct.buffers["node_tags"]) // 2


@pytest.mark.parametrize(
    ("tamper", "expected_code"),
    (
        ("schema", "NATIVE_PARSE_VERSION"),
        ("inventory_count", "NATIVE_PARSE_MODEL"),
        ("inventory_digest", "NATIVE_PROTOCOL"),
    ),
)
def test_retained_common_contract_summary_tampering_fails_closed(
    tamper: str,
    expected_code: str,
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    prepare = cast(Any, extension)._prepare_parsed_structural_snapshot_v2

    def tampered(*arguments: object, **keywords: object) -> bytes:
        encoded = bytearray(prepare(*arguments, **keywords))
        if tamper == "schema":
            encoded[8:10] = (1).to_bytes(2, "little")
        elif tamper == "inventory_count":
            count = int.from_bytes(
                encoded[_FIRST_INVENTORY_OFFSET : _FIRST_INVENTORY_OFFSET + 8],
                "little",
            )
            encoded[_FIRST_INVENTORY_OFFSET : _FIRST_INVENTORY_OFFSET + 8] = (
                count + 1
            ).to_bytes(8, "little")
        else:
            encoded[_FIRST_INVENTORY_OFFSET + 24] ^= 1
        return bytes(encoded)

    monkeypatch.setattr(
        cast(Any, extension),
        "_prepare_parsed_structural_snapshot_v2",
        tampered,
    )
    with pytest.raises(BackendProtocolError) as raised:
        load_snapshot(SOURCE, options=_options(BackendPreference.NATIVE))
    assert raised.value.code == expected_code


def test_retained_common_contract_summary_obeys_snapshot_lifetime(
    extension: NativeTestExtension,
) -> None:
    selected = load_snapshot(SOURCE, options=_options(BackendPreference.NATIVE))
    method = cast(Any, selected)._native_common_contract_summary_v1
    assert method().schema == 1

    selected.close()

    with pytest.raises(ClosedSnapshotError):
        method()
