from __future__ import annotations

import hashlib
import time
from dataclasses import replace

import pytest

from pyowl_core import (
    IRI,
    AccessDeniedError,
    CompositeResolver,
    DocumentIdentityConflictError,
    HttpAcquisitionCache,
    HttpCacheEntry,
    HttpResolver,
    ImportPolicy,
    ImportRequest,
    ImportStatus,
    MappingResolver,
    ParseLimits,
    ResolvedDocument,
    UnresolvedImportError,
    UnresolvedImportWarning,
    load_snapshot,
)

from .conftest import functional, load_options


class _SpyResolver:
    def __init__(self, values: dict[str, bytes] | None = None) -> None:
        self.values = values or {}
        self.calls: list[str] = []

    def resolve(self, request: ImportRequest) -> ResolvedDocument | None:
        self.calls.append(request.import_iri.value)
        value = self.values.get(request.import_iri.value)
        return None if value is None else ResolvedDocument(value, request.import_iri)


def test_import_policy_matrix_missing_and_success() -> None:
    root = functional("urn:root", imports=("urn:child",))
    child = functional("urn:child", body=("Declaration(Class(:Child))",))

    ignored_resolver = _SpyResolver({"urn:child": child})
    ignored = load_snapshot(
        root,
        options=load_options(ImportPolicy.IGNORE),
        resolver=ignored_resolver,
    )
    assert ignored_resolver.calls == []
    assert ignored.import_manifest.edges[0].status is ImportStatus.IGNORED
    assert not ignored.is_complete

    missing_resolver = _SpyResolver()
    with pytest.warns(UnresolvedImportWarning):
        recorded = load_snapshot(
            root,
            options=load_options(ImportPolicy.RECORD_UNRESOLVED),
            resolver=missing_resolver,
        )
    assert missing_resolver.calls == ["urn:child"]
    assert recorded.import_manifest.edges[0].status is ImportStatus.UNRESOLVED
    assert not recorded.is_complete

    for policy in (ImportPolicy.RESOLVE_LOCAL, ImportPolicy.RESOLVE_STRICT):
        with pytest.raises(UnresolvedImportError):
            load_snapshot(root, options=load_options(policy), resolver=_SpyResolver())

    complete = load_snapshot(
        root,
        options=load_options(ImportPolicy.RESOLVE_LOCAL),
        resolver=_SpyResolver({"urn:child": child}),
    )
    assert complete.is_complete
    assert len(complete.documents) == 2


def test_record_policy_retains_malformed_failure_without_partial_document() -> None:
    root = functional("urn:root", imports=("urn:bad",))
    with pytest.warns(UnresolvedImportWarning):
        snapshot = load_snapshot(
            root,
            options=load_options(ImportPolicy.RECORD_UNRESOLVED),
            resolver=MappingResolver({"urn:bad": b"not an ontology"}),
        )
    assert len(snapshot.documents) == 1
    assert snapshot.import_manifest.edges[0].status is ImportStatus.FAILED
    assert snapshot.report.diagnostics[0].code == "FORMAT_AMBIGUOUS"


def test_legal_cycle_is_retained_and_each_document_is_visited_once() -> None:
    first = functional(
        "urn:a",
        imports=("urn:b",),
        body=("Declaration(Class(:A))",),
    )
    second = functional(
        "urn:b",
        imports=("urn:a",),
        body=("Declaration(Class(:B))",),
    )
    resolver = _SpyResolver({"urn:a": first, "urn:b": second})

    snapshot = load_snapshot(
        first,
        options=load_options(ImportPolicy.RESOLVE_LOCAL),
        resolver=resolver,
    )

    assert len(snapshot.documents) == 2
    assert len(snapshot.import_manifest.edges) == 2
    assert all(edge.status is ImportStatus.RESOLVED for edge in snapshot.import_manifest.edges)
    assert resolver.calls == ["urn:b", "urn:a"]


def test_diamond_and_aliases_deduplicate_documents_but_retain_edges() -> None:
    root = functional("urn:root", imports=("urn:left", "urn:right"))
    left = functional("urn:left", imports=("urn:leaf",))
    right = functional("urn:right", imports=("urn:leaf",))
    leaf = functional("urn:leaf", body=("Declaration(Class(:Leaf))",))
    diamond = load_snapshot(
        root,
        options=load_options(ImportPolicy.RESOLVE_LOCAL),
        resolver=MappingResolver(
            {
                "urn:left": left,
                "urn:right": right,
                "urn:leaf": leaf,
            }
        ),
    )
    assert len(diamond.documents) == 4
    assert len(diamond.import_manifest.edges) == 4
    leaf_targets = {
        edge.resolved_document_key
        for edge in diamond.import_manifest.edges
        if edge.import_iri == IRI("urn:leaf")
    }
    assert len(leaf_targets) == 1

    shared = functional("urn:shared", body=("Declaration(Class(:Shared))",))
    aliases = load_snapshot(
        functional("urn:alias-root", imports=("urn:x", "urn:y")),
        options=load_options(ImportPolicy.RESOLVE_LOCAL),
        resolver=MappingResolver(
            {
                "urn:x": ResolvedDocument(shared, IRI("urn:canonical")),
                "urn:y": ResolvedDocument(shared, IRI("urn:canonical")),
            }
        ),
    )
    assert len(aliases.documents) == 2
    assert len(aliases.import_manifest.edges) == 2
    assert len({edge.resolved_document_key for edge in aliases.import_manifest.edges}) == 1


def test_ontology_and_version_identity_conflicts_fail_atomically() -> None:
    root = functional("urn:root", imports=("urn:x", "urn:y"))
    first = functional("urn:same", body=("Declaration(Class(:A))",))
    second = functional("urn:same", body=("Declaration(Class(:B))",))
    with pytest.raises(DocumentIdentityConflictError) as identity:
        load_snapshot(
            root,
            options=load_options(ImportPolicy.RESOLVE_LOCAL),
            resolver=MappingResolver({"urn:x": first, "urn:y": second}),
        )
    assert identity.value.code == "DOCUMENT_IDENTITY_CONFLICT"

    version_one = b"Ontology(<urn:o1> <urn:v> Declaration(Class(<urn:A>)))"
    version_two = b"Ontology(<urn:o2> <urn:v> Declaration(Class(<urn:B>)))"
    with pytest.raises(DocumentIdentityConflictError) as version:
        load_snapshot(
            root,
            options=load_options(ImportPolicy.RESOLVE_LOCAL),
            resolver=MappingResolver({"urn:x": version_one, "urn:y": version_two}),
        )
    assert version.value.code == "DOCUMENT_VERSION_CONFLICT"


class _NetworkSpy:
    network_capable = True

    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, request: ImportRequest) -> ResolvedDocument | None:
        self.calls += 1
        return ResolvedDocument(functional("urn:network"), request.import_iri)


def test_local_policy_never_calls_network_child() -> None:
    network = _NetworkSpy()
    root = functional("urn:root", imports=("urn:missing",))
    with pytest.raises(UnresolvedImportError):
        load_snapshot(
            root,
            options=load_options(ImportPolicy.RESOLVE_LOCAL),
            resolver=CompositeResolver((MappingResolver({}), network)),
        )
    assert network.calls == 0


def test_offline_strict_policy_uses_only_integrity_checked_http_cache() -> None:
    import_iri = IRI("https://example.test/child.owl")
    child = functional("urn:child", body=("Declaration(Class(:Child))",))
    cache = HttpAcquisitionCache()
    digest = hashlib.sha256(child).digest()
    cache.publish(
        HttpCacheEntry(
            import_iri,
            import_iri,
            child,
            digest,
            "text/owl-functional",
        )
    )
    resolver = HttpResolver(
        allowed_hosts=("example.test",),
        cache=cache,
        integrity={import_iri: digest},
    )
    snapshot = load_snapshot(
        functional("urn:root", imports=(import_iri.value,)),
        options=load_options(ImportPolicy.RESOLVE_STRICT, offline=True),
        resolver=resolver,
    )
    assert snapshot.is_complete
    assert len(snapshot.documents) == 2

    with pytest.raises(UnresolvedImportError):
        load_snapshot(
            functional("urn:root", imports=("https://example.test/missing.owl",)),
            options=load_options(ImportPolicy.RESOLVE_STRICT, offline=True),
            resolver=resolver,
        )


class _FailureResolver:
    def __init__(self, kind: str) -> None:
        self.kind = kind

    def resolve(self, request: ImportRequest) -> ResolvedDocument | None:
        del request
        if self.kind == "denied":
            raise AccessDeniedError("denied")
        raise TimeoutError("slow")


@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("denied", ImportStatus.DENIED, "ACCESS_DENIED"),
        ("timeout", ImportStatus.FAILED, "IMPORT_RESOLUTION_TIMEOUT"),
    ],
)
def test_record_policy_normalizes_resolution_failure_classes(
    kind: str,
    status: ImportStatus,
    code: str,
) -> None:
    with pytest.warns(UnresolvedImportWarning):
        snapshot = load_snapshot(
            functional("urn:root", imports=("urn:child",)),
            options=load_options(ImportPolicy.RECORD_UNRESOLVED),
            resolver=_FailureResolver(kind),
        )
    edge = snapshot.import_manifest.edges[0]
    assert edge.status is status
    assert edge.diagnostic is not None and edge.diagnostic.code == code


class _DelayedResolver:
    def __init__(self, documents: dict[str, bytes]) -> None:
        self.documents = documents

    def resolve(self, request: ImportRequest) -> ResolvedDocument | None:
        time.sleep(0.002 if request.import_iri.value.endswith("a") else 0.0001)
        data = self.documents.get(request.import_iri.value)
        return None if data is None else ResolvedDocument(data, request.import_iri)


def test_parallel_and_sequential_schedules_freeze_identically() -> None:
    root = functional("urn:root", imports=("urn:a", "urn:b", "urn:c"))
    documents = {
        name: functional(name, body=(f"Declaration(Class(<{name}#Class>))",))
        for name in ("urn:a", "urn:b", "urn:c")
    }
    parallel = load_snapshot(
        root,
        options=load_options(ImportPolicy.RESOLVE_LOCAL),
        resolver=_DelayedResolver(documents),
    )
    sequential_limits = replace(ParseLimits(), max_concurrent_fetches=1)
    sequential = load_snapshot(
        root,
        options=load_options(ImportPolicy.RESOLVE_LOCAL, limits=sequential_limits),
        resolver=_DelayedResolver(documents),
    )
    assert parallel.import_manifest == sequential.import_manifest
    assert parallel.structural_fingerprint == sequential.structural_fingerprint
    assert parallel.logical_fingerprint == sequential.logical_fingerprint
