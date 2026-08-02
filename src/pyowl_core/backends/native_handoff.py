"""Sealed retained-storage publication contract for the native backend.

The handoff contains bounded immutable metadata and exactly one opaque owning
handle. Ontology-sized values, parser state, mutable builders, and encoded-view
layouts remain behind that handle.
"""

from __future__ import annotations

import hashlib
import math
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from enum import Enum
from typing import Any, Final, NoReturn, cast

from pyowl_core._immutable import FrozenMap
from pyowl_core.config import BackendPreference, DocumentFormat, ImportPolicy, LoadOptions
from pyowl_core.diagnostics import (
    Diagnostic,
    Severity,
    SourceSpan,
    validate_diagnostic_code,
)
from pyowl_core.document.document import Fingerprint, OntologyID
from pyowl_core.document.imports import ImportManifest
from pyowl_core.document.provenance import DetectionBasis, DigestKind, DocumentProvenance
from pyowl_core.exceptions import BackendProtocolError
from pyowl_core.limits import ParseLimits
from pyowl_core.model import IRI, canonical_bytes

NATIVE_SNAPSHOT_PUBLICATION_VERSION: Final = 1
NATIVE_SNAPSHOT_PUBLICATION_LEDGER_DOMAIN: Final = (
    "pyowl-core:native-snapshot-publication-ledger:v1"
)
NATIVE_SNAPSHOT_ATTESTATION_DOMAIN: Final = "pyowl-core:native-snapshot-publication-attestation:v1"
NATIVE_SNAPSHOT_LEDGER_CANONICALIZATION: Final = "typed-toml-tree-v1"
NATIVE_SNAPSHOT_RUST_PARITY_REQUIRED: Final = True

NATIVE_SNAPSHOT_PUBLICATION_FIELDS_V1: Final = (
    (0, "version", "int", "one"),
    (1, "ledger_sha256", "bytes32", "one"),
    (2, "handle", "NativeSnapshotHandleV1", "one"),
    (3, "documents", "tuple[NativeDocumentPublicationV1]", "documents"),
    (4, "import_manifest", "NativeImportManifestPublicationV1", "one"),
    (5, "root_document_key", "str", "one"),
    (6, "load_options", "LoadOptions", "one"),
    (7, "diagnostics", "tuple[NativeDiagnosticPublicationV1]", "diagnostics"),
    (8, "report", "NativeLoadReportPublicationV1", "one"),
    (9, "capability_bits", "u64", "one"),
    (10, "root_table_sha256", "bytes32", "one"),
    (11, "fingerprint_inputs_sha256", "bytes32", "one"),
    (12, "source_manifest_sha256", "bytes32", "one"),
    (13, "provenance_manifest_sha256", "bytes32", "one"),
)

NATIVE_DOCUMENT_PUBLICATION_FIELDS_V1: Final = (
    (0, "document_key", "str", "one"),
    (1, "ontology_id", "OntologyID", "one"),
    (2, "document_iri", "IRI|None", "optional"),
    (3, "direct_imports", "tuple[IRI]", "import-edges"),
    (4, "provenance", "NativeDocumentProvenancePublicationV1", "one"),
    (5, "document_fingerprint", "Fingerprint", "one"),
    (6, "diagnostics", "tuple[NativeDiagnosticPublicationV1]", "diagnostics"),
    (7, "ontology_annotation_count", "u64", "one"),
    (8, "axiom_count", "u64", "one"),
    (9, "extension_count", "u64", "one"),
    (10, "source_map_entry_count", "u64", "one"),
    (11, "origin_entry_count", "u64", "one"),
    (12, "rdf_mapping_conformant", "bool|None", "optional"),
    (13, "rdf_mapping_report_sha256", "bytes32|None", "optional"),
)

NATIVE_DIAGNOSTIC_PUBLICATION_FIELDS_V1: Final = (
    (0, "code", "str", "one"),
    (1, "severity", "str", "one"),
    (2, "message", "str", "one"),
    (3, "document_iri", "str|None", "optional"),
    (4, "byte_start", "u64|None", "optional"),
    (5, "byte_end", "u64|None", "optional"),
    (6, "line_start", "u64|None", "optional"),
    (7, "column_start", "u64|None", "optional"),
    (8, "line_end", "u64|None", "optional"),
    (9, "column_end", "u64|None", "optional"),
    (10, "import_chain", "tuple[str]", "bounded"),
    (11, "details", "tuple[tuple[str,scalar]]", "bounded"),
)

NATIVE_PROVENANCE_PUBLICATION_FIELDS_V1: Final = (
    (0, "source_sha256", "bytes32", "one"),
    (1, "digest_kind", "str", "one"),
    (2, "byte_length", "u64", "one"),
    (3, "decoded_codepoint_length", "u64", "one"),
    (4, "document_iri", "str|None", "optional"),
    (5, "acquisition_locator", "str|None", "optional"),
    (6, "format", "str", "one"),
    (7, "detection_basis", "str", "one"),
    (8, "media_type", "str|None", "optional"),
    (9, "expected_sha256", "bytes32|None", "optional"),
    (10, "parser", "str", "one"),
    (11, "backend", "str", "one"),
    (12, "api_version", "tuple[u32,u32]", "one"),
    (13, "model_schema", "u32", "one"),
)

NATIVE_IMPORT_DOCUMENT_FIELDS_V1: Final = (
    (0, "document_key", "str", "one"),
    (1, "ontology_id", "OntologyID", "one"),
    (2, "document_iri", "IRI|None", "optional"),
    (3, "source_sha256", "bytes32", "one"),
    (4, "document_fingerprint", "Fingerprint", "one"),
    (5, "format", "str", "one"),
    (6, "status", "str", "one"),
)

NATIVE_IMPORT_EDGE_FIELDS_V1: Final = (
    (0, "importing_document_key", "str", "one"),
    (1, "import_iri", "IRI", "one"),
    (2, "status", "str", "one"),
    (3, "resolved_document_key", "str|None", "optional"),
    (4, "resolver_name", "str|None", "optional"),
    (5, "sanitized_locator", "str|None", "optional"),
    (6, "diagnostic", "NativeDiagnosticPublicationV1|None", "optional"),
)

NATIVE_IMPORT_MANIFEST_FIELDS_V1: Final = (
    (0, "policy", "str", "one"),
    (1, "offline", "bool", "one"),
    (2, "resolver_configuration_fingerprint", "bytes32", "one"),
    (3, "documents", "tuple[NativeImportDocumentPublicationV1]", "documents"),
    (4, "edges", "tuple[NativeImportEdgePublicationV1]", "import-edges"),
)

NATIVE_LOAD_REPORT_PUBLICATION_FIELDS_V1: Final = (
    (0, "backend", "str", "one"),
    (1, "api_version", "tuple[u32,u32]", "one"),
    (2, "model_schema", "u32", "one"),
    (3, "document_count", "u64", "one"),
    (4, "total_source_bytes", "u64", "one"),
    (5, "effective_axiom_count", "u64", "one"),
    (6, "resolution_attempts", "u64", "one"),
    (7, "acquisition_cache_hits", "u64", "one"),
    (8, "document_cache_hits", "u64", "one"),
    (9, "timings", "tuple[tuple[str,f64]]", "bounded"),
    (10, "structural_fingerprint", "Fingerprint", "one"),
    (11, "logical_fingerprint", "Fingerprint", "one"),
    (12, "signature_fingerprint", "Fingerprint", "one"),
    (13, "owl2_dl_validated", "bool", "one"),
    (14, "owl2_dl_conforms", "bool|None", "optional"),
    (15, "owl2_dl_report_sha256", "bytes32|None", "optional"),
)

NATIVE_SNAPSHOT_ATTESTATION_FIELDS_V1: Final = (
    (0, "version", "int", "one"),
    (1, "ledger_sha256", "bytes32", "one"),
    (2, "root_table_sha256", "bytes32", "one"),
    (3, "fingerprint_inputs_sha256", "bytes32", "one"),
    (4, "source_manifest_sha256", "bytes32", "one"),
    (5, "provenance_manifest_sha256", "bytes32", "one"),
    (6, "diagnostics_manifest_sha256", "bytes32", "one"),
    (7, "load_options_sha256", "bytes32", "one"),
    (8, "report_sha256", "bytes32", "one"),
    (9, "document_count", "u64", "one"),
    (10, "import_edge_count", "u64", "one"),
    (11, "diagnostic_count", "u64", "one"),
    (12, "ontology_annotation_count", "u64", "one"),
    (13, "stored_axiom_count", "u64", "one"),
    (14, "effective_axiom_count", "u64", "one"),
    (15, "extension_count", "u64", "one"),
    (16, "total_source_bytes", "u64", "one"),
    (17, "source_map_entry_count", "u64", "one"),
    (18, "origin_entry_count", "u64", "one"),
    (19, "rdf_mapping_report_count", "u64", "one"),
    (20, "capability_bits", "u64", "one"),
    (21, "api_version", "tuple[u32,u32]", "one"),
    (22, "model_schema", "u32", "one"),
    (23, "backend", "str", "one"),
    (24, "root_document_key", "str", "one"),
    (25, "owl2_dl_validated", "bool", "one"),
    (26, "owl2_dl_conforms", "bool|None", "optional"),
    (27, "owl2_dl_report_sha256", "bytes32|None", "optional"),
)

NATIVE_SNAPSHOT_HANDLE_MEMBERS_V1: Final = (
    (0, "publication_version", "int", "property"),
    (1, "publication_ledger_sha256", "bytes32", "property"),
    (2, "attestation", "NativeSnapshotAttestationV1", "property"),
    (3, "closed", "bool", "property"),
    (4, "close", "() -> None", "method"),
)

NATIVE_SNAPSHOT_CAPABILITY_BITS_V1: Final = (
    (1, "retained_storage"),
    (2, "lazy_scalar_materialization"),
    (4, "document_scoped_anonymous"),
    (8, "source_map"),
    (16, "origin_index"),
    (32, "rdf_mapping_report"),
)

NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1: Final[FrozenMap[str, int]] = FrozenMap(
    {
        "max_api_component": 2**32 - 1,
        "max_diagnostic_details": 64,
        "max_diagnostic_import_chain": 128,
        "max_diagnostics_per_sequence": 10_000,
        "max_direct_imports_per_document": 10_000_000,
        "max_document_key_utf8_bytes": 256,
        "max_documents": 1_000_000,
        "max_import_edges": 100_000_000,
        "max_iri_utf8_bytes": 1024 * 1024,
        "max_metadata_string_utf8_bytes": 4096,
        "max_timing_name_utf8_bytes": 64,
        "max_timing_rows": 64,
        "max_total_diagnostics": 1_000_000,
    }
)

NATIVE_SNAPSHOT_CAPABILITY_RULES_V1: Final[FrozenMap[str, object]] = FrozenMap(
    {
        "known_mask": 63,
        "required_mask": 7,
        "source_map_bit": 8,
        "origin_index_bit": 16,
        "rdf_mapping_report_bit": 32,
        "source_map_option": "preserve_source_map",
        "origin_index_option": "collect_provenance",
        "table_counts_require_bits": True,
    }
)

NATIVE_SNAPSHOT_LIFECYCLE_V1: Final[FrozenMap[str, str]] = FrozenMap(
    {
        "initial_state": "open",
        "publication_state": "open-only",
        "close": "idempotent-thread-safe",
        "metadata_after_close": "readable",
        "copy": "identity-preserving",
        "pickle": "forbidden",
        "finalization": "owner-release",
    }
)

NATIVE_LOAD_OPTION_FIELDS_V1: Final = (
    "format",
    "imports",
    "backend",
    "limits",
    "offline",
    "preserve_source_map",
    "collect_provenance",
    "validate_owl2_dl",
    "deterministic",
)

NATIVE_PARSE_LIMIT_FIELDS_V1: Final = (
    "max_source_bytes",
    "max_documents",
    "max_total_source_bytes",
    "max_axioms",
    "max_terms",
    "max_nesting_depth",
    "max_rdf_list_length",
    "max_literal_bytes",
    "max_iri_bytes",
    "max_prefixes",
    "max_import_depth",
    "max_redirects",
    "max_diagnostics",
    "max_memory_bytes",
    "deadline_seconds",
    "max_triples",
    "max_strings",
    "max_annotations",
    "max_rule_atoms",
    "max_sequence_arity",
    "max_catalog_rewrites",
    "max_resolver_attempts",
    "max_concurrent_fetches",
    "max_source_map_entries",
    "max_origin_entries",
    "max_overlay_depth",
    "max_delta_entries",
    "max_composite_members",
    "max_index_rows",
    "max_index_bytes",
    "max_wire_rows",
    "max_wire_bytes",
    "max_temporary_bytes",
    "max_disk_cache_bytes",
    "max_decompressed_bytes",
    "max_canonical_work",
    "cancellation_check_interval",
)

_HANDLE_OWNER_ATTESTATION_MEMBER = "_publication_attestation_v1"
_HANDLE_OWNER_CLOSED_MEMBER = "_publication_closed_v1"
_HANDLE_OWNER_CLOSE_MEMBER = "_publication_close_v1"
_RUST_OWNER_MODULE = "pyowl_core._native"
_RUST_OWNER_NAME = "_NativeSnapshotHandle"


def _field_schema(rows: Sequence[tuple[int, str, str, str]], tail: str) -> list[dict[str, object]]:
    return [
        {"ordinal": ordinal, "name": name, "type": type_name, tail: tail_value}
        for ordinal, name, type_name, tail_value in rows
    ]


def native_snapshot_publication_schema_semantics_v1() -> dict[str, object]:
    """Return every digest-bearing TOML semantic except the digest itself."""

    return {
        "schema": NATIVE_SNAPSHOT_PUBLICATION_VERSION,
        "name": "NativeSnapshotPublicationV1",
        "domain": NATIVE_SNAPSHOT_PUBLICATION_LEDGER_DOMAIN,
        "ledger_canonicalization": NATIVE_SNAPSHOT_LEDGER_CANONICALIZATION,
        "extension_policy": "any semantic change requires publication version 2",
        "rust_parity_required": NATIVE_SNAPSHOT_RUST_PARITY_REQUIRED,
        "envelope": {
            "python_type": "NativeSnapshotPublicationV1",
            "construction": "named-only",
            "ownership": "one-opaque-handle",
            "complexity": "O(documents+import-edges+diagnostics)",
            "fields": _field_schema(NATIVE_SNAPSHOT_PUBLICATION_FIELDS_V1, "cardinality"),
        },
        "document": {
            "python_type": "NativeDocumentPublicationV1",
            "construction": "named-only",
            "fields": _field_schema(NATIVE_DOCUMENT_PUBLICATION_FIELDS_V1, "cardinality"),
        },
        "diagnostic": {
            "python_type": "NativeDiagnosticPublicationV1",
            "construction": "named-only",
            "scalar_only": True,
            "fields": _field_schema(NATIVE_DIAGNOSTIC_PUBLICATION_FIELDS_V1, "cardinality"),
        },
        "provenance": {
            "python_type": "NativeDocumentProvenancePublicationV1",
            "construction": "named-only",
            "scalar_only": True,
            "fields": _field_schema(NATIVE_PROVENANCE_PUBLICATION_FIELDS_V1, "cardinality"),
        },
        "import_document": {
            "python_type": "NativeImportDocumentPublicationV1",
            "construction": "named-only",
            "fields": _field_schema(NATIVE_IMPORT_DOCUMENT_FIELDS_V1, "cardinality"),
        },
        "import_edge": {
            "python_type": "NativeImportEdgePublicationV1",
            "construction": "named-only",
            "fields": _field_schema(NATIVE_IMPORT_EDGE_FIELDS_V1, "cardinality"),
        },
        "import_manifest": {
            "python_type": "NativeImportManifestPublicationV1",
            "construction": "named-only",
            "fields": _field_schema(NATIVE_IMPORT_MANIFEST_FIELDS_V1, "cardinality"),
        },
        "report": {
            "python_type": "NativeLoadReportPublicationV1",
            "construction": "named-only",
            "fields": _field_schema(NATIVE_LOAD_REPORT_PUBLICATION_FIELDS_V1, "cardinality"),
        },
        "attestation": {
            "python_type": "NativeSnapshotAttestationV1",
            "construction": "named-only",
            "domain": NATIVE_SNAPSHOT_ATTESTATION_DOMAIN,
            "fields": _field_schema(NATIVE_SNAPSHOT_ATTESTATION_FIELDS_V1, "cardinality"),
        },
        "handle": {
            "python_type": "NativeSnapshotHandleV1",
            "opaque": True,
            "owning": True,
            "sealed": True,
            "registration": "exact-owner-type",
            "members": _field_schema(NATIVE_SNAPSHOT_HANDLE_MEMBERS_V1, "kind"),
        },
        "handle_registration": {
            "rust_owner_module": _RUST_OWNER_MODULE,
            "rust_owner_name": _RUST_OWNER_NAME,
            "owner_attestation_member": _HANDLE_OWNER_ATTESTATION_MEMBER,
            "owner_closed_member": _HANDLE_OWNER_CLOSED_MEMBER,
            "owner_close_member": _HANDLE_OWNER_CLOSE_MEMBER,
            "exact_type_only": True,
            "duplicate_policy": "idempotent-same-type-only",
            "fixture_owner": "bounded-immutable-two-slot-tuple",
        },
        "bounds": dict(NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1),
        "capability_rules": dict(NATIVE_SNAPSHOT_CAPABILITY_RULES_V1),
        "lifecycle": dict(NATIVE_SNAPSHOT_LIFECYCLE_V1),
        "ordering": {
            "documents": "document-key-utf8-ascending-unique",
            "import_edges": "document-key+canonical-iri+status+target-ascending",
            "direct_imports": "canonical-iri-bytes-ascending-unique",
            "diagnostic_sequences": "producer-order-preserved",
            "diagnostic_details": "producer-order-preserved-unique-keys",
            "report_timings": "name-utf8-ascending-unique",
        },
        "dynamic_limits": {
            "documents": "min(bounds.max_documents,options.limits.max_documents)",
            "import_edges": "min(bounds.max_import_edges,options.limits.max_axioms)",
            "diagnostics": "min(bounds.max_total_diagnostics,options.limits.max_diagnostics)",
            "source_map_entries": "options.limits.max_source_map_entries",
            "origin_entries": "options.limits.max_origin_entries",
        },
        "metadata_rules": {
            "diagnostic_code": "^[A-Z][A-Z0-9_]*$",
            "diagnostic_severity": "info|warning|error",
            "diagnostic_spans": "nonnegative-bytes+positive-text+forward-only",
            "provenance_digest_kind": "exact-bytes|normalized-text",
            "document_format": "rdfxml|turtle|owlxml|functional",
            "detection_basis": "explicit|media-type|content|extension",
            "scalar_details": "str|i64|bool",
        },
        "attestation_encoding": {
            "digest": "sha256",
            "record": "ordinal-field-sequence",
            "scalar_codec": "tagged-length-framed-v1",
            "float": "finite-python-hex",
            "load_options_domain": "pyowl-core:native-load-options:v1",
            "report_domain": "pyowl-core:native-load-report:v1",
            "diagnostics_domain": "pyowl-core:native-diagnostics-manifest:v1",
        },
        "capability_bits": [
            {"value": value, "name": name} for value, name in NATIVE_SNAPSHOT_CAPABILITY_BITS_V1
        ],
        "attestation_bindings": {
            "four_digest_fields": [
                "root_table_sha256",
                "fingerprint_inputs_sha256",
                "source_manifest_sha256",
                "provenance_manifest_sha256",
            ],
            "load_option_fields": list(NATIVE_LOAD_OPTION_FIELDS_V1),
            "parse_limit_fields": list(NATIVE_PARSE_LIMIT_FIELDS_V1),
            "report_fields": [row[1] for row in NATIVE_LOAD_REPORT_PUBLICATION_FIELDS_V1],
            "capability_backed_counts": [
                "source_map_entry_count",
                "origin_entry_count",
                "rdf_mapping_report_count",
            ],
            "source_map_table": "source_manifest_sha256+source_map_entry_count",
            "origin_table": "provenance_manifest_sha256+origin_entry_count",
            "rdf_mapping_tables": (
                "documents[].rdf_mapping_report_sha256+rdf_mapping_report_count"
            ),
        },
        "rust_parity": {
            "required": True,
            "record": "NativeSnapshotPublicationV1",
            "attestation": "NativeSnapshotAttestationV1",
            "status_claim": "none-until-runtime-registration",
        },
    }


def _frame(value: bytes) -> bytes:
    return str(len(value)).encode("ascii") + b":" + value


def _canonical_schema_value(value: object) -> bytes:
    if isinstance(value, bool):
        return b"b1" if value else b"b0"
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii") + b";"
    if isinstance(value, str):
        return b"s" + _frame(value.encode("utf-8"))
    if isinstance(value, list):
        return b"l" + _frame(b"".join(_frame(_canonical_schema_value(item)) for item in value))
    if isinstance(value, Mapping):
        keys = list(value)
        if not all(isinstance(key, str) for key in keys):
            raise TypeError("schema mapping keys must be strings")
        keys.sort(key=lambda key: cast(str, key).encode("utf-8"))
        body = bytearray()
        for key_object in keys:
            key = cast(str, key_object)
            body.extend(_frame(key.encode("utf-8")))
            body.extend(_frame(_canonical_schema_value(value[key])))
        return b"m" + _frame(bytes(body))
    raise TypeError(f"unsupported schema semantic: {type(value).__qualname__}")


def native_snapshot_publication_ledger_bytes_v1() -> bytes:
    """Canonicalize the complete digest-bearing schema semantic tree."""

    prefix = b"pyowl-core:typed-toml-tree:v1\x00"
    return prefix + _canonical_schema_value(native_snapshot_publication_schema_semantics_v1())


NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256: Final = hashlib.sha256(
    native_snapshot_publication_ledger_bytes_v1()
).digest()

_CAPABILITY_RETAINED_STORAGE = 1
_CAPABILITY_LAZY_SCALARS = 2
_CAPABILITY_DOCUMENT_SCOPES = 4
_CAPABILITY_SOURCE_MAP = 8
_CAPABILITY_ORIGIN_INDEX = 16
_CAPABILITY_RDF_MAPPING_REPORT = 32
_REQUIRED_CAPABILITY_BITS = 7
_KNOWN_CAPABILITY_BITS = 63
_PUBLICATION_FIELD_NAMES = tuple(row[1] for row in NATIVE_SNAPSHOT_PUBLICATION_FIELDS_V1)


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeDiagnosticPublicationV1:
    """Deep-frozen scalar-only diagnostic metadata."""

    code: str
    severity: str
    message: str
    document_iri: str | None
    byte_start: int | None
    byte_end: int | None
    line_start: int | None
    column_start: int | None
    line_end: int | None
    column_end: int | None
    import_chain: tuple[str, ...]
    details: tuple[tuple[str, str | int | bool], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _copy_string("diagnostic.code", self.code))
        validate_diagnostic_code(self.code)
        if self.severity not in {item.value for item in Severity}:
            raise ValueError("diagnostic.severity is invalid")
        object.__setattr__(self, "severity", _copy_string("diagnostic.severity", self.severity))
        object.__setattr__(self, "message", _copy_string("diagnostic.message", self.message))
        object.__setattr__(
            self,
            "document_iri",
            _copy_optional_string("diagnostic.document_iri", self.document_iri, iri=True),
        )
        for name in (
            "byte_start",
            "byte_end",
        ):
            _require_optional_u64(name, getattr(self, name))
        for name in ("line_start", "column_start", "line_end", "column_end"):
            _require_optional_positive_u64(name, getattr(self, name))
        if (
            self.byte_start is not None
            and self.byte_end is not None
            and self.byte_end < self.byte_start
        ):
            raise ValueError("diagnostic byte span is reversed")
        if self.line_start is not None and self.line_end is not None:
            start_column = self.column_start or 1
            end_column = self.column_end or 1
            if (self.line_end, end_column) < (self.line_start, start_column):
                raise ValueError("diagnostic text span is reversed")
        if len(self.import_chain) > _bound("max_diagnostic_import_chain"):
            raise ValueError("diagnostic import chain exceeds the publication bound")
        chain = tuple(
            _copy_string("diagnostic.import_chain", value, iri=True) for value in self.import_chain
        )
        if len(self.details) > _bound("max_diagnostic_details"):
            raise ValueError("diagnostic details exceed the publication bound")
        details: list[tuple[str, str | int | bool]] = []
        seen: set[str] = set()
        for key, value in self.details:
            copied_key = _copy_string("diagnostic.detail key", key)
            if copied_key in seen:
                raise ValueError("diagnostic detail keys must be unique")
            seen.add(copied_key)
            if isinstance(value, str):
                copied_value: str | int | bool = _copy_string("diagnostic.detail value", value)
            elif isinstance(value, bool) or (isinstance(value, int) and -(2**63) <= value < 2**63):
                copied_value = value
            else:
                raise TypeError("diagnostic details must contain bounded scalar values")
            details.append((copied_key, copied_value))
        object.__setattr__(self, "import_chain", chain)
        object.__setattr__(self, "details", tuple(details))


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeDocumentProvenancePublicationV1:
    """Deep-frozen scalar-only document provenance metadata."""

    source_sha256: bytes
    digest_kind: str
    byte_length: int
    decoded_codepoint_length: int
    document_iri: str | None
    acquisition_locator: str | None
    format: str
    detection_basis: str
    media_type: str | None
    expected_sha256: bytes | None
    parser: str
    backend: str
    api_version: tuple[int, int]
    model_schema: int

    def __post_init__(self) -> None:
        _require_digest("provenance.source_sha256", self.source_sha256)
        _require_nonnegative_u64("provenance.byte_length", self.byte_length)
        _require_nonnegative_u64(
            "provenance.decoded_codepoint_length", self.decoded_codepoint_length
        )
        for name in ("digest_kind", "format", "detection_basis", "parser", "backend"):
            object.__setattr__(self, name, _copy_string(f"provenance.{name}", getattr(self, name)))
        if self.digest_kind not in {item.value for item in DigestKind}:
            raise ValueError("provenance.digest_kind is invalid")
        if self.format not in {item.value for item in DocumentFormat}:
            raise ValueError("provenance.format is invalid")
        if self.detection_basis not in {item.value for item in DetectionBasis}:
            raise ValueError("provenance.detection_basis is invalid")
        for name, iri in (
            ("document_iri", True),
            ("acquisition_locator", False),
            ("media_type", False),
        ):
            object.__setattr__(
                self,
                name,
                _copy_optional_string(f"provenance.{name}", getattr(self, name), iri=iri),
            )
        if self.expected_sha256 is not None:
            _require_digest("provenance.expected_sha256", self.expected_sha256)
        _require_api_version("provenance.api_version", self.api_version)
        _require_nonnegative_u32("provenance.model_schema", self.model_schema)


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeImportDocumentPublicationV1:
    document_key: str
    ontology_id: OntologyID
    document_iri: IRI | None
    source_sha256: bytes
    document_fingerprint: Fingerprint
    format: str
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "document_key", _copy_document_key(self.document_key))
        _validate_ontology_metadata(self.ontology_id, self.document_iri)
        _require_digest("import document source_sha256", self.source_sha256)
        _require_fingerprint("import document fingerprint", self.document_fingerprint)
        object.__setattr__(self, "format", _copy_string("import document format", self.format))
        object.__setattr__(self, "status", _copy_string("import document status", self.status))
        if self.format not in {item.value for item in DocumentFormat}:
            raise ValueError("import document format is invalid")
        if self.status not in {"root", "resolved"}:
            raise ValueError("import document status is invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeImportEdgePublicationV1:
    importing_document_key: str
    import_iri: IRI
    status: str
    resolved_document_key: str | None
    resolver_name: str | None
    sanitized_locator: str | None
    diagnostic: NativeDiagnosticPublicationV1 | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "importing_document_key", _copy_document_key(self.importing_document_key)
        )
        _require_iri("import edge IRI", self.import_iri)
        object.__setattr__(self, "status", _copy_string("import edge status", self.status))
        if self.status not in {"resolved", "unresolved", "ignored", "denied", "failed"}:
            raise ValueError("import edge status is invalid")
        object.__setattr__(
            self,
            "resolved_document_key",
            _copy_optional_document_key(self.resolved_document_key),
        )
        for name in ("resolver_name", "sanitized_locator"):
            object.__setattr__(
                self,
                name,
                _copy_optional_string(f"import edge {name}", getattr(self, name)),
            )
        if self.status == "resolved" and self.resolved_document_key is None:
            raise ValueError("resolved import edge requires a target")
        if self.status != "resolved" and self.resolved_document_key is not None:
            raise ValueError("only resolved import edges may have a target")
        if (
            self.diagnostic is not None
            and type(self.diagnostic) is not NativeDiagnosticPublicationV1
        ):
            raise TypeError("import edge diagnostic must be frozen publication metadata")


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeImportManifestPublicationV1:
    policy: str
    offline: bool
    resolver_configuration_fingerprint: bytes
    documents: tuple[NativeImportDocumentPublicationV1, ...]
    edges: tuple[NativeImportEdgePublicationV1, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy", _copy_string("import policy", self.policy))
        if self.policy not in {item.value for item in ImportPolicy}:
            raise ValueError("import policy is invalid")
        if not isinstance(self.offline, bool):
            raise TypeError("import manifest offline must be bool")
        _require_digest(
            "resolver_configuration_fingerprint", self.resolver_configuration_fingerprint
        )
        documents = tuple(self.documents)
        edges = tuple(self.edges)
        if len(documents) > _bound("max_documents"):
            raise ValueError("import documents exceed the publication bound")
        if len(edges) > _bound("max_import_edges"):
            raise ValueError("import edges exceed the publication bound")
        if not all(type(item) is NativeImportDocumentPublicationV1 for item in documents):
            raise TypeError("import manifest documents must be frozen publication records")
        if not all(type(item) is NativeImportEdgePublicationV1 for item in edges):
            raise TypeError("import manifest edges must be frozen publication records")
        previous_document_key: bytes | None = None
        document_keys: set[str] = set()
        for document in documents:
            encoded_key = document.document_key.encode("utf-8")
            if previous_document_key is not None and encoded_key <= previous_document_key:
                raise ValueError("import documents must be unique and canonically ordered")
            previous_document_key = encoded_key
            document_keys.add(document.document_key)
        previous_edge_key: tuple[object, ...] | None = None
        for edge in edges:
            edge_key: tuple[object, ...] = (
                edge.importing_document_key.encode("utf-8"),
                canonical_bytes(edge.import_iri),
                edge.status,
                edge.resolved_document_key or "",
            )
            if previous_edge_key is not None and edge_key < previous_edge_key:
                raise ValueError("import edges must be canonically ordered")
            previous_edge_key = edge_key
            if edge.importing_document_key not in document_keys:
                raise ValueError("import edge source is absent from document records")
            if (
                edge.resolved_document_key is not None
                and edge.resolved_document_key not in document_keys
            ):
                raise ValueError("import edge target is absent from document records")
        object.__setattr__(self, "documents", documents)
        object.__setattr__(self, "edges", edges)


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeDocumentPublicationV1:
    """Bounded immutable facade metadata for one retained document."""

    document_key: str
    ontology_id: OntologyID
    document_iri: IRI | None
    direct_imports: tuple[IRI, ...]
    provenance: NativeDocumentProvenancePublicationV1
    document_fingerprint: Fingerprint
    diagnostics: tuple[NativeDiagnosticPublicationV1, ...]
    ontology_annotation_count: int
    axiom_count: int
    extension_count: int
    source_map_entry_count: int
    origin_entry_count: int
    rdf_mapping_conformant: bool | None
    rdf_mapping_report_sha256: bytes | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "document_key", _copy_document_key(self.document_key))
        _validate_ontology_metadata(self.ontology_id, self.document_iri)
        direct_imports = tuple(self.direct_imports)
        if len(direct_imports) > _bound("max_direct_imports_per_document"):
            raise ValueError("direct imports exceed the publication bound")
        previous: bytes | None = None
        seen: set[bytes] = set()
        for import_iri in direct_imports:
            _require_iri("direct import", import_iri)
            encoded = canonical_bytes(import_iri)
            if encoded in seen or (previous is not None and encoded < previous):
                raise ValueError("direct imports must be unique and canonically ordered")
            seen.add(encoded)
            previous = encoded
        if type(self.provenance) is not NativeDocumentProvenancePublicationV1:
            raise TypeError("provenance must be frozen scalar-only publication metadata")
        _require_fingerprint("document_fingerprint", self.document_fingerprint)
        diagnostics = tuple(self.diagnostics)
        _validate_diagnostic_sequence("document diagnostics", diagnostics)
        for name in (
            "ontology_annotation_count",
            "axiom_count",
            "extension_count",
            "source_map_entry_count",
            "origin_entry_count",
        ):
            _require_nonnegative_u64(name, getattr(self, name))
        if self.rdf_mapping_conformant is not None and not isinstance(
            self.rdf_mapping_conformant, bool
        ):
            raise TypeError("rdf_mapping_conformant must be bool or None")
        if (self.rdf_mapping_conformant is None) != (self.rdf_mapping_report_sha256 is None):
            raise ValueError("RDF mapping result and digest must be present together")
        if self.rdf_mapping_report_sha256 is not None:
            _require_digest("rdf_mapping_report_sha256", self.rdf_mapping_report_sha256)
        object.__setattr__(self, "direct_imports", direct_imports)
        object.__setattr__(self, "diagnostics", diagnostics)


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeLoadReportPublicationV1:
    """Ontology-size-independent metadata needed to publish a load report."""

    backend: str
    api_version: tuple[int, int]
    model_schema: int
    document_count: int
    total_source_bytes: int
    effective_axiom_count: int
    resolution_attempts: int
    acquisition_cache_hits: int
    document_cache_hits: int
    timings: tuple[tuple[str, float], ...]
    structural_fingerprint: Fingerprint
    logical_fingerprint: Fingerprint
    signature_fingerprint: Fingerprint
    owl2_dl_validated: bool
    owl2_dl_conforms: bool | None
    owl2_dl_report_sha256: bytes | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "backend", _copy_string("report.backend", self.backend))
        if self.backend != "native":
            raise ValueError("native publication report backend must be 'native'")
        _require_api_version("report.api_version", self.api_version)
        _require_nonnegative_u32("report.model_schema", self.model_schema)
        for name in (
            "document_count",
            "total_source_bytes",
            "effective_axiom_count",
            "resolution_attempts",
            "acquisition_cache_hits",
            "document_cache_hits",
        ):
            _require_nonnegative_u64(name, getattr(self, name))
        timings = tuple(self.timings)
        if len(timings) > _bound("max_timing_rows"):
            raise ValueError("native publication report has too many timing rows")
        previous_name: bytes | None = None
        frozen_timings: list[tuple[str, float]] = []
        for name, value in timings:
            copied_name = _copy_string(
                "report timing name",
                name,
                byte_limit=_bound("max_timing_name_utf8_bytes"),
            )
            encoded_name = copied_name.encode("utf-8")
            if previous_name is not None and encoded_name <= previous_name:
                raise ValueError("report timing names must be unique and canonically ordered")
            previous_name = encoded_name
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError("report timings must contain numbers")
            numeric = float(value)
            if not math.isfinite(numeric) or numeric < 0:
                raise ValueError("report timings must be finite and nonnegative")
            frozen_timings.append((copied_name, numeric))
        for name in (
            "structural_fingerprint",
            "logical_fingerprint",
            "signature_fingerprint",
        ):
            _require_fingerprint(name, getattr(self, name))
        if not isinstance(self.owl2_dl_validated, bool):
            raise TypeError("owl2_dl_validated must be bool")
        if self.owl2_dl_conforms is not None and not isinstance(self.owl2_dl_conforms, bool):
            raise TypeError("owl2_dl_conforms must be bool or None")
        if self.owl2_dl_validated:
            if self.owl2_dl_conforms is None or self.owl2_dl_report_sha256 is None:
                raise ValueError("validated OWL 2 DL report requires result metadata")
            _require_digest("owl2_dl_report_sha256", self.owl2_dl_report_sha256)
        elif self.owl2_dl_conforms is not None or self.owl2_dl_report_sha256 is not None:
            raise ValueError("unvalidated OWL 2 DL report cannot publish result metadata")
        object.__setattr__(self, "timings", tuple(frozen_timings))


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeSnapshotAttestationV1:
    """Domain-separated immutable claims owned by the native storage handle."""

    version: int
    ledger_sha256: bytes
    root_table_sha256: bytes
    fingerprint_inputs_sha256: bytes
    source_manifest_sha256: bytes
    provenance_manifest_sha256: bytes
    diagnostics_manifest_sha256: bytes
    load_options_sha256: bytes
    report_sha256: bytes
    document_count: int
    import_edge_count: int
    diagnostic_count: int
    ontology_annotation_count: int
    stored_axiom_count: int
    effective_axiom_count: int
    extension_count: int
    total_source_bytes: int
    source_map_entry_count: int
    origin_entry_count: int
    rdf_mapping_report_count: int
    capability_bits: int
    api_version: tuple[int, int]
    model_schema: int
    backend: str
    root_document_key: str
    owl2_dl_validated: bool
    owl2_dl_conforms: bool | None
    owl2_dl_report_sha256: bytes | None

    def __post_init__(self) -> None:
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version != NATIVE_SNAPSHOT_PUBLICATION_VERSION
        ):
            raise ValueError("attestation publication version is unsupported")
        for name in (
            "ledger_sha256",
            "root_table_sha256",
            "fingerprint_inputs_sha256",
            "source_manifest_sha256",
            "provenance_manifest_sha256",
            "diagnostics_manifest_sha256",
            "load_options_sha256",
            "report_sha256",
        ):
            _require_digest(f"attestation.{name}", getattr(self, name))
        for name in (
            "document_count",
            "import_edge_count",
            "diagnostic_count",
            "ontology_annotation_count",
            "stored_axiom_count",
            "effective_axiom_count",
            "extension_count",
            "total_source_bytes",
            "source_map_entry_count",
            "origin_entry_count",
            "rdf_mapping_report_count",
            "capability_bits",
        ):
            _require_nonnegative_u64(f"attestation.{name}", getattr(self, name))
        _require_api_version("attestation.api_version", self.api_version)
        _require_nonnegative_u32("attestation.model_schema", self.model_schema)
        object.__setattr__(self, "backend", _copy_string("attestation.backend", self.backend))
        object.__setattr__(self, "root_document_key", _copy_document_key(self.root_document_key))
        if not isinstance(self.owl2_dl_validated, bool):
            raise TypeError("attestation owl2_dl_validated must be bool")
        if self.owl2_dl_conforms is not None and not isinstance(self.owl2_dl_conforms, bool):
            raise TypeError("attestation owl2_dl_conforms must be bool or None")
        if self.owl2_dl_report_sha256 is not None:
            _require_digest("attestation.owl2_dl_report_sha256", self.owl2_dl_report_sha256)

    @property
    def digest(self) -> bytes:
        return hashlib.sha256(native_snapshot_attestation_bytes_v1(self)).digest()


class _GeneratedHandleLifecycleV1:
    __slots__ = ("_closed", "_lock")

    def __init__(self) -> None:
        self._closed = False
        self._lock = threading.Lock()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def close(self) -> None:
        with self._lock:
            self._closed = True


class _GeneratedNativeSnapshotOwnerV1(tuple[object, ...]):
    """Bounded generated owner used only by publication contract fixtures."""

    __slots__ = ()

    def __new__(cls, attestation: NativeSnapshotAttestationV1) -> _GeneratedNativeSnapshotOwnerV1:
        if type(attestation) is not NativeSnapshotAttestationV1:
            raise TypeError("generated owner requires an exact attestation record")
        return tuple.__new__(cls, (attestation, _GeneratedHandleLifecycleV1()))

    def _publication_attestation_v1(self) -> NativeSnapshotAttestationV1:
        return cast(NativeSnapshotAttestationV1, tuple.__getitem__(self, 0))

    def _publication_closed_v1(self) -> bool:
        lifecycle = cast(_GeneratedHandleLifecycleV1, tuple.__getitem__(self, 1))
        return lifecycle.closed

    def _publication_close_v1(self) -> None:
        lifecycle = cast(_GeneratedHandleLifecycleV1, tuple.__getitem__(self, 1))
        lifecycle.close()


_REGISTERED_OWNER_TYPES: set[type[object]] = {_GeneratedNativeSnapshotOwnerV1}
_registered_rust_owner_type: type[object] | None = None


class NativeSnapshotHandleV1:
    """Exact sealed wrapper around one registered native storage owner."""

    __slots__ = ("__owner",)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("NativeSnapshotHandleV1 is created only from a registered owner")

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("NativeSnapshotHandleV1 is sealed")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("NativeSnapshotHandleV1 is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("NativeSnapshotHandleV1 is immutable")

    @property
    def publication_version(self) -> int:
        return self.attestation.version

    @property
    def publication_ledger_sha256(self) -> bytes:
        return self.attestation.ledger_sha256

    @property
    def attestation(self) -> NativeSnapshotAttestationV1:
        owner = object.__getattribute__(self, "_NativeSnapshotHandleV1__owner")
        method = getattr(owner, _HANDLE_OWNER_ATTESTATION_MEMBER)
        value = method()
        if type(value) is not NativeSnapshotAttestationV1:
            _fail("registered handle returned an invalid attestation", "NATIVE_HANDLE_OWNER")
        return cast(NativeSnapshotAttestationV1, value)

    @property
    def closed(self) -> bool:
        owner = object.__getattribute__(self, "_NativeSnapshotHandleV1__owner")
        method = getattr(owner, _HANDLE_OWNER_CLOSED_MEMBER)
        value = method()
        if not isinstance(value, bool):
            _fail("registered handle returned invalid lifecycle state", "NATIVE_HANDLE_OWNER")
        return cast(bool, value)

    def close(self) -> None:
        owner = object.__getattribute__(self, "_NativeSnapshotHandleV1__owner")
        method = getattr(owner, _HANDLE_OWNER_CLOSE_MEMBER)
        result = method()
        if result is not None:
            _fail("registered handle close returned a value", "NATIVE_HANDLE_OWNER")

    def __copy__(self) -> NativeSnapshotHandleV1:
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> NativeSnapshotHandleV1:
        return self

    def __reduce__(self) -> NoReturn:
        raise TypeError("NativeSnapshotHandleV1 cannot be pickled")

    def __repr__(self) -> str:
        return f"NativeSnapshotHandleV1(closed={self.closed!r})"


def _seal_native_snapshot_owner_v1(owner: object) -> NativeSnapshotHandleV1:
    if type(owner) not in _REGISTERED_OWNER_TYPES:
        _fail("native snapshot owner type is not registered", "NATIVE_HANDLE_TYPE")
    handle = object.__new__(NativeSnapshotHandleV1)
    object.__setattr__(handle, "_NativeSnapshotHandleV1__owner", owner)
    return handle


def _generated_native_snapshot_handle_v1(
    attestation: NativeSnapshotAttestationV1,
) -> NativeSnapshotHandleV1:
    """Create the bounded generated fake used by WP16/WP17 contract fixtures."""

    return _seal_native_snapshot_owner_v1(_GeneratedNativeSnapshotOwnerV1(attestation))


def _register_rust_native_snapshot_handle_v1(owner_type: type[object]) -> None:
    """Register the exact extension-owned handle type once Rust parity exists."""

    global _registered_rust_owner_type
    module = sys.modules.get(_RUST_OWNER_MODULE)
    if (
        not isinstance(owner_type, type)
        or owner_type.__module__ != _RUST_OWNER_MODULE
        or owner_type.__name__ != _RUST_OWNER_NAME
        or module is None
        or getattr(module, _RUST_OWNER_NAME, None) is not owner_type
    ):
        raise TypeError("Rust publication owner must be the exact registered extension type")
    for member in (
        _HANDLE_OWNER_ATTESTATION_MEMBER,
        _HANDLE_OWNER_CLOSED_MEMBER,
        _HANDLE_OWNER_CLOSE_MEMBER,
    ):
        if not callable(getattr(owner_type, member, None)):
            raise TypeError(f"Rust publication owner lacks required member {member}")
    if _registered_rust_owner_type not in {None, owner_type}:
        raise RuntimeError("a different Rust publication owner is already registered")
    _registered_rust_owner_type = owner_type
    _REGISTERED_OWNER_TYPES.add(owner_type)


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeSnapshotPublicationV1:
    """Exact version-1 retained-storage publication envelope."""

    version: int
    ledger_sha256: bytes
    handle: NativeSnapshotHandleV1
    documents: tuple[NativeDocumentPublicationV1, ...]
    import_manifest: NativeImportManifestPublicationV1
    root_document_key: str
    load_options: LoadOptions
    diagnostics: tuple[NativeDiagnosticPublicationV1, ...]
    report: NativeLoadReportPublicationV1
    capability_bits: int
    root_table_sha256: bytes
    fingerprint_inputs_sha256: bytes
    source_manifest_sha256: bytes
    provenance_manifest_sha256: bytes

    def __post_init__(self) -> None:
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version != NATIVE_SNAPSHOT_PUBLICATION_VERSION
        ):
            _fail(
                "native snapshot publication version is unsupported", "NATIVE_PUBLICATION_VERSION"
            )
        _require_digest("ledger_sha256", self.ledger_sha256)
        if self.ledger_sha256 != NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256:
            _fail("native snapshot publication ledger does not match", "NATIVE_PUBLICATION_LEDGER")
        if type(self.handle) is not NativeSnapshotHandleV1:
            _fail("native snapshot publication handle is not sealed", "NATIVE_HANDLE_TYPE")
        documents = tuple(self.documents)
        if not documents or not all(
            type(item) is NativeDocumentPublicationV1 for item in documents
        ):
            _fail(
                "native snapshot publication documents are invalid", "NATIVE_PUBLICATION_DOCUMENTS"
            )
        if type(self.import_manifest) is not NativeImportManifestPublicationV1:
            _fail("native snapshot import manifest is not frozen", "NATIVE_PUBLICATION_MANIFEST")
        object.__setattr__(self, "root_document_key", _copy_document_key(self.root_document_key))
        if type(self.load_options) is not LoadOptions:
            _fail(
                "native snapshot publication load options are invalid", "NATIVE_PUBLICATION_OPTIONS"
            )
        diagnostics = tuple(self.diagnostics)
        _validate_diagnostic_sequence("publication diagnostics", diagnostics)
        if type(self.report) is not NativeLoadReportPublicationV1:
            _fail("native snapshot publication report is invalid", "NATIVE_PUBLICATION_REPORT")
        for name in (
            "root_table_sha256",
            "fingerprint_inputs_sha256",
            "source_manifest_sha256",
            "provenance_manifest_sha256",
        ):
            _require_digest(name, getattr(self, name))
        expected = native_snapshot_publication_attestation_v1(
            documents=documents,
            import_manifest=self.import_manifest,
            root_document_key=self.root_document_key,
            load_options=self.load_options,
            diagnostics=diagnostics,
            report=self.report,
            capability_bits=self.capability_bits,
            root_table_sha256=self.root_table_sha256,
            fingerprint_inputs_sha256=self.fingerprint_inputs_sha256,
            source_manifest_sha256=self.source_manifest_sha256,
            provenance_manifest_sha256=self.provenance_manifest_sha256,
        )
        _validate_handle(self.handle, expected)
        object.__setattr__(self, "documents", documents)
        object.__setattr__(self, "diagnostics", diagnostics)


def freeze_native_snapshot_publication_v1(
    fields_value: Mapping[str, object],
) -> NativeSnapshotPublicationV1:
    """Validate an exact named-field record and freeze publication metadata."""

    if not isinstance(fields_value, Mapping):
        raise TypeError("native snapshot publication fields must be a mapping")
    if not all(isinstance(name, str) for name in fields_value):
        _fail("native snapshot field names must be strings", "NATIVE_PUBLICATION_FIELDS")
    observed = set(fields_value)
    expected = set(_PUBLICATION_FIELD_NAMES)
    if observed != expected:
        missing = [name for name in _PUBLICATION_FIELD_NAMES if name not in observed]
        unknown = [name for name in fields_value if name not in expected]
        message = (
            "native snapshot fields do not match version 1 "
            f"(missing={missing!r}, unknown={unknown!r})"
        )
        _fail(
            message,
            "NATIVE_PUBLICATION_FIELDS",
        )
    return NativeSnapshotPublicationV1(**cast(Any, dict(fields_value)))


def freeze_native_diagnostic_publication_v1(
    diagnostic: Diagnostic | NativeDiagnosticPublicationV1,
) -> NativeDiagnosticPublicationV1:
    """Deep-copy a public diagnostic into scalar-only publication metadata."""

    if type(diagnostic) is NativeDiagnosticPublicationV1:
        return diagnostic
    if not isinstance(diagnostic, Diagnostic):
        raise TypeError("diagnostic must be Diagnostic")
    document_iri = _diagnostic_reference(diagnostic.document_iri)
    chain = tuple(_diagnostic_reference(item, required=True) for item in diagnostic.import_chain)
    span = diagnostic.source_span
    if span is not None and type(span) is not SourceSpan:
        raise TypeError("diagnostic source span must be exact SourceSpan metadata")
    return NativeDiagnosticPublicationV1(
        code=diagnostic.code,
        severity=diagnostic.severity.value,
        message=diagnostic.message,
        document_iri=document_iri,
        byte_start=None if span is None else span.byte_start,
        byte_end=None if span is None else span.byte_end,
        line_start=None if span is None else span.line_start,
        column_start=None if span is None else span.column_start,
        line_end=None if span is None else span.line_end,
        column_end=None if span is None else span.column_end,
        import_chain=cast(tuple[str, ...], chain),
        details=tuple(diagnostic.details.items()),
    )


def freeze_native_provenance_publication_v1(
    provenance: DocumentProvenance | NativeDocumentProvenancePublicationV1,
) -> NativeDocumentProvenancePublicationV1:
    """Deep-copy public provenance into scalar-only publication metadata."""

    if type(provenance) is NativeDocumentProvenancePublicationV1:
        return provenance
    if not isinstance(provenance, DocumentProvenance):
        raise TypeError("provenance must be DocumentProvenance")
    return NativeDocumentProvenancePublicationV1(
        source_sha256=bytes(provenance.source_sha256),
        digest_kind=provenance.digest_kind.value,
        byte_length=provenance.byte_length,
        decoded_codepoint_length=provenance.decoded_codepoint_length,
        document_iri=None if provenance.document_iri is None else provenance.document_iri.value,
        acquisition_locator=provenance.acquisition_locator,
        format=provenance.format.value,
        detection_basis=provenance.detection_basis.value,
        media_type=provenance.media_type,
        expected_sha256=(
            None if provenance.expected_sha256 is None else bytes(provenance.expected_sha256)
        ),
        parser=provenance.parser,
        backend=provenance.backend,
        api_version=(provenance.api_version[0], provenance.api_version[1]),
        model_schema=provenance.model_schema,
    )


def freeze_native_import_manifest_publication_v1(
    manifest: ImportManifest | NativeImportManifestPublicationV1,
) -> NativeImportManifestPublicationV1:
    """Deep-copy an import manifest without sorting or retaining diagnostics."""

    if type(manifest) is NativeImportManifestPublicationV1:
        return manifest
    if not isinstance(manifest, ImportManifest):
        raise TypeError("manifest must be ImportManifest")
    documents = tuple(
        NativeImportDocumentPublicationV1(
            document_key=record.document_key,
            ontology_id=_copy_ontology_id(record.ontology_id),
            document_iri=_copy_iri(record.document_iri),
            source_sha256=bytes(record.source_sha256),
            document_fingerprint=_copy_fingerprint(record.document_fingerprint),
            format=record.format.value,
            status=record.status.value,
        )
        for record in manifest.documents
    )
    edges = tuple(
        NativeImportEdgePublicationV1(
            importing_document_key=edge.importing_document_key,
            import_iri=cast(IRI, _copy_iri(edge.import_iri)),
            status=edge.status.value,
            resolved_document_key=edge.resolved_document_key,
            resolver_name=edge.resolver_name,
            sanitized_locator=edge.sanitized_locator,
            diagnostic=(
                None
                if edge.diagnostic is None
                else freeze_native_diagnostic_publication_v1(edge.diagnostic)
            ),
        )
        for edge in manifest.edges
    )
    return NativeImportManifestPublicationV1(
        policy=manifest.policy.value,
        offline=manifest.offline,
        resolver_configuration_fingerprint=bytes(manifest.resolver_configuration_fingerprint),
        documents=documents,
        edges=edges,
    )


def native_snapshot_publication_attestation_v1(
    *,
    documents: tuple[NativeDocumentPublicationV1, ...],
    import_manifest: NativeImportManifestPublicationV1,
    root_document_key: str,
    load_options: LoadOptions,
    diagnostics: tuple[NativeDiagnosticPublicationV1, ...],
    report: NativeLoadReportPublicationV1,
    capability_bits: int,
    root_table_sha256: bytes,
    fingerprint_inputs_sha256: bytes,
    source_manifest_sha256: bytes,
    provenance_manifest_sha256: bytes,
) -> NativeSnapshotAttestationV1:
    """Validate bounded claims and produce the exact handle-owned attestation."""

    _validate_publication_alignment(
        documents,
        import_manifest,
        root_document_key,
        load_options,
        diagnostics,
        report,
        capability_bits,
    )
    annotation_count = _checked_sum(
        "ontology annotation count",
        (document.ontology_annotation_count for document in documents),
    )
    stored_axiom_count = _checked_sum(
        "stored axiom count", (document.axiom_count for document in documents)
    )
    extension_count = _checked_sum(
        "extension count", (document.extension_count for document in documents)
    )
    source_map_count = _checked_sum(
        "source-map count", (document.source_map_entry_count for document in documents)
    )
    origin_count = _checked_sum(
        "origin count", (document.origin_entry_count for document in documents)
    )
    rdf_report_count = sum(document.rdf_mapping_report_sha256 is not None for document in documents)
    diagnostic_count = (
        len(diagnostics)
        + sum(len(document.diagnostics) for document in documents)
        + sum(edge.diagnostic is not None for edge in import_manifest.edges)
    )
    _require_nonnegative_u64("diagnostic_count", diagnostic_count)
    return NativeSnapshotAttestationV1(
        version=NATIVE_SNAPSHOT_PUBLICATION_VERSION,
        ledger_sha256=NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256,
        root_table_sha256=root_table_sha256,
        fingerprint_inputs_sha256=fingerprint_inputs_sha256,
        source_manifest_sha256=source_manifest_sha256,
        provenance_manifest_sha256=provenance_manifest_sha256,
        diagnostics_manifest_sha256=_diagnostics_manifest_sha256(
            diagnostics, documents, import_manifest
        ),
        load_options_sha256=hashlib.sha256(_load_options_bytes_v1(load_options)).digest(),
        report_sha256=hashlib.sha256(_report_bytes_v1(report)).digest(),
        document_count=len(documents),
        import_edge_count=len(import_manifest.edges),
        diagnostic_count=diagnostic_count,
        ontology_annotation_count=annotation_count,
        stored_axiom_count=stored_axiom_count,
        effective_axiom_count=report.effective_axiom_count,
        extension_count=extension_count,
        total_source_bytes=report.total_source_bytes,
        source_map_entry_count=source_map_count,
        origin_entry_count=origin_count,
        rdf_mapping_report_count=rdf_report_count,
        capability_bits=capability_bits,
        api_version=report.api_version,
        model_schema=report.model_schema,
        backend=report.backend,
        root_document_key=root_document_key,
        owl2_dl_validated=report.owl2_dl_validated,
        owl2_dl_conforms=report.owl2_dl_conforms,
        owl2_dl_report_sha256=report.owl2_dl_report_sha256,
    )


def native_snapshot_attestation_bytes_v1(attestation: NativeSnapshotAttestationV1) -> bytes:
    """Encode an attestation record with an explicit domain separator."""

    if type(attestation) is not NativeSnapshotAttestationV1:
        raise TypeError("attestation must be NativeSnapshotAttestationV1")
    values = tuple(getattr(attestation, row[1]) for row in NATIVE_SNAPSHOT_ATTESTATION_FIELDS_V1)
    return NATIVE_SNAPSHOT_ATTESTATION_DOMAIN.encode("ascii") + b"\x00" + _sequence_bytes(values)


def _validate_publication_alignment(
    documents: tuple[NativeDocumentPublicationV1, ...],
    manifest: NativeImportManifestPublicationV1,
    root_document_key: str,
    options: LoadOptions,
    diagnostics: tuple[NativeDiagnosticPublicationV1, ...],
    report: NativeLoadReportPublicationV1,
    capability_bits: int,
    *,
    allow_auto_backend: bool = False,
) -> None:
    if options.backend is not BackendPreference.NATIVE and not (
        allow_auto_backend and options.backend is BackendPreference.AUTO
    ):
        _fail("retained publication requires forced native backend", "NATIVE_PUBLICATION_OPTIONS")
    if options.backend is BackendPreference.AUTO and any(
        document.provenance.backend != "native" for document in documents
    ):
        _fail(
            "AUTO retained publication requires native parser provenance",
            "NATIVE_PUBLICATION_OPTIONS",
        )
    if len(documents) > min(options.limits.max_documents, _bound("max_documents")):
        _fail("native publication exceeds document limit", "NATIVE_PUBLICATION_LIMIT")
    if len(manifest.edges) > min(options.limits.max_axioms, _bound("max_import_edges")):
        _fail("native publication exceeds import-edge limit", "NATIVE_PUBLICATION_LIMIT")
    total_diagnostics = (
        len(diagnostics)
        + sum(len(document.diagnostics) for document in documents)
        + sum(edge.diagnostic is not None for edge in manifest.edges)
    )
    if total_diagnostics > min(options.limits.max_diagnostics, _bound("max_total_diagnostics")):
        _fail("native publication exceeds diagnostic limit", "NATIVE_PUBLICATION_LIMIT")
    if len(manifest.documents) != len(documents):
        _fail("native documents diverge from import records", "NATIVE_PUBLICATION_ALIGNMENT")
    roots = 0
    edge_index = 0
    for record, document in zip(manifest.documents, documents, strict=True):
        if (
            record.document_key != document.document_key
            or record.ontology_id != document.ontology_id
            or record.document_iri != document.document_iri
            or record.source_sha256 != document.provenance.source_sha256
            or record.document_fingerprint != document.document_fingerprint
            or record.format != document.provenance.format
        ):
            _fail(
                "native document metadata diverges from import records",
                "NATIVE_PUBLICATION_ALIGNMENT",
            )
        if record.status == "root":
            roots += 1
            if record.document_key != root_document_key:
                _fail("native root diverges from import records", "NATIVE_PUBLICATION_ROOT")
        import_index = 0
        while (
            edge_index < len(manifest.edges)
            and manifest.edges[edge_index].importing_document_key == document.document_key
        ):
            edge = manifest.edges[edge_index]
            if import_index >= len(document.direct_imports) or (
                edge.import_iri != document.direct_imports[import_index]
            ):
                _fail(
                    "native direct imports diverge from import edges",
                    "NATIVE_PUBLICATION_ALIGNMENT",
                )
            import_index += 1
            edge_index += 1
        if import_index != len(document.direct_imports):
            _fail("native direct imports diverge from import edges", "NATIVE_PUBLICATION_ALIGNMENT")
    if roots != 1:
        _fail("native publication requires exactly one root", "NATIVE_PUBLICATION_ROOT")
    if edge_index != len(manifest.edges):
        _fail("native import edges are not aligned", "NATIVE_PUBLICATION_ALIGNMENT")
    if options.imports.value != manifest.policy or options.offline != manifest.offline:
        _fail("native load options diverge from import manifest", "NATIVE_PUBLICATION_OPTIONS")
    total_source_bytes = _checked_sum(
        "total source bytes", (document.provenance.byte_length for document in documents)
    )
    stored_axioms = _checked_sum("stored axioms", (document.axiom_count for document in documents))
    if (
        report.document_count != len(documents)
        or report.total_source_bytes != total_source_bytes
        or report.effective_axiom_count > stored_axioms
    ):
        _fail("native report counts diverge from publication", "NATIVE_PUBLICATION_REPORT")
    if report.total_source_bytes > options.limits.max_total_source_bytes:
        _fail("native report exceeds source-byte limit", "NATIVE_PUBLICATION_LIMIT")
    if report.effective_axiom_count > options.limits.max_axioms:
        _fail("native report exceeds axiom limit", "NATIVE_PUBLICATION_LIMIT")
    if (
        report.acquisition_cache_hits > report.resolution_attempts
        or report.document_cache_hits > report.resolution_attempts
        or report.resolution_attempts > options.limits.max_resolver_attempts
    ):
        _fail("native report cache/resolution claims are inconsistent", "NATIVE_PUBLICATION_REPORT")
    if report.owl2_dl_validated != options.validate_owl2_dl:
        _fail("native OWL validation claim diverges from options", "NATIVE_PUBLICATION_REPORT")
    _validate_capabilities(documents, options, capability_bits)


def _validate_capabilities(
    documents: tuple[NativeDocumentPublicationV1, ...],
    options: LoadOptions,
    capability_bits: int,
) -> None:
    _require_nonnegative_u64("capability_bits", capability_bits)
    if capability_bits & ~_KNOWN_CAPABILITY_BITS:
        _fail("native publication has unknown capability bits", "NATIVE_PUBLICATION_CAPABILITY")
    if capability_bits & _REQUIRED_CAPABILITY_BITS != _REQUIRED_CAPABILITY_BITS:
        _fail(
            "native publication lacks required storage capabilities",
            "NATIVE_PUBLICATION_CAPABILITY",
        )
    source_count = _checked_sum(
        "source-map count", (document.source_map_entry_count for document in documents)
    )
    origin_count = _checked_sum(
        "origin count", (document.origin_entry_count for document in documents)
    )
    rdf_count = sum(document.rdf_mapping_report_sha256 is not None for document in documents)
    has_source = bool(capability_bits & _CAPABILITY_SOURCE_MAP)
    has_origin = bool(capability_bits & _CAPABILITY_ORIGIN_INDEX)
    has_rdf = bool(capability_bits & _CAPABILITY_RDF_MAPPING_REPORT)
    if has_source != options.preserve_source_map or (source_count and not has_source):
        _fail("source-map capability and table claims diverge", "NATIVE_PUBLICATION_CAPABILITY")
    if has_origin != options.collect_provenance or (origin_count and not has_origin):
        _fail("origin capability and table claims diverge", "NATIVE_PUBLICATION_CAPABILITY")
    if has_rdf != bool(rdf_count):
        _fail("RDF mapping capability and report claims diverge", "NATIVE_PUBLICATION_CAPABILITY")
    if source_count > options.limits.max_source_map_entries:
        _fail("source-map count exceeds configured limit", "NATIVE_PUBLICATION_LIMIT")
    if origin_count > options.limits.max_origin_entries:
        _fail("origin count exceeds configured limit", "NATIVE_PUBLICATION_LIMIT")


def _validate_handle(handle: NativeSnapshotHandleV1, expected: NativeSnapshotAttestationV1) -> None:
    try:
        closed = handle.closed
        attestation = handle.attestation
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        raise BackendProtocolError(
            "native snapshot handle failed during publication",
            code="NATIVE_HANDLE_OWNER",
        ) from error
    if closed:
        _fail("native snapshot publication handle is closed", "NATIVE_HANDLE_LIFECYCLE")
    if attestation != expected:
        _fail("native snapshot handle attestation does not match", "NATIVE_ATTESTATION_MISMATCH")


def _diagnostics_manifest_sha256(
    diagnostics: tuple[NativeDiagnosticPublicationV1, ...],
    documents: tuple[NativeDocumentPublicationV1, ...],
    manifest: NativeImportManifestPublicationV1,
) -> bytes:
    body = bytearray(b"pyowl-core:native-diagnostics-manifest:v1\x00")
    body.extend(_sequence_bytes(diagnostics))
    for document in documents:
        body.extend(_scalar_bytes(document.document_key))
        body.extend(_sequence_bytes(document.diagnostics))
    for edge in manifest.edges:
        body.extend(_scalar_bytes(edge.importing_document_key))
        body.extend(_scalar_bytes(edge.import_iri.value))
        body.extend(_scalar_bytes(edge.diagnostic))
    return hashlib.sha256(bytes(body)).digest()


def _load_options_bytes_v1(options: LoadOptions) -> bytes:
    option_fields = tuple(item.name for item in fields(LoadOptions))
    v2_option_fields = (*NATIVE_LOAD_OPTION_FIELDS_V1, "allow_partial_rdf_mapping")
    if option_fields not in {NATIVE_LOAD_OPTION_FIELDS_V1, v2_option_fields}:
        _fail("LoadOptions field ledger changed", "NATIVE_ATTESTATION_OPTIONS")
    if option_fields == v2_option_fields:
        allow_partial = options.allow_partial_rdf_mapping
        if type(allow_partial) is not bool:
            _fail(
                "allow_partial_rdf_mapping must be an exact bool",
                "NATIVE_ATTESTATION_OPTIONS",
            )
        if allow_partial:
            _fail(
                "partial RDF mapping cannot be encoded by the frozen V1 option ledger",
                "NATIVE_ATTESTATION_OPTIONS",
            )
    if tuple(item.name for item in fields(ParseLimits)) != NATIVE_PARSE_LIMIT_FIELDS_V1:
        _fail("ParseLimits field ledger changed", "NATIVE_ATTESTATION_OPTIONS")
    option_values: list[object] = []
    for name in NATIVE_LOAD_OPTION_FIELDS_V1:
        value = getattr(options, name)
        if name == "limits":
            option_values.append(
                tuple(getattr(value, limit_name) for limit_name in NATIVE_PARSE_LIMIT_FIELDS_V1)
            )
        else:
            option_values.append(value)
    return b"pyowl-core:native-load-options:v1\x00" + _sequence_bytes(option_values)


def _report_bytes_v1(report: NativeLoadReportPublicationV1) -> bytes:
    values = tuple(getattr(report, row[1]) for row in NATIVE_LOAD_REPORT_PUBLICATION_FIELDS_V1)
    return b"pyowl-core:native-load-report:v1\x00" + _sequence_bytes(values)


def _diagnostic_bytes(diagnostic: NativeDiagnosticPublicationV1) -> bytes:
    values = tuple(getattr(diagnostic, row[1]) for row in NATIVE_DIAGNOSTIC_PUBLICATION_FIELDS_V1)
    return b"pyowl-core:native-diagnostic:v1\x00" + _sequence_bytes(values)


def _scalar_bytes(value: object) -> bytes:
    if value is None:
        return b"n"
    if isinstance(value, bool):
        return b"b1" if value else b"b0"
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii") + b";"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite values cannot be attested")
        return b"f" + value.hex().encode("ascii") + b";"
    if isinstance(value, str):
        return b"s" + _frame(value.encode("utf-8"))
    if isinstance(value, bytes):
        return b"y" + _frame(value)
    if isinstance(value, Enum):
        return _scalar_bytes(value.value)
    if isinstance(value, IRI):
        return b"r" + _frame(value.value.encode("utf-8"))
    if isinstance(value, OntologyID):
        return b"o" + _sequence_bytes((value.ontology_iri, value.version_iri))
    if isinstance(value, Fingerprint):
        return b"p" + _sequence_bytes((value.algorithm, value.schema, value.digest))
    if isinstance(value, NativeDiagnosticPublicationV1):
        return b"d" + _frame(_diagnostic_bytes(value))
    if isinstance(value, Sequence):
        return b"q" + _frame(b"".join(_frame(_scalar_bytes(item)) for item in value))
    raise TypeError(f"unsupported attestation scalar: {type(value).__qualname__}")


def _sequence_bytes(values: Sequence[object]) -> bytes:
    return b"q" + _frame(b"".join(_frame(_scalar_bytes(value)) for value in values))


def _checked_sum(name: str, values: Any) -> int:
    total = 0
    for value in values:
        _require_nonnegative_u64(name, value)
        total += value
        if total >= 2**64:
            raise ValueError(f"{name} exceeds u64")
    return total


def _validate_diagnostic_sequence(
    name: str, diagnostics: tuple[NativeDiagnosticPublicationV1, ...]
) -> None:
    if len(diagnostics) > _bound("max_diagnostics_per_sequence"):
        raise ValueError(f"{name} exceeds the publication bound")
    if not all(type(item) is NativeDiagnosticPublicationV1 for item in diagnostics):
        raise TypeError(f"{name} must contain frozen scalar-only diagnostics")


def _diagnostic_reference(value: object, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise TypeError("diagnostic import-chain references cannot be None")
        return None
    if isinstance(value, IRI):
        return _copy_string("diagnostic reference", value.value, iri=True)
    if isinstance(value, str):
        return _copy_string("diagnostic reference", value, iri=True)
    raise TypeError("diagnostic references must be strings or IRI values")


def _validate_ontology_metadata(ontology_id: OntologyID, document_iri: IRI | None) -> None:
    if type(ontology_id) is not OntologyID:
        raise TypeError("ontology_id must be exact OntologyID metadata")
    for name, value in (
        ("ontology IRI", ontology_id.ontology_iri),
        ("version IRI", ontology_id.version_iri),
        ("document IRI", document_iri),
    ):
        if value is not None:
            _require_iri(name, value)


def _require_iri(name: str, value: object) -> None:
    if type(value) is not IRI:
        raise TypeError(f"{name} must be exact IRI metadata")
    _copy_string(name, value.value, iri=True)


def _copy_iri(value: IRI | None) -> IRI | None:
    if value is None:
        return None
    _require_iri("IRI", value)
    return IRI(value.value.encode("utf-8").decode("utf-8"))


def _copy_ontology_id(value: OntologyID) -> OntologyID:
    _validate_ontology_metadata(value, None)
    return OntologyID(_copy_iri(value.ontology_iri), _copy_iri(value.version_iri))


def _copy_fingerprint(value: Fingerprint) -> Fingerprint:
    _require_fingerprint("fingerprint", value)
    return Fingerprint(value.algorithm, value.schema, bytes(value.digest))


def _require_fingerprint(name: str, value: object) -> None:
    if type(value) is not Fingerprint:
        raise TypeError(f"{name} must be exact Fingerprint metadata")


def _copy_document_key(value: object) -> str:
    return _copy_string(
        "document key",
        value,
        byte_limit=_bound("max_document_key_utf8_bytes"),
    )


def _copy_optional_document_key(value: object) -> str | None:
    if value is None:
        return None
    return _copy_document_key(value)


def _copy_optional_string(name: str, value: object, *, iri: bool = False) -> str | None:
    if value is None:
        return None
    return _copy_string(name, value, iri=iri)


def _copy_string(
    name: str,
    value: object,
    *,
    iri: bool = False,
    byte_limit: int | None = None,
) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{name} must be a nonempty string")
    encoded = value.encode("utf-8")
    maximum = _bound("max_iri_utf8_bytes") if iri else _bound("max_metadata_string_utf8_bytes")
    if byte_limit is not None:
        maximum = byte_limit
    if len(encoded) > maximum:
        raise ValueError(f"{name} exceeds the UTF-8 publication bound")
    copied = encoded.decode("utf-8")
    if iri:
        IRI(copied)
    return copied


def _require_api_version(name: str, value: object) -> None:
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or not all(
            isinstance(item, int)
            and not isinstance(item, bool)
            and 0 <= item <= _bound("max_api_component")
            for item in value
        )
    ):
        raise TypeError(f"{name} must be a pair of u32 integers")


def _require_optional_u64(name: str, value: object) -> None:
    if value is not None:
        _require_nonnegative_u64(name, value)


def _require_optional_positive_u64(name: str, value: object) -> None:
    if value is not None:
        _require_nonnegative_u64(name, value)
        if value == 0:
            raise ValueError(f"{name} must be positive or None")


def _require_nonnegative_u64(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**64:
        raise ValueError(f"{name} must be a nonnegative u64 integer")


def _require_nonnegative_u32(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**32:
        raise ValueError(f"{name} must be a nonnegative u32 integer")


def _require_digest(name: str, value: object) -> None:
    if not isinstance(value, bytes) or len(value) != 32:
        raise ValueError(f"{name} must be exactly 32 bytes")


def _bound(name: str) -> int:
    return NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1[name]


def _fail(message: str, code: str) -> None:
    raise BackendProtocolError(message, code=code)


__all__ = [
    "NATIVE_DIAGNOSTIC_PUBLICATION_FIELDS_V1",
    "NATIVE_DOCUMENT_PUBLICATION_FIELDS_V1",
    "NATIVE_IMPORT_DOCUMENT_FIELDS_V1",
    "NATIVE_IMPORT_EDGE_FIELDS_V1",
    "NATIVE_IMPORT_MANIFEST_FIELDS_V1",
    "NATIVE_LOAD_OPTION_FIELDS_V1",
    "NATIVE_LOAD_REPORT_PUBLICATION_FIELDS_V1",
    "NATIVE_PARSE_LIMIT_FIELDS_V1",
    "NATIVE_PROVENANCE_PUBLICATION_FIELDS_V1",
    "NATIVE_SNAPSHOT_ATTESTATION_DOMAIN",
    "NATIVE_SNAPSHOT_ATTESTATION_FIELDS_V1",
    "NATIVE_SNAPSHOT_CAPABILITY_BITS_V1",
    "NATIVE_SNAPSHOT_CAPABILITY_RULES_V1",
    "NATIVE_SNAPSHOT_HANDLE_MEMBERS_V1",
    "NATIVE_SNAPSHOT_LEDGER_CANONICALIZATION",
    "NATIVE_SNAPSHOT_LIFECYCLE_V1",
    "NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1",
    "NATIVE_SNAPSHOT_PUBLICATION_FIELDS_V1",
    "NATIVE_SNAPSHOT_PUBLICATION_LEDGER_DOMAIN",
    "NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256",
    "NATIVE_SNAPSHOT_PUBLICATION_VERSION",
    "NATIVE_SNAPSHOT_RUST_PARITY_REQUIRED",
    "NativeDiagnosticPublicationV1",
    "NativeDocumentProvenancePublicationV1",
    "NativeDocumentPublicationV1",
    "NativeImportDocumentPublicationV1",
    "NativeImportEdgePublicationV1",
    "NativeImportManifestPublicationV1",
    "NativeLoadReportPublicationV1",
    "NativeSnapshotAttestationV1",
    "NativeSnapshotHandleV1",
    "NativeSnapshotPublicationV1",
    "freeze_native_diagnostic_publication_v1",
    "freeze_native_import_manifest_publication_v1",
    "freeze_native_provenance_publication_v1",
    "freeze_native_snapshot_publication_v1",
    "native_snapshot_attestation_bytes_v1",
    "native_snapshot_publication_attestation_v1",
    "native_snapshot_publication_ledger_bytes_v1",
    "native_snapshot_publication_schema_semantics_v1",
]
