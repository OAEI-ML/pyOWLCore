"""Versioned, backend-neutral output fence for loading comparators.

The common contract intentionally contains hashes and exact byte counts rather
than a second ontology object graph.  Every digest is produced during the timed
adapter traversal; post-timer comparison only compares already-published JSON
scalars and byte/count inventories.  The builder in this module is the scalar
reference adapter for the complete Python backend.  Retained-native lanes must
publish the same contract through their bulk exporter; they must not call this
scalar builder.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import fields, is_dataclass
from typing import Any, cast

from pyowl_core import (
    AxiomScope,
    CanonicalSet,
    OntologyDocument,
    OntologySnapshot,
    canonical_bytes,
)
from pyowl_core.model import LOGICAL_AXIOM_TYPES, Entity, StructuralNode, encode_varint
from pyowl_core.model.axioms import AxiomNode

COMMON_CONTRACT_SCHEMA = "pyowl-core/comparator-common-contract/v1"
_SHA256 = frozenset("0123456789abcdef")


class CommonContractError(ValueError):
    """A comparator did not publish a complete, self-consistent output fence."""


def build_core_common_contract(
    snapshot: OntologySnapshot,
    *,
    corpus_id: str,
    source_sha256: str,
    options_sha256: str,
) -> dict[str, Any]:
    """Publish the Python backend's scalar-reference common contract."""

    if not isinstance(snapshot, OntologySnapshot):
        raise TypeError("snapshot must be OntologySnapshot")
    if not corpus_id:
        raise ValueError("corpus_id must be nonempty")
    _require_digest(source_sha256, "source_sha256")
    _require_digest(options_sha256, "options_sha256")

    axioms = tuple(snapshot.iter_axioms())
    extensions = tuple(snapshot.iter_extensions())
    annotations = tuple(snapshot.ontology_annotations())
    signature = snapshot.signature(include_builtins=True)

    root_record = next(
        record
        for record in snapshot.import_manifest.documents
        if record.document_key == snapshot.root_document_key
    )

    fingerprints = {
        "document": _fingerprint_evidence(
            _document_preimage_parts(snapshot.root),
            root_record.document_fingerprint.hex,
        ),
        "structural": _fingerprint_evidence(
            _structural_preimage_parts(snapshot),
            snapshot.structural_fingerprint.hex,
        ),
        "logical": _fingerprint_evidence(
            _logical_preimage_parts(axioms, extensions),
            snapshot.logical_fingerprint.hex,
        ),
        "signature": _fingerprint_evidence(
            _signature_preimage_parts(signature, include_builtins=True),
            snapshot.signature_fingerprint.hex,
        ),
    }

    diagnostic_rows = [value.to_dict() for value in snapshot.diagnostics]
    diagnostics_bytes = _canonical_json(diagnostic_rows)
    provenance = _provenance_inventory(snapshot)
    provenance_bytes = _canonical_json(provenance)
    identity = _identity_inventory(snapshot)
    identity_bytes = _canonical_json(identity)
    inventories = {
        "ontology_annotations": _record_inventory(annotations),
        "axioms": _record_inventory(axioms),
        "extensions": _record_inventory(extensions),
        "signature": _record_inventory(signature),
        "documents": _document_inventory(snapshot),
    }
    ledger = {
        "inventories": inventories,
        "identity_sha256": hashlib.sha256(identity_bytes).hexdigest(),
        "identity_bytes": len(identity_bytes),
        "provenance_sha256": hashlib.sha256(provenance_bytes).hexdigest(),
        "provenance_bytes": len(provenance_bytes),
        "diagnostics_sha256": hashlib.sha256(diagnostics_bytes).hexdigest(),
        "diagnostics_bytes": len(diagnostics_bytes),
        "diagnostic_count": len(diagnostic_rows),
    }
    payload: dict[str, Any] = {
        "schema": COMMON_CONTRACT_SCHEMA,
        "model_schema": snapshot.capabilities.model_schema,
        "corpus_id": corpus_id,
        "source_sha256": source_sha256,
        "options_sha256": options_sha256,
        "complete_import_closure": snapshot.is_complete,
        "root_document_key": snapshot.root_document_key,
        "identity": identity,
        "provenance": provenance,
        "diagnostics": diagnostic_rows,
        "fingerprints": fingerprints,
        "ledger": ledger,
    }
    payload["contract_sha256"] = hashlib.sha256(_canonical_json(payload)).hexdigest()
    return payload


def validate_common_contract(value: Mapping[str, Any]) -> None:
    """Fully validate one contract inside its comparator readiness timer."""

    validate_common_contract_shape(value)
    ledger = cast(Mapping[str, Any], value["ledger"])
    if ledger["identity_sha256"] != hashlib.sha256(_canonical_json(value["identity"])).hexdigest():
        raise CommonContractError("identity inventory digest mismatch")
    if (
        ledger["provenance_sha256"]
        != hashlib.sha256(_canonical_json(value["provenance"])).hexdigest()
    ):
        raise CommonContractError("provenance inventory digest mismatch")
    if (
        ledger["diagnostics_sha256"]
        != hashlib.sha256(_canonical_json(value["diagnostics"])).hexdigest()
    ):
        raise CommonContractError("diagnostic inventory digest mismatch")
    if ledger["identity_bytes"] != len(_canonical_json(value["identity"])):
        raise CommonContractError("identity inventory byte count mismatch")
    if ledger["provenance_bytes"] != len(_canonical_json(value["provenance"])):
        raise CommonContractError("provenance inventory byte count mismatch")
    if ledger["diagnostics_bytes"] != len(_canonical_json(value["diagnostics"])):
        raise CommonContractError("diagnostic inventory byte count mismatch")

    unsigned = dict(value)
    observed = cast(str, unsigned.pop("contract_sha256"))
    expected = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
    if observed != expected:
        raise CommonContractError("common contract digest mismatch")


def validate_common_contract_shape(value: Mapping[str, Any]) -> None:
    """Validate published fields without re-encoding or hashing ontology data."""

    required = {
        "schema",
        "model_schema",
        "corpus_id",
        "source_sha256",
        "options_sha256",
        "complete_import_closure",
        "root_document_key",
        "identity",
        "provenance",
        "diagnostics",
        "fingerprints",
        "ledger",
        "contract_sha256",
    }
    if set(value) != required:
        raise CommonContractError("common contract fields differ from schema v1")
    if value["schema"] != COMMON_CONTRACT_SCHEMA:
        raise CommonContractError("unsupported common contract schema")
    if isinstance(value["model_schema"], bool) or not isinstance(value["model_schema"], int):
        raise CommonContractError("model_schema must be an integer")
    for name in ("corpus_id", "root_document_key"):
        if not isinstance(value[name], str) or not value[name]:
            raise CommonContractError(f"{name} must be nonempty")
    for name in ("source_sha256", "options_sha256", "contract_sha256"):
        _require_digest(value[name], name)
    if not isinstance(value["complete_import_closure"], bool):
        raise CommonContractError("complete_import_closure must be boolean")
    if not isinstance(value["diagnostics"], list):
        raise CommonContractError("diagnostics must be an array")
    for name in ("identity", "provenance", "fingerprints", "ledger"):
        if not isinstance(value[name], Mapping):
            raise CommonContractError(f"{name} must be an object")

    fingerprints = cast(Mapping[str, Any], value["fingerprints"])
    if set(fingerprints) != {"document", "structural", "logical", "signature"}:
        raise CommonContractError("exactly four fingerprint evidences are required")
    for name, raw in fingerprints.items():
        if not isinstance(raw, Mapping):
            raise CommonContractError(f"{name} fingerprint evidence must be an object")
        evidence = cast(Mapping[str, Any], raw)
        if set(evidence) != {
            "algorithm",
            "schema",
            "preimage_bytes",
            "preimage_sha256",
            "digest",
        }:
            raise CommonContractError(f"{name} fingerprint evidence fields differ")
        if evidence["algorithm"] != "sha256" or evidence["schema"] != 1:
            raise CommonContractError(f"{name} fingerprint algorithm/schema differs")
        if (
            isinstance(evidence["preimage_bytes"], bool)
            or not isinstance(evidence["preimage_bytes"], int)
            or evidence["preimage_bytes"] < 1
        ):
            raise CommonContractError(f"{name} preimage byte count is invalid")
        _require_digest(evidence["preimage_sha256"], f"{name}.preimage_sha256")
        _require_digest(evidence["digest"], f"{name}.digest")
        if evidence["preimage_sha256"] != evidence["digest"]:
            raise CommonContractError(f"{name} digest does not hash its preimage")

    ledger = cast(Mapping[str, Any], value["ledger"])
    expected_ledger_fields = {
        "inventories",
        "identity_sha256",
        "identity_bytes",
        "provenance_sha256",
        "provenance_bytes",
        "diagnostics_sha256",
        "diagnostics_bytes",
        "diagnostic_count",
    }
    if set(ledger) != expected_ledger_fields:
        raise CommonContractError("ledger fields differ from schema v1")
    for name in ("identity_sha256", "provenance_sha256", "diagnostics_sha256"):
        _require_digest(ledger[name], f"ledger.{name}")
    for name in (
        "identity_bytes",
        "provenance_bytes",
        "diagnostics_bytes",
        "diagnostic_count",
    ):
        raw = ledger[name]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise CommonContractError(f"ledger.{name} must be nonnegative")
    if ledger["diagnostic_count"] != len(cast(list[object], value["diagnostics"])):
        raise CommonContractError("diagnostic inventory count mismatch")
    _validate_inventories(ledger["inventories"])


def common_contract_equality_key(value: Mapping[str, Any]) -> tuple[object, ...]:
    """Return already-published scalars for a post-timer equality assertion.

    The producing adapter must fully validate the contract before stopping its
    readiness timer.  This function intentionally performs no JSON encoding,
    canonicalization, or hashing.
    """

    validate_common_contract_shape(value)
    fingerprints = cast(Mapping[str, Mapping[str, Any]], value["fingerprints"])
    ledger = cast(Mapping[str, Any], value["ledger"])
    return (
        value["contract_sha256"],
        value["model_schema"],
        value["source_sha256"],
        value["options_sha256"],
        value["complete_import_closure"],
        value["root_document_key"],
        tuple(
            (
                name,
                fingerprints[name]["algorithm"],
                fingerprints[name]["schema"],
                fingerprints[name]["preimage_bytes"],
                fingerprints[name]["preimage_sha256"],
                fingerprints[name]["digest"],
            )
            for name in sorted(fingerprints)
        ),
        _inventory_equality_key(cast(Mapping[str, Any], ledger["inventories"])),
        ledger["identity_sha256"],
        ledger["identity_bytes"],
        ledger["provenance_sha256"],
        ledger["provenance_bytes"],
        ledger["diagnostics_sha256"],
        ledger["diagnostics_bytes"],
        ledger["diagnostic_count"],
    )


def _fingerprint_evidence(preimage: Iterable[bytes], expected: str) -> dict[str, object]:
    digest_builder = hashlib.sha256()
    byte_count = 0
    for piece in preimage:
        digest_builder.update(piece)
        byte_count += len(piece)
    digest = digest_builder.hexdigest()
    if digest != expected:
        raise CommonContractError(
            "independent comparator preimage disagrees with published core fingerprint"
        )
    return {
        "algorithm": "sha256",
        "schema": 1,
        "preimage_bytes": byte_count,
        "preimage_sha256": digest,
        "digest": expected,
    }


def _document_preimage_parts(document: OntologyDocument) -> Iterable[bytes]:
    yield b"pyowl-core:document-fingerprint:v1\x00"
    ontology_id = document.ontology_id
    for iri in (ontology_id.ontology_iri, ontology_id.version_iri):
        yield b"0" if iri is None else b"1" + _frame(canonical_bytes(iri))
    for values in (
        document.direct_imports,
        document.ontology_annotations,
        document.axioms,
        document.extension_components,
    ):
        yield from _collection_parts(values)


def _structural_preimage_parts(snapshot: OntologySnapshot) -> Iterable[bytes]:
    yield b"pyowl-core:snapshot-structural:v1\x00"
    yield _frame(snapshot.import_manifest.canonical_bytes())
    for record in snapshot.import_manifest.documents:
        key = record.document_key
        yield _frame(key.encode("ascii"))
        for values in (
            snapshot.ontology_annotations(scope=AxiomScope.DOCUMENT, document_key=key),
            tuple(snapshot.iter_axioms(scope=AxiomScope.DOCUMENT, document_key=key)),
            tuple(snapshot.iter_extensions(scope=AxiomScope.DOCUMENT, document_key=key)),
        ):
            yield from _collection_parts(values)


def _logical_preimage_parts(
    axioms: Iterable[AxiomNode], extensions: Iterable[StructuralNode]
) -> Iterable[bytes]:
    logical = sorted(
        {
            canonical_bytes(_without_annotations(value))
            for value in axioms
            if isinstance(value, LOGICAL_AXIOM_TYPES)
        }
    )
    extension_values = sorted(
        {canonical_bytes(_without_annotations(value)) for value in extensions}
    )
    yield b"pyowl-core:snapshot-logical:v1\x00"
    yield b"datatype-policy:owl2-v1\x00"
    yield encode_varint(len(logical))
    for value in logical:
        yield _frame(value)
    yield encode_varint(len(extension_values))
    for value in extension_values:
        yield b"E" + _frame(value)


def _signature_preimage_parts(
    values: Iterable[Entity], *, include_builtins: bool
) -> Iterable[bytes]:
    members = sorted({canonical_bytes(value) for value in values})
    yield b"pyowl-core:snapshot-signature:v1\x00"
    yield bytes((int(include_builtins),))
    yield encode_varint(len(members))
    for value in members:
        yield _frame(value)


def _without_annotations(value: StructuralNode) -> StructuralNode:
    if not is_dataclass(value) or not hasattr(value, "annotations"):
        return value
    annotations = value.annotations
    if isinstance(annotations, CanonicalSet) and not annotations:
        return value
    arguments = {field.name: getattr(value, field.name) for field in fields(value)}
    arguments["annotations"] = CanonicalSet()
    return cast(StructuralNode, type(value)(**arguments))


def _collection_parts(values: Iterable[StructuralNode]) -> list[bytes]:
    encoded = tuple(canonical_bytes(value) for value in values)
    return [encode_varint(len(encoded)), *(_frame(value) for value in encoded)]


def _record_inventory(values: Iterable[StructuralNode]) -> dict[str, object]:
    encoded = tuple(sorted({canonical_bytes(value) for value in values}))
    transcript = (
        b"pyowl-core:comparator-record-inventory:v1\x00"
        + encode_varint(len(encoded))
        + b"".join(_frame(value) for value in encoded)
    )
    return {
        "count": len(encoded),
        "canonical_bytes": sum(len(value) for value in encoded),
        "transcript_bytes": len(transcript),
        "sha256": hashlib.sha256(transcript).hexdigest(),
    }


def _document_inventory(snapshot: OntologySnapshot) -> dict[str, object]:
    transcript = b"pyowl-core:comparator-document-inventory:v1\x00"
    rows: list[bytes] = []
    for record in snapshot.import_manifest.documents:
        rows.append(
            _frame(record.document_key.encode("utf-8"))
            + record.source_sha256
            + record.document_fingerprint.digest
        )
    transcript += encode_varint(len(rows)) + b"".join(rows)
    return {
        "count": len(rows),
        "canonical_bytes": sum(len(value) for value in rows),
        "transcript_bytes": len(transcript),
        "sha256": hashlib.sha256(transcript).hexdigest(),
    }


def _identity_inventory(snapshot: OntologySnapshot) -> dict[str, object]:
    documents: list[dict[str, object]] = []
    for record in snapshot.import_manifest.documents:
        documents.append(
            {
                "document_key": record.document_key,
                "document_iri": _optional_canonical(record.document_iri),
                "ontology_iri": _optional_canonical(record.ontology_id.ontology_iri),
                "version_iri": _optional_canonical(record.ontology_id.version_iri),
                "source_sha256": record.source_sha256.hex(),
                "document_fingerprint": record.document_fingerprint.hex,
                "format": record.format.value,
                "status": record.status.value,
            }
        )
    imports = [
        {
            "importing_document_key": edge.importing_document_key,
            "import_iri": canonical_bytes(edge.import_iri).hex(),
            "status": edge.status.value,
            "resolved_document_key": edge.resolved_document_key,
            "resolver_name": edge.resolver_name,
        }
        for edge in snapshot.import_manifest.edges
    ]
    return {
        "documents": documents,
        "imports": imports,
        "import_policy": snapshot.import_manifest.policy.value,
        "offline": snapshot.import_manifest.offline,
        "resolver_configuration_sha256": (
            snapshot.import_manifest.resolver_configuration_fingerprint.hex()
        ),
        "root_document_key": snapshot.root_document_key,
    }


def _provenance_inventory(snapshot: OntologySnapshot) -> dict[str, object]:
    origins: list[dict[str, object]] = []
    for digest, occurrences in sorted(snapshot.origin_index.entries.items()):
        origins.append(
            {
                "structural_sha256": digest.hex(),
                "occurrences": [
                    {
                        "document_key": item.document_key,
                        "occurrence": item.occurrence,
                        "span": None if item.span is None else item.span.to_dict(),
                    }
                    for item in occurrences
                ],
            }
        )
    return {
        "origins": origins,
        "origin_entry_count": len(origins),
        "source_byte_count": snapshot.report.total_source_bytes,
        "document_count": snapshot.report.document_count,
    }


def _optional_canonical(value: StructuralNode | None) -> str | None:
    return None if value is None else canonical_bytes(value).hex()


def _validate_inventories(value: object) -> None:
    if not isinstance(value, Mapping):
        raise CommonContractError("ledger.inventories must be an object")
    inventories = cast(Mapping[str, object], value)
    if set(inventories) != {
        "ontology_annotations",
        "axioms",
        "extensions",
        "signature",
        "documents",
    }:
        raise CommonContractError("record inventory fields differ from schema v1")
    for name, raw in inventories.items():
        if not isinstance(raw, Mapping):
            raise CommonContractError(f"inventory {name} must be an object")
        row = cast(Mapping[str, object], raw)
        if set(row) != {"count", "canonical_bytes", "transcript_bytes", "sha256"}:
            raise CommonContractError(f"inventory {name} fields differ")
        for field in ("count", "canonical_bytes", "transcript_bytes"):
            scalar = row[field]
            if isinstance(scalar, bool) or not isinstance(scalar, int) or scalar < 0:
                raise CommonContractError(f"inventory {name}.{field} is invalid")
        _require_digest(row["sha256"], f"inventory {name}.sha256")


def _inventory_equality_key(value: Mapping[str, Any]) -> tuple[object, ...]:
    """Normalize inventory mappings without hashing or structural traversal."""

    return tuple(
        (
            name,
            value[name]["count"],
            value[name]["canonical_bytes"],
            value[name]["transcript_bytes"],
            value[name]["sha256"],
        )
        for name in sorted(value)
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _frame(value: bytes) -> bytes:
    return encode_varint(len(value)) + value


def _require_digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA256 for character in value)
    ):
        raise CommonContractError(f"{name} must be lowercase SHA-256")
    return value


__all__ = [
    "COMMON_CONTRACT_SCHEMA",
    "CommonContractError",
    "build_core_common_contract",
    "common_contract_equality_key",
    "validate_common_contract",
    "validate_common_contract_shape",
]
