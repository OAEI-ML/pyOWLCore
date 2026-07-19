from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any, cast

try:
    import tomllib  # type: ignore[import-untyped, unused-ignore]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[import-not-found, import-untyped, unused-ignore]

from pyowl_core.backends.native_handoff import (
    NATIVE_DIAGNOSTIC_PUBLICATION_FIELDS_V1,
    NATIVE_DOCUMENT_PUBLICATION_FIELDS_V1,
    NATIVE_IMPORT_DOCUMENT_FIELDS_V1,
    NATIVE_IMPORT_EDGE_FIELDS_V1,
    NATIVE_IMPORT_MANIFEST_FIELDS_V1,
    NATIVE_LOAD_REPORT_PUBLICATION_FIELDS_V1,
    NATIVE_PROVENANCE_PUBLICATION_FIELDS_V1,
    NATIVE_SNAPSHOT_ATTESTATION_FIELDS_V1,
    NATIVE_SNAPSHOT_CAPABILITY_BITS_V1,
    NATIVE_SNAPSHOT_CAPABILITY_RULES_V1,
    NATIVE_SNAPSHOT_HANDLE_MEMBERS_V1,
    NATIVE_SNAPSHOT_LIFECYCLE_V1,
    NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1,
    NATIVE_SNAPSHOT_PUBLICATION_FIELDS_V1,
    NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256,
    NATIVE_SNAPSHOT_RUST_PARITY_REQUIRED,
    NativeDiagnosticPublicationV1,
    NativeDocumentProvenancePublicationV1,
    NativeDocumentPublicationV1,
    NativeImportDocumentPublicationV1,
    NativeImportEdgePublicationV1,
    NativeImportManifestPublicationV1,
    NativeLoadReportPublicationV1,
    NativeSnapshotAttestationV1,
    NativeSnapshotPublicationV1,
    native_snapshot_publication_schema_semantics_v1,
)

ROOT = Path(__file__).parents[3]
SCHEMA = ROOT / "schemas" / "native-snapshot-publication-v1.toml"
_CANONICAL_PREFIX = b"pyowl-core:typed-toml-tree:v1\x00"


def _load_schema() -> dict[str, Any]:
    with SCHEMA.open("rb") as stream:
        return cast(dict[str, Any], tomllib.load(stream))


def _rows(section: Mapping[str, object], tail: str) -> tuple[tuple[object, ...], ...]:
    values = cast(list[dict[str, object]], section["fields"])
    return tuple((item["ordinal"], item["name"], item["type"], item[tail]) for item in values)


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
        list_body = b"".join(_frame(_independent_canonical(item)) for item in value)
        return b"l" + _frame(list_body)
    if isinstance(value, Mapping):
        keys = list(value)
        assert all(isinstance(key, str) for key in keys)
        keys.sort(key=lambda key: cast(str, key).encode("utf-8"))
        map_body = bytearray()
        for raw_key in keys:
            key = cast(str, raw_key)
            map_body.extend(_frame(key.encode("utf-8")))
            map_body.extend(_frame(_independent_canonical(value[key])))
        return b"m" + _frame(bytes(map_body))
    raise TypeError(type(value).__qualname__)


def _independent_digest(schema: Mapping[str, object]) -> bytes:
    semantics = dict(schema)
    semantics.pop("ledger_sha256", None)
    return hashlib.sha256(_CANONICAL_PREFIX + _independent_canonical(semantics)).digest()


def test_schema_has_exact_python_record_and_handle_parity() -> None:
    schema = _load_schema()
    sections = (
        (
            "envelope",
            NATIVE_SNAPSHOT_PUBLICATION_FIELDS_V1,
            NativeSnapshotPublicationV1,
        ),
        ("document", NATIVE_DOCUMENT_PUBLICATION_FIELDS_V1, NativeDocumentPublicationV1),
        (
            "diagnostic",
            NATIVE_DIAGNOSTIC_PUBLICATION_FIELDS_V1,
            NativeDiagnosticPublicationV1,
        ),
        (
            "provenance",
            NATIVE_PROVENANCE_PUBLICATION_FIELDS_V1,
            NativeDocumentProvenancePublicationV1,
        ),
        (
            "import_document",
            NATIVE_IMPORT_DOCUMENT_FIELDS_V1,
            NativeImportDocumentPublicationV1,
        ),
        ("import_edge", NATIVE_IMPORT_EDGE_FIELDS_V1, NativeImportEdgePublicationV1),
        (
            "import_manifest",
            NATIVE_IMPORT_MANIFEST_FIELDS_V1,
            NativeImportManifestPublicationV1,
        ),
        (
            "report",
            NATIVE_LOAD_REPORT_PUBLICATION_FIELDS_V1,
            NativeLoadReportPublicationV1,
        ),
        (
            "attestation",
            NATIVE_SNAPSHOT_ATTESTATION_FIELDS_V1,
            NativeSnapshotAttestationV1,
        ),
    )
    for section_name, ledger, record_type in sections:
        section = cast(dict[str, object], schema[section_name])
        assert _rows(section, "cardinality") == ledger
        assert tuple(item.name for item in fields(record_type)) == tuple(row[1] for row in ledger)
        assert section["construction"] == "named-only"

    handle = cast(dict[str, object], schema["handle"])
    assert handle["sealed"] is True
    assert handle["opaque"] is True
    assert handle["owning"] is True
    members = cast(list[dict[str, object]], handle["members"])
    assert (
        tuple((item["ordinal"], item["name"], item["type"], item["kind"]) for item in members)
        == NATIVE_SNAPSHOT_HANDLE_MEMBERS_V1
    )
    assert (
        tuple(
            (item["value"], item["name"])
            for item in cast(list[dict[str, object]], schema["capability_bits"])
        )
        == NATIVE_SNAPSHOT_CAPABILITY_BITS_V1
    )


def test_independent_digest_commits_to_every_toml_semantic() -> None:
    schema = _load_schema()
    recorded = cast(str, schema["ledger_sha256"])
    assert _independent_digest(schema) == NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256
    assert recorded == NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256.hex()

    semantics = dict(schema)
    del semantics["ledger_sha256"]
    assert semantics == native_snapshot_publication_schema_semantics_v1()

    mutations = []
    changed = copy.deepcopy(schema)
    changed["extension_policy"] = "silently extensible"
    mutations.append(changed)
    changed = copy.deepcopy(schema)
    cast(dict[str, object], changed["handle"])["opaque"] = False
    mutations.append(changed)
    changed = copy.deepcopy(schema)
    cast(dict[str, object], changed["bounds"])["max_timing_rows"] = 65
    mutations.append(changed)
    changed = copy.deepcopy(schema)
    cast(dict[str, object], changed["lifecycle"])["pickle"] = "allowed"
    mutations.append(changed)
    changed = copy.deepcopy(schema)
    cast(dict[str, object], changed["capability_rules"])["required_mask"] = 3
    mutations.append(changed)
    changed = copy.deepcopy(schema)
    cast(dict[str, object], changed["attestation"])["domain"] = "wrong-domain"
    mutations.append(changed)
    for mutation in mutations:
        assert _independent_digest(mutation) != NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256


def test_bounds_capability_lifecycle_and_rust_parity_requirements_are_frozen() -> None:
    schema = _load_schema()
    assert schema["bounds"] == NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1
    assert schema["capability_rules"] == NATIVE_SNAPSHOT_CAPABILITY_RULES_V1
    assert schema["lifecycle"] == NATIVE_SNAPSHOT_LIFECYCLE_V1
    assert schema["rust_parity_required"] is NATIVE_SNAPSHOT_RUST_PARITY_REQUIRED
    rust_parity = cast(dict[str, object], schema["rust_parity"])
    assert rust_parity == {
        "required": True,
        "record": "NativeSnapshotPublicationV1",
        "attestation": "NativeSnapshotAttestationV1",
        "status_claim": "none-until-runtime-registration",
    }
    registration = cast(dict[str, object], schema["handle_registration"])
    assert registration["rust_owner_module"] == "pyowl_core._native"
    assert registration["rust_owner_name"] == "_NativeSnapshotHandle"
    assert registration["exact_type_only"] is True
    for frozen_mapping in (
        NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1,
        NATIVE_SNAPSHOT_CAPABILITY_RULES_V1,
        NATIVE_SNAPSHOT_LIFECYCLE_V1,
    ):
        try:
            cast(Any, frozen_mapping)["hostile_mutation"] = object()
        except TypeError:
            pass
        else:  # pragma: no cover - contract regression guard
            raise AssertionError("ledger semantics mapping is mutable")
