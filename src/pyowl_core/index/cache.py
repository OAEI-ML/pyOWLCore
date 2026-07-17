"""Thread-safe snapshot-local once cache for immutable structural views."""

from __future__ import annotations

import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import Enum
from typing import ClassVar, Protocol, TypeVar, cast, runtime_checkable

from pyowl_core._immutable import FrozenMap, freeze_mapping
from pyowl_core.cancellation import CancellationToken
from pyowl_core.exceptions import ReentrancyError, ResourceLimitError
from pyowl_core.limits import ParseLimits

V = TypeVar("V")


class CacheRetention(str, Enum):
    """How completed views are retained by a snapshot-local cache."""

    STRONG = "strong"
    WEAK = "weak"


@dataclass(frozen=True, slots=True)
class IndexCachePolicy:
    """Bounded immutable cache policy; limits always remain the tighter bound."""

    max_bytes: int | None = None
    retention: CacheRetention = CacheRetention.STRONG

    def __post_init__(self) -> None:
        if self.max_bytes is not None and (
            isinstance(self.max_bytes, bool)
            or not isinstance(self.max_bytes, int)
            or self.max_bytes < 1
        ):
            raise ValueError("max_bytes must be a positive integer or None")
        retention = self.retention
        if isinstance(retention, str) and not isinstance(retention, CacheRetention):
            try:
                retention = CacheRetention(retention)
            except ValueError as error:
                raise ValueError("retention must be a CacheRetention value") from error
            object.__setattr__(self, "retention", retention)
        elif not isinstance(retention, CacheRetention):
            raise TypeError("retention must be CacheRetention")


class ViewBuildStrategy(str, Enum):
    FULL_BUILD = "full_build"
    PATCHED = "patched"
    MERGED = "merged"


@dataclass(frozen=True, slots=True)
class ViewBuildReport:
    """Deterministic accounting attached to one immutable view build."""

    schema_name: str
    schema_version: int
    strategy: ViewBuildStrategy
    row_count: int
    shared_row_count: int
    own_bytes: int
    shared_bytes: int
    build_seconds: float
    tables: Mapping[str, int] = FrozenMap()

    def __post_init__(self) -> None:
        if not isinstance(self.schema_name, str) or not self.schema_name:
            raise ValueError("schema_name must be a nonempty string")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version < 1
        ):
            raise ValueError("schema_version must be a positive integer")
        if not isinstance(self.strategy, ViewBuildStrategy):
            raise TypeError("strategy must be ViewBuildStrategy")
        for name in ("row_count", "shared_row_count", "own_bytes", "shared_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if not isinstance(self.build_seconds, (int, float)) or self.build_seconds < 0:
            raise ValueError("build_seconds must be nonnegative")
        table_values: dict[str, int] = {}
        for name, value in self.tables.items():
            if not isinstance(name, str) or not name:
                raise ValueError("table names must be nonempty strings")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("table byte counts must be nonnegative integers")
            table_values[name] = value
        object.__setattr__(self, "build_seconds", float(self.build_seconds))
        object.__setattr__(self, "tables", freeze_mapping(table_values))

    @property
    def total_row_count(self) -> int:
        return self.row_count + self.shared_row_count


@dataclass(frozen=True, slots=True)
class IndexCacheReport:
    policy: IndexCachePolicy
    live_identities: int
    retained_entries: int
    retained_bytes: int
    reserved_bytes: int
    hits: int
    misses: int
    builds: int
    evictions: int
    failures: int
    waits: int


@runtime_checkable
class StructuralViewFactory(Protocol):
    """Explicit extension point for immutable syntax-only views."""

    SCHEMA_NAME: ClassVar[str]
    SCHEMA_VERSION: ClassVar[int]
    OPTIONS_TYPE: ClassVar[type[object]]

    @classmethod
    def _build(
        cls,
        ontology: object,
        options: object,
        budget: IndexBuildBudget,
        cancellation_token: CancellationToken | None,
        started: float,
    ) -> object: ...


class IndexBuildBudget:
    """Incremental row/byte reservation made before posting growth."""

    __slots__ = (
        "_bytes",
        "_cache",
        "_limits",
        "_rows",
        "_shared_rows",
        "_tables",
        "_token",
    )

    def __init__(
        self,
        cache: IndexCache,
        limits: ParseLimits,
        token: CancellationToken | None,
    ) -> None:
        self._cache = cache
        self._limits = limits
        self._token = token
        self._rows = 0
        self._shared_rows = 0
        self._bytes = 0
        self._tables: dict[str, int] = {}

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def bytes(self) -> int:
        return self._bytes

    @property
    def shared_rows(self) -> int:
        return self._shared_rows

    @property
    def tables(self) -> Mapping[str, int]:
        return freeze_mapping(self._tables)

    def add(self, table: str, *, rows: int = 1, bytes_: int = 128) -> None:
        if not isinstance(table, str) or not table:
            raise ValueError("table must be a nonempty string")
        if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
            raise ValueError("rows must be a nonnegative integer")
        if isinstance(bytes_, bool) or not isinstance(bytes_, int) or bytes_ < 0:
            raise ValueError("bytes_ must be a nonnegative integer")
        next_rows = self._rows + rows
        self._limits.enforce("max_index_rows", next_rows + self._shared_rows)
        if self._token is not None and (
            next_rows == 0 or next_rows % self._limits.cancellation_check_interval < rows
        ):
            self._token.check()
        if bytes_:
            self._cache._reserve(bytes_)
            self._bytes += bytes_
            self._tables[table] = self._tables.get(table, 0) + bytes_
        self._rows = next_rows

    def add_shared_rows(self, rows: int) -> None:
        if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
            raise ValueError("rows must be a nonnegative integer")
        selected = self._shared_rows + rows
        self._limits.enforce("max_index_rows", self._rows + selected)
        self._shared_rows = selected
        self.check()

    def check(self) -> None:
        if self._token is not None:
            self._token.check()

    def _release(self) -> None:
        if self._bytes:
            self._cache._release_reservation(self._bytes)


@dataclass(slots=True)
class _CacheEntry:
    value: weakref.ReferenceType[object]
    bytes: int

    def get(self) -> object | None:
        return self.value()


class IndexCache:
    """A per-ontology cache with one builder per canonical request key."""

    __slots__ = (
        "_builds",
        "_bytes",
        "_condition",
        "_entries",
        "_evictions",
        "_failures",
        "_hits",
        "_inflight",
        "_limits",
        "_lock",
        "_misses",
        "_policy",
        "_reserved",
        "_strong",
        "_waits",
    )

    def __init__(
        self,
        limits: ParseLimits,
        policy: IndexCachePolicy | None = None,
    ) -> None:
        if not isinstance(limits, ParseLimits):
            raise TypeError("limits must be ParseLimits")
        self._limits = limits
        self._policy = policy or IndexCachePolicy()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._entries: OrderedDict[tuple[object, ...], _CacheEntry] = OrderedDict()
        self._strong: OrderedDict[tuple[object, ...], object] = OrderedDict()
        self._inflight: dict[tuple[object, ...], int] = {}
        self._bytes = 0
        self._reserved = 0
        self._hits = 0
        self._misses = 0
        self._builds = 0
        self._evictions = 0
        self._failures = 0
        self._waits = 0

    @property
    def maximum_bytes(self) -> int:
        selected = self._policy.max_bytes
        values = [self._limits.max_index_bytes]
        if selected is not None:
            values.append(selected)
        if self._limits.max_memory_bytes is not None:
            values.append(self._limits.max_memory_bytes)
        return min(values)

    def configure(self, policy: IndexCachePolicy) -> None:
        if not isinstance(policy, IndexCachePolicy):
            raise TypeError("policy must be IndexCachePolicy")
        with self._condition:
            if self._inflight:
                raise RuntimeError("cannot change index cache policy during a build")
            self._policy = policy
            if policy.retention is CacheRetention.WEAK:
                self._strong.clear()
                self._bytes = 0
            self._evict_until(0)

    def clear(self) -> None:
        with self._condition:
            if self._inflight:
                raise RuntimeError("cannot clear index cache during a build")
            # The weak identity registry remains until callers release their
            # views. This preserves the contract that an equal request returns
            # the same object while that object is alive.
            self._strong.clear()
            self._bytes = 0

    def report(self) -> IndexCacheReport:
        with self._lock:
            self._discard_dead_weak()
            return IndexCacheReport(
                self._policy,
                len(self._entries),
                len(self._strong),
                self._bytes,
                self._reserved,
                self._hits,
                self._misses,
                self._builds,
                self._evictions,
                self._failures,
                self._waits,
            )

    def get_or_build(
        self,
        ontology: object,
        factory: type[V],
        options: object,
        token: CancellationToken | None,
    ) -> V:
        schema_name = getattr(factory, "SCHEMA_NAME", None)
        schema_version = getattr(factory, "SCHEMA_VERSION", None)
        if not isinstance(schema_name, str) or not schema_name:
            raise TypeError("view factory SCHEMA_NAME must be a nonempty string")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version < 1
        ):
            raise TypeError("view factory SCHEMA_VERSION must be a positive integer")
        try:
            hash(options)
        except TypeError as error:
            raise TypeError("view options must be immutable and hashable") from error
        key = (factory, schema_name, schema_version, options)
        identity = threading.get_ident()
        with self._condition:
            while True:
                entry = self._entries.get(key)
                if entry is not None:
                    retained = entry.get()
                    if retained is not None:
                        self._entries.move_to_end(key)
                        if self._policy.retention is CacheRetention.STRONG:
                            if key in self._strong:
                                self._strong.move_to_end(key)
                            elif entry.bytes <= self.maximum_bytes:
                                self._evict_until(entry.bytes)
                                if self._bytes + self._reserved + entry.bytes <= self.maximum_bytes:
                                    self._strong[key] = retained
                                    self._bytes += entry.bytes
                        self._hits += 1
                        return cast(V, retained)
                    self._remove_entry(key)
                owner = self._inflight.get(key)
                if owner is None:
                    self._inflight[key] = identity
                    self._misses += 1
                    break
                if owner == identity:
                    raise ReentrancyError(
                        "structural view dependency cycle",
                        code="INDEX_BUILD_REENTRANCY",
                    )
                self._waits += 1
                if token is not None:
                    token.check()
                self._condition.wait(timeout=0.05)
        budget = IndexBuildBudget(self, self._limits, token)
        started = time.monotonic()
        try:
            if token is not None:
                token.check()
            builder = getattr(factory, "_build", None)
            if not callable(builder):
                raise TypeError("view factory must implement classmethod _build")
            created = cast(V, builder(ontology, options, budget, token, started))
            if budget.bytes == 0:
                budget.add("object", rows=0, bytes_=256)
            if token is not None:
                token.check()
        except BaseException:
            budget._release()
            with self._condition:
                self._failures += 1
                self._inflight.pop(key, None)
                self._condition.notify_all()
            raise
        with self._condition:
            self._reserved -= budget.bytes
            try:
                value = weakref.ref(cast(object, created))
            except TypeError as error:
                self._inflight.pop(key, None)
                self._condition.notify_all()
                raise TypeError("structural views must support weak references") from error
            self._entries[key] = _CacheEntry(value, budget.bytes)
            self._entries.move_to_end(key)
            if self._policy.retention is CacheRetention.STRONG:
                self._strong[key] = cast(object, created)
                self._strong.move_to_end(key)
                self._bytes += budget.bytes
            self._builds += 1
            self._inflight.pop(key, None)
            self._condition.notify_all()
            return created

    def _reserve(self, amount: int) -> None:
        with self._condition:
            self._discard_dead_weak()
            maximum = self.maximum_bytes
            required = self._bytes + self._reserved + amount
            if required > maximum:
                self._evict_until(amount)
                required = self._bytes + self._reserved + amount
            if required > maximum:
                raise ResourceLimitError(
                    "resource limit max_index_bytes exceeded",
                    limit="max_index_bytes",
                    observed=required,
                    allowed=maximum,
                )
            self._reserved += amount

    def _release_reservation(self, amount: int) -> None:
        with self._condition:
            self._reserved -= amount
            if self._reserved < 0:
                raise AssertionError("negative index cache reservation")

    def _evict_until(self, incoming: int) -> None:
        maximum = self.maximum_bytes
        while self._strong and self._bytes + self._reserved + incoming > maximum:
            key, _value = self._strong.popitem(last=False)
            entry = self._entries.get(key)
            if entry is not None:
                self._bytes -= entry.bytes
            self._evictions += 1

    def _discard_dead_weak(self) -> None:
        for key, entry in tuple(self._entries.items()):
            if entry.get() is None:
                self._remove_entry(key)

    def _remove_entry(self, key: tuple[object, ...]) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None and self._strong.pop(key, None) is not None:
            self._bytes -= entry.bytes


def create_index_cache(limits: ParseLimits) -> IndexCache:
    return IndexCache(limits)


def _cache_for(ontology: object) -> IndexCache:
    cache = getattr(ontology, "_index_cache", None)
    if not isinstance(cache, IndexCache):
        raise LookupError("ontology implementation does not provide structural view caching")
    return cache


def request_index_view(
    ontology: object,
    view_type: type[V],
    options: Mapping[str, object],
) -> V:
    if not isinstance(view_type, type):
        raise TypeError("view_type must be a type")
    values = dict(options)
    token = values.pop("cancellation_token", None)
    if token is not None and not isinstance(token, CancellationToken):
        raise TypeError("cancellation_token must be CancellationToken or None")
    policy = values.pop("cache_policy", None)
    if policy is not None and not isinstance(policy, IndexCachePolicy):
        raise TypeError("cache_policy must be IndexCachePolicy")
    options_type = getattr(view_type, "OPTIONS_TYPE", None)
    if not isinstance(options_type, type):
        raise LookupError(f"view type {view_type.__name__} is not a structural view factory")
    schema_name = getattr(view_type, "SCHEMA_NAME", None)
    if (
        isinstance(schema_name, str)
        and schema_name.startswith("pyowl-core/")
        and not view_type.__module__.startswith("pyowl_core.index.")
    ):
        raise ValueError("third-party view factories cannot claim a built-in schema name")
    allowed = {item.name for item in fields(options_type)}
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise TypeError(f"unknown {view_type.__name__} option(s): {', '.join(unknown)}")
    canonical_options = options_type(**values)
    cache = _cache_for(ontology)
    if policy is not None:
        cache.configure(policy)
    return cache.get_or_build(ontology, view_type, canonical_options, token)


def configure_index_cache(ontology: object, policy: IndexCachePolicy) -> None:
    _cache_for(ontology).configure(policy)


def clear_index_cache(ontology: object) -> None:
    _cache_for(ontology).clear()


def index_cache_report(ontology: object) -> IndexCacheReport:
    return _cache_for(ontology).report()


def build_report(
    factory: type[object],
    strategy: ViewBuildStrategy,
    budget: IndexBuildBudget,
    started: float,
    *,
    shared_bytes: int = 0,
) -> ViewBuildReport:
    if budget.bytes == 0:
        budget.add("object", rows=0, bytes_=256)
    selected = cast(type[StructuralViewFactory], factory)
    return ViewBuildReport(
        selected.SCHEMA_NAME,
        selected.SCHEMA_VERSION,
        strategy,
        budget.rows,
        budget.shared_rows,
        budget.bytes,
        shared_bytes,
        time.monotonic() - started,
        budget.tables,
    )


__all__ = [
    "CacheRetention",
    "IndexBuildBudget",
    "IndexCachePolicy",
    "IndexCacheReport",
    "StructuralViewFactory",
    "ViewBuildReport",
    "ViewBuildStrategy",
    "clear_index_cache",
    "configure_index_cache",
    "create_index_cache",
    "index_cache_report",
]
