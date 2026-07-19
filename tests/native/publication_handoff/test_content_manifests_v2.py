from __future__ import annotations

import hashlib
from dataclasses import fields, replace
from typing import cast

import pytest

from pyowl_core.backends import native_handoff_v2
from pyowl_core.backends.native_handoff import (
    NativeDocumentPublicationV1,
    NativeImportManifestPublicationV1,
    NativeLoadReportPublicationV1,
)
from pyowl_core.backends.native_handoff_v2 import (
    NATIVE_AUXILIARY_CODEC_SCHEMA_SHA256_V2,
    NATIVE_DOCUMENT_ORIGIN_TABLE_DOMAIN_V2,
    NATIVE_DOCUMENT_ROOT_TABLE_DOMAIN_V2,
    NATIVE_EFFECTIVE_CLOSURE_ORIGIN_TABLE_DOMAIN_V2,
    NATIVE_EFFECTIVE_DOCUMENT_ORIGIN_TABLE_DOMAIN_V2,
    NATIVE_EFFECTIVE_DOCUMENT_ROOT_TABLE_DOMAIN_V2,
    NATIVE_EFFECTIVE_ORIGIN_MANIFEST_DOMAIN_V2,
    NATIVE_EFFECTIVE_ROOT_TABLE_MANIFEST_DOMAIN_V2,
    NATIVE_FACADE_CARDINALITY_SUMMARY_DOMAIN_V2,
    NATIVE_FINGERPRINT_INPUTS_MANIFEST_DOMAIN_V2,
    NATIVE_PROVENANCE_MANIFEST_DOMAIN_V2,
    NATIVE_ROOT_TABLE_MANIFEST_DOMAIN_V2,
    NATIVE_SOURCE_MANIFEST_DOMAIN_V2,
    NativeFacadeCollectionV2,
    NativeFacadeScopeV2,
    NativeOriginRowV2,
    NativeOWL2DLReportSummaryV2,
    NativeSignatureKindV2,
    NativeSnapshotPublicationV2,
    NativeSourceMapRowV2,
    encode_native_auxiliary_row_v2,
    freeze_native_snapshot_publication_v2,
    native_snapshot_content_digests_v2,
)
from pyowl_core.config import LoadOptions
from pyowl_core.exceptions import BackendProtocolError
from pyowl_core.model import IRI, Class, Declaration, canonical_bytes, structural_digest

from ._support import publication_fields
from ._support_v2 import (
    FixtureKey,
    content_digests,
    facade_cardinality_summary,
    fingerprint_evidence,
    fingerprint_preimages,
    fixture_collections,
    publication,
    source_load_row_budget,
)


def _h(domain: str, body: bytes) -> bytes:
    return hashlib.sha256(domain.encode("ascii") + b"\x00" + body).digest()


def _u32(value: int) -> bytes:
    return value.to_bytes(4, "little")


def _u64(value: int) -> bytes:
    return value.to_bytes(8, "little")


def _frame(value: bytes) -> bytes:
    return _u64(len(value)) + value


def _text(value: str) -> bytes:
    return _frame(value.encode("utf-8"))


def _two_document_manifest_fixture(
    *,
    with_source_rows: bool = False,
) -> tuple[
    dict[str, object],
    dict[FixtureKey, tuple[bytes, ...]],
    tuple[bytes, bytes],
]:
    values = publication_fields()
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    first = documents[0]
    second_key = "d1:" + "2" * 64
    second = replace(first, document_key=second_key)
    values["documents"] = (first, second)
    manifest = cast(NativeImportManifestPublicationV1, values["import_manifest"])
    values["import_manifest"] = replace(
        manifest,
        documents=(
            manifest.documents[0],
            replace(
                manifest.documents[0],
                document_key=second_key,
                status="resolved",
            ),
        ),
    )
    report = cast(NativeLoadReportPublicationV1, values["report"])
    values["report"] = replace(
        report,
        document_count=2,
        total_source_bytes=report.total_source_bytes * 2,
        effective_axiom_count=2,
    )
    collections = dict(fixture_collections())
    first_axiom = collections[
        (
            NativeFacadeCollectionV2.AXIOMS,
            NativeFacadeScopeV2.DOCUMENT,
            0,
            NativeSignatureKindV2.ALL,
            True,
        )
    ][0]
    second_axiom_value = Declaration(Class(IRI("urn:manifest:second-document")))
    second_axiom = canonical_bytes(second_axiom_value)
    first_digest = structural_digest(Declaration(Class(IRI("urn:handoff:Class"))))
    second_digest = structural_digest(second_axiom_value)
    _origin_collection, second_origin = encode_native_auxiliary_row_v2(
        NativeOriginRowV2(
            digest=second_digest,
            document_key=second_key,
            occurrence=0,
            span=None,
        ),
        max_row_bytes=source_load_row_budget(values),
    )
    first_origin = collections[
        (
            NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            NativeFacadeScopeV2.DOCUMENT,
            0,
            NativeSignatureKindV2.ALL,
            True,
        )
    ][0]
    collections[
        (
            NativeFacadeCollectionV2.AXIOMS,
            NativeFacadeScopeV2.DOCUMENT,
            1,
            NativeSignatureKindV2.ALL,
            True,
        )
    ] = (second_axiom,)
    collections[
        (
            NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            NativeFacadeScopeV2.DOCUMENT,
            1,
            NativeSignatureKindV2.ALL,
            True,
        )
    ] = (second_origin,)
    collections[
        (
            NativeFacadeCollectionV2.AXIOMS,
            NativeFacadeScopeV2.CLOSURE,
            None,
            NativeSignatureKindV2.ALL,
            True,
        )
    ] = tuple(sorted((first_axiom, second_axiom)))
    collections[
        (
            NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            NativeFacadeScopeV2.CLOSURE,
            None,
            NativeSignatureKindV2.ALL,
            True,
        )
    ] = tuple(sorted((first_origin, second_origin)))
    if with_source_rows:
        values["documents"] = (
            replace(first, source_map_entry_count=1),
            replace(second, source_map_entry_count=1),
        )
        options = cast(LoadOptions, values["load_options"])
        values["load_options"] = replace(options, preserve_source_map=True)
        values["capability_bits"] = cast(int, values["capability_bits"]) | 8
        for ordinal, digest in enumerate((first_digest, second_digest)):
            _source_collection, source_row = encode_native_auxiliary_row_v2(
                NativeSourceMapRowV2(
                    digest=digest,
                    occurrence=0,
                    span=None,
                ),
                max_row_bytes=source_load_row_budget(values),
            )
            collections[
                (
                    NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES,
                    NativeFacadeScopeV2.DOCUMENT,
                    ordinal,
                    NativeSignatureKindV2.ALL,
                    True,
                )
            ] = (source_row,)
    return values, collections, (first_digest, second_digest)


def test_manifest_preimages_match_the_independent_exact_table() -> None:
    value = publication()
    document = value.documents[0]
    collections = fixture_collections()
    axiom_rows = collections[
        (
            NativeFacadeCollectionV2.AXIOMS,
            NativeFacadeScopeV2.DOCUMENT,
            0,
            NativeSignatureKindV2.ALL,
            True,
        )
    ]
    origin_rows = collections[
        (
            NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            NativeFacadeScopeV2.DOCUMENT,
            0,
            NativeSignatureKindV2.ALL,
            True,
        )
    ]

    document_root_body = (
        _text(document.document_key)
        + b"\x01"
        + _u64(0)
        + b"\x02"
        + _u64(1)
        + _frame(axiom_rows[0])
        + b"\x03"
        + _u64(0)
    )
    document_root = _h(NATIVE_DOCUMENT_ROOT_TABLE_DOMAIN_V2, document_root_body)
    root_body = (
        _u32(value.report.model_schema)
        + _u64(1)
        + _text(document.document_key)
        + _u64(0)
        + _u64(1)
        + _u64(0)
        + document_root
    )
    assert value.root_table_sha256 == _h(NATIVE_ROOT_TABLE_MANIFEST_DOMAIN_V2, root_body)

    effective_document_root = _h(
        NATIVE_EFFECTIVE_DOCUMENT_ROOT_TABLE_DOMAIN_V2,
        document_root_body,
    )
    effective_root_body = (
        _u32(value.report.model_schema)
        + _u64(1)
        + _text(document.document_key)
        + _u64(0)
        + _u64(1)
        + _u64(0)
        + effective_document_root
    )
    assert value.effective_root_table_sha256 == _h(
        NATIVE_EFFECTIVE_ROOT_TABLE_MANIFEST_DOMAIN_V2,
        effective_root_body,
    )

    evidence = fingerprint_evidence(publication_fields())
    fingerprint_body = bytearray(
        _u32(value.report.model_schema)
        + _text(value.root_document_key)
        + _u64(len(value.documents))
    )
    for item in evidence:
        fingerprint_body.extend(bytes((item.tag,)))
        if item.document_key is not None:
            fingerprint_body.extend(_text(item.document_key))
        fingerprint_body.extend(_u64(item.preimage_byte_length))
        fingerprint_body.extend(_u32(item.fingerprint_schema))
        fingerprint_body.extend(item.digest)
    assert value.fingerprint_inputs_sha256 == _h(
        NATIVE_FINGERPRINT_INPUTS_MANIFEST_DOMAIN_V2,
        bytes(fingerprint_body),
    )

    source_body = (
        NATIVE_AUXILIARY_CODEC_SCHEMA_SHA256_V2 + _u64(1) + _text(document.document_key) + b"\x00"
    )
    assert value.source_manifest_sha256 == _h(NATIVE_SOURCE_MANIFEST_DOMAIN_V2, source_body)

    origin_body = _text(document.document_key) + _u64(1) + _frame(origin_rows[0])
    origin_digest = _h(NATIVE_DOCUMENT_ORIGIN_TABLE_DOMAIN_V2, origin_body)
    provenance_body = (
        NATIVE_AUXILIARY_CODEC_SCHEMA_SHA256_V2
        + _u64(1)
        + _text(document.document_key)
        + b"\x01"
        + _u64(1)
        + origin_digest
        + b"\x00"
    )
    assert value.provenance_manifest_sha256 == _h(
        NATIVE_PROVENANCE_MANIFEST_DOMAIN_V2,
        provenance_body,
    )

    effective_document_origin = _h(
        NATIVE_EFFECTIVE_DOCUMENT_ORIGIN_TABLE_DOMAIN_V2,
        origin_body,
    )
    effective_closure_origin = _h(
        NATIVE_EFFECTIVE_CLOSURE_ORIGIN_TABLE_DOMAIN_V2,
        _u64(1) + _frame(origin_rows[0]),
    )
    effective_origin_body = (
        NATIVE_AUXILIARY_CODEC_SCHEMA_SHA256_V2
        + _u64(1)
        + _text(document.document_key)
        + _u64(1)
        + effective_document_origin
        + _u64(1)
        + effective_closure_origin
    )
    assert value.effective_origin_manifest_sha256 == _h(
        NATIVE_EFFECTIVE_ORIGIN_MANIFEST_DOMAIN_V2,
        effective_origin_body,
    )

    summary = value.facade_cardinality_summary
    summary_body = (
        _u64(1)
        + _text(document.document_key)
        + b"".join(
            _u64(item)
            for item in (
                summary.documents[0].effective_annotation_count,
                summary.documents[0].effective_axiom_count,
                summary.documents[0].effective_extension_count,
                summary.documents[0].effective_origin_count,
                summary.documents[0].raw_source_prefix_count,
                summary.documents[0].rdf_unconsumed_triple_count,
                summary.documents[0].rdf_rule_count,
                summary.documents[0].rdf_diagnostic_count,
                summary.closure.effective_annotation_count,
                summary.closure.effective_axiom_count,
                summary.closure.effective_extension_count,
                summary.closure.effective_origin_count,
            )
        )
    )
    assert value.handle.attestation.facade_cardinality_summary_sha256 == _h(
        NATIVE_FACADE_CARDINALITY_SUMMARY_DOMAIN_V2,
        summary_body,
    )


def test_each_retained_structural_row_and_origin_changes_bound_manifests() -> None:
    values = publication_fields()
    evidence = fingerprint_evidence(values)
    original_collections = fixture_collections()
    original = content_digests(values, original_collections, evidence)
    changed_collections: dict[FixtureKey, tuple[bytes, ...]] = dict(original_collections)
    replacement = Declaration(Class(IRI("urn:handoff:Replacement")))
    replacement_row = canonical_bytes(replacement)
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    _collection, origin = encode_native_auxiliary_row_v2(
        NativeOriginRowV2(
            digest=structural_digest(replacement),
            document_key=documents[0].document_key,
            occurrence=0,
            span=None,
        ),
        max_row_bytes=source_load_row_budget(values),
    )
    for scope, ordinal in (
        (NativeFacadeScopeV2.DOCUMENT, 0),
        (NativeFacadeScopeV2.CLOSURE, None),
    ):
        changed_collections[
            (
                NativeFacadeCollectionV2.AXIOMS,
                scope,
                ordinal,
                NativeSignatureKindV2.ALL,
                True,
            )
        ] = (replacement_row,)
    changed_collections[
        (
            NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            NativeFacadeScopeV2.DOCUMENT,
            0,
            NativeSignatureKindV2.ALL,
            True,
        )
    ] = (origin,)
    changed_collections[
        (
            NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            NativeFacadeScopeV2.CLOSURE,
            None,
            NativeSignatureKindV2.ALL,
            True,
        )
    ] = (origin,)
    changed = content_digests(values, changed_collections, evidence)
    assert changed.root_table_sha256 != original.root_table_sha256
    assert changed.provenance_manifest_sha256 != original.provenance_manifest_sha256
    assert changed.fingerprint_inputs_sha256 == original.fingerprint_inputs_sha256


def test_source_presence_is_distinct_from_present_empty() -> None:
    values = publication_fields()
    collections = fixture_collections()
    evidence = fingerprint_evidence(values)
    absent = content_digests(values, collections, evidence)
    present = native_snapshot_content_digests_v2(
        documents=cast(tuple[NativeDocumentPublicationV1, ...], values["documents"]),
        report=cast(NativeLoadReportPublicationV1, values["report"]),
        root_document_key=cast(str, values["root_document_key"]),
        load_options=cast(LoadOptions, values["load_options"]),
        capability_bits=cast(int, values["capability_bits"]) | 8,
        collections=collections,
        fingerprint_evidence=evidence,
        fingerprint_preimages=fingerprint_preimages(values),
        owl2_dl_report_summary=None,
        facade_cardinality_summary=facade_cardinality_summary(values, collections),
    )
    assert absent.source_manifest_sha256 != present.source_manifest_sha256


def test_manifest_rejects_non_structural_origin_digest_and_fingerprint_echo() -> None:
    values = publication_fields()
    collections: dict[FixtureKey, tuple[bytes, ...]] = dict(fixture_collections())
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    _collection, invalid_origin = encode_native_auxiliary_row_v2(
        NativeOriginRowV2(
            digest=b"x" * 32,
            document_key=documents[0].document_key,
            occurrence=0,
            span=None,
        ),
        max_row_bytes=source_load_row_budget(values),
    )
    collections[
        (
            NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            NativeFacadeScopeV2.DOCUMENT,
            0,
            NativeSignatureKindV2.ALL,
            True,
        )
    ] = (invalid_origin,)
    with pytest.raises(BackendProtocolError, match="structural row"):
        content_digests(values, collections, fingerprint_evidence(values))

    evidence = fingerprint_evidence(values)
    with pytest.raises(BackendProtocolError, match="fingerprint evidence"):
        content_digests(
            values,
            fixture_collections(),
            (replace(evidence[0], digest=b"y" * 32), *evidence[1:]),
        )


def test_source_and_document_origin_references_are_scoped_to_their_raw_document() -> None:
    source_values, source_collections, (_first_digest, second_digest) = (
        _two_document_manifest_fixture(with_source_rows=True)
    )
    source_key = (
        NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES,
        NativeFacadeScopeV2.DOCUMENT,
        0,
        NativeSignatureKindV2.ALL,
        True,
    )
    _source_collection, substituted_source = encode_native_auxiliary_row_v2(
        NativeSourceMapRowV2(
            digest=second_digest,
            occurrence=0,
            span=None,
        ),
        max_row_bytes=source_load_row_budget(source_values),
    )
    source_collections[source_key] = (substituted_source,)
    with pytest.raises(BackendProtocolError, match="in its document"):
        content_digests(
            source_values,
            source_collections,
            fingerprint_evidence(source_values),
        )

    values, collections, (_first_digest, second_digest) = _two_document_manifest_fixture()
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    origin_key = (
        NativeFacadeCollectionV2.ORIGIN_ENTRIES,
        NativeFacadeScopeV2.DOCUMENT,
        0,
        NativeSignatureKindV2.ALL,
        True,
    )
    for document_key, message in (
        (documents[0].document_key, "in its document"),
        (documents[1].document_key, "wrong document"),
    ):
        changed = dict(collections)
        _origin_collection, substituted_origin = encode_native_auxiliary_row_v2(
            NativeOriginRowV2(
                digest=second_digest,
                document_key=document_key,
                occurrence=0,
                span=None,
            ),
            max_row_bytes=source_load_row_budget(values),
        )
        changed[origin_key] = (substituted_origin,)
        with pytest.raises(BackendProtocolError, match=message):
            content_digests(values, changed, fingerprint_evidence(values))


@pytest.mark.parametrize(
    ("document_key", "message"),
    (
        ("first", "in its document"),
        ("unknown", "unknown document"),
    ),
)
def test_closure_origins_route_digest_checks_by_embedded_document_key(
    document_key: str,
    message: str,
) -> None:
    values, collections, (first_digest, second_digest) = _two_document_manifest_fixture()
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    closure_key = (
        NativeFacadeCollectionV2.ORIGIN_ENTRIES,
        NativeFacadeScopeV2.CLOSURE,
        None,
        NativeSignatureKindV2.ALL,
        True,
    )
    first_origin = next(row for row in collections[closure_key] if row[:32] == first_digest)
    selected_document_key = (
        documents[0].document_key if document_key == "first" else "d1:" + "9" * 64
    )
    _origin_collection, substituted = encode_native_auxiliary_row_v2(
        NativeOriginRowV2(
            digest=second_digest,
            document_key=selected_document_key,
            occurrence=0,
            span=None,
        ),
        max_row_bytes=source_load_row_budget(values),
    )
    collections[closure_key] = tuple(sorted((first_origin, substituted)))

    with pytest.raises(BackendProtocolError, match=message):
        content_digests(values, collections, fingerprint_evidence(values))


def test_document_structural_digest_indexes_are_precomputed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values, collections, _digests = _two_document_manifest_fixture(with_source_rows=True)
    original = structural_digest
    calls = 0

    def counted(*args: object, **kwargs: object) -> bytes:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(native_handoff_v2, "structural_digest", counted)
    content_digests(values, collections, fingerprint_evidence(values))
    assert calls == 2


def test_fingerprint_evidence_is_bound_to_authoritative_preimages_and_v1_results() -> None:
    values = publication_fields()
    collections = fixture_collections()
    evidence = fingerprint_evidence(values)
    preimages = fingerprint_preimages(values)
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    report = cast(NativeLoadReportPublicationV1, values["report"])
    published_digests = (
        *(document.document_fingerprint.digest for document in documents),
        report.structural_fingerprint.digest,
        report.logical_fingerprint.digest,
        report.signature_fingerprint.digest,
    )
    assert tuple(item.preimage_byte_length for item in evidence) == tuple(map(len, preimages))
    assert tuple(item.digest for item in evidence) == tuple(
        hashlib.sha256(preimage).digest() for preimage in preimages
    )
    assert tuple(item.digest for item in evidence) == published_digests

    tampered_preimages = (b"tampered-document", *preimages[1:])
    with pytest.raises(BackendProtocolError, match="authoritative preimage"):
        content_digests(
            values,
            collections,
            evidence,
            preimages=tampered_preimages,
        )

    with pytest.raises(BackendProtocolError, match="authoritative preimage"):
        content_digests(
            values,
            collections,
            (
                replace(evidence[0], preimage_byte_length=evidence[0].preimage_byte_length + 1),
                *evidence[1:],
            ),
            preimages=preimages,
        )

    tampered_digest = hashlib.sha256(tampered_preimages[0]).digest()
    with pytest.raises(BackendProtocolError, match="published fingerprint"):
        content_digests(
            values,
            collections,
            (
                replace(
                    evidence[0],
                    preimage_byte_length=len(tampered_preimages[0]),
                    digest=tampered_digest,
                ),
                *evidence[1:],
            ),
            preimages=tampered_preimages,
        )

    class EvilBytes(bytes):
        def __len__(self) -> int:
            raise AssertionError("hostile preimage bytes were inspected")

    with pytest.raises(TypeError, match="exact tuple of exact bytes"):
        content_digests(
            values,
            collections,
            evidence,
            preimages=(EvilBytes(preimages[0]), *preimages[1:]),
        )


def test_publication_rejects_caller_digest_that_differs_from_owner() -> None:
    value = publication()
    fields_value = {item.name: getattr(value, item.name) for item in fields(value)}
    fields_value["root_table_sha256"] = b"z" * 32
    with pytest.raises(BackendProtocolError, match="diverge from the owner"):
        freeze_native_snapshot_publication_v2(fields_value)
    assert type(value) is NativeSnapshotPublicationV2


def test_facade_summary_rejects_effective_structural_count_drift() -> None:
    values = publication_fields()
    collections = fixture_collections()
    summary = facade_cardinality_summary(values, collections)
    values["facade_cardinality_summary"] = replace(
        summary,
        documents=(
            replace(
                summary.documents[0],
                effective_axiom_count=summary.documents[0].effective_axiom_count + 1,
            ),
        ),
    )
    with pytest.raises(BackendProtocolError, match="effective structural counts"):
        content_digests(values, collections, fingerprint_evidence(values))


@pytest.mark.parametrize(
    ("capability_bits", "summary_field", "document_has_rdf"),
    (
        (7 | 16, "raw_source_prefix_count", False),
        (7, "effective_origin_count", False),
        (7 | 16, "rdf_unconsumed_triple_count", False),
        (7 | 16 | 32, "rdf_unconsumed_triple_count", False),
    ),
)
def test_summary_subsections_require_capability_and_rdf_document_metadata(
    capability_bits: int,
    summary_field: str,
    document_has_rdf: bool,
) -> None:
    values = publication_fields()
    collections = fixture_collections()
    summary = facade_cardinality_summary(values, collections)
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    if document_has_rdf:
        values["documents"] = (
            replace(
                documents[0],
                rdf_mapping_conformant=True,
                rdf_mapping_report_sha256=b"r" * 32,
            ),
        )
    values["capability_bits"] = capability_bits
    summary_row = summary.documents[0]
    if summary_field == "raw_source_prefix_count":
        changed_row = replace(summary_row, raw_source_prefix_count=1)
    elif summary_field == "effective_origin_count":
        changed_row = replace(summary_row, effective_origin_count=1)
    else:
        changed_row = replace(summary_row, rdf_unconsumed_triple_count=1)
    values["facade_cardinality_summary"] = replace(
        summary,
        documents=(changed_row,),
    )

    with pytest.raises(BackendProtocolError, match="require") as raised:
        content_digests(values, collections, fingerprint_evidence(values))
    assert raised.value.code == "NATIVE_PUBLICATION_CAPABILITY"


@pytest.mark.parametrize(
    "limit_name",
    (
        "max_prefixes",
        "max_triples",
        "max_origin_entries",
    ),
)
def test_facade_summary_enforces_configured_collection_limits(
    limit_name: str,
) -> None:
    values = publication_fields()
    collections = fixture_collections()
    summary = facade_cardinality_summary(values, collections)
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    document = documents[0]
    capability_bits = cast(int, values["capability_bits"])
    options = cast(LoadOptions, values["load_options"])
    if limit_name == "max_prefixes":
        capability_bits |= 8
        options = replace(options, preserve_source_map=True)
    elif limit_name == "max_triples":
        capability_bits |= 32
        document = replace(
            document,
            rdf_mapping_conformant=True,
            rdf_mapping_report_sha256=b"r" * 32,
        )
    if limit_name == "max_prefixes":
        changed_row = replace(summary.documents[0], raw_source_prefix_count=2)
        changed_closure = summary.closure
        selected_limits = replace(options.limits, max_prefixes=1)
    elif limit_name == "max_triples":
        changed_row = replace(summary.documents[0], rdf_unconsumed_triple_count=2)
        changed_closure = summary.closure
        selected_limits = replace(options.limits, max_triples=1)
    else:
        changed_row = replace(summary.documents[0], effective_origin_count=2)
        changed_closure = replace(summary.closure, effective_origin_count=2)
        selected_limits = replace(options.limits, max_origin_entries=1)
    selected_summary = replace(
        summary,
        documents=(changed_row,),
        closure=changed_closure,
    )
    options = replace(options, limits=selected_limits)

    with pytest.raises(BackendProtocolError, match=limit_name) as raised:
        native_handoff_v2._facade_cardinality_summary_sha256_v2(
            selected_summary,
            (document,),
            cast(NativeLoadReportPublicationV1, values["report"]),
            capability_bits=capability_bits,
            load_options=options,
            metadata_diagnostic_count=0,
            owl2_dl_report_summary=None,
        )
    assert raised.value.code == "NATIVE_PUBLICATION_LIMIT"


def test_facade_summary_enforces_combined_diagnostic_and_owl_index_limits() -> None:
    values = publication_fields()
    collections = fixture_collections()
    summary = facade_cardinality_summary(values, collections)
    document = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])[0]
    document = replace(
        document,
        rdf_mapping_conformant=True,
        rdf_mapping_report_sha256=b"r" * 32,
    )
    selected_summary = replace(
        summary,
        documents=(replace(summary.documents[0], rdf_diagnostic_count=1),),
    )
    options = cast(LoadOptions, values["load_options"])
    options = replace(
        options,
        limits=replace(options.limits, max_diagnostics=2),
    )
    owl_summary = NativeOWL2DLReportSummaryV2(
        structural_values_checked=1,
        structural_complete=True,
        report_complete=True,
        structural_issue_count=1,
        issue_count=1,
        role_property_count=0,
        role_hierarchy_count=0,
        role_composite_count=0,
        role_non_simple_count=0,
    )

    with pytest.raises(BackendProtocolError, match="diagnostics") as raised:
        native_handoff_v2._facade_cardinality_summary_sha256_v2(
            selected_summary,
            (document,),
            cast(NativeLoadReportPublicationV1, values["report"]),
            capability_bits=cast(int, values["capability_bits"]) | 32,
            load_options=options,
            metadata_diagnostic_count=2,
            owl2_dl_report_summary=None,
        )
    assert raised.value.code == "NATIVE_PUBLICATION_LIMIT"

    with pytest.raises(BackendProtocolError, match="max_index_rows") as raised:
        native_handoff_v2._facade_cardinality_summary_sha256_v2(
            summary,
            cast(tuple[NativeDocumentPublicationV1, ...], values["documents"]),
            cast(NativeLoadReportPublicationV1, values["report"]),
            capability_bits=cast(int, values["capability_bits"]),
            load_options=replace(
                cast(LoadOptions, values["load_options"]),
                limits=replace(
                    cast(LoadOptions, values["load_options"]).limits,
                    max_index_rows=1,
                ),
            ),
            metadata_diagnostic_count=0,
            owl2_dl_report_summary=owl_summary,
        )
    assert raised.value.code == "NATIVE_PUBLICATION_LIMIT"
