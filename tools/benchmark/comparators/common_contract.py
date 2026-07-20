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
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Any, cast

from pyowl_core import (
    AxiomScope,
    CanonicalSet,
    OntologyDocument,
    OntologySnapshot,
    SourceSpan,
    canonical_bytes,
)
from pyowl_core.document.native_storage import (
    _NativeCommonContractFingerprintEvidenceV1,
    _NativeCommonContractRecordInventoryV1,
    _NativeCommonContractSummaryV1,
)
from pyowl_core.model import LOGICAL_AXIOM_TYPES, Entity, StructuralNode, encode_varint
from pyowl_core.model.axioms import AxiomNode

from ..native_redesign.encoded_contract import (
    DigestResult,
    EncodedContractUnavailable,
    EncodedStructuralTraversal,
    EncodedTraversalEvidence,
    combine_traversal_evidence,
)

COMMON_CONTRACT_SCHEMA = "pyowl-core/comparator-common-contract/v1"
_SHA256 = frozenset("0123456789abcdef")
_RECORD_INVENTORY_DOMAIN = b"pyowl-core:comparator-record-inventory:v1\x00"


class CommonContractError(ValueError):
    """A comparator did not publish a complete, self-consistent output fence."""


@dataclass(frozen=True, slots=True)
class EncodedCommonContractResult:
    """One bulk-produced common contract and its traversal-fence evidence."""

    contract: dict[str, Any]
    evidence: EncodedTraversalEvidence


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


def build_encoded_core_common_contract(
    snapshot: OntologySnapshot,
    *,
    corpus_id: str,
    source_sha256: str,
    options_sha256: str,
    require_native_direct: bool = True,
) -> EncodedCommonContractResult:
    """Publish the common contract by streaming frozen encoded columns.

    The ontology-sized portions of this adapter are consumed only through
    ``EncodedStructuralView`` buffers. Snapshot metadata remains the authority
    for identity, provenance, diagnostics, and the expected public digests.
    """

    if not isinstance(snapshot, OntologySnapshot):
        raise TypeError("snapshot must be OntologySnapshot")
    if not corpus_id:
        raise ValueError("corpus_id must be nonempty")
    _require_digest(source_sha256, "source_sha256")
    _require_digest(options_sha256, "options_sha256")

    manifest = snapshot.import_manifest
    native_summary = (
        _native_common_contract_summary(snapshot)
        if len(manifest.documents) == 1
        else None
    )
    closure: EncodedStructuralTraversal | None = None
    encoded_inventories: Mapping[str, dict[str, object]] | None
    if native_summary is not None:
        root_record = manifest.documents[0]
        fingerprints, encoded_inventories = _native_summary_contract_components(
            native_summary,
            document_fingerprint=root_record.document_fingerprint.hex,
            structural_fingerprint=snapshot.structural_fingerprint.hex,
            logical_fingerprint=snapshot.logical_fingerprint.hex,
            signature_fingerprint=snapshot.signature_fingerprint.hex,
        )
        traversal_evidence = EncodedTraversalEvidence(
            view_count=0,
            document_view_count=0,
            node_count=native_summary.node_count,
            root_count=native_summary.root_count,
            referenced_buffer_bytes=0,
            native_common_contract_summary_count=1,
        )
    else:
        closure = EncodedStructuralTraversal.from_snapshot(
            snapshot,
            scope=AxiomScope.CLOSURE,
            require_native_direct=require_native_direct,
        )
        document_rows = (
            ((manifest.documents[0], closure),)
            if len(manifest.documents) == 1
            else tuple(
                (
                    record,
                    EncodedStructuralTraversal.from_snapshot(
                        snapshot,
                        scope=AxiomScope.DOCUMENT,
                        document_key=record.document_key,
                        require_native_direct=require_native_direct,
                    ),
                )
                for record in manifest.documents
            )
        )
        root_record, root_traversal = next(
            row for row in document_rows if row[0].document_key == snapshot.root_document_key
        )
        direct_imports = tuple(
            sorted(
                {
                    canonical_bytes(edge.import_iri)
                    for edge in manifest.edges
                    if edge.importing_document_key == root_record.document_key
                }
            )
        )

        if len(document_rows) == 1:
            combined = closure.single_document_contract_digests(
                ontology_iri=_optional_canonical_bytes(root_record.ontology_id.ontology_iri),
                version_iri=_optional_canonical_bytes(root_record.ontology_id.version_iri),
                direct_imports=direct_imports,
                manifest_bytes=manifest.canonical_bytes(),
                document_key=root_record.document_key,
            )
            fingerprints = {
                "document": _encoded_fingerprint_evidence(
                    combined.document,
                    root_record.document_fingerprint.hex,
                ),
                "structural": _encoded_fingerprint_evidence(
                    combined.structural,
                    snapshot.structural_fingerprint.hex,
                ),
                "logical": _encoded_fingerprint_evidence(
                    combined.logical,
                    snapshot.logical_fingerprint.hex,
                ),
                "signature": _encoded_fingerprint_evidence(
                    combined.signature,
                    snapshot.signature_fingerprint.hex,
                ),
            }
            encoded_inventories = combined.inventories
        else:
            fingerprints = {
                "document": _encoded_fingerprint_evidence(
                    root_traversal.document_preimage(
                        ontology_iri=_optional_canonical_bytes(root_record.ontology_id.ontology_iri),
                        version_iri=_optional_canonical_bytes(root_record.ontology_id.version_iri),
                        direct_imports=direct_imports,
                    ),
                    root_record.document_fingerprint.hex,
                ),
                "structural": _encoded_fingerprint_evidence(
                    EncodedStructuralTraversal.structural_preimage(
                        manifest.canonical_bytes(),
                        tuple(
                            (record.document_key, traversal)
                            for record, traversal in document_rows
                        ),
                    ),
                    snapshot.structural_fingerprint.hex,
                ),
                "logical": _encoded_fingerprint_evidence(
                    closure.logical_preimage(),
                    snapshot.logical_fingerprint.hex,
                ),
                "signature": _encoded_fingerprint_evidence(
                    closure.signature_preimage(),
                    snapshot.signature_fingerprint.hex,
                ),
            }
            encoded_inventories = None
        traversal_evidence = combine_traversal_evidence(
            closure,
            tuple(traversal for _record, traversal in document_rows),
        )

    diagnostic_rows = [value.to_dict() for value in snapshot.diagnostics]
    diagnostics_bytes = _canonical_json(diagnostic_rows)
    if require_native_direct:
        provenance, provenance_rows_streamed = _encoded_provenance_inventory(snapshot)
    else:
        provenance = _provenance_inventory(snapshot)
        provenance_rows_streamed = 0
    provenance_bytes = _canonical_json(provenance)
    identity = _identity_inventory(snapshot)
    identity_bytes = _canonical_json(identity)
    if encoded_inventories is not None:
        inventories = {
            **encoded_inventories,
            "documents": _document_inventory(snapshot),
        }
    else:
        # ``None`` is produced only by the multi-document encoded fallback.
        if closure is None:  # pragma: no cover - guarded by the branches above
            raise CommonContractError("encoded inventory traversal was not initialized")
        inventories = {
            "ontology_annotations": closure.record_inventory(1),
            "axioms": closure.record_inventory(2),
            "extensions": closure.record_inventory(3),
            "signature": closure.signature_inventory(),
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
    return EncodedCommonContractResult(
        payload,
        replace(
            traversal_evidence,
            provenance_rows_streamed=provenance_rows_streamed,
        ),
    )


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


def _encoded_fingerprint_evidence(preimage: DigestResult, expected: str) -> dict[str, object]:
    if preimage.sha256 != expected:
        raise CommonContractError(
            "encoded comparator preimage disagrees with published core fingerprint"
        )
    return {
        "algorithm": "sha256",
        "schema": 1,
        "preimage_bytes": preimage.byte_count,
        "preimage_sha256": preimage.sha256,
        "digest": expected,
    }


def _native_common_contract_summary(
    snapshot: OntologySnapshot,
) -> _NativeCommonContractSummaryV1 | None:
    exporter = getattr(snapshot, "_native_common_contract_summary_v1", None)
    if not callable(exporter):
        return None
    summary = exporter()
    if type(summary) is not _NativeCommonContractSummaryV1:
        raise CommonContractError(
            "native common-contract summary has the wrong exact type"
        )
    return summary


def _native_summary_contract_components(
    summary: _NativeCommonContractSummaryV1,
    *,
    document_fingerprint: str,
    structural_fingerprint: str,
    logical_fingerprint: str,
    signature_fingerprint: str,
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    """Validate and publish one compact Rust-owned contract summary."""

    if type(summary) is not _NativeCommonContractSummaryV1:
        raise CommonContractError("native common-contract summary has the wrong exact type")
    if type(summary.schema) is not int or summary.schema != 1:
        raise CommonContractError("native common-contract summary schema differs")

    expected_fingerprints = {
        "document": (summary.document_fingerprint, document_fingerprint),
        "structural": (summary.structural_fingerprint, structural_fingerprint),
        "logical": (summary.logical_fingerprint, logical_fingerprint),
        "signature": (summary.signature_fingerprint, signature_fingerprint),
    }
    fingerprints: dict[str, dict[str, object]] = {}
    for name, (fingerprint_evidence, expected) in expected_fingerprints.items():
        if type(fingerprint_evidence) is not _NativeCommonContractFingerprintEvidenceV1:
            raise CommonContractError(
                f"native common-contract {name} fingerprint has the wrong exact type"
            )
        if (
            type(fingerprint_evidence.preimage_bytes) is not int
            or not 0 < fingerprint_evidence.preimage_bytes < 2**64
        ):
            raise CommonContractError(
                f"native common-contract {name} preimage length is not a positive u64"
            )
        if (
            type(fingerprint_evidence.sha256) is not bytes
            or len(fingerprint_evidence.sha256) != 32
        ):
            raise CommonContractError(
                f"native common-contract {name} fingerprint is not exact bytes32"
            )
        observed = fingerprint_evidence.sha256.hex()
        if observed != expected:
            raise CommonContractError(
                f"native common-contract {name} fingerprint disagrees with publication metadata"
            )
        fingerprints[name] = {
            "algorithm": "sha256",
            "schema": 1,
            "preimage_bytes": fingerprint_evidence.preimage_bytes,
            "preimage_sha256": observed,
            "digest": expected,
        }

    expected_inventories = {
        "ontology_annotations": summary.ontology_annotations,
        "axioms": summary.axioms,
        "extensions": summary.extensions,
        "signature": summary.signature,
    }
    inventories: dict[str, dict[str, object]] = {}
    for name, inventory in expected_inventories.items():
        if type(inventory) is not _NativeCommonContractRecordInventoryV1:
            raise CommonContractError(
                f"native common-contract {name} inventory has the wrong exact type"
            )
        scalars = (
            inventory.count,
            inventory.canonical_bytes,
            inventory.transcript_bytes,
        )
        if any(type(item) is not int or not 0 <= item < 2**64 for item in scalars):
            raise CommonContractError(
                f"native common-contract {name} inventory contains a non-u64 scalar"
            )
        if (inventory.count == 0) != (inventory.canonical_bytes == 0):
            raise CommonContractError(
                f"native common-contract {name} inventory count and bytes disagree"
            )
        minimum_transcript = (
            len(_RECORD_INVENTORY_DOMAIN)
            + len(encode_varint(inventory.count))
            + inventory.count
            + inventory.canonical_bytes
        )
        if inventory.transcript_bytes < minimum_transcript:
            raise CommonContractError(
                f"native common-contract {name} inventory transcript is undersized"
            )
        if type(inventory.sha256) is not bytes or len(inventory.sha256) != 32:
            raise CommonContractError(
                f"native common-contract {name} inventory digest is not exact bytes32"
            )
        inventories[name] = {
            "count": inventory.count,
            "canonical_bytes": inventory.canonical_bytes,
            "transcript_bytes": inventory.transcript_bytes,
            "sha256": inventory.sha256.hex(),
        }

    if (
        type(summary.root_count) is not int
        or not 0 <= summary.root_count < 2**64
        or type(summary.node_count) is not int
        or not 0 <= summary.node_count < 2**64
    ):
        raise CommonContractError("native common-contract graph counts are not exact u64 values")
    inventory_roots = sum(
        value.count
        for value in (
            summary.ontology_annotations,
            summary.axioms,
            summary.extensions,
        )
    )
    if summary.root_count != inventory_roots:
        raise CommonContractError(
            "native common-contract root count diverges from record inventories"
        )
    if summary.node_count < summary.root_count or summary.signature.count > summary.node_count:
        raise CommonContractError(
            "native common-contract node count is inconsistent with its inventories"
        )
    return fingerprints, inventories


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
        _RECORD_INVENTORY_DOMAIN
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


def _encoded_provenance_inventory(
    snapshot: OntologySnapshot,
) -> tuple[dict[str, object], int]:
    bulk_records = getattr(snapshot, "_native_origin_records_v2", None)
    if not callable(bulk_records):
        raise EncodedContractUnavailable(
            "installed native lane did not publish validated retained provenance records"
        )

    origins: list[dict[str, object]] = []
    occurrences: list[dict[str, object]] = []
    active_digest: bytes | None = None
    row_count = 0
    for record in bulk_records():
        if type(record) is not tuple or len(record) != 4:
            raise CommonContractError(
                "bulk retained provenance record is not an exact four-tuple"
            )
        digest, document_key, occurrence, span = record
        if type(digest) is not bytes or len(digest) != 32:
            raise CommonContractError(
                "bulk retained provenance record digest is not exact bytes32"
            )
        if type(document_key) is not str or not document_key:
            raise CommonContractError(
                "bulk retained provenance record document key is not a non-empty exact string"
            )
        if type(occurrence) is not int or not 0 <= occurrence < 2**64:
            raise CommonContractError(
                "bulk retained provenance record occurrence is not a non-negative u64 integer"
            )
        if span is not None and type(span) is not SourceSpan:
            raise CommonContractError(
                "bulk retained provenance record span has the wrong type"
            )
        if active_digest is not None and digest < active_digest:
            raise CommonContractError(
                "bulk retained provenance records are not canonical"
            )
        if digest != active_digest:
            if active_digest is not None:
                origins.append(
                    {
                        "structural_sha256": active_digest.hex(),
                        "occurrences": occurrences,
                    }
                )
            active_digest = digest
            occurrences = []
        occurrences.append(
            {
                "document_key": document_key,
                "occurrence": occurrence,
                "span": None if span is None else span.to_dict(),
            }
        )
        row_count += 1
    if active_digest is not None:
        origins.append(
            {
                "structural_sha256": active_digest.hex(),
                "occurrences": occurrences,
            }
        )
    return (
        {
            "origins": origins,
            "origin_entry_count": len(origins),
            "source_byte_count": snapshot.report.total_source_bytes,
            "document_count": snapshot.report.document_count,
        },
        row_count,
    )


def _optional_canonical(value: StructuralNode | None) -> str | None:
    return None if value is None else canonical_bytes(value).hex()


def _optional_canonical_bytes(value: StructuralNode | None) -> bytes | None:
    return None if value is None else canonical_bytes(value)


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
