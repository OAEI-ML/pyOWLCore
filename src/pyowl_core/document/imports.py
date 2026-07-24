"""Deterministic import-closure traversal, manifests, and content caches."""

from __future__ import annotations

import hashlib
import os
import threading
import time
import warnings
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, TypeAlias
from urllib.parse import urlsplit, urlunsplit

from pyowl_core.cancellation import CancellationToken
from pyowl_core.config import BackendPreference, DocumentFormat, ImportPolicy, LoadOptions
from pyowl_core.diagnostics import Diagnostic, Severity
from pyowl_core.exceptions import (
    AccessDeniedError,
    BackendUnavailableError,
    DocumentIdentityConflictError,
    ImportResolutionError,
    IntegrityError,
    OperationCancelledError,
    OptionConflictError,
    ParseError,
    ResourceLimitError,
    UnresolvedImportError,
    UnresolvedImportWarning,
)
from pyowl_core.io.resolver import (
    ImportRequest,
    ImportResolver,
    ResolutionKind,
    ResolutionMode,
    ResolvedDocument,
    ResolverOutcome,
    resolver_configuration_fingerprint,
)
from pyowl_core.io.resolver.base import resolve_with_mode
from pyowl_core.io.source import DocumentSource, acquire_source
from pyowl_core.model import IRI, canonical_bytes, encode_varint, walk

from .document import Fingerprint, OntologyDocument, OntologyID

if TYPE_CHECKING:
    from .snapshot import OntologySnapshot

DocumentInput: TypeAlias = DocumentSource | OntologyDocument


class DocumentStatus(str, Enum):
    ROOT = "root"
    RESOLVED = "resolved"


class ImportStatus(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    IGNORED = "ignored"
    DENIED = "denied"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, order=True)
class DocumentRecord:
    document_key: str
    ontology_id: OntologyID
    document_iri: IRI | None
    source_sha256: bytes
    document_fingerprint: Fingerprint
    format: DocumentFormat
    status: DocumentStatus

    def __post_init__(self) -> None:
        if not isinstance(self.document_key, str) or not self.document_key:
            raise ValueError("document_key must be a nonempty string")
        if not isinstance(self.ontology_id, OntologyID):
            raise TypeError("ontology_id must be OntologyID")
        if self.document_iri is not None and not isinstance(self.document_iri, IRI):
            raise TypeError("document_iri must be IRI or None")
        if not isinstance(self.source_sha256, bytes) or len(self.source_sha256) != 32:
            raise ValueError("source_sha256 must be exactly 32 bytes")
        if not isinstance(self.document_fingerprint, Fingerprint):
            raise TypeError("document_fingerprint must be Fingerprint")
        if not isinstance(self.format, DocumentFormat):
            raise TypeError("format must be DocumentFormat")
        if not isinstance(self.status, DocumentStatus):
            raise TypeError("status must be DocumentStatus")


@dataclass(frozen=True, slots=True, order=True)
class ImportEdge:
    importing_document_key: str
    import_iri: IRI
    status: ImportStatus
    resolved_document_key: str | None = None
    resolver_name: str | None = None
    sanitized_locator: str | None = field(default=None, compare=False)
    diagnostic: Diagnostic | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.importing_document_key, str) or not self.importing_document_key:
            raise ValueError("importing_document_key must be a nonempty string")
        if not isinstance(self.import_iri, IRI):
            raise TypeError("import_iri must be IRI")
        if not isinstance(self.status, ImportStatus):
            raise TypeError("status must be ImportStatus")
        if self.status is ImportStatus.RESOLVED and not self.resolved_document_key:
            raise ValueError("resolved edge requires resolved_document_key")
        if self.status is not ImportStatus.RESOLVED and self.resolved_document_key is not None:
            raise ValueError("only resolved edge may have resolved_document_key")
        for name in ("resolved_document_key", "resolver_name", "sanitized_locator"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be a nonempty string or None")
        if self.diagnostic is not None and not isinstance(self.diagnostic, Diagnostic):
            raise TypeError("diagnostic must be Diagnostic or None")


@dataclass(frozen=True, slots=True)
class ImportManifest:
    policy: ImportPolicy
    offline: bool
    resolver_configuration_fingerprint: bytes
    documents: tuple[DocumentRecord, ...]
    edges: tuple[ImportEdge, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.policy, ImportPolicy):
            raise TypeError("policy must be ImportPolicy")
        if not isinstance(self.offline, bool):
            raise TypeError("offline must be bool")
        if (
            not isinstance(self.resolver_configuration_fingerprint, bytes)
            or len(self.resolver_configuration_fingerprint) != 32
        ):
            raise ValueError("resolver_configuration_fingerprint must be exactly 32 bytes")
        documents = tuple(sorted(self.documents, key=lambda item: item.document_key))
        edges = tuple(
            sorted(
                self.edges,
                key=lambda item: (
                    item.importing_document_key,
                    canonical_bytes(item.import_iri),
                    item.status.value,
                    item.resolved_document_key or "",
                ),
            )
        )
        if not all(isinstance(item, DocumentRecord) for item in documents):
            raise TypeError("documents must contain DocumentRecord values")
        if not all(isinstance(item, ImportEdge) for item in edges):
            raise TypeError("edges must contain ImportEdge values")
        keys = {item.document_key for item in documents}
        if len(keys) != len(documents):
            raise ValueError("manifest document keys must be unique")
        for edge in edges:
            if edge.importing_document_key not in keys:
                raise ValueError("edge importer is absent from manifest")
            if edge.resolved_document_key is not None and edge.resolved_document_key not in keys:
                raise ValueError("edge target is absent from manifest")
        object.__setattr__(self, "documents", documents)
        object.__setattr__(self, "edges", edges)

    @property
    def is_complete(self) -> bool:
        return all(edge.status is ImportStatus.RESOLVED for edge in self.edges)

    def canonical_bytes(self) -> bytes:
        pieces = [
            b"pyowl-core:import-manifest:v1\x00",
            _frame(self.policy.value.encode("ascii")),
            bytes((int(self.offline),)),
            self.resolver_configuration_fingerprint,
            encode_varint(len(self.documents)),
        ]
        for record in self.documents:
            pieces.append(_record_bytes(record))
        pieces.append(encode_varint(len(self.edges)))
        for edge in self.edges:
            pieces.append(_edge_bytes(edge))
        return b"".join(pieces)


@dataclass(frozen=True, slots=True)
class AcquiredImport:
    data: bytes
    source_sha256: bytes
    locator: str | None
    cache_hit: bool


class AcquisitionCache:
    """Exact-byte, stat-ledger cache with atomic publication."""

    __slots__ = ("_aliases", "_content", "_lock")

    def __init__(self) -> None:
        self._content: dict[bytes, bytes] = {}
        self._aliases: dict[tuple[object, ...], bytes] = {}
        self._lock = threading.Lock()

    def acquire(
        self,
        resolved: ResolvedDocument,
        *,
        limits: object,
        cancellation_token: CancellationToken | None = None,
    ) -> AcquiredImport:
        from pyowl_core.limits import ParseLimits

        if not isinstance(resolved, ResolvedDocument):
            raise TypeError("resolved must be ResolvedDocument")
        if not isinstance(limits, ParseLimits):
            raise TypeError("limits must be ParseLimits")
        try:
            key = _acquisition_key(resolved.source)
        except OSError as error:
            raise ImportResolutionError(
                "resolved import source is unavailable",
                code="IMPORT_SOURCE_NOT_FOUND",
            ) from error
        if key is not None:
            with self._lock:
                digest = self._aliases.get(key)
                data = None if digest is None else self._content.get(digest)
            if digest is not None and data is not None:
                if hashlib.sha256(data).digest() != digest:
                    raise IntegrityError(
                        "acquisition cache is corrupt", code="ACQUISITION_CACHE_CORRUPT"
                    )
                _check_expected(digest, resolved.expected_sha256)
                return AcquiredImport(data, digest, _resolved_locator(resolved), True)
        if isinstance(resolved.source, bytes):
            limits.enforce("max_source_bytes", len(resolved.source))
            if cancellation_token is not None:
                cancellation_token.check()
            digest = hashlib.sha256(resolved.source).digest()
            _check_expected(digest, resolved.expected_sha256)
            with self._lock:
                self._content[digest] = resolved.source
                if key is not None:
                    self._aliases[key] = digest
            return AcquiredImport(
                resolved.source,
                digest,
                _resolved_locator(resolved),
                False,
            )
        try:
            payload = acquire_source(
                resolved.source,
                format=resolved.format,
                document_iri=resolved.document_iri,
                limits=limits,
                cancellation_token=cancellation_token,
            )
        except OSError as error:
            raise ImportResolutionError(
                "resolved import source is unavailable",
                code="IMPORT_SOURCE_NOT_FOUND",
            ) from error
        _check_expected(payload.source_sha256, resolved.expected_sha256)
        # Only a complete and integrity-checked value is made visible.
        with self._lock:
            self._content[payload.source_sha256] = payload.data
            if key is not None:
                self._aliases[key] = payload.source_sha256
        return AcquiredImport(
            payload.data,
            payload.source_sha256,
            _resolved_locator(resolved) or payload.locator,
            False,
        )

    def clear(self) -> None:
        with self._lock:
            self._content.clear()
            self._aliases.clear()


class ParsedDocumentCache:
    """Content/options keyed parsed-document cache; exceptions are not retained."""

    __slots__ = ("_documents", "_lock")

    def __init__(self) -> None:
        self._documents: dict[tuple[object, ...], OntologyDocument] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple[object, ...]) -> OntologyDocument | None:
        with self._lock:
            return self._documents.get(key)

    def publish(self, key: tuple[object, ...], document: OntologyDocument) -> OntologyDocument:
        if not isinstance(document, OntologyDocument):
            raise TypeError("document must be OntologyDocument")
        with self._lock:
            retained = self._documents.setdefault(key, document)
        return retained

    def clear(self) -> None:
        with self._lock:
            self._documents.clear()


@dataclass(frozen=True, slots=True)
class _Pending:
    importing_document_key: str
    importing_document_iri: IRI | None
    import_iri: IRI
    chain: tuple[IRI, ...]
    depth: int


@dataclass(frozen=True, slots=True)
class _Node:
    key: str
    document: OntologyDocument
    status: DocumentStatus


_ImportParseResult: TypeAlias = (
    tuple[OntologyDocument, object | None, tuple[tuple[str, float], ...], bool] | Exception
)


_DEFAULT_ACQUISITION_CACHE = AcquisitionCache()
_DEFAULT_DOCUMENT_CACHE = ParsedDocumentCache()


def _prepare_retained_native_root(options: LoadOptions) -> bool:
    return (
        options.backend in {BackendPreference.AUTO, BackendPreference.NATIVE}
        and not options.validate_owl2_dl
        and options.format
        in {
            None,
            DocumentFormat.FUNCTIONAL,
            DocumentFormat.RDF_XML,
        }
    )


class SnapshotLoader:
    """Reusable loader whose mutable state is limited to atomic content caches."""

    __slots__ = ("_acquisition_cache", "_document_cache")

    def __init__(
        self,
        *,
        acquisition_cache: AcquisitionCache | None = None,
        document_cache: ParsedDocumentCache | None = None,
    ) -> None:
        self._acquisition_cache = acquisition_cache or _DEFAULT_ACQUISITION_CACHE
        self._document_cache = document_cache or _DEFAULT_DOCUMENT_CACHE

    def load(
        self,
        source: DocumentInput,
        *,
        document_iri: IRI | str | None = None,
        options: LoadOptions | None = None,
        resolver: ImportResolver | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> OntologySnapshot:
        from .snapshot import OntologySnapshot

        selected = LoadOptions() if options is None else options
        if not isinstance(selected, LoadOptions):
            raise TypeError("options must be LoadOptions or None")
        if resolver is not None and not isinstance(resolver, ImportResolver):
            raise TypeError("resolver must implement ImportResolver or be None")
        if cancellation_token is not None and not isinstance(cancellation_token, CancellationToken):
            raise TypeError("cancellation_token must be CancellationToken or None")
        if document_iri is not None and not isinstance(document_iri, (IRI, str)):
            raise TypeError("document_iri must be IRI, str, or None")
        if isinstance(source, OntologyDocument) and document_iri is not None:
            raise OptionConflictError(
                "document_iri applies only to an unparsed root source",
                code="DOCUMENT_IRI_SOURCE_CONFLICT",
            )
        if isinstance(source, OntologyDocument) and selected.backend is BackendPreference.NATIVE:
            raise BackendUnavailableError(
                "native snapshot construction is not available in this build",
                code="NATIVE_BACKEND_UNAVAILABLE",
            )
        started = time.monotonic()
        _check_operation(cancellation_token, started, selected)
        native_storage: object | None = None
        native_phase_timings: tuple[tuple[str, float], ...] = ()
        root_parse_started = time.monotonic()
        if isinstance(source, OntologyDocument):
            root = source
            root_cache_hit = False
        elif _prepare_retained_native_root(selected):
            from pyowl_core.backends.parser import _parse_document_for_retained_load

            parsed = _parse_document_for_retained_load(
                source,
                document_iri=document_iri,
                options=selected,
                resolver=resolver,
                cancellation_token=cancellation_token,
                load_started=started,
                root_parse_started=root_parse_started,
            )
            if parsed.snapshot is not None:
                return parsed.snapshot
            if parsed.document is None:
                raise AssertionError("retained root parse did not publish a document")
            root = parsed.document
            native_storage = parsed.native_storage
            native_phase_timings = parsed.phase_timings
            root_cache_hit = False
        else:
            from pyowl_core.backends.parser import parse_document

            root = parse_document(
                source,
                document_iri=document_iri,
                options=selected,
                cancellation_token=cancellation_token,
            )
            root_cache_hit = False
        root_parse_seconds = time.monotonic() - root_parse_started
        root_node = _node(root, DocumentStatus.ROOT)
        nodes: dict[str, _Node] = {root_node.key: root_node}
        source_identity: dict[tuple[bytes, bytes], _Node] = {_source_identity(root): root_node}
        native_storages: dict[tuple[bytes, bytes], object] = {}
        if native_storage is not None:
            native_storages[_source_identity(root)] = native_storage
        ontology_identity: dict[tuple[str, ...], _Node] = {}
        version_identity: dict[str, _Node] = {}
        _register_identity(root_node, ontology_identity, version_identity)
        counters = {
            "total_source_bytes": root.provenance.byte_length,
            "axioms": len(root.axioms),
            "terms": _document_terms(root),
            "resolver_attempts": 0,
            "acquisition_cache_hits": int(root_cache_hit),
            "document_cache_hits": 0,
        }
        _enforce_closure_limits(selected, len(nodes), counters)
        edges: list[ImportEdge] = []
        diagnostics: list[Diagnostic] = []
        pending = _initial_pending(root_node, selected)
        if selected.imports is ImportPolicy.IGNORE:
            edges.extend(
                ImportEdge(item.importing_document_key, item.import_iri, ImportStatus.IGNORED)
                for item in pending
            )
            pending = []
        elif selected.imports is ImportPolicy.RECORD_UNRESOLVED and resolver is None:
            recorded_edges, recorded_diagnostics, resolution_attempts = (
                _record_unresolved_without_resolver(
                    root_node.key,
                    root.document_iri,
                    root.direct_imports,
                    selected,
                )
            )
            edges.extend(recorded_edges)
            diagnostics.extend(recorded_diagnostics)
            counters["resolver_attempts"] += resolution_attempts
            pending = []
        while pending:
            _check_operation(cancellation_token, started, selected)
            pending.sort(key=_pending_key)
            selected.limits.enforce(
                "max_resolver_attempts",
                counters["resolver_attempts"] + len(pending),
            )
            outcomes = self._resolve_batch(
                pending,
                selected,
                resolver,
                cancellation_token,
                started,
            )
            next_pending: list[_Pending] = []
            acquired_by_index: dict[int, tuple[AcquiredImport, ResolvedDocument]] = {}
            acquisition_errors: dict[int, Exception] = {}
            parse_rows: list[tuple[int, AcquiredImport, ResolvedDocument]] = []
            process_count = len(pending)
            for index, (_item, outcome) in enumerate(zip(pending, outcomes, strict=True)):
                _check_operation(cancellation_token, started, selected)
                counters["resolver_attempts"] += max(1, len(outcome.attempts))
                selected.limits.enforce("max_resolver_attempts", counters["resolver_attempts"])
                if outcome.kind is not ResolutionKind.RESOLVED:
                    if selected.imports is not ImportPolicy.RECORD_UNRESOLVED:
                        process_count = index + 1
                        break
                    continue
                resolved = outcome.resolved
                if resolved is None:
                    raise AssertionError("resolved outcome has no document")
                try:
                    acquired = self._acquisition_cache.acquire(
                        resolved,
                        limits=selected.limits,
                        cancellation_token=cancellation_token,
                    )
                    counters["acquisition_cache_hits"] += int(acquired.cache_hit)
                except (ResourceLimitError, OperationCancelledError, MemoryError) as error:
                    acquisition_errors[index] = error
                    process_count = index + 1
                    break
                except ImportResolutionError as error:
                    acquisition_errors[index] = error
                    if selected.imports is not ImportPolicy.RECORD_UNRESOLVED:
                        process_count = index + 1
                        break
                    continue
                acquired_by_index[index] = (acquired, resolved)
                parse_rows.append((index, acquired, resolved))

            parsed_by_index = self._parse_import_batch(
                tuple(parse_rows),
                selected,
                cancellation_token,
                started,
            )
            for index, (item, outcome) in enumerate(
                zip(pending[:process_count], outcomes[:process_count], strict=True)
            ):
                _check_operation(cancellation_token, started, selected)
                if outcome.kind is not ResolutionKind.RESOLVED:
                    edge, diagnostic = _failed_edge(item, outcome, selected.imports)
                    if selected.imports is not ImportPolicy.RECORD_UNRESOLVED:
                        _raise_resolution(item, outcome)
                    edges.append(edge)
                    _append_diagnostic(diagnostics, diagnostic, selected)
                    continue

                batch_error = acquisition_errors.get(index)
                parse_result = parsed_by_index.get(index)
                if batch_error is None and isinstance(parse_result, Exception):
                    batch_error = parse_result
                if batch_error is not None:
                    if isinstance(
                        batch_error,
                        (ResourceLimitError, OperationCancelledError, MemoryError),
                    ):
                        raise batch_error
                    if not isinstance(batch_error, (ImportResolutionError, ParseError)):
                        raise batch_error
                    if selected.imports is not ImportPolicy.RECORD_UNRESOLVED:
                        raise batch_error
                    failed = _outcome_from_error(outcome.resolver_name, batch_error)
                    edge, diagnostic = _failed_edge(item, failed, selected.imports)
                    edges.append(edge)
                    _append_diagnostic(diagnostics, diagnostic, selected)
                    continue
                if not isinstance(parse_result, tuple):
                    raise AssertionError("resolved import has no parse result")
                document, parsed_storage, parsed_phase_timings, cache_hit = parse_result
                counters["document_cache_hits"] += int(cache_hit)
                acquired, _resolved = acquired_by_index[index]
                candidate = _node(
                    document,
                    DocumentStatus.RESOLVED,
                    import_iri=item.import_iri,
                )
                retained = source_identity.get(_source_identity(document))
                if retained is None:
                    _register_identity(candidate, ontology_identity, version_identity)
                    collision = nodes.get(candidate.key)
                    if collision is not None and _source_identity(
                        collision.document
                    ) != _source_identity(document):
                        raise DocumentIdentityConflictError(
                            "canonical document key collision",
                            code="DOCUMENT_KEY_CONFLICT",
                        )
                    retained = candidate
                    nodes[candidate.key] = candidate
                    source_identity[_source_identity(document)] = candidate
                    counters["total_source_bytes"] += document.provenance.byte_length
                    counters["axioms"] += len(document.axioms)
                    counters["terms"] += _document_terms(document)
                    _enforce_closure_limits(selected, len(nodes), counters)
                    next_pending.extend(_initial_pending(candidate, selected, parent=item))
                    if parsed_storage is not None:
                        native_storages[_source_identity(document)] = parsed_storage
                        native_phase_timings = _merge_phase_timings(
                            native_phase_timings,
                            parsed_phase_timings,
                        )
                edges.append(
                    ImportEdge(
                        item.importing_document_key,
                        item.import_iri,
                        ImportStatus.RESOLVED,
                        retained.key,
                        outcome.resolver_name,
                        acquired.locator,
                    )
                )
            pending = next_pending
        records = tuple(_record(node) for node in nodes.values())
        manifest = ImportManifest(
            selected.imports,
            selected.offline,
            resolver_configuration_fingerprint(resolver),
            records,
            tuple(edges),
        )
        ordered_documents = tuple(
            nodes[record.document_key].document for record in manifest.documents
        )
        elapsed = time.monotonic() - started
        timings = {"load_seconds": elapsed}
        if native_storage is not None:
            timings["root_parse_seconds"] = root_parse_seconds
            timings.update(native_phase_timings)
        snapshot = OntologySnapshot(
            root,
            ordered_documents,
            manifest,
            root_node.key,
            selected,
            diagnostics=tuple(diagnostics),
            timings=timings,
            resolution_attempts=counters["resolver_attempts"],
            acquisition_cache_hits=counters["acquisition_cache_hits"],
            document_cache_hits=counters["document_cache_hits"],
        )
        if selected.backend is BackendPreference.NATIVE or native_storage is not None:
            from pyowl_core.backends.native_ingestion import retain_native_snapshot_v2

            parsed_native_storage: object | None = native_storage
            if len(ordered_documents) > 1 and len(native_storages) == len(ordered_documents):
                parsed_native_storage = tuple(
                    native_storages[_source_identity(document)] for document in ordered_documents
                )
            snapshot = retain_native_snapshot_v2(
                snapshot,
                cancellation_token=cancellation_token,
                parsed_native_storage=parsed_native_storage,
            )
        for diagnostic in diagnostics:
            warnings.warn(diagnostic.message, UnresolvedImportWarning, stacklevel=3)
        return snapshot

    def _resolve_batch(
        self,
        pending: list[_Pending],
        options: LoadOptions,
        resolver: ImportResolver | None,
        token: CancellationToken | None,
        started: float,
    ) -> tuple[ResolverOutcome, ...]:
        mode = _resolution_mode(options)

        def resolve_one(item: _Pending) -> ResolverOutcome:
            _check_operation(token, started, options)
            if resolver is None:
                return ResolverOutcome.missing("none")
            request = ImportRequest(
                item.import_iri,
                item.importing_document_iri,
                item.chain,
                options.limits,
            )
            result = resolve_with_mode(resolver, request, mode=mode)
            _check_operation(token, started, options)
            return result

        workers = min(options.limits.max_concurrent_fetches, len(pending))
        if workers <= 1:
            return tuple(resolve_one(item) for item in pending)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pyowl-import") as pool:
            return tuple(pool.map(resolve_one, pending))

    def _parse_import_batch(
        self,
        rows: tuple[tuple[int, AcquiredImport, ResolvedDocument], ...],
        options: LoadOptions,
        cancellation_token: CancellationToken | None,
        started: float,
    ) -> dict[int, _ImportParseResult]:
        if not rows:
            return {}
        grouped: dict[
            tuple[object, ...],
            list[tuple[int, AcquiredImport, ResolvedDocument]],
        ] = {}
        for row in rows:
            _index, acquired, resolved = row
            key = _parsed_document_key(acquired, resolved, options)
            grouped.setdefault(key, []).append(row)
        unique_rows = tuple(group[0] for group in grouped.values())

        def parse_one(
            row: tuple[int, AcquiredImport, ResolvedDocument],
        ) -> _ImportParseResult:
            _index, acquired, resolved = row
            try:
                _check_operation(cancellation_token, started, options)
                parsed = self._parse_import(
                    acquired,
                    resolved,
                    options,
                    cancellation_token,
                )
                _check_operation(cancellation_token, started, options)
                return parsed
            except Exception as error:
                return error

        workers = min(options.limits.max_concurrent_fetches, len(unique_rows))
        if options.backend is BackendPreference.PYTHON:
            workers = 1
        if workers <= 1:
            unique_results = tuple(parse_one(row) for row in unique_rows)
        else:
            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="pyowl-parse",
            ) as pool:
                unique_results = tuple(pool.map(parse_one, unique_rows))

        results: dict[int, _ImportParseResult] = {}
        for group, result in zip(grouped.values(), unique_results, strict=True):
            for offset, (index, acquired, resolved) in enumerate(group):
                if isinstance(result, Exception):
                    results[index] = result
                    continue
                document, storage, phase_timings, cache_hit = result
                results[index] = (
                    _with_resolved_provenance(
                        document,
                        acquired,
                        resolved,
                        resolved.provenance.get("media_type") or None,
                    ),
                    storage,
                    phase_timings,
                    cache_hit if offset == 0 else True,
                )
        return results

    def _parse_import(
        self,
        acquired: AcquiredImport,
        resolved: ResolvedDocument,
        options: LoadOptions,
        cancellation_token: CancellationToken | None,
    ) -> tuple[OntologyDocument, object | None, tuple[tuple[str, float], ...], bool]:
        media_type, parser_options, key = _parsed_document_context(
            acquired,
            resolved,
            options,
        )
        cached = self._document_cache.get(key)
        native_storage: object | None = None
        phase_timings: tuple[tuple[str, float], ...] = ()
        retain_native = options.backend is not BackendPreference.PYTHON and resolved.format in {
            None,
            DocumentFormat.FUNCTIONAL,
        }
        if retain_native:
            from pyowl_core.backends.parser import _parse_import_for_retained_load

            parsed = _parse_import_for_retained_load(
                acquired.data,
                format=resolved.format,
                document_iri=resolved.document_iri,
                options=parser_options,
                media_type=media_type,
                cancellation_token=cancellation_token,
            )
            if parsed.document is None or parsed.snapshot is not None:
                raise AssertionError("retained import parse did not publish a document")
            document = parsed.document
            native_storage = parsed.native_storage
            phase_timings = parsed.phase_timings
        else:
            if cached is not None:
                return cached, None, (), True
            from pyowl_core.backends.parser import parse_document

            document = parse_document(
                acquired.data,
                format=resolved.format,
                document_iri=resolved.document_iri,
                options=parser_options,
                media_type=media_type,
                cancellation_token=cancellation_token,
            )
        retained = self._document_cache.publish(key, document)
        return retained, native_storage, phase_timings, cached is not None


def load_snapshot(
    source: DocumentInput,
    *,
    document_iri: IRI | str | None = None,
    options: LoadOptions | None = None,
    resolver: ImportResolver | None = None,
    cancellation_token: CancellationToken | None = None,
) -> OntologySnapshot:
    """Build one closure; ``document_iri`` binds only an unparsed root source."""

    return SnapshotLoader().load(
        source,
        document_iri=document_iri,
        options=options,
        resolver=resolver,
        cancellation_token=cancellation_token,
    )


def clear_import_caches() -> None:
    """Clear process caches explicitly for tests and memory-sensitive applications."""

    _DEFAULT_ACQUISITION_CACHE.clear()
    _DEFAULT_DOCUMENT_CACHE.clear()


def _node(
    document: OntologyDocument,
    status: DocumentStatus,
    *,
    import_iri: IRI | None = None,
) -> _Node:
    return _Node(_document_key(document, import_iri=import_iri), document, status)


def _document_key(document: OntologyDocument, *, import_iri: IRI | None = None) -> str:
    identity = _identity_claim(document)
    if identity is None:
        # Acquisition bytes are diagnostic provenance, not document identity.
        # A root uses canonical structure; an imported anonymous document also
        # includes the semantic import IRI so equal documents reached at two
        # graph locations retain distinct document/blank-node scopes.
        payload = b"anonymous" + document.document_fingerprint.digest
        if import_iri is not None:
            payload += _frame(canonical_bytes(import_iri))
    else:
        payload = b"named" + b"".join(_frame(item.encode("utf-8")) for item in identity)
    digest = hashlib.sha256(b"pyowl-core:document-key:v1\x00" + payload).hexdigest()
    return f"d1:{digest}"


def _identity_claim(document: OntologyDocument) -> tuple[str, ...] | None:
    ontology = document.ontology_id.ontology_iri
    version = document.ontology_id.version_iri
    if ontology is None:
        return None
    if version is None:
        return ("ontology", ontology.value)
    return ("version", ontology.value, version.value)


def _source_identity(document: OntologyDocument) -> tuple[bytes, bytes]:
    return document.provenance.source_sha256, document.document_fingerprint.digest


def _register_identity(
    node: _Node,
    identities: dict[tuple[str, ...], _Node],
    versions: dict[str, _Node],
) -> None:
    claim = _identity_claim(node.document)
    if claim is None:
        return
    retained = identities.get(claim)
    if (
        retained is not None
        and retained.document.provenance.source_sha256 != node.document.provenance.source_sha256
    ):
        raise DocumentIdentityConflictError(
            "distinct byte sources claim the same ontology identity",
            code="DOCUMENT_IDENTITY_CONFLICT",
        )
    identities[claim] = retained or node
    version = node.document.ontology_id.version_iri
    if version is not None:
        retained_version = versions.get(version.value)
        if (
            retained_version is not None
            and retained_version.document.ontology_id != node.document.ontology_id
        ):
            raise DocumentIdentityConflictError(
                "one version IRI is claimed by different ontology identities",
                code="DOCUMENT_VERSION_CONFLICT",
            )
        versions[version.value] = retained_version or node


def _record(node: _Node) -> DocumentRecord:
    document = node.document
    return DocumentRecord(
        node.key,
        document.ontology_id,
        document.document_iri,
        document.provenance.source_sha256,
        document.document_fingerprint,
        document.provenance.format,
        node.status,
    )


def _initial_pending(
    node: _Node,
    options: LoadOptions,
    *,
    parent: _Pending | None = None,
) -> list[_Pending]:
    depth = 1 if parent is None else parent.depth + 1
    chain_prefix = () if parent is None else parent.chain
    values: list[_Pending] = []
    for iri in node.document.direct_imports:
        options.limits.enforce("max_import_depth", depth)
        values.append(
            _Pending(
                node.key,
                node.document.document_iri,
                iri,
                (*chain_prefix, iri),
                depth,
            )
        )
    return values


def _pending_key(item: _Pending) -> tuple[bytes, str, tuple[bytes, ...]]:
    return (
        canonical_bytes(item.import_iri),
        item.importing_document_key,
        tuple(canonical_bytes(iri) for iri in item.chain),
    )


def _resolution_mode(options: LoadOptions) -> ResolutionMode:
    if options.imports is ImportPolicy.RESOLVE_LOCAL:
        return ResolutionMode.LOCAL_ONLY
    if options.offline:
        return ResolutionMode.OFFLINE_CACHE
    return ResolutionMode.NETWORK


def _failed_edge(
    pending: _Pending,
    outcome: ResolverOutcome,
    policy: ImportPolicy,
) -> tuple[ImportEdge, Diagnostic]:
    del policy
    if outcome.kind is ResolutionKind.DENIED:
        status = ImportStatus.DENIED
    elif outcome.kind is ResolutionKind.NOT_FOUND:
        status = ImportStatus.UNRESOLVED
    else:
        status = ImportStatus.FAILED
    code = "UNRESOLVED_IMPORT" if outcome.error is None else outcome.error.code
    diagnostic = Diagnostic(
        code=code,
        severity=Severity.WARNING,
        message=f"import could not be resolved ({outcome.kind.value})",
        document_iri=pending.importing_document_iri,
        import_chain=pending.chain,
        details={
            "import_iri": _sanitize_iri(pending.import_iri),
            "resolver": outcome.resolver_name,
        },
    )
    return (
        ImportEdge(
            pending.importing_document_key,
            pending.import_iri,
            status,
            resolver_name=outcome.resolver_name,
            diagnostic=diagnostic,
        ),
        diagnostic,
    )


def _record_unresolved_without_resolver(
    document_key: str,
    document_iri: IRI | None,
    direct_imports: tuple[IRI, ...],
    options: LoadOptions,
) -> tuple[tuple[ImportEdge, ...], tuple[Diagnostic, ...], int]:
    """Build the exact single-document RECORD_UNRESOLVED result for no resolver."""

    if options.imports is not ImportPolicy.RECORD_UNRESOLVED:
        raise AssertionError("missing-resolver recording requires RECORD_UNRESOLVED")
    pending = [
        _Pending(document_key, document_iri, import_iri, (import_iri,), 1)
        for import_iri in direct_imports
    ]
    options.limits.enforce("max_resolver_attempts", len(pending))
    for _item in pending:
        options.limits.enforce("max_import_depth", 1)
    pending.sort(key=_pending_key)
    diagnostics: list[Diagnostic] = []
    edges: list[ImportEdge] = []
    for item in pending:
        edge, diagnostic = _failed_edge(
            item,
            ResolverOutcome.missing("none"),
            options.imports,
        )
        edges.append(edge)
        _append_diagnostic(diagnostics, diagnostic, options)
    return tuple(edges), tuple(diagnostics), len(pending)


def _raise_resolution(pending: _Pending, outcome: ResolverOutcome) -> None:
    if outcome.error is not None:
        raise outcome.error
    raise UnresolvedImportError(
        f"unresolved import {_sanitize_iri(pending.import_iri)}",
        code="UNRESOLVED_IMPORT",
    )


def _outcome_from_error(name: str, error: ImportResolutionError | ParseError) -> ResolverOutcome:
    if isinstance(error, AccessDeniedError):
        kind = ResolutionKind.DENIED
    elif isinstance(error, IntegrityError):
        kind = ResolutionKind.INTEGRITY
    elif isinstance(error, ParseError):
        kind = ResolutionKind.MALFORMED
    else:
        kind = ResolutionKind.FAILED
    wrapped = (
        error
        if isinstance(error, ImportResolutionError)
        else ImportResolutionError("resolved import document is malformed", code=error.code)
    )
    if wrapped is not error:
        wrapped.__cause__ = error
    return ResolverOutcome(kind, name, error=wrapped)


def _append_diagnostic(
    diagnostics: list[Diagnostic], diagnostic: Diagnostic, options: LoadOptions
) -> None:
    maximum = options.limits.max_diagnostics
    if len(diagnostics) < maximum:
        diagnostics.append(diagnostic)
        return
    suppressed = 1
    if diagnostics and diagnostics[-1].code == "DIAGNOSTICS_SUPPRESSED":
        previous = diagnostics[-1].details.get("count", 0)
        suppressed += previous if isinstance(previous, int) else 0
    else:
        # Replacing the last retained item suppresses it in addition to this item.
        suppressed += 1
    diagnostics[-1] = Diagnostic(
        code="DIAGNOSTICS_SUPPRESSED",
        severity=Severity.WARNING,
        message="additional import diagnostics were suppressed",
        details={"count": suppressed},
    )


def _enforce_closure_limits(
    options: LoadOptions,
    document_count: int,
    counters: Mapping[str, int],
) -> None:
    options.limits.enforce("max_documents", document_count)
    options.limits.enforce("max_total_source_bytes", counters["total_source_bytes"])
    options.limits.enforce("max_axioms", counters["axioms"])
    options.limits.enforce("max_terms", counters["terms"])


def _document_terms(document: OntologyDocument) -> int:
    return sum(
        1
        for root in (
            *document.ontology_annotations,
            *document.axioms,
            *document.extension_components,
        )
        for _ in walk(root)
    )


def _check_operation(
    token: CancellationToken | None,
    started: float,
    options: LoadOptions,
) -> None:
    if token is not None:
        token.check()
    deadline = options.limits.deadline_seconds
    elapsed = time.monotonic() - started
    if deadline is not None and elapsed >= deadline:
        raise ResourceLimitError(
            "resource limit deadline_seconds exceeded",
            limit="deadline_seconds",
            observed=elapsed,
            allowed=deadline,
        )


def _parsed_document_context(
    acquired: AcquiredImport,
    resolved: ResolvedDocument,
    options: LoadOptions,
) -> tuple[str | None, LoadOptions, tuple[object, ...]]:
    media_type = resolved.provenance.get("media_type") or None
    parser_options = replace(options, format=None)
    return (
        media_type,
        parser_options,
        (
            acquired.source_sha256,
            None if resolved.format is None else resolved.format.value,
            resolved.document_iri.value,
            media_type,
            parser_options.backend.value,
            parser_options.preserve_source_map,
            parser_options.collect_provenance,
            parser_options.limits,
        ),
    )


def _parsed_document_key(
    acquired: AcquiredImport,
    resolved: ResolvedDocument,
    options: LoadOptions,
) -> tuple[object, ...]:
    return _parsed_document_context(acquired, resolved, options)[2]


def _with_resolved_provenance(
    document: OntologyDocument,
    acquired: AcquiredImport,
    resolved: ResolvedDocument,
    media_type: str | None,
) -> OntologyDocument:
    provenance = replace(
        document.provenance,
        expected_sha256=resolved.expected_sha256,
        acquisition_locator=acquired.locator,
        media_type=media_type,
    )
    return document if provenance == document.provenance else replace(
        document,
        provenance=provenance,
    )


def _merge_phase_timings(
    left: tuple[tuple[str, float], ...],
    right: tuple[tuple[str, float], ...],
) -> tuple[tuple[str, float], ...]:
    totals = dict(left)
    for name, seconds in right:
        totals[name] = totals.get(name, 0.0) + seconds
    return tuple(sorted(totals.items(), key=lambda item: item[0].encode("utf-8")))


def _check_expected(actual: bytes, expected: bytes | None) -> None:
    if expected is not None and actual != expected:
        raise IntegrityError("resolved import digest mismatch", code="IMPORT_DIGEST_MISMATCH")


def _acquisition_key(source: object) -> tuple[object, ...] | None:
    if isinstance(source, bytes):
        return ("bytes", hashlib.sha256(source).digest())
    if isinstance(source, (str, os.PathLike)):
        path = os.path.abspath(os.fspath(source))
        metadata = os.stat(path, follow_symlinks=False)
        return (
            "path",
            path,
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        )
    return None


def _resolved_locator(resolved: ResolvedDocument) -> str | None:
    for key in ("final_locator", "locator"):
        value = resolved.provenance.get(key)
        if value:
            return value
    if isinstance(resolved.source, (str, os.PathLike)):
        return os.path.basename(os.fspath(resolved.source))
    return None


def _sanitize_iri(value: IRI) -> str:
    split = urlsplit(value.value)
    if split.scheme in {"http", "https"}:
        host = split.hostname or ""
        port = "" if split.port is None else f":{split.port}"
        result = urlunsplit((split.scheme, host + port, split.path, "", ""))
    else:
        result = value.value
    if len(result) > 512:
        return result[:509] + "..."
    return result


def _record_bytes(record: DocumentRecord) -> bytes:
    ontology = record.ontology_id
    return b"".join(
        (
            _frame(record.document_key.encode("ascii")),
            _optional_iri(ontology.ontology_iri),
            _optional_iri(ontology.version_iri),
            record.document_fingerprint.digest,
            _frame(record.status.value.encode("ascii")),
        )
    )


def _edge_bytes(edge: ImportEdge) -> bytes:
    return b"".join(
        (
            _frame(edge.importing_document_key.encode("ascii")),
            _frame(canonical_bytes(edge.import_iri)),
            _frame(edge.status.value.encode("ascii")),
            _optional_text(edge.resolved_document_key),
            _optional_text(edge.resolver_name),
            _optional_text(None if edge.diagnostic is None else edge.diagnostic.code),
        )
    )


def _optional_iri(value: IRI | None) -> bytes:
    return b"0" if value is None else b"1" + _frame(canonical_bytes(value))


def _optional_text(value: str | None) -> bytes:
    return b"0" if value is None else b"1" + _frame(value.encode("utf-8"))


def _frame(value: bytes) -> bytes:
    return encode_varint(len(value)) + value


__all__ = [
    "AcquiredImport",
    "AcquisitionCache",
    "DocumentInput",
    "DocumentRecord",
    "DocumentStatus",
    "ImportEdge",
    "ImportManifest",
    "ImportStatus",
    "ParsedDocumentCache",
    "SnapshotLoader",
    "clear_import_caches",
    "load_snapshot",
]
