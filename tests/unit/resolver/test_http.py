from __future__ import annotations

import gzip
import hashlib

import pytest

from pyowl_core import (
    IRI,
    AccessDeniedError,
    HttpAcquisitionCache,
    HttpResolver,
    ImportCycleError,
    ImportRequest,
    IntegrityError,
    ParseLimits,
    ResolutionKind,
    ResolutionMode,
)


class _Response:
    def __init__(
        self,
        status: int,
        data: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._status = status
        self._data = data
        self._offset = 0
        self.headers = headers or {}

    def getcode(self) -> int:
        return self._status

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            amount = len(self._data)
        result = self._data[self._offset : self._offset + amount]
        self._offset += len(result)
        return result

    def close(self) -> None:
        return None


class _Opener:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.calls = 0

    def open(self, request: object, timeout: float) -> _Response:
        del request, timeout
        self.calls += 1
        return self.responses.pop(0)


def request(
    value: str = "https://example.test/value.owl", limits: ParseLimits | None = None
) -> ImportRequest:
    iri = IRI(value)
    return ImportRequest(iri, None, (iri,), limits or ParseLimits())


def resolver(**options: object) -> HttpResolver:
    return HttpResolver(
        allowed_hosts=("example.test",),
        allow_private_networks=True,
        **options,
    )


def test_http_success_integrity_cache_and_offline_reuse() -> None:
    data = b"Ontology(<urn:http>)"
    digest = hashlib.sha256(data).digest()
    cache = HttpAcquisitionCache()
    selected = resolver(integrity={"https://example.test/value.owl": digest}, cache=cache)
    opener = _Opener([_Response(200, data, {"Content-Type": "text/owl-functional"})])
    selected._opener = opener  # type: ignore[attr-defined]

    online = selected.resolve_outcome(request(), mode=ResolutionMode.NETWORK)
    offline = selected.resolve_outcome(request(), mode=ResolutionMode.OFFLINE_CACHE)

    assert online.kind is ResolutionKind.RESOLVED
    assert offline.kind is ResolutionKind.RESOLVED
    assert online.resolved is not None and online.resolved.source == data
    assert opener.calls == 1


def test_http_redirect_cycle_and_host_revalidation() -> None:
    selected = resolver()
    selected._opener = _Opener(  # type: ignore[attr-defined]
        [
            _Response(302, headers={"Location": "https://example.test/b"}),
            _Response(302, headers={"Location": "https://example.test/value.owl"}),
        ]
    )
    with pytest.raises(ImportCycleError) as caught:
        selected.resolve(request())
    assert caught.value.code == "HTTP_REDIRECT_CYCLE"

    denied = resolver()
    with pytest.raises(AccessDeniedError) as host_error:
        denied.resolve(request("https://metadata.invalid/value.owl"))
    assert host_error.value.code == "HTTP_HOST_DENIED"


def test_http_digest_decompression_and_size_fail_closed() -> None:
    data = b"Ontology(<urn:compressed>)"
    selected = resolver(integrity={"https://example.test/value.owl": b"x" * 32})
    selected._opener = _Opener([_Response(200, data)])  # type: ignore[attr-defined]
    with pytest.raises(IntegrityError):
        selected.resolve(request())

    compressed = gzip.compress(data)
    expanded = resolver()
    expanded._opener = _Opener(  # type: ignore[attr-defined]
        [_Response(200, compressed, {"Content-Encoding": "gzip"})]
    )
    result = expanded.resolve(request())
    assert result is not None and result.source == data

    oversized = resolver()
    oversized._opener = _Opener([_Response(200, b"12345")])  # type: ignore[attr-defined]
    with pytest.raises(Exception) as limit:
        oversized.resolve(request(limits=ParseLimits(max_source_bytes=4)))
    assert getattr(limit.value, "code", None) == "RESOURCE_LIMIT"
