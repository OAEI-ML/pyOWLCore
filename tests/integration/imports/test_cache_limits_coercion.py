from __future__ import annotations

from dataclasses import replace

import pytest

from pyowl_core import (
    AcquisitionCache,
    AdapterCompatibilityError,
    CancellationSource,
    ImportPolicy,
    IntegrityError,
    MappingResolver,
    OperationCancelledError,
    OptionConflictError,
    ParsedDocumentCache,
    ParseLimits,
    ProfileError,
    ResolvedDocument,
    ResourceLimitError,
    SnapshotLoader,
    UnresolvedImportWarning,
    coerce_snapshot,
    load_snapshot,
    parse_document,
)
from pyowl_core.backends.python import PythonParser

from .conftest import functional, load_options


class _Provider:
    def __init__(self, snapshot: object) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def owl_snapshot(self) -> object:
        self.calls += 1
        return self.snapshot


def test_coercion_preserves_identity_and_invokes_provider_once() -> None:
    document = parse_document(
        functional("urn:root", body=("Declaration(Class(:A))",)),
        options=load_options(ImportPolicy.IGNORE),
    )
    snapshot = load_snapshot(document, options=load_options(ImportPolicy.IGNORE))
    provider = _Provider(snapshot)

    assert snapshot.root is document
    assert coerce_snapshot(snapshot) is snapshot
    assert coerce_snapshot(provider) is snapshot  # type: ignore[arg-type]
    assert provider.calls == 1
    assert coerce_snapshot(document, options=load_options(ImportPolicy.IGNORE)).root is document

    with pytest.raises(TypeError):
        load_snapshot(snapshot)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        parse_document(snapshot)  # type: ignore[arg-type]

    invalid = _Provider(object())
    with pytest.raises(AdapterCompatibilityError) as incompatible:
        coerce_snapshot(invalid)  # type: ignore[arg-type]
    assert invalid.calls == 1
    assert incompatible.value.code == "ADAPTER_PROVIDER_RESULT"


def test_existing_view_option_and_resolver_conflicts_never_rebuild() -> None:
    snapshot = load_snapshot(
        functional("urn:root"),
        options=load_options(ImportPolicy.IGNORE),
    )
    with pytest.raises(OptionConflictError) as imports:
        coerce_snapshot(snapshot, options=load_options(ImportPolicy.RESOLVE_LOCAL))
    assert imports.value.code == "VIEW_IMPORT_OPTION_CONFLICT"
    with pytest.raises(OptionConflictError) as resolver:
        coerce_snapshot(snapshot, resolver=MappingResolver({}))
    assert resolver.value.code == "VIEW_RESOLVER_CONFLICT"


def test_diamond_uses_acquisition_and_document_caches_once(monkeypatch: pytest.MonkeyPatch) -> None:
    root = parse_document(
        functional("urn:root", imports=("urn:left", "urn:right")),
        options=load_options(ImportPolicy.IGNORE),
    )
    left = functional("urn:left", imports=("urn:leaf",))
    right = functional("urn:right", imports=("urn:leaf",))
    leaf = functional("urn:leaf", body=("Declaration(Class(:Leaf))",))
    loader = SnapshotLoader(
        acquisition_cache=AcquisitionCache(),
        document_cache=ParsedDocumentCache(),
    )
    original = PythonParser.parse
    calls = 0

    def counted(self: PythonParser, *args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(PythonParser, "parse", counted)
    snapshot = loader.load(
        root,
        options=load_options(ImportPolicy.RESOLVE_LOCAL),
        resolver=MappingResolver({"urn:left": left, "urn:right": right, "urn:leaf": leaf}),
    )
    assert calls == 3
    assert snapshot.report.acquisition_cache_hits >= 1
    assert snapshot.report.document_cache_hits >= 1


class _CrashingDocumentCache(ParsedDocumentCache):
    def publish(self, key: tuple[object, ...], document: object) -> object:
        del key, document
        raise RuntimeError("simulated cache publication crash")


def test_cache_publication_crash_returns_no_partial_snapshot() -> None:
    loader = SnapshotLoader(
        acquisition_cache=AcquisitionCache(),
        document_cache=_CrashingDocumentCache(),
    )
    with pytest.raises(RuntimeError, match="publication crash"):
        loader.load(
            parse_document(
                functional("urn:root", imports=("urn:child",)),
                options=load_options(ImportPolicy.IGNORE),
            ),
            options=load_options(ImportPolicy.RESOLVE_LOCAL),
            resolver=MappingResolver({"urn:child": functional("urn:child")}),
        )


def test_integrity_failure_occurs_before_parse_or_cache_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = parse_document(
        functional("urn:root", imports=("urn:child",)),
        options=load_options(ImportPolicy.IGNORE),
    )
    calls = 0
    original = PythonParser.parse

    def counted(self: PythonParser, *args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(PythonParser, "parse", counted)
    with pytest.raises(IntegrityError):
        SnapshotLoader(
            acquisition_cache=AcquisitionCache(),
            document_cache=ParsedDocumentCache(),
        ).load(
            root,
            options=load_options(ImportPolicy.RESOLVE_LOCAL),
            resolver=MappingResolver(
                {
                    "urn:child": ResolvedDocument(
                        functional("urn:child"),
                        root.ontology_id.ontology_iri or pytest.fail("root IRI missing"),
                        expected_sha256=b"x" * 32,
                    )
                }
            ),
        )
    assert calls == 0


@pytest.mark.parametrize(
    ("limits", "root", "mapping", "expected_limit"),
    [
        (
            replace(ParseLimits(), max_documents=1),
            functional("urn:root", imports=("urn:child",)),
            {"urn:child": functional("urn:child")},
            "max_documents",
        ),
        (
            replace(ParseLimits(), max_axioms=1),
            functional("urn:root", imports=("urn:child",), body=("Declaration(Class(:A))",)),
            {"urn:child": functional("urn:child", body=("Declaration(Class(:B))",))},
            "max_axioms",
        ),
        (
            replace(ParseLimits(), max_import_depth=1),
            functional("urn:root", imports=("urn:child",)),
            {
                "urn:child": functional("urn:child", imports=("urn:leaf",)),
                "urn:leaf": functional("urn:leaf"),
            },
            "max_import_depth",
        ),
        (
            replace(ParseLimits(), max_resolver_attempts=1),
            functional("urn:root", imports=("urn:a", "urn:b")),
            {"urn:a": functional("urn:a"), "urn:b": functional("urn:b")},
            "max_resolver_attempts",
        ),
        (
            replace(ParseLimits(), max_origin_entries=1),
            functional(
                "urn:root",
                body=("Declaration(Class(:A))", "Declaration(Class(:B))"),
            ),
            {},
            "max_origin_entries",
        ),
    ],
)
def test_closure_limits_fail_without_snapshot(
    limits: ParseLimits,
    root: bytes,
    mapping: dict[str, bytes],
    expected_limit: str,
) -> None:
    with pytest.raises(ResourceLimitError) as caught:
        load_snapshot(
            root,
            options=load_options(ImportPolicy.RESOLVE_LOCAL, limits=limits),
            resolver=MappingResolver(mapping),
        )
    assert caught.value.limit == expected_limit


def test_total_bytes_terms_deadline_and_cancellation_limits() -> None:
    root = functional("urn:root", imports=("urn:a", "urn:b"))
    first = functional("urn:a", body=("Declaration(Class(:A))",))
    second = functional("urn:b", body=("Declaration(Class(:B))",))
    total_limit = len(root) + len(first) + len(second) - 1
    with pytest.raises(ResourceLimitError) as total:
        load_snapshot(
            root,
            options=load_options(
                ImportPolicy.RESOLVE_LOCAL,
                limits=replace(ParseLimits(), max_total_source_bytes=total_limit),
            ),
            resolver=MappingResolver({"urn:a": first, "urn:b": second}),
        )
    assert total.value.limit == "max_total_source_bytes"

    with pytest.raises(ResourceLimitError) as terms:
        load_snapshot(
            root,
            options=load_options(
                ImportPolicy.RESOLVE_LOCAL,
                limits=replace(ParseLimits(), max_terms=3),
            ),
            resolver=MappingResolver({"urn:a": first, "urn:b": second}),
        )
    assert terms.value.limit == "max_terms"

    with pytest.raises(ResourceLimitError) as deadline:
        load_snapshot(
            root,
            options=load_options(
                ImportPolicy.IGNORE,
                limits=replace(ParseLimits(), deadline_seconds=1e-12),
            ),
        )
    assert deadline.value.limit == "deadline_seconds"

    source = CancellationSource()
    source.cancel("test")
    with pytest.raises(OperationCancelledError):
        load_snapshot(
            root,
            options=load_options(ImportPolicy.IGNORE),
            cancellation_token=source.token,
        )


def test_diagnostic_cap_publishes_deterministic_suppression_count() -> None:
    limits = replace(ParseLimits(), max_diagnostics=2)
    with pytest.warns(UnresolvedImportWarning):
        snapshot = load_snapshot(
            functional("urn:root", imports=("urn:a", "urn:b", "urn:c")),
            options=load_options(
                ImportPolicy.RECORD_UNRESOLVED,
                limits=limits,
            ),
            resolver=MappingResolver({}),
        )
    assert len(snapshot.report.diagnostics) == 2
    suppressed = snapshot.report.diagnostics[-1]
    assert suppressed.code == "DIAGNOSTICS_SUPPRESSED"
    assert suppressed.details["count"] == 2


def test_closure_level_owl2_dl_validation() -> None:
    valid = load_snapshot(
        functional(
            "urn:root",
            body=(
                "Declaration(Class(:A))",
                "Declaration(Class(:B))",
                "SubClassOf(:A :B)",
            ),
        ),
        options=load_options(ImportPolicy.RESOLVE_LOCAL, validate_owl2_dl=True),
    )
    assert valid.owl2_dl_report is not None
    assert valid.owl2_dl_report.conforms
    assert "owl2-dl-validated" in valid.capabilities.features

    with pytest.raises(ProfileError) as incomplete:
        load_snapshot(
            functional("urn:root", imports=("urn:missing",)),
            options=load_options(
                ImportPolicy.RECORD_UNRESOLVED,
                validate_owl2_dl=True,
            ),
            resolver=MappingResolver({}),
        )
    assert incomplete.value.code == "OWL2DL_INCOMPLETE_CLOSURE"
