from __future__ import annotations

import copy
import hashlib
import pickle
from collections.abc import Iterator, Mapping
from dataclasses import fields, replace
from typing import cast

import pytest

from pyowl_core.backends import native_handoff_v2
from pyowl_core.backends.native_handoff import (
    NativeDiagnosticPublicationV1,
    NativeDocumentPublicationV1,
    NativeImportManifestPublicationV1,
    freeze_native_snapshot_publication_v1,
)
from pyowl_core.backends.native_handoff_v2 import (
    NATIVE_AUXILIARY_CODEC_SCHEMA_SHA256_V2,
    NATIVE_FACADE_ACCESS_SCHEMA_SHA256_V2,
    NATIVE_FACADE_COUNTER_FIELDS_V2,
    NATIVE_FACADE_PAGE_FIELDS_V2,
    NATIVE_PYTHON_FACADE_COUNTER_FIELDS_V2,
    NATIVE_RDF_MAPPING_REPORT_DOMAIN_V2,
    NATIVE_SNAPSHOT_ATTESTATION_FIELDS_V2,
    NATIVE_SNAPSHOT_PUBLICATION_FIELDS_V2,
    NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2,
    NativeDiagnosticReferenceKindsV2,
    NativeDiagnosticReferenceKindV2,
    NativeDocumentHandleV2,
    NativeFacadeCollectionV2,
    NativeFacadeContainsRequestV2,
    NativeFacadeCountersV2,
    NativeFacadePageRequestV2,
    NativeFacadePageV2,
    NativeFacadeScopeV2,
    NativeOriginRowV2,
    NativePythonFacadeCountersV2,
    NativeRDFDiagnosticRowV2,
    NativeRDFReportHeaderRowV2,
    NativeRDFRuleRowV2,
    NativeRDFTripleRowV2,
    NativeSignatureKindV2,
    NativeSnapshotAttestationV2,
    NativeSnapshotHandleV2,
    NativeSnapshotPublicationV2,
    NativeSourceMapRowV2,
    NativeSourcePrefixRowV2,
    decode_native_auxiliary_row_v2,
    encode_native_auxiliary_row_v2,
    freeze_native_snapshot_publication_v2,
    require_native_facade_publication_v2,
)
from pyowl_core.diagnostics import SourceSpan
from pyowl_core.document.document import Fingerprint, OntologyID
from pyowl_core.exceptions import BackendProtocolError, ClosedSnapshotError
from pyowl_core.model import (
    IRI,
    OWL_THING,
    CanonicalSet,
    Class,
    Datatype,
    Declaration,
    StructuralNode,
    canonical_bytes,
    structural_digest,
)
from pyowl_core.model.swrl import SWRLRule

from ._support import publication_fields
from ._support_v2 import (
    attestation,
    fixture_collections,
    fixture_max_row_bytes,
    publication,
    source_load_row_budget,
)

_FIXTURE_ROW_BOUND = fixture_max_row_bytes()


class _EvilText(str):
    def encode(self, *_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("hostile str subclass method was invoked")


class _EvilInt(int):
    def __lt__(self, _other: object) -> bool:
        raise AssertionError("hostile int subclass method was invoked")


class _EvilBytes(bytes):
    def __len__(self) -> int:
        raise AssertionError("hostile bytes subclass method was invoked")


class _EvilTuple(tuple[object, ...]):
    def __iter__(self) -> Iterator[object]:
        raise AssertionError("hostile tuple subclass was traversed")


def test_v2_publication_is_exact_frozen_and_binds_all_amended_semantics() -> None:
    value = publication()
    assert type(value) is NativeSnapshotPublicationV2
    assert type(value.handle) is NativeSnapshotHandleV2
    assert tuple(item.name for item in fields(value)) == tuple(
        row[1] for row in NATIVE_SNAPSHOT_PUBLICATION_FIELDS_V2
    )
    assert tuple(item.name for item in fields(value.handle.attestation)) == tuple(
        row[1] for row in NATIVE_SNAPSHOT_ATTESTATION_FIELDS_V2
    )
    assert value.ledger_sha256 == NATIVE_SNAPSHOT_PUBLICATION_LEDGER_SHA256_V2
    assert value.handle.attestation.facade_access_schema_sha256 == (
        NATIVE_FACADE_ACCESS_SCHEMA_SHA256_V2
    )
    assert value.handle.attestation.auxiliary_codec_schema_sha256 == (
        NATIVE_AUXILIARY_CODEC_SCHEMA_SHA256_V2
    )
    assert value.handle.attestation.metadata_manifest_sha256
    assert value.max_facade_row_bytes == _FIXTURE_ROW_BOUND
    with pytest.raises((AttributeError, TypeError)):
        value.version = 3  # type: ignore[misc]
    with pytest.raises(TypeError, match="created only"):
        NativeSnapshotHandleV2()
    with pytest.raises(TypeError, match="sealed"):

        class InvalidHandle(NativeSnapshotHandleV2):
            pass

    assert copy.copy(value.handle) is value.handle
    assert copy.deepcopy(value.handle) is value.handle
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(value.handle)


def test_facade_dispatch_requires_v2_and_marks_v1_legacy() -> None:
    value = publication()
    assert require_native_facade_publication_v2(value) is value
    with pytest.raises(BackendProtocolError, match="V2 paged"):
        require_native_facade_publication_v2(
            freeze_native_snapshot_publication_v1(publication_fields())
        )


def test_publication_row_maximum_is_positive_actual_and_within_load_budgets() -> None:
    value = publication()
    assert value.max_facade_row_bytes == max(
        len(row) for rows in fixture_collections().values() for row in rows
    )
    assert value.max_facade_row_bytes <= source_load_row_budget()
    with pytest.raises(BackendProtocolError, match="max_canonical_work"):
        attestation(max_facade_row_bytes=source_load_row_budget() + 1)


def test_generated_pages_echo_bounds_support_contains_and_report_exact_counters() -> None:
    value = publication()
    request = NativeFacadePageRequestV2(
        collection=NativeFacadeCollectionV2.AXIOMS,
        scope=NativeFacadeScopeV2.DOCUMENT,
        document_ordinal=0,
        start=0,
        max_rows=64,
        max_bytes=1,
        max_row_bytes=value.max_facade_row_bytes,
    )
    page = value.handle._facade_page_v2(request)
    assert type(page) is NativeFacadePageV2
    assert page.total_count == 1
    assert page.terminal and page.next_cursor is None
    assert len(page.rows) == 1 and page.page_bytes > request.max_bytes
    axiom = Declaration(Class(IRI("urn:handoff:Class")))
    assert value.handle._facade_contains_v2(
        NativeFacadeContainsRequestV2(
            collection=NativeFacadeCollectionV2.AXIOMS,
            scope=NativeFacadeScopeV2.CLOSURE,
            document_ordinal=None,
            canonical=canonical_bytes(axiom),
            max_row_bytes=value.max_facade_row_bytes,
        )
    )
    counters = value.handle._facade_counters_v2()
    assert tuple(item.name for item in fields(counters)) == tuple(
        row[1] for row in NATIVE_FACADE_COUNTER_FIELDS_V2
    )
    assert counters.page_requests == counters.pages_returned == 1
    assert counters.rows_emitted == counters.axiom_rows_emitted == 1
    assert counters.payload_bytes_copied == counters.canonical_payload_bytes_copied
    assert counters.contains_requests == counters.contains_hits == 1


def test_counter_namespaces_classify_frozen_runtime_and_python_values() -> None:
    value = publication()
    counters = value.handle._facade_counters_v2()
    assert counters.canonical_input_rows == 1
    assert counters.retained_axiom_rows == 2  # document + explicitly retained fake closure
    assert counters.retained_origin_rows == 2  # document + explicitly retained fake closure
    assert counters.retained_owner_bytes == (
        counters.retained_component_bytes
        + counters.retained_root_bytes
        + counters.retained_source_bytes
        + counters.retained_origin_bytes
        + counters.retained_rdf_bytes
        + counters.retained_owl2_dl_bytes
        + counters.retained_index_bytes
        + counters.retained_metadata_bytes
    )
    assert counters.page_requests == counters.rows_emitted == 0
    with pytest.raises(ValueError, match="per-collection"):
        NativeFacadeCountersV2(rows_emitted=1)
    python_counters = NativePythonFacadeCountersV2()
    assert tuple(item.name for item in fields(python_counters)) == tuple(
        row[1] for row in NATIVE_PYTHON_FACADE_COUNTER_FIELDS_V2
    )
    assert not (
        {item.name for item in fields(python_counters)} & {item.name for item in fields(counters)}
    )
    with pytest.raises(ValueError, match="cannot exceed"):
        NativePythonFacadeCountersV2(cache_current_bytes=2, cache_peak_bytes=1)


def test_generated_owner_discards_authoritative_fingerprint_preimages() -> None:
    baseline = publication()
    values = publication_fields()
    documents = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])
    manifest = cast(NativeImportManifestPublicationV1, values["import_manifest"])
    document_preimage = b"large-authoritative-document-preimage" * 65_536
    document_fingerprint = Fingerprint(
        "sha256",
        1,
        hashlib.sha256(document_preimage).digest(),
    )
    values["documents"] = (replace(documents[0], document_fingerprint=document_fingerprint),)
    values["import_manifest"] = replace(
        manifest,
        documents=(
            replace(
                manifest.documents[0],
                document_fingerprint=document_fingerprint,
            ),
        ),
    )

    published = publication(
        values=values,
        preimages=(
            document_preimage,
            b"structural",
            b"logical",
            b"signature",
        ),
    )
    owner = object.__getattribute__(published.handle, "_owner_v2")
    fixture = owner[2]

    assert not hasattr(fixture, "_fingerprint_preimages")
    assert not hasattr(fixture, "_fingerprint_evidence")
    assert published.handle._facade_counters_v2().retained_metadata_bytes == (
        baseline.handle._facade_counters_v2().retained_metadata_bytes
    )


def test_validation_decodes_are_retained_page_locally_for_exactly_once_consumption() -> None:
    value = publication()
    axiom_page = value.handle._facade_page_v2(
        NativeFacadePageRequestV2(
            collection=NativeFacadeCollectionV2.AXIOMS,
            scope=NativeFacadeScopeV2.CLOSURE,
            document_ordinal=None,
            start=0,
            max_rows=1,
            max_bytes=value.max_facade_row_bytes,
            max_row_bytes=value.max_facade_row_bytes,
        )
    )
    assert tuple(
        canonical_bytes(cast(StructuralNode, item)) for item in axiom_page._validated_rows_v2()
    ) == (axiom_page.rows)
    origin_page = value.handle._facade_page_v2(
        NativeFacadePageRequestV2(
            collection=NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            start=0,
            max_rows=1,
            max_bytes=value.max_facade_row_bytes,
            max_row_bytes=value.max_facade_row_bytes,
        )
    )
    assert all(type(item) is NativeOriginRowV2 for item in origin_page._validated_rows_v2())
    contains = NativeFacadeContainsRequestV2(
        collection=NativeFacadeCollectionV2.AXIOMS,
        scope=NativeFacadeScopeV2.CLOSURE,
        document_ordinal=None,
        canonical=axiom_page.rows[0],
        max_row_bytes=value.max_facade_row_bytes,
    )
    assert canonical_bytes(contains._validated_axiom_v2()) == contains.canonical
    assert contains._validated_canonical_v2() is contains.canonical
    assert "_validated_rows" not in {row[1] for row in NATIVE_FACADE_PAGE_FIELDS_V2}


def test_traversal_order_validation_reuses_retained_auxiliary_decodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = publication()
    original = native_handoff_v2.decode_native_auxiliary_row_v2
    calls = 0

    def counted_decode(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        native_handoff_v2,
        "decode_native_auxiliary_row_v2",
        counted_decode,
    )
    page = value.handle._facade_page_v2(
        NativeFacadePageRequestV2(
            collection=NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            start=0,
            max_rows=1,
            max_bytes=value.max_facade_row_bytes,
            max_row_bytes=value.max_facade_row_bytes,
        )
    )
    assert calls == len(page.rows) == len(page._validated_rows_v2())


def test_page_and_contains_requests_are_exact_scoped_and_resource_bounded() -> None:
    with pytest.raises(ValueError, match="document_ordinal=None"):
        NativeFacadePageRequestV2(
            collection=NativeFacadeCollectionV2.AXIOMS,
            scope=NativeFacadeScopeV2.CLOSURE,
            document_ordinal=0,
            start=0,
            max_rows=1,
            max_bytes=1,
            max_row_bytes=_FIXTURE_ROW_BOUND,
        )
    with pytest.raises(ValueError, match="document scope only"):
        NativeFacadePageRequestV2(
            collection=NativeFacadeCollectionV2.RDF_RULE_IDS,
            scope=NativeFacadeScopeV2.CLOSURE,
            document_ordinal=None,
            start=0,
            max_rows=1,
            max_bytes=1,
            max_row_bytes=_FIXTURE_ROW_BOUND,
        )
    with pytest.raises(ValueError, match="max_rows"):
        NativeFacadePageRequestV2(
            collection=NativeFacadeCollectionV2.AXIOMS,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            start=0,
            max_rows=65,
            max_bytes=1,
            max_row_bytes=_FIXTURE_ROW_BOUND,
        )
    with pytest.raises(ValueError, match="axioms-only"):
        NativeFacadeContainsRequestV2(
            collection=NativeFacadeCollectionV2.EXTENSIONS,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            canonical=canonical_bytes(Declaration(Class(IRI("urn:test:C")))),
            max_row_bytes=_FIXTURE_ROW_BOUND,
        )


def test_page_checks_exact_tuple_and_count_before_row_traversal() -> None:
    class ExplosiveRows:
        def __iter__(self) -> None:
            raise AssertionError("rows were traversed before exact-tuple validation")

    with pytest.raises(TypeError, match="exact tuple"):
        NativeFacadePageV2(
            collection=NativeFacadeCollectionV2.AXIOMS,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            start=0,
            max_rows=1,
            max_bytes=1024,
            max_row_bytes=_FIXTURE_ROW_BOUND,
            signature_kind=NativeSignatureKindV2.ALL,
            include_builtins=True,
            total_count=0,
            next_cursor=None,
            terminal=True,
            page_bytes=0,
            rows=ExplosiveRows(),  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="exceed max_rows"):
        NativeFacadePageV2(
            collection=NativeFacadeCollectionV2.AXIOMS,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            start=0,
            max_rows=1,
            max_bytes=1024,
            max_row_bytes=_FIXTURE_ROW_BOUND,
            signature_kind=NativeSignatureKindV2.ALL,
            include_builtins=True,
            total_count=2,
            next_cursor=None,
            terminal=True,
            page_bytes=0,
            rows=(object(), object()),  # type: ignore[arg-type]
        )


def test_envelope_rejects_recursive_scalar_subclasses_before_owner_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published = publication()
    base = {item.name: getattr(published, item.name) for item in fields(published)}
    document = published.documents[0]
    cases: list[dict[str, object]] = []

    def changed(**updates: object) -> dict[str, object]:
        return {**base, **updates}

    bad_document_key = replace(document)
    object.__setattr__(bad_document_key, "document_key", _EvilText(document.document_key))
    cases.append(changed(documents=(bad_document_key,)))

    bad_direct_imports = replace(document)
    object.__setattr__(bad_direct_imports, "direct_imports", _EvilTuple(()))
    cases.append(changed(documents=(bad_direct_imports,)))

    bad_provenance = replace(document.provenance)
    object.__setattr__(bad_provenance, "source_sha256", _EvilBytes(b"p" * 32))
    cases.append(changed(documents=(replace(document, provenance=bad_provenance),)))

    bad_fingerprint = replace(document.document_fingerprint)
    bad_fingerprint_document = replace(document, document_fingerprint=bad_fingerprint)
    object.__setattr__(bad_fingerprint, "digest", _EvilBytes(b"f" * 32))
    cases.append(changed(documents=(bad_fingerprint_document,)))

    bad_iri = IRI("urn:hostile:iri")
    bad_ontology_id = OntologyID(bad_iri)
    bad_iri_document = replace(document, ontology_id=bad_ontology_id)
    object.__setattr__(bad_iri, "value", _EvilText("urn:hostile:iri"))
    cases.append(changed(documents=(bad_iri_document,)))

    bad_options = replace(published.load_options)
    object.__setattr__(bad_options, "offline", _EvilInt(1))
    cases.append(changed(load_options=bad_options))

    bad_limits = replace(published.load_options.limits)
    bad_limit_options = replace(published.load_options, limits=bad_limits)
    object.__setattr__(bad_limits, "max_axioms", _EvilInt(1))
    cases.append(changed(load_options=bad_limit_options))

    bad_diagnostic = replace(published.diagnostics[0])
    object.__setattr__(bad_diagnostic, "code", _EvilText("NATIVE_FIXTURE"))
    cases.append(changed(diagnostics=(bad_diagnostic,)))

    bad_report_count = replace(published.report)
    object.__setattr__(bad_report_count, "effective_axiom_count", _EvilInt(1))
    cases.append(changed(report=bad_report_count))

    bad_report_version = replace(published.report)
    object.__setattr__(bad_report_version, "api_version", _EvilTuple((0, 1)))
    cases.append(changed(report=bad_report_version))
    cases.append(changed(documents=_EvilTuple((document,))))
    cases.append(changed(version=_EvilInt(2)))
    cases.append(changed(ledger_sha256=_EvilBytes(published.ledger_sha256)))

    def owner_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("owner was called before recursive exact validation")

    monkeypatch.setattr(
        native_handoff_v2._NativeFacadeHandleBaseV2,
        "_call_owner_v2",
        owner_must_not_run,
    )
    for fields_value in cases:
        with pytest.raises((TypeError, BackendProtocolError)):
            freeze_native_snapshot_publication_v2(fields_value)


def test_envelope_revalidates_corrupted_exact_records_before_owner_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published = publication()
    base = {item.name: getattr(published, item.name) for item in fields(published)}
    document = published.documents[0]
    cases: list[dict[str, object]] = []

    def changed(**updates: object) -> dict[str, object]:
        return {**base, **updates}

    bad_fingerprint = replace(document.document_fingerprint)
    bad_fingerprint_document = replace(document, document_fingerprint=bad_fingerprint)
    object.__setattr__(bad_fingerprint, "algorithm", "sha512")
    cases.append(changed(documents=(bad_fingerprint_document,)))

    version_iri = IRI("urn:hostile:version")
    bad_ontology_id = OntologyID(IRI("urn:hostile:ontology"), version_iri)
    bad_ontology_document = replace(document, ontology_id=bad_ontology_id)
    object.__setattr__(bad_ontology_id, "ontology_iri", None)
    cases.append(changed(documents=(bad_ontology_document,)))

    for deadline in (float("nan"), float("inf")):
        bad_limits = replace(published.load_options.limits)
        bad_options = replace(published.load_options, limits=bad_limits)
        object.__setattr__(bad_limits, "deadline_seconds", deadline)
        cases.append(changed(load_options=bad_options))

    bad_report = replace(published.report)
    object.__setattr__(bad_report, "timings", (("freeze_seconds", float("nan")),))
    cases.append(changed(report=bad_report))

    bad_diagnostic = replace(published.diagnostics[0])
    object.__setattr__(bad_diagnostic, "severity", "critical")
    cases.append(changed(diagnostics=(bad_diagnostic,)))

    bad_provenance = replace(document.provenance)
    bad_provenance_document = replace(document, provenance=bad_provenance)
    object.__setattr__(bad_provenance, "format", "invented")
    cases.append(changed(documents=(bad_provenance_document,)))

    bad_manifest = replace(published.import_manifest)
    object.__setattr__(bad_manifest, "policy", "invented")
    cases.append(changed(import_manifest=bad_manifest))

    bad_record = replace(published.import_manifest.documents[0])
    bad_record_manifest = replace(published.import_manifest, documents=(bad_record,))
    object.__setattr__(bad_record, "status", "invented")
    cases.append(changed(import_manifest=bad_record_manifest))

    def owner_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("owner was called before exact-record semantic validation")

    monkeypatch.setattr(
        native_handoff_v2._NativeFacadeHandleBaseV2,
        "_call_owner_v2",
        owner_must_not_run,
    )
    for fields_value in cases:
        with pytest.raises(ValueError):
            freeze_native_snapshot_publication_v2(fields_value)


def test_auxiliary_encoder_revalidates_corrupted_source_span_and_diagnostic() -> None:
    source_row = NativeSourceMapRowV2(
        digest=b"s" * 32,
        occurrence=0,
        span=SourceSpan(byte_start=1, byte_end=2),
    )
    object.__setattr__(source_row.span, "byte_start", _EvilInt(1))
    with pytest.raises(TypeError):
        encode_native_auxiliary_row_v2(
            source_row,
            max_row_bytes=source_load_row_budget(),
        )

    reversed_span_row = NativeSourceMapRowV2(
        digest=b"s" * 32,
        occurrence=0,
        span=SourceSpan(byte_start=1, byte_end=2),
    )
    object.__setattr__(reversed_span_row.span, "byte_end", 0)
    with pytest.raises(ValueError, match="must not precede"):
        encode_native_auxiliary_row_v2(
            reversed_span_row,
            max_row_bytes=source_load_row_budget(),
        )

    diagnostic = NativeDiagnosticPublicationV1(
        code="RDF_MAPPING",
        severity="warning",
        message="mapping evidence",
        document_iri=None,
        byte_start=None,
        byte_end=None,
        line_start=None,
        column_start=None,
        line_end=None,
        column_end=None,
        import_chain=(),
        details=(),
    )
    diagnostic_row = NativeRDFDiagnosticRowV2(
        diagnostic=diagnostic,
        reference_kinds=NativeDiagnosticReferenceKindsV2(
            document_reference_kind=None,
            import_chain_kinds=(),
        ),
    )
    object.__setattr__(diagnostic, "message", _EvilText("mapping evidence"))
    with pytest.raises(TypeError):
        encode_native_auxiliary_row_v2(
            diagnostic_row,
            max_row_bytes=source_load_row_budget(),
        )


def test_freeze_copies_a_mapping_once_before_validation() -> None:
    published = publication()
    selected = {item.name: getattr(published, item.name) for item in fields(published)}

    class SinglePassMapping(Mapping[str, object]):
        def __init__(self) -> None:
            self.iterations = 0

        def __getitem__(self, key: str) -> object:
            return selected[key]

        def __iter__(self) -> Iterator[str]:
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("publication mapping was iterated more than once")
            return iter(selected)

        def __len__(self) -> int:
            return len(selected)

    mapping = SinglePassMapping()
    assert type(freeze_native_snapshot_publication_v2(mapping)) is NativeSnapshotPublicationV2
    assert mapping.iterations == 1


def test_structural_pages_enforce_swrl_and_signature_filters() -> None:
    rule = canonical_bytes(SWRLRule(CanonicalSet(), CanonicalSet()))
    extension_page = NativeFacadePageV2(
        collection=NativeFacadeCollectionV2.EXTENSIONS,
        scope=NativeFacadeScopeV2.DOCUMENT,
        document_ordinal=0,
        start=0,
        max_rows=1,
        max_bytes=1024,
        max_row_bytes=max(_FIXTURE_ROW_BOUND, len(rule)),
        signature_kind=NativeSignatureKindV2.ALL,
        include_builtins=True,
        total_count=1,
        next_cursor=None,
        terminal=True,
        page_bytes=len(rule),
        rows=(rule,),
    )
    assert extension_page.rows == (rule,)

    class_row = canonical_bytes(Class(IRI("urn:test:C")))
    with pytest.raises(ValueError, match="wrong category"):
        NativeFacadePageV2(
            collection=NativeFacadeCollectionV2.SIGNATURE,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            start=0,
            max_rows=1,
            max_bytes=1024,
            max_row_bytes=max(_FIXTURE_ROW_BOUND, len(class_row)),
            signature_kind=NativeSignatureKindV2.DATATYPE,
            include_builtins=True,
            total_count=1,
            next_cursor=None,
            terminal=True,
            page_bytes=len(class_row),
            rows=(class_row,),
        )
    builtin = canonical_bytes(OWL_THING)
    with pytest.raises(ValueError, match="wrong category"):
        NativeFacadePageV2(
            collection=NativeFacadeCollectionV2.SIGNATURE,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            start=0,
            max_rows=1,
            max_bytes=1024,
            max_row_bytes=max(_FIXTURE_ROW_BOUND, len(builtin)),
            signature_kind=NativeSignatureKindV2.CLASS,
            include_builtins=False,
            total_count=1,
            next_cursor=None,
            terminal=True,
            page_bytes=len(builtin),
            rows=(builtin,),
        )
    datatype = canonical_bytes(Datatype(IRI("urn:test:datatype")))
    assert NativeFacadePageV2(
        collection=NativeFacadeCollectionV2.SIGNATURE,
        scope=NativeFacadeScopeV2.DOCUMENT,
        document_ordinal=0,
        start=0,
        max_rows=1,
        max_bytes=1024,
        max_row_bytes=max(_FIXTURE_ROW_BOUND, len(datatype)),
        signature_kind=NativeSignatureKindV2.DATATYPE,
        include_builtins=False,
        total_count=1,
        next_cursor=None,
        terminal=True,
        page_bytes=len(datatype),
        rows=(datatype,),
    ).rows == (datatype,)


def test_auxiliary_encoder_uses_complete_effective_row_bound() -> None:
    row = NativeSourcePrefixRowV2(prefix="ex", iri="urn:example")
    with pytest.raises(ValueError, match="encoded auxiliary row"):
        encode_native_auxiliary_row_v2(row, max_row_bytes=1)
    _collection, encoded = encode_native_auxiliary_row_v2(
        row,
        max_row_bytes=source_load_row_budget(),
    )
    assert len(encoded) > len(row.prefix.encode()) + len(row.iri.encode())


def test_each_auxiliary_codec_round_trips_exact_records() -> None:
    diagnostic = NativeDiagnosticPublicationV1(
        code="RDF_MAPPING",
        severity="warning",
        message="mapping evidence",
        document_iri="urn:test:document",
        byte_start=1,
        byte_end=3,
        line_start=1,
        column_start=2,
        line_end=1,
        column_end=4,
        import_chain=("urn:test:import",),
        details=(("count", 3), ("retained", True), ("rule", "R1")),
    )
    values = (
        NativeSourceMapRowV2(
            digest=b"s" * 32,
            occurrence=2,
            span=SourceSpan(byte_start=1, byte_end=4),
            lexical=(("prefix", "ex"),),
        ),
        NativeSourcePrefixRowV2(prefix="ex", iri="https://example.test/"),
        NativeOriginRowV2(
            digest=b"o" * 32,
            document_key="d1:" + "1" * 64,
            occurrence=5,
            span=None,
        ),
        NativeRDFReportHeaderRowV2(
            conformant=False,
            consumed_triples=2,
            total_triples=3,
        ),
        NativeRDFTripleRowV2(subject="s", predicate="p", object="o"),
        NativeRDFRuleRowV2(rule_id="R1"),
        NativeRDFDiagnosticRowV2(
            diagnostic=diagnostic,
            reference_kinds=NativeDiagnosticReferenceKindsV2(
                document_reference_kind=NativeDiagnosticReferenceKindV2.IRI,
                import_chain_kinds=(NativeDiagnosticReferenceKindV2.IRI,),
            ),
        ),
    )
    for value in values:
        budget = source_load_row_budget()
        collection, encoded = encode_native_auxiliary_row_v2(
            value,
            max_row_bytes=budget,
        )
        assert (
            decode_native_auxiliary_row_v2(
                collection,
                encoded,
                max_row_bytes=budget,
            )
            == value
        )
        with pytest.raises(ValueError, match="trailing bytes"):
            decode_native_auxiliary_row_v2(
                collection,
                encoded + b"x",
                max_row_bytes=budget,
            )


def test_rdf_triple_and_diagnostic_pages_preserve_producer_multiplicity() -> None:
    def encoded(
        value: NativeRDFReportHeaderRowV2 | NativeRDFTripleRowV2 | NativeRDFDiagnosticRowV2,
        bound: int,
    ) -> bytes:
        return encode_native_auxiliary_row_v2(
            value,
            max_row_bytes=bound,
        )[1]

    def frame(value: bytes) -> bytes:
        return len(value).to_bytes(8, "little") + value

    def u64(value: int) -> bytes:
        return value.to_bytes(8, "little")

    gathered_by_page_size: list[dict[NativeFacadeCollectionV2, tuple[bytes, ...]]] = []
    expected: dict[NativeFacadeCollectionV2, tuple[bytes, ...]] | None = None
    for max_rows in (1, 3):
        values = publication_fields()
        bound = source_load_row_budget(values)
        document = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])[0]
        header = encoded(
            NativeRDFReportHeaderRowV2(
                conformant=False,
                consumed_triples=0,
                total_triples=3,
            ),
            bound,
        )
        triple_first = encoded(
            NativeRDFTripleRowV2(subject="z-producer-first", predicate="p", object="o"),
            bound,
        )
        triple_second = encoded(
            NativeRDFTripleRowV2(subject="a-producer-second", predicate="p", object="o"),
            bound,
        )
        triples = (triple_first, triple_second, triple_second)
        diagnostic_first = NativeDiagnosticPublicationV1(
            code="Z_PRODUCER_FIRST",
            severity="warning",
            message="producer first",
            document_iri=None,
            byte_start=None,
            byte_end=None,
            line_start=None,
            column_start=None,
            line_end=None,
            column_end=None,
            import_chain=(),
            details=(),
        )
        diagnostic_second = replace(
            diagnostic_first,
            code="A_PRODUCER_SECOND",
            message="producer second",
        )
        reference_kinds = NativeDiagnosticReferenceKindsV2(
            document_reference_kind=None,
            import_chain_kinds=(),
        )
        first_diagnostic_row = encoded(
            NativeRDFDiagnosticRowV2(
                diagnostic=diagnostic_first,
                reference_kinds=reference_kinds,
            ),
            bound,
        )
        second_diagnostic_row = encoded(
            NativeRDFDiagnosticRowV2(
                diagnostic=diagnostic_second,
                reference_kinds=reference_kinds,
            ),
            bound,
        )
        diagnostic_rows = (
            first_diagnostic_row,
            second_diagnostic_row,
            second_diagnostic_row,
        )
        document_key = document.document_key.encode("utf-8")
        report_body = (
            frame(document_key)
            + frame(header)
            + u64(len(triples))
            + b"".join(frame(row) for row in triples)
            + u64(0)
            + u64(len(diagnostic_rows))
            + b"".join(frame(row) for row in diagnostic_rows)
        )
        report_digest = hashlib.sha256(
            NATIVE_RDF_MAPPING_REPORT_DOMAIN_V2.encode("ascii") + b"\x00" + report_body
        ).digest()
        values["documents"] = (
            replace(
                document,
                rdf_mapping_conformant=False,
                rdf_mapping_report_sha256=report_digest,
            ),
        )
        values["capability_bits"] = cast(int, values["capability_bits"]) | 32
        collections = dict(fixture_collections())
        for collection, collection_rows in (
            (NativeFacadeCollectionV2.RDF_REPORT_HEADER, (header,)),
            (NativeFacadeCollectionV2.RDF_UNCONSUMED_TRIPLES, triples),
            (NativeFacadeCollectionV2.RDF_DIAGNOSTICS, diagnostic_rows),
        ):
            collections[
                (
                    collection,
                    NativeFacadeScopeV2.DOCUMENT,
                    0,
                    NativeSignatureKindV2.ALL,
                    True,
                )
            ] = collection_rows
        expected = {
            NativeFacadeCollectionV2.RDF_UNCONSUMED_TRIPLES: triples,
            NativeFacadeCollectionV2.RDF_DIAGNOSTICS: diagnostic_rows,
        }
        published = publication(collections, values=values)
        gathered: dict[NativeFacadeCollectionV2, tuple[bytes, ...]] = {}
        for collection, retained in expected.items():
            page_rows: list[bytes] = []
            start = 0
            while True:
                page = published.handle._facade_page_v2(
                    NativeFacadePageRequestV2(
                        collection=collection,
                        scope=NativeFacadeScopeV2.DOCUMENT,
                        document_ordinal=0,
                        start=start,
                        max_rows=max_rows,
                        max_bytes=published.max_facade_row_bytes * max_rows,
                        max_row_bytes=published.max_facade_row_bytes,
                    )
                )
                page_rows.extend(page.rows)
                if page.terminal:
                    break
                assert page.next_cursor is not None
                start = page.next_cursor
            assert tuple(page_rows) == retained
            gathered[collection] = tuple(page_rows)
        gathered_by_page_size.append(gathered)

    assert expected is not None
    assert gathered_by_page_size == [expected, expected]
    for retained_rows in expected.values():
        assert retained_rows[1] == retained_rows[2]


def test_source_auxiliary_same_digest_preserves_producer_order_and_duplicates() -> None:
    budget = source_load_row_budget()
    encoded = tuple(
        encode_native_auxiliary_row_v2(
            NativeSourceMapRowV2(
                digest=b"d" * 32,
                occurrence=1,
                span=span,
            ),
            max_row_bytes=budget,
        )[1]
        for span in (
            SourceSpan(byte_start=1, byte_end=2),
            SourceSpan(byte_start=2, byte_end=3),
        )
    )
    for retained in (tuple(reversed(encoded)), (encoded[0], encoded[0])):
        page = NativeFacadePageV2(
            collection=NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            start=0,
            max_rows=2,
            max_bytes=1024,
            max_row_bytes=budget,
            signature_kind=NativeSignatureKindV2.ALL,
            include_builtins=True,
            total_count=2,
            next_cursor=None,
            terminal=True,
            page_bytes=sum(map(len, retained)),
            rows=retained,
        )
        assert page.rows == retained


def test_source_prefixes_are_unique_by_prefix_key_not_full_encoded_row() -> None:
    budget = source_load_row_budget()
    rows = tuple(
        encode_native_auxiliary_row_v2(
            NativeSourcePrefixRowV2(prefix="ex", iri=iri),
            max_row_bytes=budget,
        )[1]
        for iri in ("urn:prefix:first", "urn:prefix:second")
    )
    with pytest.raises(ValueError, match="ascending unique"):
        NativeFacadePageV2(
            collection=NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            start=0,
            max_rows=2,
            max_bytes=sum(map(len, rows)),
            max_row_bytes=budget,
            signature_kind=NativeSignatureKindV2.ALL,
            include_builtins=True,
            total_count=2,
            next_cursor=None,
            terminal=True,
            page_bytes=sum(map(len, rows)),
            rows=rows,
        )


def test_attestation_metadata_manifest_changes_with_shared_metadata() -> None:
    value = publication()
    original = value.handle.attestation
    changed = replace(
        original,
        metadata_manifest_sha256=bytes(reversed(original.metadata_manifest_sha256)),
    )
    assert type(changed) is NativeSnapshotAttestationV2
    assert changed.digest != original.digest


def test_generated_fixture_totals_must_match_publication_counts() -> None:
    collections = fixture_collections()
    document_key = next(key for key in collections if key[1] is NativeFacadeScopeV2.DOCUMENT)
    del collections[document_key]
    with pytest.raises(BackendProtocolError, match="diverge"):
        publication(collections)


def test_envelope_validation_is_page_free_and_uses_only_frozen_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published = publication()
    fields_value = {item.name: getattr(published, item.name) for item in fields(published)}
    before = published.handle._facade_counters_v2()

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("envelope validation traversed retained ontology rows")

    for name in (
        "decode_native_auxiliary_row_v2",
        "decode_canonical",
        "structural_digest",
        "native_snapshot_content_digests_v2",
        "_validate_page_rows_v2",
    ):
        monkeypatch.setattr(native_handoff_v2, name, forbidden)
    monkeypatch.setattr(native_handoff_v2._GeneratedFacadeFixtureV2, "page", forbidden)

    frozen = freeze_native_snapshot_publication_v2(fields_value)
    after = frozen.handle._facade_counters_v2()
    assert after.page_requests == before.page_requests == 0
    assert after.rows_emitted == before.rows_emitted == 0
    assert after.payload_bytes_copied == before.payload_bytes_copied == 0


def test_generated_counters_include_distinct_raw_effective_and_closure_storage() -> None:
    values = publication_fields()
    effective = fixture_collections()
    raw = dict(effective)
    document = cast(tuple[NativeDocumentPublicationV1, ...], values["documents"])[0]
    raw_axiom_value = Declaration(Class(IRI("urn:handoff:RawOnly")))
    raw_axiom = canonical_bytes(raw_axiom_value)
    _origin_collection, raw_origin = encode_native_auxiliary_row_v2(
        NativeOriginRowV2(
            digest=structural_digest(raw_axiom_value),
            document_key=document.document_key,
            occurrence=0,
            span=None,
        ),
        max_row_bytes=source_load_row_budget(values),
    )
    raw[
        (
            NativeFacadeCollectionV2.AXIOMS,
            NativeFacadeScopeV2.DOCUMENT,
            0,
            NativeSignatureKindV2.ALL,
            True,
        )
    ] = (raw_axiom,)
    raw[
        (
            NativeFacadeCollectionV2.ORIGIN_ENTRIES,
            NativeFacadeScopeV2.DOCUMENT,
            0,
            NativeSignatureKindV2.ALL,
            True,
        )
    ] = (raw_origin,)

    published = publication(
        effective,
        values=values,
        raw_document_collections=raw,
    )
    counters = published.handle._facade_counters_v2()
    assert counters.retained_axiom_rows == 3
    assert counters.retained_origin_rows == 3
    assert counters.canonical_input_rows == 1
    assert counters.page_requests == counters.rows_emitted == 0


def test_close_is_idempotent_and_metadata_remains_readable() -> None:
    value = publication()
    value.handle.close()
    value.handle.close()
    assert value.handle.closed
    assert value.handle.attestation.document_count == 1
    assert value.handle._facade_counters_v2().close_requests == 2
    with pytest.raises(ClosedSnapshotError, match="closed"):
        value.handle._facade_page_v2(
            NativeFacadePageRequestV2(
                collection=NativeFacadeCollectionV2.AXIOMS,
                scope=NativeFacadeScopeV2.DOCUMENT,
                document_ordinal=0,
                start=0,
                max_rows=1,
                max_bytes=1024,
                max_row_bytes=value.max_facade_row_bytes,
            )
        )


def test_document_handle_has_independent_lifecycle_and_fixed_scope() -> None:
    value = publication()
    document = value.handle._facade_document_v2(0)
    assert type(document) is NativeDocumentHandleV2
    assert document.document_ordinal == 0
    assert copy.copy(document) is document
    assert copy.deepcopy(document) is document
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(document)
    with pytest.raises(BackendProtocolError, match="fixed ordinal"):
        document._facade_page_v2(
            NativeFacadePageRequestV2(
                collection=NativeFacadeCollectionV2.AXIOMS,
                scope=NativeFacadeScopeV2.CLOSURE,
                document_ordinal=None,
                start=0,
                max_rows=1,
                max_bytes=1024,
                max_row_bytes=value.max_facade_row_bytes,
            )
        )
    value.handle.close()
    page = document._facade_page_v2(
        NativeFacadePageRequestV2(
            collection=NativeFacadeCollectionV2.AXIOMS,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            start=0,
            max_rows=1,
            max_bytes=1024,
            max_row_bytes=value.max_facade_row_bytes,
        )
    )
    assert page.total_count == 1
    document.close()
    assert document.closed
    with pytest.raises(ClosedSnapshotError):
        document._facade_contains_v2(
            NativeFacadeContainsRequestV2(
                collection=NativeFacadeCollectionV2.AXIOMS,
                scope=NativeFacadeScopeV2.DOCUMENT,
                document_ordinal=0,
                canonical=page.rows[0],
                max_row_bytes=value.max_facade_row_bytes,
            )
        )
