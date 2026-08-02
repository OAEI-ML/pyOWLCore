"""Version-2 retained native publication and paged facade contract.

Version 1 deliberately sealed the owning handle before it specified how a
public facade could request scalar values.  This sibling contract keeps every
version-1 metadata record, adds an exact bounded query surface, and binds the
complete metadata/access/auxiliary-codec semantics into the owner attestation.
"""

from __future__ import annotations

import hashlib
import math
import os
import sys
import threading
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import InitVar, dataclass, field, fields
from enum import Enum
from itertools import pairwise
from typing import Any, Final, NoReturn, cast

from pyowl_core._immutable import FrozenMap
from pyowl_core.backends.native_handoff import (
    NATIVE_DIAGNOSTIC_PUBLICATION_FIELDS_V1,
    NATIVE_DOCUMENT_PUBLICATION_FIELDS_V1,
    NATIVE_IMPORT_DOCUMENT_FIELDS_V1,
    NATIVE_IMPORT_EDGE_FIELDS_V1,
    NATIVE_IMPORT_MANIFEST_FIELDS_V1,
    NATIVE_LOAD_OPTION_FIELDS_V1,
    NATIVE_LOAD_REPORT_PUBLICATION_FIELDS_V1,
    NATIVE_PARSE_LIMIT_FIELDS_V1,
    NATIVE_PROVENANCE_PUBLICATION_FIELDS_V1,
    NATIVE_SNAPSHOT_CAPABILITY_BITS_V1,
    NATIVE_SNAPSHOT_CAPABILITY_RULES_V1,
    NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1,
    NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256,
    NativeDiagnosticPublicationV1,
    NativeDocumentProvenancePublicationV1,
    NativeDocumentPublicationV1,
    NativeImportDocumentPublicationV1,
    NativeImportEdgePublicationV1,
    NativeImportManifestPublicationV1,
    NativeLoadReportPublicationV1,
    _canonical_schema_value,
    _diagnostics_manifest_sha256,
    _fail,
    _report_bytes_v1,
    _sequence_bytes,
    _validate_publication_alignment,
)
from pyowl_core.config import (
    BackendPreference,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
)
from pyowl_core.diagnostics import SourceSpan, validate_diagnostic_code
from pyowl_core.document.document import Fingerprint, OntologyID
from pyowl_core.exceptions import BackendProtocolError, ClosedSnapshotError
from pyowl_core.limits import ParseLimits
from pyowl_core.model import (
    IRI,
    Annotation,
    Entity,
    ObjectInverseOf,
    ObjectProperty,
    StructuralNode,
    ValidationSeverity,
    canonical_bytes,
    decode_canonical,
    structural_digest,
)
from pyowl_core.model.axioms import AxiomNode
from pyowl_core.model.swrl import SWRLRule

NATIVE_LOAD_OPTION_FIELDS_V2: Final = (
    *NATIVE_LOAD_OPTION_FIELDS_V1,
    "allow_partial_rdf_mapping",
)
NATIVE_ACTIVE_API_VERSION_V2: Final = (0, 2)
NATIVE_ACTIVE_MODEL_SCHEMA_V2: Final = 2


def _load_options_bytes_v2(options: LoadOptions) -> bytes:
    if tuple(item.name for item in fields(LoadOptions)) != NATIVE_LOAD_OPTION_FIELDS_V2:
        _fail("LoadOptions V2 field ledger changed", "NATIVE_ATTESTATION_OPTIONS")
    if tuple(item.name for item in fields(ParseLimits)) != NATIVE_PARSE_LIMIT_FIELDS_V1:
        _fail("ParseLimits field ledger changed", "NATIVE_ATTESTATION_OPTIONS")
    if type(options.allow_partial_rdf_mapping) is not bool:
        _fail(
            "allow_partial_rdf_mapping must be an exact bool",
            "NATIVE_ATTESTATION_OPTIONS",
        )
    option_values: list[object] = []
    for name in NATIVE_LOAD_OPTION_FIELDS_V2:
        value = getattr(options, name)
        if name == "limits":
            option_values.append(
                tuple(getattr(value, limit_name) for limit_name in NATIVE_PARSE_LIMIT_FIELDS_V1)
            )
        else:
            option_values.append(value)
    return b"pyowl-core:native-load-options:v2\x00" + _sequence_bytes(option_values)


def _require_nonnegative_u64(name: str, value: object) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an exact int")
    if not 0 <= value < 2**64:
        raise ValueError(f"{name} must be a nonnegative u64 integer")


def _require_nonnegative_u32(name: str, value: object) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an exact int")
    if not 0 <= value < 2**32:
        raise ValueError(f"{name} must be a nonnegative u32 integer")


def _require_optional_u64_v2(name: str, value: object) -> None:
    if value is not None:
        _require_nonnegative_u64(name, value)


def _require_optional_positive_u64_v2(name: str, value: object) -> None:
    if value is not None:
        _require_nonnegative_u64(name, value)
        if value == 0:
            raise ValueError(f"{name} must be positive or None")


def _require_digest(name: str, value: object) -> None:
    if type(value) is not bytes:
        raise TypeError(f"{name} must be exact bytes")
    if len(value) != 32:
        raise ValueError(f"{name} must be exactly 32 bytes")


def _require_api_version(name: str, value: object) -> None:
    if type(value) is not tuple or len(value) != 2:
        raise TypeError(f"{name} must be an exact pair of u32 integers")
    for item in value:
        _require_nonnegative_u32(name, item)


def _copy_document_key(value: object) -> str:
    if type(value) is not str or not value:
        raise TypeError("document key must be a nonempty exact str")
    encoded = value.encode("utf-8")
    if len(encoded) > NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1["max_document_key_utf8_bytes"]:
        raise ValueError("document key exceeds the UTF-8 publication bound")
    return encoded.decode("utf-8")


def _checked_sum(name: str, values: Sequence[int] | Any) -> int:
    total = 0
    for value in values:
        _require_nonnegative_u64(name, value)
        total += value
        if total >= 2**64:
            raise ValueError(f"{name} exceeds u64")
    return total


def _require_exact_text_v2(
    name: str,
    value: object,
    *,
    optional: bool = False,
    iri: bool = False,
    byte_limit: int | None = None,
) -> None:
    if value is None and optional:
        return
    if type(value) is not str or not value:
        raise TypeError(f"{name} must be an exact str{' or None' if optional else ''}")
    encoded = value.encode("utf-8")
    maximum = (
        NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1["max_iri_utf8_bytes"]
        if iri
        else NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1["max_metadata_string_utf8_bytes"]
    )
    if byte_limit is not None:
        maximum = byte_limit
    if len(encoded) > maximum:
        raise ValueError(f"{name} exceeds the UTF-8 publication bound")
    if iri:
        IRI(value)


def _validate_exact_iri_v2(name: str, value: object, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if type(value) is not IRI:
        raise TypeError(f"{name} must be an exact IRI{' or None' if optional else ''}")
    _require_exact_text_v2(f"{name}.value", value.value, iri=True)


def _validate_exact_ontology_id_v2(value: object) -> None:
    if type(value) is not OntologyID:
        raise TypeError("ontology_id must be an exact OntologyID")
    selected = value
    _validate_exact_iri_v2("ontology_id.ontology_iri", selected.ontology_iri, optional=True)
    _validate_exact_iri_v2("ontology_id.version_iri", selected.version_iri, optional=True)
    if selected.version_iri is not None and selected.ontology_iri is None:
        raise ValueError("ontology_id.version_iri requires ontology_id.ontology_iri")


def _validate_exact_fingerprint_v2(name: str, value: object) -> None:
    if type(value) is not Fingerprint:
        raise TypeError(f"{name} must be an exact Fingerprint")
    selected = value
    _require_exact_text_v2(f"{name}.algorithm", selected.algorithm)
    if selected.algorithm != "sha256":
        raise ValueError(f"{name}.algorithm must be 'sha256'")
    _require_nonnegative_u32(f"{name}.schema", selected.schema)
    if selected.schema == 0:
        raise ValueError(f"{name}.schema must be positive")
    _require_digest(f"{name}.digest", selected.digest)


def _validate_exact_diagnostic_v2(value: object) -> None:
    if type(value) is not NativeDiagnosticPublicationV1:
        raise TypeError("diagnostic must be an exact V1 publication record")
    diagnostic = value
    for name in ("code", "severity", "message"):
        _require_exact_text_v2(f"diagnostic.{name}", getattr(diagnostic, name))
    validate_diagnostic_code(diagnostic.code)
    if diagnostic.severity not in {"info", "warning", "error"}:
        raise ValueError("diagnostic.severity is invalid")
    _require_exact_text_v2(
        "diagnostic.document_iri",
        diagnostic.document_iri,
        optional=True,
        iri=True,
    )
    for name in ("byte_start", "byte_end"):
        _require_optional_u64_v2(f"diagnostic.{name}", getattr(diagnostic, name))
    for name in ("line_start", "column_start", "line_end", "column_end"):
        _require_optional_positive_u64_v2(f"diagnostic.{name}", getattr(diagnostic, name))
    if (
        diagnostic.byte_start is not None
        and diagnostic.byte_end is not None
        and diagnostic.byte_end < diagnostic.byte_start
    ):
        raise ValueError("diagnostic byte span is reversed")
    if diagnostic.line_start is not None and diagnostic.line_end is not None:
        start_column = diagnostic.column_start or 1
        end_column = diagnostic.column_end or 1
        if (diagnostic.line_end, end_column) < (diagnostic.line_start, start_column):
            raise ValueError("diagnostic text span is reversed")
    if type(diagnostic.import_chain) is not tuple:
        raise TypeError("diagnostic.import_chain must be an exact tuple")
    if (
        len(diagnostic.import_chain)
        > NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1["max_diagnostic_import_chain"]
    ):
        raise ValueError("diagnostic import chain exceeds the publication bound")
    for item in diagnostic.import_chain:
        _require_exact_text_v2("diagnostic.import_chain item", item, iri=True)
    if type(diagnostic.details) is not tuple:
        raise TypeError("diagnostic.details must be an exact tuple")
    if len(diagnostic.details) > NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1["max_diagnostic_details"]:
        raise ValueError("diagnostic details exceed the publication bound")
    detail_keys: set[str] = set()
    for detail in diagnostic.details:
        if type(detail) is not tuple or len(detail) != 2:
            raise TypeError("diagnostic detail must be an exact pair")
        key, scalar = detail
        _require_exact_text_v2("diagnostic detail key", key)
        if key in detail_keys:
            raise ValueError("diagnostic detail keys must be unique")
        detail_keys.add(key)
        if type(scalar) not in {str, int, bool}:
            raise TypeError("diagnostic detail scalar must have an exact scalar type")
        if type(scalar) is int and not -(2**63) <= scalar < 2**63:
            raise ValueError("diagnostic detail integer must fit i64")
        if type(scalar) is str:
            _require_exact_text_v2("diagnostic detail value", scalar)


def _validate_diagnostic_sequence(
    name: str,
    diagnostics: object,
) -> None:
    if type(diagnostics) is not tuple:
        raise TypeError(f"{name} must be an exact tuple")
    if len(diagnostics) > NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1["max_diagnostics_per_sequence"]:
        raise ValueError(f"{name} exceeds the publication bound")
    for diagnostic in diagnostics:
        _validate_exact_diagnostic_v2(diagnostic)


def _validate_exact_provenance_v2(value: object) -> None:
    if type(value) is not NativeDocumentProvenancePublicationV1:
        raise TypeError("provenance must be an exact V1 publication record")
    provenance = value
    _require_digest("provenance.source_sha256", provenance.source_sha256)
    for name in (
        "digest_kind",
        "format",
        "detection_basis",
        "parser",
        "backend",
    ):
        _require_exact_text_v2(f"provenance.{name}", getattr(provenance, name))
    if provenance.digest_kind not in {"exact-bytes", "normalized-text"}:
        raise ValueError("provenance.digest_kind is invalid")
    if provenance.format not in {item.value for item in DocumentFormat}:
        raise ValueError("provenance.format is invalid")
    if provenance.detection_basis not in {"explicit", "media-type", "content", "extension"}:
        raise ValueError("provenance.detection_basis is invalid")
    _require_exact_text_v2(
        "provenance.document_iri",
        provenance.document_iri,
        optional=True,
        iri=True,
    )
    for name in ("acquisition_locator", "media_type"):
        _require_exact_text_v2(
            f"provenance.{name}",
            getattr(provenance, name),
            optional=True,
        )
    for name in ("byte_length", "decoded_codepoint_length"):
        _require_nonnegative_u64(f"provenance.{name}", getattr(provenance, name))
    if provenance.expected_sha256 is not None:
        _require_digest("provenance.expected_sha256", provenance.expected_sha256)
    _require_api_version("provenance.api_version", provenance.api_version)
    _require_nonnegative_u32("provenance.model_schema", provenance.model_schema)
    if provenance.api_version != NATIVE_ACTIVE_API_VERSION_V2:
        raise ValueError("provenance API version must be (0, 2)")
    if provenance.model_schema != NATIVE_ACTIVE_MODEL_SCHEMA_V2:
        raise ValueError("provenance model schema must be 2")


def _validate_exact_document_v2(value: object) -> None:
    if type(value) is not NativeDocumentPublicationV1:
        raise TypeError("document must be an exact V1 publication record")
    document = value
    _copy_document_key(document.document_key)
    _validate_exact_ontology_id_v2(document.ontology_id)
    _validate_exact_iri_v2("document.document_iri", document.document_iri, optional=True)
    if type(document.direct_imports) is not tuple:
        raise TypeError("document.direct_imports must be an exact tuple")
    if (
        len(document.direct_imports)
        > NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1["max_direct_imports_per_document"]
    ):
        raise ValueError("document.direct_imports exceeds the publication bound")
    previous_import: bytes | None = None
    for item in document.direct_imports:
        _validate_exact_iri_v2("document.direct_import", item)
        encoded_import = canonical_bytes(item)
        if previous_import is not None and encoded_import <= previous_import:
            raise ValueError("document.direct_imports must be canonical ascending unique")
        previous_import = encoded_import
    _validate_exact_provenance_v2(document.provenance)
    _validate_exact_fingerprint_v2("document.document_fingerprint", document.document_fingerprint)
    _validate_diagnostic_sequence("document.diagnostics", document.diagnostics)
    for name in (
        "ontology_annotation_count",
        "axiom_count",
        "extension_count",
        "source_map_entry_count",
        "origin_entry_count",
    ):
        _require_nonnegative_u64(f"document.{name}", getattr(document, name))
    if document.rdf_mapping_conformant is not None and (
        type(document.rdf_mapping_conformant) is not bool
    ):
        raise TypeError("document.rdf_mapping_conformant must be an exact bool or None")
    if (document.rdf_mapping_conformant is None) != (document.rdf_mapping_report_sha256 is None):
        raise ValueError("document RDF mapping result and digest must be present together")
    if document.rdf_mapping_report_sha256 is not None:
        _require_digest(
            "document.rdf_mapping_report_sha256",
            document.rdf_mapping_report_sha256,
        )


def _validate_exact_import_document_v2(value: object) -> None:
    if type(value) is not NativeImportDocumentPublicationV1:
        raise TypeError("import document must be an exact V1 publication record")
    document = value
    _copy_document_key(document.document_key)
    _validate_exact_ontology_id_v2(document.ontology_id)
    _validate_exact_iri_v2("import document.document_iri", document.document_iri, optional=True)
    _require_digest("import document.source_sha256", document.source_sha256)
    _validate_exact_fingerprint_v2(
        "import document.document_fingerprint", document.document_fingerprint
    )
    _require_exact_text_v2("import document.format", document.format)
    _require_exact_text_v2("import document.status", document.status)
    if document.format not in {item.value for item in DocumentFormat}:
        raise ValueError("import document format is invalid")
    if document.status not in {"root", "resolved"}:
        raise ValueError("import document status is invalid")


def _validate_exact_import_edge_v2(value: object) -> None:
    if type(value) is not NativeImportEdgePublicationV1:
        raise TypeError("import edge must be an exact V1 publication record")
    edge = value
    _copy_document_key(edge.importing_document_key)
    _validate_exact_iri_v2("import edge.import_iri", edge.import_iri)
    _require_exact_text_v2("import edge.status", edge.status)
    if edge.status not in {"resolved", "unresolved", "ignored", "denied", "failed"}:
        raise ValueError("import edge status is invalid")
    if edge.resolved_document_key is not None:
        _copy_document_key(edge.resolved_document_key)
    for name in ("resolver_name", "sanitized_locator"):
        _require_exact_text_v2(f"import edge.{name}", getattr(edge, name), optional=True)
    if edge.diagnostic is not None:
        _validate_exact_diagnostic_v2(edge.diagnostic)
    if edge.status == "resolved" and edge.resolved_document_key is None:
        raise ValueError("resolved import edge requires a target")
    if edge.status != "resolved" and edge.resolved_document_key is not None:
        raise ValueError("only resolved import edges may have a target")


def _validate_exact_manifest_v2(value: object) -> None:
    if type(value) is not NativeImportManifestPublicationV1:
        raise TypeError("import manifest must be an exact V1 publication record")
    manifest = value
    _require_exact_text_v2("import manifest.policy", manifest.policy)
    if manifest.policy not in {item.value for item in ImportPolicy}:
        raise ValueError("import manifest policy is invalid")
    if type(manifest.offline) is not bool:
        raise TypeError("import manifest.offline must be an exact bool")
    _require_digest(
        "import manifest.resolver_configuration_fingerprint",
        manifest.resolver_configuration_fingerprint,
    )
    if type(manifest.documents) is not tuple or type(manifest.edges) is not tuple:
        raise TypeError("import manifest tables must be exact tuples")
    if len(manifest.documents) > NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1["max_documents"]:
        raise ValueError("import manifest documents exceed the publication bound")
    if len(manifest.edges) > NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1["max_import_edges"]:
        raise ValueError("import manifest edges exceed the publication bound")
    previous_document_key: bytes | None = None
    document_keys: set[str] = set()
    for document in manifest.documents:
        _validate_exact_import_document_v2(document)
        encoded_key = document.document_key.encode("utf-8")
        if previous_document_key is not None and encoded_key <= previous_document_key:
            raise ValueError("import manifest documents must be key-ascending unique")
        previous_document_key = encoded_key
        document_keys.add(document.document_key)
    previous_edge_key: tuple[object, ...] | None = None
    for edge in manifest.edges:
        _validate_exact_import_edge_v2(edge)
        edge_key: tuple[object, ...] = (
            edge.importing_document_key.encode("utf-8"),
            canonical_bytes(edge.import_iri),
            edge.status,
            edge.resolved_document_key or "",
        )
        if previous_edge_key is not None and edge_key < previous_edge_key:
            raise ValueError("import manifest edges must be canonically ordered")
        previous_edge_key = edge_key
        if edge.importing_document_key not in document_keys:
            raise ValueError("import edge source is absent from document records")
        if (
            edge.resolved_document_key is not None
            and edge.resolved_document_key not in document_keys
        ):
            raise ValueError("import edge target is absent from document records")


def _validate_exact_parse_limits_v2(value: object) -> None:
    if type(value) is not ParseLimits:
        raise TypeError("load_options.limits must be an exact ParseLimits")
    limits = value
    for name in NATIVE_PARSE_LIMIT_FIELDS_V1:
        selected = getattr(limits, name)
        if name == "deadline_seconds":
            if selected is not None and type(selected) not in {int, float}:
                raise TypeError("deadline_seconds must have an exact numeric type or None")
            if selected is not None and (not math.isfinite(selected) or selected <= 0):
                raise ValueError("deadline_seconds must be positive and finite")
        elif name == "max_memory_bytes":
            if selected is not None:
                if type(selected) is not int:
                    raise TypeError("max_memory_bytes must be an exact positive int or None")
                if selected < 1:
                    raise ValueError("max_memory_bytes must be positive or None")
        else:
            if type(selected) is not int:
                raise TypeError(f"{name} must be an exact int")
            if selected < 1:
                raise ValueError(f"{name} must be positive")


def _validate_exact_load_options_v2(value: object) -> None:
    if type(value) is not LoadOptions:
        raise TypeError("load_options must be an exact LoadOptions")
    options = value
    if options.format is not None and type(options.format) is not DocumentFormat:
        raise TypeError("load_options.format must be an exact DocumentFormat or None")
    if type(options.imports) is not ImportPolicy:
        raise TypeError("load_options.imports must be an exact ImportPolicy")
    if type(options.backend) is not BackendPreference:
        raise TypeError("load_options.backend must be an exact BackendPreference")
    _validate_exact_parse_limits_v2(options.limits)
    for name in (
        "offline",
        "preserve_source_map",
        "collect_provenance",
        "validate_owl2_dl",
        "deterministic",
    ):
        if type(getattr(options, name)) is not bool:
            raise TypeError(f"load_options.{name} must be an exact bool")


def _validate_exact_report_v2(value: object) -> None:
    if type(value) is not NativeLoadReportPublicationV1:
        raise TypeError("report must be an exact V1 publication record")
    report = value
    _require_exact_text_v2("report.backend", report.backend)
    if report.backend != "native":
        raise ValueError("report.backend must be 'native'")
    _require_api_version("report.api_version", report.api_version)
    _require_nonnegative_u32("report.model_schema", report.model_schema)
    if report.api_version != NATIVE_ACTIVE_API_VERSION_V2:
        raise ValueError("report API version must be (0, 2)")
    if report.model_schema != NATIVE_ACTIVE_MODEL_SCHEMA_V2:
        raise ValueError("report model schema must be 2")
    for name in (
        "document_count",
        "total_source_bytes",
        "effective_axiom_count",
        "resolution_attempts",
        "acquisition_cache_hits",
        "document_cache_hits",
    ):
        _require_nonnegative_u64(f"report.{name}", getattr(report, name))
    if type(report.timings) is not tuple:
        raise TypeError("report.timings must be an exact tuple")
    if len(report.timings) > NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1["max_timing_rows"]:
        raise ValueError("report.timings exceeds the publication bound")
    previous_timing_name: bytes | None = None
    for item in report.timings:
        if type(item) is not tuple or len(item) != 2:
            raise TypeError("report timing must be an exact pair")
        name, value = item
        _require_exact_text_v2(
            "report timing name",
            name,
            byte_limit=NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1["max_timing_name_utf8_bytes"],
        )
        encoded_name = name.encode("utf-8")
        if previous_timing_name is not None and encoded_name <= previous_timing_name:
            raise ValueError("report timing names must be canonical ascending unique")
        previous_timing_name = encoded_name
        if type(value) is not float:
            raise TypeError("report timing value must be an exact float")
        if not math.isfinite(value) or value < 0:
            raise ValueError("report timing value must be finite and nonnegative")
    for name in (
        "structural_fingerprint",
        "logical_fingerprint",
        "signature_fingerprint",
    ):
        _validate_exact_fingerprint_v2(f"report.{name}", getattr(report, name))
    if type(report.owl2_dl_validated) is not bool:
        raise TypeError("report.owl2_dl_validated must be an exact bool")
    if report.owl2_dl_conforms is not None and type(report.owl2_dl_conforms) is not bool:
        raise TypeError("report.owl2_dl_conforms must be an exact bool or None")
    if report.owl2_dl_report_sha256 is not None:
        _require_digest("report.owl2_dl_report_sha256", report.owl2_dl_report_sha256)
    if report.owl2_dl_validated:
        if report.owl2_dl_conforms is None or report.owl2_dl_report_sha256 is None:
            raise ValueError("validated OWL2-DL report requires result metadata")
    elif report.owl2_dl_conforms is not None or report.owl2_dl_report_sha256 is not None:
        raise ValueError("unvalidated OWL2-DL report cannot publish result metadata")


def _validate_exact_publication_metadata_v2(
    documents: object,
    import_manifest: object,
    root_document_key: object,
    load_options: object,
    diagnostics: object,
    report: object,
    capability_bits: object,
) -> None:
    if type(documents) is not tuple or not documents:
        raise TypeError("documents must be a nonempty exact tuple")
    for document in documents:
        _validate_exact_document_v2(document)
    _validate_exact_manifest_v2(import_manifest)
    _copy_document_key(root_document_key)
    _validate_exact_load_options_v2(load_options)
    _validate_diagnostic_sequence("snapshot diagnostics", diagnostics)
    _validate_exact_report_v2(report)
    _require_nonnegative_u64("capability_bits", capability_bits)


NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2: Final = 2
NATIVE_SNAPSHOT_PUBLICATION_LEDGER_DOMAIN_V2: Final = (
    "pyowl-core:native-snapshot-publication-ledger:v2"
)
NATIVE_SNAPSHOT_ATTESTATION_DOMAIN_V2: Final = (
    "pyowl-core:native-snapshot-publication-attestation:v2"
)
NATIVE_SNAPSHOT_LEDGER_CANONICALIZATION_V2: Final = "typed-toml-tree-v1"
NATIVE_SNAPSHOT_RUST_PARITY_REQUIRED_V2: Final = True
NATIVE_FACADE_ACCESS_DOMAIN_V2: Final = "pyowl-core:native-facade-access-schema:v2"
NATIVE_AUXILIARY_CODEC_DOMAIN_V2: Final = "pyowl-core:native-auxiliary-row-codec:v2"
NATIVE_ROOT_TABLE_MANIFEST_DOMAIN_V2: Final = "pyowl-core:native-root-table-manifest:v2"
NATIVE_DOCUMENT_ROOT_TABLE_DOMAIN_V2: Final = "pyowl-core:native-document-root-table:v2"
NATIVE_EFFECTIVE_ROOT_TABLE_MANIFEST_DOMAIN_V2: Final = (
    "pyowl-core:native-effective-root-table-manifest:v2"
)
NATIVE_EFFECTIVE_DOCUMENT_ROOT_TABLE_DOMAIN_V2: Final = (
    "pyowl-core:native-effective-document-root-table:v2"
)
NATIVE_FINGERPRINT_INPUTS_MANIFEST_DOMAIN_V2: Final = (
    "pyowl-core:native-fingerprint-inputs-manifest:v2"
)
NATIVE_SOURCE_MANIFEST_DOMAIN_V2: Final = "pyowl-core:native-source-manifest:v2"
NATIVE_DOCUMENT_SOURCE_TABLE_DOMAIN_V2: Final = "pyowl-core:native-document-source-table:v2"
NATIVE_PROVENANCE_MANIFEST_DOMAIN_V2: Final = "pyowl-core:native-provenance-manifest:v2"
NATIVE_DOCUMENT_ORIGIN_TABLE_DOMAIN_V2: Final = "pyowl-core:native-document-origin-table:v2"
NATIVE_EFFECTIVE_ORIGIN_MANIFEST_DOMAIN_V2: Final = "pyowl-core:native-effective-origin-manifest:v2"
NATIVE_EFFECTIVE_DOCUMENT_ORIGIN_TABLE_DOMAIN_V2: Final = (
    "pyowl-core:native-effective-document-origin-table:v2"
)
NATIVE_EFFECTIVE_CLOSURE_ORIGIN_TABLE_DOMAIN_V2: Final = (
    "pyowl-core:native-effective-closure-origin-table:v2"
)
NATIVE_RDF_MAPPING_REPORT_DOMAIN_V2: Final = "pyowl-core:native-rdf-mapping-report:v2"
NATIVE_OWL2_DL_REPORT_DOMAIN_V2: Final = "pyowl-core:native-owl2-dl-report:v2"
NATIVE_DIAGNOSTIC_REFERENCE_KINDS_DOMAIN_V2: Final = (
    "pyowl-core:native-diagnostic-reference-kinds:v2"
)
NATIVE_FACADE_CARDINALITY_SUMMARY_DOMAIN_V2: Final = (
    "pyowl-core:native-facade-cardinality-summary:v2"
)


class NativeFacadeCollectionV2(str, Enum):
    """Exact retained collections addressable by the facade."""

    ONTOLOGY_ANNOTATIONS = "ontology-annotations"
    AXIOMS = "axioms"
    EXTENSIONS = "extensions"
    SIGNATURE = "signature"
    SOURCE_MAP_ENTRIES = "source-map-entries"
    SOURCE_MAP_PREFIXES = "source-map-prefixes"
    ORIGIN_ENTRIES = "origin-entries"
    RDF_REPORT_HEADER = "rdf-report-header"
    RDF_UNCONSUMED_TRIPLES = "rdf-unconsumed-triples"
    RDF_RULE_IDS = "rdf-rule-ids"
    RDF_DIAGNOSTICS = "rdf-diagnostics"
    OWL2_DL_STRUCTURAL_ISSUES = "owl2-dl-structural-issues"
    OWL2_DL_ISSUES = "owl2-dl-issues"
    OWL2_DL_ROLE_PROPERTIES = "owl2-dl-role-properties"
    OWL2_DL_ROLE_HIERARCHY = "owl2-dl-role-hierarchy"
    OWL2_DL_ROLE_COMPOSITE = "owl2-dl-role-composite"
    OWL2_DL_ROLE_NON_SIMPLE = "owl2-dl-role-non-simple"


class NativeFacadeScopeV2(str, Enum):
    DOCUMENT = "document"
    CLOSURE = "closure"


class NativeSignatureKindV2(str, Enum):
    ALL = "all"
    CLASS = "class"
    DATATYPE = "datatype"
    OBJECT_PROPERTY = "object_property"
    DATA_PROPERTY = "data_property"
    ANNOTATION_PROPERTY = "annotation_property"
    NAMED_INDIVIDUAL = "named_individual"


class NativeDiagnosticReferenceKindV2(str, Enum):
    IRI = "iri"
    TEXT = "text"


_STRUCTURAL_COLLECTIONS: Final = frozenset(
    {
        NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS,
        NativeFacadeCollectionV2.AXIOMS,
        NativeFacadeCollectionV2.EXTENSIONS,
        NativeFacadeCollectionV2.SIGNATURE,
        NativeFacadeCollectionV2.OWL2_DL_ROLE_PROPERTIES,
        NativeFacadeCollectionV2.OWL2_DL_ROLE_COMPOSITE,
        NativeFacadeCollectionV2.OWL2_DL_ROLE_NON_SIMPLE,
    }
)
_ROOT_STRUCTURAL_COLLECTIONS_V2: Final = frozenset(
    {
        NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS,
        NativeFacadeCollectionV2.AXIOMS,
        NativeFacadeCollectionV2.EXTENSIONS,
    }
)
_OWL2_DL_COLLECTIONS_V2: Final = frozenset(
    {
        NativeFacadeCollectionV2.OWL2_DL_STRUCTURAL_ISSUES,
        NativeFacadeCollectionV2.OWL2_DL_ISSUES,
        NativeFacadeCollectionV2.OWL2_DL_ROLE_PROPERTIES,
        NativeFacadeCollectionV2.OWL2_DL_ROLE_HIERARCHY,
        NativeFacadeCollectionV2.OWL2_DL_ROLE_COMPOSITE,
        NativeFacadeCollectionV2.OWL2_DL_ROLE_NON_SIMPLE,
    }
)
_CLOSURE_ONLY_COLLECTIONS_V2: Final = _OWL2_DL_COLLECTIONS_V2
_DOCUMENT_ONLY_COLLECTIONS: Final = frozenset(
    {
        NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES,
        NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES,
        NativeFacadeCollectionV2.RDF_REPORT_HEADER,
        NativeFacadeCollectionV2.RDF_UNCONSUMED_TRIPLES,
        NativeFacadeCollectionV2.RDF_RULE_IDS,
        NativeFacadeCollectionV2.RDF_DIAGNOSTICS,
    }
)
_OPTIONAL_COLLECTION_CAPABILITIES_V2: Final = FrozenMap(
    {
        NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES: 8,
        NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES: 8,
        NativeFacadeCollectionV2.ORIGIN_ENTRIES: 16,
        NativeFacadeCollectionV2.RDF_REPORT_HEADER: 32,
        NativeFacadeCollectionV2.RDF_UNCONSUMED_TRIPLES: 32,
        NativeFacadeCollectionV2.RDF_RULE_IDS: 32,
        NativeFacadeCollectionV2.RDF_DIAGNOSTICS: 32,
    }
)

NATIVE_SNAPSHOT_PUBLICATION_FIELDS_V2: Final = (
    (0, "version", "int", "one"),
    (1, "ledger_sha256", "bytes32", "one"),
    (2, "handle", "NativeSnapshotHandleV2", "one"),
    (3, "documents", "tuple[NativeDocumentPublicationV1]", "documents"),
    (4, "import_manifest", "NativeImportManifestPublicationV1", "one"),
    (5, "root_document_key", "str", "one"),
    (6, "load_options", "LoadOptions", "one"),
    (7, "diagnostics", "tuple[NativeDiagnosticPublicationV1]", "diagnostics"),
    (8, "diagnostic_reference_sidecars", "NativeDiagnosticReferenceSidecarsV2", "one"),
    (9, "facade_cardinality_summary", "NativeFacadeCardinalitySummaryV2", "one"),
    (10, "report", "NativeLoadReportPublicationV1", "one"),
    (11, "capability_bits", "u64", "one"),
    (12, "root_table_sha256", "bytes32", "one"),
    (13, "effective_root_table_sha256", "bytes32", "one"),
    (14, "fingerprint_inputs_sha256", "bytes32", "one"),
    (15, "source_manifest_sha256", "bytes32", "one"),
    (16, "provenance_manifest_sha256", "bytes32", "one"),
    (17, "effective_origin_manifest_sha256", "bytes32", "one"),
    (18, "max_facade_row_bytes", "u64", "one"),
    (19, "owl2_dl_report_summary", "NativeOWL2DLReportSummaryV2|None", "optional"),
)

NATIVE_SNAPSHOT_ATTESTATION_FIELDS_V2: Final = (
    (0, "version", "int", "one"),
    (1, "ledger_sha256", "bytes32", "one"),
    (2, "metadata_manifest_sha256", "bytes32", "one"),
    (3, "facade_access_schema_sha256", "bytes32", "one"),
    (4, "auxiliary_codec_schema_sha256", "bytes32", "one"),
    (5, "root_table_sha256", "bytes32", "one"),
    (6, "effective_root_table_sha256", "bytes32", "one"),
    (7, "fingerprint_inputs_sha256", "bytes32", "one"),
    (8, "source_manifest_sha256", "bytes32", "one"),
    (9, "provenance_manifest_sha256", "bytes32", "one"),
    (10, "effective_origin_manifest_sha256", "bytes32", "one"),
    (11, "diagnostics_manifest_sha256", "bytes32", "one"),
    (12, "diagnostic_reference_kinds_sha256", "bytes32", "one"),
    (13, "facade_cardinality_summary_sha256", "bytes32", "one"),
    (14, "load_options_sha256", "bytes32", "one"),
    (15, "report_sha256", "bytes32", "one"),
    (16, "max_facade_row_bytes", "u64", "one"),
    (17, "document_count", "u64", "one"),
    (18, "import_edge_count", "u64", "one"),
    (19, "diagnostic_count", "u64", "one"),
    (20, "ontology_annotation_count", "u64", "one"),
    (21, "stored_axiom_count", "u64", "one"),
    (22, "effective_axiom_count", "u64", "one"),
    (23, "extension_count", "u64", "one"),
    (24, "total_source_bytes", "u64", "one"),
    (25, "source_map_entry_count", "u64", "one"),
    (26, "origin_entry_count", "u64", "one"),
    (27, "rdf_mapping_report_count", "u64", "one"),
    (28, "capability_bits", "u64", "one"),
    (29, "api_version", "tuple[u32,u32]", "one"),
    (30, "model_schema", "u32", "one"),
    (31, "backend", "str", "one"),
    (32, "root_document_key", "str", "one"),
    (33, "owl2_dl_report_summary", "NativeOWL2DLReportSummaryV2|None", "optional"),
    (34, "owl2_dl_validated", "bool", "one"),
    (35, "owl2_dl_conforms", "bool|None", "optional"),
    (36, "owl2_dl_report_sha256", "bytes32|None", "optional"),
)

NATIVE_FACADE_PAGE_REQUEST_FIELDS_V2: Final = (
    (0, "collection", "NativeFacadeCollectionV2", "one"),
    (1, "scope", "NativeFacadeScopeV2", "one"),
    (2, "document_ordinal", "u64|None", "scope-dependent"),
    (3, "start", "u64", "one"),
    (4, "max_rows", "u32", "one"),
    (5, "max_bytes", "u64", "one"),
    (6, "max_row_bytes", "u64", "publication-bound"),
    (7, "signature_kind", "NativeSignatureKindV2", "one"),
    (8, "include_builtins", "bool", "one"),
    (9, "digest_filter", "bytes32|None", "optional"),
)

NATIVE_FACADE_PAGE_FIELDS_V2: Final = (
    *NATIVE_FACADE_PAGE_REQUEST_FIELDS_V2,
    (10, "total_count", "u64", "one"),
    (11, "next_cursor", "u64|None", "optional"),
    (12, "terminal", "bool", "one"),
    (13, "page_bytes", "u64", "one"),
    (14, "rows", "tuple[bytes]", "bounded-page"),
)

NATIVE_FACADE_CONTAINS_REQUEST_FIELDS_V2: Final = (
    (0, "collection", "NativeFacadeCollectionV2.AXIOMS", "one"),
    (1, "scope", "NativeFacadeScopeV2", "one"),
    (2, "document_ordinal", "u64|None", "scope-dependent"),
    (3, "canonical", "bytes", "one"),
    (4, "max_row_bytes", "u64", "publication-bound"),
)

_NATIVE_FACADE_COUNTER_DEFINITIONS_V2: Final = (
    ("component_node_requests", "monotonic-frozen-build"),
    ("component_node_hits", "monotonic-frozen-build"),
    ("string_requests", "monotonic-frozen-build"),
    ("string_hits", "monotonic-frozen-build"),
    ("byte_string_requests", "monotonic-frozen-build"),
    ("byte_string_hits", "monotonic-frozen-build"),
    ("integer_requests", "monotonic-frozen-build"),
    ("integer_hits", "monotonic-frozen-build"),
    ("component_sequence_requests", "monotonic-frozen-build"),
    ("component_sequence_hits", "monotonic-frozen-build"),
    ("canonical_input_rows", "monotonic-frozen-build"),
    ("canonical_input_bytes", "monotonic-frozen-build"),
    ("unique_component_nodes", "frozen-cardinality-gauge"),
    ("unique_strings", "frozen-cardinality-gauge"),
    ("unique_byte_strings", "frozen-cardinality-gauge"),
    ("unique_integers", "frozen-cardinality-gauge"),
    ("unique_component_sequences", "frozen-cardinality-gauge"),
    ("retained_document_tables", "frozen-cardinality-gauge"),
    ("retained_annotation_rows", "frozen-cardinality-gauge"),
    ("retained_axiom_rows", "frozen-cardinality-gauge"),
    ("retained_extension_rows", "frozen-cardinality-gauge"),
    ("retained_source_map_rows", "frozen-cardinality-gauge"),
    ("retained_source_prefix_rows", "frozen-cardinality-gauge"),
    ("retained_origin_rows", "frozen-cardinality-gauge"),
    ("retained_rdf_header_rows", "frozen-cardinality-gauge"),
    ("retained_rdf_triple_rows", "frozen-cardinality-gauge"),
    ("retained_rdf_rule_rows", "frozen-cardinality-gauge"),
    ("retained_rdf_diagnostic_rows", "frozen-cardinality-gauge"),
    ("retained_owl2_dl_structural_issue_rows", "frozen-cardinality-gauge"),
    ("retained_owl2_dl_issue_rows", "frozen-cardinality-gauge"),
    ("retained_owl2_dl_role_property_rows", "frozen-cardinality-gauge"),
    ("retained_owl2_dl_role_hierarchy_rows", "frozen-cardinality-gauge"),
    ("retained_owl2_dl_role_composite_rows", "frozen-cardinality-gauge"),
    ("retained_owl2_dl_role_non_simple_rows", "frozen-cardinality-gauge"),
    ("retained_component_bytes", "frozen-disjoint-memory-gauge"),
    ("retained_root_bytes", "frozen-disjoint-memory-gauge"),
    ("retained_source_bytes", "frozen-disjoint-memory-gauge"),
    ("retained_origin_bytes", "frozen-disjoint-memory-gauge"),
    ("retained_rdf_bytes", "frozen-disjoint-memory-gauge"),
    ("retained_owl2_dl_bytes", "frozen-disjoint-memory-gauge"),
    ("retained_index_bytes", "frozen-disjoint-memory-gauge"),
    ("retained_metadata_bytes", "frozen-disjoint-memory-gauge"),
    ("retained_owner_bytes", "frozen-total-memory-gauge"),
    ("peak_builder_live_bytes", "maximum-high-water-gauge"),
    ("peak_freeze_live_bytes", "maximum-high-water-gauge"),
    ("peak_facade_cache_bytes", "maximum-high-water-gauge"),
    ("publication_metadata_records_emitted", "monotonic-publication"),
    ("publication_structural_rows_copied", "monotonic-publication"),
    ("publication_structural_bytes_copied", "monotonic-publication"),
    ("page_requests", "monotonic-process-epoch-facade"),
    ("pages_returned", "monotonic-process-epoch-facade"),
    ("rows_emitted", "monotonic-process-epoch-facade"),
    ("payload_bytes_copied", "monotonic-process-epoch-facade"),
    ("canonical_payload_bytes_copied", "monotonic-process-epoch-facade"),
    ("auxiliary_payload_bytes_copied", "monotonic-process-epoch-facade"),
    ("contains_requests", "monotonic-process-epoch-facade"),
    ("contains_hits", "monotonic-process-epoch-facade"),
    ("ontology_annotation_rows_emitted", "monotonic-process-epoch-facade"),
    ("axiom_rows_emitted", "monotonic-process-epoch-facade"),
    ("extension_rows_emitted", "monotonic-process-epoch-facade"),
    ("signature_rows_emitted", "monotonic-process-epoch-facade"),
    ("source_map_rows_emitted", "monotonic-process-epoch-facade"),
    ("source_prefix_rows_emitted", "monotonic-process-epoch-facade"),
    ("origin_rows_emitted", "monotonic-process-epoch-facade"),
    ("rdf_header_rows_emitted", "monotonic-process-epoch-facade"),
    ("rdf_triple_rows_emitted", "monotonic-process-epoch-facade"),
    ("rdf_rule_rows_emitted", "monotonic-process-epoch-facade"),
    ("rdf_diagnostic_rows_emitted", "monotonic-process-epoch-facade"),
    ("owl2_dl_structural_issue_rows_emitted", "monotonic-process-epoch-facade"),
    ("owl2_dl_issue_rows_emitted", "monotonic-process-epoch-facade"),
    ("owl2_dl_role_property_rows_emitted", "monotonic-process-epoch-facade"),
    ("owl2_dl_role_hierarchy_rows_emitted", "monotonic-process-epoch-facade"),
    ("owl2_dl_role_composite_rows_emitted", "monotonic-process-epoch-facade"),
    ("owl2_dl_role_non_simple_rows_emitted", "monotonic-process-epoch-facade"),
    ("canonical_encode_requests", "monotonic-process-epoch-facade"),
    ("canonical_encode_cache_hits", "monotonic-process-epoch-facade"),
    ("facade_cache_hits", "monotonic-process-epoch-facade"),
    ("facade_cache_misses", "monotonic-process-epoch-facade"),
    ("facade_cache_evictions", "monotonic-process-epoch-facade"),
    ("close_requests", "monotonic-process-epoch-facade"),
    ("close_transitions", "monotonic-process-epoch-facade"),
    ("fork_reinitializations", "monotonic-process-epoch-facade"),
    ("facade_cache_current_entries", "current-process-epoch-gauge"),
    ("facade_cache_current_bytes", "current-process-epoch-gauge"),
    ("parser_bytes", "monotonic-native-runtime"),
    ("encoded_view_requests", "monotonic-native-runtime"),
    ("wire_encode_requests", "monotonic-native-runtime"),
    ("wire_decode_requests", "monotonic-native-runtime"),
    ("base_flatten_requests", "monotonic-native-runtime"),
)
NATIVE_FACADE_COUNTER_FIELDS_V2: Final = tuple(
    (ordinal, name, "u64", counter_class)
    for ordinal, (name, counter_class) in enumerate(_NATIVE_FACADE_COUNTER_DEFINITIONS_V2)
)

_NATIVE_PYTHON_COUNTER_DEFINITIONS_V2: Final = (
    ("publication_objects", "monotonic-python"),
    ("model_rows_materialized", "monotonic-python"),
    ("auxiliary_rows_decoded", "monotonic-python"),
    ("cache_hits", "monotonic-python"),
    ("cache_misses", "monotonic-python"),
    ("cache_evictions", "monotonic-python"),
    ("cache_current_entries", "current-python-gauge"),
    ("cache_current_bytes", "current-python-gauge"),
    ("cache_peak_bytes", "maximum-python-gauge"),
)
NATIVE_PYTHON_FACADE_COUNTER_FIELDS_V2: Final = tuple(
    (ordinal, name, "u64", counter_class)
    for ordinal, (name, counter_class) in enumerate(_NATIVE_PYTHON_COUNTER_DEFINITIONS_V2)
)

NATIVE_SOURCE_MAP_ROW_FIELDS_V2: Final = (
    (0, "digest", "bytes32", "one"),
    (1, "occurrence", "u64", "one"),
    (2, "span", "SourceSpan|None", "optional"),
    (3, "lexical", "tuple[tuple[str,str]]", "bounded"),
)
NATIVE_SOURCE_PREFIX_ROW_FIELDS_V2: Final = (
    (0, "prefix", "str", "one"),
    (1, "iri", "str", "one"),
)
NATIVE_ORIGIN_ROW_FIELDS_V2: Final = (
    (0, "digest", "bytes32", "one"),
    (1, "document_key", "str", "one"),
    (2, "occurrence", "u64", "one"),
    (3, "span", "SourceSpan|None", "optional"),
)
NATIVE_RDF_REPORT_HEADER_ROW_FIELDS_V2: Final = (
    (0, "conformant", "bool", "one"),
    (1, "consumed_triples", "u64", "one"),
    (2, "total_triples", "u64", "one"),
)
NATIVE_RDF_TRIPLE_ROW_FIELDS_V2: Final = (
    (0, "subject", "str", "one"),
    (1, "predicate", "str", "one"),
    (2, "object", "str", "one"),
)
NATIVE_RDF_RULE_ROW_FIELDS_V2: Final = ((0, "rule_id", "str", "one"),)
NATIVE_RDF_DIAGNOSTIC_ROW_FIELDS_V2: Final = (
    (0, "diagnostic", "NativeDiagnosticPublicationV1", "one"),
    (1, "reference_kinds", "NativeDiagnosticReferenceKindsV2", "one"),
)
NATIVE_OWL2_DL_REPORT_SUMMARY_FIELDS_V2: Final = (
    (0, "structural_values_checked", "u64", "one"),
    (1, "structural_complete", "bool", "one"),
    (2, "report_complete", "bool", "one"),
    (3, "structural_issue_count", "u64", "one"),
    (4, "issue_count", "u64", "one"),
    (5, "role_property_count", "u64", "one"),
    (6, "role_hierarchy_count", "u64", "one"),
    (7, "role_composite_count", "u64", "one"),
    (8, "role_non_simple_count", "u64", "one"),
)
NATIVE_OWL2_DL_ISSUE_ROW_FIELDS_V2: Final = (
    (0, "code", "str", "one"),
    (1, "severity", "ValidationSeverity", "one"),
    (2, "message", "str", "one"),
    (3, "constructor", "str|None", "optional"),
)
NATIVE_OWL2_DL_ROLE_EDGE_ROW_FIELDS_V2: Final = (
    (0, "sub_property", "model-canonical-v2-bytes", "one"),
    (1, "super_property", "model-canonical-v2-bytes", "one"),
)
NATIVE_DIAGNOSTIC_REFERENCE_KINDS_FIELDS_V2: Final = (
    (0, "document_reference_kind", "NativeDiagnosticReferenceKindV2|None", "optional"),
    (1, "import_chain_kinds", "tuple[NativeDiagnosticReferenceKindV2]", "ordered"),
)
NATIVE_DIAGNOSTIC_REFERENCE_SIDECARS_FIELDS_V2: Final = (
    (0, "snapshot", "tuple[NativeDiagnosticReferenceKindsV2]", "diagnostics"),
    (1, "documents", "tuple[tuple[NativeDiagnosticReferenceKindsV2]]", "documents"),
    (
        2,
        "import_edges",
        "tuple[NativeDiagnosticReferenceKindsV2|None]",
        "import-edges",
    ),
)
NATIVE_DOCUMENT_FACADE_CARDINALITIES_FIELDS_V2: Final = (
    (0, "document_key", "str", "one"),
    (1, "effective_annotation_count", "u64", "one"),
    (2, "effective_axiom_count", "u64", "one"),
    (3, "effective_extension_count", "u64", "one"),
    (4, "effective_origin_count", "u64", "one"),
    (5, "raw_source_prefix_count", "u64", "one"),
    (6, "rdf_unconsumed_triple_count", "u64", "one"),
    (7, "rdf_rule_count", "u64", "one"),
    (8, "rdf_diagnostic_count", "u64", "one"),
)
NATIVE_CLOSURE_FACADE_CARDINALITIES_FIELDS_V2: Final = (
    (0, "effective_annotation_count", "u64", "one"),
    (1, "effective_axiom_count", "u64", "one"),
    (2, "effective_extension_count", "u64", "one"),
    (3, "effective_origin_count", "u64", "one"),
)
NATIVE_FACADE_CARDINALITY_SUMMARY_FIELDS_V2: Final = (
    (0, "documents", "tuple[NativeDocumentFacadeCardinalitiesV2]", "documents"),
    (1, "closure", "NativeClosureFacadeCardinalitiesV2", "one"),
)
NATIVE_FINGERPRINT_EVIDENCE_FIELDS_V2: Final = (
    (0, "tag", "u8", "one"),
    (1, "document_key", "str|None", "tag-dependent"),
    (2, "preimage_byte_length", "u64", "one"),
    (3, "fingerprint_schema", "u32", "one"),
    (4, "digest", "bytes32", "one"),
)

NATIVE_SNAPSHOT_HANDLE_MEMBERS_V2: Final = (
    (0, "publication_version", "int", "property"),
    (1, "publication_ledger_sha256", "bytes32", "property"),
    (2, "attestation", "NativeSnapshotAttestationV2", "property"),
    (3, "closed", "bool", "property"),
    (4, "close", "() -> None", "method"),
    (5, "_facade_page_v2", "(NativeFacadePageRequestV2) -> NativeFacadePageV2", "method"),
    (6, "_facade_contains_v2", "(NativeFacadeContainsRequestV2) -> bool", "method"),
    (7, "_facade_counters_v2", "() -> NativeFacadeCountersV2", "method"),
    (8, "_facade_document_v2", "(u64) -> NativeDocumentHandleV2", "method"),
)

NATIVE_DOCUMENT_HANDLE_MEMBERS_V2: Final = (
    (0, "publication_version", "int", "property"),
    (1, "publication_ledger_sha256", "bytes32", "property"),
    (2, "attestation", "NativeSnapshotAttestationV2", "property"),
    (3, "document_ordinal", "u64", "property"),
    (4, "closed", "bool", "property"),
    (5, "close", "() -> None", "method"),
    (6, "_facade_page_v2", "(NativeFacadePageRequestV2) -> NativeFacadePageV2", "method"),
    (7, "_facade_contains_v2", "(NativeFacadeContainsRequestV2) -> bool", "method"),
    (8, "_facade_counters_v2", "() -> NativeFacadeCountersV2", "method"),
)

NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2: Final[FrozenMap[str, int]] = FrozenMap(
    {
        **dict(NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1),
        "max_facade_page_rows": 64,
        "max_facade_page_bytes": 8 * 1024 * 1024,
        "max_facade_cache_entries": 256,
        "max_facade_cache_bytes": 8 * 1024 * 1024,
    }
)
_FACADE_ROW_BUDGET_FIELDS_V2: Final = (
    "max_canonical_work",
    "max_index_bytes",
    "max_wire_bytes",
    "max_temporary_bytes",
)

NATIVE_SNAPSHOT_LIFECYCLE_V2: Final[FrozenMap[str, str]] = FrozenMap(
    {
        "initial_state": "open",
        "publication_state": "open-only",
        "close": "idempotent-thread-safe",
        "metadata_after_close": "readable",
        "scalar_after_close": "raises-ClosedSnapshotError",
        "document_fork": "snapshot-open-linearized+independent-logical-owner+shared-storage",
        "document_scope": "fixed-attested-ordinal-only",
        "process_fork": "immutable-owner+pid-detect+process-local-lock-and-cache-reset",
        "concurrent_reads": "safe",
        "copy": "identity-preserving",
        "pickle": "forbidden",
        "finalization": "owner-release",
    }
)

_HANDLE_OWNER_ATTESTATION_MEMBER_V2 = "_publication_attestation_v2"
_HANDLE_OWNER_CLOSED_MEMBER_V2 = "_publication_closed_v2"
_HANDLE_OWNER_CLOSE_MEMBER_V2 = "_publication_close_v2"
_HANDLE_OWNER_PAGE_MEMBER_V2 = "_publication_page_v2"
_HANDLE_OWNER_CONTAINS_MEMBER_V2 = "_publication_contains_v2"
_HANDLE_OWNER_COUNTERS_MEMBER_V2 = "_publication_counters_v2"
_HANDLE_OWNER_DOCUMENT_MEMBER_V2 = "_publication_document_v2"
_RUST_OWNER_MODULE_V2 = "pyowl_core._native"
_RUST_OWNER_NAME_V2 = "_NativeSnapshotHandle"
_RUST_DOCUMENT_OWNER_NAME_V2 = "_NativeDocumentHandle"


def _field_schema(rows: Sequence[tuple[int, str, str, str]], tail: str) -> list[dict[str, object]]:
    return [
        {"ordinal": ordinal, "name": name, "type": type_name, tail: tail_value}
        for ordinal, name, type_name, tail_value in rows
    ]


def native_facade_access_schema_semantics_v2() -> dict[str, object]:
    return {
        "domain": NATIVE_FACADE_ACCESS_DOMAIN_V2,
        "collections": [
            {
                "name": item.value,
                "row_codec": (
                    "model-canonical-v2" if item in _STRUCTURAL_COLLECTIONS else item.value + "-v2"
                ),
                "scopes": (
                    ["document"]
                    if item in _DOCUMENT_ONLY_COLLECTIONS
                    else (
                        ["closure"]
                        if item in _CLOSURE_ONLY_COLLECTIONS_V2
                        else ["document", "closure"]
                    )
                ),
            }
            for item in NativeFacadeCollectionV2
        ],
        "scopes": [item.value for item in NativeFacadeScopeV2],
        "signature_kinds": [item.value for item in NativeSignatureKindV2],
        "page_request_fields": _field_schema(NATIVE_FACADE_PAGE_REQUEST_FIELDS_V2, "cardinality"),
        "page_fields": _field_schema(NATIVE_FACADE_PAGE_FIELDS_V2, "cardinality"),
        "contains_request_fields": _field_schema(
            NATIVE_FACADE_CONTAINS_REQUEST_FIELDS_V2, "cardinality"
        ),
        "cursor": "zero-based-u64-row-offset",
        "digest_filter": (
            "bytes32-or-none+source-map-entries-or-origin-entries-only+"
            "group-relative-total-and-cursor+two-binary-search-prefix-bounds+"
            "O(log-N)-lookup+page-sized-slice-only"
        ),
        "echo": "all-request-coordinates-exact",
        "request_boundary": (
            "exact-record-reconstruction+recursive-field-validation-before-owner-call+"
            "contains-authoritative-decode-once-under-bound-publication-limits"
        ),
        "row_bound": "request.max_row_bytes-equals-attestation.max_facade_row_bytes",
        "row_budget_fields": [*_FACADE_ROW_BUDGET_FIELDS_V2, "max_memory_bytes-if-set"],
        "row_bound_meaning": "positive-actual-maximum-retained-encoded-row",
        "terminal": "next-cursor-none-iff-start+rows-equals-total-count",
        "normal_page": "nonempty-unless-terminal+rows<=max_rows+bytes<=max_bytes",
        "oversized_first_row": (
            "one-sole-row-may-exceed-max_bytes-up-to-attestation.max_facade_row_bytes"
        ),
        "contains": "axioms-only+exact-canonical-model-v2-bytes+bound-ParseLimits",
        "ordering": {
            "structural": "canonical-ascending-unique-within-and-across-pages",
            "source-map-entries": (
                "digest-groups-ascending+same-digest-producer-order-and-multiplicity-preserved"
            ),
            "raw-document-origin-entries": (
                "digest-groups-ascending+same-digest-producer-order-and-multiplicity-preserved"
            ),
            "effective-origin-entries": (
                "digest+document-key-utf8+occurrence+encoded-row-ascending-unique"
            ),
            "producer-sequence-collections": "exact-order-and-multiplicity-preserved",
        },
        "traversal_validation": (
            "role-specific-attested-total+coordinate-total-pin+owner-role-specific-"
            "contiguous-page-boundary-order"
        ),
        "cardinality_summary": {
            "domain": NATIVE_FACADE_CARDINALITY_SUMMARY_DOMAIN_V2,
            "document_fields": _field_schema(
                NATIVE_DOCUMENT_FACADE_CARDINALITIES_FIELDS_V2,
                "cardinality",
            ),
            "closure_fields": _field_schema(
                NATIVE_CLOSURE_FACADE_CARDINALITIES_FIELDS_V2,
                "cardinality",
            ),
            "source": "owner-computed-during-freeze+content-manifest-cross-checked",
            "structural_count_invariant": (
                "effective-document-annotation-axiom-extension-counts-equal-v1-raw-counts"
            ),
            "unattested_totals": "signature-projections+digest-filter-groups-only",
        },
        "signature_validation": "exact-kind+canonical-builtins-policy",
        "optional_collections": "capability-bit-and-configured-limit-gated-before-owner-call",
        "owner_roles": {
            "document.structural+origin": "raw-document-rows",
            "snapshot.document.structural+origin": "snapshot-effective-document-rows",
            "snapshot.closure.structural+origin": "effective-merged-deduplicated-rows",
            "source-map+prefix": "raw-document-metadata-through-either-owner",
            "rdf+report": "collection-defined-scope-unchanged-by-owner-role",
        },
        "validation_decodes": {
            "page": (
                "derived-private-page-local-tuple+structural-and-auxiliary+"
                "exact-attested-ParseLimits+excluded-from-owner-fields+consumed-once-by-facade"
            ),
            "contains": (
                "derived-private-validation-axiom+boundary-decoded-once-under-exact-attested-"
                "ParseLimits+consumed-once-by-owner"
            ),
            "owl2-dl-role-edge": (
                "both-canonical-endpoints-decoded-once+retained-private+accessor-reuses-values"
            ),
            "python_counters": "model_rows_materialized-or-auxiliary_rows_decoded-on-consume",
            "lifetime": "bounded-by-page-or-request-never-arena-sized",
        },
    }


def native_auxiliary_codec_schema_semantics_v2() -> dict[str, object]:
    return {
        "domain": NATIVE_AUXILIARY_CODEC_DOMAIN_V2,
        "byte_order": "little-endian",
        "primitives": {
            "u16": "fixed-2-bytes",
            "u32": "fixed-4-bytes",
            "u64": "fixed-8-bytes",
            "bool": "one-byte-0-or-1",
            "bytes32": "exact-32-bytes",
            "text": "u32-byte-length+strict-utf8",
            "optional_text": "bool-present+text",
            "span": (
                "u8-bit7-record-present+six-low-coordinate-presence-bits+present-u64-coordinates"
            ),
            "diagnostic_scalar": "tag-u8(str=0,i64=1,bool=2)+typed-value",
            "diagnostic_reference_kind": "u8(absent=0,iri=1,text=2)",
        },
        "source-map-entries-v2": (
            "bytes32-digest+u64-occurrence+span+u16-lexical-count+sorted-unique-text-pairs"
        ),
        "source-map-prefixes-v2": "text-prefix+text-iri",
        "origin-entries-v2": "bytes32-digest+text-document-key+u64-occurrence+span",
        "rdf-report-header-v2": "bool-conformant+u64-consumed+u64-total",
        "rdf-unconsumed-triples-v2": "text-subject+text-predicate+text-object",
        "rdf-rule-ids-v2": "text-rule-id",
        "rdf-diagnostics-v2": (
            "text-code+u8-severity+text-message+tagged-optional-document-reference+span+"
            "u16-import-chain+each(tagged-reference)+u16-ordered-unique-details"
        ),
        "owl2-dl-structural-issues-v2": (
            "text-code+u8-severity+text-message+optional-text-constructor"
        ),
        "owl2-dl-issues-v2": ("text-code+u8-severity+text-message+optional-text-constructor"),
        "owl2-dl-role-hierarchy-v2": (
            "u32-sub-canonical-length+sub-canonical+u32-super-canonical-length+super-canonical"
        ),
        "severity_tags": {"info": 0, "warning": 1, "error": 2},
        "count_bounds": {
            "source_lexical_pairs": "u16<=65535",
            "diagnostic_import_chain": "u16<=min(65535,bounds.max_diagnostic_import_chain)",
            "diagnostic_details": "u16<=min(65535,bounds.max_diagnostic_details)",
        },
        "decoded_python_records": {
            "source_map_entry": _field_schema(NATIVE_SOURCE_MAP_ROW_FIELDS_V2, "cardinality"),
            "source_prefix": _field_schema(NATIVE_SOURCE_PREFIX_ROW_FIELDS_V2, "cardinality"),
            "origin": _field_schema(NATIVE_ORIGIN_ROW_FIELDS_V2, "cardinality"),
            "rdf_report_header": _field_schema(
                NATIVE_RDF_REPORT_HEADER_ROW_FIELDS_V2, "cardinality"
            ),
            "rdf_triple": _field_schema(NATIVE_RDF_TRIPLE_ROW_FIELDS_V2, "cardinality"),
            "rdf_rule": _field_schema(NATIVE_RDF_RULE_ROW_FIELDS_V2, "cardinality"),
            "rdf_diagnostic": _field_schema(NATIVE_RDF_DIAGNOSTIC_ROW_FIELDS_V2, "cardinality"),
            "diagnostic_reference_kinds": _field_schema(
                NATIVE_DIAGNOSTIC_REFERENCE_KINDS_FIELDS_V2,
                "cardinality",
            ),
            "owl2_dl_report_summary": _field_schema(
                NATIVE_OWL2_DL_REPORT_SUMMARY_FIELDS_V2, "cardinality"
            ),
            "owl2_dl_issue": _field_schema(NATIVE_OWL2_DL_ISSUE_ROW_FIELDS_V2, "cardinality"),
            "owl2_dl_role_edge": _field_schema(
                NATIVE_OWL2_DL_ROLE_EDGE_ROW_FIELDS_V2, "cardinality"
            ),
        },
        "row_granularity": "one-occurrence-prefix-triple-rule-or-diagnostic-per-row",
        "structural_digest": (
            "source-digest=raw-document-structural-row+origin-digest=owner-role-"
            "raw-or-effective-structural-row"
        ),
        "duplicate_policy": {
            "canonical-unique": (
                "structural+source-prefix-key+rdf-rule-id+owl2-dl-role-hierarchy+"
                "effective-document-origin+effective-closure-origin"
            ),
            "raw-producer-multiplicity": "source-map-entries+raw-document-origin-entries",
            "producer-sequence-multiplicity": (
                "rdf-unconsumed-triples+rdf-diagnostics+owl2-dl-structural-issues+owl2-dl-issues"
            ),
        },
        "ordering": {
            "source-map-entries": (
                "digest-groups-ascending+same-digest-exact-producer-order-preserved"
            ),
            "source-map-prefixes": "prefix-key-utf8-ascending-unique-irrespective-of-iri",
            "raw-document-origin-entries": (
                "digest-groups-ascending+same-digest-exact-producer-order-preserved"
            ),
            "effective-origin-entries": (
                "digest+document-key-utf8+occurrence+encoded-row-ascending-unique"
            ),
            "rdf-unconsumed-triples": "producer-order-and-multiplicity-preserved",
            "rdf-rule-ids": "utf8-ascending-unique",
            "rdf-diagnostics": "producer-order-and-multiplicity-preserved",
            "owl2-dl-structural-issues": "producer-order-and-multiplicity-preserved",
            "owl2-dl-issues": "producer-order-and-multiplicity-preserved",
            "owl2-dl-role-properties": "model-canonical-ascending-unique",
            "owl2-dl-role-hierarchy": "sub-canonical+super-canonical-ascending-unique",
            "owl2-dl-role-composite": "model-canonical-ascending-unique",
            "owl2-dl-role-non-simple": "model-canonical-ascending-unique",
        },
    }


def native_content_manifest_schema_semantics_v2() -> dict[str, object]:
    return {
        "hash": "sha256(ascii-domain+0x00+body)",
        "framing": {
            "u32": "fixed-little-endian-4",
            "u64": "fixed-little-endian-8",
            "bytes": "u64-length+bytes",
            "text": "u64-byte-length+strict-utf8",
            "optional": "0x00-absent|0x01+payload-present",
        },
        "document_order": "document-key-strict-utf8-ascending-unique",
        "structural_rows": "model-canonical-v2-bytes-ascending-unique",
        "auxiliary_rows": "exact-v2-codec-bytes-in-collection-order",
        "root_table": {
            "manifest_domain": NATIVE_ROOT_TABLE_MANIFEST_DOMAIN_V2,
            "document_domain": NATIVE_DOCUMENT_ROOT_TABLE_DOMAIN_V2,
            "manifest_body": (
                "u32(model_schema)+u64(document_count)+each(text(key)+u64(annotation_count)+"
                "u64(axiom_count)+u64(extension_count)+document_root_digest)"
            ),
            "document_body": (
                "text(key)+section(0x01,annotations)+section(0x02,axioms)+section(0x03,extensions)"
            ),
            "section": "u8(tag)+u64(count)+each(bytes(row))",
            "owner_role": "raw-document-roots+document-fingerprint-inputs",
        },
        "effective_root_table": {
            "manifest_domain": NATIVE_EFFECTIVE_ROOT_TABLE_MANIFEST_DOMAIN_V2,
            "document_domain": NATIVE_EFFECTIVE_DOCUMENT_ROOT_TABLE_DOMAIN_V2,
            "manifest_body": (
                "u32(model_schema)+u64(document_count)+each(text(key)+"
                "u64(annotation_count)+u64(axiom_count)+u64(extension_count)+"
                "effective_document_root_digest)"
            ),
            "document_body": (
                "text(key)+section(0x01,annotations)+section(0x02,axioms)+section(0x03,extensions)"
            ),
            "closure": "derived-sorted-unique-union+not-retained-as-a-copied-table",
            "fingerprint_inputs": "snapshot-structural+logical+signature",
        },
        "fingerprint_inputs": {
            "domain": NATIVE_FINGERPRINT_INPUTS_MANIFEST_DOMAIN_V2,
            "body": (
                "u32(model_schema)+text(root_document_key)+u64(document_count)+"
                "document-evidence-then-structural-logical-signature-evidence"
            ),
            "evidence_fields": _field_schema(NATIVE_FINGERPRINT_EVIDENCE_FIELDS_V2, "cardinality"),
            "tags": {"document": 1, "structural": 2, "logical": 3, "signature": 4},
            "evidence": (
                "u8(tag)+text(document_key-if-tag-1)+u64(preimage_byte_length)+"
                "u32(fingerprint_schema)+sha256(authoritative-preimage)"
            ),
            "authoritative_domains": {
                "document": "pyowl-core:document-fingerprint:v2",
                "structural": "pyowl-core:snapshot-structural:v2",
                "logical": "pyowl-core:snapshot-logical:v2",
                "signature": "pyowl-core:snapshot-signature:v2",
            },
            "digest_rule": (
                "evidence-digest-equals-authoritative-preimage-and-published-fingerprint"
            ),
            "preimage_lifetime": (
                "validation-input-only+discarded-before-owner-retention+excluded-from-counters"
            ),
        },
        "source": {
            "manifest_domain": NATIVE_SOURCE_MANIFEST_DOMAIN_V2,
            "document_domain": NATIVE_DOCUMENT_SOURCE_TABLE_DOMAIN_V2,
            "manifest_body": (
                "auxiliary_codec_schema_sha256+u64(document_count)+each(text(key)+"
                "optional(u64(entry_count)+u64(prefix_count)+document_source_digest))"
            ),
            "document_body": (
                "text(key)+u64(entry_count)+each(bytes(source-row))+u64(prefix_count)+"
                "each(bytes(prefix-row))"
            ),
            "presence": "capability-retained-absent-distinct-from-present-empty",
            "row_order": "raw-source-producer-order-and-multiplicity-within-digest-groups",
        },
        "provenance": {
            "manifest_domain": NATIVE_PROVENANCE_MANIFEST_DOMAIN_V2,
            "origin_domain": NATIVE_DOCUMENT_ORIGIN_TABLE_DOMAIN_V2,
            "rdf_domain": NATIVE_RDF_MAPPING_REPORT_DOMAIN_V2,
            "manifest_body": (
                "auxiliary_codec_schema_sha256+u64(document_count)+each(text(key)+"
                "optional(u64(origin_count)+origin_digest)+optional(u64(unconsumed_count)+"
                "u64(rule_count)+u64(diagnostic_count)+rdf_digest))"
            ),
            "origin_body": "text(key)+u64(row_count)+each(bytes(origin-row))",
            "rdf_body": (
                "text(key)+bytes(one-header)+u64(unconsumed_count)+producer-order-triples+"
                "u64(rule_count)+utf8-sorted-unique-rules+u64(diagnostic_count)+"
                "producer-order-diagnostics"
            ),
            "presence": "origin-capability-and-rdf-report-absent-distinct-from-present-empty",
            "owner_role": "raw-document-origins+owner-role-invariant-RDF-report",
            "raw_origin_order": ("producer-order-and-multiplicity-within-ascending-digest-groups"),
            "rdf_producer_sequences": "triple-and-diagnostic-order-and-multiplicity-preserved",
        },
        "effective_origin": {
            "manifest_domain": NATIVE_EFFECTIVE_ORIGIN_MANIFEST_DOMAIN_V2,
            "document_domain": NATIVE_EFFECTIVE_DOCUMENT_ORIGIN_TABLE_DOMAIN_V2,
            "closure_domain": NATIVE_EFFECTIVE_CLOSURE_ORIGIN_TABLE_DOMAIN_V2,
            "manifest_body": (
                "auxiliary_codec_schema_sha256+u64(document_count)+each(text(key)+"
                "u64(row_count)+document_digest)+u64(closure_count)+closure_digest"
            ),
            "document_body": "text(key)+u64(row_count)+each(bytes(effective-origin-row))",
            "closure_body": "u64(row_count)+each(bytes(merged-deduplicated-row))",
            "closure": "derived-k-way-merge-with-no-copied-closure-table",
            "ordering": ("digest+document-key-utf8+occurrence+encoded-row-ascending-unique"),
        },
        "owl2_dl_report": {
            "domain": NATIVE_OWL2_DL_REPORT_DOMAIN_V2,
            "summary_fields": _field_schema(
                NATIVE_OWL2_DL_REPORT_SUMMARY_FIELDS_V2,
                "cardinality",
            ),
            "body": (
                "summary-scalars+section(0x01,structural-issues)+section(0x02,issues)+"
                "section(0x03,role-properties)+section(0x04,role-hierarchy)+"
                "section(0x05,role-composite)+section(0x06,role-non-simple)"
            ),
            "summary_scalars": (
                "u64(structural_values_checked)+bool(structural_complete)+"
                "bool(report_complete)+six-u64-counts"
            ),
            "section": "u8(tag)+u64(count)+each(bytes(exact-row))",
            "presence": "present-iff-owl2_dl_validated",
            "digest": "equals-NativeLoadReportPublicationV1.owl2_dl_report_sha256",
            "conforms": ("successful-envelope-requires-true+complete-flags+no-error-severity"),
        },
        "facade_cardinalities": (
            "all-owner-role-whole-collection-counts-cross-checked-against-exact-rows"
        ),
        "validation": {
            "routing": ("document-key-scoped-source-and-origin+closure-embedded-document-key"),
            "structural_digest_indexes": "precomputed-exactly-once-per-document",
            "limits": "exact-publication-ParseLimits-for-structural-and-auxiliary-content",
            "capabilities": "presence-and-count-matrix-cross-checked-before-publication",
        },
        "ownership": "computed-by-native-owner-during-freeze-never-caller-echoed",
    }


def native_counter_schema_semantics_v2() -> dict[str, object]:
    return {
        "snapshot": "coherent-atomic-checked-u64",
        "native_fields": _field_schema(
            NATIVE_FACADE_COUNTER_FIELDS_V2,
            "counter_class",
        ),
        "python_fields": _field_schema(
            NATIVE_PYTHON_FACADE_COUNTER_FIELDS_V2,
            "counter_class",
        ),
        "namespaces": {
            "native": "construction+frozen-arena+publication+process-epoch-facade",
            "python": "publication-wrapper+materialization+python-cache-only",
        },
        "python_cache": {
            "ownership": "strong-lru-because-model-nodes-are-not-weak-referenceable",
            "bounds": "entry-count-and-recursive-python-byte-size",
            "measurement": "recursive-owned-python-bytes-with-shared-identity-deduplication",
            "oversized_row": "decode-and-return-without-cache-insertion",
        },
        "component_mapping": {
            "node_requests": "component_node_requests",
            "node_hits": "component_node_hits",
            "unique_nodes": "unique_component_nodes",
            "scalar+sequence": "corresponding-typed-request-hit-unique-fields",
            "peak_builder_bytes": "peak_builder_live_bytes",
            "retained_bytes": "retained_component_bytes-only",
        },
        "invariants": [
            "each-interner-requests=hits+unique",
            "pages_returned<=page_requests",
            "contains_hits<=contains_requests",
            "rows_emitted=sum(per-collection-rows-emitted)",
            "payload_bytes_copied=canonical+auxiliary-payload-bytes-copied",
            "retained_owner_bytes=sum(disjoint-retained-byte-fields)",
            "close_transitions<=close_requests",
            "canonical_encode_cache_hits<=canonical_encode_requests",
            "python.cache_current_bytes<=python.cache_peak_bytes",
        ],
        "merge": {
            "monotonic": "sum-disjoint-owner-or-process-epochs-only",
            "maximum": "max",
            "current": "latest-same-process-epoch",
            "retained_arc": "deduplicate-shared-arena-identity",
            "native_python": "never-sum-native-emission-with-python-decoding",
        },
        "fork": "new-pid-resets-process-epoch-events+current-cache+cache-peak",
        "publication_zero": [
            "page_requests",
            "pages_returned",
            "rows_emitted",
            "payload_bytes_copied",
            "canonical_payload_bytes_copied",
            "auxiliary_payload_bytes_copied",
            "publication_structural_rows_copied",
            "publication_structural_bytes_copied",
            "model_rows_materialized",
            "auxiliary_rows_decoded",
            "encoded_view_requests",
            "wire_encode_requests",
            "wire_decode_requests",
            "base_flatten_requests",
        ],
        "publication_access": "metadata+attestation+summaries+counters-only-no-pages",
        "retained_metadata": (
            "content-digests+bound-total-scalars-only+fingerprint-preimages-never-retained"
        ),
    }


def native_snapshot_publication_schema_semantics_v2() -> dict[str, object]:
    """Return every V2 digest-bearing TOML semantic except its digest."""

    return {
        "schema": NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2,
        "name": "NativeSnapshotPublicationV2",
        "domain": NATIVE_SNAPSHOT_PUBLICATION_LEDGER_DOMAIN_V2,
        "ledger_canonicalization": NATIVE_SNAPSHOT_LEDGER_CANONICALIZATION_V2,
        "amends": "NativeSnapshotPublicationV1",
        "amendment_reason": (
            "version 1 omitted the ledgered bounded scalar query surface required by its lazy "
            "facade capability"
        ),
        "extension_policy": "any semantic change requires publication version 3",
        "shared_metadata_ledger_sha256": NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256.hex(),
        "rust_parity_required": NATIVE_SNAPSHOT_RUST_PARITY_REQUIRED_V2,
        "envelope": {
            "python_type": "NativeSnapshotPublicationV2",
            "construction": "named-only",
            "ownership": "one-opaque-handle",
            "complexity": (
                "O(publication-metadata)+O(documents)-owner-binding+no-facade-pages-"
                "no-row-decodes-no-ontology-traversal"
            ),
            "validation": "recursive-exact-types-and-semantic-invariants-before-owner-call",
            "fields": _field_schema(NATIVE_SNAPSHOT_PUBLICATION_FIELDS_V2, "cardinality"),
        },
        "shared_records": {
            "document": _field_schema(NATIVE_DOCUMENT_PUBLICATION_FIELDS_V1, "cardinality"),
            "diagnostic": _field_schema(NATIVE_DIAGNOSTIC_PUBLICATION_FIELDS_V1, "cardinality"),
            "provenance": _field_schema(NATIVE_PROVENANCE_PUBLICATION_FIELDS_V1, "cardinality"),
            "import_document": _field_schema(NATIVE_IMPORT_DOCUMENT_FIELDS_V1, "cardinality"),
            "import_edge": _field_schema(NATIVE_IMPORT_EDGE_FIELDS_V1, "cardinality"),
            "import_manifest": _field_schema(NATIVE_IMPORT_MANIFEST_FIELDS_V1, "cardinality"),
            "report": _field_schema(NATIVE_LOAD_REPORT_PUBLICATION_FIELDS_V1, "cardinality"),
        },
        "attestation": {
            "python_type": "NativeSnapshotAttestationV2",
            "construction": "named-only",
            "domain": NATIVE_SNAPSHOT_ATTESTATION_DOMAIN_V2,
            "fields": _field_schema(NATIVE_SNAPSHOT_ATTESTATION_FIELDS_V2, "cardinality"),
        },
        "owl2_dl_report_summary": {
            "python_type": "NativeOWL2DLReportSummaryV2",
            "construction": "named-only",
            "fields": _field_schema(
                NATIVE_OWL2_DL_REPORT_SUMMARY_FIELDS_V2,
                "cardinality",
            ),
        },
        "diagnostic_reference_sidecars": {
            "python_type": "NativeDiagnosticReferenceSidecarsV2",
            "construction": "named-only",
            "domain": NATIVE_DIAGNOSTIC_REFERENCE_KINDS_DOMAIN_V2,
            "fields": _field_schema(
                NATIVE_DIAGNOSTIC_REFERENCE_SIDECARS_FIELDS_V2,
                "cardinality",
            ),
            "row_fields": _field_schema(
                NATIVE_DIAGNOSTIC_REFERENCE_KINDS_FIELDS_V2,
                "cardinality",
            ),
            "tags": {"absent": 0, "iri": 1, "text": 2},
            "alignment": "snapshot+document+import-edge-diagnostic-order-exact",
            "kind_row": ("u8(optional-document-kind)+u64(import-chain-count)+each(u8(kind))"),
            "digest_body": (
                "u64(snapshot-count)+each(bytes(kind-row))+u64(document-count)+"
                "each(text(document-key)+u64(row-count)+each(bytes(kind-row)))+"
                "u64(import-edge-count)+each(0x00|0x01+bytes(kind-row))"
            ),
        },
        "facade_cardinality_summary": {
            "python_type": "NativeFacadeCardinalitySummaryV2",
            "construction": "named-only",
            "domain": NATIVE_FACADE_CARDINALITY_SUMMARY_DOMAIN_V2,
            "fields": _field_schema(
                NATIVE_FACADE_CARDINALITY_SUMMARY_FIELDS_V2,
                "cardinality",
            ),
            "document_fields": _field_schema(
                NATIVE_DOCUMENT_FACADE_CARDINALITIES_FIELDS_V2,
                "cardinality",
            ),
            "closure_fields": _field_schema(
                NATIVE_CLOSURE_FACADE_CARDINALITIES_FIELDS_V2,
                "cardinality",
            ),
            "digest_body": (
                "u64(document-count)+each(text(document-key)+eight-u64-document-counts)+"
                "four-u64-closure-counts"
            ),
            "document_order": "publication-document-order",
        },
        "page_request": {
            "python_type": "NativeFacadePageRequestV2",
            "construction": "named-only",
            "fields": _field_schema(NATIVE_FACADE_PAGE_REQUEST_FIELDS_V2, "cardinality"),
        },
        "page": {
            "python_type": "NativeFacadePageV2",
            "construction": "named-only",
            "fields": _field_schema(NATIVE_FACADE_PAGE_FIELDS_V2, "cardinality"),
        },
        "contains_request": {
            "python_type": "NativeFacadeContainsRequestV2",
            "construction": "named-only",
            "fields": _field_schema(NATIVE_FACADE_CONTAINS_REQUEST_FIELDS_V2, "cardinality"),
        },
        "counters": {
            "python_type": "NativeFacadeCountersV2",
            "construction": "named-only",
            "fields": _field_schema(NATIVE_FACADE_COUNTER_FIELDS_V2, "counter_class"),
        },
        "python_counters": {
            "python_type": "NativePythonFacadeCountersV2",
            "construction": "named-only",
            "fields": _field_schema(
                NATIVE_PYTHON_FACADE_COUNTER_FIELDS_V2,
                "counter_class",
            ),
        },
        "handle": {
            "python_type": "NativeSnapshotHandleV2",
            "opaque": True,
            "owning": True,
            "sealed": True,
            "registration": "exact-owner-type",
            "members": _field_schema(NATIVE_SNAPSHOT_HANDLE_MEMBERS_V2, "kind"),
        },
        "document_handle": {
            "python_type": "NativeDocumentHandleV2",
            "opaque": True,
            "owning": True,
            "sealed": True,
            "registration": "exact-owner-type",
            "members": _field_schema(NATIVE_DOCUMENT_HANDLE_MEMBERS_V2, "kind"),
        },
        "handle_registration": {
            "rust_owner_module": _RUST_OWNER_MODULE_V2,
            "rust_owner_name": _RUST_OWNER_NAME_V2,
            "rust_document_owner_name": _RUST_DOCUMENT_OWNER_NAME_V2,
            "owner_attestation_member": _HANDLE_OWNER_ATTESTATION_MEMBER_V2,
            "owner_closed_member": _HANDLE_OWNER_CLOSED_MEMBER_V2,
            "owner_close_member": _HANDLE_OWNER_CLOSE_MEMBER_V2,
            "owner_page_member": _HANDLE_OWNER_PAGE_MEMBER_V2,
            "owner_contains_member": _HANDLE_OWNER_CONTAINS_MEMBER_V2,
            "owner_counters_member": _HANDLE_OWNER_COUNTERS_MEMBER_V2,
            "owner_document_member": _HANDLE_OWNER_DOCUMENT_MEMBER_V2,
            "exact_type_only": True,
            "duplicate_policy": "idempotent-same-type-only",
            "fixture_owner": "exact-generated-page-fixture",
        },
        "bounds": dict(NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2),
        "capability_bits": [
            {"value": value, "name": name} for value, name in NATIVE_SNAPSHOT_CAPABILITY_BITS_V1
        ],
        "capability_rules": dict(NATIVE_SNAPSHOT_CAPABILITY_RULES_V1),
        "lifecycle": dict(NATIVE_SNAPSHOT_LIFECYCLE_V2),
        "dispatch": {
            "facade_required_publication": "NativeSnapshotPublicationV2",
            "required_surface": "V2-paged+contains+document-owner",
            "v1": "legacy-metadata-only+never-facade-dispatchable",
        },
        "access_protocol": native_facade_access_schema_semantics_v2(),
        "auxiliary_codecs": native_auxiliary_codec_schema_semantics_v2(),
        "content_manifests": native_content_manifest_schema_semantics_v2(),
        "counter_semantics": native_counter_schema_semantics_v2(),
        "attestation_bindings": {
            "metadata_manifest": (
                "all-envelope-metadata+load-options+capabilities+raw-effective-table-digests"
            ),
            "access_schema": "canonical-access-protocol-subtree",
            "auxiliary_codec_schema": "canonical-auxiliary-codecs-subtree",
            "root_tables": "owner-computed-raw+effective-root-table-digests",
            "fingerprint_inputs": "owner-computed-fingerprint_inputs_sha256",
            "source_tables": "owner-computed-source_manifest_sha256",
            "provenance_tables": "owner-computed-raw-provenance+effective-origin-digests",
            "diagnostic_reference_kinds": "owner-computed-aligned-sidecar-digest",
            "facade_cardinalities": "owner-computed-O(documents)-summary-digest",
            "load_option_fields": list(NATIVE_LOAD_OPTION_FIELDS_V1),
            "parse_limit_fields": list(NATIVE_PARSE_LIMIT_FIELDS_V1),
        },
        "rust_parity": {
            "required": True,
            "record": "NativeSnapshotPublicationV2",
            "attestation": "NativeSnapshotAttestationV2",
            "status_claim": "none-until-runtime-registration-and-page-parity",
        },
    }


def native_snapshot_publication_ledger_bytes_v2() -> bytes:
    prefix = b"pyowl-core:typed-toml-tree:v1\x00"
    return prefix + _canonical_schema_value(native_snapshot_publication_schema_semantics_v2())


NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2: Final = hashlib.sha256(
    native_snapshot_publication_ledger_bytes_v2()
).digest()
NATIVE_FACADE_ACCESS_SCHEMA_SHA256_V2: Final = hashlib.sha256(
    NATIVE_FACADE_ACCESS_DOMAIN_V2.encode("ascii")
    + b"\x00"
    + _canonical_schema_value(native_facade_access_schema_semantics_v2())
).digest()
NATIVE_AUXILIARY_CODEC_SCHEMA_SHA256_V2: Final = hashlib.sha256(
    NATIVE_AUXILIARY_CODEC_DOMAIN_V2.encode("ascii")
    + b"\x00"
    + _canonical_schema_value(native_auxiliary_codec_schema_semantics_v2())
).digest()


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeOWL2DLReportSummaryV2:
    structural_values_checked: int
    structural_complete: bool
    report_complete: bool
    structural_issue_count: int
    issue_count: int
    role_property_count: int
    role_hierarchy_count: int
    role_composite_count: int
    role_non_simple_count: int

    def __post_init__(self) -> None:
        _require_nonnegative_u64(
            "OWL2-DL structural values checked", self.structural_values_checked
        )
        if type(self.structural_complete) is not bool or type(self.report_complete) is not bool:
            raise TypeError("OWL2-DL summary complete flags must be bool")
        for name in (
            "structural_issue_count",
            "issue_count",
            "role_property_count",
            "role_hierarchy_count",
            "role_composite_count",
            "role_non_simple_count",
        ):
            _require_nonnegative_u64(f"OWL2-DL summary {name}", getattr(self, name))

    @property
    def row_count(self) -> int:
        return (
            self.structural_issue_count
            + self.issue_count
            + self.role_property_count
            + self.role_hierarchy_count
            + self.role_composite_count
            + self.role_non_simple_count
        )


def _owl2_dl_summary_values_v2(
    summary: NativeOWL2DLReportSummaryV2 | None,
) -> tuple[object, ...] | None:
    if summary is None:
        return None
    _validate_exact_owl2_summary_v2(summary)
    return tuple(getattr(summary, row[1]) for row in NATIVE_OWL2_DL_REPORT_SUMMARY_FIELDS_V2)


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeDiagnosticReferenceKindsV2:
    document_reference_kind: NativeDiagnosticReferenceKindV2 | None
    import_chain_kinds: tuple[NativeDiagnosticReferenceKindV2, ...]

    def __post_init__(self) -> None:
        if self.document_reference_kind is not None and (
            type(self.document_reference_kind) is not NativeDiagnosticReferenceKindV2
        ):
            raise TypeError("diagnostic document reference kind must be an exact V2 enum")
        if type(self.import_chain_kinds) is not tuple or not all(
            type(item) is NativeDiagnosticReferenceKindV2 for item in self.import_chain_kinds
        ):
            raise TypeError("diagnostic import-chain kinds must be an exact enum tuple")


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeDiagnosticReferenceSidecarsV2:
    snapshot: tuple[NativeDiagnosticReferenceKindsV2, ...]
    documents: tuple[tuple[NativeDiagnosticReferenceKindsV2, ...], ...]
    import_edges: tuple[NativeDiagnosticReferenceKindsV2 | None, ...]

    def __post_init__(self) -> None:
        if type(self.snapshot) is not tuple or not all(
            type(item) is NativeDiagnosticReferenceKindsV2 for item in self.snapshot
        ):
            raise TypeError("snapshot diagnostic reference sidecars must be an exact tuple")
        if type(self.documents) is not tuple:
            raise TypeError("document diagnostic reference sidecars must be an exact tuple")
        for rows in self.documents:
            if type(rows) is not tuple or not all(
                type(item) is NativeDiagnosticReferenceKindsV2 for item in rows
            ):
                raise TypeError("each document diagnostic sidecar table must be exact")
        if type(self.import_edges) is not tuple or not all(
            item is None or type(item) is NativeDiagnosticReferenceKindsV2
            for item in self.import_edges
        ):
            raise TypeError("import-edge diagnostic reference sidecars must be exact")


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeDocumentFacadeCardinalitiesV2:
    document_key: str
    effective_annotation_count: int
    effective_axiom_count: int
    effective_extension_count: int
    effective_origin_count: int
    raw_source_prefix_count: int
    rdf_unconsumed_triple_count: int
    rdf_rule_count: int
    rdf_diagnostic_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "document_key", _copy_document_key(self.document_key))
        for item in fields(self):
            if item.name != "document_key":
                _require_nonnegative_u64(
                    f"document facade cardinality {item.name}",
                    getattr(self, item.name),
                )


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeClosureFacadeCardinalitiesV2:
    effective_annotation_count: int
    effective_axiom_count: int
    effective_extension_count: int
    effective_origin_count: int

    def __post_init__(self) -> None:
        for item in fields(self):
            _require_nonnegative_u64(
                f"closure facade cardinality {item.name}",
                getattr(self, item.name),
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeFacadeCardinalitySummaryV2:
    documents: tuple[NativeDocumentFacadeCardinalitiesV2, ...]
    closure: NativeClosureFacadeCardinalitiesV2

    def __post_init__(self) -> None:
        if type(self.documents) is not tuple or not all(
            type(item) is NativeDocumentFacadeCardinalitiesV2 for item in self.documents
        ):
            raise TypeError("facade document cardinalities must be an exact tuple")
        keys = tuple(item.document_key for item in self.documents)
        if keys != tuple(sorted(keys, key=str.encode)) or len(keys) != len(set(keys)):
            raise ValueError("facade document cardinalities must be key-ascending unique")
        if type(self.closure) is not NativeClosureFacadeCardinalitiesV2:
            raise TypeError("facade closure cardinalities must be an exact record")


def _validate_exact_diagnostic_reference_sidecars_v2(value: object) -> None:
    if type(value) is not NativeDiagnosticReferenceSidecarsV2:
        raise TypeError("diagnostic reference sidecars must be an exact V2 record")
    sidecars = value
    if type(sidecars.snapshot) is not tuple or type(sidecars.documents) is not tuple:
        raise TypeError("diagnostic reference sidecar tables must be exact tuples")
    if type(sidecars.import_edges) is not tuple:
        raise TypeError("import-edge diagnostic sidecars must be an exact tuple")

    def validate_kinds(item: object) -> None:
        if type(item) is not NativeDiagnosticReferenceKindsV2:
            raise TypeError("diagnostic reference kinds must be an exact V2 record")
        kinds = item
        if kinds.document_reference_kind is not None and (
            type(kinds.document_reference_kind) is not NativeDiagnosticReferenceKindV2
        ):
            raise TypeError("diagnostic document reference kind must be exact")
        if type(kinds.import_chain_kinds) is not tuple or not all(
            type(kind) is NativeDiagnosticReferenceKindV2 for kind in kinds.import_chain_kinds
        ):
            raise TypeError("diagnostic import-chain kinds must be an exact tuple")

    for item in sidecars.snapshot:
        validate_kinds(item)
    for rows in sidecars.documents:
        if type(rows) is not tuple:
            raise TypeError("document diagnostic sidecars must be exact tuples")
        for item in rows:
            validate_kinds(item)
    for edge_sidecar in sidecars.import_edges:
        if edge_sidecar is not None:
            validate_kinds(edge_sidecar)


def _validate_exact_owl2_summary_v2(value: object, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if type(value) is not NativeOWL2DLReportSummaryV2:
        raise TypeError("OWL2-DL summary must be an exact V2 record")
    summary = value
    _require_nonnegative_u64("OWL2-DL structural values checked", summary.structural_values_checked)
    if type(summary.structural_complete) is not bool or type(summary.report_complete) is not bool:
        raise TypeError("OWL2-DL completeness flags must be exact bools")
    for name in (
        "structural_issue_count",
        "issue_count",
        "role_property_count",
        "role_hierarchy_count",
        "role_composite_count",
        "role_non_simple_count",
    ):
        _require_nonnegative_u64(f"OWL2-DL summary {name}", getattr(summary, name))


def _validate_exact_facade_cardinality_summary_v2(value: object) -> None:
    if type(value) is not NativeFacadeCardinalitySummaryV2:
        raise TypeError("facade cardinality summary must be an exact V2 record")
    summary = value
    if type(summary.documents) is not tuple:
        raise TypeError("facade document cardinalities must be an exact tuple")
    for row in summary.documents:
        if type(row) is not NativeDocumentFacadeCardinalitiesV2:
            raise TypeError("facade document cardinality must be an exact V2 record")
        _copy_document_key(row.document_key)
        for item in fields(row):
            if item.name != "document_key":
                _require_nonnegative_u64(
                    f"document facade cardinality {item.name}",
                    getattr(row, item.name),
                )
    if type(summary.closure) is not NativeClosureFacadeCardinalitiesV2:
        raise TypeError("facade closure cardinality must be an exact V2 record")
    for item in fields(summary.closure):
        _require_nonnegative_u64(
            f"closure facade cardinality {item.name}",
            getattr(summary.closure, item.name),
        )


def native_diagnostic_reference_kinds_v2(
    *,
    document_reference: IRI | str | None,
    import_chain: tuple[IRI | str, ...],
) -> NativeDiagnosticReferenceKindsV2:
    """Capture IRI-vs-text identity before V1 diagnostic scalar flattening."""

    if type(import_chain) is not tuple:
        raise TypeError("diagnostic import chain must be an exact tuple")

    def classify(
        value: object,
        *,
        optional: bool = False,
    ) -> NativeDiagnosticReferenceKindV2 | None:
        if value is None and optional:
            return None
        if type(value) is IRI:
            return NativeDiagnosticReferenceKindV2.IRI
        if type(value) is str:
            return NativeDiagnosticReferenceKindV2.TEXT
        raise TypeError("diagnostic references must be exact IRI or str values")

    return NativeDiagnosticReferenceKindsV2(
        document_reference_kind=classify(document_reference, optional=True),
        import_chain_kinds=tuple(
            cast(NativeDiagnosticReferenceKindV2, classify(item)) for item in import_chain
        ),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeOWL2DLIssueRowV2:
    code: str
    severity: ValidationSeverity
    message: str
    constructor: str | None

    def __post_init__(self) -> None:
        _validate_owl2_dl_issue_row_v2(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeOWL2DLStructuralIssueRowV2:
    code: str
    severity: ValidationSeverity
    message: str
    constructor: str | None

    def __post_init__(self) -> None:
        _validate_owl2_dl_issue_row_v2(self)


def _validate_owl2_dl_issue_row_v2(
    value: NativeOWL2DLIssueRowV2 | NativeOWL2DLStructuralIssueRowV2,
) -> None:
    _require_aux_text_v2("OWL2-DL issue code", value.code, nonempty=True)
    if type(value.severity) is not ValidationSeverity:
        raise TypeError("OWL2-DL issue severity must be an exact ValidationSeverity")
    _require_aux_text_v2("OWL2-DL issue message", value.message, nonempty=True)
    if value.constructor is not None:
        _require_aux_text_v2(
            "OWL2-DL issue constructor",
            value.constructor,
            nonempty=True,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeOWL2DLRoleEdgeRowV2:
    sub_property: bytes
    super_property: bytes
    _validation_limits: InitVar[ParseLimits | None] = None
    _validated_sub_property: ObjectProperty | ObjectInverseOf = field(
        init=False,
        repr=False,
        compare=False,
    )
    _validated_super_property: ObjectProperty | ObjectInverseOf = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self, _validation_limits: ParseLimits | None) -> None:
        decoded: list[ObjectProperty | ObjectInverseOf] = []
        for name in ("sub_property", "super_property"):
            row = getattr(self, name)
            if type(row) is not bytes or not row:
                raise TypeError(f"OWL2-DL role edge {name} must be nonempty exact bytes")
            try:
                value = decode_canonical(row, limits=_validation_limits)
            except Exception as error:
                raise ValueError(f"OWL2-DL role edge {name} is not canonical-model-v2") from error
            if not isinstance(value, (ObjectProperty, ObjectInverseOf)):
                raise ValueError(f"OWL2-DL role edge {name} has the wrong structural type")
            if canonical_bytes(value, limits=_validation_limits) != row:
                raise ValueError(f"OWL2-DL role edge {name} is not in canonical form")
            decoded.append(value)
        object.__setattr__(self, "_validated_sub_property", decoded[0])
        object.__setattr__(self, "_validated_super_property", decoded[1])

    def _validated_properties_v2(
        self,
    ) -> tuple[ObjectProperty | ObjectInverseOf, ObjectProperty | ObjectInverseOf]:
        """Return the validation-decoded endpoints without decoding them again."""

        return self._validated_sub_property, self._validated_super_property


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeFingerprintEvidenceV2:
    tag: int
    document_key: str | None
    preimage_byte_length: int
    fingerprint_schema: int
    digest: bytes

    def __post_init__(self) -> None:
        _require_nonnegative_u32("fingerprint evidence tag", self.tag)
        if self.tag not in {1, 2, 3, 4}:
            raise ValueError("fingerprint evidence tag must be 1, 2, 3, or 4")
        if self.tag == 1:
            if self.document_key is None:
                raise ValueError("document fingerprint evidence requires a document key")
            object.__setattr__(self, "document_key", _copy_document_key(self.document_key))
        elif self.document_key is not None:
            raise ValueError("snapshot fingerprint evidence cannot carry a document key")
        _require_nonnegative_u64(
            "fingerprint evidence preimage byte length", self.preimage_byte_length
        )
        _require_nonnegative_u32("fingerprint evidence schema", self.fingerprint_schema)
        if self.fingerprint_schema == 0:
            raise ValueError("fingerprint evidence schema must be positive")
        _require_digest("fingerprint evidence digest", self.digest)


def _validate_exact_fingerprint_evidence_v2(value: object) -> None:
    if type(value) is not NativeFingerprintEvidenceV2:
        raise TypeError("fingerprint evidence must be an exact V2 record")
    evidence = value
    _require_nonnegative_u32("fingerprint evidence tag", evidence.tag)
    if evidence.tag not in {1, 2, 3, 4}:
        raise ValueError("fingerprint evidence tag must be 1, 2, 3, or 4")
    if evidence.tag == 1:
        if evidence.document_key is None:
            raise ValueError("document fingerprint evidence requires a document key")
        _copy_document_key(evidence.document_key)
    elif evidence.document_key is not None:
        raise ValueError("snapshot fingerprint evidence cannot carry a document key")
    _require_nonnegative_u64(
        "fingerprint evidence preimage byte length",
        evidence.preimage_byte_length,
    )
    _require_nonnegative_u32("fingerprint evidence schema", evidence.fingerprint_schema)
    if evidence.fingerprint_schema == 0:
        raise ValueError("fingerprint evidence schema must be positive")
    _require_digest("fingerprint evidence digest", evidence.digest)


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeSnapshotContentDigestsV2:
    root_table_sha256: bytes
    effective_root_table_sha256: bytes
    fingerprint_inputs_sha256: bytes
    source_manifest_sha256: bytes
    provenance_manifest_sha256: bytes
    effective_origin_manifest_sha256: bytes

    def __post_init__(self) -> None:
        for item in fields(self):
            _require_digest(item.name, getattr(self, item.name))


def native_snapshot_content_digests_v2(
    *,
    documents: tuple[NativeDocumentPublicationV1, ...],
    report: NativeLoadReportPublicationV1,
    root_document_key: str,
    load_options: LoadOptions,
    capability_bits: int,
    collections: Mapping[_FixtureKey, Sequence[bytes]],
    fingerprint_evidence: tuple[NativeFingerprintEvidenceV2, ...],
    fingerprint_preimages: tuple[bytes, ...],
    owl2_dl_report_summary: NativeOWL2DLReportSummaryV2 | None,
    facade_cardinality_summary: NativeFacadeCardinalitySummaryV2,
    raw_document_collections: Mapping[_FixtureKey, Sequence[bytes]] | None = None,
) -> NativeSnapshotContentDigestsV2:
    """Compute the exact owner-side V2 content manifest digests."""

    if type(documents) is not tuple or not documents:
        raise TypeError("content manifest documents must be a nonempty exact tuple")
    for document in documents:
        _validate_exact_document_v2(document)
    keys = tuple(item.document_key for item in documents)
    if keys != tuple(sorted(keys, key=str.encode)) or len(set(keys)) != len(keys):
        raise ValueError("content manifest documents must be UTF-8 key ascending unique")
    _validate_exact_report_v2(report)
    _validate_exact_load_options_v2(load_options)
    selected_root_key = _copy_document_key(root_document_key)
    if selected_root_key not in set(keys):
        raise ValueError("content manifest root document key is unknown")
    _require_nonnegative_u64("content manifest capability bits", capability_bits)
    if type(fingerprint_evidence) is not tuple:
        raise TypeError("fingerprint evidence must be an exact tuple")
    if len(fingerprint_evidence) != len(documents) + 3:
        raise ValueError("fingerprint evidence count must be document_count + 3")
    for item in fingerprint_evidence:
        _validate_exact_fingerprint_evidence_v2(item)
    if type(fingerprint_preimages) is not tuple or not all(
        type(item) is bytes for item in fingerprint_preimages
    ):
        raise TypeError("fingerprint preimages must be an exact tuple of exact bytes")
    if len(fingerprint_preimages) != len(fingerprint_evidence):
        raise ValueError("fingerprint preimages and evidence are not aligned")

    effective_document_rows = tuple(
        _manifest_document_rows_v2(collections, ordinal) for ordinal in range(len(documents))
    )
    raw_collections = collections if raw_document_collections is None else raw_document_collections
    _facade_cardinality_summary_sha256_v2(
        facade_cardinality_summary,
        documents,
        report,
        capability_bits=capability_bits,
        load_options=load_options,
        owl2_dl_report_summary=owl2_dl_report_summary,
    )
    _validate_facade_cardinality_collections_v2(
        facade_cardinality_summary,
        collections,
        raw_collections,
    )
    raw_document_rows = tuple(
        _manifest_document_rows_v2(raw_collections, ordinal) for ordinal in range(len(documents))
    )
    effective_digest_indexes = _structural_digest_indexes_v2(
        effective_document_rows,
        load_options.limits,
    )
    raw_digest_indexes = (
        effective_digest_indexes
        if raw_collections is collections
        else _structural_digest_indexes_v2(raw_document_rows, load_options.limits)
    )
    root_digest = _root_table_manifest_sha256_v2(
        documents,
        report.model_schema,
        raw_document_rows,
    )
    effective_root_digest = _effective_root_table_manifest_sha256_v2(
        documents,
        report,
        collections,
        effective_document_rows,
    )
    fingerprint_digest = _fingerprint_inputs_manifest_sha256_v2(
        documents,
        report,
        selected_root_key,
        fingerprint_evidence,
        fingerprint_preimages,
    )
    source_digest = _source_manifest_sha256_v2(
        documents,
        capability_bits,
        raw_document_rows,
        raw_digest_indexes,
    )
    provenance_digest = _provenance_manifest_sha256_v2(
        documents,
        capability_bits,
        raw_document_rows,
        effective_document_rows,
        raw_digest_indexes,
        load_options,
    )
    effective_origin_digest = _effective_origin_manifest_sha256_v2(
        documents,
        collections,
        effective_document_rows,
        effective_digest_indexes,
    )
    _validate_owl2_dl_report_manifest_v2(
        report,
        owl2_dl_report_summary,
        collections,
        load_options.limits,
    )
    return NativeSnapshotContentDigestsV2(
        root_table_sha256=root_digest,
        effective_root_table_sha256=effective_root_digest,
        fingerprint_inputs_sha256=fingerprint_digest,
        source_manifest_sha256=source_digest,
        provenance_manifest_sha256=provenance_digest,
        effective_origin_manifest_sha256=effective_origin_digest,
    )


def _effective_root_table_manifest_sha256_v2(
    documents: tuple[NativeDocumentPublicationV1, ...],
    report: NativeLoadReportPublicationV1,
    collections: Mapping[_FixtureKey, Sequence[bytes]],
    document_rows: tuple[_ManifestDocumentRowsV2, ...],
) -> bytes:
    sections = (
        (1, NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS),
        (2, NativeFacadeCollectionV2.AXIOMS),
        (3, NativeFacadeCollectionV2.EXTENSIONS),
    )
    body = bytearray(_u32_le_v2(report.model_schema) + _u64_le_v2(len(documents)))
    for document, rows in zip(documents, document_rows, strict=True):
        document_body = bytearray(_text64_v2(document.document_key))
        counts: list[int] = []
        for tag, collection in sections:
            values = rows[collection]
            counts.append(len(values))
            document_body.extend(bytes((tag,)) + _u64_le_v2(len(values)))
            document_body.extend(b"".join(_frame64_v2(row) for row in values))
        document_digest = _manifest_hash_v2(
            NATIVE_EFFECTIVE_DOCUMENT_ROOT_TABLE_DOMAIN_V2,
            bytes(document_body),
        )
        body.extend(_text64_v2(document.document_key))
        body.extend(b"".join(_u64_le_v2(value) for value in counts))
        body.extend(document_digest)

    for collection in _ROOT_STRUCTURAL_COLLECTIONS_V2:
        closure_key: _FixtureKey = (
            collection,
            NativeFacadeScopeV2.CLOSURE,
            None,
            NativeSignatureKindV2.ALL,
            True,
        )
        source = collections.get(closure_key, ())
        if type(source) is not tuple:
            raise TypeError("effective closure root rows must be exact tuples")
        closure_rows = source
        expected = tuple(sorted({row for rows in document_rows for row in rows[collection]}))
        if closure_rows != expected:
            _fail(
                "V2 closure roots diverge from the non-copied effective document union",
                "NATIVE_EFFECTIVE_ROOT_TABLE",
            )
    closure_axioms = collections.get(
        (
            NativeFacadeCollectionV2.AXIOMS,
            NativeFacadeScopeV2.CLOSURE,
            None,
            NativeSignatureKindV2.ALL,
            True,
        ),
        (),
    )
    if len(closure_axioms) != report.effective_axiom_count:
        _fail(
            "V2 effective closure axiom rows diverge from report metadata",
            "NATIVE_PAGE_TOTAL",
        )
    return _manifest_hash_v2(NATIVE_EFFECTIVE_ROOT_TABLE_MANIFEST_DOMAIN_V2, bytes(body))


_StructuralDigestIndexesV2 = tuple[frozenset[bytes], ...]


def _structural_digest_indexes_v2(
    document_rows: tuple[_ManifestDocumentRowsV2, ...],
    limits: ParseLimits,
) -> _StructuralDigestIndexesV2:
    indexes: list[frozenset[bytes]] = []
    for rows in document_rows:
        digests: set[bytes] = set()
        for collection in _ROOT_STRUCTURAL_COLLECTIONS_V2:
            retained = rows[collection]
            bound = max((1, *(len(row) for row in retained)))
            decoded = _validate_page_rows_v2(
                collection,
                retained,
                len(retained),
                bound,
                NativeSignatureKindV2.ALL,
                True,
                limits=limits,
            )
            digests.update(
                structural_digest(cast(StructuralNode, value), limits=limits) for value in decoded
            )
        indexes.append(frozenset(digests))
    return tuple(indexes)


def _effective_origin_manifest_sha256_v2(
    documents: tuple[NativeDocumentPublicationV1, ...],
    collections: Mapping[_FixtureKey, Sequence[bytes]],
    document_rows: tuple[_ManifestDocumentRowsV2, ...],
    digest_indexes: _StructuralDigestIndexesV2,
) -> bytes:
    body = bytearray(NATIVE_AUXILIARY_CODEC_SCHEMA_SHA256_V2 + _u64_le_v2(len(documents)))
    decoded_document_origins: list[tuple[tuple[bytes, NativeOriginRowV2], ...]] = []
    for document, rows, digests in zip(
        documents,
        document_rows,
        digest_indexes,
        strict=True,
    ):
        origins = rows[NativeFacadeCollectionV2.ORIGIN_ENTRIES]
        decoded_origins = cast(
            tuple[NativeOriginRowV2, ...],
            _validate_manifest_reference_rows_v2(
                NativeFacadeCollectionV2.ORIGIN_ENTRIES,
                origins,
                allowed_digests=digests,
                expected_document_key=document.document_key,
            ),
        )
        _validate_effective_origin_order_v2(origins, decoded_origins)
        decoded_document_origins.append(tuple(zip(origins, decoded_origins, strict=True)))
        document_body = (
            _text64_v2(document.document_key)
            + _u64_le_v2(len(origins))
            + b"".join(_frame64_v2(row) for row in origins)
        )
        document_digest = _manifest_hash_v2(
            NATIVE_EFFECTIVE_DOCUMENT_ORIGIN_TABLE_DOMAIN_V2,
            document_body,
        )
        body.extend(_text64_v2(document.document_key) + _u64_le_v2(len(origins)) + document_digest)

    closure_key: _FixtureKey = (
        NativeFacadeCollectionV2.ORIGIN_ENTRIES,
        NativeFacadeScopeV2.CLOSURE,
        None,
        NativeSignatureKindV2.ALL,
        True,
    )
    source = collections.get(closure_key, ())
    if type(source) is not tuple:
        raise TypeError("effective closure origin rows must be an exact tuple")
    closure_origins = source
    digest_by_document_key = {
        document.document_key: digests
        for document, digests in zip(documents, digest_indexes, strict=True)
    }
    decoded_closure = cast(
        tuple[NativeOriginRowV2, ...],
        _validate_manifest_reference_rows_v2(
            NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            closure_origins,
            digest_by_document_key=digest_by_document_key,
        ),
    )
    decoded_by_row = {
        encoded: decoded for rows in decoded_document_origins for encoded, decoded in rows
    }
    expected = tuple(
        sorted(
            decoded_by_row,
            key=lambda encoded: _canonical_origin_order_key_v2(decoded_by_row[encoded], encoded),
        )
    )
    if closure_origins != expected:
        _fail(
            "V2 closure origins diverge from the effective merged/deduplicated index",
            "NATIVE_EFFECTIVE_ORIGIN_TABLE",
        )
    _validate_effective_origin_order_v2(closure_origins, decoded_closure)
    closure_digest = _manifest_hash_v2(
        NATIVE_EFFECTIVE_CLOSURE_ORIGIN_TABLE_DOMAIN_V2,
        _u64_le_v2(len(closure_origins)) + b"".join(_frame64_v2(row) for row in closure_origins),
    )
    body.extend(_u64_le_v2(len(closure_origins)) + closure_digest)
    return _manifest_hash_v2(NATIVE_EFFECTIVE_ORIGIN_MANIFEST_DOMAIN_V2, bytes(body))


def _validate_owl2_dl_report_manifest_v2(
    report: NativeLoadReportPublicationV1,
    summary: NativeOWL2DLReportSummaryV2 | None,
    collections: Mapping[_FixtureKey, Sequence[bytes]],
    limits: ParseLimits,
) -> None:
    sections = (
        (1, NativeFacadeCollectionV2.OWL2_DL_STRUCTURAL_ISSUES),
        (2, NativeFacadeCollectionV2.OWL2_DL_ISSUES),
        (3, NativeFacadeCollectionV2.OWL2_DL_ROLE_PROPERTIES),
        (4, NativeFacadeCollectionV2.OWL2_DL_ROLE_HIERARCHY),
        (5, NativeFacadeCollectionV2.OWL2_DL_ROLE_COMPOSITE),
        (6, NativeFacadeCollectionV2.OWL2_DL_ROLE_NON_SIMPLE),
    )
    rows_by_collection: dict[NativeFacadeCollectionV2, tuple[bytes, ...]] = {}
    largest_row = 1
    for _tag, collection in sections:
        key: _FixtureKey = (
            collection,
            NativeFacadeScopeV2.CLOSURE,
            None,
            NativeSignatureKindV2.ALL,
            True,
        )
        source = collections.get(key, ())
        if type(source) is not tuple:
            raise TypeError("OWL2-DL report rows must be exact tuples")
        rows = source
        if not all(type(row) is bytes and row for row in rows):
            raise TypeError("OWL2-DL report rows must be nonempty exact bytes")
        largest_row = max((largest_row, *(len(row) for row in rows)))
        rows_by_collection[collection] = rows

    if not report.owl2_dl_validated:
        if summary is not None or any(rows_by_collection.values()):
            _fail(
                "unvalidated V2 publication cannot retain an OWL2-DL report",
                "NATIVE_OWL2_DL_REPORT",
            )
        return
    if type(summary) is not NativeOWL2DLReportSummaryV2:
        _fail(
            "validated V2 publication requires an exact OWL2-DL report summary",
            "NATIVE_OWL2_DL_REPORT",
        )

    selected_summary = cast(NativeOWL2DLReportSummaryV2, summary)
    if (
        report.owl2_dl_conforms is not True
        or not selected_summary.structural_complete
        or not selected_summary.report_complete
    ):
        _fail(
            "successful V2 publication requires a complete conforming OWL2-DL report",
            "NATIVE_OWL2_DL_REPORT",
        )
    expected_counts = (
        selected_summary.structural_issue_count,
        selected_summary.issue_count,
        selected_summary.role_property_count,
        selected_summary.role_hierarchy_count,
        selected_summary.role_composite_count,
        selected_summary.role_non_simple_count,
    )
    observed_counts = tuple(len(rows_by_collection[collection]) for _tag, collection in sections)
    if observed_counts != expected_counts:
        _fail(
            "V2 OWL2-DL rows diverge from the report summary counts",
            "NATIVE_PAGE_TOTAL",
        )
    validated_rows: dict[NativeFacadeCollectionV2, tuple[object, ...]] = {}
    for _tag, collection in sections:
        rows = rows_by_collection[collection]
        validated_rows[collection] = _validate_page_rows_v2(
            collection,
            rows,
            len(rows),
            largest_row,
            NativeSignatureKindV2.ALL,
            True,
            limits=limits,
        )

    issue_values = tuple(
        cast(
            NativeOWL2DLIssueRowV2 | NativeOWL2DLStructuralIssueRowV2,
            value,
        )
        for collection in (
            NativeFacadeCollectionV2.OWL2_DL_STRUCTURAL_ISSUES,
            NativeFacadeCollectionV2.OWL2_DL_ISSUES,
        )
        for value in validated_rows[collection]
    )
    conforms = (
        selected_summary.structural_complete
        and selected_summary.report_complete
        and not any(value.severity is ValidationSeverity.ERROR for value in issue_values)
    )
    if report.owl2_dl_conforms is not conforms:
        _fail(
            "V2 OWL2-DL conforms flag diverges from report completeness and severities",
            "NATIVE_OWL2_DL_REPORT",
        )

    body = bytearray(
        _u64_le_v2(selected_summary.structural_values_checked)
        + bytes(
            (
                int(selected_summary.structural_complete),
                int(selected_summary.report_complete),
            )
        )
        + b"".join(_u64_le_v2(value) for value in expected_counts)
    )
    for tag, collection in sections:
        rows = rows_by_collection[collection]
        body.extend(bytes((tag,)) + _u64_le_v2(len(rows)))
        body.extend(b"".join(_frame64_v2(row) for row in rows))
    digest = _manifest_hash_v2(NATIVE_OWL2_DL_REPORT_DOMAIN_V2, bytes(body))
    if digest != report.owl2_dl_report_sha256:
        _fail("V2 OWL2-DL report digest diverges from metadata", "NATIVE_OWL2_DL_REPORT")


_ManifestDocumentRowsV2 = FrozenMap[NativeFacadeCollectionV2, tuple[bytes, ...]]


def _manifest_document_rows_v2(
    collections: Mapping[_FixtureKey, Sequence[bytes]],
    document_ordinal: int,
) -> _ManifestDocumentRowsV2:
    selected: dict[NativeFacadeCollectionV2, tuple[bytes, ...]] = {}
    for collection in NativeFacadeCollectionV2:
        if collection is NativeFacadeCollectionV2.SIGNATURE:
            continue
        key: _FixtureKey = (
            collection,
            NativeFacadeScopeV2.DOCUMENT,
            document_ordinal,
            NativeSignatureKindV2.ALL,
            True,
        )
        source = collections.get(key, ())
        if type(source) is not tuple:
            raise TypeError("content manifest collection rows must be exact tuples")
        rows = source
        _require_nonnegative_u64("content manifest collection row count", len(rows))
        if not all(type(row) is bytes and row for row in rows):
            raise TypeError("content manifest rows must be nonempty exact bytes")
        selected[collection] = rows
    return FrozenMap(selected)


def _root_table_manifest_sha256_v2(
    documents: tuple[NativeDocumentPublicationV1, ...],
    model_schema: int,
    document_rows: tuple[_ManifestDocumentRowsV2, ...],
) -> bytes:
    body = bytearray(_u32_le_v2(model_schema) + _u64_le_v2(len(documents)))
    for document, rows in zip(documents, document_rows, strict=True):
        sections = (
            (1, NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS),
            (2, NativeFacadeCollectionV2.AXIOMS),
            (3, NativeFacadeCollectionV2.EXTENSIONS),
        )
        document_body = bytearray(_text64_v2(document.document_key))
        counts: list[int] = []
        for tag, collection in sections:
            values = rows[collection]
            counts.append(len(values))
            document_body.extend(bytes((tag,)) + _u64_le_v2(len(values)))
            document_body.extend(b"".join(_frame64_v2(row) for row in values))
        if counts != [
            document.ontology_annotation_count,
            document.axiom_count,
            document.extension_count,
        ]:
            _fail("V2 root table rows diverge from document counts", "NATIVE_PAGE_TOTAL")
        document_digest = _manifest_hash_v2(
            NATIVE_DOCUMENT_ROOT_TABLE_DOMAIN_V2,
            bytes(document_body),
        )
        body.extend(_text64_v2(document.document_key))
        body.extend(b"".join(_u64_le_v2(value) for value in counts))
        body.extend(document_digest)
    return _manifest_hash_v2(NATIVE_ROOT_TABLE_MANIFEST_DOMAIN_V2, bytes(body))


def _fingerprint_inputs_manifest_sha256_v2(
    documents: tuple[NativeDocumentPublicationV1, ...],
    report: NativeLoadReportPublicationV1,
    root_document_key: str,
    evidence: tuple[NativeFingerprintEvidenceV2, ...],
    preimages: tuple[bytes, ...],
) -> bytes:
    expected = (
        *((1, document.document_key, document.document_fingerprint) for document in documents),
        (2, None, report.structural_fingerprint),
        (3, None, report.logical_fingerprint),
        (4, None, report.signature_fingerprint),
    )
    body = bytearray(
        _u32_le_v2(report.model_schema) + _text64_v2(root_document_key) + _u64_le_v2(len(documents))
    )
    for observed, preimage, (tag, document_key, fingerprint) in zip(
        evidence,
        preimages,
        expected,
        strict=True,
    ):
        authoritative_digest = hashlib.sha256(preimage).digest()
        if (
            observed.tag != tag
            or observed.document_key != document_key
            or observed.preimage_byte_length != len(preimage)
            or observed.fingerprint_schema != fingerprint.schema
            or observed.digest != authoritative_digest
            or authoritative_digest != fingerprint.digest
        ):
            _fail(
                "V2 fingerprint evidence diverges from its authoritative preimage "
                "or published fingerprint",
                "NATIVE_FINGERPRINT_INPUTS",
            )
        body.extend(bytes((observed.tag,)))
        if observed.document_key is not None:
            body.extend(_text64_v2(observed.document_key))
        body.extend(_u64_le_v2(observed.preimage_byte_length))
        body.extend(_u32_le_v2(observed.fingerprint_schema))
        body.extend(observed.digest)
    return _manifest_hash_v2(NATIVE_FINGERPRINT_INPUTS_MANIFEST_DOMAIN_V2, bytes(body))


def _source_manifest_sha256_v2(
    documents: tuple[NativeDocumentPublicationV1, ...],
    capability_bits: int,
    document_rows: tuple[_ManifestDocumentRowsV2, ...],
    digest_indexes: _StructuralDigestIndexesV2,
) -> bytes:
    present = bool(capability_bits & 8)
    body = bytearray(NATIVE_AUXILIARY_CODEC_SCHEMA_SHA256_V2 + _u64_le_v2(len(documents)))
    for document, rows, digests in zip(
        documents,
        document_rows,
        digest_indexes,
        strict=True,
    ):
        entries = rows[NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES]
        prefixes = rows[NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES]
        body.extend(_text64_v2(document.document_key))
        if not present:
            if entries or prefixes or document.source_map_entry_count:
                _fail("V2 source rows exist without capability", "NATIVE_PUBLICATION_CAPABILITY")
            body.extend(b"\x00")
            continue
        if len(entries) != document.source_map_entry_count:
            _fail("V2 source rows diverge from document count", "NATIVE_PAGE_TOTAL")
        _validate_manifest_reference_rows_v2(
            NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES,
            entries,
            allowed_digests=digests,
        )
        document_body = (
            _text64_v2(document.document_key)
            + _u64_le_v2(len(entries))
            + b"".join(_frame64_v2(row) for row in entries)
            + _u64_le_v2(len(prefixes))
            + b"".join(_frame64_v2(row) for row in prefixes)
        )
        digest = _manifest_hash_v2(NATIVE_DOCUMENT_SOURCE_TABLE_DOMAIN_V2, document_body)
        body.extend(b"\x01" + _u64_le_v2(len(entries)) + _u64_le_v2(len(prefixes)) + digest)
    return _manifest_hash_v2(NATIVE_SOURCE_MANIFEST_DOMAIN_V2, bytes(body))


def _provenance_manifest_sha256_v2(
    documents: tuple[NativeDocumentPublicationV1, ...],
    capability_bits: int,
    raw_document_rows: tuple[_ManifestDocumentRowsV2, ...],
    effective_document_rows: tuple[_ManifestDocumentRowsV2, ...],
    raw_digest_indexes: _StructuralDigestIndexesV2,
    load_options: LoadOptions,
) -> bytes:
    origin_present = bool(capability_bits & 16)
    body = bytearray(NATIVE_AUXILIARY_CODEC_SCHEMA_SHA256_V2 + _u64_le_v2(len(documents)))
    for document, raw_rows, effective_rows, raw_digests in zip(
        documents,
        raw_document_rows,
        effective_document_rows,
        raw_digest_indexes,
        strict=True,
    ):
        origins = raw_rows[NativeFacadeCollectionV2.ORIGIN_ENTRIES]
        body.extend(_text64_v2(document.document_key))
        if not origin_present:
            if origins or document.origin_entry_count:
                _fail("V2 origin rows exist without capability", "NATIVE_PUBLICATION_CAPABILITY")
            body.extend(b"\x00")
        else:
            if len(origins) != document.origin_entry_count:
                _fail("V2 origin rows diverge from document count", "NATIVE_PAGE_TOTAL")
            _validate_manifest_reference_rows_v2(
                NativeFacadeCollectionV2.ORIGIN_ENTRIES,
                origins,
                allowed_digests=raw_digests,
                expected_document_key=document.document_key,
                alternate_document_key=document.document_fingerprint.digest.hex(),
            )
            origin_body = (
                _text64_v2(document.document_key)
                + _u64_le_v2(len(origins))
                + b"".join(_frame64_v2(row) for row in origins)
            )
            origin_digest = _manifest_hash_v2(
                NATIVE_DOCUMENT_ORIGIN_TABLE_DOMAIN_V2,
                origin_body,
            )
            body.extend(b"\x01" + _u64_le_v2(len(origins)) + origin_digest)
        _extend_rdf_manifest_entry_v2(
            body,
            document,
            effective_rows,
            capability_bits,
            load_options,
        )
    return _manifest_hash_v2(NATIVE_PROVENANCE_MANIFEST_DOMAIN_V2, bytes(body))


def _extend_rdf_manifest_entry_v2(
    body: bytearray,
    document: NativeDocumentPublicationV1,
    rows: _ManifestDocumentRowsV2,
    capability_bits: int,
    load_options: LoadOptions,
) -> None:
    header = rows[NativeFacadeCollectionV2.RDF_REPORT_HEADER]
    triples = rows[NativeFacadeCollectionV2.RDF_UNCONSUMED_TRIPLES]
    rules = rows[NativeFacadeCollectionV2.RDF_RULE_IDS]
    diagnostics = rows[NativeFacadeCollectionV2.RDF_DIAGNOSTICS]
    if not capability_bits & 32:
        if header or triples or rules or diagnostics:
            _fail("V2 RDF rows exist without capability", "NATIVE_PUBLICATION_CAPABILITY")
        if document.rdf_mapping_report_sha256 is not None:
            _fail(
                "V2 RDF metadata exists without capability",
                "NATIVE_PUBLICATION_CAPABILITY",
            )
    if document.rdf_mapping_report_sha256 is None:
        if header or triples or rules or diagnostics:
            _fail("V2 RDF rows exist without report metadata", "NATIVE_PUBLICATION_CAPABILITY")
        body.extend(b"\x00")
        return
    if len(header) != 1:
        _fail("V2 RDF report requires exactly one header row", "NATIVE_PAGE_TOTAL")
    bound = max(1, *(len(row) for row in (*header, *triples, *rules, *diagnostics)))
    decoded_header = cast(
        NativeRDFReportHeaderRowV2,
        decode_native_auxiliary_row_v2(
            NativeFacadeCollectionV2.RDF_REPORT_HEADER,
            header[0],
            max_row_bytes=bound,
        ),
    )
    if decoded_header.conformant != document.rdf_mapping_conformant:
        _fail("V2 RDF header conformant flag diverges from metadata", "NATIVE_RDF_REPORT")
    if decoded_header.total_triples > load_options.limits.max_triples:
        _fail("V2 RDF report exceeds max_triples", "NATIVE_PUBLICATION_LIMIT")
    if len(diagnostics) > load_options.limits.max_diagnostics:
        _fail("V2 RDF report exceeds max_diagnostics", "NATIVE_PUBLICATION_LIMIT")
    rdf_body = (
        _text64_v2(document.document_key)
        + _frame64_v2(header[0])
        + _u64_le_v2(len(triples))
        + b"".join(_frame64_v2(row) for row in triples)
        + _u64_le_v2(len(rules))
        + b"".join(_frame64_v2(row) for row in rules)
        + _u64_le_v2(len(diagnostics))
        + b"".join(_frame64_v2(row) for row in diagnostics)
    )
    rdf_digest = _manifest_hash_v2(NATIVE_RDF_MAPPING_REPORT_DOMAIN_V2, rdf_body)
    if rdf_digest != document.rdf_mapping_report_sha256:
        _fail("V2 RDF report digest diverges from metadata", "NATIVE_RDF_REPORT")
    body.extend(
        b"\x01"
        + _u64_le_v2(len(triples))
        + _u64_le_v2(len(rules))
        + _u64_le_v2(len(diagnostics))
        + rdf_digest
    )


def _validate_manifest_reference_rows_v2(
    collection: NativeFacadeCollectionV2,
    rows: tuple[bytes, ...],
    *,
    allowed_digests: frozenset[bytes] | None = None,
    expected_document_key: str | None = None,
    alternate_document_key: str | None = None,
    digest_by_document_key: Mapping[str, frozenset[bytes]] | None = None,
) -> tuple[NativeSourceMapRowV2 | NativeOriginRowV2, ...]:
    if (allowed_digests is None) == (digest_by_document_key is None):
        raise TypeError("manifest references require exactly one digest index mode")
    bound = max((1, *(len(row) for row in rows)))
    decoded_rows: list[NativeSourceMapRowV2 | NativeOriginRowV2] = []
    for row in rows:
        decoded = cast(
            NativeSourceMapRowV2 | NativeOriginRowV2,
            decode_native_auxiliary_row_v2(
                collection,
                row,
                max_row_bytes=bound,
            ),
        )
        selected_digests = allowed_digests
        if type(decoded) is NativeOriginRowV2:
            if (
                expected_document_key is not None
                and decoded.document_key != expected_document_key
                and decoded.document_key != alternate_document_key
            ):
                _fail(
                    "V2 origin row belongs to the wrong document",
                    "NATIVE_STRUCTURAL_DIGEST",
                )
            if digest_by_document_key is not None:
                selected_digests = digest_by_document_key.get(decoded.document_key)
                if selected_digests is None:
                    _fail(
                        "V2 origin row names an unknown document",
                        "NATIVE_STRUCTURAL_DIGEST",
                    )
        if selected_digests is None or decoded.digest not in selected_digests:
            _fail(
                "V2 source/origin row digest does not identify a structural row in its document",
                "NATIVE_STRUCTURAL_DIGEST",
            )
        decoded_rows.append(decoded)
    return tuple(decoded_rows)


def _canonical_origin_order_key_v2(
    value: NativeOriginRowV2,
    encoded: bytes,
) -> tuple[object, ...]:
    return (
        value.digest,
        value.document_key.encode("utf-8"),
        value.occurrence,
        encoded,
    )


def _validate_effective_origin_order_v2(
    rows: tuple[bytes, ...],
    decoded: tuple[NativeOriginRowV2, ...],
) -> None:
    keys = tuple(
        _canonical_origin_order_key_v2(value, encoded)
        for value, encoded in zip(decoded, rows, strict=True)
    )
    if any(left >= right for left, right in pairwise(keys)):
        _fail(
            "V2 effective origin rows must be canonically ascending unique",
            "NATIVE_EFFECTIVE_ORIGIN_TABLE",
        )


def _diagnostic_reference_kinds_sha256_v2(
    sidecars: NativeDiagnosticReferenceSidecarsV2,
    diagnostics: tuple[NativeDiagnosticPublicationV1, ...],
    documents: tuple[NativeDocumentPublicationV1, ...],
    manifest: NativeImportManifestPublicationV1,
) -> bytes:
    _validate_exact_diagnostic_reference_sidecars_v2(sidecars)
    if len(sidecars.snapshot) != len(diagnostics):
        _fail("snapshot diagnostic reference sidecars are not aligned", "NATIVE_DIAGNOSTICS")
    if len(sidecars.documents) != len(documents):
        _fail("document diagnostic sidecar tables are not aligned", "NATIVE_DIAGNOSTICS")
    if len(sidecars.import_edges) != len(manifest.edges):
        _fail("import-edge diagnostic sidecars are not aligned", "NATIVE_DIAGNOSTICS")

    body = bytearray(_u64_le_v2(len(diagnostics)))
    for diagnostic, kinds in zip(diagnostics, sidecars.snapshot, strict=True):
        _validate_diagnostic_reference_kinds_v2(diagnostic, kinds)
        body.extend(_frame64_v2(_diagnostic_reference_kinds_bytes_v2(kinds)))
    body.extend(_u64_le_v2(len(documents)))
    for document, rows in zip(documents, sidecars.documents, strict=True):
        if len(rows) != len(document.diagnostics):
            _fail("document diagnostic reference sidecars are not aligned", "NATIVE_DIAGNOSTICS")
        body.extend(_text64_v2(document.document_key) + _u64_le_v2(len(rows)))
        for diagnostic, kinds in zip(document.diagnostics, rows, strict=True):
            _validate_diagnostic_reference_kinds_v2(diagnostic, kinds)
            body.extend(_frame64_v2(_diagnostic_reference_kinds_bytes_v2(kinds)))
    body.extend(_u64_le_v2(len(manifest.edges)))
    for edge, edge_kinds in zip(manifest.edges, sidecars.import_edges, strict=True):
        if edge.diagnostic is None:
            if edge_kinds is not None:
                _fail("diagnostic-free import edge has a reference sidecar", "NATIVE_DIAGNOSTICS")
            body.extend(b"\x00")
            continue
        if type(edge_kinds) is not NativeDiagnosticReferenceKindsV2:
            _fail("diagnostic import edge lacks a reference sidecar", "NATIVE_DIAGNOSTICS")
        selected_edge_kinds = cast(NativeDiagnosticReferenceKindsV2, edge_kinds)
        _validate_diagnostic_reference_kinds_v2(edge.diagnostic, selected_edge_kinds)
        body.extend(
            b"\x01" + _frame64_v2(_diagnostic_reference_kinds_bytes_v2(selected_edge_kinds))
        )
    return _manifest_hash_v2(NATIVE_DIAGNOSTIC_REFERENCE_KINDS_DOMAIN_V2, bytes(body))


def _diagnostic_reference_kinds_bytes_v2(
    value: NativeDiagnosticReferenceKindsV2,
) -> bytes:
    body = bytearray(_encode_diagnostic_reference_kind_v2(value.document_reference_kind))
    body.extend(_u64_le_v2(len(value.import_chain_kinds)))
    for kind in value.import_chain_kinds:
        body.extend(_encode_diagnostic_reference_kind_v2(kind))
    return bytes(body)


def _diagnostic_reference_sidecar_values_v2(
    value: NativeDiagnosticReferenceSidecarsV2,
) -> tuple[object, ...]:
    def row(item: NativeDiagnosticReferenceKindsV2) -> tuple[object, ...]:
        return (
            item.document_reference_kind.value if item.document_reference_kind else None,
            tuple(kind.value for kind in item.import_chain_kinds),
        )

    return (
        tuple(row(item) for item in value.snapshot),
        tuple(tuple(row(item) for item in rows) for rows in value.documents),
        tuple(None if item is None else row(item) for item in value.import_edges),
    )


def _facade_cardinality_summary_sha256_v2(
    summary: NativeFacadeCardinalitySummaryV2,
    documents: tuple[NativeDocumentPublicationV1, ...],
    report: NativeLoadReportPublicationV1,
    *,
    capability_bits: int,
    load_options: LoadOptions,
    metadata_diagnostic_count: int = 0,
    owl2_dl_report_summary: NativeOWL2DLReportSummaryV2 | None = None,
) -> bytes:
    _validate_exact_facade_cardinality_summary_v2(summary)
    _validate_exact_owl2_summary_v2(owl2_dl_report_summary, optional=True)
    if tuple(item.document_key for item in summary.documents) != tuple(
        item.document_key for item in documents
    ):
        _fail("facade cardinality documents are not aligned", "NATIVE_PAGE_TOTAL")
    for summary_row, document in zip(summary.documents, documents, strict=True):
        if (
            summary_row.effective_annotation_count,
            summary_row.effective_axiom_count,
            summary_row.effective_extension_count,
        ) != (
            document.ontology_annotation_count,
            document.axiom_count,
            document.extension_count,
        ):
            _fail(
                "facade effective structural counts diverge from V1 document metadata",
                "NATIVE_PAGE_TOTAL",
            )
    if summary.closure.effective_axiom_count != report.effective_axiom_count:
        _fail(
            "facade closure axiom count diverges from report metadata",
            "NATIVE_PAGE_TOTAL",
        )
    _validate_facade_cardinality_limits_v2(
        summary,
        documents,
        capability_bits,
        load_options,
        metadata_diagnostic_count,
        owl2_dl_report_summary,
    )
    body = bytearray(_u64_le_v2(len(summary.documents)))
    for row in summary.documents:
        body.extend(_text64_v2(row.document_key))
        body.extend(
            b"".join(
                _u64_le_v2(getattr(row, item[1]))
                for item in NATIVE_DOCUMENT_FACADE_CARDINALITIES_FIELDS_V2
                if item[1] != "document_key"
            )
        )
    body.extend(
        b"".join(
            _u64_le_v2(getattr(summary.closure, item[1]))
            for item in NATIVE_CLOSURE_FACADE_CARDINALITIES_FIELDS_V2
        )
    )
    return _manifest_hash_v2(NATIVE_FACADE_CARDINALITY_SUMMARY_DOMAIN_V2, bytes(body))


def _validate_facade_cardinality_limits_v2(
    summary: NativeFacadeCardinalitySummaryV2,
    documents: tuple[NativeDocumentPublicationV1, ...],
    capability_bits: int,
    load_options: LoadOptions,
    metadata_diagnostic_count: int,
    owl2_dl_report_summary: NativeOWL2DLReportSummaryV2 | None,
) -> None:
    _require_nonnegative_u64("metadata diagnostic count", metadata_diagnostic_count)
    limits = load_options.limits
    has_source = bool(capability_bits & 8)
    has_origin = bool(capability_bits & 16)
    has_rdf = bool(capability_bits & 32)
    prefix_counts: list[int] = []
    origin_counts: list[int] = []
    annotation_counts: list[int] = []
    axiom_counts: list[int] = []
    triple_counts: list[int] = []
    rdf_diagnostic_counts: list[int] = []
    for row, document in zip(summary.documents, documents, strict=True):
        prefix_counts.append(row.raw_source_prefix_count)
        origin_counts.append(row.effective_origin_count)
        annotation_counts.append(row.effective_annotation_count)
        axiom_counts.append(row.effective_axiom_count)
        triple_counts.append(row.rdf_unconsumed_triple_count)
        rdf_diagnostic_counts.append(row.rdf_diagnostic_count)
        if row.raw_source_prefix_count and not has_source:
            _fail(
                "V2 source-prefix counts require the source-map capability",
                "NATIVE_PUBLICATION_CAPABILITY",
            )
        if row.effective_origin_count and not has_origin:
            _fail(
                "V2 effective-origin counts require the origin capability",
                "NATIVE_PUBLICATION_CAPABILITY",
            )
        rdf_subsections = (
            row.rdf_unconsumed_triple_count,
            row.rdf_rule_count,
            row.rdf_diagnostic_count,
        )
        if any(rdf_subsections) and (not has_rdf or document.rdf_mapping_report_sha256 is None):
            _fail(
                "V2 RDF subsection counts require capability and document report metadata",
                "NATIVE_PUBLICATION_CAPABILITY",
            )
        for observed, maximum, label in (
            (row.raw_source_prefix_count, limits.max_prefixes, "max_prefixes"),
            (row.effective_origin_count, limits.max_origin_entries, "max_origin_entries"),
            (row.effective_annotation_count, limits.max_annotations, "max_annotations"),
            (row.effective_axiom_count, limits.max_axioms, "max_axioms"),
            (row.rdf_unconsumed_triple_count, limits.max_triples, "max_triples"),
            (row.rdf_diagnostic_count, limits.max_diagnostics, "max_diagnostics"),
        ):
            if observed > maximum:
                _fail(
                    f"V2 facade cardinality exceeds {label}",
                    "NATIVE_PUBLICATION_LIMIT",
                )
    if summary.closure.effective_origin_count and not has_origin:
        _fail(
            "V2 closure-origin counts require the origin capability",
            "NATIVE_PUBLICATION_CAPABILITY",
        )
    aggregate_checks = (
        (_checked_sum("source prefix count", prefix_counts), limits.max_prefixes),
        (_checked_sum("effective origin count", origin_counts), limits.max_origin_entries),
        (_checked_sum("effective annotation count", annotation_counts), limits.max_annotations),
        (_checked_sum("effective axiom count", axiom_counts), limits.max_axioms),
        (_checked_sum("RDF triple count", triple_counts), limits.max_triples),
    )
    if any(observed > maximum for observed, maximum in aggregate_checks):
        _fail(
            "V2 aggregate facade cardinality exceeds configured limits",
            "NATIVE_PUBLICATION_LIMIT",
        )
    if summary.closure.effective_origin_count > limits.max_origin_entries:
        _fail("V2 closure origins exceed max_origin_entries", "NATIVE_PUBLICATION_LIMIT")
    if summary.closure.effective_annotation_count > limits.max_annotations:
        _fail("V2 closure annotations exceed max_annotations", "NATIVE_PUBLICATION_LIMIT")
    if summary.closure.effective_axiom_count > limits.max_axioms:
        _fail("V2 closure axioms exceed max_axioms", "NATIVE_PUBLICATION_LIMIT")
    rdf_diagnostics = _checked_sum("RDF diagnostic count", rdf_diagnostic_counts)
    owl_diagnostics = 0
    if owl2_dl_report_summary is not None:
        owl_diagnostics = _checked_sum(
            "OWL2-DL diagnostic count",
            (
                owl2_dl_report_summary.structural_issue_count,
                owl2_dl_report_summary.issue_count,
            ),
        )
        if owl2_dl_report_summary.row_count > limits.max_index_rows:
            _fail("V2 OWL2-DL rows exceed max_index_rows", "NATIVE_PUBLICATION_LIMIT")
    total_diagnostics = _checked_sum(
        "V2 total diagnostic count",
        (metadata_diagnostic_count, rdf_diagnostics, owl_diagnostics),
    )
    if total_diagnostics > limits.max_diagnostics:
        _fail("V2 diagnostics exceed max_diagnostics", "NATIVE_PUBLICATION_LIMIT")


def _facade_cardinality_summary_values_v2(
    summary: NativeFacadeCardinalitySummaryV2,
) -> tuple[object, ...]:
    return (
        tuple(
            tuple(getattr(row, item[1]) for item in NATIVE_DOCUMENT_FACADE_CARDINALITIES_FIELDS_V2)
            for row in summary.documents
        ),
        tuple(
            getattr(summary.closure, item[1])
            for item in NATIVE_CLOSURE_FACADE_CARDINALITIES_FIELDS_V2
        ),
    )


def _validate_facade_cardinality_collections_v2(
    summary: NativeFacadeCardinalitySummaryV2,
    collections: Mapping[_FixtureKey, Sequence[bytes]],
    raw_document_collections: Mapping[_FixtureKey, Sequence[bytes]],
) -> None:
    for ordinal, row in enumerate(summary.documents):
        effective_base = (
            NativeFacadeScopeV2.DOCUMENT,
            ordinal,
            NativeSignatureKindV2.ALL,
            True,
        )
        observed = (
            len(
                collections.get(
                    (NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS, *effective_base), ()
                )
            ),
            len(collections.get((NativeFacadeCollectionV2.AXIOMS, *effective_base), ())),
            len(collections.get((NativeFacadeCollectionV2.EXTENSIONS, *effective_base), ())),
            len(collections.get((NativeFacadeCollectionV2.ORIGIN_ENTRIES, *effective_base), ())),
            len(
                raw_document_collections.get(
                    (NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES, *effective_base), ()
                )
            ),
            len(
                collections.get(
                    (NativeFacadeCollectionV2.RDF_UNCONSUMED_TRIPLES, *effective_base), ()
                )
            ),
            len(collections.get((NativeFacadeCollectionV2.RDF_RULE_IDS, *effective_base), ())),
            len(collections.get((NativeFacadeCollectionV2.RDF_DIAGNOSTICS, *effective_base), ())),
        )
        expected = tuple(
            getattr(row, item[1])
            for item in NATIVE_DOCUMENT_FACADE_CARDINALITIES_FIELDS_V2
            if item[1] != "document_key"
        )
        if observed != expected:
            _fail(
                f"facade cardinality summary diverges for document {row.document_key}",
                "NATIVE_PAGE_TOTAL",
            )
    closure_base = (
        NativeFacadeScopeV2.CLOSURE,
        None,
        NativeSignatureKindV2.ALL,
        True,
    )
    observed_closure = tuple(
        len(collections.get((collection, *closure_base), ()))
        for collection in (
            NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS,
            NativeFacadeCollectionV2.AXIOMS,
            NativeFacadeCollectionV2.EXTENSIONS,
            NativeFacadeCollectionV2.ORIGIN_ENTRIES,
        )
    )
    expected_closure = tuple(
        getattr(summary.closure, item[1]) for item in NATIVE_CLOSURE_FACADE_CARDINALITIES_FIELDS_V2
    )
    if observed_closure != expected_closure:
        _fail("facade closure cardinality summary diverges", "NATIVE_PAGE_TOTAL")


def _manifest_hash_v2(domain: str, body: bytes) -> bytes:
    return hashlib.sha256(domain.encode("ascii") + b"\x00" + body).digest()


def _u32_le_v2(value: int) -> bytes:
    _require_nonnegative_u32("V2 manifest u32", value)
    return value.to_bytes(4, "little")


def _u64_le_v2(value: int) -> bytes:
    _require_nonnegative_u64("V2 manifest u64", value)
    return value.to_bytes(8, "little")


def _frame64_v2(value: bytes) -> bytes:
    if type(value) is not bytes:
        raise TypeError("V2 manifest framed value must be exact bytes")
    return _u64_le_v2(len(value)) + value


def _text64_v2(value: str) -> bytes:
    if type(value) is not str:
        raise TypeError("V2 manifest text must be exact str")
    return _frame64_v2(value.encode("utf-8"))


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeSourceMapRowV2:
    digest: bytes
    occurrence: int
    span: SourceSpan | None
    lexical: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _require_digest("source-map row digest", self.digest)
        _require_nonnegative_u64("source-map row occurrence", self.occurrence)
        _require_optional_span_v2(self.span)
        if type(self.lexical) is not tuple:
            raise TypeError("source-map lexical pairs must be an exact tuple")
        lexical = self.lexical
        if len(lexical) >= 2**16:
            raise ValueError("source-map lexical pairs exceed u16")
        previous: bytes | None = None
        for pair in lexical:
            if type(pair) is not tuple or len(pair) != 2:
                raise TypeError("source-map lexical pair must be an exact 2-tuple")
            key, value = pair
            _require_aux_text_v2("source-map lexical key", key, nonempty=True)
            _require_aux_text_v2("source-map lexical value", value)
            encoded = key.encode("utf-8")
            if previous is not None and encoded <= previous:
                raise ValueError("source-map lexical keys must be UTF-8 ascending unique")
            previous = encoded


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeSourcePrefixRowV2:
    prefix: str
    iri: str

    def __post_init__(self) -> None:
        _require_aux_text_v2("source prefix", self.prefix)
        _require_aux_text_v2("source prefix IRI", self.iri, nonempty=True)


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeOriginRowV2:
    digest: bytes
    document_key: str
    occurrence: int
    span: SourceSpan | None

    def __post_init__(self) -> None:
        _require_digest("origin row digest", self.digest)
        object.__setattr__(self, "document_key", _copy_document_key(self.document_key))
        _require_nonnegative_u64("origin row occurrence", self.occurrence)
        _require_optional_span_v2(self.span)


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeRDFReportHeaderRowV2:
    conformant: bool
    consumed_triples: int
    total_triples: int

    def __post_init__(self) -> None:
        if type(self.conformant) is not bool:
            raise TypeError("RDF report conformant must be bool")
        _require_nonnegative_u64("RDF consumed triples", self.consumed_triples)
        _require_nonnegative_u64("RDF total triples", self.total_triples)
        if self.consumed_triples > self.total_triples:
            raise ValueError("RDF consumed triples cannot exceed total triples")


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeRDFTripleRowV2:
    subject: str
    predicate: str
    object: str

    def __post_init__(self) -> None:
        _require_aux_text_v2("RDF triple subject", self.subject, nonempty=True)
        _require_aux_text_v2("RDF triple predicate", self.predicate, nonempty=True)
        _require_aux_text_v2("RDF triple object", self.object, nonempty=True)


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeRDFRuleRowV2:
    rule_id: str

    def __post_init__(self) -> None:
        _require_aux_text_v2("RDF rule id", self.rule_id, nonempty=True)


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeRDFDiagnosticRowV2:
    diagnostic: NativeDiagnosticPublicationV1
    reference_kinds: NativeDiagnosticReferenceKindsV2

    def __post_init__(self) -> None:
        _validate_exact_diagnostic_v2(self.diagnostic)
        if type(self.reference_kinds) is not NativeDiagnosticReferenceKindsV2:
            raise TypeError("RDF diagnostic row requires exact V2 reference kinds")
        _validate_diagnostic_reference_kinds_v2(self.diagnostic, self.reference_kinds)


NativeAuxiliaryRowV2 = (
    NativeSourceMapRowV2
    | NativeSourcePrefixRowV2
    | NativeOriginRowV2
    | NativeRDFReportHeaderRowV2
    | NativeRDFTripleRowV2
    | NativeRDFRuleRowV2
    | NativeRDFDiagnosticRowV2
    | NativeOWL2DLStructuralIssueRowV2
    | NativeOWL2DLIssueRowV2
    | NativeOWL2DLRoleEdgeRowV2
)


def encode_native_auxiliary_row_v2(
    value: NativeAuxiliaryRowV2,
    *,
    max_row_bytes: int,
) -> tuple[NativeFacadeCollectionV2, bytes]:
    """Encode one exact auxiliary record with its collection discriminator."""

    if type(value) not in {
        NativeSourceMapRowV2,
        NativeSourcePrefixRowV2,
        NativeOriginRowV2,
        NativeRDFReportHeaderRowV2,
        NativeRDFTripleRowV2,
        NativeRDFRuleRowV2,
        NativeRDFDiagnosticRowV2,
        NativeOWL2DLStructuralIssueRowV2,
        NativeOWL2DLIssueRowV2,
        NativeOWL2DLRoleEdgeRowV2,
    }:
        raise TypeError("value must be an exact V2 auxiliary row record")
    if type(value) is NativeOWL2DLRoleEdgeRowV2:
        value.__post_init__(None)
    else:
        cast(Any, value).__post_init__()
    if type(value) is NativeSourceMapRowV2:
        selected = value
        body = bytearray(selected.digest)
        body.extend(_encode_u64_v2(selected.occurrence))
        body.extend(_encode_span_v2(selected.span))
        body.extend(_encode_u16_v2(len(selected.lexical)))
        for key, lexical_value in selected.lexical:
            body.extend(_encode_text_v2(key))
            body.extend(_encode_text_v2(lexical_value))
        return _bounded_auxiliary_row_v2(
            NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES, bytes(body), max_row_bytes
        )
    if type(value) is NativeSourcePrefixRowV2:
        selected_prefix = value
        return _bounded_auxiliary_row_v2(
            NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES,
            _encode_text_v2(selected_prefix.prefix) + _encode_text_v2(selected_prefix.iri),
            max_row_bytes,
        )
    if type(value) is NativeOriginRowV2:
        selected_origin = value
        return _bounded_auxiliary_row_v2(
            NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            selected_origin.digest
            + _encode_text_v2(selected_origin.document_key)
            + _encode_u64_v2(selected_origin.occurrence)
            + _encode_span_v2(selected_origin.span),
            max_row_bytes,
        )
    if type(value) is NativeRDFReportHeaderRowV2:
        selected_header = value
        return _bounded_auxiliary_row_v2(
            NativeFacadeCollectionV2.RDF_REPORT_HEADER,
            bytes((int(selected_header.conformant),))
            + _encode_u64_v2(selected_header.consumed_triples)
            + _encode_u64_v2(selected_header.total_triples),
            max_row_bytes,
        )
    if type(value) is NativeRDFTripleRowV2:
        selected_triple = value
        return _bounded_auxiliary_row_v2(
            NativeFacadeCollectionV2.RDF_UNCONSUMED_TRIPLES,
            _encode_text_v2(selected_triple.subject)
            + _encode_text_v2(selected_triple.predicate)
            + _encode_text_v2(selected_triple.object),
            max_row_bytes,
        )
    if type(value) is NativeRDFRuleRowV2:
        selected_rule = value
        return _bounded_auxiliary_row_v2(
            NativeFacadeCollectionV2.RDF_RULE_IDS,
            _encode_text_v2(selected_rule.rule_id),
            max_row_bytes,
        )
    if type(value) is NativeRDFDiagnosticRowV2:
        selected_diagnostic = value
        return _bounded_auxiliary_row_v2(
            NativeFacadeCollectionV2.RDF_DIAGNOSTICS,
            _encode_diagnostic_v2(selected_diagnostic),
            max_row_bytes,
        )
    if type(value) is NativeOWL2DLStructuralIssueRowV2:
        return _bounded_auxiliary_row_v2(
            NativeFacadeCollectionV2.OWL2_DL_STRUCTURAL_ISSUES,
            _encode_owl2_dl_issue_v2(value),
            max_row_bytes,
        )
    if type(value) is NativeOWL2DLIssueRowV2:
        return _bounded_auxiliary_row_v2(
            NativeFacadeCollectionV2.OWL2_DL_ISSUES,
            _encode_owl2_dl_issue_v2(value),
            max_row_bytes,
        )
    if type(value) is NativeOWL2DLRoleEdgeRowV2:
        selected_edge = value
        edge_body = (
            _encode_u32_v2(len(selected_edge.sub_property))
            + selected_edge.sub_property
            + _encode_u32_v2(len(selected_edge.super_property))
            + selected_edge.super_property
        )
        return _bounded_auxiliary_row_v2(
            NativeFacadeCollectionV2.OWL2_DL_ROLE_HIERARCHY,
            edge_body,
            max_row_bytes,
        )
    raise TypeError("value must be an exact V2 auxiliary row record")


def decode_native_auxiliary_row_v2(
    collection: NativeFacadeCollectionV2,
    row: bytes,
    *,
    max_row_bytes: int,
    limits: ParseLimits | None = None,
) -> NativeAuxiliaryRowV2:
    """Decode and exhaustively validate one collection-specific auxiliary row."""

    if type(collection) is not NativeFacadeCollectionV2:
        raise TypeError("collection must be an exact NativeFacadeCollectionV2")
    if collection in _STRUCTURAL_COLLECTIONS:
        raise ValueError("structural collections use canonical-model-v2, not an auxiliary codec")
    if type(row) is not bytes or not row:
        raise TypeError("auxiliary row must be nonempty exact bytes")
    _require_positive_u64_v2("max_row_bytes", max_row_bytes)
    if len(row) > max_row_bytes:
        raise ValueError("auxiliary row exceeds the effective V2 row bound")
    decoder = _AuxDecoderV2(row)
    if collection is NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES:
        digest = decoder.take(32)
        occurrence = decoder.u64()
        span = decoder.span()
        count = decoder.u16()
        lexical = tuple((decoder.text(nonempty=True), decoder.text()) for _ in range(count))
        value: NativeAuxiliaryRowV2 = NativeSourceMapRowV2(
            digest=digest,
            occurrence=occurrence,
            span=span,
            lexical=lexical,
        )
    elif collection is NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES:
        value = NativeSourcePrefixRowV2(prefix=decoder.text(), iri=decoder.text(nonempty=True))
    elif collection is NativeFacadeCollectionV2.ORIGIN_ENTRIES:
        value = NativeOriginRowV2(
            digest=decoder.take(32),
            document_key=decoder.text(nonempty=True),
            occurrence=decoder.u64(),
            span=decoder.span(),
        )
    elif collection is NativeFacadeCollectionV2.RDF_REPORT_HEADER:
        value = NativeRDFReportHeaderRowV2(
            conformant=decoder.boolean(),
            consumed_triples=decoder.u64(),
            total_triples=decoder.u64(),
        )
    elif collection is NativeFacadeCollectionV2.RDF_UNCONSUMED_TRIPLES:
        value = NativeRDFTripleRowV2(
            subject=decoder.text(nonempty=True),
            predicate=decoder.text(nonempty=True),
            object=decoder.text(nonempty=True),
        )
    elif collection is NativeFacadeCollectionV2.RDF_RULE_IDS:
        value = NativeRDFRuleRowV2(rule_id=decoder.text(nonempty=True))
    elif collection is NativeFacadeCollectionV2.RDF_DIAGNOSTICS:
        value = decoder.diagnostic()
    elif collection is NativeFacadeCollectionV2.OWL2_DL_STRUCTURAL_ISSUES:
        code, severity, message, constructor = decoder.owl2_dl_issue()
        value = NativeOWL2DLStructuralIssueRowV2(
            code=code,
            severity=severity,
            message=message,
            constructor=constructor,
        )
    elif collection is NativeFacadeCollectionV2.OWL2_DL_ISSUES:
        code, severity, message, constructor = decoder.owl2_dl_issue()
        value = NativeOWL2DLIssueRowV2(
            code=code,
            severity=severity,
            message=message,
            constructor=constructor,
        )
    elif collection is NativeFacadeCollectionV2.OWL2_DL_ROLE_HIERARCHY:
        value = NativeOWL2DLRoleEdgeRowV2(
            sub_property=decoder.take(decoder.u32()),
            super_property=decoder.take(decoder.u32()),
            _validation_limits=limits,
        )
    else:  # pragma: no cover - exhaustive enum guard
        raise AssertionError(collection)
    decoder.finish()
    return value


class _AuxDecoderV2:
    __slots__ = ("_data", "_offset")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def take(self, count: int) -> bytes:
        end = self._offset + count
        if count < 0 or end > len(self._data):
            raise ValueError("truncated V2 auxiliary row")
        value = self._data[self._offset : end]
        self._offset = end
        return value

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return int.from_bytes(self.take(2), "little")

    def u32(self) -> int:
        return int.from_bytes(self.take(4), "little")

    def u64(self) -> int:
        return int.from_bytes(self.take(8), "little")

    def i64(self) -> int:
        return int.from_bytes(self.take(8), "little", signed=True)

    def boolean(self) -> bool:
        value = self.u8()
        if value not in {0, 1}:
            raise ValueError("V2 auxiliary boolean must be 0 or 1")
        return bool(value)

    def text(self, *, nonempty: bool = False) -> str:
        raw = self.take(self.u32())
        try:
            value = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValueError("V2 auxiliary text is not strict UTF-8") from error
        _require_aux_text_v2("decoded auxiliary text", value, nonempty=nonempty)
        return value

    def optional_text(self) -> str | None:
        return self.text() if self.boolean() else None

    def span(self) -> SourceSpan | None:
        mask = self.u8()
        if mask == 0:
            return None
        if not mask & 0x80 or mask & 0x40:
            raise ValueError("V2 auxiliary span mask is invalid")
        names = (
            "byte_start",
            "byte_end",
            "line_start",
            "column_start",
            "line_end",
            "column_end",
        )
        values: dict[str, int | None] = {}
        for index, name in enumerate(names):
            values[name] = self.u64() if mask & (1 << index) else None
        return SourceSpan(**values)

    def diagnostic(self) -> NativeRDFDiagnosticRowV2:
        code = self.text(nonempty=True)
        severity_tag = self.u8()
        severity = {0: "info", 1: "warning", 2: "error"}.get(severity_tag)
        if severity is None:
            raise ValueError("V2 RDF diagnostic severity tag is invalid")
        message = self.text(nonempty=True)
        document_kind = self.reference_kind(optional=True)
        document_iri = None if document_kind is None else self.text(nonempty=True)
        span = self.span()
        chain_count = self.u16()
        if chain_count > _bound_v2("max_diagnostic_import_chain"):
            raise ValueError("V2 RDF diagnostic import chain exceeds its bound")
        chain_kinds: list[NativeDiagnosticReferenceKindV2] = []
        chain: list[str] = []
        for _ in range(chain_count):
            chain_kind = self.reference_kind(optional=False)
            chain_kinds.append(cast(NativeDiagnosticReferenceKindV2, chain_kind))
            chain.append(self.text(nonempty=True))
        detail_count = self.u16()
        if detail_count > _bound_v2("max_diagnostic_details"):
            raise ValueError("V2 RDF diagnostic details exceed their bound")
        details: list[tuple[str, str | int | bool]] = []
        for _ in range(detail_count):
            key = self.text(nonempty=True)
            tag = self.u8()
            if tag == 0:
                scalar: str | int | bool = self.text()
            elif tag == 1:
                scalar = self.i64()
            elif tag == 2:
                scalar = self.boolean()
            else:
                raise ValueError("V2 RDF diagnostic scalar tag is invalid")
            details.append((key, scalar))
        return NativeRDFDiagnosticRowV2(
            diagnostic=NativeDiagnosticPublicationV1(
                code=code,
                severity=severity,
                message=message,
                document_iri=document_iri,
                byte_start=None if span is None else span.byte_start,
                byte_end=None if span is None else span.byte_end,
                line_start=None if span is None else span.line_start,
                column_start=None if span is None else span.column_start,
                line_end=None if span is None else span.line_end,
                column_end=None if span is None else span.column_end,
                import_chain=tuple(chain),
                details=tuple(details),
            ),
            reference_kinds=NativeDiagnosticReferenceKindsV2(
                document_reference_kind=document_kind,
                import_chain_kinds=tuple(chain_kinds),
            ),
        )

    def reference_kind(
        self,
        *,
        optional: bool,
    ) -> NativeDiagnosticReferenceKindV2 | None:
        tag = self.u8()
        if optional and tag == 0:
            return None
        kind = {
            1: NativeDiagnosticReferenceKindV2.IRI,
            2: NativeDiagnosticReferenceKindV2.TEXT,
        }.get(tag)
        if kind is None:
            raise ValueError("V2 diagnostic reference kind tag is invalid")
        return kind

    def owl2_dl_issue(
        self,
    ) -> tuple[str, ValidationSeverity, str, str | None]:
        code = self.text(nonempty=True)
        severity_tag = self.u8()
        severity = {
            0: ValidationSeverity.INFO,
            1: ValidationSeverity.WARNING,
            2: ValidationSeverity.ERROR,
        }.get(severity_tag)
        if severity is None:
            raise ValueError("V2 OWL2-DL issue severity tag is invalid")
        return (
            code,
            severity,
            self.text(nonempty=True),
            self.optional_text(),
        )

    def finish(self) -> None:
        if self._offset != len(self._data):
            raise ValueError("V2 auxiliary row has trailing bytes")


def _encode_diagnostic_v2(value: NativeRDFDiagnosticRowV2) -> bytes:
    diagnostic = value.diagnostic
    reference_kinds = value.reference_kinds
    severity = {"info": 0, "warning": 1, "error": 2}[diagnostic.severity]
    span = _diagnostic_span_v2(diagnostic)
    body = bytearray(_encode_text_v2(diagnostic.code))
    body.append(severity)
    body.extend(_encode_text_v2(diagnostic.message))
    body.extend(_encode_diagnostic_reference_kind_v2(reference_kinds.document_reference_kind))
    if diagnostic.document_iri is not None:
        body.extend(_encode_text_v2(diagnostic.document_iri))
    body.extend(_encode_span_v2(span))
    body.extend(_encode_u16_v2(len(diagnostic.import_chain)))
    for kind, item in zip(
        reference_kinds.import_chain_kinds,
        diagnostic.import_chain,
        strict=True,
    ):
        body.extend(_encode_diagnostic_reference_kind_v2(kind))
        body.extend(_encode_text_v2(item))
    body.extend(_encode_u16_v2(len(diagnostic.details)))
    for key, scalar in diagnostic.details:
        body.extend(_encode_text_v2(key))
        if isinstance(scalar, str):
            body.append(0)
            body.extend(_encode_text_v2(scalar))
        elif isinstance(scalar, bool):
            body.extend((2, int(scalar)))
        else:
            body.append(1)
            body.extend(int(scalar).to_bytes(8, "little", signed=True))
    return bytes(body)


def _encode_diagnostic_reference_kind_v2(
    value: NativeDiagnosticReferenceKindV2 | None,
) -> bytes:
    return bytes(
        (
            {
                None: 0,
                NativeDiagnosticReferenceKindV2.IRI: 1,
                NativeDiagnosticReferenceKindV2.TEXT: 2,
            }[value],
        )
    )


def _validate_diagnostic_reference_kinds_v2(
    diagnostic: NativeDiagnosticPublicationV1,
    reference_kinds: NativeDiagnosticReferenceKindsV2,
) -> None:
    if (diagnostic.document_iri is None) != (reference_kinds.document_reference_kind is None):
        raise ValueError("diagnostic document reference kind presence is not aligned")
    if len(diagnostic.import_chain) != len(reference_kinds.import_chain_kinds):
        raise ValueError("diagnostic import-chain reference kinds are not aligned")


def _encode_owl2_dl_issue_v2(
    value: NativeOWL2DLIssueRowV2 | NativeOWL2DLStructuralIssueRowV2,
) -> bytes:
    severity = {
        ValidationSeverity.INFO: 0,
        ValidationSeverity.WARNING: 1,
        ValidationSeverity.ERROR: 2,
    }[value.severity]
    return (
        _encode_text_v2(value.code)
        + bytes((severity,))
        + _encode_text_v2(value.message)
        + _encode_optional_text_v2(value.constructor)
    )


def _diagnostic_span_v2(value: NativeDiagnosticPublicationV1) -> SourceSpan | None:
    coordinates = (
        value.byte_start,
        value.byte_end,
        value.line_start,
        value.column_start,
        value.line_end,
        value.column_end,
    )
    return None if all(item is None for item in coordinates) else SourceSpan(*coordinates)


def _encode_span_v2(value: SourceSpan | None) -> bytes:
    if value is None:
        return b"\x00"
    _require_optional_span_v2(value)
    coordinates = (
        value.byte_start,
        value.byte_end,
        value.line_start,
        value.column_start,
        value.line_end,
        value.column_end,
    )
    mask = 0x80
    body = bytearray()
    for index, coordinate in enumerate(coordinates):
        if coordinate is not None:
            mask |= 1 << index
            body.extend(_encode_u64_v2(coordinate))
    return bytes((mask,)) + bytes(body)


def _encode_optional_text_v2(value: str | None) -> bytes:
    return b"\x00" if value is None else b"\x01" + _encode_text_v2(value)


def _encode_text_v2(value: str) -> bytes:
    _require_aux_text_v2("auxiliary text", value)
    raw = value.encode("utf-8")
    if len(raw) >= 2**32:
        raise ValueError("V2 auxiliary text exceeds u32")
    return len(raw).to_bytes(4, "little") + raw


def _encode_u16_v2(value: int) -> bytes:
    if type(value) is not int:
        raise TypeError("V2 auxiliary count must be an exact int")
    if not 0 <= value < 2**16:
        raise ValueError("V2 auxiliary count must fit u16")
    return value.to_bytes(2, "little")


def _encode_u32_v2(value: int) -> bytes:
    _require_nonnegative_u32("V2 auxiliary u32", value)
    return value.to_bytes(4, "little")


def _encode_u64_v2(value: int) -> bytes:
    _require_nonnegative_u64("V2 auxiliary u64", value)
    return value.to_bytes(8, "little")


def _require_aux_text_v2(name: str, value: object, *, nonempty: bool = False) -> None:
    if type(value) is not str or (nonempty and not value):
        raise TypeError(f"{name} must be {'nonempty ' if nonempty else ''}str")


def _bounded_auxiliary_row_v2(
    collection: NativeFacadeCollectionV2,
    row: bytes,
    max_row_bytes: int,
) -> tuple[NativeFacadeCollectionV2, bytes]:
    _require_positive_u64_v2("max_row_bytes", max_row_bytes)
    if len(row) > max_row_bytes:
        raise ValueError("encoded auxiliary row exceeds the effective V2 row bound")
    return collection, row


def _require_positive_u64_v2(name: str, value: object) -> None:
    _require_nonnegative_u64(name, value)
    if cast(int, value) == 0:
        raise ValueError(f"{name} must be positive")


def _require_optional_span_v2(value: object) -> None:
    if value is None:
        return
    if type(value) is not SourceSpan:
        raise TypeError("auxiliary span must be an exact SourceSpan or None")
    span = value
    for name in ("byte_start", "byte_end"):
        _require_optional_u64_v2(f"SourceSpan.{name}", getattr(span, name))
    for name in ("line_start", "column_start", "line_end", "column_end"):
        _require_optional_positive_u64_v2(f"SourceSpan.{name}", getattr(span, name))
    if (
        span.byte_start is not None
        and span.byte_end is not None
        and span.byte_end < span.byte_start
    ):
        raise ValueError("SourceSpan byte_end must not precede byte_start")
    if span.line_start is not None and span.line_end is not None:
        start_column = span.column_start or 1
        end_column = span.column_end or 1
        if (span.line_end, end_column) < (span.line_start, start_column):
            raise ValueError("SourceSpan text end must not precede text start")


def _is_builtin_entity_v2(value: Entity) -> bool:
    # This is the single canonical builtin ledger used by the public signature
    # indexes.  Import lazily so the handoff contract does not create an index
    # construction dependency at module import time.
    from pyowl_core.index.signature import _is_builtin

    return _is_builtin(value)


def _validate_collection_capability_v2(
    attestation: NativeSnapshotAttestationV2,
    collection: NativeFacadeCollectionV2,
) -> None:
    if collection in _OWL2_DL_COLLECTIONS_V2 and attestation.owl2_dl_report_summary is None:
        _fail(
            f"V2 facade collection {collection.value} has no validated report",
            "NATIVE_PUBLICATION_CAPABILITY",
        )
    required = _OPTIONAL_COLLECTION_CAPABILITIES_V2.get(collection)
    if required is not None and not attestation.capability_bits & required:
        _fail(
            f"V2 facade collection {collection.value} is not retained by this publication",
            "NATIVE_PUBLICATION_CAPABILITY",
        )


def _validate_page_coordinates(
    collection: object,
    scope: object,
    document_ordinal: object,
    signature_kind: object,
    include_builtins: object,
    digest_filter: object = None,
) -> None:
    if type(collection) is not NativeFacadeCollectionV2:
        raise TypeError("collection must be an exact NativeFacadeCollectionV2")
    if type(scope) is not NativeFacadeScopeV2:
        raise TypeError("scope must be an exact NativeFacadeScopeV2")
    selected_collection = collection
    selected_scope = scope
    if selected_scope is NativeFacadeScopeV2.DOCUMENT:
        _require_nonnegative_u64("document_ordinal", document_ordinal)
    elif document_ordinal is not None:
        raise ValueError("closure page requests require document_ordinal=None")
    if selected_collection in _DOCUMENT_ONLY_COLLECTIONS and (
        selected_scope is not NativeFacadeScopeV2.DOCUMENT
    ):
        raise ValueError(f"{selected_collection.value} supports document scope only")
    if selected_collection in _CLOSURE_ONLY_COLLECTIONS_V2 and (
        selected_scope is not NativeFacadeScopeV2.CLOSURE
    ):
        raise ValueError(f"{selected_collection.value} supports closure scope only")
    if type(signature_kind) is not NativeSignatureKindV2:
        raise TypeError("signature_kind must be an exact NativeSignatureKindV2")
    if type(include_builtins) is not bool:
        raise TypeError("include_builtins must be an exact bool")
    if selected_collection is not NativeFacadeCollectionV2.SIGNATURE and (
        signature_kind is not NativeSignatureKindV2.ALL or include_builtins is not True
    ):
        raise ValueError("non-signature pages require signature_kind=ALL and builtins included")
    if digest_filter is not None:
        _require_digest("digest_filter", digest_filter)
        if selected_collection not in {
            NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES,
            NativeFacadeCollectionV2.ORIGIN_ENTRIES,
        }:
            raise ValueError("digest_filter is supported only for source-map and origin rows")


def _validate_page_rows_v2(
    collection: NativeFacadeCollectionV2,
    rows: tuple[bytes, ...],
    total_count: int,
    max_row_bytes: int,
    signature_kind: NativeSignatureKindV2,
    include_builtins: bool,
    digest_filter: bytes | None = None,
    limits: ParseLimits | None = None,
    *,
    raw_document_owner: bool = False,
) -> tuple[object, ...]:
    if type(raw_document_owner) is not bool:
        raise TypeError("raw_document_owner must be an exact bool")
    if collection in _STRUCTURAL_COLLECTIONS:
        if any(left >= right for left, right in pairwise(rows)):
            raise ValueError("structural facade rows must be canonical ascending unique")
        decoded_structural: list[object] = []
        for row in rows:
            try:
                value = decode_canonical(row, limits=limits)
            except Exception as error:
                raise ValueError("structural facade row is not canonical-model-v2") from error
            if canonical_bytes(value, limits=limits) != row:
                raise ValueError("structural facade row is not in canonical form")
            if collection is NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS:
                valid = isinstance(value, Annotation)
            elif collection is NativeFacadeCollectionV2.AXIOMS:
                valid = isinstance(value, AxiomNode)
            elif collection is NativeFacadeCollectionV2.SIGNATURE:
                valid = (
                    isinstance(value, Entity)
                    and (
                        signature_kind is NativeSignatureKindV2.ALL
                        or value.kind.value == signature_kind.value
                    )
                    and (include_builtins or not _is_builtin_entity_v2(value))
                )
            elif collection is NativeFacadeCollectionV2.EXTENSIONS:
                valid = isinstance(value, SWRLRule)
            else:
                valid = isinstance(value, (ObjectProperty, ObjectInverseOf))
            if not valid:
                raise ValueError(f"row has the wrong category for {collection.value}")
            decoded_structural.append(value)
        return tuple(decoded_structural)
    decoded = tuple(
        decode_native_auxiliary_row_v2(
            collection,
            row,
            max_row_bytes=max_row_bytes,
            limits=limits,
        )
        for row in rows
    )
    if collection is NativeFacadeCollectionV2.RDF_REPORT_HEADER and total_count > 1:
        raise ValueError("RDF report header collection has at most one row")
    if digest_filter is not None:
        for decoded_value in decoded:
            if (
                cast(
                    NativeSourceMapRowV2 | NativeOriginRowV2,
                    decoded_value,
                ).digest
                != digest_filter
            ):
                raise ValueError("digest-filtered page contains a row from another digest group")
    if collection is NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES or (
        collection is NativeFacadeCollectionV2.ORIGIN_ENTRIES and raw_document_owner
    ):
        digests = tuple(
            cast(NativeSourceMapRowV2 | NativeOriginRowV2, value).digest for value in decoded
        )
        if any(left > right for left, right in pairwise(digests)):
            raise ValueError(f"{collection.value} digest groups are not ascending")
    elif collection is NativeFacadeCollectionV2.ORIGIN_ENTRIES:
        origin_keys = tuple(
            _auxiliary_order_key_v2(collection, value, encoded)
            for value, encoded in zip(decoded, rows, strict=True)
        )
        if any(left >= right for left, right in pairwise(origin_keys)):
            raise ValueError("effective origin rows are not canonically ascending unique")
    elif collection in {
        NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES,
        NativeFacadeCollectionV2.RDF_RULE_IDS,
        NativeFacadeCollectionV2.OWL2_DL_ROLE_HIERARCHY,
    }:
        keys = tuple(
            _auxiliary_order_key_v2(collection, value, encoded)
            for value, encoded in zip(decoded, rows, strict=True)
        )
        if any(left >= right for left, right in pairwise(keys)):
            raise ValueError(f"{collection.value} rows are not ascending unique")
    return cast(tuple[object, ...], decoded)


def _auxiliary_order_key_v2(
    collection: NativeFacadeCollectionV2,
    value: NativeAuxiliaryRowV2,
    encoded: bytes,
) -> tuple[object, ...]:
    if collection is NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES:
        row = cast(NativeSourceMapRowV2, value)
        return (row.digest,)
    if collection is NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES:
        prefix = cast(NativeSourcePrefixRowV2, value)
        return (prefix.prefix.encode("utf-8"),)
    if collection is NativeFacadeCollectionV2.ORIGIN_ENTRIES:
        origin = cast(NativeOriginRowV2, value)
        return _canonical_origin_order_key_v2(origin, encoded)
    if collection is NativeFacadeCollectionV2.RDF_RULE_IDS:
        rule = cast(NativeRDFRuleRowV2, value)
        return (rule.rule_id.encode("utf-8"), encoded)
    if collection is NativeFacadeCollectionV2.OWL2_DL_ROLE_HIERARCHY:
        edge = cast(NativeOWL2DLRoleEdgeRowV2, value)
        return (edge.sub_property, edge.super_property, encoded)
    raise AssertionError(collection)


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeFacadePageRequestV2:
    collection: NativeFacadeCollectionV2
    scope: NativeFacadeScopeV2
    document_ordinal: int | None
    start: int
    max_rows: int
    max_bytes: int
    max_row_bytes: int
    signature_kind: NativeSignatureKindV2 = NativeSignatureKindV2.ALL
    include_builtins: bool = True
    digest_filter: bytes | None = None

    def __post_init__(self) -> None:
        _validate_page_coordinates(
            self.collection,
            self.scope,
            self.document_ordinal,
            self.signature_kind,
            self.include_builtins,
            self.digest_filter,
        )
        _require_nonnegative_u64("start", self.start)
        _require_nonnegative_u32("max_rows", self.max_rows)
        if not 1 <= self.max_rows <= _bound_v2("max_facade_page_rows"):
            raise ValueError("max_rows exceeds the V2 facade page bound")
        _require_nonnegative_u64("max_bytes", self.max_bytes)
        if not 1 <= self.max_bytes <= _bound_v2("max_facade_page_bytes"):
            raise ValueError("max_bytes exceeds the V2 facade page bound")
        _require_positive_u64_v2("max_row_bytes", self.max_row_bytes)


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeFacadePageV2:
    collection: NativeFacadeCollectionV2
    scope: NativeFacadeScopeV2
    document_ordinal: int | None
    start: int
    max_rows: int
    max_bytes: int
    max_row_bytes: int
    signature_kind: NativeSignatureKindV2
    include_builtins: bool
    digest_filter: bytes | None = None
    total_count: int
    next_cursor: int | None
    terminal: bool
    page_bytes: int
    rows: tuple[bytes, ...]
    _validation_limits: InitVar[ParseLimits | None] = None
    _raw_document_owner: InitVar[bool] = False
    _validated_rows: tuple[object, ...] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(
        self,
        _validation_limits: ParseLimits | None,
        _raw_document_owner: bool,
    ) -> None:
        NativeFacadePageRequestV2(
            collection=self.collection,
            scope=self.scope,
            document_ordinal=self.document_ordinal,
            start=self.start,
            max_rows=self.max_rows,
            max_bytes=self.max_bytes,
            max_row_bytes=self.max_row_bytes,
            signature_kind=self.signature_kind,
            include_builtins=self.include_builtins,
            digest_filter=self.digest_filter,
        )
        _require_nonnegative_u64("total_count", self.total_count)
        if type(self.terminal) is not bool:
            raise TypeError("terminal must be bool")
        _require_nonnegative_u64("page_bytes", self.page_bytes)
        if type(self.rows) is not tuple:
            raise TypeError("facade page rows must be an exact tuple")
        rows = self.rows
        if len(rows) > self.max_rows:
            raise ValueError("facade page rows exceed max_rows")
        for row in rows:
            if type(row) is not bytes or not row:
                raise TypeError("facade page rows must contain nonempty exact bytes")
            if len(row) > self.max_row_bytes:
                raise ValueError("facade page row exceeds the publication row bound")
        observed_bytes = sum(len(row) for row in rows)
        _require_nonnegative_u64("observed page bytes", observed_bytes)
        if observed_bytes != self.page_bytes:
            raise ValueError("facade page page_bytes does not match rows")
        if observed_bytes > self.max_bytes and len(rows) != 1:
            raise ValueError("only one oversized first row may exceed max_bytes")
        validated_rows = _validate_page_rows_v2(
            self.collection,
            rows,
            self.total_count,
            self.max_row_bytes,
            self.signature_kind,
            self.include_builtins,
            self.digest_filter,
            _validation_limits,
            raw_document_owner=_raw_document_owner,
        )
        object.__setattr__(self, "_validated_rows", validated_rows)
        end = self.start + len(rows)
        _require_nonnegative_u64("page end cursor", end)
        if self.start > self.total_count or end > self.total_count:
            raise ValueError("facade page coordinates exceed total_count")
        if self.terminal:
            if self.next_cursor is not None or end != self.total_count:
                raise ValueError("terminal page must end at total_count with no next cursor")
        else:
            if not rows or self.next_cursor != end or end >= self.total_count:
                raise ValueError("nonterminal page must make progress to its exact next cursor")
        object.__setattr__(self, "rows", rows)

    def _validated_rows_v2(self) -> tuple[object, ...]:
        """Return validation-decoded rows for exactly-once facade materialization."""

        return self._validated_rows


def _unchecked_owner_page_v2(
    request: NativeFacadePageRequestV2,
    *,
    total_count: int,
    next_cursor: int | None,
    terminal: bool,
    rows: tuple[bytes, ...],
) -> NativeFacadePageV2:
    """Build an owner response whose validation occurs once at the facade boundary."""

    values: dict[str, object] = {
        item.name: getattr(request, item.name)
        for item in fields(NativeFacadePageRequestV2)
        if item.init
    }
    values.update(
        total_count=total_count,
        next_cursor=next_cursor,
        terminal=terminal,
        page_bytes=sum(len(row) for row in rows),
        rows=rows,
    )
    page = object.__new__(NativeFacadePageV2)
    for item in fields(NativeFacadePageV2):
        object.__setattr__(page, item.name, values[item.name] if item.init else ())
    return page


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeFacadeContainsRequestV2:
    collection: NativeFacadeCollectionV2
    scope: NativeFacadeScopeV2
    document_ordinal: int | None
    canonical: bytes
    max_row_bytes: int
    _validation_limits: InitVar[ParseLimits | None] = None
    _validated_axiom: AxiomNode = field(init=False, repr=False, compare=False)
    _validated_canonical: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self, _validation_limits: ParseLimits | None) -> None:
        if self.collection is not NativeFacadeCollectionV2.AXIOMS:
            raise ValueError("native contains requests are explicitly axioms-only")
        _validate_page_coordinates(
            self.collection,
            self.scope,
            self.document_ordinal,
            NativeSignatureKindV2.ALL,
            True,
        )
        if type(self.canonical) is not bytes or not self.canonical:
            raise TypeError("canonical must be nonempty exact bytes")
        _require_positive_u64_v2("max_row_bytes", self.max_row_bytes)
        if len(self.canonical) > self.max_row_bytes:
            raise ValueError("contains canonical bytes exceed the publication row bound")
        try:
            value = decode_canonical(self.canonical, limits=_validation_limits)
        except Exception as error:
            raise ValueError("contains canonical bytes are not a valid model row") from error
        if not isinstance(value, AxiomNode):
            raise ValueError("contains canonical bytes must encode an OWL axiom")
        if canonical_bytes(value, limits=_validation_limits) != self.canonical:
            raise ValueError("contains canonical bytes are not in canonical form")
        object.__setattr__(self, "_validated_axiom", value)
        object.__setattr__(self, "_validated_canonical", self.canonical)

    def _validated_axiom_v2(self) -> AxiomNode:
        """Return the validation-decoded axiom for exactly-once contains handling."""

        return self._validated_axiom

    def _validated_canonical_v2(self) -> bytes:
        """Return the exact bytes bound to the validation-decoded axiom."""

        return self._validated_canonical


def _unchecked_contains_request_v2(
    *,
    collection: NativeFacadeCollectionV2,
    scope: NativeFacadeScopeV2,
    document_ordinal: int | None,
    canonical: bytes,
    max_row_bytes: int,
) -> NativeFacadeContainsRequestV2:
    """Build an internal request whose sole decode occurs at the owner boundary."""

    values: dict[str, object] = {
        "collection": collection,
        "scope": scope,
        "document_ordinal": document_ordinal,
        "canonical": canonical,
        "max_row_bytes": max_row_bytes,
    }
    request = object.__new__(NativeFacadeContainsRequestV2)
    for item in fields(NativeFacadeContainsRequestV2):
        object.__setattr__(request, item.name, values[item.name] if item.init else ())
    return request


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeFacadeCountersV2:
    component_node_requests: int = 0
    component_node_hits: int = 0
    string_requests: int = 0
    string_hits: int = 0
    byte_string_requests: int = 0
    byte_string_hits: int = 0
    integer_requests: int = 0
    integer_hits: int = 0
    component_sequence_requests: int = 0
    component_sequence_hits: int = 0
    canonical_input_rows: int = 0
    canonical_input_bytes: int = 0
    unique_component_nodes: int = 0
    unique_strings: int = 0
    unique_byte_strings: int = 0
    unique_integers: int = 0
    unique_component_sequences: int = 0
    retained_document_tables: int = 0
    retained_annotation_rows: int = 0
    retained_axiom_rows: int = 0
    retained_extension_rows: int = 0
    retained_source_map_rows: int = 0
    retained_source_prefix_rows: int = 0
    retained_origin_rows: int = 0
    retained_rdf_header_rows: int = 0
    retained_rdf_triple_rows: int = 0
    retained_rdf_rule_rows: int = 0
    retained_rdf_diagnostic_rows: int = 0
    retained_owl2_dl_structural_issue_rows: int = 0
    retained_owl2_dl_issue_rows: int = 0
    retained_owl2_dl_role_property_rows: int = 0
    retained_owl2_dl_role_hierarchy_rows: int = 0
    retained_owl2_dl_role_composite_rows: int = 0
    retained_owl2_dl_role_non_simple_rows: int = 0
    retained_component_bytes: int = 0
    retained_root_bytes: int = 0
    retained_source_bytes: int = 0
    retained_origin_bytes: int = 0
    retained_rdf_bytes: int = 0
    retained_owl2_dl_bytes: int = 0
    retained_index_bytes: int = 0
    retained_metadata_bytes: int = 0
    retained_owner_bytes: int = 0
    peak_builder_live_bytes: int = 0
    peak_freeze_live_bytes: int = 0
    peak_facade_cache_bytes: int = 0
    publication_metadata_records_emitted: int = 0
    publication_structural_rows_copied: int = 0
    publication_structural_bytes_copied: int = 0
    page_requests: int = 0
    pages_returned: int = 0
    rows_emitted: int = 0
    payload_bytes_copied: int = 0
    canonical_payload_bytes_copied: int = 0
    auxiliary_payload_bytes_copied: int = 0
    contains_requests: int = 0
    contains_hits: int = 0
    ontology_annotation_rows_emitted: int = 0
    axiom_rows_emitted: int = 0
    extension_rows_emitted: int = 0
    signature_rows_emitted: int = 0
    source_map_rows_emitted: int = 0
    source_prefix_rows_emitted: int = 0
    origin_rows_emitted: int = 0
    rdf_header_rows_emitted: int = 0
    rdf_triple_rows_emitted: int = 0
    rdf_rule_rows_emitted: int = 0
    rdf_diagnostic_rows_emitted: int = 0
    owl2_dl_structural_issue_rows_emitted: int = 0
    owl2_dl_issue_rows_emitted: int = 0
    owl2_dl_role_property_rows_emitted: int = 0
    owl2_dl_role_hierarchy_rows_emitted: int = 0
    owl2_dl_role_composite_rows_emitted: int = 0
    owl2_dl_role_non_simple_rows_emitted: int = 0
    canonical_encode_requests: int = 0
    canonical_encode_cache_hits: int = 0
    facade_cache_hits: int = 0
    facade_cache_misses: int = 0
    facade_cache_evictions: int = 0
    close_requests: int = 0
    close_transitions: int = 0
    fork_reinitializations: int = 0
    facade_cache_current_entries: int = 0
    facade_cache_current_bytes: int = 0
    parser_bytes: int = 0
    encoded_view_requests: int = 0
    wire_encode_requests: int = 0
    wire_decode_requests: int = 0
    base_flatten_requests: int = 0

    def __post_init__(self) -> None:
        for item in fields(self):
            _require_nonnegative_u64(f"counters.{item.name}", getattr(self, item.name))
        if self.pages_returned > self.page_requests:
            raise ValueError("pages_returned cannot exceed page_requests")
        if self.contains_hits > self.contains_requests:
            raise ValueError("contains_hits cannot exceed contains_requests")
        if self.close_transitions > self.close_requests:
            raise ValueError("close_transitions cannot exceed close_requests")
        if self.canonical_encode_cache_hits > self.canonical_encode_requests:
            raise ValueError("canonical encode cache hits cannot exceed requests")
        for requests, hits, unique, name in (
            (
                self.component_node_requests,
                self.component_node_hits,
                self.unique_component_nodes,
                "component nodes",
            ),
            (self.string_requests, self.string_hits, self.unique_strings, "strings"),
            (
                self.byte_string_requests,
                self.byte_string_hits,
                self.unique_byte_strings,
                "byte strings",
            ),
            (self.integer_requests, self.integer_hits, self.unique_integers, "integers"),
            (
                self.component_sequence_requests,
                self.component_sequence_hits,
                self.unique_component_sequences,
                "component sequences",
            ),
        ):
            if requests != hits + unique:
                raise ValueError(f"{name} requests must equal hits + unique")
        emitted_rows = (
            self.ontology_annotation_rows_emitted
            + self.axiom_rows_emitted
            + self.extension_rows_emitted
            + self.signature_rows_emitted
            + self.source_map_rows_emitted
            + self.source_prefix_rows_emitted
            + self.origin_rows_emitted
            + self.rdf_header_rows_emitted
            + self.rdf_triple_rows_emitted
            + self.rdf_rule_rows_emitted
            + self.rdf_diagnostic_rows_emitted
            + self.owl2_dl_structural_issue_rows_emitted
            + self.owl2_dl_issue_rows_emitted
            + self.owl2_dl_role_property_rows_emitted
            + self.owl2_dl_role_hierarchy_rows_emitted
            + self.owl2_dl_role_composite_rows_emitted
            + self.owl2_dl_role_non_simple_rows_emitted
        )
        if self.rows_emitted != emitted_rows:
            raise ValueError("rows_emitted diverges from per-collection emitted counters")
        if self.payload_bytes_copied != (
            self.canonical_payload_bytes_copied + self.auxiliary_payload_bytes_copied
        ):
            raise ValueError("payload bytes diverge from canonical + auxiliary copies")
        retained = (
            self.retained_component_bytes
            + self.retained_root_bytes
            + self.retained_source_bytes
            + self.retained_origin_bytes
            + self.retained_rdf_bytes
            + self.retained_owl2_dl_bytes
            + self.retained_index_bytes
            + self.retained_metadata_bytes
        )
        if self.retained_owner_bytes != retained:
            raise ValueError("retained_owner_bytes diverges from disjoint retained byte gauges")


@dataclass(frozen=True, slots=True, kw_only=True)
class NativePythonFacadeCountersV2:
    publication_objects: int = 0
    model_rows_materialized: int = 0
    auxiliary_rows_decoded: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    cache_evictions: int = 0
    cache_current_entries: int = 0
    cache_current_bytes: int = 0
    cache_peak_bytes: int = 0

    def __post_init__(self) -> None:
        for item in fields(self):
            _require_nonnegative_u64(f"python_counters.{item.name}", getattr(self, item.name))
        if self.cache_current_bytes > self.cache_peak_bytes:
            raise ValueError("Python cache current bytes cannot exceed its peak")


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeSnapshotAttestationV2:
    version: int
    ledger_sha256: bytes
    metadata_manifest_sha256: bytes
    facade_access_schema_sha256: bytes
    auxiliary_codec_schema_sha256: bytes
    root_table_sha256: bytes
    effective_root_table_sha256: bytes
    fingerprint_inputs_sha256: bytes
    source_manifest_sha256: bytes
    provenance_manifest_sha256: bytes
    effective_origin_manifest_sha256: bytes
    diagnostics_manifest_sha256: bytes
    diagnostic_reference_kinds_sha256: bytes
    facade_cardinality_summary_sha256: bytes
    load_options_sha256: bytes
    report_sha256: bytes
    max_facade_row_bytes: int
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
    owl2_dl_report_summary: NativeOWL2DLReportSummaryV2 | None
    owl2_dl_validated: bool
    owl2_dl_conforms: bool | None
    owl2_dl_report_sha256: bytes | None

    def __post_init__(self) -> None:
        _require_nonnegative_u32("attestation.version", self.version)
        if self.version != NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2:
            raise ValueError("attestation publication version is unsupported")
        for name in (
            "ledger_sha256",
            "metadata_manifest_sha256",
            "facade_access_schema_sha256",
            "auxiliary_codec_schema_sha256",
            "root_table_sha256",
            "effective_root_table_sha256",
            "fingerprint_inputs_sha256",
            "source_manifest_sha256",
            "provenance_manifest_sha256",
            "effective_origin_manifest_sha256",
            "diagnostics_manifest_sha256",
            "diagnostic_reference_kinds_sha256",
            "facade_cardinality_summary_sha256",
            "load_options_sha256",
            "report_sha256",
        ):
            _require_digest(f"attestation.{name}", getattr(self, name))
        if self.ledger_sha256 != NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2:
            raise ValueError("attestation ledger digest does not match V2")
        if self.facade_access_schema_sha256 != NATIVE_FACADE_ACCESS_SCHEMA_SHA256_V2:
            raise ValueError("attestation facade access schema digest does not match V2")
        if self.auxiliary_codec_schema_sha256 != NATIVE_AUXILIARY_CODEC_SCHEMA_SHA256_V2:
            raise ValueError("attestation auxiliary codec schema digest does not match V2")
        _require_positive_u64_v2("attestation.max_facade_row_bytes", self.max_facade_row_bytes)
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
        if self.api_version != NATIVE_ACTIVE_API_VERSION_V2:
            raise ValueError("attestation API version must be (0, 2)")
        if self.model_schema != NATIVE_ACTIVE_MODEL_SCHEMA_V2:
            raise ValueError("attestation model schema must be 2")
        _require_exact_text_v2("attestation.backend", self.backend)
        if self.backend != "native":
            raise ValueError("attestation backend must be native")
        object.__setattr__(self, "root_document_key", _copy_document_key(self.root_document_key))
        if type(self.owl2_dl_validated) is not bool:
            raise TypeError("attestation owl2_dl_validated must be bool")
        if self.owl2_dl_conforms is not None and type(self.owl2_dl_conforms) is not bool:
            raise TypeError("attestation owl2_dl_conforms must be bool or None")
        if self.owl2_dl_validated:
            if type(self.owl2_dl_report_summary) is not NativeOWL2DLReportSummaryV2:
                raise ValueError("validated OWL2-DL attestation requires an exact report summary")
            if self.owl2_dl_conforms is None or self.owl2_dl_report_sha256 is None:
                raise ValueError("validated OWL2-DL attestation requires result metadata")
            if self.owl2_dl_conforms is not True:
                raise ValueError("successful V2 publication requires OWL2-DL conformance")
            summary = self.owl2_dl_report_summary
            if not summary.structural_complete or not summary.report_complete:
                raise ValueError("successful V2 publication requires a complete OWL2-DL report")
            _require_digest("attestation.owl2_dl_report_sha256", self.owl2_dl_report_sha256)
        elif (
            self.owl2_dl_report_summary is not None
            or self.owl2_dl_conforms is not None
            or self.owl2_dl_report_sha256 is not None
        ):
            raise ValueError("unvalidated OWL2-DL attestation cannot publish report metadata")

    @property
    def digest(self) -> bytes:
        return hashlib.sha256(native_snapshot_attestation_bytes_v2(self)).digest()


class _GeneratedHandleLifecycleV2:
    __slots__ = ("closed", "lock", "pid")

    def __init__(self) -> None:
        self.closed = False
        self.lock = threading.RLock()
        self.pid = os.getpid()

    def reinitialize_after_fork(self) -> bool:
        current = os.getpid()
        if current == self.pid:
            return False
        self.pid = current
        self.lock = threading.RLock()
        return True


_FixtureKey = tuple[
    NativeFacadeCollectionV2,
    NativeFacadeScopeV2,
    int | None,
    NativeSignatureKindV2,
    bool,
]


def _freeze_fixture_collections_v2(
    collections: Mapping[_FixtureKey, Sequence[bytes]],
    attestation: NativeSnapshotAttestationV2,
    limits: ParseLimits,
    *,
    raw_document_owner: bool,
) -> tuple[dict[_FixtureKey, tuple[bytes, ...]], int]:
    if type(raw_document_owner) is not bool:
        raise TypeError("raw_document_owner must be an exact bool")
    frozen: dict[_FixtureKey, tuple[bytes, ...]] = {}
    largest_row = 0
    for key, rows_source in collections.items():
        if (
            type(key) is not tuple
            or len(key) != 5
            or type(key[0]) is not NativeFacadeCollectionV2
            or type(key[1]) is not NativeFacadeScopeV2
            or type(key[3]) is not NativeSignatureKindV2
            or type(key[4]) is not bool
        ):
            raise TypeError("generated facade fixture has an invalid exact key")
        _validate_page_coordinates(key[0], key[1], key[2], key[3], key[4])
        _validate_collection_capability_v2(attestation, key[0])
        if type(rows_source) is not tuple:
            raise TypeError("generated facade fixture rows must be an exact tuple")
        rows = rows_source
        _require_nonnegative_u64("generated facade fixture row count", len(rows))
        for row in rows:
            if type(row) is not bytes or not row:
                raise TypeError("generated facade fixture rows must be nonempty exact bytes")
            if len(row) > attestation.max_facade_row_bytes:
                raise ValueError("generated facade fixture row exceeds its attested maximum")
            largest_row = max(largest_row, len(row))
        _validate_page_rows_v2(
            key[0],
            rows,
            len(rows),
            attestation.max_facade_row_bytes,
            key[3],
            key[4],
            limits=limits,
            raw_document_owner=(raw_document_owner and key[1] is NativeFacadeScopeV2.DOCUMENT),
        )
        frozen[key] = rows
    return frozen, largest_row


def _digest_prefix_v2(row: bytes) -> bytes:
    return row[:32]


def _bounded_page_rows_v2(
    rows: tuple[bytes, ...],
    start: int,
    stop: int,
    max_bytes: int,
) -> tuple[bytes, ...]:
    selected: list[bytes] = []
    used = 0
    for index in range(start, stop):
        row = rows[index]
        following = used + len(row)
        if selected and following > max_bytes:
            break
        selected.append(row)
        used = following
        if used > max_bytes:
            break
    return tuple(selected)


@dataclass(frozen=True, slots=True)
class _GeneratedFacadeBindingV2:
    content_digests: NativeSnapshotContentDigestsV2
    snapshot_totals: FrozenMap[_TraversalCoordinateV2, int]
    document_totals: FrozenMap[_TraversalCoordinateV2, int]


class _GeneratedFacadeFixtureV2:
    __slots__ = (
        "_binding",
        "_collections",
        "_counters",
        "_lock",
        "_pid",
        "_raw_document_collections",
    )

    def __init__(
        self,
        collections: Mapping[_FixtureKey, Sequence[bytes]],
        attestation: NativeSnapshotAttestationV2,
        fingerprint_evidence: tuple[NativeFingerprintEvidenceV2, ...],
        fingerprint_preimages: tuple[bytes, ...],
        documents: tuple[NativeDocumentPublicationV1, ...],
        report: NativeLoadReportPublicationV1,
        root_document_key: str,
        load_options: LoadOptions,
        capability_bits: int,
        owl2_dl_report_summary: NativeOWL2DLReportSummaryV2 | None,
        facade_cardinality_summary: NativeFacadeCardinalitySummaryV2,
        raw_document_collections: Mapping[_FixtureKey, Sequence[bytes]] | None = None,
    ) -> None:
        if type(fingerprint_evidence) is not tuple or not all(
            type(item) is NativeFingerprintEvidenceV2 for item in fingerprint_evidence
        ):
            raise TypeError("generated facade fingerprint evidence must be an exact tuple")
        if type(fingerprint_preimages) is not tuple or not all(
            type(item) is bytes for item in fingerprint_preimages
        ):
            raise TypeError("generated facade fingerprint preimages must be exact bytes")
        frozen, effective_largest = _freeze_fixture_collections_v2(
            collections,
            attestation,
            load_options.limits,
            raw_document_owner=False,
        )
        raw_source = collections if raw_document_collections is None else raw_document_collections
        raw_frozen, raw_largest = _freeze_fixture_collections_v2(
            raw_source,
            attestation,
            load_options.limits,
            raw_document_owner=True,
        )
        largest_row = max(effective_largest, raw_largest)
        if max(1, largest_row) != attestation.max_facade_row_bytes:
            raise ValueError("generated facade fixture does not match its actual maximum row size")
        self._collections = FrozenMap(frozen)
        self._raw_document_collections = FrozenMap(raw_frozen)
        observed_content = native_snapshot_content_digests_v2(
            documents=documents,
            report=report,
            root_document_key=root_document_key,
            load_options=load_options,
            capability_bits=capability_bits,
            collections=self._collections,
            fingerprint_evidence=fingerprint_evidence,
            fingerprint_preimages=fingerprint_preimages,
            owl2_dl_report_summary=owl2_dl_report_summary,
            facade_cardinality_summary=facade_cardinality_summary,
            raw_document_collections=self._raw_document_collections,
        )
        owner_content = NativeSnapshotContentDigestsV2(
            root_table_sha256=attestation.root_table_sha256,
            effective_root_table_sha256=attestation.effective_root_table_sha256,
            fingerprint_inputs_sha256=attestation.fingerprint_inputs_sha256,
            source_manifest_sha256=attestation.source_manifest_sha256,
            provenance_manifest_sha256=attestation.provenance_manifest_sha256,
            effective_origin_manifest_sha256=(attestation.effective_origin_manifest_sha256),
        )
        if observed_content != owner_content:
            _fail(
                "generated V2 fixture content diverges from its owner attestation",
                "NATIVE_CONTENT_MANIFEST",
            )
        self._binding = _GeneratedFacadeBindingV2(
            content_digests=observed_content,
            snapshot_totals=FrozenMap(
                _known_publication_totals_v2(
                    documents,
                    report,
                    owl2_dl_report_summary,
                    facade_cardinality_summary,
                    raw_document_owner=False,
                )
            ),
            document_totals=FrozenMap(
                _known_publication_totals_v2(
                    documents,
                    report,
                    owl2_dl_report_summary,
                    facade_cardinality_summary,
                    raw_document_owner=True,
                )
            ),
        )
        self._counters = {
            name: 0
            for _ordinal, name, _type_name, _cardinality in (NATIVE_FACADE_COUNTER_FIELDS_V2)
        }
        input_items = tuple(
            (key, rows)
            for key, rows in raw_frozen.items()
            if key[1] is NativeFacadeScopeV2.DOCUMENT
            and key[3] is NativeSignatureKindV2.ALL
            and key[4] is True
        )
        retained_names = {
            NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS: "retained_annotation_rows",
            NativeFacadeCollectionV2.AXIOMS: "retained_axiom_rows",
            NativeFacadeCollectionV2.EXTENSIONS: "retained_extension_rows",
            NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES: "retained_source_map_rows",
            NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES: "retained_source_prefix_rows",
            NativeFacadeCollectionV2.ORIGIN_ENTRIES: "retained_origin_rows",
            NativeFacadeCollectionV2.RDF_REPORT_HEADER: "retained_rdf_header_rows",
            NativeFacadeCollectionV2.RDF_UNCONSUMED_TRIPLES: "retained_rdf_triple_rows",
            NativeFacadeCollectionV2.RDF_RULE_IDS: "retained_rdf_rule_rows",
            NativeFacadeCollectionV2.RDF_DIAGNOSTICS: "retained_rdf_diagnostic_rows",
            NativeFacadeCollectionV2.OWL2_DL_STRUCTURAL_ISSUES: (
                "retained_owl2_dl_structural_issue_rows"
            ),
            NativeFacadeCollectionV2.OWL2_DL_ISSUES: "retained_owl2_dl_issue_rows",
            NativeFacadeCollectionV2.OWL2_DL_ROLE_PROPERTIES: (
                "retained_owl2_dl_role_property_rows"
            ),
            NativeFacadeCollectionV2.OWL2_DL_ROLE_HIERARCHY: (
                "retained_owl2_dl_role_hierarchy_rows"
            ),
            NativeFacadeCollectionV2.OWL2_DL_ROLE_COMPOSITE: (
                "retained_owl2_dl_role_composite_rows"
            ),
            NativeFacadeCollectionV2.OWL2_DL_ROLE_NON_SIMPLE: (
                "retained_owl2_dl_role_non_simple_rows"
            ),
        }
        retained_items = [*frozen.items()]
        retained_items.extend(
            (key, rows) for key, rows in raw_frozen.items() if frozen.get(key) is not rows
        )
        for key, rows in retained_items:
            retained_name = retained_names.get(key[0])
            if retained_name is not None:
                self._counters[retained_name] += len(rows)
        self._counters["retained_document_tables"] = attestation.document_count
        input_root_rows = tuple(
            row
            for key, rows in input_items
            if key[0]
            in {
                NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS,
                NativeFacadeCollectionV2.AXIOMS,
                NativeFacadeCollectionV2.EXTENSIONS,
            }
            for row in rows
        )
        self._counters["canonical_input_rows"] = len(input_root_rows)
        self._counters["canonical_input_bytes"] = sum(map(len, input_root_rows))
        for key, rows in retained_items:
            collection = key[0]
            retained_bytes = sum(map(len, rows))
            if collection in _OWL2_DL_COLLECTIONS_V2:
                memory_name = "retained_owl2_dl_bytes"
            elif collection in _ROOT_STRUCTURAL_COLLECTIONS_V2:
                memory_name = "retained_root_bytes"
            elif collection is NativeFacadeCollectionV2.SIGNATURE:
                memory_name = "retained_index_bytes"
            elif collection in {
                NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES,
                NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES,
            }:
                memory_name = "retained_source_bytes"
            elif collection is NativeFacadeCollectionV2.ORIGIN_ENTRIES:
                memory_name = "retained_origin_bytes"
            else:
                memory_name = "retained_rdf_bytes"
            self._counters[memory_name] += retained_bytes
        self._counters["retained_metadata_bytes"] = (
            len(fields(observed_content)) * 32
            + (len(self._binding.snapshot_totals) + len(self._binding.document_totals)) * 8
        )
        self._counters["retained_owner_bytes"] = sum(
            self._counters[name]
            for name in (
                "retained_component_bytes",
                "retained_root_bytes",
                "retained_source_bytes",
                "retained_origin_bytes",
                "retained_rdf_bytes",
                "retained_owl2_dl_bytes",
                "retained_index_bytes",
                "retained_metadata_bytes",
            )
        )
        self._counters["publication_metadata_records_emitted"] = (
            2 + attestation.document_count + attestation.import_edge_count
        )
        self._lock = threading.Lock()
        self._pid = os.getpid()

    def _reinitialize_after_fork(self) -> None:
        current = os.getpid()
        if current == self._pid:
            return
        self._pid = current
        self._lock = threading.Lock()
        for name, counter_class in _NATIVE_FACADE_COUNTER_DEFINITIONS_V2:
            if counter_class in {
                "monotonic-process-epoch-facade",
                "current-process-epoch-gauge",
            }:
                self._counters[name] = 0
        self._counters["peak_facade_cache_bytes"] = 0
        self._counters["fork_reinitializations"] = 1

    def page(
        self,
        request: NativeFacadePageRequestV2,
        *,
        raw_document_owner: bool,
    ) -> NativeFacadePageV2:
        self._reinitialize_after_fork()
        key = (
            request.collection,
            request.scope,
            request.document_ordinal,
            request.signature_kind,
            request.include_builtins,
        )
        use_raw = request.scope is NativeFacadeScopeV2.DOCUMENT and (
            request.collection
            in {
                NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES,
                NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES,
            }
            or (
                raw_document_owner
                and request.collection
                in {*_ROOT_STRUCTURAL_COLLECTIONS_V2, NativeFacadeCollectionV2.ORIGIN_ENTRIES}
            )
        )
        selected_collections = self._raw_document_collections if use_raw else self._collections
        retained_rows = selected_collections.get(key, ())
        if request.digest_filter is None:
            lower = 0
            upper = len(retained_rows)
        else:
            lower = bisect_left(
                retained_rows,
                request.digest_filter,
                key=_digest_prefix_v2,
            )
            upper = bisect_right(
                retained_rows,
                request.digest_filter,
                key=_digest_prefix_v2,
            )
        total_count = upper - lower
        if request.start > total_count:
            selected: tuple[bytes, ...] = ()
        else:
            absolute_start = lower + request.start
            absolute_stop = min(upper, absolute_start + request.max_rows)
            selected = _bounded_page_rows_v2(
                retained_rows,
                absolute_start,
                absolute_stop,
                request.max_bytes,
            )
        end = request.start + len(selected)
        terminal = end == total_count
        page = _unchecked_owner_page_v2(
            request,
            total_count=total_count,
            next_cursor=None if terminal else end,
            terminal=terminal,
            rows=selected,
        )
        with self._lock:
            self._counters["page_requests"] += 1
            self._counters["pages_returned"] += 1
            self._counters["rows_emitted"] += len(selected)
            self._counters["payload_bytes_copied"] += page.page_bytes
            emitted_counter = {
                NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS: ("ontology_annotation_rows_emitted"),
                NativeFacadeCollectionV2.AXIOMS: "axiom_rows_emitted",
                NativeFacadeCollectionV2.EXTENSIONS: "extension_rows_emitted",
                NativeFacadeCollectionV2.SIGNATURE: "signature_rows_emitted",
                NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES: "source_map_rows_emitted",
                NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES: "source_prefix_rows_emitted",
                NativeFacadeCollectionV2.ORIGIN_ENTRIES: "origin_rows_emitted",
                NativeFacadeCollectionV2.RDF_REPORT_HEADER: "rdf_header_rows_emitted",
                NativeFacadeCollectionV2.RDF_UNCONSUMED_TRIPLES: "rdf_triple_rows_emitted",
                NativeFacadeCollectionV2.RDF_RULE_IDS: "rdf_rule_rows_emitted",
                NativeFacadeCollectionV2.RDF_DIAGNOSTICS: "rdf_diagnostic_rows_emitted",
                NativeFacadeCollectionV2.OWL2_DL_STRUCTURAL_ISSUES: (
                    "owl2_dl_structural_issue_rows_emitted"
                ),
                NativeFacadeCollectionV2.OWL2_DL_ISSUES: "owl2_dl_issue_rows_emitted",
                NativeFacadeCollectionV2.OWL2_DL_ROLE_PROPERTIES: (
                    "owl2_dl_role_property_rows_emitted"
                ),
                NativeFacadeCollectionV2.OWL2_DL_ROLE_HIERARCHY: (
                    "owl2_dl_role_hierarchy_rows_emitted"
                ),
                NativeFacadeCollectionV2.OWL2_DL_ROLE_COMPOSITE: (
                    "owl2_dl_role_composite_rows_emitted"
                ),
                NativeFacadeCollectionV2.OWL2_DL_ROLE_NON_SIMPLE: (
                    "owl2_dl_role_non_simple_rows_emitted"
                ),
            }[request.collection]
            self._counters[emitted_counter] += len(selected)
            byte_counter = (
                "canonical_payload_bytes_copied"
                if request.collection in _STRUCTURAL_COLLECTIONS
                else "auxiliary_payload_bytes_copied"
            )
            self._counters[byte_counter] += page.page_bytes
        return page

    def contains(
        self,
        request: NativeFacadeContainsRequestV2,
        *,
        raw_document_owner: bool,
    ) -> bool:
        self._reinitialize_after_fork()
        key = (
            request.collection,
            request.scope,
            request.document_ordinal,
            NativeSignatureKindV2.ALL,
            True,
        )
        selected_collections = (
            self._raw_document_collections
            if raw_document_owner and request.scope is NativeFacadeScopeV2.DOCUMENT
            else self._collections
        )
        rows = selected_collections.get(key, ())
        index = bisect_left(rows, request.canonical)
        found = index < len(rows) and rows[index] == request.canonical
        with self._lock:
            self._counters["contains_requests"] += 1
            self._counters["contains_hits"] += int(found)
        return found

    def counters(self) -> NativeFacadeCountersV2:
        self._reinitialize_after_fork()
        with self._lock:
            return NativeFacadeCountersV2(**self._counters)

    def bump_close(self, *, transitioned: bool) -> None:
        self._reinitialize_after_fork()
        with self._lock:
            self._counters["close_requests"] += 1
            self._counters["close_transitions"] += int(transitioned)


class _GeneratedNativeDocumentOwnerV2(tuple[object, ...]):
    __slots__ = ()

    def __new__(
        cls,
        attestation: NativeSnapshotAttestationV2,
        document_ordinal: int,
        fixture: _GeneratedFacadeFixtureV2,
    ) -> _GeneratedNativeDocumentOwnerV2:
        if type(attestation) is not NativeSnapshotAttestationV2:
            raise TypeError("generated V2 document owner requires an exact attestation")
        _require_nonnegative_u64("generated V2 document ordinal", document_ordinal)
        if document_ordinal >= attestation.document_count:
            raise ValueError("generated V2 document ordinal is out of bounds")
        if type(fixture) is not _GeneratedFacadeFixtureV2:
            raise TypeError("generated V2 document owner requires an exact fixture")
        return tuple.__new__(
            cls,
            (attestation, _GeneratedHandleLifecycleV2(), fixture, document_ordinal),
        )

    def _publication_attestation_v2(self) -> NativeSnapshotAttestationV2:
        self._prepare_process()
        return cast(NativeSnapshotAttestationV2, tuple.__getitem__(self, 0))

    def _publication_closed_v2(self) -> bool:
        lifecycle, _fixture = self._prepare_process()
        with lifecycle.lock:
            return lifecycle.closed

    def _publication_close_v2(self) -> None:
        lifecycle, fixture = self._prepare_process()
        with lifecycle.lock:
            fixture.bump_close(transitioned=not lifecycle.closed)
            lifecycle.closed = True

    def _publication_page_v2(self, request: NativeFacadePageRequestV2) -> NativeFacadePageV2:
        lifecycle, fixture = self._prepare_process()
        with lifecycle.lock:
            if lifecycle.closed:
                raise ClosedSnapshotError("native V2 document handle is closed")
            return fixture.page(request, raw_document_owner=True)

    def _publication_contains_v2(self, request: NativeFacadeContainsRequestV2) -> bool:
        lifecycle, fixture = self._prepare_process()
        with lifecycle.lock:
            if lifecycle.closed:
                raise ClosedSnapshotError("native V2 document handle is closed")
            return fixture.contains(request, raw_document_owner=True)

    def _publication_counters_v2(self) -> NativeFacadeCountersV2:
        _lifecycle, fixture = self._prepare_process()
        return fixture.counters()

    def _prepare_process(self) -> tuple[_GeneratedHandleLifecycleV2, _GeneratedFacadeFixtureV2]:
        lifecycle = cast(_GeneratedHandleLifecycleV2, tuple.__getitem__(self, 1))
        fixture = cast(_GeneratedFacadeFixtureV2, tuple.__getitem__(self, 2))
        lifecycle.reinitialize_after_fork()
        fixture._reinitialize_after_fork()
        return lifecycle, fixture


class _GeneratedNativeSnapshotOwnerV2(tuple[object, ...]):
    __slots__ = ()

    def __new__(
        cls,
        attestation: NativeSnapshotAttestationV2,
        fixture: _GeneratedFacadeFixtureV2,
    ) -> _GeneratedNativeSnapshotOwnerV2:
        if type(attestation) is not NativeSnapshotAttestationV2:
            raise TypeError("generated V2 owner requires an exact attestation")
        if type(fixture) is not _GeneratedFacadeFixtureV2:
            raise TypeError("generated V2 owner requires an exact fixture")
        return tuple.__new__(cls, (attestation, _GeneratedHandleLifecycleV2(), fixture))

    def _publication_attestation_v2(self) -> NativeSnapshotAttestationV2:
        self._prepare_process()
        return cast(NativeSnapshotAttestationV2, tuple.__getitem__(self, 0))

    def _publication_closed_v2(self) -> bool:
        lifecycle, _fixture = self._prepare_process()
        with lifecycle.lock:
            return lifecycle.closed

    def _publication_close_v2(self) -> None:
        lifecycle, fixture = self._prepare_process()
        with lifecycle.lock:
            fixture.bump_close(transitioned=not lifecycle.closed)
            lifecycle.closed = True

    def _publication_page_v2(self, request: NativeFacadePageRequestV2) -> NativeFacadePageV2:
        lifecycle, fixture = self._prepare_process()
        with lifecycle.lock:
            if lifecycle.closed:
                raise ClosedSnapshotError("native V2 snapshot handle is closed")
            return fixture.page(request, raw_document_owner=False)

    def _publication_contains_v2(self, request: NativeFacadeContainsRequestV2) -> bool:
        lifecycle, fixture = self._prepare_process()
        with lifecycle.lock:
            if lifecycle.closed:
                raise ClosedSnapshotError("native V2 snapshot handle is closed")
            return fixture.contains(request, raw_document_owner=False)

    def _publication_counters_v2(self) -> NativeFacadeCountersV2:
        _lifecycle, fixture = self._prepare_process()
        return fixture.counters()

    def _publication_document_v2(self, document_ordinal: int) -> _GeneratedNativeDocumentOwnerV2:
        lifecycle, fixture = self._prepare_process()
        with lifecycle.lock:
            if lifecycle.closed:
                raise ClosedSnapshotError("native V2 snapshot handle is closed")
            return _GeneratedNativeDocumentOwnerV2(
                cast(NativeSnapshotAttestationV2, tuple.__getitem__(self, 0)),
                document_ordinal,
                fixture,
            )

    def _prepare_process(self) -> tuple[_GeneratedHandleLifecycleV2, _GeneratedFacadeFixtureV2]:
        lifecycle = cast(_GeneratedHandleLifecycleV2, tuple.__getitem__(self, 1))
        fixture = cast(_GeneratedFacadeFixtureV2, tuple.__getitem__(self, 2))
        lifecycle.reinitialize_after_fork()
        fixture._reinitialize_after_fork()
        return lifecycle, fixture


_TraversalCoordinateV2 = tuple[
    NativeFacadeCollectionV2,
    NativeFacadeScopeV2,
    int | None,
    NativeSignatureKindV2,
    bool,
    bytes | None,
]


class _NativeFacadeTraversalStateV2:
    __slots__ = (
        "_bound",
        "_boundaries",
        "_document_expected",
        "_expected",
        "_limits",
        "_lock",
        "_pid",
        "_raw_document_owner",
        "_totals",
    )

    def __init__(self, *, raw_document_owner: bool = False) -> None:
        if type(raw_document_owner) is not bool:
            raise TypeError("raw_document_owner must be an exact bool")
        self._pid = os.getpid()
        self._lock = threading.RLock()
        self._totals: dict[_TraversalCoordinateV2, int] = {}
        self._boundaries: dict[_TraversalCoordinateV2, tuple[int, tuple[object, ...]]] = {}
        self._expected: dict[_TraversalCoordinateV2, int] = {}
        self._document_expected: dict[_TraversalCoordinateV2, int] = {}
        self._limits: ParseLimits | None = None
        self._raw_document_owner = raw_document_owner
        self._bound = False

    def bind_publication(
        self,
        documents: tuple[NativeDocumentPublicationV1, ...],
        report: NativeLoadReportPublicationV1,
        load_options: LoadOptions,
        owl2_dl_report_summary: NativeOWL2DLReportSummaryV2 | None,
        facade_cardinality_summary: NativeFacadeCardinalitySummaryV2,
    ) -> None:
        expected = _known_publication_totals_v2(
            documents,
            report,
            owl2_dl_report_summary,
            facade_cardinality_summary,
            raw_document_owner=False,
        )
        document_expected = _known_publication_totals_v2(
            documents,
            report,
            owl2_dl_report_summary,
            facade_cardinality_summary,
            raw_document_owner=True,
        )
        _validate_exact_load_options_v2(load_options)
        selected_limits = load_options.limits
        self._prepare_process()
        with self._lock:
            if self._bound and (
                self._expected != expected
                or self._document_expected != document_expected
                or self._limits != selected_limits
            ):
                _fail("V2 handle was rebound to different publication totals", "NATIVE_PAGE_TOTAL")
            self._expected = expected
            self._document_expected = document_expected
            self._limits = selected_limits
            self._bound = True

    def for_document(self) -> _NativeFacadeTraversalStateV2:
        selected = _NativeFacadeTraversalStateV2(raw_document_owner=True)
        self._prepare_process()
        with self._lock:
            selected._expected = dict(self._document_expected)
            selected._document_expected = dict(self._document_expected)
            selected._limits = self._limits
            selected._bound = self._bound
        return selected

    def validation_limits(self) -> ParseLimits | None:
        self._prepare_process()
        with self._lock:
            return self._limits

    def raw_document_owner(self) -> bool:
        self._prepare_process()
        with self._lock:
            return self._raw_document_owner

    def validate_page(
        self,
        request: NativeFacadePageRequestV2,
        page: NativeFacadePageV2,
    ) -> None:
        coordinate = _traversal_coordinate_v2(request)
        bounds = _page_order_bounds_v2(
            page,
            raw_document_owner=self._raw_document_owner,
        )
        self._prepare_process()
        with self._lock:
            known = self._expected.get(coordinate)
            if known is not None and page.total_count != known:
                _fail(
                    "V2 facade page total diverges from publication metadata",
                    "NATIVE_PAGE_TOTAL",
                )
            pinned = self._totals.setdefault(coordinate, page.total_count)
            if pinned != page.total_count:
                _fail("V2 facade page total changed within one traversal", "NATIVE_PAGE_TOTAL")
            previous = self._boundaries.get(coordinate)
            if request.start == 0:
                previous = None
            elif (
                previous is not None
                and previous[0] == request.start
                and bounds is not None
                and _page_boundary_is_invalid_v2(
                    request.collection,
                    previous[1],
                    bounds[0],
                    raw_document_owner=self._raw_document_owner,
                )
            ):
                _fail(
                    "V2 facade page boundary is not ascending unique",
                    "NATIVE_PAGE_ORDER",
                )
            if page.next_cursor is not None and bounds is not None:
                self._boundaries[coordinate] = (page.next_cursor, bounds[1])
            else:
                self._boundaries.pop(coordinate, None)

    def _prepare_process(self) -> None:
        current = os.getpid()
        if current == self._pid:
            return
        self._pid = current
        self._lock = threading.RLock()
        self._totals = {}
        self._boundaries = {}


def _known_publication_totals_v2(
    documents: tuple[NativeDocumentPublicationV1, ...],
    report: NativeLoadReportPublicationV1,
    owl2_dl_report_summary: NativeOWL2DLReportSummaryV2 | None,
    facade_cardinality_summary: NativeFacadeCardinalitySummaryV2,
    *,
    raw_document_owner: bool,
) -> dict[_TraversalCoordinateV2, int]:
    result: dict[_TraversalCoordinateV2, int] = {}
    for ordinal, document in enumerate(documents):
        summary = facade_cardinality_summary.documents[ordinal]
        base = (
            NativeFacadeScopeV2.DOCUMENT,
            ordinal,
            NativeSignatureKindV2.ALL,
            True,
            None,
        )
        if raw_document_owner:
            result[(NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS, *base)] = (
                document.ontology_annotation_count
            )
            result[(NativeFacadeCollectionV2.AXIOMS, *base)] = document.axiom_count
            result[(NativeFacadeCollectionV2.EXTENSIONS, *base)] = document.extension_count
        else:
            result[(NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS, *base)] = (
                summary.effective_annotation_count
            )
            result[(NativeFacadeCollectionV2.AXIOMS, *base)] = summary.effective_axiom_count
            result[(NativeFacadeCollectionV2.EXTENSIONS, *base)] = summary.effective_extension_count
        result[(NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES, *base)] = (
            document.source_map_entry_count
        )
        result[(NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES, *base)] = (
            summary.raw_source_prefix_count
        )
        if raw_document_owner:
            result[(NativeFacadeCollectionV2.ORIGIN_ENTRIES, *base)] = document.origin_entry_count
        else:
            result[(NativeFacadeCollectionV2.ORIGIN_ENTRIES, *base)] = (
                summary.effective_origin_count
            )
        result[(NativeFacadeCollectionV2.RDF_REPORT_HEADER, *base)] = int(
            document.rdf_mapping_report_sha256 is not None
        )
        result[(NativeFacadeCollectionV2.RDF_UNCONSUMED_TRIPLES, *base)] = (
            summary.rdf_unconsumed_triple_count
        )
        result[(NativeFacadeCollectionV2.RDF_RULE_IDS, *base)] = summary.rdf_rule_count
        result[(NativeFacadeCollectionV2.RDF_DIAGNOSTICS, *base)] = summary.rdf_diagnostic_count
    result[
        (
            NativeFacadeCollectionV2.AXIOMS,
            NativeFacadeScopeV2.CLOSURE,
            None,
            NativeSignatureKindV2.ALL,
            True,
            None,
        )
    ] = report.effective_axiom_count
    closure_base = (
        NativeFacadeScopeV2.CLOSURE,
        None,
        NativeSignatureKindV2.ALL,
        True,
        None,
    )
    result[(NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS, *closure_base)] = (
        facade_cardinality_summary.closure.effective_annotation_count
    )
    result[(NativeFacadeCollectionV2.EXTENSIONS, *closure_base)] = (
        facade_cardinality_summary.closure.effective_extension_count
    )
    result[(NativeFacadeCollectionV2.ORIGIN_ENTRIES, *closure_base)] = (
        facade_cardinality_summary.closure.effective_origin_count
    )
    if owl2_dl_report_summary is not None:
        for collection, count in (
            (
                NativeFacadeCollectionV2.OWL2_DL_STRUCTURAL_ISSUES,
                owl2_dl_report_summary.structural_issue_count,
            ),
            (NativeFacadeCollectionV2.OWL2_DL_ISSUES, owl2_dl_report_summary.issue_count),
            (
                NativeFacadeCollectionV2.OWL2_DL_ROLE_PROPERTIES,
                owl2_dl_report_summary.role_property_count,
            ),
            (
                NativeFacadeCollectionV2.OWL2_DL_ROLE_HIERARCHY,
                owl2_dl_report_summary.role_hierarchy_count,
            ),
            (
                NativeFacadeCollectionV2.OWL2_DL_ROLE_COMPOSITE,
                owl2_dl_report_summary.role_composite_count,
            ),
            (
                NativeFacadeCollectionV2.OWL2_DL_ROLE_NON_SIMPLE,
                owl2_dl_report_summary.role_non_simple_count,
            ),
        ):
            result[
                (
                    collection,
                    NativeFacadeScopeV2.CLOSURE,
                    None,
                    NativeSignatureKindV2.ALL,
                    True,
                    None,
                )
            ] = count
    return result


def _traversal_coordinate_v2(
    request: NativeFacadePageRequestV2,
) -> _TraversalCoordinateV2:
    return (
        request.collection,
        request.scope,
        request.document_ordinal,
        request.signature_kind,
        request.include_builtins,
        request.digest_filter,
    )


def _page_order_bounds_v2(
    page: NativeFacadePageV2,
    *,
    raw_document_owner: bool,
) -> tuple[tuple[object, ...], tuple[object, ...]] | None:
    if not page.rows:
        return None
    if page.collection in _STRUCTURAL_COLLECTIONS:
        return (page.rows[0],), (page.rows[-1],)
    if page.collection not in {
        NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES,
        NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES,
        NativeFacadeCollectionV2.ORIGIN_ENTRIES,
        NativeFacadeCollectionV2.RDF_RULE_IDS,
        NativeFacadeCollectionV2.OWL2_DL_ROLE_HIERARCHY,
    }:
        return None
    validated_rows = page._validated_rows_v2()
    first = cast(NativeAuxiliaryRowV2, validated_rows[0])
    last = cast(NativeAuxiliaryRowV2, validated_rows[-1])
    if page.collection is NativeFacadeCollectionV2.ORIGIN_ENTRIES and raw_document_owner:
        return (cast(NativeOriginRowV2, first).digest,), (cast(NativeOriginRowV2, last).digest,)
    return (
        _auxiliary_order_key_v2(page.collection, first, page.rows[0]),
        _auxiliary_order_key_v2(page.collection, last, page.rows[-1]),
    )


def _page_boundary_is_invalid_v2(
    collection: NativeFacadeCollectionV2,
    previous: tuple[object, ...],
    current: tuple[object, ...],
    *,
    raw_document_owner: bool,
) -> bool:
    if collection is NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES or (
        collection is NativeFacadeCollectionV2.ORIGIN_ENTRIES and raw_document_owner
    ):
        return previous > current
    return previous >= current


_REGISTERED_OWNER_TYPES_V2: set[type[object]] = {_GeneratedNativeSnapshotOwnerV2}
_REGISTERED_DOCUMENT_OWNER_TYPES_V2: set[type[object]] = {_GeneratedNativeDocumentOwnerV2}
_registered_rust_owner_type_v2: type[object] | None = None
_registered_rust_document_owner_type_v2: type[object] | None = None


class _NativeFacadeHandleBaseV2:
    __slots__ = ("_owner_v2", "_traversal_v2")

    def _call_owner_v2(self, member: str, *arguments: object) -> object:
        owner = object.__getattribute__(self, "_owner_v2")
        try:
            method = getattr(owner, member)
            return method(*arguments)
        except (MemoryError, KeyboardInterrupt, SystemExit):
            raise
        except (BackendProtocolError, ClosedSnapshotError):
            raise
        except Exception as error:
            raise BackendProtocolError(
                "native V2 snapshot owner failed", code="NATIVE_HANDLE_OWNER"
            ) from error

    def _attestation_v2(self) -> NativeSnapshotAttestationV2:
        value = self._call_owner_v2(_HANDLE_OWNER_ATTESTATION_MEMBER_V2)
        if type(value) is not NativeSnapshotAttestationV2:
            _fail("registered V2 owner returned an invalid attestation", "NATIVE_HANDLE_OWNER")
        observed = cast(NativeSnapshotAttestationV2, value)
        return NativeSnapshotAttestationV2(
            **{item.name: getattr(observed, item.name) for item in fields(observed)}
        )

    def _closed_v2(self) -> bool:
        value = self._call_owner_v2(_HANDLE_OWNER_CLOSED_MEMBER_V2)
        if type(value) is not bool:
            _fail("registered V2 owner returned invalid lifecycle state", "NATIVE_HANDLE_OWNER")
        return cast(bool, value)

    def _close_v2(self) -> None:
        if self._call_owner_v2(_HANDLE_OWNER_CLOSE_MEMBER_V2) is not None:
            _fail("registered V2 owner close returned a value", "NATIVE_HANDLE_OWNER")

    def _page_v2(
        self,
        request: NativeFacadePageRequestV2,
        *,
        fixed_document_ordinal: int | None = None,
    ) -> NativeFacadePageV2:
        if type(request) is not NativeFacadePageRequestV2:
            raise TypeError("request must be an exact NativeFacadePageRequestV2")
        request = NativeFacadePageRequestV2(
            **{
                item.name: getattr(request, item.name)
                for item in fields(NativeFacadePageRequestV2)
                if item.init
            }
        )
        traversal = cast(
            _NativeFacadeTraversalStateV2,
            object.__getattribute__(self, "_traversal_v2"),
        )
        validation_limits = traversal.validation_limits()
        raw_document_owner = traversal.raw_document_owner()
        attestation = self._attestation_v2()
        _validate_request_context_v2(request, attestation, fixed_document_ordinal)
        value = self._call_owner_v2(_HANDLE_OWNER_PAGE_MEMBER_V2, request)
        if type(value) is not NativeFacadePageV2:
            _fail("registered V2 owner returned an invalid page", "NATIVE_HANDLE_OWNER")
        observed = cast(NativeFacadePageV2, value)
        try:
            page = NativeFacadePageV2(
                **{
                    item.name: getattr(observed, item.name)
                    for item in fields(observed)
                    if item.init
                },
                _validation_limits=validation_limits,
                _raw_document_owner=raw_document_owner,
            )
        except (MemoryError, KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            raise BackendProtocolError(
                "registered V2 owner returned an invalid page response",
                code="NATIVE_PAGE_RESPONSE",
            ) from error
        echoed = tuple(getattr(page, row[1]) for row in NATIVE_FACADE_PAGE_REQUEST_FIELDS_V2)
        expected = tuple(getattr(request, row[1]) for row in NATIVE_FACADE_PAGE_REQUEST_FIELDS_V2)
        if echoed != expected:
            _fail("registered V2 owner did not echo exact page coordinates", "NATIVE_PAGE_ECHO")
        traversal.validate_page(request, page)
        return page

    def _contains_v2(
        self,
        request: NativeFacadeContainsRequestV2,
        *,
        fixed_document_ordinal: int | None = None,
    ) -> bool:
        if type(request) is not NativeFacadeContainsRequestV2:
            raise TypeError("request must be an exact NativeFacadeContainsRequestV2")
        traversal = cast(
            _NativeFacadeTraversalStateV2,
            object.__getattribute__(self, "_traversal_v2"),
        )
        validation_limits = traversal.validation_limits()
        request = NativeFacadeContainsRequestV2(
            **{
                item.name: getattr(request, item.name)
                for item in fields(NativeFacadeContainsRequestV2)
                if item.init
            },
            _validation_limits=validation_limits,
        )
        attestation = self._attestation_v2()
        _validate_contains_context_v2(request, attestation, fixed_document_ordinal)
        value = self._call_owner_v2(_HANDLE_OWNER_CONTAINS_MEMBER_V2, request)
        if type(value) is not bool:
            _fail("registered V2 owner returned an invalid contains result", "NATIVE_HANDLE_OWNER")
        return cast(bool, value)

    def _counters_v2(self) -> NativeFacadeCountersV2:
        value = self._call_owner_v2(_HANDLE_OWNER_COUNTERS_MEMBER_V2)
        if type(value) is not NativeFacadeCountersV2:
            _fail("registered V2 owner returned invalid counters", "NATIVE_HANDLE_OWNER")
        observed = cast(NativeFacadeCountersV2, value)
        return NativeFacadeCountersV2(
            **{item.name: getattr(observed, item.name) for item in fields(observed)}
        )


class NativeSnapshotHandleV2(_NativeFacadeHandleBaseV2):
    """Exact sealed wrapper around one V2 retained-storage owner."""

    __slots__ = ()

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("NativeSnapshotHandleV2 is created only from a registered owner")

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("NativeSnapshotHandleV2 is sealed")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("NativeSnapshotHandleV2 is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("NativeSnapshotHandleV2 is immutable")

    @property
    def publication_version(self) -> int:
        return self.attestation.version

    @property
    def publication_ledger_sha256(self) -> bytes:
        return self.attestation.ledger_sha256

    @property
    def attestation(self) -> NativeSnapshotAttestationV2:
        return self._attestation_v2()

    @property
    def closed(self) -> bool:
        return self._closed_v2()

    def close(self) -> None:
        self._close_v2()

    def _facade_page_v2(self, request: NativeFacadePageRequestV2) -> NativeFacadePageV2:
        return self._page_v2(request)

    def _facade_contains_v2(self, request: NativeFacadeContainsRequestV2) -> bool:
        return self._contains_v2(request)

    def _facade_counters_v2(self) -> NativeFacadeCountersV2:
        return self._counters_v2()

    def _facade_document_v2(self, document_ordinal: int) -> NativeDocumentHandleV2:
        _require_nonnegative_u64("document_ordinal", document_ordinal)
        attestation = self.attestation
        if document_ordinal >= attestation.document_count:
            _fail("V2 facade document ordinal is out of bounds", "NATIVE_DOCUMENT_ORDINAL")
        owner = self._call_owner_v2(_HANDLE_OWNER_DOCUMENT_MEMBER_V2, document_ordinal)
        traversal = cast(
            _NativeFacadeTraversalStateV2,
            object.__getattribute__(self, "_traversal_v2"),
        )
        handle = _seal_native_document_owner_v2(
            owner,
            document_ordinal,
            traversal.for_document(),
        )
        if handle.attestation != attestation:
            _fail(
                "V2 document owner attestation does not match snapshot",
                "NATIVE_ATTESTATION_MISMATCH",
            )
        return handle

    def _bind_publication_v2(
        self,
        documents: tuple[NativeDocumentPublicationV1, ...],
        report: NativeLoadReportPublicationV1,
        load_options: LoadOptions,
        owl2_dl_report_summary: NativeOWL2DLReportSummaryV2 | None = None,
        facade_cardinality_summary: NativeFacadeCardinalitySummaryV2 | None = None,
    ) -> None:
        traversal = cast(
            _NativeFacadeTraversalStateV2,
            object.__getattribute__(self, "_traversal_v2"),
        )
        if facade_cardinality_summary is None:
            raise TypeError("V2 publication binding requires facade cardinalities")
        traversal.bind_publication(
            documents,
            report,
            load_options,
            owl2_dl_report_summary,
            facade_cardinality_summary,
        )

    def __copy__(self) -> NativeSnapshotHandleV2:
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> NativeSnapshotHandleV2:
        return self

    def __reduce__(self) -> NoReturn:
        raise TypeError("NativeSnapshotHandleV2 cannot be pickled")

    def __repr__(self) -> str:
        return f"NativeSnapshotHandleV2(closed={self.closed!r})"


class NativeDocumentHandleV2(_NativeFacadeHandleBaseV2):
    """Exact independently closeable owner for one attested document ordinal."""

    __slots__ = ("__document_ordinal",)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("NativeDocumentHandleV2 is created only from a registered owner")

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("NativeDocumentHandleV2 is sealed")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("NativeDocumentHandleV2 is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("NativeDocumentHandleV2 is immutable")

    @property
    def publication_version(self) -> int:
        return self.attestation.version

    @property
    def publication_ledger_sha256(self) -> bytes:
        return self.attestation.ledger_sha256

    @property
    def attestation(self) -> NativeSnapshotAttestationV2:
        return self._attestation_v2()

    @property
    def document_ordinal(self) -> int:
        return cast(int, object.__getattribute__(self, "_NativeDocumentHandleV2__document_ordinal"))

    @property
    def closed(self) -> bool:
        return self._closed_v2()

    def close(self) -> None:
        self._close_v2()

    def _facade_page_v2(self, request: NativeFacadePageRequestV2) -> NativeFacadePageV2:
        return self._page_v2(request, fixed_document_ordinal=self.document_ordinal)

    def _facade_contains_v2(self, request: NativeFacadeContainsRequestV2) -> bool:
        return self._contains_v2(request, fixed_document_ordinal=self.document_ordinal)

    def _facade_counters_v2(self) -> NativeFacadeCountersV2:
        return self._counters_v2()

    def __copy__(self) -> NativeDocumentHandleV2:
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> NativeDocumentHandleV2:
        return self

    def __reduce__(self) -> NoReturn:
        raise TypeError("NativeDocumentHandleV2 cannot be pickled")

    def __repr__(self) -> str:
        return (
            f"NativeDocumentHandleV2(document_ordinal={self.document_ordinal!r}, "
            f"closed={self.closed!r})"
        )


def _validate_request_context_v2(
    request: NativeFacadePageRequestV2,
    attestation: NativeSnapshotAttestationV2,
    fixed_document_ordinal: int | None,
) -> None:
    _validate_collection_capability_v2(attestation, request.collection)
    if request.max_row_bytes != attestation.max_facade_row_bytes:
        _fail("V2 page row bound does not match publication", "NATIVE_PAGE_BOUND")
    if (
        request.document_ordinal is not None
        and request.document_ordinal >= attestation.document_count
    ):
        _fail("V2 facade document ordinal is out of bounds", "NATIVE_DOCUMENT_ORDINAL")
    if fixed_document_ordinal is not None and (
        request.scope is not NativeFacadeScopeV2.DOCUMENT
        or request.document_ordinal != fixed_document_ordinal
    ):
        _fail("V2 document handle request escaped its fixed ordinal", "NATIVE_DOCUMENT_SCOPE")


def _validate_contains_context_v2(
    request: NativeFacadeContainsRequestV2,
    attestation: NativeSnapshotAttestationV2,
    fixed_document_ordinal: int | None,
) -> None:
    if request.max_row_bytes != attestation.max_facade_row_bytes:
        _fail("V2 contains row bound does not match publication", "NATIVE_PAGE_BOUND")
    if (
        request.document_ordinal is not None
        and request.document_ordinal >= attestation.document_count
    ):
        _fail("V2 facade document ordinal is out of bounds", "NATIVE_DOCUMENT_ORDINAL")
    if fixed_document_ordinal is not None and (
        request.scope is not NativeFacadeScopeV2.DOCUMENT
        or request.document_ordinal != fixed_document_ordinal
    ):
        _fail("V2 document handle request escaped its fixed ordinal", "NATIVE_DOCUMENT_SCOPE")


def _seal_native_snapshot_owner_v2(owner: object) -> NativeSnapshotHandleV2:
    if type(owner) not in _REGISTERED_OWNER_TYPES_V2:
        _fail("native V2 snapshot owner type is not registered", "NATIVE_HANDLE_TYPE")
    handle = object.__new__(NativeSnapshotHandleV2)
    object.__setattr__(handle, "_owner_v2", owner)
    object.__setattr__(handle, "_traversal_v2", _NativeFacadeTraversalStateV2())
    return handle


def _seal_native_document_owner_v2(
    owner: object,
    document_ordinal: int,
    traversal: _NativeFacadeTraversalStateV2 | None = None,
) -> NativeDocumentHandleV2:
    if type(owner) not in _REGISTERED_DOCUMENT_OWNER_TYPES_V2:
        _fail("native V2 document owner type is not registered", "NATIVE_HANDLE_TYPE")
    handle = object.__new__(NativeDocumentHandleV2)
    object.__setattr__(handle, "_owner_v2", owner)
    object.__setattr__(
        handle,
        "_traversal_v2",
        traversal or _NativeFacadeTraversalStateV2(raw_document_owner=True),
    )
    object.__setattr__(
        handle,
        "_NativeDocumentHandleV2__document_ordinal",
        document_ordinal,
    )
    return handle


def _generated_native_snapshot_handle_v2(
    attestation: NativeSnapshotAttestationV2,
    collections: Mapping[_FixtureKey, Sequence[bytes]],
    fingerprint_evidence: tuple[NativeFingerprintEvidenceV2, ...],
    fingerprint_preimages: tuple[bytes, ...],
    *,
    documents: tuple[NativeDocumentPublicationV1, ...],
    report: NativeLoadReportPublicationV1,
    root_document_key: str,
    load_options: LoadOptions,
    capability_bits: int,
    owl2_dl_report_summary: NativeOWL2DLReportSummaryV2 | None,
    facade_cardinality_summary: NativeFacadeCardinalitySummaryV2,
    raw_document_collections: Mapping[_FixtureKey, Sequence[bytes]] | None = None,
) -> NativeSnapshotHandleV2:
    fixture = _GeneratedFacadeFixtureV2(
        collections,
        attestation,
        fingerprint_evidence,
        fingerprint_preimages,
        documents,
        report,
        root_document_key,
        load_options,
        capability_bits,
        owl2_dl_report_summary,
        facade_cardinality_summary,
        raw_document_collections,
    )
    return _seal_native_snapshot_owner_v2(_GeneratedNativeSnapshotOwnerV2(attestation, fixture))


def _register_rust_native_snapshot_handle_v2(owner_type: type[object]) -> None:
    """Register the exact extension snapshot owner after full V2 parity exists."""

    global _registered_rust_owner_type_v2
    _validate_rust_owner_type_v2(owner_type, _RUST_OWNER_NAME_V2)
    for member in (
        _HANDLE_OWNER_ATTESTATION_MEMBER_V2,
        _HANDLE_OWNER_CLOSED_MEMBER_V2,
        _HANDLE_OWNER_CLOSE_MEMBER_V2,
        _HANDLE_OWNER_PAGE_MEMBER_V2,
        _HANDLE_OWNER_CONTAINS_MEMBER_V2,
        _HANDLE_OWNER_COUNTERS_MEMBER_V2,
        _HANDLE_OWNER_DOCUMENT_MEMBER_V2,
    ):
        if not callable(getattr(owner_type, member, None)):
            raise TypeError(f"Rust V2 publication owner lacks required member {member}")
    if _registered_rust_owner_type_v2 not in {None, owner_type}:
        raise RuntimeError("a different Rust V2 publication owner is already registered")
    _registered_rust_owner_type_v2 = owner_type
    _REGISTERED_OWNER_TYPES_V2.add(owner_type)


def _register_rust_native_document_handle_v2(owner_type: type[object]) -> None:
    """Register the exact extension document owner after full V2 parity exists."""

    global _registered_rust_document_owner_type_v2
    _validate_rust_owner_type_v2(owner_type, _RUST_DOCUMENT_OWNER_NAME_V2)
    for member in (
        _HANDLE_OWNER_ATTESTATION_MEMBER_V2,
        _HANDLE_OWNER_CLOSED_MEMBER_V2,
        _HANDLE_OWNER_CLOSE_MEMBER_V2,
        _HANDLE_OWNER_PAGE_MEMBER_V2,
        _HANDLE_OWNER_CONTAINS_MEMBER_V2,
        _HANDLE_OWNER_COUNTERS_MEMBER_V2,
    ):
        if not callable(getattr(owner_type, member, None)):
            raise TypeError(f"Rust V2 document owner lacks required member {member}")
    if _registered_rust_document_owner_type_v2 not in {None, owner_type}:
        raise RuntimeError("a different Rust V2 document owner is already registered")
    _registered_rust_document_owner_type_v2 = owner_type
    _REGISTERED_DOCUMENT_OWNER_TYPES_V2.add(owner_type)


def _validate_rust_owner_type_v2(owner_type: type[object], expected_name: str) -> None:
    module = sys.modules.get(_RUST_OWNER_MODULE_V2)
    if (
        not isinstance(owner_type, type)
        or owner_type.__module__ != _RUST_OWNER_MODULE_V2
        or owner_type.__name__ != expected_name
        or module is None
        or getattr(module, expected_name, None) is not owner_type
    ):
        raise TypeError("Rust V2 publication owner must be the exact registered extension type")


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeSnapshotPublicationV2:
    version: int
    ledger_sha256: bytes
    handle: NativeSnapshotHandleV2
    documents: tuple[NativeDocumentPublicationV1, ...]
    import_manifest: NativeImportManifestPublicationV1
    root_document_key: str
    load_options: LoadOptions
    diagnostics: tuple[NativeDiagnosticPublicationV1, ...]
    diagnostic_reference_sidecars: NativeDiagnosticReferenceSidecarsV2
    facade_cardinality_summary: NativeFacadeCardinalitySummaryV2
    report: NativeLoadReportPublicationV1
    capability_bits: int
    root_table_sha256: bytes
    effective_root_table_sha256: bytes
    fingerprint_inputs_sha256: bytes
    source_manifest_sha256: bytes
    provenance_manifest_sha256: bytes
    effective_origin_manifest_sha256: bytes
    max_facade_row_bytes: int
    owl2_dl_report_summary: NativeOWL2DLReportSummaryV2 | None

    def __post_init__(self) -> None:
        _require_nonnegative_u32("publication.version", self.version)
        if self.version != NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2:
            _fail(
                "native snapshot publication version is unsupported",
                "NATIVE_PUBLICATION_VERSION",
            )
        _require_digest("ledger_sha256", self.ledger_sha256)
        if self.ledger_sha256 != NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2:
            _fail("native V2 publication ledger does not match", "NATIVE_PUBLICATION_LEDGER")
        if type(self.handle) is not NativeSnapshotHandleV2:
            _fail("native V2 publication handle is not sealed", "NATIVE_HANDLE_TYPE")
        if type(self.documents) is not tuple or not self.documents:
            _fail("native V2 publication documents are invalid", "NATIVE_PUBLICATION_DOCUMENTS")
        documents = self.documents
        if len(documents) > _bound_v2("max_documents"):
            _fail("native V2 publication has too many documents", "NATIVE_PUBLICATION_LIMIT")
        if not all(type(item) is NativeDocumentPublicationV1 for item in documents):
            _fail("native V2 publication documents are invalid", "NATIVE_PUBLICATION_DOCUMENTS")
        if type(self.import_manifest) is not NativeImportManifestPublicationV1:
            _fail("native V2 import manifest is not frozen", "NATIVE_PUBLICATION_MANIFEST")
        object.__setattr__(self, "root_document_key", _copy_document_key(self.root_document_key))
        if type(self.load_options) is not LoadOptions:
            _fail("native V2 load options are invalid", "NATIVE_PUBLICATION_OPTIONS")
        if type(self.diagnostics) is not tuple:
            _fail(
                "native V2 publication diagnostics are not an exact tuple",
                "NATIVE_PUBLICATION_DIAGNOSTICS",
            )
        diagnostics = self.diagnostics
        if len(diagnostics) > _bound_v2("max_diagnostics_per_sequence"):
            _fail(
                "native V2 publication diagnostics exceed the row bound",
                "NATIVE_PUBLICATION_LIMIT",
            )
        _validate_diagnostic_sequence("V2 publication diagnostics", diagnostics)
        if type(self.diagnostic_reference_sidecars) is not NativeDiagnosticReferenceSidecarsV2:
            _fail(
                "native V2 diagnostic reference sidecars are invalid",
                "NATIVE_DIAGNOSTICS",
            )
        if type(self.facade_cardinality_summary) is not NativeFacadeCardinalitySummaryV2:
            _fail("native V2 facade cardinality summary is invalid", "NATIVE_PAGE_TOTAL")
        if type(self.report) is not NativeLoadReportPublicationV1:
            _fail("native V2 publication report is invalid", "NATIVE_PUBLICATION_REPORT")
        for name in (
            "root_table_sha256",
            "effective_root_table_sha256",
            "fingerprint_inputs_sha256",
            "source_manifest_sha256",
            "provenance_manifest_sha256",
            "effective_origin_manifest_sha256",
        ):
            _require_digest(name, getattr(self, name))
        if self.owl2_dl_report_summary is not None and (
            type(self.owl2_dl_report_summary) is not NativeOWL2DLReportSummaryV2
        ):
            _fail("native V2 OWL2-DL report summary is invalid", "NATIVE_OWL2_DL_REPORT")
        _validate_exact_publication_metadata_v2(
            documents,
            self.import_manifest,
            self.root_document_key,
            self.load_options,
            diagnostics,
            self.report,
            self.capability_bits,
        )
        _validate_exact_diagnostic_reference_sidecars_v2(self.diagnostic_reference_sidecars)
        _validate_exact_facade_cardinality_summary_v2(self.facade_cardinality_summary)
        _validate_exact_owl2_summary_v2(self.owl2_dl_report_summary, optional=True)
        _validate_max_facade_row_bytes_v2(self.max_facade_row_bytes, self.load_options)
        owner_attestation = self.handle.attestation
        content_digests = NativeSnapshotContentDigestsV2(
            root_table_sha256=owner_attestation.root_table_sha256,
            effective_root_table_sha256=owner_attestation.effective_root_table_sha256,
            fingerprint_inputs_sha256=owner_attestation.fingerprint_inputs_sha256,
            source_manifest_sha256=owner_attestation.source_manifest_sha256,
            provenance_manifest_sha256=owner_attestation.provenance_manifest_sha256,
            effective_origin_manifest_sha256=(owner_attestation.effective_origin_manifest_sha256),
        )
        if any(
            getattr(self, item.name) != getattr(content_digests, item.name)
            for item in fields(content_digests)
        ):
            _fail(
                "native V2 envelope content digests diverge from the owner",
                "NATIVE_CONTENT_MANIFEST",
            )
        expected = native_snapshot_publication_attestation_v2(
            documents=documents,
            import_manifest=self.import_manifest,
            root_document_key=self.root_document_key,
            load_options=self.load_options,
            diagnostics=diagnostics,
            diagnostic_reference_sidecars=self.diagnostic_reference_sidecars,
            facade_cardinality_summary=self.facade_cardinality_summary,
            report=self.report,
            capability_bits=self.capability_bits,
            content_digests=content_digests,
            max_facade_row_bytes=self.max_facade_row_bytes,
            owl2_dl_report_summary=self.owl2_dl_report_summary,
        )
        _validate_handle_v2(self.handle, expected)
        _validate_generated_fixture_binding_v2(
            self.handle,
            documents,
            self.report,
            self.owl2_dl_report_summary,
            self.facade_cardinality_summary,
            content_digests,
        )
        self.handle._bind_publication_v2(
            documents,
            self.report,
            self.load_options,
            self.owl2_dl_report_summary,
            self.facade_cardinality_summary,
        )
        object.__setattr__(self, "documents", documents)
        object.__setattr__(self, "diagnostics", diagnostics)


def freeze_native_snapshot_publication_v2(
    fields_value: Mapping[str, object],
) -> NativeSnapshotPublicationV2:
    if not isinstance(fields_value, Mapping):
        raise TypeError("native V2 publication fields must be a mapping")
    copied_fields = dict(fields_value)
    if not all(type(name) is str for name in copied_fields):
        _fail("native V2 field names must be strings", "NATIVE_PUBLICATION_FIELDS")
    expected = tuple(row[1] for row in NATIVE_SNAPSHOT_PUBLICATION_FIELDS_V2)
    if set(copied_fields) != set(expected):
        missing = [name for name in expected if name not in copied_fields]
        unknown = [name for name in copied_fields if name not in expected]
        _fail(
            f"native V2 fields do not match (missing={missing!r}, unknown={unknown!r})",
            "NATIVE_PUBLICATION_FIELDS",
        )
    return NativeSnapshotPublicationV2(**cast(Any, copied_fields))


def require_native_facade_publication_v2(value: object) -> NativeSnapshotPublicationV2:
    """Require the exact V2 paged surface; V1 is metadata-only legacy input."""

    if type(value) is not NativeSnapshotPublicationV2:
        raise BackendProtocolError(
            "native facade dispatch requires an exact V2 paged publication",
            code="NATIVE_FACADE_V2_REQUIRED",
        )
    return value


def _validate_max_facade_row_bytes_v2(value: int, options: LoadOptions) -> None:
    _require_positive_u64_v2("max_facade_row_bytes", value)
    for name in _FACADE_ROW_BUDGET_FIELDS_V2:
        allowed = cast(int, getattr(options.limits, name))
        if value > allowed:
            _fail(
                f"max_facade_row_bytes exceeds configured {name}",
                "NATIVE_PUBLICATION_LIMIT",
            )
    if options.limits.max_memory_bytes is not None and value > options.limits.max_memory_bytes:
        _fail(
            "max_facade_row_bytes exceeds configured max_memory_bytes",
            "NATIVE_PUBLICATION_LIMIT",
        )


def native_snapshot_publication_attestation_v2(
    *,
    documents: tuple[NativeDocumentPublicationV1, ...],
    import_manifest: NativeImportManifestPublicationV1,
    root_document_key: str,
    load_options: LoadOptions,
    diagnostics: tuple[NativeDiagnosticPublicationV1, ...],
    diagnostic_reference_sidecars: NativeDiagnosticReferenceSidecarsV2,
    facade_cardinality_summary: NativeFacadeCardinalitySummaryV2,
    report: NativeLoadReportPublicationV1,
    capability_bits: int,
    content_digests: NativeSnapshotContentDigestsV2,
    max_facade_row_bytes: int,
    owl2_dl_report_summary: NativeOWL2DLReportSummaryV2 | None,
) -> NativeSnapshotAttestationV2:
    _validate_exact_publication_metadata_v2(
        documents,
        import_manifest,
        root_document_key,
        load_options,
        diagnostics,
        report,
        capability_bits,
    )
    if type(content_digests) is not NativeSnapshotContentDigestsV2:
        raise TypeError("content_digests must be an exact NativeSnapshotContentDigestsV2")
    root_table_sha256 = content_digests.root_table_sha256
    fingerprint_inputs_sha256 = content_digests.fingerprint_inputs_sha256
    source_manifest_sha256 = content_digests.source_manifest_sha256
    provenance_manifest_sha256 = content_digests.provenance_manifest_sha256
    _validate_publication_alignment(
        documents,
        import_manifest,
        root_document_key,
        load_options,
        diagnostics,
        report,
        capability_bits,
        allow_auto_backend=True,
    )
    diagnostic_reference_kinds_sha256 = _diagnostic_reference_kinds_sha256_v2(
        diagnostic_reference_sidecars,
        diagnostics,
        documents,
        import_manifest,
    )
    metadata_diagnostic_count = (
        len(diagnostics)
        + sum(len(item.diagnostics) for item in documents)
        + sum(item.diagnostic is not None for item in import_manifest.edges)
    )
    facade_cardinality_summary_sha256 = _facade_cardinality_summary_sha256_v2(
        facade_cardinality_summary,
        documents,
        report,
        capability_bits=capability_bits,
        load_options=load_options,
        metadata_diagnostic_count=metadata_diagnostic_count,
        owl2_dl_report_summary=owl2_dl_report_summary,
    )
    _validate_max_facade_row_bytes_v2(max_facade_row_bytes, load_options)
    annotation_count = _checked_sum(
        "ontology annotation count", (item.ontology_annotation_count for item in documents)
    )
    stored_axiom_count = _checked_sum(
        "stored axiom count", (item.axiom_count for item in documents)
    )
    extension_count = _checked_sum("extension count", (item.extension_count for item in documents))
    source_map_count = _checked_sum(
        "source-map count", (item.source_map_entry_count for item in documents)
    )
    origin_count = _checked_sum("origin count", (item.origin_entry_count for item in documents))
    rdf_count = sum(item.rdf_mapping_report_sha256 is not None for item in documents)
    diagnostic_count = metadata_diagnostic_count
    _require_nonnegative_u64("diagnostic_count", diagnostic_count)
    metadata_sha256 = _metadata_manifest_sha256_v2(
        documents,
        import_manifest,
        root_document_key,
        load_options,
        diagnostics,
        diagnostic_reference_sidecars,
        facade_cardinality_summary,
        report,
        capability_bits,
        root_table_sha256,
        content_digests.effective_root_table_sha256,
        fingerprint_inputs_sha256,
        source_manifest_sha256,
        provenance_manifest_sha256,
        content_digests.effective_origin_manifest_sha256,
        max_facade_row_bytes,
        owl2_dl_report_summary,
    )
    return NativeSnapshotAttestationV2(
        version=NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2,
        ledger_sha256=NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2,
        metadata_manifest_sha256=metadata_sha256,
        facade_access_schema_sha256=NATIVE_FACADE_ACCESS_SCHEMA_SHA256_V2,
        auxiliary_codec_schema_sha256=NATIVE_AUXILIARY_CODEC_SCHEMA_SHA256_V2,
        root_table_sha256=root_table_sha256,
        effective_root_table_sha256=content_digests.effective_root_table_sha256,
        fingerprint_inputs_sha256=fingerprint_inputs_sha256,
        source_manifest_sha256=source_manifest_sha256,
        provenance_manifest_sha256=provenance_manifest_sha256,
        effective_origin_manifest_sha256=content_digests.effective_origin_manifest_sha256,
        diagnostics_manifest_sha256=_diagnostics_manifest_sha256(
            diagnostics, documents, import_manifest
        ),
        diagnostic_reference_kinds_sha256=diagnostic_reference_kinds_sha256,
        facade_cardinality_summary_sha256=facade_cardinality_summary_sha256,
        load_options_sha256=hashlib.sha256(_load_options_bytes_v2(load_options)).digest(),
        report_sha256=hashlib.sha256(_report_bytes_v1(report)).digest(),
        max_facade_row_bytes=max_facade_row_bytes,
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
        rdf_mapping_report_count=rdf_count,
        capability_bits=capability_bits,
        api_version=report.api_version,
        model_schema=report.model_schema,
        backend=report.backend,
        root_document_key=root_document_key,
        owl2_dl_report_summary=owl2_dl_report_summary,
        owl2_dl_validated=report.owl2_dl_validated,
        owl2_dl_conforms=report.owl2_dl_conforms,
        owl2_dl_report_sha256=report.owl2_dl_report_sha256,
    )


def native_snapshot_attestation_bytes_v2(attestation: NativeSnapshotAttestationV2) -> bytes:
    if type(attestation) is not NativeSnapshotAttestationV2:
        raise TypeError("attestation must be NativeSnapshotAttestationV2")
    values = tuple(
        _native_attestation_field_value_v2(attestation, row[1])
        for row in NATIVE_SNAPSHOT_ATTESTATION_FIELDS_V2
    )
    return NATIVE_SNAPSHOT_ATTESTATION_DOMAIN_V2.encode("ascii") + b"\x00" + _sequence_bytes(values)


def _native_attestation_field_value_v2(
    attestation: NativeSnapshotAttestationV2,
    name: str,
) -> object:
    if name == "owl2_dl_report_summary":
        return _owl2_dl_summary_values_v2(attestation.owl2_dl_report_summary)
    return getattr(attestation, name)


def _metadata_manifest_sha256_v2(
    documents: tuple[NativeDocumentPublicationV1, ...],
    manifest: NativeImportManifestPublicationV1,
    root_document_key: str,
    options: LoadOptions,
    diagnostics: tuple[NativeDiagnosticPublicationV1, ...],
    diagnostic_reference_sidecars: NativeDiagnosticReferenceSidecarsV2,
    facade_cardinality_summary: NativeFacadeCardinalitySummaryV2,
    report: NativeLoadReportPublicationV1,
    capability_bits: int,
    root_table_sha256: bytes,
    effective_root_table_sha256: bytes,
    fingerprint_inputs_sha256: bytes,
    source_manifest_sha256: bytes,
    provenance_manifest_sha256: bytes,
    effective_origin_manifest_sha256: bytes,
    max_facade_row_bytes: int,
    owl2_dl_report_summary: NativeOWL2DLReportSummaryV2 | None,
) -> bytes:
    document_values = tuple(_document_metadata_values_v2(item) for item in documents)
    manifest_values = (
        manifest.policy,
        manifest.offline,
        manifest.resolver_configuration_fingerprint,
        tuple(
            (
                item.document_key,
                item.ontology_id,
                item.document_iri,
                item.source_sha256,
                item.document_fingerprint,
                item.format,
                item.status,
            )
            for item in manifest.documents
        ),
        tuple(_import_edge_metadata_values_v2(item) for item in manifest.edges),
    )
    report_values = tuple(
        getattr(report, row[1]) for row in NATIVE_LOAD_REPORT_PUBLICATION_FIELDS_V1
    )
    values = (
        document_values,
        manifest_values,
        root_document_key,
        hashlib.sha256(_load_options_bytes_v2(options)).digest(),
        diagnostics,
        _diagnostic_reference_sidecar_values_v2(diagnostic_reference_sidecars),
        _facade_cardinality_summary_values_v2(facade_cardinality_summary),
        report_values,
        capability_bits,
        root_table_sha256,
        effective_root_table_sha256,
        fingerprint_inputs_sha256,
        source_manifest_sha256,
        provenance_manifest_sha256,
        effective_origin_manifest_sha256,
        max_facade_row_bytes,
        _owl2_dl_summary_values_v2(owl2_dl_report_summary),
    )
    body = b"pyowl-core:native-publication-metadata-manifest:v2\x00" + _sequence_bytes(values)
    return hashlib.sha256(body).digest()


def _document_metadata_values_v2(document: NativeDocumentPublicationV1) -> tuple[object, ...]:
    provenance = document.provenance
    provenance_values = tuple(
        getattr(provenance, row[1]) for row in NATIVE_PROVENANCE_PUBLICATION_FIELDS_V1
    )
    return (
        document.document_key,
        document.ontology_id,
        document.document_iri,
        document.direct_imports,
        provenance_values,
        document.document_fingerprint,
        document.diagnostics,
        document.ontology_annotation_count,
        document.axiom_count,
        document.extension_count,
        document.source_map_entry_count,
        document.origin_entry_count,
        document.rdf_mapping_conformant,
        document.rdf_mapping_report_sha256,
    )


def _import_edge_metadata_values_v2(edge: NativeImportEdgePublicationV1) -> tuple[object, ...]:
    return tuple(getattr(edge, row[1]) for row in NATIVE_IMPORT_EDGE_FIELDS_V1)


def _validate_handle_v2(
    handle: NativeSnapshotHandleV2, expected: NativeSnapshotAttestationV2
) -> None:
    try:
        closed = handle.closed
        attestation = handle.attestation
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        raise BackendProtocolError(
            "native V2 snapshot handle failed during publication", code="NATIVE_HANDLE_OWNER"
        ) from error
    if closed:
        _fail("native V2 publication handle is closed", "NATIVE_HANDLE_LIFECYCLE")
    if attestation != expected:
        _fail("native V2 owner attestation does not match", "NATIVE_ATTESTATION_MISMATCH")
    counters = handle._facade_counters_v2()
    for name in (
        "publication_structural_rows_copied",
        "publication_structural_bytes_copied",
        "page_requests",
        "pages_returned",
        "rows_emitted",
        "payload_bytes_copied",
        "canonical_payload_bytes_copied",
        "auxiliary_payload_bytes_copied",
        "contains_requests",
        "contains_hits",
        "ontology_annotation_rows_emitted",
        "axiom_rows_emitted",
        "extension_rows_emitted",
        "signature_rows_emitted",
        "source_map_rows_emitted",
        "source_prefix_rows_emitted",
        "origin_rows_emitted",
        "rdf_header_rows_emitted",
        "rdf_triple_rows_emitted",
        "rdf_rule_rows_emitted",
        "rdf_diagnostic_rows_emitted",
        "owl2_dl_structural_issue_rows_emitted",
        "owl2_dl_issue_rows_emitted",
        "owl2_dl_role_property_rows_emitted",
        "owl2_dl_role_hierarchy_rows_emitted",
        "owl2_dl_role_composite_rows_emitted",
        "owl2_dl_role_non_simple_rows_emitted",
        "canonical_encode_requests",
        "canonical_encode_cache_hits",
        "facade_cache_hits",
        "facade_cache_misses",
        "facade_cache_evictions",
        "close_requests",
        "close_transitions",
        "encoded_view_requests",
        "wire_encode_requests",
        "wire_decode_requests",
        "base_flatten_requests",
    ):
        if getattr(counters, name) != 0:
            _fail(
                f"native V2 publication counter {name} must initially be zero",
                "NATIVE_PUBLICATION_COUNTER",
            )


def _validate_generated_fixture_binding_v2(
    handle: NativeSnapshotHandleV2,
    documents: tuple[NativeDocumentPublicationV1, ...],
    report: NativeLoadReportPublicationV1,
    owl2_dl_report_summary: NativeOWL2DLReportSummaryV2 | None,
    facade_cardinality_summary: NativeFacadeCardinalitySummaryV2,
    content_digests: NativeSnapshotContentDigestsV2,
) -> None:
    owner = object.__getattribute__(handle, "_owner_v2")
    if type(owner) is not _GeneratedNativeSnapshotOwnerV2:
        return
    fixture = cast(_GeneratedFacadeFixtureV2, tuple.__getitem__(owner, 2))
    binding = fixture._binding
    expected_snapshot = FrozenMap(
        _known_publication_totals_v2(
            documents,
            report,
            owl2_dl_report_summary,
            facade_cardinality_summary,
            raw_document_owner=False,
        )
    )
    expected_document = FrozenMap(
        _known_publication_totals_v2(
            documents,
            report,
            owl2_dl_report_summary,
            facade_cardinality_summary,
            raw_document_owner=True,
        )
    )
    if (
        binding.content_digests != content_digests
        or binding.snapshot_totals != expected_snapshot
        or binding.document_totals != expected_document
    ):
        _fail(
            "generated V2 fixture binding diverges from envelope metadata",
            "NATIVE_CONTENT_MANIFEST",
        )


def _bound_v2(name: str) -> int:
    return NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2[name]


__all__ = [
    "NATIVE_AUXILIARY_CODEC_DOMAIN_V2",
    "NATIVE_AUXILIARY_CODEC_SCHEMA_SHA256_V2",
    "NATIVE_CLOSURE_FACADE_CARDINALITIES_FIELDS_V2",
    "NATIVE_DIAGNOSTIC_REFERENCE_KINDS_DOMAIN_V2",
    "NATIVE_DIAGNOSTIC_REFERENCE_KINDS_FIELDS_V2",
    "NATIVE_DIAGNOSTIC_REFERENCE_SIDECARS_FIELDS_V2",
    "NATIVE_DOCUMENT_FACADE_CARDINALITIES_FIELDS_V2",
    "NATIVE_DOCUMENT_HANDLE_MEMBERS_V2",
    "NATIVE_DOCUMENT_ORIGIN_TABLE_DOMAIN_V2",
    "NATIVE_DOCUMENT_ROOT_TABLE_DOMAIN_V2",
    "NATIVE_DOCUMENT_SOURCE_TABLE_DOMAIN_V2",
    "NATIVE_EFFECTIVE_CLOSURE_ORIGIN_TABLE_DOMAIN_V2",
    "NATIVE_EFFECTIVE_DOCUMENT_ORIGIN_TABLE_DOMAIN_V2",
    "NATIVE_EFFECTIVE_DOCUMENT_ROOT_TABLE_DOMAIN_V2",
    "NATIVE_EFFECTIVE_ORIGIN_MANIFEST_DOMAIN_V2",
    "NATIVE_EFFECTIVE_ROOT_TABLE_MANIFEST_DOMAIN_V2",
    "NATIVE_FACADE_ACCESS_DOMAIN_V2",
    "NATIVE_FACADE_ACCESS_SCHEMA_SHA256_V2",
    "NATIVE_FACADE_CARDINALITY_SUMMARY_DOMAIN_V2",
    "NATIVE_FACADE_CARDINALITY_SUMMARY_FIELDS_V2",
    "NATIVE_FACADE_CONTAINS_REQUEST_FIELDS_V2",
    "NATIVE_FACADE_COUNTER_FIELDS_V2",
    "NATIVE_FACADE_PAGE_FIELDS_V2",
    "NATIVE_FACADE_PAGE_REQUEST_FIELDS_V2",
    "NATIVE_FINGERPRINT_EVIDENCE_FIELDS_V2",
    "NATIVE_FINGERPRINT_INPUTS_MANIFEST_DOMAIN_V2",
    "NATIVE_LOAD_OPTION_FIELDS_V2",
    "NATIVE_ORIGIN_ROW_FIELDS_V2",
    "NATIVE_OWL2_DL_ISSUE_ROW_FIELDS_V2",
    "NATIVE_OWL2_DL_REPORT_DOMAIN_V2",
    "NATIVE_OWL2_DL_REPORT_SUMMARY_FIELDS_V2",
    "NATIVE_OWL2_DL_ROLE_EDGE_ROW_FIELDS_V2",
    "NATIVE_PROVENANCE_MANIFEST_DOMAIN_V2",
    "NATIVE_PYTHON_FACADE_COUNTER_FIELDS_V2",
    "NATIVE_RDF_DIAGNOSTIC_ROW_FIELDS_V2",
    "NATIVE_RDF_MAPPING_REPORT_DOMAIN_V2",
    "NATIVE_RDF_REPORT_HEADER_ROW_FIELDS_V2",
    "NATIVE_RDF_RULE_ROW_FIELDS_V2",
    "NATIVE_RDF_TRIPLE_ROW_FIELDS_V2",
    "NATIVE_ROOT_TABLE_MANIFEST_DOMAIN_V2",
    "NATIVE_SNAPSHOT_ATTESTATION_DOMAIN_V2",
    "NATIVE_SNAPSHOT_ATTESTATION_FIELDS_V2",
    "NATIVE_SNAPSHOT_HANDLE_MEMBERS_V2",
    "NATIVE_SNAPSHOT_LEDGER_CANONICALIZATION_V2",
    "NATIVE_SNAPSHOT_LIFECYCLE_V2",
    "NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2",
    "NATIVE_SNAPSHOT_PUBLICATION_FIELDS_V2",
    "NATIVE_SNAPSHOT_PUBLICATION_LEDGER_DOMAIN_V2",
    "NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2",
    "NATIVE_SNAPSHOT_PUBLICATION_VERSION_V2",
    "NATIVE_SNAPSHOT_RUST_PARITY_REQUIRED_V2",
    "NATIVE_SOURCE_MANIFEST_DOMAIN_V2",
    "NATIVE_SOURCE_MAP_ROW_FIELDS_V2",
    "NATIVE_SOURCE_PREFIX_ROW_FIELDS_V2",
    "NativeClosureFacadeCardinalitiesV2",
    "NativeDiagnosticReferenceKindV2",
    "NativeDiagnosticReferenceKindsV2",
    "NativeDiagnosticReferenceSidecarsV2",
    "NativeDocumentFacadeCardinalitiesV2",
    "NativeDocumentHandleV2",
    "NativeFacadeCardinalitySummaryV2",
    "NativeFacadeCollectionV2",
    "NativeFacadeContainsRequestV2",
    "NativeFacadeCountersV2",
    "NativeFacadePageRequestV2",
    "NativeFacadePageV2",
    "NativeFacadeScopeV2",
    "NativeFingerprintEvidenceV2",
    "NativeOWL2DLIssueRowV2",
    "NativeOWL2DLReportSummaryV2",
    "NativeOWL2DLRoleEdgeRowV2",
    "NativeOWL2DLStructuralIssueRowV2",
    "NativeOriginRowV2",
    "NativePythonFacadeCountersV2",
    "NativeRDFDiagnosticRowV2",
    "NativeRDFReportHeaderRowV2",
    "NativeRDFRuleRowV2",
    "NativeRDFTripleRowV2",
    "NativeSignatureKindV2",
    "NativeSnapshotAttestationV2",
    "NativeSnapshotContentDigestsV2",
    "NativeSnapshotHandleV2",
    "NativeSnapshotPublicationV2",
    "NativeSourceMapRowV2",
    "NativeSourcePrefixRowV2",
    "_load_options_bytes_v2",
    "decode_native_auxiliary_row_v2",
    "encode_native_auxiliary_row_v2",
    "freeze_native_snapshot_publication_v2",
    "native_auxiliary_codec_schema_semantics_v2",
    "native_content_manifest_schema_semantics_v2",
    "native_counter_schema_semantics_v2",
    "native_diagnostic_reference_kinds_v2",
    "native_facade_access_schema_semantics_v2",
    "native_snapshot_attestation_bytes_v2",
    "native_snapshot_content_digests_v2",
    "native_snapshot_publication_attestation_v2",
    "native_snapshot_publication_ledger_bytes_v2",
    "native_snapshot_publication_schema_semantics_v2",
    "require_native_facade_publication_v2",
]
