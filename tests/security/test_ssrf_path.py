from __future__ import annotations

import gzip
import socket
from pathlib import Path
from typing import Any, cast

import pytest

from pyowl_core import (
    IRI,
    AccessDeniedError,
    BackendPreference,
    DirectoryResolver,
    HttpAcquisitionCache,
    HttpResolver,
    ImportPolicy,
    ImportRequest,
    LoadOptions,
    ParseLimits,
    ResolutionKind,
    ResolutionMode,
    UnresolvedImportError,
    load_snapshot,
    parse_document,
)


class _Response:
    def __init__(self, data: bytes, headers: dict[str, str]) -> None:
        self._data = data
        self._offset = 0
        self.headers = headers

    def getcode(self) -> int:
        return 200

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            amount = len(self._data)
        result = self._data[self._offset : self._offset + amount]
        self._offset += len(result)
        return result

    def close(self) -> None:
        return None


class _Opener:
    def __init__(self, response: _Response | None = None) -> None:
        self.response = response
        self.calls = 0

    def open(self, request: object, timeout: float) -> _Response:
        del request, timeout
        self.calls += 1
        if self.response is None:
            raise AssertionError("network opener must not be called")
        return self.response


def _request(value: str, *, limits: ParseLimits | None = None) -> ImportRequest:
    iri = IRI(value)
    return ImportRequest(iri, None, (iri,), ParseLimits() if limits is None else limits)


def test_dns_rebinding_to_metadata_address_is_denied_before_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = HttpResolver(allowed_hosts=("metadata.example",))
    opener = _Opener()
    cast(Any, resolver)._opener = opener

    def metadata_address(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]

    monkeypatch.setattr("pyowl_core.io.resolver.http.socket.getaddrinfo", metadata_address)
    with pytest.raises(AccessDeniedError) as caught:
        resolver.resolve(_request("https://metadata.example/latest/meta-data"))
    assert caught.value.code == "HTTP_PRIVATE_ADDRESS"
    assert opener.calls == 0


def test_default_offline_load_never_calls_http_opener() -> None:
    source = b"Ontology(<urn:root> Import(<https://example.test/imported.owl>))"
    resolver = HttpResolver(
        allowed_hosts=("example.test",),
        allow_private_networks=True,
    )
    opener = _Opener()
    cast(Any, resolver)._opener = opener
    with pytest.raises(UnresolvedImportError):
        load_snapshot(
            source,
            options=LoadOptions(
                imports=ImportPolicy.RESOLVE_LOCAL,
                backend=BackendPreference.PYTHON,
            ),
            resolver=resolver,
        )
    assert opener.calls == 0


def test_decompression_ratio_failure_does_not_publish_cache() -> None:
    iri = "https://example.test/bomb.owl"
    compressed = gzip.compress(b"A" * 20_000)
    cache = HttpAcquisitionCache()
    resolver = HttpResolver(
        allowed_hosts=("example.test",),
        allow_private_networks=True,
        maximum_decompression_ratio=2,
        cache=cache,
    )
    cast(Any, resolver)._opener = _Opener(
        _Response(compressed, {"Content-Encoding": "gzip"})
    )
    with pytest.raises(AccessDeniedError) as caught:
        resolver.resolve(
            _request(
                iri,
                limits=ParseLimits(
                    max_source_bytes=64 * 1024,
                    max_decompressed_bytes=64 * 1024,
                ),
            )
        )
    assert caught.value.code == "HTTP_DECOMPRESSION_RATIO"
    outcome = resolver.resolve_outcome(_request(iri), mode=ResolutionMode.OFFLINE_CACHE)
    assert outcome.kind is ResolutionKind.NOT_FOUND


def test_path_and_url_sources_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(AccessDeniedError):
        parse_document("https://example.test/ontology.owl")

    resolver = DirectoryResolver(
        tmp_path,
        strategy="relative",
        iri_prefix="https://example.test/",
    )
    for value in (
        "https://example.test/%2e%2e/secret.owl",
        "https://example.test/%2fetc/passwd",
        "https://example.test/%5cserver/share.owl",
    ):
        with pytest.raises(AccessDeniedError) as caught:
            resolver.resolve(_request(value))
        assert caught.value.code == "IMPORT_PATH_ESCAPE"
