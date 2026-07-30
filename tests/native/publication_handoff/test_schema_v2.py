from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any, cast

try:
    import tomllib  # type: ignore[import-untyped, unused-ignore]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[import-not-found, import-untyped, unused-ignore]

from pyowl_core.backends.native_handoff import NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256
from pyowl_core.backends.native_handoff_v2 import (
    NATIVE_AUXILIARY_CODEC_SCHEMA_SHA256_V2,
    NATIVE_CLOSURE_FACADE_CARDINALITIES_FIELDS_V2,
    NATIVE_DIAGNOSTIC_REFERENCE_KINDS_FIELDS_V2,
    NATIVE_DIAGNOSTIC_REFERENCE_SIDECARS_FIELDS_V2,
    NATIVE_DOCUMENT_FACADE_CARDINALITIES_FIELDS_V2,
    NATIVE_DOCUMENT_HANDLE_MEMBERS_V2,
    NATIVE_FACADE_ACCESS_SCHEMA_SHA256_V2,
    NATIVE_FACADE_CARDINALITY_SUMMARY_FIELDS_V2,
    NATIVE_FACADE_CONTAINS_REQUEST_FIELDS_V2,
    NATIVE_FACADE_COUNTER_FIELDS_V2,
    NATIVE_FACADE_PAGE_FIELDS_V2,
    NATIVE_FACADE_PAGE_REQUEST_FIELDS_V2,
    NATIVE_OWL2_DL_REPORT_SUMMARY_FIELDS_V2,
    NATIVE_PYTHON_FACADE_COUNTER_FIELDS_V2,
    NATIVE_SNAPSHOT_ATTESTATION_FIELDS_V2,
    NATIVE_SNAPSHOT_HANDLE_MEMBERS_V2,
    NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2,
    NATIVE_SNAPSHOT_PUBLICATION_FIELDS_V2,
    NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2,
    NativeClosureFacadeCardinalitiesV2,
    NativeDiagnosticReferenceKindsV2,
    NativeDiagnosticReferenceSidecarsV2,
    NativeDocumentFacadeCardinalitiesV2,
    NativeDocumentHandleV2,
    NativeFacadeCardinalitySummaryV2,
    NativeFacadeContainsRequestV2,
    NativeFacadeCountersV2,
    NativeFacadePageRequestV2,
    NativeFacadePageV2,
    NativeOWL2DLReportSummaryV2,
    NativePythonFacadeCountersV2,
    NativeSnapshotAttestationV2,
    NativeSnapshotPublicationV2,
    native_auxiliary_codec_schema_semantics_v2,
    native_facade_access_schema_semantics_v2,
    native_snapshot_publication_schema_semantics_v2,
)
from tools.schema.native_snapshot_publication_v2 import (
    render_native_snapshot_publication_v2_schema,
)

ROOT = Path(__file__).parents[3]
SCHEMA = ROOT / "schemas" / "native-snapshot-publication-v2.toml"
RUST_FACADE = ROOT / "native" / "src" / "publication" / "facade_v2.rs"
RUST_AUXILIARY = ROOT / "native" / "src" / "publication" / "auxiliary.rs"
_CANONICAL_PREFIX = b"pyowl-core:typed-toml-tree:v1\x00"


def _load_schema() -> dict[str, Any]:
    with SCHEMA.open("rb") as stream:
        return cast(dict[str, Any], tomllib.load(stream))


def _frame(value: bytes) -> bytes:
    return str(len(value)).encode("ascii") + b":" + value


def _independent_canonical(value: object) -> bytes:
    if isinstance(value, bool):
        return b"b1" if value else b"b0"
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii") + b";"
    if isinstance(value, str):
        return b"s" + _frame(value.encode("utf-8"))
    if isinstance(value, list):
        return b"l" + _frame(b"".join(_frame(_independent_canonical(item)) for item in value))
    if isinstance(value, Mapping):
        keys = list(value)
        assert all(isinstance(key, str) for key in keys)
        keys.sort(key=lambda item: cast(str, item).encode("utf-8"))
        body = bytearray()
        for raw_key in keys:
            key = cast(str, raw_key)
            body.extend(_frame(key.encode("utf-8")))
            body.extend(_frame(_independent_canonical(value[key])))
        return b"m" + _frame(bytes(body))
    raise TypeError(type(value).__qualname__)


def _rows(
    section: Mapping[str, object],
    tail: str,
    field_name: str = "fields",
) -> tuple[tuple[object, ...], ...]:
    rows = cast(list[dict[str, object]], section[field_name])
    return tuple((row["ordinal"], row["name"], row["type"], row[tail]) for row in rows)


def _rust_digest(name: str) -> bytes:
    matches = []
    for path in (RUST_FACADE, RUST_AUXILIARY):
        source = path.read_text(encoding="utf-8")
        if matched := re.search(rf"\b{name}\b[^=]*=\s*\[(.*?)\];", source, re.DOTALL):
            matches.append(matched)
    assert len(matches) == 1, f"expected one Rust V2 digest constant {name}, found {len(matches)}"
    matched = matches[0]
    octets = bytes(int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{2})", matched[1]))
    assert len(octets) == 32, f"Rust V2 digest constant {name} is not bytes32"
    return octets


def test_v2_toml_is_the_exact_full_typed_semantic_tree() -> None:
    schema = _load_schema()
    recorded = cast(str, schema.pop("ledger_sha256"))
    semantics = native_snapshot_publication_schema_semantics_v2()
    assert schema == semantics
    independent = hashlib.sha256(_CANONICAL_PREFIX + _independent_canonical(semantics)).digest()
    assert independent == NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2
    assert recorded == independent.hex()
    assert schema["shared_metadata_ledger_sha256"] == (
        NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256.hex()
    )


def test_v2_toml_has_no_generated_ledger_drift() -> None:
    assert SCHEMA.read_text(encoding="utf-8") == (render_native_snapshot_publication_v2_schema())


def test_v2_named_records_and_handle_match_the_ledger_exactly() -> None:
    schema = _load_schema()
    records = (
        ("envelope", NATIVE_SNAPSHOT_PUBLICATION_FIELDS_V2, NativeSnapshotPublicationV2),
        ("attestation", NATIVE_SNAPSHOT_ATTESTATION_FIELDS_V2, NativeSnapshotAttestationV2),
        ("page_request", NATIVE_FACADE_PAGE_REQUEST_FIELDS_V2, NativeFacadePageRequestV2),
        ("page", NATIVE_FACADE_PAGE_FIELDS_V2, NativeFacadePageV2),
        (
            "contains_request",
            NATIVE_FACADE_CONTAINS_REQUEST_FIELDS_V2,
            NativeFacadeContainsRequestV2,
        ),
        ("counters", NATIVE_FACADE_COUNTER_FIELDS_V2, NativeFacadeCountersV2),
        (
            "python_counters",
            NATIVE_PYTHON_FACADE_COUNTER_FIELDS_V2,
            NativePythonFacadeCountersV2,
        ),
        (
            "owl2_dl_report_summary",
            NATIVE_OWL2_DL_REPORT_SUMMARY_FIELDS_V2,
            NativeOWL2DLReportSummaryV2,
        ),
        (
            "diagnostic_reference_sidecars",
            NATIVE_DIAGNOSTIC_REFERENCE_SIDECARS_FIELDS_V2,
            NativeDiagnosticReferenceSidecarsV2,
        ),
        (
            "facade_cardinality_summary",
            NATIVE_FACADE_CARDINALITY_SUMMARY_FIELDS_V2,
            NativeFacadeCardinalitySummaryV2,
        ),
    )
    for section_name, ledger, record in records:
        section = cast(dict[str, object], schema[section_name])
        tail = "counter_class" if section_name in {"counters", "python_counters"} else "cardinality"
        assert _rows(section, tail) == ledger
        assert tuple(item.name for item in fields(record) if item.init) == tuple(
            row[1] for row in ledger
        )
        assert section["construction"] == "named-only"

    diagnostic_sidecars = cast(dict[str, object], schema["diagnostic_reference_sidecars"])
    assert _rows(diagnostic_sidecars, "cardinality", "row_fields") == (
        NATIVE_DIAGNOSTIC_REFERENCE_KINDS_FIELDS_V2
    )
    assert tuple(item.name for item in fields(NativeDiagnosticReferenceKindsV2)) == tuple(
        row[1] for row in NATIVE_DIAGNOSTIC_REFERENCE_KINDS_FIELDS_V2
    )
    cardinalities = cast(dict[str, object], schema["facade_cardinality_summary"])
    for field_name, ledger, cardinality_record in (
        (
            "document_fields",
            NATIVE_DOCUMENT_FACADE_CARDINALITIES_FIELDS_V2,
            NativeDocumentFacadeCardinalitiesV2,
        ),
        (
            "closure_fields",
            NATIVE_CLOSURE_FACADE_CARDINALITIES_FIELDS_V2,
            NativeClosureFacadeCardinalitiesV2,
        ),
    ):
        assert _rows(cardinalities, "cardinality", field_name) == ledger
        assert tuple(item.name for item in fields(cardinality_record)) == tuple(
            row[1] for row in ledger
        )

    handle = cast(dict[str, object], schema["handle"])
    members = cast(list[dict[str, object]], handle["members"])
    assert (
        tuple((item["ordinal"], item["name"], item["type"], item["kind"]) for item in members)
        == NATIVE_SNAPSHOT_HANDLE_MEMBERS_V2
    )
    assert handle["sealed"] is handle["opaque"] is handle["owning"] is True
    document_handle = cast(dict[str, object], schema["document_handle"])
    document_members = cast(list[dict[str, object]], document_handle["members"])
    assert (
        tuple(
            (item["ordinal"], item["name"], item["type"], item["kind"]) for item in document_members
        )
        == NATIVE_DOCUMENT_HANDLE_MEMBERS_V2
    )
    assert (
        document_handle["python_type"] == NativeDocumentHandleV2.__name__
        and document_handle["sealed"] is document_handle["opaque"] is True
        and document_handle["owning"] is True
    )


def test_access_and_auxiliary_subtree_digests_are_independently_bound() -> None:
    access = native_facade_access_schema_semantics_v2()
    auxiliary = native_auxiliary_codec_schema_semantics_v2()
    access_digest = hashlib.sha256(
        b"pyowl-core:native-facade-access-schema:v2\x00" + _independent_canonical(access)
    ).digest()
    auxiliary_digest = hashlib.sha256(
        b"pyowl-core:native-auxiliary-row-codec:v2\x00" + _independent_canonical(auxiliary)
    ).digest()
    assert access_digest == NATIVE_FACADE_ACCESS_SCHEMA_SHA256_V2
    assert auxiliary_digest == NATIVE_AUXILIARY_CODEC_SCHEMA_SHA256_V2
    assert _load_schema()["access_protocol"] == access
    assert _load_schema()["auxiliary_codecs"] == auxiliary


def test_embedded_rust_hashes_match_all_three_frozen_python_vectors() -> None:
    assert _rust_digest("PUBLICATION_LEDGER_SHA256_V2") == (
        NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2
    )
    assert _rust_digest("FACADE_ACCESS_SCHEMA_SHA256_V2") == (
        NATIVE_FACADE_ACCESS_SCHEMA_SHA256_V2
    )
    assert _rust_digest("AUXILIARY_CODEC_SCHEMA_SHA256_V2") == (
        NATIVE_AUXILIARY_CODEC_SCHEMA_SHA256_V2
    )


def test_every_contract_mutation_changes_the_independent_ledger_digest() -> None:
    original = native_snapshot_publication_schema_semantics_v2()
    changes = []
    changed = copy.deepcopy(original)
    changed["amendment_reason"] = "silent side channel"
    changes.append(changed)
    changed = copy.deepcopy(original)
    cast(dict[str, object], changed["bounds"])["max_facade_page_rows"] = 65
    changes.append(changed)
    changed = copy.deepcopy(original)
    cast(dict[str, object], changed["access_protocol"])["contains"] = "generic opcode"
    changes.append(changed)
    changed = copy.deepcopy(original)
    cast(dict[str, object], changed["auxiliary_codecs"])["byte_order"] = "native"
    changes.append(changed)
    baseline = hashlib.sha256(_CANONICAL_PREFIX + _independent_canonical(original)).digest()
    for mutation in changes:
        assert (
            hashlib.sha256(_CANONICAL_PREFIX + _independent_canonical(mutation)).digest()
            != baseline
        )
    assert _load_schema()["bounds"] == NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2
