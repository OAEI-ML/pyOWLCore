from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any, cast
from unittest.mock import patch

import pytest

from pyowl_core import (
    IRI,
    AcquisitionCache,
    BackendPreference,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    MappingResolver,
    OntologySnapshot,
    OperationCancelledError,
    ParsedDocumentCache,
    ParseError,
    ParseLimits,
    ResolvedDocument,
    ResourceLimitError,
    SnapshotLoader,
    UnresolvedImportWarning,
    encode_snapshot,
    parse_document,
)
from pyowl_core.backends import native
from pyowl_core.document.imports import AcquiredImport, _with_resolved_provenance
from tests.native.foundation._support import NativeTestExtension, load_extension


def _rdfxml(
    ontology_iri: str,
    *,
    imports: tuple[str, ...] = (),
    class_name: str,
) -> bytes:
    import_rows = "".join(
        f'<owl:imports rdf:resource="{import_iri}"/>' for import_iri in imports
    )
    return (
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns:owl="http://www.w3.org/2002/07/owl#">'
        f'<owl:Ontology rdf:about="{ontology_iri}">{import_rows}</owl:Ontology>'
        f'<owl:Class rdf:about="{ontology_iri}#{class_name}"/>'
        "</rdf:RDF>"
    ).encode()


ROOT = _rdfxml("urn:rdfxml:root", imports=("urn:rdfxml:child",), class_name="Root")
NO_IMPORT_ROOT = _rdfxml("urn:rdfxml:no-import-root", class_name="Root")
UNRESOLVED_ROOT = _rdfxml(
    "urn:rdfxml:unresolved-root",
    imports=("urn:rdfxml:missing",),
    class_name="Root",
)
SELF_CYCLE = _rdfxml(
    "urn:rdfxml:self-cycle",
    imports=("urn:rdfxml:self-alias",),
    class_name="Self",
)
CHILD = _rdfxml("urn:rdfxml:child-document", class_name="Child")

DIAMOND_ROOT = _rdfxml(
    "urn:rdfxml:diamond-root",
    imports=("urn:rdfxml:left", "urn:rdfxml:right"),
    class_name="Root",
)
LEFT = _rdfxml(
    "urn:rdfxml:left-document",
    imports=("urn:rdfxml:shared-a",),
    class_name="Left",
)
RIGHT = _rdfxml(
    "urn:rdfxml:right-document",
    imports=("urn:rdfxml:shared-b",),
    class_name="Right",
)
SHARED = _rdfxml("urn:rdfxml:shared-document", class_name="Shared")


@pytest.fixture(scope="module", autouse=True)
def extension() -> NativeTestExtension:
    selected = load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available:
        pytest.skip(result.reason or "native backend is unavailable")
    for hook in (
        "_parse_rdfxml_retained_v2",
        "_fork_parsed_structural_storage_v2",
        "_merge_parsed_structural_snapshot_v2",
    ):
        if not hasattr(selected, hook):
            pytest.skip(f"selected native artifact lacks {hook}")
    return selected


def _options(
    backend: BackendPreference,
    *,
    workers: int,
    preserve_source_map: bool,
    collect_provenance: bool,
    import_policy: ImportPolicy = ImportPolicy.RESOLVE_LOCAL,
    limits: ParseLimits | None = None,
) -> LoadOptions:
    selected_limits = ParseLimits() if limits is None else limits
    return LoadOptions(
        format=DocumentFormat.RDF_XML,
        imports=import_policy,
        backend=backend,
        preserve_source_map=preserve_source_map,
        collect_provenance=collect_provenance,
        limits=replace(selected_limits, max_concurrent_fetches=workers),
    )


def _load(
    source: bytes,
    resolver: MappingResolver,
    *,
    backend: BackendPreference,
    workers: int,
    preserve_source_map: bool,
    collect_provenance: bool,
    import_policy: ImportPolicy = ImportPolicy.RESOLVE_LOCAL,
    limits: ParseLimits | None = None,
) -> OntologySnapshot:
    loader = SnapshotLoader(
        acquisition_cache=AcquisitionCache(),
        document_cache=ParsedDocumentCache(),
    )
    options = _options(
        backend,
        workers=workers,
        preserve_source_map=preserve_source_map,
        collect_provenance=collect_provenance,
        import_policy=import_policy,
        limits=limits,
    )
    if backend is BackendPreference.PYTHON:
        return loader.load(source, options=options, resolver=resolver)
    unexpected = AssertionError("retained RDF/XML closure crossed the Python RDF mapper")
    with (
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.select",
            autospec=True,
            return_value="native",
        ),
        patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected),
    ):
        return loader.load(source, options=options, resolver=resolver)


def _two_document_resolver() -> MappingResolver:
    return MappingResolver(
        {
            "urn:rdfxml:child": ResolvedDocument(
                CHILD,
                IRI("urn:rdfxml:child-document"),
                format=DocumentFormat.RDF_XML,
                provenance={"locator": "memory:rdfxml-child"},
            )
        }
    )


@pytest.mark.parametrize(
    ("preserve_source_map", "collect_provenance"),
    ((False, False), (True, True)),
)
def test_rdfxml_two_document_closure_is_exact_and_owner_first(
    extension: NativeTestExtension,
    preserve_source_map: bool,
    collect_provenance: bool,
) -> None:
    reference = _load(
        ROOT,
        _two_document_resolver(),
        backend=BackendPreference.PYTHON,
        workers=1,
        preserve_source_map=preserve_source_map,
        collect_provenance=collect_provenance,
    )
    selected = _load(
        ROOT,
        _two_document_resolver(),
        backend=BackendPreference.NATIVE,
        workers=3,
        preserve_source_map=preserve_source_map,
        collect_provenance=collect_provenance,
    )

    assert "parse-rdfxml-v1" in extension.INGESTION_FEATURES
    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.capabilities.backend == "native"
    assert selected == reference
    assert selected.import_manifest == reference.import_manifest
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.origin_index == reference.origin_index
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert len(selected.documents) == 2
    for document, expected in zip(selected.documents, reference.documents, strict=True):
        assert type(document).__name__ == "_NativeOntologyDocument"
        assert document == expected
        assert document.source_map == expected.source_map
        assert document.rdf_mapping_report == expected.rdf_mapping_report
        assert document.rdf_mapping_report is not None
        assert document.rdf_mapping_report.conformant

    owner = cast(Any, selected)._native_snapshot_state.owner.handle._owner_v2
    counters = owner._publication_counters_v2()
    assert counters.parser_bytes == len(ROOT) + len(CHILD)
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0


@pytest.mark.parametrize(
    "import_policy",
    (
        ImportPolicy.RESOLVE_LOCAL,
        ImportPolicy.RESOLVE_STRICT,
        ImportPolicy.RECORD_UNRESOLVED,
    ),
)
@pytest.mark.parametrize("preserve_source_map", (False, True))
def test_rdfxml_zero_import_resolver_policy_stays_native(
    import_policy: ImportPolicy,
    preserve_source_map: bool,
) -> None:
    reference = _load(
        NO_IMPORT_ROOT,
        MappingResolver({}),
        backend=BackendPreference.PYTHON,
        workers=1,
        preserve_source_map=preserve_source_map,
        collect_provenance=True,
        import_policy=import_policy,
    )
    selected = _load(
        NO_IMPORT_ROOT,
        MappingResolver({}),
        backend=BackendPreference.NATIVE,
        workers=3,
        preserve_source_map=preserve_source_map,
        collect_provenance=True,
        import_policy=import_policy,
    )

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.capabilities.backend == "native"
    assert len(selected.documents) == 1
    assert selected == reference
    assert selected.load_options == _options(
        BackendPreference.NATIVE,
        workers=3,
        preserve_source_map=preserve_source_map,
        collect_provenance=True,
        import_policy=import_policy,
    )
    assert selected.import_manifest == reference.import_manifest
    assert selected.root.source_map == reference.root.source_map
    assert selected.origin_index == reference.origin_index
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert encode_snapshot(selected) == encode_snapshot(reference)
    owner = cast(Any, selected)._native_snapshot_state.owner.handle._owner_v2
    counters = owner._publication_counters_v2()
    assert counters.parser_bytes == len(NO_IMPORT_ROOT)
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0


@pytest.mark.parametrize("preserve_source_map", (False, True))
def test_rdfxml_unresolved_single_owner_closure_stays_native(
    preserve_source_map: bool,
) -> None:
    with pytest.warns(UnresolvedImportWarning):
        reference = _load(
            UNRESOLVED_ROOT,
            MappingResolver({}),
            backend=BackendPreference.PYTHON,
            workers=1,
            preserve_source_map=preserve_source_map,
            collect_provenance=True,
            import_policy=ImportPolicy.RECORD_UNRESOLVED,
        )
    with pytest.warns(UnresolvedImportWarning):
        selected = _load(
            UNRESOLVED_ROOT,
            MappingResolver({}),
            backend=BackendPreference.NATIVE,
            workers=3,
            preserve_source_map=preserve_source_map,
            collect_provenance=True,
            import_policy=ImportPolicy.RECORD_UNRESOLVED,
        )

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert len(selected.documents) == 1
    assert selected == reference
    assert selected.import_manifest == reference.import_manifest
    assert selected.root.source_map == reference.root.source_map
    assert selected.origin_index == reference.origin_index
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert encode_snapshot(selected) == encode_snapshot(reference)


def test_rdfxml_self_cycle_single_owner_closure_stays_native() -> None:
    resolver = MappingResolver(
        {
            "urn:rdfxml:self-alias": ResolvedDocument(
                SELF_CYCLE,
                IRI("urn:rdfxml:self-cycle-document"),
                format=DocumentFormat.RDF_XML,
                provenance={"locator": "memory:rdfxml-self"},
            )
        }
    )
    reference = _load(
        SELF_CYCLE,
        resolver,
        backend=BackendPreference.PYTHON,
        workers=1,
        preserve_source_map=True,
        collect_provenance=True,
    )
    selected = _load(
        SELF_CYCLE,
        resolver,
        backend=BackendPreference.NATIVE,
        workers=3,
        preserve_source_map=True,
        collect_provenance=True,
    )

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert len(selected.documents) == 1
    assert selected == reference
    assert selected.import_manifest == reference.import_manifest
    assert selected.import_manifest.edges[0].resolved_document_key == selected.root_document_key
    assert selected.root.source_map == reference.root.source_map
    assert selected.origin_index == reference.origin_index
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert encode_snapshot(selected) == encode_snapshot(reference)


def test_rdfxml_malformed_resolved_child_publishes_no_closure_owner(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    owner_calls = 0

    def unexpected_owner(*_arguments: object, **_keywords: object) -> object:
        nonlocal owner_calls
        owner_calls += 1
        raise AssertionError("malformed RDF/XML closure reached final owner publication")

    monkeypatch.setattr(
        cast(Any, extension),
        "_merge_parsed_structural_snapshot_v2",
        unexpected_owner,
    )
    resolver = MappingResolver(
        {
            "urn:rdfxml:child": ResolvedDocument(
                b"<rdf:RDF",
                IRI("urn:rdfxml:malformed-child"),
                format=DocumentFormat.RDF_XML,
            )
        }
    )

    with pytest.raises(ParseError):
        _load(
            ROOT,
            resolver,
            backend=BackendPreference.NATIVE,
            workers=2,
            preserve_source_map=True,
            collect_provenance=True,
        )

    assert owner_calls == 0


def test_rdfxml_closure_document_limit_publishes_no_final_owner(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    owner_calls = 0

    def unexpected_owner(*_arguments: object, **_keywords: object) -> object:
        nonlocal owner_calls
        owner_calls += 1
        raise AssertionError("over-limit RDF/XML closure reached final owner publication")

    monkeypatch.setattr(
        cast(Any, extension),
        "_merge_parsed_structural_snapshot_v2",
        unexpected_owner,
    )
    with pytest.raises(ResourceLimitError) as raised:
        _load(
            ROOT,
            _two_document_resolver(),
            backend=BackendPreference.NATIVE,
            workers=2,
            preserve_source_map=True,
            collect_provenance=True,
            limits=ParseLimits(max_documents=1),
        )

    assert raised.value.limit == "max_documents"
    assert raised.value.observed == 2
    assert raised.value.allowed == 1
    assert owner_calls == 0


@pytest.mark.parametrize("phase", ("parse", "fork"))
def test_rdfxml_parser_or_fork_cancellation_publishes_no_closure_owner(
    phase: str,
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    owner_calls = 0
    cancellation_calls = 0

    def unexpected_owner(*_arguments: object, **_keywords: object) -> object:
        nonlocal owner_calls
        owner_calls += 1
        raise AssertionError("cancelled RDF/XML closure reached final owner publication")

    hook_name = (
        "_parse_rdfxml_retained_v2"
        if phase == "parse"
        else "_fork_parsed_structural_storage_v2"
    )
    real_hook = getattr(cast(Any, extension), hook_name)

    def cancel_at_native_entry(
        *arguments: object,
        **keywords: object,
    ) -> object:
        nonlocal cancellation_calls
        cancellation_calls += 1
        cancel = keywords.get("cancel")
        if cancel is None and arguments:
            cancel = arguments[-1]
        assert cancel is not None
        assert cast(Any, cancel).cancel()
        return real_hook(*arguments, **keywords)

    monkeypatch.setattr(cast(Any, extension), hook_name, cancel_at_native_entry)
    monkeypatch.setattr(
        cast(Any, extension),
        "_merge_parsed_structural_snapshot_v2",
        unexpected_owner,
    )
    with pytest.raises(OperationCancelledError):
        _load(
            ROOT,
            _two_document_resolver(),
            backend=BackendPreference.NATIVE,
            workers=2,
            preserve_source_map=False,
            collect_provenance=False,
        )

    assert cancellation_calls == 1
    assert owner_calls == 0


def test_native_rdfxml_fork_cancellation_restores_original_parser_owner(
    monkeypatch: pytest.MonkeyPatch,
    extension: NativeTestExtension,
) -> None:
    retained = native._parse_rdfxml_retained_v2(
        CHILD,
        document_iri="urn:rdfxml:child-document",
        collect_provenance=True,
        preserve_source_map=True,
    )
    real_fork = cast(Any, extension)._fork_parsed_structural_storage_v2

    def cancel_fork(parsed: object, cancel: object | None = None) -> object:
        assert cancel is not None
        assert cast(Any, cancel).cancel()
        return real_fork(parsed, cancel)

    monkeypatch.setattr(
        cast(Any, extension),
        "_fork_parsed_structural_storage_v2",
        cancel_fork,
    )
    with pytest.raises(OperationCancelledError):
        native._fork_parsed_structural_storage_v2(
            retained.storage,
            limits=ParseLimits(cancellation_check_interval=1),
        )

    monkeypatch.setattr(
        cast(Any, extension),
        "_fork_parsed_structural_storage_v2",
        real_fork,
    )
    first = native._fork_parsed_structural_storage_v2(
        retained.storage,
        limits=ParseLimits(cancellation_check_interval=1),
    )
    second = native._fork_parsed_structural_storage_v2(
        retained.storage,
        limits=ParseLimits(cancellation_check_interval=1),
    )
    assert type(first) is type(retained.storage)
    assert type(second) is type(retained.storage)
    assert first is not second


def test_native_rdfxml_provenance_rebind_shares_every_lazy_owner() -> None:
    unexpected = AssertionError("native provenance rebind materialized a structural row")
    with (
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.select",
            autospec=True,
            return_value="native",
        ),
        patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected),
    ):
        document = parse_document(
            CHILD,
            document_iri=IRI("urn:rdfxml:child-document"),
            options=LoadOptions(
                format=DocumentFormat.RDF_XML,
                imports=ImportPolicy.IGNORE,
                backend=BackendPreference.NATIVE,
                preserve_source_map=True,
                collect_provenance=True,
            ),
        )

    digest = hashlib.sha256(CHILD).digest()
    resolved = ResolvedDocument(
        CHILD,
        IRI("urn:rdfxml:child-document"),
        format=DocumentFormat.RDF_XML,
        expected_sha256=digest,
    )
    with patch.object(type(document.axioms), "__iter__", side_effect=unexpected):
        rebound = _with_resolved_provenance(
            document,
            AcquiredImport(CHILD, digest, "cache:child-alias", True),
            resolved,
            "application/rdf+xml",
        )

    assert type(rebound).__name__ == "_NativeOntologyDocument"
    assert rebound is not document
    assert cast(Any, rebound)._native_document_state.owner is cast(
        Any, document
    )._native_document_state.owner
    assert rebound.ontology_id is document.ontology_id
    assert rebound.document_iri is document.document_iri
    assert rebound.direct_imports is document.direct_imports
    assert rebound.ontology_annotations is document.ontology_annotations
    assert rebound.axioms is document.axioms
    assert rebound.extension_components is document.extension_components
    assert rebound.source_map is document.source_map
    assert rebound.origin_index is document.origin_index
    assert rebound.rdf_mapping_report is document.rdf_mapping_report
    assert rebound.diagnostics is document.diagnostics
    assert rebound.document_fingerprint is document.document_fingerprint
    assert rebound.provenance == replace(
        document.provenance,
        expected_sha256=digest,
        acquisition_locator="cache:child-alias",
        media_type="application/rdf+xml",
    )


def _diamond_resolver() -> MappingResolver:
    return MappingResolver(
        {
            "urn:rdfxml:left": ResolvedDocument(
                LEFT,
                IRI("urn:rdfxml:left-document"),
                format=DocumentFormat.RDF_XML,
                provenance={"locator": "memory:rdfxml-left"},
            ),
            "urn:rdfxml:right": ResolvedDocument(
                RIGHT,
                IRI("urn:rdfxml:right-document"),
                format=DocumentFormat.RDF_XML,
                provenance={"locator": "memory:rdfxml-right"},
            ),
            "urn:rdfxml:shared-a": ResolvedDocument(
                SHARED,
                IRI("urn:rdfxml:shared-document"),
                format=DocumentFormat.RDF_XML,
                provenance={"locator": "memory:rdfxml-shared-a"},
            ),
            "urn:rdfxml:shared-b": ResolvedDocument(
                SHARED,
                IRI("urn:rdfxml:shared-document"),
                format=DocumentFormat.RDF_XML,
                provenance={"locator": "memory:rdfxml-shared-b"},
            ),
        }
    )


def test_rdfxml_diamond_closure_is_worker_and_shared_digest_exact() -> None:
    reference = _load(
        DIAMOND_ROOT,
        _diamond_resolver(),
        backend=BackendPreference.PYTHON,
        workers=3,
        preserve_source_map=True,
        collect_provenance=True,
    )
    original_parse = native._parse_rdfxml_retained_v2
    with patch(
        "pyowl_core.backends.native._parse_rdfxml_retained_v2",
        wraps=original_parse,
    ) as parse_hook:
        single_worker = _load(
            DIAMOND_ROOT,
            _diamond_resolver(),
            backend=BackendPreference.NATIVE,
            workers=1,
            preserve_source_map=True,
            collect_provenance=True,
        )
        assert parse_hook.call_count == 4
    parallel = _load(
        DIAMOND_ROOT,
        _diamond_resolver(),
        backend=BackendPreference.NATIVE,
        workers=3,
        preserve_source_map=True,
        collect_provenance=True,
    )

    assert len(single_worker.documents) == 4
    assert single_worker == reference
    assert parallel == reference
    assert single_worker.import_manifest == parallel.import_manifest
    assert single_worker.origin_index == parallel.origin_index
    assert encode_snapshot(single_worker) == encode_snapshot(parallel)
    assert encode_snapshot(single_worker) == encode_snapshot(reference)
    assert all(
        document.rdf_mapping_report is not None
        and document.rdf_mapping_report.conformant
        for document in single_worker.documents
    )
    shared_edges = tuple(
        edge
        for edge in single_worker.import_manifest.edges
        if edge.import_iri.value in {"urn:rdfxml:shared-a", "urn:rdfxml:shared-b"}
    )
    assert {edge.sanitized_locator for edge in shared_edges} == {
        "memory:rdfxml-shared-a",
        "memory:rdfxml-shared-b",
    }
    assert len({edge.resolved_document_key for edge in shared_edges}) == 1
    shared_document_key = shared_edges[0].resolved_document_key
    shared_document = next(
        document
        for record, document in zip(
            single_worker.import_manifest.documents,
            single_worker.documents,
            strict=True,
        )
        if record.document_key == shared_document_key
    )
    assert shared_document.provenance.acquisition_locator == "memory:rdfxml-shared-a"
    assert single_worker.report.acquisition_cache_hits >= 1
    assert single_worker.report.document_cache_hits >= 1

    owner = cast(Any, single_worker)._native_snapshot_state.owner.handle._owner_v2
    counters = owner._publication_counters_v2()
    assert counters.parser_bytes == len(DIAMOND_ROOT) + len(LEFT) + len(RIGHT) + len(SHARED)
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0
