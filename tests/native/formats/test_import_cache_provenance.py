from __future__ import annotations

import hashlib
import threading
from collections import Counter
from dataclasses import replace
from typing import Any, cast

import pytest

from pyowl_core import (
    IRI,
    AcquisitionCache,
    BackendPreference,
    DocumentFormat,
    ImportPolicy,
    IntegrityError,
    LoadOptions,
    MappingResolver,
    OntologyDocument,
    OntologySnapshot,
    ParsedDocumentCache,
    ParseLimits,
    ResolvedDocument,
    SnapshotLoader,
    encode_snapshot,
)
from pyowl_core.backends import native, native_ingestion, parser
from tests.native.foundation._support import NativeTestExtension, load_extension


def _functional(
    ontology_iri: str,
    *,
    imports: tuple[str, ...] = (),
    body: tuple[str, ...] = (),
) -> bytes:
    components = [*(f"Import(<{item}>)" for item in imports), *body]
    return (f"Prefix(:=<urn:test#>) Ontology(<{ontology_iri}> {' '.join(components)})").encode()


ROOT = _functional(
    "urn:root",
    imports=("urn:first", "urn:second", "urn:third"),
    body=("Declaration(Class(:Root))",),
)
SHARED = _functional("urn:shared", body=("Declaration(Class(:Shared))",))
UNIQUE = _functional("urn:unique", body=("Declaration(Class(:Unique))",))
SHARED_DIGEST = hashlib.sha256(SHARED).digest()
UNIQUE_DIGEST = hashlib.sha256(UNIQUE).digest()


@pytest.fixture(scope="module", autouse=True)
def extension() -> NativeTestExtension:
    selected = load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available or "parse-functional-v1" not in result.features:
        pytest.skip(result.reason or "native Functional parser capability is unavailable")
    if not hasattr(selected, "_merge_parsed_structural_snapshot_v2"):
        pytest.skip("selected native artifact lacks retained closure composition")
    return selected


def _options(
    backend: BackendPreference,
    *,
    workers: int,
) -> LoadOptions:
    return LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.RESOLVE_LOCAL,
        backend=backend,
        limits=replace(ParseLimits(), max_concurrent_fetches=workers),
        preserve_source_map=True,
        collect_provenance=True,
    )


def _resolver() -> MappingResolver:
    return MappingResolver(
        {
            "urn:first": ResolvedDocument(
                SHARED,
                IRI("urn:shared"),
                format=DocumentFormat.FUNCTIONAL,
                expected_sha256=SHARED_DIGEST,
                provenance={"locator": "cache:first"},
            ),
            "urn:second": ResolvedDocument(
                SHARED,
                IRI("urn:shared"),
                format=DocumentFormat.FUNCTIONAL,
                provenance={"locator": "cache:second"},
            ),
            "urn:third": ResolvedDocument(
                UNIQUE,
                IRI("urn:unique"),
                format=DocumentFormat.FUNCTIONAL,
                expected_sha256=UNIQUE_DIGEST,
                provenance={"locator": "cache:third"},
            ),
        }
    )


def _load(
    backend: BackendPreference,
    *,
    workers: int,
) -> OntologySnapshot:
    return SnapshotLoader(
        acquisition_cache=AcquisitionCache(),
        document_cache=ParsedDocumentCache(),
    ).load(
        ROOT,
        options=_options(backend, workers=workers),
        resolver=_resolver(),
    )


def _document(snapshot: OntologySnapshot, ontology_iri: str) -> OntologyDocument:
    return next(
        document
        for document in snapshot.documents
        if document.ontology_id.ontology_iri == IRI(ontology_iri)
    )


def test_shared_digest_provenance_is_deterministic_across_parallel_native_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    calls_lock = threading.Lock()
    original = cast(Any, parser._parse_import_for_retained_load)

    def counted(*args: Any, **kwargs: Any) -> Any:
        document_iri = kwargs["document_iri"]
        assert isinstance(document_iri, IRI)
        with calls_lock:
            calls.append(document_iri.value)
        return original(*args, **kwargs)

    monkeypatch.setattr(parser, "_parse_import_for_retained_load", counted)

    reference = _load(BackendPreference.PYTHON, workers=1)
    selected_by_workers: dict[int, OntologySnapshot] = {}
    for workers in (1, 3):
        calls.clear()
        selected = _load(BackendPreference.NATIVE, workers=workers)
        selected_by_workers[workers] = selected

        assert Counter(calls) == Counter({"urn:shared": 1, "urn:unique": 1})
        assert selected.capabilities.backend == "native"
        assert type(selected).__name__ == "_NativeOntologySnapshot"
        assert selected.report.acquisition_cache_hits == 1
        assert selected.report.document_cache_hits == 1
        assert selected.import_manifest == reference.import_manifest
        assert selected.structural_fingerprint == reference.structural_fingerprint
        assert selected.logical_fingerprint == reference.logical_fingerprint
        assert selected.signature_fingerprint == reference.signature_fingerprint
        assert selected.origin_index == reference.origin_index
        assert encode_snapshot(selected) == encode_snapshot(reference)

        shared = _document(selected, "urn:shared")
        assert shared.provenance.expected_sha256 == SHARED_DIGEST
        assert shared.provenance.acquisition_locator == "cache:first"
        assert shared.source_map == _document(reference, "urn:shared").source_map
        assert {
            edge.import_iri.value: edge.sanitized_locator for edge in selected.import_manifest.edges
        } == {
            "urn:first": "cache:first",
            "urn:second": "cache:second",
            "urn:third": "cache:third",
        }

    sequential = selected_by_workers[1]
    parallel = selected_by_workers[3]
    assert parallel.import_manifest == sequential.import_manifest
    assert parallel.structural_fingerprint == sequential.structural_fingerprint
    assert parallel.logical_fingerprint == sequential.logical_fingerprint
    assert parallel.signature_fingerprint == sequential.signature_fingerprint
    assert parallel.origin_index == sequential.origin_index
    assert encode_snapshot(parallel) == encode_snapshot(sequential)


def test_later_shared_digest_pin_is_validated_before_batch_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _functional("urn:root", imports=("urn:first", "urn:second"))
    bad_digest = bytes((SHARED_DIGEST[0] ^ 1,)) + SHARED_DIGEST[1:]
    resolver = MappingResolver(
        {
            "urn:first": ResolvedDocument(
                SHARED,
                IRI("urn:shared"),
                format=DocumentFormat.FUNCTIONAL,
                provenance={"locator": "cache:first"},
            ),
            "urn:second": ResolvedDocument(
                SHARED,
                IRI("urn:shared"),
                format=DocumentFormat.FUNCTIONAL,
                expected_sha256=bad_digest,
                provenance={"locator": "cache:second"},
            ),
        }
    )
    final_owner_calls = 0

    def unexpected_final_owner(*_args: object, **_kwargs: object) -> object:
        nonlocal final_owner_calls
        final_owner_calls += 1
        raise AssertionError("integrity failure reached final retained publication")

    monkeypatch.setattr(
        native_ingestion,
        "retain_native_snapshot_v2",
        unexpected_final_owner,
    )
    with pytest.raises(IntegrityError) as caught:
        SnapshotLoader(
            acquisition_cache=AcquisitionCache(),
            document_cache=ParsedDocumentCache(),
        ).load(
            root,
            options=_options(BackendPreference.NATIVE, workers=2),
            resolver=resolver,
        )

    assert caught.value.code == "IMPORT_DIGEST_MISMATCH"
    assert final_owner_calls == 0
