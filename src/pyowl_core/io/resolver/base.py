"""Public import-resolution contracts and normalized resolver outcomes."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import BinaryIO, Protocol, TypeAlias, runtime_checkable

from pyowl_core._immutable import FrozenMap, freeze_mapping
from pyowl_core.config import DocumentFormat
from pyowl_core.exceptions import (
    AccessDeniedError,
    ImportCycleError,
    ImportResolutionError,
    IntegrityError,
)
from pyowl_core.limits import ParseLimits
from pyowl_core.model import IRI, encode_varint

ResolvedSource: TypeAlias = bytes | BinaryIO | str | os.PathLike[str]


@dataclass(frozen=True, slots=True)
class ImportRequest:
    """One direct import request made by the closure loader."""

    import_iri: IRI
    importing_document_iri: IRI | None
    chain: tuple[IRI, ...]
    limits: ParseLimits

    def __post_init__(self) -> None:
        if not isinstance(self.import_iri, IRI):
            raise TypeError("import_iri must be IRI")
        if self.importing_document_iri is not None and not isinstance(
            self.importing_document_iri, IRI
        ):
            raise TypeError("importing_document_iri must be IRI or None")
        chain = tuple(self.chain)
        if not all(isinstance(item, IRI) for item in chain):
            raise TypeError("chain must contain IRI values")
        if not isinstance(self.limits, ParseLimits):
            raise TypeError("limits must be ParseLimits")
        object.__setattr__(self, "chain", chain)


@dataclass(frozen=True, slots=True)
class ResolvedDocument:
    """Acquired-document description returned by a resolver, never parsed here."""

    source: ResolvedSource
    document_iri: IRI
    format: DocumentFormat | None = None
    expected_sha256: bytes | None = None
    provenance: Mapping[str, str] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        if not isinstance(self.source, (bytes, str, os.PathLike)) and not callable(
            getattr(self.source, "read", None)
        ):
            raise TypeError("source must be bytes, a path, or BinaryIO")
        if not isinstance(self.document_iri, IRI):
            raise TypeError("document_iri must be IRI")
        if self.format is not None and not isinstance(self.format, DocumentFormat):
            raise TypeError("format must be DocumentFormat or None")
        if self.expected_sha256 is not None and (
            not isinstance(self.expected_sha256, bytes) or len(self.expected_sha256) != 32
        ):
            raise ValueError("expected_sha256 must be exactly 32 bytes or None")
        clean: dict[str, str] = {}
        for key, value in self.provenance.items():
            if not isinstance(key, str) or not key or not isinstance(value, str):
                raise TypeError("provenance must map nonempty strings to strings")
            clean[key] = value
        object.__setattr__(self, "provenance", freeze_mapping(clean))


@runtime_checkable
class ImportResolver(Protocol):
    """Trusted, synchronous resolver extension point frozen by the public API."""

    def resolve(self, request: ImportRequest) -> ResolvedDocument | None: ...


class ResolutionKind(str, Enum):
    RESOLVED = "resolved"
    NOT_FOUND = "not_found"
    DENIED = "denied"
    TIMEOUT = "timeout"
    INTEGRITY = "integrity"
    MALFORMED = "malformed"
    FAILED = "failed"


class ResolutionMode(str, Enum):
    """Loader-selected resolver capability boundary."""

    LOCAL_ONLY = "local_only"
    OFFLINE_CACHE = "offline_cache"
    NETWORK = "network"


@dataclass(frozen=True, slots=True, order=True)
class ResolutionAttempt:
    resolver_name: str
    kind: ResolutionKind
    detail_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.resolver_name, str) or not self.resolver_name:
            raise ValueError("resolver_name must be a nonempty string")
        if not isinstance(self.kind, ResolutionKind):
            raise TypeError("kind must be ResolutionKind")
        if self.detail_code is not None and (
            not isinstance(self.detail_code, str) or not self.detail_code
        ):
            raise ValueError("detail_code must be a nonempty string or None")


@dataclass(frozen=True, slots=True)
class ResolverOutcome:
    kind: ResolutionKind
    resolver_name: str
    resolved: ResolvedDocument | None = None
    attempts: tuple[ResolutionAttempt, ...] = ()
    error: ImportResolutionError | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ResolutionKind):
            raise TypeError("kind must be ResolutionKind")
        if not isinstance(self.resolver_name, str) or not self.resolver_name:
            raise ValueError("resolver_name must be a nonempty string")
        attempts = tuple(self.attempts)
        if not all(isinstance(item, ResolutionAttempt) for item in attempts):
            raise TypeError("attempts must contain ResolutionAttempt values")
        if self.kind is ResolutionKind.RESOLVED and self.resolved is None:
            raise ValueError("a resolved outcome requires ResolvedDocument")
        if self.kind is not ResolutionKind.RESOLVED and self.resolved is not None:
            raise ValueError("only a resolved outcome may contain ResolvedDocument")
        if self.resolved is not None and not isinstance(self.resolved, ResolvedDocument):
            raise TypeError("resolved must be ResolvedDocument or None")
        if self.error is not None and not isinstance(self.error, ImportResolutionError):
            raise TypeError("error must be ImportResolutionError or None")
        object.__setattr__(self, "attempts", attempts)

    @classmethod
    def success(
        cls,
        resolver_name: str,
        resolved: ResolvedDocument,
        *,
        attempts: tuple[ResolutionAttempt, ...] = (),
    ) -> ResolverOutcome:
        trace = attempts or (ResolutionAttempt(resolver_name, ResolutionKind.RESOLVED),)
        return cls(ResolutionKind.RESOLVED, resolver_name, resolved, trace)

    @classmethod
    def missing(
        cls,
        resolver_name: str,
        *,
        attempts: tuple[ResolutionAttempt, ...] = (),
    ) -> ResolverOutcome:
        trace = attempts or (ResolutionAttempt(resolver_name, ResolutionKind.NOT_FOUND),)
        return cls(ResolutionKind.NOT_FOUND, resolver_name, attempts=trace)


class _OutcomeResolver(Protocol):
    def resolve_outcome(
        self, request: ImportRequest, *, mode: ResolutionMode
    ) -> ResolverOutcome: ...


def resolve_with_mode(
    resolver: ImportResolver,
    request: ImportRequest,
    *,
    mode: ResolutionMode,
) -> ResolverOutcome:
    """Invoke a resolver once and normalize trusted callback failures."""

    if not isinstance(request, ImportRequest):
        raise TypeError("request must be ImportRequest")
    if not isinstance(mode, ResolutionMode):
        raise TypeError("mode must be ResolutionMode")
    method = getattr(resolver, "resolve_outcome", None)
    if callable(method):
        try:
            outcome = method(request, mode=mode)
        except (MemoryError, KeyboardInterrupt, SystemExit):
            raise
        except ImportResolutionError as error:
            return _error_outcome(resolver_name(resolver), error)
        except TimeoutError as error:
            wrapped = ImportResolutionError("resolver timed out", code="IMPORT_RESOLUTION_TIMEOUT")
            wrapped.__cause__ = error
            return _error_outcome(resolver_name(resolver), wrapped)
        except Exception as error:
            wrapped = ImportResolutionError(
                "resolver callback failed", code="IMPORT_RESOLVER_FAILED"
            )
            wrapped.__cause__ = error
            return _error_outcome(resolver_name(resolver), wrapped)
        if not isinstance(outcome, ResolverOutcome):
            raise TypeError("resolve_outcome() must return ResolverOutcome")
        return outcome
    if mode is not ResolutionMode.NETWORK and bool(getattr(resolver, "network_capable", False)):
        return ResolverOutcome.missing(resolver_name(resolver))
    try:
        result = resolver.resolve(request)
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except ImportResolutionError as error:
        return _error_outcome(resolver_name(resolver), error)
    except TimeoutError as error:
        wrapped = ImportResolutionError("resolver timed out", code="IMPORT_RESOLUTION_TIMEOUT")
        wrapped.__cause__ = error
        return _error_outcome(resolver_name(resolver), wrapped)
    except Exception as error:
        wrapped = ImportResolutionError("resolver callback failed", code="IMPORT_RESOLVER_FAILED")
        wrapped.__cause__ = error
        return _error_outcome(resolver_name(resolver), wrapped)
    if result is None:
        return ResolverOutcome.missing(resolver_name(resolver))
    if not isinstance(result, ResolvedDocument):
        error = ImportResolutionError(
            "resolver returned an unsupported value", code="IMPORT_RESOLVER_PROTOCOL"
        )
        return _error_outcome(resolver_name(resolver), error)
    return ResolverOutcome.success(resolver_name(resolver), result)


def resolver_name(resolver: object) -> str:
    name = getattr(resolver, "name", None)
    if isinstance(name, str) and name:
        return name
    return type(resolver).__name__


def resolver_configuration_fingerprint(resolver: ImportResolver | None) -> bytes:
    """Return a credential-free SHA-256 configuration identity."""

    domain = b"pyowl-core:resolver-configuration:v1\x00"
    if resolver is None:
        payload = b"none"
    else:
        method = getattr(resolver, "configuration_bytes", None)
        if callable(method):
            raw = method()
            if not isinstance(raw, bytes):
                raise TypeError("configuration_bytes() must return bytes")
            payload = raw
        else:
            qualified = f"{type(resolver).__module__}.{type(resolver).__qualname__}"
            payload = qualified.encode("utf-8")
    return hashlib.sha256(domain + encode_varint(len(payload)) + payload).digest()


def framed_text(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return encode_varint(len(encoded)) + encoded


def _error_outcome(name: str, error: ImportResolutionError) -> ResolverOutcome:
    if isinstance(error, AccessDeniedError):
        kind = ResolutionKind.DENIED
    elif isinstance(error, IntegrityError):
        kind = ResolutionKind.INTEGRITY
    elif isinstance(error, ImportCycleError):
        kind = ResolutionKind.FAILED
    elif error.code == "IMPORT_RESOLUTION_TIMEOUT":
        kind = ResolutionKind.TIMEOUT
    else:
        kind = ResolutionKind.FAILED
    return ResolverOutcome(
        kind,
        name,
        attempts=(ResolutionAttempt(name, kind, error.code),),
        error=error,
    )


__all__ = [
    "ImportRequest",
    "ImportResolver",
    "ResolutionAttempt",
    "ResolutionKind",
    "ResolutionMode",
    "ResolvedDocument",
    "ResolvedSource",
    "ResolverOutcome",
    "resolver_configuration_fingerprint",
]
