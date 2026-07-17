"""Opt-in bounded HTTP resolver with explicit SSRF and integrity policy."""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import threading
import urllib.error
import urllib.request
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import urljoin, urlsplit, urlunsplit

from pyowl_core._immutable import freeze_mapping
from pyowl_core.exceptions import AccessDeniedError, ImportCycleError, IntegrityError
from pyowl_core.model import IRI, encode_varint

from .base import (
    ImportRequest,
    ResolutionAttempt,
    ResolutionKind,
    ResolutionMode,
    ResolvedDocument,
    ResolverOutcome,
    framed_text,
)


@dataclass(frozen=True, slots=True)
class HttpCacheEntry:
    request_iri: IRI
    final_iri: IRI
    data: bytes
    source_sha256: bytes
    media_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request_iri, IRI) or not isinstance(self.final_iri, IRI):
            raise TypeError("cache IRIs must be IRI values")
        if not isinstance(self.data, bytes):
            raise TypeError("cache data must be bytes")
        if not isinstance(self.source_sha256, bytes) or len(self.source_sha256) != 32:
            raise ValueError("source_sha256 must be exactly 32 bytes")
        if hashlib.sha256(self.data).digest() != self.source_sha256:
            raise IntegrityError("HTTP cache content digest mismatch", code="HTTP_CACHE_CORRUPT")


class HttpAcquisitionCache:
    """Thread-safe atomic in-memory cache; failures are never published."""

    __slots__ = ("_entries", "_lock")

    def __init__(self) -> None:
        self._entries: dict[IRI, HttpCacheEntry] = {}
        self._lock = threading.Lock()

    def get(self, iri: IRI) -> HttpCacheEntry | None:
        with self._lock:
            entry = self._entries.get(iri)
        if entry is None:
            return None
        if hashlib.sha256(entry.data).digest() != entry.source_sha256:
            raise IntegrityError("HTTP cache content digest mismatch", code="HTTP_CACHE_CORRUPT")
        return entry

    def publish(self, entry: HttpCacheEntry) -> None:
        if not isinstance(entry, HttpCacheEntry):
            raise TypeError("entry must be HttpCacheEntry")
        with self._lock:
            self._entries[entry.request_iri] = entry

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


class _HttpHeaders(Protocol):
    def get(self, name: str, default: str | None = None) -> str | None: ...


class _HttpResponse(Protocol):
    headers: _HttpHeaders

    def getcode(self) -> int | None: ...

    def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...


class _Decompressor(Protocol):
    def decompress(self, data: bytes) -> bytes: ...

    def flush(self) -> bytes: ...


class HttpResolver:
    """Acquire allowed HTTP(S) documents; never enabled implicitly."""

    __slots__ = (
        "_allow_environment_proxy",
        "_allow_private_networks",
        "_allowed_hosts",
        "_allowed_ports",
        "_allowed_schemes",
        "_cache",
        "_integrity",
        "_mandatory_integrity",
        "_maximum_ratio",
        "_opener",
        "_timeout",
    )
    name = "http"
    network_capable = True

    def __init__(
        self,
        *,
        allowed_hosts: Iterable[str],
        allowed_schemes: Iterable[str] = ("https",),
        allowed_ports: Iterable[int] | None = None,
        integrity: Mapping[IRI | str, bytes | str] | None = None,
        mandatory_integrity: bool = False,
        timeout_seconds: float = 30.0,
        maximum_decompression_ratio: int = 100,
        allow_private_networks: bool = False,
        allow_environment_proxy: bool = False,
        cache: HttpAcquisitionCache | None = None,
    ) -> None:
        hosts = frozenset(_host(item) for item in allowed_hosts)
        if not hosts:
            raise ValueError("allowed_hosts must not be empty")
        schemes = frozenset(item.lower() for item in allowed_schemes)
        if not schemes or not schemes <= {"http", "https"}:
            raise ValueError("allowed_schemes must contain only http and/or https")
        ports = None if allowed_ports is None else frozenset(allowed_ports)
        if ports is not None and (
            not ports
            or any(
                isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
                for port in ports
            )
        ):
            raise ValueError("allowed_ports must contain valid TCP ports")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        if (
            isinstance(maximum_decompression_ratio, bool)
            or not isinstance(maximum_decompression_ratio, int)
            or maximum_decompression_ratio < 1
        ):
            raise ValueError("maximum_decompression_ratio must be a positive integer")
        expected: dict[IRI, bytes] = {}
        for iri, digest in (integrity or {}).items():
            key = iri if isinstance(iri, IRI) else IRI(iri)
            expected[key] = _digest(digest)
        if not isinstance(mandatory_integrity, bool):
            raise TypeError("mandatory_integrity must be bool")
        if not isinstance(allow_private_networks, bool) or not isinstance(
            allow_environment_proxy, bool
        ):
            raise TypeError("network policy flags must be bool")
        handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
        if not allow_environment_proxy:
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)
        self._allowed_hosts = hosts
        self._allowed_schemes = schemes
        self._allowed_ports = ports
        self._integrity = freeze_mapping(expected)
        self._mandatory_integrity = mandatory_integrity
        self._timeout = float(timeout_seconds)
        self._maximum_ratio = maximum_decompression_ratio
        self._allow_private_networks = allow_private_networks
        self._allow_environment_proxy = allow_environment_proxy
        self._cache = HttpAcquisitionCache() if cache is None else cache

    def resolve(self, request: ImportRequest) -> ResolvedDocument | None:
        return self.resolve_outcome(request, mode=ResolutionMode.NETWORK).resolved

    def resolve_outcome(self, request: ImportRequest, *, mode: ResolutionMode) -> ResolverOutcome:
        cached = self._cache.get(request.import_iri)
        expected = self._integrity.get(request.import_iri)
        if mode is ResolutionMode.LOCAL_ONLY:
            return ResolverOutcome.missing(self.name)
        if mode is ResolutionMode.OFFLINE_CACHE:
            if cached is None:
                return ResolverOutcome.missing(self.name)
            request.limits.enforce("max_source_bytes", len(cached.data))
            _verify_integrity(cached.data, expected, mandatory=self._mandatory_integrity)
            return ResolverOutcome.success(self.name, _from_cache(cached, expected))
        current = request.import_iri.value
        visited: list[str] = []
        attempts: list[ResolutionAttempt] = []
        conditional: dict[str, str] = {}
        if cached is not None:
            if cached.etag:
                conditional["If-None-Match"] = cached.etag
            if cached.last_modified:
                conditional["If-Modified-Since"] = cached.last_modified
        for redirect_count in range(request.limits.max_redirects + 1):
            if current in visited:
                raise ImportCycleError("HTTP redirect cycle", code="HTTP_REDIRECT_CYCLE")
            visited.append(current)
            self._validate_url(current)
            attempts.append(ResolutionAttempt(self.name, ResolutionKind.NOT_FOUND, "HTTP_GET"))
            response = self._open(current, conditional if redirect_count == 0 else {})
            status = response.getcode()
            if status is None:
                response.close()
                raise AccessDeniedError("HTTP response has no status", code="HTTP_PROTOCOL")
            if status == 304 and cached is not None:
                request.limits.enforce("max_source_bytes", len(cached.data))
                _verify_integrity(cached.data, expected, mandatory=self._mandatory_integrity)
                attempts[-1] = ResolutionAttempt(self.name, ResolutionKind.RESOLVED, "HTTP_304")
                return ResolverOutcome.success(
                    self.name, _from_cache(cached, expected), attempts=tuple(attempts)
                )
            if status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise AccessDeniedError(
                        "HTTP redirect has no location", code="HTTP_REDIRECT_MALFORMED"
                    )
                if redirect_count >= request.limits.max_redirects:
                    raise ImportCycleError(
                        "HTTP redirect limit exceeded", code="HTTP_REDIRECT_LIMIT"
                    )
                current = urljoin(current, location)
                continue
            if status in {401, 403}:
                response.close()
                raise AccessDeniedError("HTTP import access denied", code="HTTP_ACCESS_DENIED")
            if status == 404:
                response.close()
                return ResolverOutcome.missing(self.name, attempts=tuple(attempts))
            if not 200 <= status < 300:
                response.close()
                raise AccessDeniedError(
                    "HTTP resolver rejected response status", code="HTTP_STATUS_DENIED"
                )
            media_type = response.headers.get("Content-Type")
            if media_type:
                media_type = media_type.split(";", 1)[0].strip().lower()
            encoding = (response.headers.get("Content-Encoding") or "identity").lower()
            data = _read_response(response, request, encoding, self._maximum_ratio)
            response.close()
            _verify_integrity(data, expected, mandatory=self._mandatory_integrity)
            final = IRI(current)
            entry = HttpCacheEntry(
                request.import_iri,
                final,
                data,
                hashlib.sha256(data).digest(),
                media_type,
                response.headers.get("ETag"),
                response.headers.get("Last-Modified"),
            )
            self._cache.publish(entry)
            attempts[-1] = ResolutionAttempt(self.name, ResolutionKind.RESOLVED, "HTTP_2XX")
            return ResolverOutcome.success(
                self.name, _from_cache(entry, expected), attempts=tuple(attempts)
            )
        raise ImportCycleError("HTTP redirect limit exceeded", code="HTTP_REDIRECT_LIMIT")

    def configuration_bytes(self) -> bytes:
        pieces = [
            b"http:v1",
            encode_varint(len(self._allowed_schemes)),
            *(framed_text(item) for item in sorted(self._allowed_schemes)),
            encode_varint(len(self._allowed_hosts)),
            *(framed_text(item) for item in sorted(self._allowed_hosts)),
        ]
        ports = () if self._allowed_ports is None else tuple(sorted(self._allowed_ports))
        pieces.append(encode_varint(len(ports)))
        pieces.extend(port.to_bytes(2, "big") for port in ports)
        pieces.extend(
            (
                bytes((int(self._mandatory_integrity), int(self._allow_private_networks))),
                encode_varint(self._maximum_ratio),
                encode_varint(len(self._integrity)),
            )
        )
        for iri, digest in self._integrity.items():
            pieces.extend((framed_text(iri.value), digest))
        # Timeout/proxy affect acquisition policy; no header or credential is retained.
        pieces.extend(
            (
                framed_text(format(self._timeout, ".17g")),
                bytes((int(self._allow_environment_proxy),)),
            )
        )
        return b"".join(pieces)

    def _validate_url(self, value: str) -> None:
        split = urlsplit(value)
        scheme = split.scheme.lower()
        if scheme not in self._allowed_schemes:
            raise AccessDeniedError("HTTP scheme is not allowlisted", code="HTTP_SCHEME_DENIED")
        if split.username is not None or split.password is not None:
            raise AccessDeniedError(
                "credentials in import URLs are forbidden", code="HTTP_CREDENTIALS"
            )
        host = _host(split.hostname or "")
        if host not in self._allowed_hosts:
            raise AccessDeniedError("HTTP host is not allowlisted", code="HTTP_HOST_DENIED")
        port = split.port or (443 if scheme == "https" else 80)
        if self._allowed_ports is not None and port not in self._allowed_ports:
            raise AccessDeniedError("HTTP port is not allowlisted", code="HTTP_PORT_DENIED")
        if not self._allow_private_networks:
            try:
                addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except OSError as error:
                raise AccessDeniedError(
                    "HTTP host lookup failed", code="HTTP_DNS_FAILED"
                ) from error
            if not addresses:
                raise AccessDeniedError("HTTP host has no addresses", code="HTTP_DNS_FAILED")
            for address in addresses:
                ip = ipaddress.ip_address(address[4][0])
                if not ip.is_global:
                    raise AccessDeniedError(
                        "HTTP host resolved to a non-global address", code="HTTP_PRIVATE_ADDRESS"
                    )

    def _open(self, value: str, conditional: Mapping[str, str]) -> _HttpResponse:
        headers = {"Accept-Encoding": "gzip, deflate, identity", **conditional}
        request = urllib.request.Request(value, headers=headers, method="GET")
        try:
            return cast(_HttpResponse, self._opener.open(request, timeout=self._timeout))
        except urllib.error.HTTPError as error:
            # HTTPError is also a readable response and is needed for redirects/304.
            return cast(_HttpResponse, error)
        except TimeoutError as error:
            raise TimeoutError("HTTP import timed out") from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, (socket.timeout, TimeoutError)):
                raise TimeoutError("HTTP import timed out") from error
            raise AccessDeniedError(
                "HTTP acquisition failed", code="HTTP_ACQUISITION_FAILED"
            ) from error


def _read_response(
    response: _HttpResponse,
    request: ImportRequest,
    encoding: str,
    maximum_ratio: int,
) -> bytes:
    if encoding not in {"identity", "gzip", "x-gzip", "deflate"}:
        raise AccessDeniedError("unsupported HTTP content encoding", code="HTTP_ENCODING_DENIED")
    decompressor: _Decompressor | None
    if encoding in {"gzip", "x-gzip"}:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        decompressor = zlib.decompressobj()
    else:
        decompressor = None
    compressed = 0
    expanded = 0
    chunks: list[bytes] = []
    while True:
        chunk = response.read(64 * 1024)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise AccessDeniedError("HTTP response returned non-bytes", code="HTTP_PROTOCOL")
        compressed += len(chunk)
        request.limits.enforce("max_source_bytes", compressed)
        try:
            output = chunk if decompressor is None else decompressor.decompress(chunk)
        except zlib.error as error:
            raise AccessDeniedError(
                "malformed compressed HTTP response", code="HTTP_COMPRESSION"
            ) from error
        expanded += len(output)
        request.limits.enforce("max_decompressed_bytes", expanded)
        request.limits.enforce("max_source_bytes", expanded)
        if compressed and expanded > compressed * maximum_ratio:
            raise AccessDeniedError(
                "HTTP decompression ratio exceeded", code="HTTP_DECOMPRESSION_RATIO"
            )
        chunks.append(output)
    if decompressor is not None:
        try:
            tail = decompressor.flush()
        except zlib.error as error:
            raise AccessDeniedError(
                "malformed compressed HTTP response", code="HTTP_COMPRESSION"
            ) from error
        expanded += len(tail)
        request.limits.enforce("max_decompressed_bytes", expanded)
        request.limits.enforce("max_source_bytes", expanded)
        if compressed and expanded > compressed * maximum_ratio:
            raise AccessDeniedError(
                "HTTP decompression ratio exceeded", code="HTTP_DECOMPRESSION_RATIO"
            )
        chunks.append(tail)
    data = b"".join(chunks)
    request.limits.enforce("max_source_bytes", len(data))
    return data


def _from_cache(entry: HttpCacheEntry, expected: bytes | None) -> ResolvedDocument:
    provenance = {
        "resolver": "http",
        "media_type": entry.media_type or "",
        "final_locator": _sanitize_url(entry.final_iri.value),
        "cache": "hit",
    }
    return ResolvedDocument(
        entry.data,
        entry.final_iri,
        expected_sha256=expected or entry.source_sha256,
        provenance=provenance,
    )


def _verify_integrity(data: bytes, expected: bytes | None, *, mandatory: bool) -> None:
    if mandatory and expected is None:
        raise IntegrityError(
            "HTTP import requires a configured digest", code="HTTP_DIGEST_REQUIRED"
        )
    if expected is not None and hashlib.sha256(data).digest() != expected:
        raise IntegrityError("HTTP import digest mismatch", code="IMPORT_DIGEST_MISMATCH")


def _digest(value: bytes | str) -> bytes:
    if isinstance(value, str):
        try:
            result = bytes.fromhex(value)
        except ValueError as error:
            raise ValueError("integrity digests must be hex SHA-256") from error
    elif isinstance(value, bytes):
        result = value
    else:
        raise TypeError("integrity digests must be bytes or hexadecimal strings")
    if len(result) != 32:
        raise ValueError("integrity digests must be exactly 32 bytes")
    return result


def _host(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("host names must be nonempty strings")
    return value.rstrip(".").encode("idna").decode("ascii").lower()


def _sanitize_url(value: str) -> str:
    split = urlsplit(value)
    host = split.hostname or ""
    port = "" if split.port is None else f":{split.port}"
    return urlunsplit((split.scheme, host + port, split.path, "", ""))


__all__ = ["HttpAcquisitionCache", "HttpCacheEntry", "HttpResolver"]
