from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import cast

import pytest

from pyowl_core.backends.native_handoff import (
    NativeDiagnosticPublicationV1,
    freeze_native_diagnostic_publication_v1,
)
from pyowl_core.backends.native_handoff_v2 import (
    NATIVE_DIAGNOSTIC_REFERENCE_KINDS_DOMAIN_V2,
    NativeDiagnosticReferenceKindsV2,
    NativeDiagnosticReferenceKindV2,
    NativeDiagnosticReferenceSidecarsV2,
    NativeFacadeCollectionV2,
    NativeRDFDiagnosticRowV2,
    decode_native_auxiliary_row_v2,
    encode_native_auxiliary_row_v2,
    native_diagnostic_reference_kinds_v2,
)
from pyowl_core.diagnostics import Diagnostic, Severity
from pyowl_core.exceptions import BackendProtocolError
from pyowl_core.model import IRI

from ._support import publication_fields
from ._support_v2 import (
    diagnostic_reference_sidecars,
    publication,
    source_load_row_budget,
)


def _u64(value: int) -> bytes:
    return value.to_bytes(8, "little")


def _frame(value: bytes) -> bytes:
    return _u64(len(value)) + value


def _text(value: str) -> bytes:
    return _frame(value.encode("utf-8"))


def _unresolved_import_diagnostic() -> tuple[
    NativeDiagnosticPublicationV1,
    NativeDiagnosticReferenceKindsV2,
]:
    raw = Diagnostic(
        "UNRESOLVED_IMPORT",
        Severity.ERROR,
        "the imported ontology could not be resolved",
        document_iri=IRI("urn:diagnostic:document"),
        import_chain=(IRI("urn:diagnostic:import"), "urn:diagnostic:text-reference"),
    )
    return (
        freeze_native_diagnostic_publication_v1(raw),
        native_diagnostic_reference_kinds_v2(
            document_reference=cast(IRI, raw.document_iri),
            import_chain=cast(tuple[IRI | str, ...], raw.import_chain),
        ),
    )


def test_rdf_diagnostic_codec_preserves_iri_vs_text_reference_tags() -> None:
    diagnostic, kinds = _unresolved_import_diagnostic()
    row = NativeRDFDiagnosticRowV2(diagnostic=diagnostic, reference_kinds=kinds)
    collection, encoded = encode_native_auxiliary_row_v2(
        row,
        max_row_bytes=source_load_row_budget(),
    )
    assert collection is NativeFacadeCollectionV2.RDF_DIAGNOSTICS
    decoded = decode_native_auxiliary_row_v2(
        collection,
        encoded,
        max_row_bytes=source_load_row_budget(),
    )
    assert decoded == row
    assert type(decoded) is NativeRDFDiagnosticRowV2
    assert decoded.reference_kinds == (
        NativeDiagnosticReferenceKindsV2(
            document_reference_kind=NativeDiagnosticReferenceKindV2.IRI,
            import_chain_kinds=(
                NativeDiagnosticReferenceKindV2.IRI,
                NativeDiagnosticReferenceKindV2.TEXT,
            ),
        )
    )


def test_publication_attests_ordered_diagnostic_reference_sidecars() -> None:
    values = publication_fields()
    diagnostic, kinds = _unresolved_import_diagnostic()
    values["diagnostics"] = (diagnostic,)
    default = diagnostic_reference_sidecars(values)
    sidecars = replace(default, snapshot=(kinds,))
    values["diagnostic_reference_sidecars"] = sidecars
    value = publication(values=values)
    assert value.diagnostic_reference_sidecars == sidecars
    snapshot_kind_row = b"\x01" + _u64(2) + b"\x01\x02"
    document_kind_row = b"\x00" + _u64(0)
    exact_body = (
        _u64(1)
        + _frame(snapshot_kind_row)
        + _u64(1)
        + _text(value.documents[0].document_key)
        + _u64(1)
        + _frame(document_kind_row)
        + _u64(0)
    )
    assert (
        value.handle.attestation.diagnostic_reference_kinds_sha256
        == hashlib.sha256(
            NATIVE_DIAGNOSTIC_REFERENCE_KINDS_DOMAIN_V2.encode("ascii") + b"\x00" + exact_body
        ).digest()
    )

    changed = dict(values)
    changed["diagnostic_reference_sidecars"] = replace(
        sidecars,
        snapshot=(
            replace(
                kinds,
                document_reference_kind=NativeDiagnosticReferenceKindV2.TEXT,
            ),
        ),
    )
    changed_value = publication(values=changed)
    assert (
        changed_value.handle.attestation.diagnostic_reference_kinds_sha256
        != value.handle.attestation.diagnostic_reference_kinds_sha256
    )


def test_diagnostic_reference_tags_reject_unaligned_and_unknown_objects() -> None:
    diagnostic, kinds = _unresolved_import_diagnostic()
    with pytest.raises(TypeError, match="exact IRI or str"):
        native_diagnostic_reference_kinds_v2(
            document_reference=object(),  # type: ignore[arg-type]
            import_chain=(),
        )
    with pytest.raises(ValueError, match="not aligned"):
        NativeRDFDiagnosticRowV2(
            diagnostic=diagnostic,
            reference_kinds=replace(kinds, import_chain_kinds=()),
        )

    values = publication_fields()
    malformed = NativeDiagnosticReferenceSidecarsV2(
        snapshot=(),
        documents=(),
        import_edges=(),
    )
    values["diagnostic_reference_sidecars"] = malformed
    with pytest.raises(BackendProtocolError, match="not aligned"):
        publication(values=values)
