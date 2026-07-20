"""Private lazy document/snapshot facades over the exact native V2 handoff.

This module deliberately does not alter the public dataclass constructors.  A
native publication has already frozen and attested the work performed by those
constructors, so calling either public ``__post_init__`` here would rebuild the
ontology-sized Python graph that the retained arena is intended to avoid.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping
from collections.abc import Set as AbstractSet
from contextlib import suppress
from dataclasses import dataclass, fields, is_dataclass
from itertools import zip_longest
from types import TracebackType
from typing import Any, Generic, TypeVar, cast

from pyowl_core._immutable import FrozenMap, freeze_mapping
from pyowl_core.backends.native_handoff import (
    NativeDiagnosticPublicationV1,
    NativeDocumentProvenancePublicationV1,
    NativeImportManifestPublicationV1,
)
from pyowl_core.backends.native_handoff_v2 import (
    NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2,
    NativeDiagnosticReferenceKindsV2,
    NativeDiagnosticReferenceKindV2,
    NativeDocumentHandleV2,
    NativeFacadeCollectionV2,
    NativeFacadePageRequestV2,
    NativeFacadeScopeV2,
    NativeOriginRowV2,
    NativeOWL2DLIssueRowV2,
    NativeOWL2DLReportSummaryV2,
    NativeOWL2DLRoleEdgeRowV2,
    NativeOWL2DLStructuralIssueRowV2,
    NativePythonFacadeCountersV2,
    NativeRDFDiagnosticRowV2,
    NativeRDFReportHeaderRowV2,
    NativeRDFRuleRowV2,
    NativeRDFTripleRowV2,
    NativeSignatureKindV2,
    NativeSnapshotHandleV2,
    NativeSnapshotPublicationV2,
    NativeSourceMapRowV2,
    NativeSourcePrefixRowV2,
    _unchecked_contains_request_v2,
    require_native_facade_publication_v2,
)
from pyowl_core.cancellation import CancellationToken
from pyowl_core.config import DocumentFormat, ImportPolicy, LoadOptions
from pyowl_core.diagnostics import Diagnostic, Severity, SourceSpan
from pyowl_core.exceptions import (
    BackendProtocolError,
    ClosedSnapshotError,
    SnapshotInUseError,
)
from pyowl_core.model import (
    IRI,
    Annotation,
    AnonymousIndividual,
    CanonicalSet,
    Entity,
    EntityKind,
    ObjectPropertyExpression,
    OWL2DLReport,
    RoleAnalysis,
    RoleEdge,
    StructuralNode,
    StructuralReport,
    ValidationIssue,
    canonical_bytes,
    encode_varint,
    walk,
)
from pyowl_core.model.axioms import AxiomNode

from .document import Fingerprint, OntologyDocument, OntologyID
from .identity import _identity_metadata_from_manifest, _OntologyIdentityMetadata
from .imports import (
    DocumentRecord,
    DocumentStatus,
    ImportEdge,
    ImportManifest,
    ImportStatus,
)
from .provenance import (
    DetectionBasis,
    DigestKind,
    DocumentProvenance,
    OriginIndex,
    OriginOccurrence,
    RDFMappingReport,
    RDFTripleEvidence,
    SourceMap,
    SourceOccurrence,
)
from .snapshot import AxiomScope, CoreCapabilities, LoadReport, OntologySnapshot

T = TypeVar("T", bound=StructuralNode)
A = TypeVar("A", bound=AxiomNode)
V = TypeVar("V")
S = TypeVar("S")

_STRUCTURAL_COLLECTIONS = frozenset(
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
_MISSING = object()
_REPLACE_ERROR = "native ontology facades cannot be replaced; materialize them first"
_EMPTY_CACHE_BYTES = sys.getsizeof(OrderedDict())
_WIRE_STRUCTURAL_ALIAS_SEAL_V1 = object()
_NO_ANONYMOUS_SCOPES_SEAL_V2 = object()


@dataclass(frozen=True, slots=True)
class _NativeIngestionCountersV2:
    """Private evidence for the bounded retained-parser publication seam."""

    parser_result_bytes_scanned: int = 0
    canonical_rows_scanned: int = 0
    structural_occurrence_rows_scanned: int = 0
    structural_root_rows_published: int = 0
    eager_structural_objects_materialized: int = 0
    metadata_iri_objects_materialized: int = 0
    provenance_occurrence_records_materialized: int = 0

    def __post_init__(self) -> None:
        for name in (
            "parser_result_bytes_scanned",
            "canonical_rows_scanned",
            "structural_occurrence_rows_scanned",
            "structural_root_rows_published",
            "eager_structural_objects_materialized",
            "metadata_iri_objects_materialized",
            "provenance_occurrence_records_materialized",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")


def _deep_size(value: object, seen: set[int] | None = None) -> int:
    """Return a conservative recursive charge for an immutable cached value."""

    visited = set() if seen is None else seen
    identity = id(value)
    if identity in visited:
        return 0
    visited.add(identity)
    total = sys.getsizeof(value)
    if isinstance(value, Mapping):
        return total + sum(
            _deep_size(key, visited) + _deep_size(item, visited) for key, item in value.items()
        )
    if isinstance(value, (tuple, list, set, frozenset, CanonicalSet)):
        return total + sum(_deep_size(item, visited) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        return total + sum(_deep_size(getattr(value, item.name), visited) for item in fields(value))
    return total


class _NativeSharedState:
    """Process-local facade cache and instrumentation shared by sibling owners."""

    __slots__ = (
        "_cache",
        "_cache_bytes",
        "_cache_entry_bytes",
        "_cache_peak_bytes",
        "_counters",
        "_lock",
        "_max_cache_bytes",
        "_max_cache_entries",
        "_page_bytes",
        "_pid",
        "max_row_bytes",
    )

    def __init__(self, publication: NativeSnapshotPublicationV2) -> None:
        limits = publication.load_options.limits
        page_candidates = [
            NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2["max_facade_page_bytes"],
            limits.max_temporary_bytes,
            limits.max_canonical_work,
            limits.max_index_bytes,
            limits.max_wire_bytes,
        ]
        if limits.max_memory_bytes is not None:
            page_candidates.append(limits.max_memory_bytes)
        self._page_bytes = max(1, min(page_candidates))
        self.max_row_bytes = publication.max_facade_row_bytes
        self._max_cache_entries = NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2["max_facade_cache_entries"]
        cache_candidates = [
            NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2["max_facade_cache_bytes"],
            limits.max_temporary_bytes,
        ]
        if limits.max_memory_bytes is not None:
            cache_candidates.append(limits.max_memory_bytes)
        self._max_cache_bytes = max(1, min(cache_candidates))
        self._cache: OrderedDict[tuple[str, bytes], tuple[object, int]] | None = None
        self._cache_bytes = 0
        self._cache_entry_bytes = 0
        self._cache_peak_bytes = 0
        self._counters = {item.name: 0 for item in fields(NativePythonFacadeCountersV2)}
        self._lock = threading.RLock()
        self._pid = os.getpid()

    @property
    def page_bytes(self) -> int:
        self._after_fork()
        return self._page_bytes

    def publication_object(self, count: int = 1) -> None:
        self._after_fork()
        with self._lock:
            self._counters["publication_objects"] += count

    def consume(
        self,
        collection: NativeFacadeCollectionV2,
        encoded: bytes,
        decoded: object,
    ) -> object:
        """Canonicalize one already validation-decoded page value through the LRU."""

        self._after_fork()
        category = "model" if collection in _STRUCTURAL_COLLECTIONS else collection.value
        key = (category, encoded)
        with self._lock:
            cache = self._cache
            retained = None if cache is None else cache.get(key)
            if retained is not None:
                assert cache is not None
                cache.move_to_end(key)
                self._counters["cache_hits"] += 1
                return retained[0]
            self._counters["cache_misses"] += 1
            counter = (
                "model_rows_materialized"
                if collection in _STRUCTURAL_COLLECTIONS
                else "auxiliary_rows_decoded"
            )
            self._counters[counter] += 1
            value = (decoded, 0)
            visited: set[int] = set()
            charge = _deep_size(key, visited) + _deep_size(value, visited)
            # The stored charge replaces the small integer zero above.  Both
            # are one-digit Python integers because a cache entry can never
            # reach the fixed 8 MiB publication bound, so their retained sizes
            # are equal.  OrderedDict's table and linked-list nodes are charged
            # separately after insertion via sys.getsizeof().
            if charge + _EMPTY_CACHE_BYTES > self._max_cache_bytes:
                return decoded
            if cache is None:
                cache = OrderedDict()
                self._cache = cache
            cache[key] = (decoded, charge)
            self._cache_entry_bytes += charge
            evicted = False
            while cache and (
                len(cache) > self._max_cache_entries
                or self._retained_cache_bytes() > self._max_cache_bytes
            ):
                _old_key, (_old_value, old_charge) = cache.popitem(last=False)
                self._cache_entry_bytes -= old_charge
                self._counters["cache_evictions"] += 1
                evicted = True
            if evicted and cache:
                # CPython dictionaries may retain a grown table after pops.
                # Rebuilding at the fixed 256-entry bound makes the memory
                # gauge describe the actually retained container, not an
                # optimistic per-entry estimate.
                cache = OrderedDict(cache.items())
                self._cache = cache
                while cache and self._retained_cache_bytes() > self._max_cache_bytes:
                    _old_key, (_old_value, old_charge) = cache.popitem(last=False)
                    self._cache_entry_bytes -= old_charge
                    self._counters["cache_evictions"] += 1
            if not cache:
                # Drop a grown empty table instead of retaining uncharged
                # capacity.  The next cacheable value recreates it lazily.
                self._cache = None
            self._cache_bytes = self._retained_cache_bytes()
            self._cache_peak_bytes = max(self._cache_peak_bytes, self._cache_bytes)
            self._counters["cache_current_entries"] = 0 if self._cache is None else len(self._cache)
            self._counters["cache_current_bytes"] = self._cache_bytes
            self._counters["cache_peak_bytes"] = self._cache_peak_bytes
            return decoded

    def _retained_cache_bytes(self) -> int:
        if self._cache is None:
            return 0
        return sys.getsizeof(self._cache) + self._cache_entry_bytes

    def counters(self) -> NativePythonFacadeCountersV2:
        self._after_fork()
        with self._lock:
            values = dict(self._counters)
            values["cache_current_entries"] = 0 if self._cache is None else len(self._cache)
            values["cache_current_bytes"] = self._cache_bytes
            values["cache_peak_bytes"] = self._cache_peak_bytes
            return NativePythonFacadeCountersV2(**values)

    def _after_fork(self) -> None:
        current = os.getpid()
        if current == self._pid:
            return
        self._pid = current
        self._lock = threading.RLock()
        self._cache = None
        self._cache_bytes = 0
        self._cache_entry_bytes = 0
        self._cache_peak_bytes = 0
        self._counters["cache_hits"] = 0
        self._counters["cache_misses"] = 0
        self._counters["cache_evictions"] = 0
        self._counters["cache_current_entries"] = 0
        self._counters["cache_current_bytes"] = 0
        self._counters["cache_peak_bytes"] = 0


@dataclass(frozen=True, slots=True)
class _NativeOwnerState:
    handle: NativeSnapshotHandleV2 | NativeDocumentHandleV2
    shared: _NativeSharedState

    def ensure_open(self) -> None:
        if self.handle.closed:
            raise ClosedSnapshotError("native ontology storage is closed")

    @property
    def closed(self) -> bool:
        return self.handle.closed


@dataclass(frozen=True, slots=True)
class _NativeCollectionRef(Generic[T]):
    owner: _NativeOwnerState
    collection: NativeFacadeCollectionV2
    scope: NativeFacadeScopeV2
    document_ordinal: int | None
    count: int | None
    signature_kind: NativeSignatureKindV2 = NativeSignatureKindV2.ALL
    include_builtins: bool = True

    def __post_init__(self) -> None:
        self.owner.shared.publication_object()

    def iter_pairs(self, *, digest_filter: bytes | None = None) -> Iterator[tuple[bytes, object]]:
        self.owner.ensure_open()
        if self.count == 0:
            return
        cursor = 0
        observed_total: int | None = None
        while True:
            request = NativeFacadePageRequestV2(
                collection=self.collection,
                scope=self.scope,
                document_ordinal=self.document_ordinal,
                start=cursor,
                max_rows=NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2["max_facade_page_rows"],
                max_bytes=self.owner.shared.page_bytes,
                max_row_bytes=self.owner.shared.max_row_bytes,
                signature_kind=self.signature_kind,
                include_builtins=self.include_builtins,
                digest_filter=digest_filter,
            )
            page = self.owner.handle._facade_page_v2(request)
            if observed_total is None:
                observed_total = page.total_count
            elif observed_total != page.total_count:
                raise BackendProtocolError(
                    "native page total changed during traversal",
                    code="NATIVE_PAGE_TOTAL",
                )
            if self.count is not None and digest_filter is None and page.total_count != self.count:
                raise BackendProtocolError(
                    "native page total diverges from facade cardinality",
                    code="NATIVE_PAGE_TOTAL",
                )
            decoded_rows = page._validated_rows_v2()
            if len(decoded_rows) != len(page.rows):
                raise BackendProtocolError(
                    "native page validation rows are not aligned",
                    code="NATIVE_PAGE_ROWS",
                )
            for encoded, decoded in zip(page.rows, decoded_rows, strict=True):
                # A generator suspension is a public-operation boundary.  Do
                # not let values buffered by an earlier page call escape after
                # the independently owned document/snapshot handle closes.
                self.owner.ensure_open()
                yield encoded, self.owner.shared.consume(self.collection, encoded, decoded)
            if page.terminal:
                return
            if page.next_cursor is None:
                raise BackendProtocolError(
                    "native nonterminal page omitted its cursor",
                    code="NATIVE_PAGE_CURSOR",
                )
            cursor = page.next_cursor

    def iter_encoded(self) -> Iterator[bytes]:
        """Traverse validated canonical rows without retaining their Python values."""

        self.owner.ensure_open()
        if self.count == 0:
            return
        cursor = 0
        observed_total: int | None = None
        while True:
            page = self.owner.handle._facade_page_v2(
                NativeFacadePageRequestV2(
                    collection=self.collection,
                    scope=self.scope,
                    document_ordinal=self.document_ordinal,
                    start=cursor,
                    max_rows=NATIVE_SNAPSHOT_PUBLICATION_BOUNDS_V2["max_facade_page_rows"],
                    max_bytes=self.owner.shared.page_bytes,
                    max_row_bytes=self.owner.shared.max_row_bytes,
                    signature_kind=self.signature_kind,
                    include_builtins=self.include_builtins,
                )
            )
            if observed_total is None:
                observed_total = page.total_count
            elif observed_total != page.total_count:
                raise BackendProtocolError(
                    "native page total changed during traversal",
                    code="NATIVE_PAGE_TOTAL",
                )
            if self.count is not None and page.total_count != self.count:
                raise BackendProtocolError(
                    "native page total diverges from facade cardinality",
                    code="NATIVE_PAGE_TOTAL",
                )
            # The owner has already validation-decoded this page.  Retrieve the
            # fixed tuple exactly once even when the caller only needs bytes;
            # this preserves the V2 page-consumption invariant without adding
            # those values to the facade materialization/cache counters.
            decoded_rows = page._validated_rows_v2()
            if len(decoded_rows) != len(page.rows):
                raise BackendProtocolError(
                    "native page validation rows are not aligned",
                    code="NATIVE_PAGE_ROWS",
                )
            for encoded in page.rows:
                self.owner.ensure_open()
                yield encoded
            if page.terminal:
                return
            if page.next_cursor is None:
                raise BackendProtocolError(
                    "native nonterminal page omitted its cursor",
                    code="NATIVE_PAGE_CURSOR",
                )
            cursor = page.next_cursor

    def row_at(self, index: int) -> tuple[bytes, object]:
        self.owner.ensure_open()
        if self.count is None or not 0 <= index < self.count:
            raise IndexError(index)
        page = self.owner.handle._facade_page_v2(
            NativeFacadePageRequestV2(
                collection=self.collection,
                scope=self.scope,
                document_ordinal=self.document_ordinal,
                start=index,
                max_rows=1,
                max_bytes=self.owner.shared.page_bytes,
                max_row_bytes=self.owner.shared.max_row_bytes,
                signature_kind=self.signature_kind,
                include_builtins=self.include_builtins,
            )
        )
        if len(page.rows) != 1:
            raise BackendProtocolError(
                "native ordinal lookup did not return one row",
                code="NATIVE_PAGE_ROWS",
            )
        if page.total_count != self.count:
            raise BackendProtocolError(
                "native page total diverges from facade cardinality",
                code="NATIVE_PAGE_TOTAL",
            )
        decoded_rows = page._validated_rows_v2()
        if len(decoded_rows) != 1:
            raise BackendProtocolError(
                "native ordinal validation did not return one row",
                code="NATIVE_PAGE_ROWS",
            )
        decoded = decoded_rows[0]
        return page.rows[0], self.owner.shared.consume(self.collection, page.rows[0], decoded)

    def contains_axiom(self, value: AxiomNode) -> bool:
        self.owner.ensure_open()
        encoded = canonical_bytes(value)
        if len(encoded) > self.owner.shared.max_row_bytes:
            return False
        if self.count == 0:
            return False
        return self.owner.handle._facade_contains_v2(
            _unchecked_contains_request_v2(
                collection=NativeFacadeCollectionV2.AXIOMS,
                scope=self.scope,
                document_ordinal=self.document_ordinal,
                canonical=encoded,
                max_row_bytes=self.owner.shared.max_row_bytes,
            )
        )


class _NativeCanonicalSet(CanonicalSet[T]):
    """CanonicalSet-compatible paged collection without an eager ``_items`` tuple."""

    __slots__ = ("_ref",)

    def __init__(self, ref: _NativeCollectionRef[T]) -> None:
        self._ref = ref
        self._cached_hash = None
        ref.owner.shared.publication_object()

    @classmethod
    def _from_iterable(cls, values: Iterable[S]) -> AbstractSet[S]:
        """Keep ABC set algebra from invoking the private facade constructor."""

        return cast(
            AbstractSet[S],
            CanonicalSet(cast(Iterable[StructuralNode], values)),
        )

    def __contains__(self, value: object) -> bool:
        self._ref.owner.ensure_open()
        if not isinstance(value, StructuralNode):
            return False
        if self._ref.collection is NativeFacadeCollectionV2.AXIOMS:
            if not isinstance(value, AxiomNode):
                return False
            return self._ref.contains_axiom(value)
        if self._ref.count is None:
            target = canonical_bytes(value)
            return any(encoded == target for encoded in self._ref.iter_encoded())
        target = canonical_bytes(value)
        lower = 0
        upper = self._ref.count
        while lower < upper:
            middle = (lower + upper) // 2
            candidate, _decoded = self._ref.row_at(middle)
            if candidate < target:
                lower = middle + 1
            else:
                upper = middle
        if lower >= self._ref.count:
            return False
        candidate, _decoded = self._ref.row_at(lower)
        return candidate == target

    def __eq__(self, other: object) -> bool:
        self._ref.owner.ensure_open()
        if not isinstance(other, CanonicalSet):
            return False
        if len(self) != len(other):
            return False
        left = self._ref.iter_encoded()
        right = (
            other._ref.iter_encoded()
            if isinstance(other, _NativeCanonicalSet)
            else (canonical_bytes(item) for item in other)
        )
        return all(
            first == second for first, second in zip_longest(left, right, fillvalue=_MISSING)
        )

    def __iter__(self) -> Iterator[T]:
        for _encoded, decoded in self._ref.iter_pairs():
            yield cast(T, decoded)

    def __len__(self) -> int:
        self._ref.owner.ensure_open()
        if self._ref.count is None:
            raise TypeError("dynamic native collections do not expose zero-page length")
        return self._ref.count

    def __hash__(self) -> int:
        self._ref.owner.ensure_open()
        cached = self._cached_hash
        if cached is None:
            hasher = hashlib.sha256(b"pyowl-core:canonical-set:v1\x00")
            for encoded in self._ref.iter_encoded():
                hasher.update(len(encoded).to_bytes(8, "big"))
                hasher.update(encoded)
            value = int.from_bytes(hasher.digest()[:8], "big", signed=True)
            cached = -2 if value == -1 else value
            self._cached_hash = cached
        return cached

    def __repr__(self) -> str:
        state = "closed" if self._ref.owner.closed else "open"
        return (
            "CanonicalSet(<native "
            f"collection={self._ref.collection.value!r}, count={self._ref.count!r}, "
            f"state={state!r}>)"
        )

    def as_tuple(self) -> tuple[T, ...]:
        return tuple(self)

    def __copy__(self) -> _NativeCanonicalSet[T]:
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> _NativeCanonicalSet[T]:
        return self

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("native ontology collections cannot be pickled")


class _NativeOriginMapping(Mapping[bytes, tuple[OriginOccurrence, ...]]):
    __slots__ = ("_ref",)

    def __init__(self, ref: _NativeCollectionRef[StructuralNode]) -> None:
        self._ref = ref
        ref.owner.shared.publication_object()

    @staticmethod
    def _occurrence(value: NativeOriginRowV2) -> OriginOccurrence:
        return OriginOccurrence(value.document_key, value.occurrence, value.span)

    def __getitem__(self, key: bytes) -> tuple[OriginOccurrence, ...]:
        if not isinstance(key, bytes) or len(key) != 32:
            raise KeyError(key)
        rows = tuple(
            self._occurrence(cast(NativeOriginRowV2, decoded))
            for _encoded, decoded in self._ref.iter_pairs(digest_filter=key)
        )
        if not rows:
            raise KeyError(key)
        return rows

    def __iter__(self) -> Iterator[bytes]:
        previous: bytes | None = None
        for _encoded, decoded in self._ref.iter_pairs():
            digest = cast(NativeOriginRowV2, decoded).digest
            if digest != previous:
                yield digest
                previous = digest

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __hash__(self) -> int:
        return _frozen_map_hash((key, self[key]) for key in self)

    def __eq__(self, other: object) -> bool:
        self._ref.owner.ensure_open()
        return _mapping_equals(self, other)

    def __repr__(self) -> str:
        return f"<native origin mapping rows={self._ref.count!r}>"

    def __copy__(self) -> _NativeOriginMapping:
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> _NativeOriginMapping:
        return self

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("native origin mappings cannot be pickled")


class _NativeSourceEntryMapping(Mapping[bytes, tuple[SourceOccurrence, ...]]):
    __slots__ = ("_ref",)

    def __init__(self, ref: _NativeCollectionRef[StructuralNode]) -> None:
        self._ref = ref
        ref.owner.shared.publication_object()

    @staticmethod
    def _occurrence(value: NativeSourceMapRowV2) -> SourceOccurrence:
        return SourceOccurrence(value.occurrence, value.span, FrozenMap(value.lexical))

    def __getitem__(self, key: bytes) -> tuple[SourceOccurrence, ...]:
        if not isinstance(key, bytes) or len(key) != 32:
            raise KeyError(key)
        rows = tuple(
            self._occurrence(cast(NativeSourceMapRowV2, decoded))
            for _encoded, decoded in self._ref.iter_pairs(digest_filter=key)
        )
        if not rows:
            raise KeyError(key)
        return rows

    def __iter__(self) -> Iterator[bytes]:
        previous: bytes | None = None
        for _encoded, decoded in self._ref.iter_pairs():
            digest = cast(NativeSourceMapRowV2, decoded).digest
            if digest != previous:
                yield digest
                previous = digest

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __hash__(self) -> int:
        return _frozen_map_hash((key, self[key]) for key in self)

    def __eq__(self, other: object) -> bool:
        self._ref.owner.ensure_open()
        return _mapping_equals(self, other)

    def __repr__(self) -> str:
        return f"<native source mapping rows={self._ref.count!r}>"

    def __copy__(self) -> _NativeSourceEntryMapping:
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> _NativeSourceEntryMapping:
        return self

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("native source mappings cannot be pickled")


class _NativePrefixMapping(Mapping[str, str]):
    __slots__ = ("_ref",)

    def __init__(self, ref: _NativeCollectionRef[StructuralNode]) -> None:
        self._ref = ref
        ref.owner.shared.publication_object()

    def __getitem__(self, key: str) -> str:
        if not isinstance(key, str):
            raise KeyError(key)
        lower = 0
        upper = cast(int, self._ref.count)
        target = key.encode("utf-8")
        while lower < upper:
            middle = (lower + upper) // 2
            _encoded, decoded = self._ref.row_at(middle)
            row = cast(NativeSourcePrefixRowV2, decoded)
            candidate = row.prefix.encode("utf-8")
            if candidate < target:
                lower = middle + 1
            else:
                upper = middle
        if lower >= cast(int, self._ref.count):
            raise KeyError(key)
        _encoded, decoded = self._ref.row_at(lower)
        row = cast(NativeSourcePrefixRowV2, decoded)
        if row.prefix != key:
            raise KeyError(key)
        return row.iri

    def __iter__(self) -> Iterator[str]:
        for _encoded, decoded in self._ref.iter_pairs():
            yield cast(NativeSourcePrefixRowV2, decoded).prefix

    def __len__(self) -> int:
        self._ref.owner.ensure_open()
        return cast(int, self._ref.count)

    def __hash__(self) -> int:
        return _frozen_map_hash((key, self[key]) for key in self)

    def __eq__(self, other: object) -> bool:
        self._ref.owner.ensure_open()
        return _mapping_equals(self, other)

    def __repr__(self) -> str:
        return f"<native prefix mapping rows={self._ref.count!r}>"

    def __copy__(self) -> _NativePrefixMapping:
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> _NativePrefixMapping:
        return self

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("native prefix mappings cannot be pickled")


def _mapping_equals(left: Mapping[Any, Any], right: object) -> bool:
    if not isinstance(right, Mapping) or len(left) != len(right):
        return False
    return all(key in right and right[key] == value for key, value in left.items())


def _frozen_map_hash(items: Iterator[tuple[object, object]]) -> int:
    """Match ``FrozenMap`` hashing without eagerly retaining facade mappings."""

    return hash(
        tuple(
            sorted(
                items,
                key=lambda item: (type(item[0]).__qualname__, repr(item[0])),
            )
        )
    )


class _NativeOriginIndex(OriginIndex):
    __slots__ = ()

    def __init__(self, *_args: object, **_changes: object) -> None:
        raise TypeError(_REPLACE_ERROR)

    def __eq__(self, other: object) -> bool:
        cast(_NativeOriginMapping, self.entries)._ref.owner.ensure_open()
        if not isinstance(other, OriginIndex):
            return NotImplemented
        return self.entries == other.entries

    def __hash__(self) -> int:
        cast(_NativeOriginMapping, self.entries)._ref.owner.ensure_open()
        return hash((self.entries,))

    def __copy__(self) -> _NativeOriginIndex:
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> _NativeOriginIndex:
        return self

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("native origin indexes cannot be pickled")


class _NativeSourceMap(SourceMap):
    __slots__ = ()

    def __init__(self, *_args: object, **_changes: object) -> None:
        raise TypeError(_REPLACE_ERROR)

    def __eq__(self, other: object) -> bool:
        cast(_NativeSourceEntryMapping, self.entries)._ref.owner.ensure_open()
        if not isinstance(other, SourceMap):
            return NotImplemented
        return self.entries == other.entries and self.prefixes == other.prefixes

    def __hash__(self) -> int:
        cast(_NativeSourceEntryMapping, self.entries)._ref.owner.ensure_open()
        return hash((self.entries, self.prefixes))

    def __copy__(self) -> _NativeSourceMap:
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> _NativeSourceMap:
        return self

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("native source maps cannot be pickled")


def _lazy_origin_index(ref: _NativeCollectionRef[StructuralNode]) -> OriginIndex:
    value = object.__new__(_NativeOriginIndex)
    object.__setattr__(value, "entries", _NativeOriginMapping(ref))
    return value


def _lazy_source_map(
    entries: _NativeCollectionRef[StructuralNode],
    prefixes: _NativeCollectionRef[StructuralNode],
) -> SourceMap:
    value = object.__new__(_NativeSourceMap)
    object.__setattr__(value, "entries", _NativeSourceEntryMapping(entries))
    object.__setattr__(value, "prefixes", _NativePrefixMapping(prefixes))
    return value


class _NativeRDFReportState:
    __slots__ = (
        "_header",
        "_lock",
        "_pid",
        "conformant",
        "diagnostics",
        "header",
        "owner",
        "rules",
        "triples",
    )

    def __init__(
        self,
        *,
        owner: _NativeOwnerState,
        conformant: bool,
        header: _NativeCollectionRef[StructuralNode],
        triples: _NativeCollectionRef[StructuralNode],
        rules: _NativeCollectionRef[StructuralNode],
        diagnostics: _NativeCollectionRef[StructuralNode],
    ) -> None:
        self.owner = owner
        self.conformant = conformant
        self.header = header
        self.triples = triples
        self.rules = rules
        self.diagnostics = diagnostics
        self._header: NativeRDFReportHeaderRowV2 | None = None
        self._lock = threading.RLock()
        self._pid = os.getpid()
        owner.shared.publication_object()

    def header_row(self) -> NativeRDFReportHeaderRowV2:
        self.owner.ensure_open()
        self._after_fork()
        with self._lock:
            self.owner.ensure_open()
            if self._header is None:
                _encoded, decoded = self.header.row_at(0)
                if type(decoded) is not NativeRDFReportHeaderRowV2:
                    raise BackendProtocolError(
                        "native RDF report header has the wrong row type",
                        code="NATIVE_RDF_REPORT",
                    )
                self._header = decoded
            return self._header

    def _after_fork(self) -> None:
        current = os.getpid()
        if current == self._pid:
            return
        self._pid = current
        self._lock = threading.RLock()
        self._header = None


class _NativeRDFMappingReport(RDFMappingReport):
    _native_rdf_state: _NativeRDFReportState

    __slots__ = ("_native_rdf_state",)

    def __init__(
        self,
        state: _NativeRDFReportState | None = None,
        **_changes: object,
    ) -> None:
        if state is None or _changes:
            raise TypeError(_REPLACE_ERROR)
        object.__setattr__(self, "_native_rdf_state", state)
        state.owner.shared.publication_object()

    @property
    def conformant(self) -> bool:
        return self._native_rdf_state.conformant

    @property
    def consumed_triples(self) -> int:
        return self._native_rdf_state.header_row().consumed_triples

    @property
    def total_triples(self) -> int:
        return self._native_rdf_state.header_row().total_triples

    @property
    def unconsumed(self) -> tuple[RDFTripleEvidence, ...]:
        return tuple(
            _rdf_triple_evidence(decoded)
            for _encoded, decoded in self._native_rdf_state.triples.iter_pairs()
        )

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(
            _rdf_rule_id(decoded) for _encoded, decoded in self._native_rdf_state.rules.iter_pairs()
        )

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        return tuple(
            _rdf_diagnostic(decoded)
            for _encoded, decoded in self._native_rdf_state.diagnostics.iter_pairs()
        )

    def __eq__(self, other: object) -> bool:
        self._native_rdf_state.owner.ensure_open()
        if not isinstance(other, RDFMappingReport):
            return NotImplemented
        return (
            self.conformant == other.conformant
            and self.consumed_triples == other.consumed_triples
            and self.total_triples == other.total_triples
            and self.unconsumed == other.unconsumed
            and self.rule_ids == other.rule_ids
            and self.diagnostics == other.diagnostics
        )

    def __hash__(self) -> int:
        self._native_rdf_state.owner.ensure_open()
        return hash(
            (
                self.conformant,
                self.consumed_triples,
                self.total_triples,
                self.unconsumed,
                self.rule_ids,
                self.diagnostics,
            )
        )

    def __repr__(self) -> str:
        state = "closed" if self._native_rdf_state.owner.closed else "open"
        return (
            "RDFMappingReport("
            f"conformant={self.conformant!r}, "
            f"unconsumed_count={self._native_rdf_state.triples.count!r}, "
            f"rule_count={self._native_rdf_state.rules.count!r}, "
            f"diagnostic_count={self._native_rdf_state.diagnostics.count!r}, "
            f"storage='native', state={state!r})"
        )

    def __copy__(self) -> _NativeRDFMappingReport:
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> _NativeRDFMappingReport:
        return self

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("native RDF mapping reports cannot be pickled")


def _rdf_triple_evidence(value: object) -> RDFTripleEvidence:
    if type(value) is not NativeRDFTripleRowV2:
        raise BackendProtocolError(
            "native RDF unconsumed collection has the wrong row type",
            code="NATIVE_RDF_REPORT",
        )
    return RDFTripleEvidence(value.subject, value.predicate, value.object)


def _rdf_rule_id(value: object) -> str:
    if type(value) is not NativeRDFRuleRowV2:
        raise BackendProtocolError(
            "native RDF rule collection has the wrong row type",
            code="NATIVE_RDF_REPORT",
        )
    return value.rule_id


def _rdf_diagnostic(value: object) -> Diagnostic:
    if type(value) is not NativeRDFDiagnosticRowV2:
        raise BackendProtocolError(
            "native RDF diagnostic collection has the wrong row type",
            code="NATIVE_RDF_REPORT",
        )
    return _diagnostic(value.diagnostic, value.reference_kinds)


class _NativeOWL2DLReportState:
    __slots__ = (
        "conforms",
        "issues",
        "owner",
        "role_composite",
        "role_hierarchy",
        "role_non_simple",
        "role_properties",
        "roles",
        "structural",
        "structural_issues",
        "summary",
    )

    def __init__(
        self,
        *,
        owner: _NativeOwnerState,
        summary: NativeOWL2DLReportSummaryV2,
        conforms: bool,
        structural_issues: _NativeCollectionRef[StructuralNode],
        issues: _NativeCollectionRef[StructuralNode],
        role_properties: _NativeCollectionRef[StructuralNode],
        role_hierarchy: _NativeCollectionRef[StructuralNode],
        role_composite: _NativeCollectionRef[StructuralNode],
        role_non_simple: _NativeCollectionRef[StructuralNode],
    ) -> None:
        self.owner = owner
        self.summary = summary
        self.conforms = conforms
        self.structural_issues = structural_issues
        self.issues = issues
        self.role_properties = role_properties
        self.role_hierarchy = role_hierarchy
        self.role_composite = role_composite
        self.role_non_simple = role_non_simple
        self.structural = _NativeStructuralReport(self)
        self.roles = _NativeRoleAnalysis(self)
        owner.shared.publication_object()


def _validation_issue(
    value: object,
    expected: type[NativeOWL2DLIssueRowV2] | type[NativeOWL2DLStructuralIssueRowV2],
) -> ValidationIssue:
    if type(value) is not expected or not isinstance(
        value, (NativeOWL2DLIssueRowV2, NativeOWL2DLStructuralIssueRowV2)
    ):
        raise BackendProtocolError(
            "native OWL 2 DL issue collection has the wrong row type",
            code="NATIVE_OWL2_DL_REPORT",
        )
    return ValidationIssue(
        value.code,
        value.severity,
        value.message,
        value.constructor,
    )


class _NativeStructuralReport(StructuralReport):
    _native_owl_state: _NativeOWL2DLReportState

    __slots__ = ("_native_owl_state",)

    def __init__(
        self,
        state: _NativeOWL2DLReportState | None = None,
        **_changes: object,
    ) -> None:
        if state is None or _changes:
            raise TypeError(_REPLACE_ERROR)
        object.__setattr__(self, "_native_owl_state", state)
        state.owner.shared.publication_object()

    @property
    def issues(self) -> tuple[ValidationIssue, ...]:
        return tuple(
            _validation_issue(decoded, NativeOWL2DLStructuralIssueRowV2)
            for _encoded, decoded in self._native_owl_state.structural_issues.iter_pairs()
        )

    @property
    def values_checked(self) -> int:
        return self._native_owl_state.summary.structural_values_checked

    @property
    def complete(self) -> bool:
        return self._native_owl_state.summary.structural_complete

    def __eq__(self, other: object) -> bool:
        self._native_owl_state.owner.ensure_open()
        if not isinstance(other, StructuralReport):
            return NotImplemented
        return (
            self.issues == other.issues
            and self.values_checked == other.values_checked
            and self.complete == other.complete
        )

    def __hash__(self) -> int:
        self._native_owl_state.owner.ensure_open()
        return hash((self.issues, self.values_checked, self.complete))

    def __repr__(self) -> str:
        state = "closed" if self._native_owl_state.owner.closed else "open"
        return (
            "StructuralReport("
            f"values_checked={self.values_checked!r}, complete={self.complete!r}, "
            f"issue_count={self._native_owl_state.structural_issues.count!r}, "
            f"storage='native', state={state!r})"
        )

    def __copy__(self) -> _NativeStructuralReport:
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> _NativeStructuralReport:
        return self

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("native OWL 2 DL reports cannot be pickled")


def _role_edge(value: object) -> RoleEdge:
    if type(value) is not NativeOWL2DLRoleEdgeRowV2:
        raise BackendProtocolError(
            "native OWL 2 DL hierarchy has the wrong row type",
            code="NATIVE_OWL2_DL_REPORT",
        )
    sub_property, super_property = value._validated_properties_v2()
    return RoleEdge(sub_property, super_property)


class _NativeRoleAnalysis(RoleAnalysis):
    _native_owl_state: _NativeOWL2DLReportState

    __slots__ = ("_native_owl_state",)

    def __init__(
        self,
        state: _NativeOWL2DLReportState | None = None,
        **_changes: object,
    ) -> None:
        if state is None or _changes:
            raise TypeError(_REPLACE_ERROR)
        object.__setattr__(self, "_native_owl_state", state)
        state.owner.shared.publication_object()

    @property
    def properties(self) -> tuple[ObjectPropertyExpression, ...]:
        return tuple(
            cast(ObjectPropertyExpression, decoded)
            for _encoded, decoded in self._native_owl_state.role_properties.iter_pairs()
        )

    @property
    def hierarchy(self) -> tuple[RoleEdge, ...]:
        return tuple(
            _role_edge(decoded)
            for _encoded, decoded in self._native_owl_state.role_hierarchy.iter_pairs()
        )

    @property
    def composite(self) -> tuple[ObjectPropertyExpression, ...]:
        return tuple(
            cast(ObjectPropertyExpression, decoded)
            for _encoded, decoded in self._native_owl_state.role_composite.iter_pairs()
        )

    @property
    def non_simple(self) -> tuple[ObjectPropertyExpression, ...]:
        return tuple(
            cast(ObjectPropertyExpression, decoded)
            for _encoded, decoded in self._native_owl_state.role_non_simple.iter_pairs()
        )

    def __eq__(self, other: object) -> bool:
        self._native_owl_state.owner.ensure_open()
        if not isinstance(other, RoleAnalysis):
            return NotImplemented
        return (
            self.properties == other.properties
            and self.hierarchy == other.hierarchy
            and self.composite == other.composite
            and self.non_simple == other.non_simple
        )

    def __hash__(self) -> int:
        self._native_owl_state.owner.ensure_open()
        return hash(
            (
                self.properties,
                self.hierarchy,
                self.composite,
                self.non_simple,
            )
        )

    def __repr__(self) -> str:
        state = "closed" if self._native_owl_state.owner.closed else "open"
        return (
            "RoleAnalysis("
            f"property_count={self._native_owl_state.role_properties.count!r}, "
            f"hierarchy_count={self._native_owl_state.role_hierarchy.count!r}, "
            f"composite_count={self._native_owl_state.role_composite.count!r}, "
            f"non_simple_count={self._native_owl_state.role_non_simple.count!r}, "
            f"storage='native', state={state!r})"
        )

    def __copy__(self) -> _NativeRoleAnalysis:
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> _NativeRoleAnalysis:
        return self

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("native OWL 2 DL role analyses cannot be pickled")


class _NativeOWL2DLReport(OWL2DLReport):
    _native_owl_state: _NativeOWL2DLReportState

    __slots__ = ("_native_owl_state",)

    def __init__(
        self,
        state: _NativeOWL2DLReportState | None = None,
        **_changes: object,
    ) -> None:
        if state is None or _changes:
            raise TypeError(_REPLACE_ERROR)
        object.__setattr__(self, "_native_owl_state", state)
        state.owner.shared.publication_object()

    @property
    def structural(self) -> StructuralReport:
        return self._native_owl_state.structural

    @property
    def issues(self) -> tuple[ValidationIssue, ...]:
        return tuple(
            _validation_issue(decoded, NativeOWL2DLIssueRowV2)
            for _encoded, decoded in self._native_owl_state.issues.iter_pairs()
        )

    @property
    def roles(self) -> RoleAnalysis:
        return self._native_owl_state.roles

    @property
    def complete(self) -> bool:
        return self._native_owl_state.summary.report_complete

    @property
    def conforms(self) -> bool:
        return self._native_owl_state.conforms

    def __eq__(self, other: object) -> bool:
        self._native_owl_state.owner.ensure_open()
        if not isinstance(other, OWL2DLReport):
            return NotImplemented
        return (
            self.structural == other.structural
            and self.issues == other.issues
            and self.roles == other.roles
            and self.complete == other.complete
        )

    def __hash__(self) -> int:
        self._native_owl_state.owner.ensure_open()
        return hash((self.structural, self.issues, self.roles, self.complete))

    def __repr__(self) -> str:
        state = "closed" if self._native_owl_state.owner.closed else "open"
        return (
            "OWL2DLReport("
            f"complete={self.complete!r}, conforms={self.conforms!r}, "
            f"issue_count={self._native_owl_state.issues.count!r}, "
            f"storage='native', state={state!r})"
        )

    def __copy__(self) -> _NativeOWL2DLReport:
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> _NativeOWL2DLReport:
        return self

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("native OWL 2 DL reports cannot be pickled")


def _lazy_rdf_mapping_report(
    *,
    owner: _NativeOwnerState,
    conformant: bool,
    header: _NativeCollectionRef[StructuralNode],
    triples: _NativeCollectionRef[StructuralNode],
    rules: _NativeCollectionRef[StructuralNode],
    diagnostics: _NativeCollectionRef[StructuralNode],
) -> RDFMappingReport:
    return _NativeRDFMappingReport(
        _NativeRDFReportState(
            owner=owner,
            conformant=conformant,
            header=header,
            triples=triples,
            rules=rules,
            diagnostics=diagnostics,
        )
    )


def _lazy_owl2_dl_report(
    *,
    owner: _NativeOwnerState,
    summary: NativeOWL2DLReportSummaryV2,
    conforms: bool,
    structural_issues: _NativeCollectionRef[StructuralNode],
    issues: _NativeCollectionRef[StructuralNode],
    role_properties: _NativeCollectionRef[StructuralNode],
    role_hierarchy: _NativeCollectionRef[StructuralNode],
    role_composite: _NativeCollectionRef[StructuralNode],
    role_non_simple: _NativeCollectionRef[StructuralNode],
) -> OWL2DLReport:
    state = _NativeOWL2DLReportState(
        owner=owner,
        summary=summary,
        conforms=conforms,
        structural_issues=structural_issues,
        issues=issues,
        role_properties=role_properties,
        role_hierarchy=role_hierarchy,
        role_composite=role_composite,
        role_non_simple=role_non_simple,
    )
    return _NativeOWL2DLReport(state)


def _span(value: NativeDiagnosticPublicationV1) -> SourceSpan | None:
    coordinates = (
        value.byte_start,
        value.byte_end,
        value.line_start,
        value.column_start,
        value.line_end,
        value.column_end,
    )
    return None if all(item is None for item in coordinates) else SourceSpan(*coordinates)


def _diagnostic_reference(
    value: str | None,
    kind: NativeDiagnosticReferenceKindV2 | None,
) -> IRI | str | None:
    if value is None:
        return None
    if kind is NativeDiagnosticReferenceKindV2.IRI:
        return IRI(value)
    if kind is NativeDiagnosticReferenceKindV2.TEXT:
        return value
    raise BackendProtocolError(
        "native diagnostic reference kind is absent",
        code="NATIVE_DIAGNOSTICS",
    )


def _diagnostic(
    value: NativeDiagnosticPublicationV1,
    kinds: NativeDiagnosticReferenceKindsV2,
) -> Diagnostic:
    chain = tuple(
        cast(IRI | str, _diagnostic_reference(item, kind))
        for item, kind in zip(value.import_chain, kinds.import_chain_kinds, strict=True)
    )
    return Diagnostic(
        value.code,
        Severity(value.severity),
        value.message,
        _diagnostic_reference(value.document_iri, kinds.document_reference_kind),
        _span(value),
        chain,
        FrozenMap(value.details),
    )


def _provenance(value: NativeDocumentProvenancePublicationV1) -> DocumentProvenance:
    return DocumentProvenance(
        value.source_sha256,
        DigestKind(value.digest_kind),
        value.byte_length,
        value.decoded_codepoint_length,
        None if value.document_iri is None else IRI(value.document_iri),
        value.acquisition_locator,
        DocumentFormat(value.format),
        DetectionBasis(value.detection_basis),
        value.media_type,
        value.expected_sha256,
        value.parser,
        value.backend,
        value.api_version,
        value.model_schema,
    )


def _import_manifest(
    value: NativeImportManifestPublicationV1,
    edge_diagnostic_kinds: tuple[NativeDiagnosticReferenceKindsV2 | None, ...],
) -> ImportManifest:
    documents = tuple(
        DocumentRecord(
            item.document_key,
            item.ontology_id,
            item.document_iri,
            item.source_sha256,
            item.document_fingerprint,
            DocumentFormat(item.format),
            DocumentStatus(item.status),
        )
        for item in value.documents
    )
    edges = tuple(
        ImportEdge(
            item.importing_document_key,
            item.import_iri,
            ImportStatus(item.status),
            item.resolved_document_key,
            item.resolver_name,
            item.sanitized_locator,
            (
                None
                if item.diagnostic is None
                else _diagnostic(item.diagnostic, cast(NativeDiagnosticReferenceKindsV2, kinds))
            ),
        )
        for item, kinds in zip(value.edges, edge_diagnostic_kinds, strict=True)
    )
    return ImportManifest(
        ImportPolicy(value.policy),
        value.offline,
        value.resolver_configuration_fingerprint,
        documents,
        edges,
    )


@dataclass(slots=True)
class _NativeDocumentState:
    owner: _NativeOwnerState
    document_key: str
    ontology_id: OntologyID
    document_iri: IRI | None
    direct_imports: tuple[IRI, ...]
    ontology_annotations: _NativeCanonicalSet[Annotation]
    axioms: _NativeCanonicalSet[AxiomNode]
    extension_components: _NativeCanonicalSet[StructuralNode]
    provenance: DocumentProvenance
    source_map: SourceMap | None
    origin_index: OriginIndex | None
    rdf_mapping_report: RDFMappingReport | None
    diagnostics: tuple[Diagnostic, ...]
    document_fingerprint: Fingerprint


class _NativeOntologyDocument(OntologyDocument):
    _native_document_state: _NativeDocumentState

    __slots__ = ("_native_document_state",)

    def __init__(self, state: _NativeDocumentState | None = None, **_changes: object) -> None:
        if state is None or _changes:
            raise TypeError(_REPLACE_ERROR)
        object.__setattr__(self, "_native_document_state", state)
        state.owner.shared.publication_object()

    @property
    def ontology_id(self) -> OntologyID:
        return self._native_document_state.ontology_id

    @property
    def document_iri(self) -> IRI | None:
        return self._native_document_state.document_iri

    @property
    def direct_imports(self) -> tuple[IRI, ...]:
        return self._native_document_state.direct_imports

    @property
    def ontology_annotations(self) -> CanonicalSet[Annotation]:
        return self._native_document_state.ontology_annotations

    @property
    def axioms(self) -> CanonicalSet[AxiomNode]:
        return self._native_document_state.axioms

    @property
    def extension_components(self) -> CanonicalSet[StructuralNode]:
        return self._native_document_state.extension_components

    @property
    def provenance(self) -> DocumentProvenance:
        return self._native_document_state.provenance

    @property
    def source_map(self) -> SourceMap | None:
        return self._native_document_state.source_map

    @property
    def origin_index(self) -> OriginIndex | None:
        return self._native_document_state.origin_index

    @property
    def rdf_mapping_report(self) -> RDFMappingReport | None:
        return self._native_document_state.rdf_mapping_report

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        return self._native_document_state.diagnostics

    @property
    def document_fingerprint(self) -> Fingerprint:
        return self._native_document_state.document_fingerprint

    @property
    def closed(self) -> bool:
        return self._native_document_state.owner.closed

    def close(self) -> None:
        self._native_document_state.owner.handle.close()

    def iter_axioms(self, axiom_type: type[A] | None = None) -> Iterator[AxiomNode | A]:
        if axiom_type is None:
            yield from self.axioms
            return
        if not isinstance(axiom_type, type) or not issubclass(axiom_type, AxiomNode):
            raise TypeError("axiom_type must be an axiom class or None")
        yield from cast(Iterator[A], (item for item in self.axioms if type(item) is axiom_type))

    def iter_extensions(self, namespace: str | None = None) -> Iterator[StructuralNode]:
        if namespace not in {None, "swrl"}:
            return
        yield from self.extension_components

    def signature(
        self,
        kind: EntityKind | None = None,
        *,
        include_builtins: bool = True,
    ) -> tuple[Entity, ...]:
        if kind is not None and not isinstance(kind, EntityKind):
            raise TypeError("kind must be EntityKind or None")
        if not isinstance(include_builtins, bool):
            raise TypeError("include_builtins must be bool")
        ref: _NativeCollectionRef[Entity] = _NativeCollectionRef(
            self._native_document_state.owner,
            NativeFacadeCollectionV2.SIGNATURE,
            NativeFacadeScopeV2.DOCUMENT,
            cast(NativeDocumentHandleV2, self._native_document_state.owner.handle).document_ordinal,
            None,
            NativeSignatureKindV2.ALL if kind is None else NativeSignatureKindV2(kind.value),
            include_builtins,
        )
        return tuple(cast(Entity, decoded) for _encoded, decoded in ref.iter_pairs())

    def __eq__(self, other: object) -> bool:
        self._native_document_state.owner.ensure_open()
        if not isinstance(other, OntologyDocument):
            return NotImplemented
        return all(
            first == second
            for first, second in zip_longest(
                _document_fingerprint_parts(self),
                _document_fingerprint_parts(other),
                fillvalue=_MISSING,
            )
        )

    def __hash__(self) -> int:
        value = int.from_bytes(self.document_fingerprint.digest[:8], "big", signed=True)
        return -2 if value == -1 else value

    def __repr__(self) -> str:
        state = "closed" if self.closed else "open"
        return (
            "OntologyDocument("
            f"ontology_id={self.ontology_id!r}, document_iri={self.document_iri!r}, "
            f"document_key={self._native_document_state.document_key!r}, "
            f"axiom_count={self._native_document_state.axioms._ref.count!r}, "
            f"storage='native', state={state!r})"
        )

    def __copy__(self) -> _NativeOntologyDocument:
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> _NativeOntologyDocument:
        return self

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("native ontology documents cannot be pickled")


def _frame(value: bytes) -> bytes:
    return encode_varint(len(value)) + value


def _document_fingerprint_parts(document: OntologyDocument) -> Iterator[bytes]:
    yield b"pyowl-core:document-fingerprint:v1\x00"
    for iri in (document.ontology_id.ontology_iri, document.ontology_id.version_iri):
        if iri is None:
            yield b"0"
        else:
            yield b"1" + _frame(canonical_bytes(iri))
    for collection in (
        document.direct_imports,
        document.ontology_annotations,
        document.axioms,
        document.extension_components,
    ):
        yield encode_varint(len(collection))
        if isinstance(collection, _NativeCanonicalSet):
            for encoded in collection._ref.iter_encoded():
                yield _frame(encoded)
        else:
            for item in collection:
                yield _frame(canonical_bytes(item))


class _NativeSnapshotState:
    __slots__ = (
        "annotations_by_key",
        "anonymous_scopes",
        "axioms_by_key",
        "capabilities",
        "closure_annotations",
        "closure_axioms",
        "closure_extensions",
        "dependents",
        "diagnostics",
        "document_by_key",
        "documents",
        "extensions_by_key",
        "identity_metadata",
        "import_manifest",
        "index_cache",
        "ingestion_counters",
        "load_options",
        "lock",
        "logical_fingerprint",
        "origin_index",
        "owner",
        "pid",
        "report",
        "root",
        "root_document_key",
        "signature_fingerprint",
        "structural_fingerprint",
        "wire_structural_aliases",
    )

    def __init__(
        self,
        *,
        owner: _NativeOwnerState,
        root: OntologyDocument,
        documents: tuple[OntologyDocument, ...],
        import_manifest: ImportManifest,
        root_document_key: str,
        load_options: LoadOptions,
        diagnostics: tuple[Diagnostic, ...],
        annotations_by_key: Mapping[str, CanonicalSet[Annotation]],
        axioms_by_key: Mapping[str, CanonicalSet[AxiomNode]],
        extensions_by_key: Mapping[str, CanonicalSet[StructuralNode]],
        closure_annotations: CanonicalSet[Annotation],
        closure_axioms: CanonicalSet[AxiomNode],
        closure_extensions: CanonicalSet[StructuralNode],
        origin_index: OriginIndex,
        capabilities: CoreCapabilities,
        report: LoadReport,
        wire_structural_aliases: bool,
        ingestion_counters: _NativeIngestionCountersV2,
        anonymous_scopes: frozenset[bytes] | None,
    ) -> None:
        from pyowl_core.index.cache import create_index_cache

        self.owner = owner
        self.root = root
        self.documents = documents
        self.import_manifest = import_manifest
        self.root_document_key = root_document_key
        self.load_options = load_options
        self.diagnostics = diagnostics
        self.document_by_key = freeze_mapping(
            {
                record.document_key: document
                for record, document in zip(
                    import_manifest.documents,
                    documents,
                    strict=True,
                )
            }
        )
        self.annotations_by_key = freeze_mapping(annotations_by_key)
        self.axioms_by_key = freeze_mapping(axioms_by_key)
        self.extensions_by_key = freeze_mapping(extensions_by_key)
        self.closure_annotations = closure_annotations
        self.closure_axioms = closure_axioms
        self.closure_extensions = closure_extensions
        self.origin_index = origin_index
        self.capabilities = capabilities
        self.structural_fingerprint = report.structural_fingerprint
        self.logical_fingerprint = report.logical_fingerprint
        self.signature_fingerprint = report.signature_fingerprint
        self.report = report
        self.wire_structural_aliases = wire_structural_aliases
        self.ingestion_counters = ingestion_counters
        self.anonymous_scopes = anonymous_scopes
        self.identity_metadata = _identity_metadata_from_manifest(
            import_manifest,
            diagnostics,
            is_complete=import_manifest.is_complete,
        )
        self.index_cache = create_index_cache(self.load_options.limits)
        self.dependents = 0
        self.lock = threading.RLock()
        self.pid = os.getpid()
        owner.shared.publication_object()

    def after_fork(self) -> None:
        current = os.getpid()
        if current == self.pid:
            return
        self.pid = current
        self.lock = threading.RLock()
        from pyowl_core.index.cache import create_index_cache

        self.index_cache = create_index_cache(self.load_options.limits)

    def retain(self) -> _NativeLease:
        self.owner.ensure_open()
        self.after_fork()
        with self.lock:
            self.owner.ensure_open()
            self.dependents += 1
        return _NativeLease(self)

    def release(self) -> None:
        self.after_fork()
        with self.lock:
            if self.dependents:
                self.dependents -= 1

    def close(self) -> None:
        self.after_fork()
        with self.lock:
            if self.owner.closed:
                return
            if self.dependents:
                raise SnapshotInUseError(
                    "native snapshot still has dependent ontology views",
                    code="SNAPSHOT_IN_USE",
                )
            self.owner.handle.close()


class _NativeLease:
    __slots__ = ("_active", "_state")

    def __init__(self, state: _NativeSnapshotState) -> None:
        self._state = state
        self._active = True

    def release(self) -> None:
        if self._active:
            self._active = False
            self._state.release()

    def __del__(self) -> None:
        self.release()


class _NativeOntologySnapshot(OntologySnapshot):
    _native_snapshot_state: _NativeSnapshotState

    __slots__ = ("_native_snapshot_state",)

    def __init__(self, state: _NativeSnapshotState | None = None, **_changes: object) -> None:
        if state is None or _changes:
            raise TypeError(_REPLACE_ERROR)
        object.__setattr__(self, "_native_snapshot_state", state)

    @property
    def root(self) -> OntologyDocument:
        return self._native_snapshot_state.root

    @property
    def documents(self) -> tuple[OntologyDocument, ...]:
        return self._native_snapshot_state.documents

    @property
    def import_manifest(self) -> ImportManifest:
        return self._native_snapshot_state.import_manifest

    @property
    def root_document_key(self) -> str:
        return self._native_snapshot_state.root_document_key

    @property
    def load_options(self) -> LoadOptions:
        return self._native_snapshot_state.load_options

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        return self._native_snapshot_state.diagnostics

    @property
    def timings(self) -> Mapping[str, float]:
        return self.report.timings

    @property
    def resolution_attempts(self) -> int:
        return self.report.resolution_attempts

    @property
    def acquisition_cache_hits(self) -> int:
        return self.report.acquisition_cache_hits

    @property
    def document_cache_hits(self) -> int:
        return self.report.document_cache_hits

    @property
    def _document_by_key(self) -> Mapping[str, OntologyDocument]:
        return self._native_snapshot_state.document_by_key

    @property
    def _axioms_by_key(self) -> Mapping[str, CanonicalSet[AxiomNode]]:
        return self._native_snapshot_state.axioms_by_key

    @property
    def _annotations_by_key(self) -> Mapping[str, CanonicalSet[Annotation]]:
        return self._native_snapshot_state.annotations_by_key

    @property
    def _extensions_by_key(self) -> Mapping[str, CanonicalSet[StructuralNode]]:
        return self._native_snapshot_state.extensions_by_key

    @property
    def _closure_axioms(self) -> CanonicalSet[AxiomNode]:
        return self._native_snapshot_state.closure_axioms

    @property
    def _closure_annotations(self) -> CanonicalSet[Annotation]:
        return self._native_snapshot_state.closure_annotations

    @property
    def _closure_extensions(self) -> CanonicalSet[StructuralNode]:
        return self._native_snapshot_state.closure_extensions

    @property
    def _anonymous_scopes(self) -> frozenset[bytes]:
        return self._anonymous_document_scopes()

    @property
    def _origin_index(self) -> OriginIndex:
        return self._native_snapshot_state.origin_index

    @property
    def _capabilities(self) -> CoreCapabilities:
        return self._native_snapshot_state.capabilities

    @property
    def _structural_fingerprint(self) -> Fingerprint:
        return self._native_snapshot_state.structural_fingerprint

    @property
    def _logical_fingerprint(self) -> Fingerprint:
        return self._native_snapshot_state.logical_fingerprint

    @property
    def _signature_fingerprint(self) -> Fingerprint:
        return self._native_snapshot_state.signature_fingerprint

    @property
    def _owl2_dl_report(self) -> OWL2DLReport | None:
        return self._native_snapshot_state.report.owl2_dl_report

    @property
    def _report(self) -> LoadReport:
        return self._native_snapshot_state.report

    @property
    def _preserve_document_scopes(self) -> bool:
        return True

    @property
    def _origin_index_override(self) -> OriginIndex:
        return self.origin_index

    @property
    def _structural_context(self) -> None:
        return None

    @property
    def _structural_fingerprint_override(self) -> Fingerprint:
        return self.structural_fingerprint

    @property
    def _complete_override(self) -> bool:
        return self.is_complete

    @property
    def _identity_metadata_override(self) -> _OntologyIdentityMetadata:
        return self._native_snapshot_state.identity_metadata

    @property
    def _wire_verified(self) -> bool:
        return False

    @property
    def _index_cache(self) -> object:
        self._check_open()
        return self._native_snapshot_state.index_cache

    @property
    def closed(self) -> bool:
        return self._native_snapshot_state.owner.closed

    def close(self) -> None:
        self._native_snapshot_state.close()

    def __enter__(self) -> _NativeOntologySnapshot:
        self._check_open()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, exception, traceback
        self.close()

    @property
    def capabilities(self) -> CoreCapabilities:
        return self._native_snapshot_state.capabilities

    def _check_open(self) -> None:
        self._native_snapshot_state.owner.ensure_open()

    def _retain_dependent(self) -> object:
        return self._native_snapshot_state.retain()

    def _anonymous_document_scopes(self) -> frozenset[bytes]:
        self._check_open()
        retained = self._native_snapshot_state.anonymous_scopes
        if retained is not None:
            return retained
        scopes: set[bytes] = set()
        collections: tuple[Mapping[str, CanonicalSet[StructuralNode]], ...] = (
            cast(Mapping[str, CanonicalSet[StructuralNode]], self._annotations_by_key),
            cast(Mapping[str, CanonicalSet[StructuralNode]], self._axioms_by_key),
            self._extensions_by_key,
        )
        for collection in collections:
            for values in collection.values():
                for root in values:
                    for node in walk(root):
                        if isinstance(node, AnonymousIndividual):
                            scopes.add(node.document_scope)
        return frozenset(scopes)

    def _anonymous_scope_lineage(self) -> tuple[tuple[bytes, bytes, bytes], ...]:
        leaf = self.structural_fingerprint.digest
        return tuple((scope, scope, leaf) for scope in sorted(self._anonymous_document_scopes()))

    def _ontology_identity_metadata(
        self,
        *,
        cancellation_token: CancellationToken | None = None,
    ) -> _OntologyIdentityMetadata:
        self._check_open()
        if cancellation_token is not None:
            cancellation_token.check()
        return self._native_snapshot_state.identity_metadata

    @property
    def is_complete(self) -> bool:
        return self.import_manifest.is_complete

    @property
    def origin_index(self) -> OriginIndex:
        return self._native_snapshot_state.origin_index

    @property
    def structural_context(self) -> None:
        return None

    @property
    def structural_fingerprint(self) -> Fingerprint:
        return self._native_snapshot_state.structural_fingerprint

    @property
    def logical_fingerprint(self) -> Fingerprint:
        return self._native_snapshot_state.logical_fingerprint

    @property
    def signature_fingerprint(self) -> Fingerprint:
        return self._native_snapshot_state.signature_fingerprint

    @property
    def report(self) -> LoadReport:
        return self._native_snapshot_state.report

    @property
    def owl2_dl_report(self) -> OWL2DLReport | None:
        return self._native_snapshot_state.report.owl2_dl_report

    def document(self, document_key: str) -> OntologyDocument:
        if not isinstance(document_key, str) or not document_key:
            raise ValueError("document_key must be a nonempty string")
        return self._document_by_key[document_key]

    def iter_documents(self) -> Iterator[tuple[DocumentRecord, OntologyDocument]]:
        yield from zip(self.import_manifest.documents, self.documents, strict=True)

    def iter_axioms(
        self,
        axiom_type: type[A] | None = None,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> Iterator[AxiomNode | A]:
        values = self._axioms(scope, document_key)
        if axiom_type is None:
            yield from values
            return
        if not isinstance(axiom_type, type) or not issubclass(axiom_type, AxiomNode):
            raise TypeError("axiom_type must be an axiom class or None")
        yield from cast(Iterator[A], (item for item in values if type(item) is axiom_type))

    def iter_extensions(
        self,
        namespace: str | None = None,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> Iterator[StructuralNode]:
        if namespace not in {None, "swrl"}:
            return
        yield from self._extensions(scope, document_key)

    def ontology_annotations(
        self,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> CanonicalSet[Annotation]:
        if scope is AxiomScope.CLOSURE:
            _reject_document_key(scope, document_key)
            return self._closure_annotations
        return self._annotations_by_key[self._scope_key(scope, document_key)]

    def contains(
        self,
        axiom: AxiomNode,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> bool:
        if not isinstance(axiom, AxiomNode):
            raise TypeError("axiom must be an OWL axiom")
        return axiom in self._axioms(scope, document_key)

    def signature(
        self,
        kind: EntityKind | None = None,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
        include_builtins: bool = True,
    ) -> tuple[Entity, ...]:
        if kind is not None and not isinstance(kind, EntityKind):
            raise TypeError("kind must be EntityKind or None")
        if not isinstance(include_builtins, bool):
            raise TypeError("include_builtins must be bool")
        selected_scope, ordinal = self._native_scope(scope, document_key)
        ref: _NativeCollectionRef[Entity] = _NativeCollectionRef(
            self._native_snapshot_state.owner,
            NativeFacadeCollectionV2.SIGNATURE,
            selected_scope,
            ordinal,
            None,
            NativeSignatureKindV2.ALL if kind is None else NativeSignatureKindV2(kind.value),
            include_builtins,
        )
        return tuple(cast(Entity, decoded) for _encoded, decoded in ref.iter_pairs())

    def view(self, view_type: type[V], /, **options: object) -> V:
        if not isinstance(view_type, type):
            raise TypeError("view_type must be a type")
        if view_type is OntologySnapshot or isinstance(self, view_type):
            if options:
                raise TypeError("OntologySnapshot identity view accepts no options")
            return cast(V, self)
        from pyowl_core.index.cache import request_index_view

        return request_index_view(self, view_type, options)

    def _axioms(self, scope: AxiomScope, document_key: str | None) -> CanonicalSet[AxiomNode]:
        if not isinstance(scope, AxiomScope):
            raise TypeError("scope must be AxiomScope")
        if scope is AxiomScope.CLOSURE:
            _reject_document_key(scope, document_key)
            return self._closure_axioms
        return self._axioms_by_key[self._scope_key(scope, document_key)]

    def _extensions(
        self,
        scope: AxiomScope,
        document_key: str | None,
    ) -> CanonicalSet[StructuralNode]:
        if not isinstance(scope, AxiomScope):
            raise TypeError("scope must be AxiomScope")
        if scope is AxiomScope.CLOSURE:
            _reject_document_key(scope, document_key)
            return self._closure_extensions
        return self._extensions_by_key[self._scope_key(scope, document_key)]

    def _scope_key(self, scope: AxiomScope, document_key: str | None) -> str:
        if scope is AxiomScope.ROOT:
            _reject_document_key(scope, document_key)
            return self.root_document_key
        if scope is AxiomScope.DOCUMENT:
            if not isinstance(document_key, str) or not document_key:
                raise ValueError("AxiomScope.DOCUMENT requires document_key")
            if document_key not in self._document_by_key:
                raise KeyError(document_key)
            return document_key
        raise AssertionError(scope)

    def _native_scope(
        self,
        scope: AxiomScope,
        document_key: str | None,
    ) -> tuple[NativeFacadeScopeV2, int | None]:
        if scope is AxiomScope.CLOSURE:
            _reject_document_key(scope, document_key)
            return NativeFacadeScopeV2.CLOSURE, None
        key = self._scope_key(scope, document_key)
        ordinal = next(
            index
            for index, record in enumerate(self.import_manifest.documents)
            if record.document_key == key
        )
        return NativeFacadeScopeV2.DOCUMENT, ordinal

    def __eq__(self, other: object) -> bool:
        self._check_open()
        if not isinstance(other, OntologySnapshot):
            return NotImplemented
        return (
            self.structural_fingerprint == other.structural_fingerprint
            and self.import_manifest == other.import_manifest
            and self._closure_axioms == other._closure_axioms
        )

    def __hash__(self) -> int:
        value = int.from_bytes(self.structural_fingerprint.digest[:8], "big", signed=True)
        return -2 if value == -1 else value

    def __repr__(self) -> str:
        state = "closed" if self.closed else "open"
        return (
            "OntologySnapshot("
            f"root_document_key={self.root_document_key!r}, "
            f"document_count={len(self.documents)!r}, "
            f"effective_axiom_count={self.report.effective_axiom_count!r}, "
            f"storage='native', state={state!r})"
        )

    def __copy__(self) -> _NativeOntologySnapshot:
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> _NativeOntologySnapshot:
        return self

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("native ontology snapshots cannot be pickled")

    def _native_python_counters(self) -> NativePythonFacadeCountersV2:
        return self._native_snapshot_state.owner.shared.counters()

    def _native_ingestion_counters_v2(self) -> _NativeIngestionCountersV2:
        """Return immutable counters for eager retained-parser publication work."""

        self._check_open()
        return self._native_snapshot_state.ingestion_counters

    def _native_wire_structural_aliases_v1(self) -> bool:
        """Report an internally attested raw/effective/closure root alias."""

        self._check_open()
        return self._native_snapshot_state.wire_structural_aliases

    def _native_origin_rows_v2(self) -> Iterator[bytes]:
        """Yield each retained origin row once without facade-cache materialization."""

        self._check_open()
        entries = self._native_snapshot_state.origin_index.entries
        if not self.load_options.collect_provenance:
            if entries:
                raise BackendProtocolError(
                    "native wire source retained origins without provenance capability",
                    code="NATIVE_WIRE_SOURCE",
                )
            return
        if not isinstance(entries, _NativeOriginMapping):
            raise BackendProtocolError(
                "native wire source does not retain an origin-row facade",
                code="NATIVE_WIRE_SOURCE",
            )
        yield from entries._ref.iter_encoded()


def _reject_document_key(scope: AxiomScope, document_key: str | None) -> None:
    if document_key is not None:
        raise ValueError(f"document_key is not valid for {scope.value} scope")


def _capabilities(publication: NativeSnapshotPublicationV2) -> CoreCapabilities:
    features = {
        "owl2-structural",
        "document-boundaries",
        "import-manifest",
        "immutable-snapshot",
        "document-scoped-anonymous",
        "structural-indexes",
        "ontology-identity-index",
        "lazy-model",
    }
    if publication.capability_bits & 8:
        features.add("source-map")
    if publication.report.owl2_dl_validated:
        features.add("owl2-dl-validated")
    return CoreCapabilities(
        1,
        publication.report.model_schema,
        (1, 1),
        frozenset(features),
        {},
        "native",
    )


def ontology_snapshot_from_native_publication_v2(
    publication: NativeSnapshotPublicationV2,
    /,
    *,
    _wire_structural_aliases: object | None = None,
    _ingestion_counters: _NativeIngestionCountersV2 | None = None,
    _anonymous_scope_evidence: object | None = None,
) -> OntologySnapshot:
    """Publish a lazy public snapshot without materializing retained roots."""

    if (
        _wire_structural_aliases is not None
        and _wire_structural_aliases is not _WIRE_STRUCTURAL_ALIAS_SEAL_V1
    ):
        raise TypeError("_wire_structural_aliases carries an invalid internal seal")
    wire_structural_aliases = _wire_structural_aliases is _WIRE_STRUCTURAL_ALIAS_SEAL_V1
    if _ingestion_counters is None:
        ingestion_counters = _NativeIngestionCountersV2()
    elif type(_ingestion_counters) is _NativeIngestionCountersV2:
        ingestion_counters = _ingestion_counters
    else:
        raise TypeError("_ingestion_counters has the wrong internal type")
    anonymous_scopes: frozenset[bytes] | None
    if _anonymous_scope_evidence is None:
        anonymous_scopes = None
    elif _anonymous_scope_evidence is _NO_ANONYMOUS_SCOPES_SEAL_V2:
        anonymous_scopes = frozenset()
    else:
        raise TypeError("_anonymous_scope_evidence carries an invalid internal seal")
    selected = require_native_facade_publication_v2(publication)
    shared = _NativeSharedState(selected)
    sidecars = selected.diagnostic_reference_sidecars
    diagnostics = tuple(
        _diagnostic(item, kinds)
        for item, kinds in zip(selected.diagnostics, sidecars.snapshot, strict=True)
    )
    manifest = _import_manifest(selected.import_manifest, sidecars.import_edges)
    document_handles: list[NativeDocumentHandleV2] = []
    try:
        for ordinal in range(len(selected.documents)):
            # Append immediately so a later owner-fork failure reaches the
            # deterministic cleanup below.  A comprehension would lose the
            # successfully created prefix when assignment never completes.
            document_handles.append(selected.handle._facade_document_v2(ordinal))
        documents: list[OntologyDocument] = []
        effective_annotations: dict[str, CanonicalSet[Annotation]] = {}
        effective_axioms: dict[str, CanonicalSet[AxiomNode]] = {}
        effective_extensions: dict[str, CanonicalSet[StructuralNode]] = {}
        for ordinal, (metadata, cardinalities, handle, diagnostic_kinds) in enumerate(
            zip(
                selected.documents,
                selected.facade_cardinality_summary.documents,
                document_handles,
                sidecars.documents,
                strict=True,
            )
        ):
            raw_owner = _NativeOwnerState(handle, shared)
            snapshot_owner = _NativeOwnerState(selected.handle, shared)

            def ref(
                owner: _NativeOwnerState,
                collection: NativeFacadeCollectionV2,
                count: int,
                selected_ordinal: int = ordinal,
            ) -> _NativeCollectionRef[StructuralNode]:
                return _NativeCollectionRef(
                    owner,
                    collection,
                    NativeFacadeScopeV2.DOCUMENT,
                    selected_ordinal,
                    count,
                )

            raw_annotations = _NativeCanonicalSet(
                cast(
                    _NativeCollectionRef[Annotation],
                    ref(
                        raw_owner,
                        NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS,
                        metadata.ontology_annotation_count,
                    ),
                )
            )
            raw_axioms = _NativeCanonicalSet(
                cast(
                    _NativeCollectionRef[AxiomNode],
                    ref(raw_owner, NativeFacadeCollectionV2.AXIOMS, metadata.axiom_count),
                )
            )
            raw_extensions = _NativeCanonicalSet(
                ref(raw_owner, NativeFacadeCollectionV2.EXTENSIONS, metadata.extension_count)
            )
            source_map = None
            if selected.capability_bits & 8:
                source_map = _lazy_source_map(
                    ref(
                        raw_owner,
                        NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES,
                        metadata.source_map_entry_count,
                    ),
                    ref(
                        raw_owner,
                        NativeFacadeCollectionV2.SOURCE_MAP_PREFIXES,
                        cardinalities.raw_source_prefix_count,
                    ),
                )
            raw_origin = None
            if selected.capability_bits & 16:
                raw_origin = _lazy_origin_index(
                    ref(
                        raw_owner,
                        NativeFacadeCollectionV2.ORIGIN_ENTRIES,
                        metadata.origin_entry_count,
                    )
                )
            rdf_mapping_report = None
            if metadata.rdf_mapping_report_sha256 is not None:
                if metadata.rdf_mapping_conformant is None:
                    raise BackendProtocolError(
                        "native RDF report metadata omits its conformance scalar",
                        code="NATIVE_RDF_REPORT",
                    )
                rdf_mapping_report = _lazy_rdf_mapping_report(
                    owner=raw_owner,
                    conformant=metadata.rdf_mapping_conformant,
                    header=ref(
                        raw_owner,
                        NativeFacadeCollectionV2.RDF_REPORT_HEADER,
                        1,
                    ),
                    triples=ref(
                        raw_owner,
                        NativeFacadeCollectionV2.RDF_UNCONSUMED_TRIPLES,
                        cardinalities.rdf_unconsumed_triple_count,
                    ),
                    rules=ref(
                        raw_owner,
                        NativeFacadeCollectionV2.RDF_RULE_IDS,
                        cardinalities.rdf_rule_count,
                    ),
                    diagnostics=ref(
                        raw_owner,
                        NativeFacadeCollectionV2.RDF_DIAGNOSTICS,
                        cardinalities.rdf_diagnostic_count,
                    ),
                )
            document_diagnostics = tuple(
                _diagnostic(item, kinds)
                for item, kinds in zip(metadata.diagnostics, diagnostic_kinds, strict=True)
            )
            state = _NativeDocumentState(
                raw_owner,
                metadata.document_key,
                metadata.ontology_id,
                metadata.document_iri,
                metadata.direct_imports,
                raw_annotations,
                raw_axioms,
                raw_extensions,
                _provenance(metadata.provenance),
                source_map,
                raw_origin,
                rdf_mapping_report,
                document_diagnostics,
                metadata.document_fingerprint,
            )
            documents.append(_NativeOntologyDocument(state))
            effective_annotations[metadata.document_key] = _NativeCanonicalSet(
                cast(
                    _NativeCollectionRef[Annotation],
                    ref(
                        snapshot_owner,
                        NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS,
                        cardinalities.effective_annotation_count,
                    ),
                )
            )
            effective_axioms[metadata.document_key] = _NativeCanonicalSet(
                cast(
                    _NativeCollectionRef[AxiomNode],
                    ref(
                        snapshot_owner,
                        NativeFacadeCollectionV2.AXIOMS,
                        cardinalities.effective_axiom_count,
                    ),
                )
            )
            effective_extensions[metadata.document_key] = _NativeCanonicalSet(
                ref(
                    snapshot_owner,
                    NativeFacadeCollectionV2.EXTENSIONS,
                    cardinalities.effective_extension_count,
                )
            )

        snapshot_owner = _NativeOwnerState(selected.handle, shared)
        closure = selected.facade_cardinality_summary.closure
        if wire_structural_aliases:
            if len(selected.documents) != 1:
                raise BackendProtocolError(
                    "native wire structural aliases require exactly one document",
                    code="NATIVE_WIRE_SOURCE",
                )
            document = selected.documents[0]
            effective = selected.facade_cardinality_summary.documents[0]
            raw_counts = (
                document.ontology_annotation_count,
                document.axiom_count,
                document.extension_count,
            )
            effective_counts = (
                effective.effective_annotation_count,
                effective.effective_axiom_count,
                effective.effective_extension_count,
            )
            closure_counts = (
                closure.effective_annotation_count,
                closure.effective_axiom_count,
                closure.effective_extension_count,
            )
            if raw_counts != effective_counts or effective_counts != closure_counts:
                raise BackendProtocolError(
                    "native wire structural alias cardinalities diverge",
                    code="NATIVE_WIRE_SOURCE",
                )

        def closure_ref(
            collection: NativeFacadeCollectionV2,
            count: int,
        ) -> _NativeCollectionRef[StructuralNode]:
            return _NativeCollectionRef(
                snapshot_owner,
                collection,
                NativeFacadeScopeV2.CLOSURE,
                None,
                count,
            )

        closure_annotations = _NativeCanonicalSet(
            cast(
                _NativeCollectionRef[Annotation],
                closure_ref(
                    NativeFacadeCollectionV2.ONTOLOGY_ANNOTATIONS,
                    closure.effective_annotation_count,
                ),
            )
        )
        closure_axioms = _NativeCanonicalSet(
            cast(
                _NativeCollectionRef[AxiomNode],
                closure_ref(
                    NativeFacadeCollectionV2.AXIOMS,
                    closure.effective_axiom_count,
                ),
            )
        )
        closure_extensions = _NativeCanonicalSet(
            closure_ref(
                NativeFacadeCollectionV2.EXTENSIONS,
                closure.effective_extension_count,
            )
        )
        origin_index = (
            _lazy_origin_index(
                closure_ref(
                    NativeFacadeCollectionV2.ORIGIN_ENTRIES,
                    closure.effective_origin_count,
                )
            )
            if selected.capability_bits & 16
            else OriginIndex()
        )
        report_metadata = selected.report
        owl2_dl_report = None
        owl2_summary = selected.owl2_dl_report_summary
        if owl2_summary is not None:
            if report_metadata.owl2_dl_conforms is None:
                raise BackendProtocolError(
                    "native OWL 2 DL report metadata omits its conformance scalar",
                    code="NATIVE_OWL2_DL_REPORT",
                )
            owl2_dl_report = _lazy_owl2_dl_report(
                owner=snapshot_owner,
                summary=owl2_summary,
                conforms=report_metadata.owl2_dl_conforms,
                structural_issues=closure_ref(
                    NativeFacadeCollectionV2.OWL2_DL_STRUCTURAL_ISSUES,
                    owl2_summary.structural_issue_count,
                ),
                issues=closure_ref(
                    NativeFacadeCollectionV2.OWL2_DL_ISSUES,
                    owl2_summary.issue_count,
                ),
                role_properties=closure_ref(
                    NativeFacadeCollectionV2.OWL2_DL_ROLE_PROPERTIES,
                    owl2_summary.role_property_count,
                ),
                role_hierarchy=closure_ref(
                    NativeFacadeCollectionV2.OWL2_DL_ROLE_HIERARCHY,
                    owl2_summary.role_hierarchy_count,
                ),
                role_composite=closure_ref(
                    NativeFacadeCollectionV2.OWL2_DL_ROLE_COMPOSITE,
                    owl2_summary.role_composite_count,
                ),
                role_non_simple=closure_ref(
                    NativeFacadeCollectionV2.OWL2_DL_ROLE_NON_SIMPLE,
                    owl2_summary.role_non_simple_count,
                ),
            )
        report = LoadReport(
            report_metadata.backend,
            report_metadata.api_version,
            report_metadata.model_schema,
            report_metadata.document_count,
            report_metadata.total_source_bytes,
            report_metadata.effective_axiom_count,
            report_metadata.resolution_attempts,
            report_metadata.acquisition_cache_hits,
            report_metadata.document_cache_hits,
            FrozenMap(report_metadata.timings),
            diagnostics,
            report_metadata.structural_fingerprint,
            report_metadata.logical_fingerprint,
            report_metadata.signature_fingerprint,
            owl2_dl_report,
        )
        document_tuple = tuple(documents)
        document_by_key = {
            record.document_key: document
            for record, document in zip(manifest.documents, document_tuple, strict=True)
        }
        root = document_by_key[selected.root_document_key]
        snapshot_state = _NativeSnapshotState(
            owner=snapshot_owner,
            root=root,
            documents=document_tuple,
            import_manifest=manifest,
            root_document_key=selected.root_document_key,
            load_options=selected.load_options,
            diagnostics=diagnostics,
            annotations_by_key=effective_annotations,
            axioms_by_key=effective_axioms,
            extensions_by_key=effective_extensions,
            closure_annotations=closure_annotations,
            closure_axioms=closure_axioms,
            closure_extensions=closure_extensions,
            origin_index=origin_index,
            capabilities=_capabilities(selected),
            report=report,
            wire_structural_aliases=wire_structural_aliases,
            ingestion_counters=ingestion_counters,
            anonymous_scopes=anonymous_scopes,
        )
        result = _NativeOntologySnapshot(snapshot_state)
        shared.publication_object()
        return result
    except BaseException:
        for handle in document_handles:
            with suppress(Exception):
                handle.close()
        raise


__all__ = ["ontology_snapshot_from_native_publication_v2"]
