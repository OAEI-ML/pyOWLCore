"""Reusable zero-reparse conformance probes for ontology consumers."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import NoReturn, TypeAlias

from pyowl_core.diagnostics import Diagnostic, Severity, validate_diagnostic_code
from pyowl_core.document.document import Fingerprint
from pyowl_core.document.snapshot import CoreCapabilities, OntologyView
from pyowl_core.exceptions import AdapterCompatibilityError
from pyowl_core.index import OntologyIdentityIndex
from pyowl_core.model import canonical_bytes

from .cache import CacheKeyReport, ConsumerCacheKey, compare_cache_keys
from .compatibility import AdapterRequirement, NegotiationReport, negotiate_view


@dataclass(frozen=True, slots=True)
class OperationCounts:
    """Small deterministic call ledger used by consumer integration fixtures."""

    parser: int = 0
    resolver: int = 0
    wire_encode: int = 0
    wire_decode: int = 0
    mmap_open: int = 0
    path_access: int = 0

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")

    def __sub__(self, other: OperationCounts) -> OperationCounts:
        if not isinstance(other, OperationCounts):
            return NotImplemented
        values = {
            name: getattr(self, name) - getattr(other, name) for name in self.__dataclass_fields__
        }
        if any(value < 0 for value in values.values()):
            raise ValueError("operation counter values cannot move backwards")
        return OperationCounts(**values)

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


class OperationCounters:
    """Thread-safe mutable owner for immutable :class:`OperationCounts` snapshots."""

    __slots__ = ("_counts", "_lock")

    _NAMES = frozenset(OperationCounts.__dataclass_fields__)

    def __init__(self) -> None:
        self._counts = {name: 0 for name in self._NAMES}
        self._lock = threading.Lock()

    def increment(self, name: str, amount: int = 1) -> None:
        if name not in self._NAMES:
            raise ValueError(f"unknown operation counter {name!r}")
        if type(amount) is not int or amount < 1:
            raise ValueError("amount must be a positive integer")
        with self._lock:
            self._counts[name] += amount

    def snapshot(self) -> OperationCounts:
        with self._lock:
            return OperationCounts(**self._counts)


class SnapshotProviderProbe:
    """One-view provider that turns source recovery into a typed failure."""

    __slots__ = ("_lock", "_provider_calls", "_source_accesses", "snapshot")

    def __init__(self, snapshot: OntologyView) -> None:
        self.snapshot = snapshot
        self._lock = threading.Lock()
        self._provider_calls = 0
        self._source_accesses = 0

    @property
    def provider_calls(self) -> int:
        with self._lock:
            return self._provider_calls

    @property
    def source_accesses(self) -> int:
        with self._lock:
            return self._source_accesses

    def owl_snapshot(self) -> OntologyView:
        with self._lock:
            self._provider_calls += 1
        return self.snapshot

    def __fspath__(self) -> str:
        self._reject_source_access("path")

    def read(self, *_args: object, **_kwargs: object) -> bytes:
        self._reject_source_access("stream")

    @property
    def origin(self) -> object:
        self._reject_source_access("origin")

    def _reject_source_access(self, kind: str) -> NoReturn:
        with self._lock:
            self._source_accesses += 1
        message = f"consumer attempted {kind} recovery after a shared view was supplied"
        diagnostic = Diagnostic(
            code="ADAPTER_CONFORMANCE_SOURCE_ACCESS",
            severity=Severity.ERROR,
            message=message,
            details={"access": kind},
        )
        raise AdapterCompatibilityError(message, diagnostic=diagnostic)


@dataclass(frozen=True, slots=True)
class ViewContract:
    """Canonical public observation before and after consumer handoff."""

    capabilities: CoreCapabilities
    structural_fingerprint: Fingerprint
    logical_fingerprint: Fingerprint
    signature_fingerprint: Fingerprint
    axiom_count: int
    axiom_sha256: bytes
    signature_count: int
    signature_sha256: bytes
    document_keys: tuple[str, ...]
    import_manifest_sha256: bytes
    loader_diagnostics_sha256: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.capabilities, CoreCapabilities):
            raise TypeError("capabilities must be CoreCapabilities")
        for name in (
            "structural_fingerprint",
            "logical_fingerprint",
            "signature_fingerprint",
        ):
            if not isinstance(getattr(self, name), Fingerprint):
                raise TypeError(f"{name} must be Fingerprint")
        for name in ("axiom_count", "signature_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        for name in (
            "axiom_sha256",
            "signature_sha256",
            "import_manifest_sha256",
            "loader_diagnostics_sha256",
        ):
            value = getattr(self, name)
            if not isinstance(value, bytes) or len(value) != 32:
                raise ValueError(f"{name} must be exactly 32 bytes")
        if not all(isinstance(item, str) and item for item in self.document_keys):
            raise TypeError("document_keys must contain nonempty strings")


def capture_view_contract(view: OntologyView) -> ViewContract:
    """Observe only public immutable structure and the public identity index."""

    axioms = tuple(sorted(canonical_bytes(item) for item in view.iter_axioms()))
    signature = tuple(sorted(canonical_bytes(item) for item in view.signature()))
    identity = view.view(OntologyIdentityIndex)
    return ViewContract(
        capabilities=view.capabilities,
        structural_fingerprint=view.structural_fingerprint,
        logical_fingerprint=view.logical_fingerprint,
        signature_fingerprint=view.signature_fingerprint,
        axiom_count=len(axioms),
        axiom_sha256=_rows_digest(b"axioms", axioms),
        signature_count=len(signature),
        signature_sha256=_rows_digest(b"signature", signature),
        document_keys=identity.document_keys,
        import_manifest_sha256=identity.import_manifest_digest,
        loader_diagnostics_sha256=identity.loader_diagnostics_digest,
    )


class UnsupportedDisposition(str, Enum):
    UNSUPPORTED = "unsupported"
    PARTIAL = "partial"
    IGNORED = "ignored"
    NONLOGICAL = "nonlogical"


@dataclass(frozen=True, slots=True, order=True)
class UnsupportedFeature:
    """One stable, counted consumer decision for a structural constructor."""

    code: str
    constructor: str
    disposition: UnsupportedDisposition
    count: int = 1

    def __post_init__(self) -> None:
        validate_diagnostic_code(self.code)
        if not isinstance(self.constructor, str) or not self.constructor:
            raise ValueError("constructor must be a nonempty string")
        if not isinstance(self.disposition, UnsupportedDisposition):
            raise TypeError("disposition must be UnsupportedDisposition")
        if type(self.count) is not int or self.count < 1:
            raise ValueError("count must be a positive integer")

    def to_dict(self) -> dict[str, str | int]:
        return {
            "code": self.code,
            "constructor": self.constructor,
            "disposition": self.disposition.value,
            "count": self.count,
        }


@dataclass(frozen=True, slots=True)
class UnsupportedFeatureReport:
    """Canonical exhaustive unsupported/partial/ignored constructor report."""

    features: tuple[UnsupportedFeature, ...] = ()

    def __post_init__(self) -> None:
        features = tuple(sorted(self.features))
        if not all(isinstance(item, UnsupportedFeature) for item in features):
            raise TypeError("features must contain UnsupportedFeature values")
        keys = tuple((item.code, item.constructor, item.disposition) for item in features)
        if len(keys) != len(set(keys)):
            raise ValueError("unsupported feature report contains duplicate entries")
        object.__setattr__(self, "features", features)

    @property
    def digest(self) -> bytes:
        payload = json.dumps(
            [item.to_dict() for item in self.features],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(b"pyowl-core/unsupported-report/v1\0" + payload).digest()

    def to_dict(self) -> dict[str, object]:
        return {
            "features": [item.to_dict() for item in self.features],
            "sha256": self.digest.hex(),
        }


@dataclass(frozen=True, slots=True)
class ConsumerObservation:
    """Minimal adapter-owned values returned to the reusable conformance kit."""

    view: OntologyView
    result_sha256: bytes
    cache_key: ConsumerCacheKey
    unsupported: UnsupportedFeatureReport

    def __post_init__(self) -> None:
        if not isinstance(self.result_sha256, bytes) or len(self.result_sha256) != 32:
            raise ValueError("result_sha256 must be exactly 32 bytes")
        if not isinstance(self.cache_key, ConsumerCacheKey):
            raise TypeError("cache_key must be ConsumerCacheKey")
        if not isinstance(self.unsupported, UnsupportedFeatureReport):
            raise TypeError("unsupported must be UnsupportedFeatureReport")


@dataclass(frozen=True, slots=True)
class HandoffReport:
    """Successful identity/counter/cache/unsupported conformance evidence."""

    before: ViewContract
    after: ViewContract
    negotiation: NegotiationReport
    cache: CacheKeyReport
    unsupported: UnsupportedFeatureReport
    operation_delta: OperationCounts
    provider_calls: int
    source_accesses: int
    result_sha256: bytes

    @property
    def passed(self) -> bool:
        return (
            self.before == self.after
            and self.negotiation.compatible
            and self.cache.compatible
            and self.operation_delta == OperationCounts()
            and self.provider_calls == 1
            and self.source_accesses == 0
        )


ConsumerAdapter: TypeAlias = Callable[[object], ConsumerObservation]
_EMPTY_UNSUPPORTED_REPORT = UnsupportedFeatureReport()


def verify_consumer_handoff(
    view: OntologyView,
    adapter: ConsumerAdapter,
    *,
    requirement: AdapterRequirement,
    expected_cache_key: ConsumerCacheKey,
    expected_unsupported: UnsupportedFeatureReport = _EMPTY_UNSUPPORTED_REPORT,
    counters: OperationCounters | None = None,
) -> HandoffReport:
    """Verify one provider handoff with zero parse/resolver/wire/path work."""

    if not callable(adapter):
        raise TypeError("adapter must be callable")
    if not isinstance(expected_unsupported, UnsupportedFeatureReport):
        raise TypeError("expected_unsupported must be UnsupportedFeatureReport")
    selected_counters = OperationCounters() if counters is None else counters
    if not isinstance(selected_counters, OperationCounters):
        raise TypeError("counters must be OperationCounters or None")
    before_counts = selected_counters.snapshot()
    before = capture_view_contract(view)
    probe = SnapshotProviderProbe(view)
    observation = adapter(probe)
    if not isinstance(observation, ConsumerObservation):
        _fail("adapter did not return ConsumerObservation", field="observation")
    if observation.view is not view:
        _fail("consumer did not retain the exact supplied view identity", field="view_identity")
    negotiation = negotiate_view(observation.view, requirement)
    negotiation.raise_for_errors()
    cache = compare_cache_keys(observation.cache_key, expected_cache_key)
    cache.raise_for_errors()
    if observation.unsupported != expected_unsupported:
        _unsupported_failure(observation.unsupported, expected_unsupported)
    after = capture_view_contract(view)
    if after != before:
        _fail("consumer changed the shared public view contract", field="view_contract")
    delta = selected_counters.snapshot() - before_counts
    if delta != OperationCounts():
        _fail(
            "in-process consumer handoff performed forbidden core work",
            field="operation_counters",
            expected=json.dumps(OperationCounts().to_dict(), sort_keys=True),
            actual=json.dumps(delta.to_dict(), sort_keys=True),
        )
    if probe.provider_calls != 1:
        _fail(
            "consumer must call SnapshotProvider.owl_snapshot() exactly once",
            field="provider_calls",
            expected="1",
            actual=str(probe.provider_calls),
        )
    if probe.source_accesses:
        _fail(
            "consumer accessed a source after provider handoff",
            field="source_accesses",
            expected="0",
            actual=str(probe.source_accesses),
        )
    report = HandoffReport(
        before=before,
        after=after,
        negotiation=negotiation,
        cache=cache,
        unsupported=observation.unsupported,
        operation_delta=delta,
        provider_calls=probe.provider_calls,
        source_accesses=probe.source_accesses,
        result_sha256=observation.result_sha256,
    )
    if not report.passed:  # pragma: no cover - individual failures above are specific
        _fail("consumer handoff conformance failed", field="handoff")
    return report


def semantic_result_digest(rows: Iterable[bytes]) -> bytes:
    """Hash a canonical consumer result without Python hashes or object identity."""

    return _rows_digest(b"consumer-result", tuple(sorted(rows)))


def _rows_digest(domain: bytes, rows: Iterable[bytes]) -> bytes:
    digest = hashlib.sha256()
    digest.update(b"pyowl-core/conformance/")
    digest.update(domain)
    digest.update(b"/v1\0")
    for row in rows:
        if not isinstance(row, bytes):
            raise TypeError("canonical rows must be bytes")
        digest.update(len(row).to_bytes(8, "big"))
        digest.update(row)
    return digest.digest()


def _unsupported_failure(
    actual: UnsupportedFeatureReport,
    expected: UnsupportedFeatureReport,
) -> None:
    _fail(
        "consumer unsupported-feature report is not exhaustive",
        field="unsupported_features",
        expected=json.dumps(expected.to_dict(), sort_keys=True, separators=(",", ":")),
        actual=json.dumps(actual.to_dict(), sort_keys=True, separators=(",", ":")),
    )


def _fail(
    message: str,
    *,
    field: str,
    expected: str = "conformant",
    actual: str = "nonconformant",
) -> None:
    diagnostic = Diagnostic(
        code="ADAPTER_CONFORMANCE",
        severity=Severity.ERROR,
        message=message,
        details={"field": field, "expected": expected, "actual": actual},
    )
    raise AdapterCompatibilityError(message, diagnostic=diagnostic)


__all__ = [
    "ConsumerAdapter",
    "ConsumerObservation",
    "HandoffReport",
    "OperationCounters",
    "OperationCounts",
    "SnapshotProviderProbe",
    "UnsupportedDisposition",
    "UnsupportedFeature",
    "UnsupportedFeatureReport",
    "ViewContract",
    "capture_view_contract",
    "semantic_result_digest",
    "verify_consumer_handoff",
]
