from __future__ import annotations

import builtins
import copy
import hashlib
import pickle
from dataclasses import FrozenInstanceError, fields, replace
from typing import Any, cast

import pytest

from pyowl_core.backends.native_handoff import (
    NATIVE_DIAGNOSTIC_PUBLICATION_FIELDS_V1,
    NATIVE_DOCUMENT_PUBLICATION_FIELDS_V1,
    NATIVE_LOAD_REPORT_PUBLICATION_FIELDS_V1,
    NATIVE_PROVENANCE_PUBLICATION_FIELDS_V1,
    NATIVE_SNAPSHOT_ATTESTATION_FIELDS_V1,
    NATIVE_SNAPSHOT_HANDLE_MEMBERS_V1,
    NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1,
    NATIVE_SNAPSHOT_PUBLICATION_FIELDS_V1,
    NativeDiagnosticPublicationV1,
    NativeDocumentProvenancePublicationV1,
    NativeDocumentPublicationV1,
    NativeLoadReportPublicationV1,
    NativeSnapshotAttestationV1,
    NativeSnapshotHandleV1,
    NativeSnapshotPublicationV1,
    _register_rust_native_snapshot_handle_v1,
    freeze_native_diagnostic_publication_v1,
    freeze_native_provenance_publication_v1,
    freeze_native_snapshot_publication_v1,
)
from pyowl_core.config import BackendPreference, LoadOptions
from pyowl_core.diagnostics import Diagnostic, Severity
from pyowl_core.document.provenance import DocumentProvenance
from pyowl_core.exceptions import BackendProtocolError, InvalidIRIError
from pyowl_core.model import IRI

from ._support import generated_handle, publication_fields, reattest_fields


def _publication() -> NativeSnapshotPublicationV1:
    return freeze_native_snapshot_publication_v1(publication_fields())


def test_exact_named_fields_freeze_one_sealed_owner_and_immutable_metadata() -> None:
    values = publication_fields()
    publication = freeze_native_snapshot_publication_v1(values)

    for value, ledger in (
        (publication, NATIVE_SNAPSHOT_PUBLICATION_FIELDS_V1),
        (publication.documents[0], NATIVE_DOCUMENT_PUBLICATION_FIELDS_V1),
        (publication.documents[0].provenance, NATIVE_PROVENANCE_PUBLICATION_FIELDS_V1),
        (publication.diagnostics[0], NATIVE_DIAGNOSTIC_PUBLICATION_FIELDS_V1),
        (publication.report, NATIVE_LOAD_REPORT_PUBLICATION_FIELDS_V1),
        (publication.handle.attestation, NATIVE_SNAPSHOT_ATTESTATION_FIELDS_V1),
    ):
        assert tuple(item.name for item in fields(value)) == tuple(row[1] for row in ledger)
    assert publication.handle is values["handle"]
    assert sum(item.name == "handle" for item in fields(publication)) == 1
    assert type(publication.handle) is NativeSnapshotHandleV1

    with pytest.raises(FrozenInstanceError):
        publication.root_document_key = "replacement"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        publication.documents[0].axiom_count = 9  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        publication.import_manifest.offline = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        publication.diagnostics[0].message = "replacement"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        publication.handle.attestation.document_count = 9  # type: ignore[misc]
    assert isinstance(publication.report.timings, tuple)
    assert isinstance(publication.diagnostics[0].details, tuple)


def test_publication_records_are_named_only() -> None:
    publication = _publication()
    values: tuple[tuple[type[object], object], ...] = (
        (NativeSnapshotPublicationV1, publication),
        (NativeDocumentPublicationV1, publication.documents[0]),
        (NativeLoadReportPublicationV1, publication.report),
        (NativeSnapshotAttestationV1, publication.handle.attestation),
    )
    for record_type, value in values:
        with pytest.raises(TypeError):
            cast(Any, record_type)(
                *(getattr(value, item.name) for item in fields(cast(Any, value)))
            )
    with pytest.raises(TypeError):
        NativeSnapshotHandleV1()


@pytest.mark.parametrize("field", tuple(row[1] for row in NATIVE_SNAPSHOT_PUBLICATION_FIELDS_V1))
def test_every_missing_field_fails_closed(field: str) -> None:
    values = publication_fields()
    del values[field]
    with pytest.raises(BackendProtocolError) as raised:
        freeze_native_snapshot_publication_v1(values)
    assert raised.value.code == "NATIVE_PUBLICATION_FIELDS"


def test_unknown_field_version_ledger_and_capability_bits_fail_closed() -> None:
    unknown = publication_fields()
    unknown["parser_state"] = object()
    with pytest.raises(BackendProtocolError) as raised:
        freeze_native_snapshot_publication_v1(unknown)
    assert raised.value.code == "NATIVE_PUBLICATION_FIELDS"

    for field, replacement, code in (
        ("version", 2, "NATIVE_PUBLICATION_VERSION"),
        ("version", True, "NATIVE_PUBLICATION_VERSION"),
        ("ledger_sha256", bytes(32), "NATIVE_PUBLICATION_LEDGER"),
        ("capability_bits", (1 | 2 | 4 | 16) | (1 << 63), "NATIVE_PUBLICATION_CAPABILITY"),
        ("capability_bits", 1 | 2 | 16, "NATIVE_PUBLICATION_CAPABILITY"),
    ):
        values = publication_fields()
        values[field] = replacement
        with pytest.raises((BackendProtocolError, ValueError)) as raised_any:
            freeze_native_snapshot_publication_v1(values)
        if isinstance(raised_any.value, BackendProtocolError):
            assert raised_any.value.code == code


def test_arbitrary_duck_handles_and_subclasses_are_rejected() -> None:
    publication = _publication()

    class DuckHandle:
        publication_version = publication.version
        publication_ledger_sha256 = publication.ledger_sha256
        attestation = publication.handle.attestation
        closed = False

        def close(self) -> None:
            return None

    values = publication_fields()
    values["handle"] = DuckHandle()
    with pytest.raises(BackendProtocolError) as raised:
        freeze_native_snapshot_publication_v1(values)
    assert raised.value.code == "NATIVE_HANDLE_TYPE"

    with pytest.raises(TypeError, match="sealed"):
        type("HostileHandle", (NativeSnapshotHandleV1,), {})

    class _NativeSnapshotHandle:
        __module__ = "pyowl_core._native"

    with pytest.raises(TypeError, match="exact registered extension type"):
        _register_rust_native_snapshot_handle_v1(_NativeSnapshotHandle)


def test_generated_fake_is_bounded_immutable_unpickleable_and_lifecycle_only() -> None:
    values = publication_fields()
    handle = cast(NativeSnapshotHandleV1, values["handle"])
    assert not hasattr(handle, "__dict__")
    assert copy.copy(handle) is handle
    assert copy.deepcopy(handle) is handle
    with pytest.raises(AttributeError):
        handle.axioms = [object()]
    with pytest.raises(AttributeError):
        handle.builder = object()
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(handle)

    owner = object.__getattribute__(handle, "_NativeSnapshotHandleV1__owner")
    assert type(owner).__slots__ == ()
    assert len(owner) == 2
    assert type(owner[0]) is NativeSnapshotAttestationV1
    assert not hasattr(owner, "__dict__")
    with pytest.raises(AttributeError):
        owner.axioms = ()

    assert not handle.closed
    handle.close()
    handle.close()
    assert handle.closed
    with pytest.raises(BackendProtocolError) as raised:
        freeze_native_snapshot_publication_v1(values)
    assert raised.value.code == "NATIVE_HANDLE_LIFECYCLE"


@pytest.mark.parametrize(
    "digest_field",
    (
        "root_table_sha256",
        "fingerprint_inputs_sha256",
        "source_manifest_sha256",
        "provenance_manifest_sha256",
    ),
)
def test_handle_attestation_binds_each_publication_digest(digest_field: str) -> None:
    values = publication_fields()
    values[digest_field] = hashlib.sha256((digest_field + ":tampered").encode()).digest()
    with pytest.raises(BackendProtocolError) as raised:
        freeze_native_snapshot_publication_v1(values)
    assert raised.value.code == "NATIVE_ATTESTATION_MISMATCH"


def test_handle_attestation_binds_options_report_diagnostics_and_counts() -> None:
    changes = []

    values = publication_fields()
    options = cast(LoadOptions, values["load_options"])
    values["load_options"] = replace(options, deterministic=False)
    changes.append(values)

    values = publication_fields()
    report = cast(NativeLoadReportPublicationV1, values["report"])
    values["report"] = replace(
        report,
        structural_fingerprint=replace(
            report.structural_fingerprint,
            digest=hashlib.sha256(b"tampered structural fingerprint").digest(),
        ),
    )
    changes.append(values)

    values = publication_fields()
    diagnostics = cast(tuple[NativeDiagnosticPublicationV1, ...], values["diagnostics"])
    values["diagnostics"] = (replace(diagnostics[0], message="tampered diagnostic"),)
    changes.append(values)

    values = publication_fields()
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    values["documents"] = (replace(documents[0], axiom_count=2),)
    changes.append(values)

    for changed in changes:
        with pytest.raises(BackendProtocolError) as raised:
            freeze_native_snapshot_publication_v1(changed)
        assert raised.value.code == "NATIVE_ATTESTATION_MISMATCH"

    values = publication_fields()
    handle = cast(NativeSnapshotHandleV1, values["handle"])
    values["handle"] = generated_handle(replace(handle.attestation, ledger_sha256=bytes(32)))
    with pytest.raises(BackendProtocolError) as raised:
        freeze_native_snapshot_publication_v1(values)
    assert raised.value.code == "NATIVE_ATTESTATION_MISMATCH"


def test_capability_bits_are_exactly_backed_by_options_and_table_counts() -> None:
    for capability_bits in (1 | 2 | 4, 1 | 2 | 4 | 8 | 16):
        values = publication_fields()
        values["capability_bits"] = capability_bits
        with pytest.raises(BackendProtocolError) as raised:
            freeze_native_snapshot_publication_v1(values)
        assert raised.value.code == "NATIVE_PUBLICATION_CAPABILITY"

    values = publication_fields()
    options = cast(LoadOptions, values["load_options"])
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    values["load_options"] = replace(options, preserve_source_map=True)
    values["documents"] = (replace(documents[0], source_map_entry_count=1),)
    values["capability_bits"] = cast(int, values["capability_bits"]) | 8
    reattest_fields(values)
    assert (
        freeze_native_snapshot_publication_v1(values).handle.attestation.source_map_entry_count == 1
    )

    values = publication_fields()
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    values["documents"] = (
        replace(
            documents[0],
            rdf_mapping_conformant=True,
            rdf_mapping_report_sha256=hashlib.sha256(b"rdf mapping report").digest(),
        ),
    )
    with pytest.raises(BackendProtocolError) as raised:
        freeze_native_snapshot_publication_v1(values)
    assert raised.value.code == "NATIVE_PUBLICATION_CAPABILITY"


def test_auto_backend_and_inconsistent_owl_validation_claims_are_rejected() -> None:
    values = publication_fields()
    options = cast(LoadOptions, values["load_options"])
    values["load_options"] = replace(options, backend=BackendPreference.AUTO)
    with pytest.raises(BackendProtocolError) as raised:
        freeze_native_snapshot_publication_v1(values)
    assert raised.value.code == "NATIVE_PUBLICATION_OPTIONS"

    values = publication_fields()
    options = cast(LoadOptions, values["load_options"])
    values["load_options"] = replace(options, validate_owl2_dl=True)
    with pytest.raises(BackendProtocolError) as raised:
        freeze_native_snapshot_publication_v1(values)
    assert raised.value.code == "NATIVE_PUBLICATION_REPORT"

    values = publication_fields()
    options = cast(LoadOptions, values["load_options"])
    report = cast(NativeLoadReportPublicationV1, values["report"])
    values["load_options"] = replace(options, validate_owl2_dl=True)
    values["report"] = replace(
        report,
        owl2_dl_validated=True,
        owl2_dl_conforms=True,
        owl2_dl_report_sha256=hashlib.sha256(b"OWL 2 DL report").digest(),
    )
    reattest_fields(values)
    assert freeze_native_snapshot_publication_v1(values).report.owl2_dl_conforms is True


def test_diagnostic_and_provenance_conversion_is_scalar_only_deep_frozen_and_bounded() -> None:
    details = {"safe": "value"}
    diagnostic = Diagnostic(
        "SAFE_DIAGNOSTIC",
        Severity.INFO,
        "safe message",
        document_iri=IRI("urn:safe:document"),
        import_chain=(IRI("urn:safe:import"),),
        details=details,
    )
    details["late"] = "mutation"
    frozen = freeze_native_diagnostic_publication_v1(diagnostic)
    assert frozen.document_iri == "urn:safe:document"
    assert frozen.import_chain == ("urn:safe:import",)
    assert frozen.details == (("safe", "value"),)
    with pytest.raises(FrozenInstanceError):
        frozen.message = "mutation"  # type: ignore[misc]
    with pytest.raises(ValueError, match="diagnostic codes"):
        replace(frozen, code="hostile_code")
    with pytest.raises(InvalidIRIError, match="IRI"):
        replace(frozen, document_iri="relative-reference")

    with pytest.raises(TypeError, match="strings or IRI"):
        freeze_native_diagnostic_publication_v1(
            Diagnostic("HOSTILE_REFERENCE", Severity.ERROR, "bad", document_iri=object())
        )
    with pytest.raises(ValueError, match="UTF-8 publication bound"):
        freeze_native_diagnostic_publication_v1(
            Diagnostic(
                "OVERSIZED_MESSAGE",
                Severity.ERROR,
                "x" * (NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1["max_metadata_string_utf8_bytes"] + 1),
            )
        )
    with pytest.raises(ValueError, match="details exceed"):
        replace(
            frozen,
            details=tuple(
                (f"key_{index}", index)
                for index in range(
                    NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1["max_diagnostic_details"] + 1
                )
            ),
        )
    with pytest.raises(ValueError, match="import chain exceeds"):
        replace(
            frozen,
            import_chain=tuple(
                f"urn:chain:{index}"
                for index in range(
                    NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1["max_diagnostic_import_chain"] + 1
                )
            ),
        )

    document = _publication().documents[0]
    with pytest.raises(ValueError, match="u64"):
        replace(document, axiom_count=2**64)

    provenance = publication_fields_provenance_source()
    frozen_provenance = freeze_native_provenance_publication_v1(provenance)
    assert type(frozen_provenance) is NativeDocumentProvenancePublicationV1
    assert isinstance(frozen_provenance.document_iri, str)
    with pytest.raises(ValueError, match="format is invalid"):
        replace(frozen_provenance, format="hostile-format")
    with pytest.raises(ValueError, match="UTF-8 publication bound"):
        freeze_native_provenance_publication_v1(
            replace(
                provenance,
                parser="x"
                * (NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V1["max_metadata_string_utf8_bytes"] + 1),
            )
        )


def publication_fields_provenance_source() -> DocumentProvenance:
    publication = _publication()
    metadata = publication.documents[0].provenance
    source = publication_fields()
    original_document = cast(tuple[NativeDocumentPublicationV1, ...], source["documents"])[0]
    assert original_document.provenance == metadata
    from pyowl_core.config import DocumentFormat
    from pyowl_core.document.provenance import DetectionBasis, DigestKind

    return DocumentProvenance(
        metadata.source_sha256,
        DigestKind(metadata.digest_kind),
        metadata.byte_length,
        metadata.decoded_codepoint_length,
        IRI(metadata.document_iri) if metadata.document_iri is not None else None,
        metadata.acquisition_locator,
        DocumentFormat(metadata.format),
        DetectionBasis(metadata.detection_basis),
        parser=metadata.parser,
        backend=metadata.backend,
        api_version=metadata.api_version,
        model_schema=metadata.model_schema,
    )


def test_publication_validation_performs_no_input_sized_sort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = publication_fields()

    def forbidden_sorted(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("publication validation attempted to sort")

    monkeypatch.setattr(builtins, "sorted", forbidden_sorted)
    assert freeze_native_snapshot_publication_v1(values).report.document_count == 1


def test_report_metadata_bounds_and_claim_consistency_fail_closed() -> None:
    report = _publication().report
    with pytest.raises(ValueError, match="too many timing rows"):
        replace(
            report,
            timings=tuple((f"phase_{index:03d}", 0.0) for index in range(65)),
        )
    with pytest.raises(ValueError, match="canonically ordered"):
        replace(report, timings=(("z", 0.0), ("a", 0.0)))
    with pytest.raises(ValueError, match="finite and nonnegative"):
        replace(report, timings=(("freeze", float("nan")),))

    for changes in (
        {"total_source_bytes": 22},
        {"effective_axiom_count": 2},
        {"resolution_attempts": 0, "acquisition_cache_hits": 1},
    ):
        values = publication_fields()
        current = cast(NativeLoadReportPublicationV1, values["report"])
        values["report"] = cast(Any, replace)(current, **changes)
        with pytest.raises(BackendProtocolError) as raised:
            freeze_native_snapshot_publication_v1(values)
        assert raised.value.code == "NATIVE_PUBLICATION_REPORT"


def test_import_root_and_direct_import_alignment_fail_closed() -> None:
    values = publication_fields()
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    values["documents"] = (replace(documents[0], direct_imports=(IRI("urn:missing:import-edge"),)),)
    with pytest.raises(BackendProtocolError) as raised:
        freeze_native_snapshot_publication_v1(values)
    assert raised.value.code == "NATIVE_PUBLICATION_ALIGNMENT"

    values = publication_fields()
    manifest = cast(Any, values["import_manifest"])
    values["import_manifest"] = replace(
        manifest,
        documents=(replace(manifest.documents[0], status="resolved"),),
    )
    with pytest.raises(BackendProtocolError) as raised:
        freeze_native_snapshot_publication_v1(values)
    assert raised.value.code == "NATIVE_PUBLICATION_ROOT"


def test_envelope_has_no_ontology_sized_or_successor_owned_field() -> None:
    envelope = {item.name for item in fields(NativeSnapshotPublicationV1)}
    document = {item.name for item in fields(NativeDocumentPublicationV1)}
    forbidden = {
        "axioms",
        "annotations",
        "extensions",
        "terms",
        "parser_state",
        "mutable_builder",
        "encoded_view",
        "encoded_view_layout",
        "buffers",
        "capabilities",
        "features",
    }
    assert not envelope & forbidden
    assert not document & forbidden
    assert "axiom_count" in document
    assert "diagnostics" not in {item.name for item in fields(NativeLoadReportPublicationV1)}
    assert "owl2_dl_report" not in {item.name for item in fields(NativeLoadReportPublicationV1)}
    assert tuple(row[1] for row in NATIVE_SNAPSHOT_HANDLE_MEMBERS_V1) == (
        "publication_version",
        "publication_ledger_sha256",
        "attestation",
        "closed",
        "close",
    )
