"""Bounded, single-pass source acquisition with explicit stream ownership."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TextIO, TypeAlias

from pyowl_core.cancellation import CancellationToken
from pyowl_core.config import DocumentFormat
from pyowl_core.document.provenance import DigestKind
from pyowl_core.exceptions import AccessDeniedError, OntologySyntaxError, ResourceLimitError
from pyowl_core.limits import ParseLimits
from pyowl_core.model import IRI

DocumentSource: TypeAlias = (
    str | os.PathLike[str] | bytes | bytearray | memoryview | BinaryIO | TextIO
)


@dataclass(frozen=True, slots=True)
class SourcePayload:
    data: bytes
    source_sha256: bytes
    digest_kind: DigestKind
    byte_length: int
    decoded_codepoint_length: int | None
    locator: str | None
    extension: str | None
    is_text_stream: bool

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("data must be bytes")
        if not isinstance(self.source_sha256, bytes) or len(self.source_sha256) != 32:
            raise ValueError("source_sha256 must be exactly 32 bytes")
        if self.byte_length != len(self.data):
            raise ValueError("byte_length must equal data length")


def acquire_source(
    source: DocumentSource,
    *,
    format: DocumentFormat | None,
    document_iri: IRI | None,
    limits: ParseLimits,
    cancellation_token: CancellationToken | None = None,
) -> SourcePayload:
    """Read a source once without closing caller-owned streams."""

    if not isinstance(limits, ParseLimits):
        raise TypeError("limits must be ParseLimits")
    started = time.monotonic()
    if isinstance(source, (str, os.PathLike)):
        path = os.fspath(source)
        if not isinstance(path, str):
            raise TypeError("filesystem paths must resolve to str")
        if "://" in path:
            raise AccessDeniedError("a document source string is a path, not a URL")
        with open(path, "rb") as stream:
            mode = os.fstat(stream.fileno()).st_mode
            if not stat.S_ISREG(mode):
                raise AccessDeniedError("document path must identify a regular file")
            data, digest = _read_binary(stream, limits, cancellation_token, started)
        return SourcePayload(
            data,
            digest,
            DigestKind.EXACT_BYTES,
            len(data),
            None,
            path,
            Path(path).suffix.lower() or None,
            False,
        )
    if isinstance(source, (bytes, bytearray, memoryview)):
        try:
            data = bytes(source)
        except (TypeError, ValueError) as error:
            raise TypeError("source buffer must be contiguous bytes") from error
        limits.enforce("max_source_bytes", len(data))
        _check(cancellation_token, limits, started)
        return SourcePayload(
            data,
            hashlib.sha256(data).digest(),
            DigestKind.EXACT_BYTES,
            len(data),
            None,
            None,
            None,
            False,
        )
    read = getattr(source, "read", None)
    if not callable(read):
        raise TypeError("source must be a path, bytes-like value, BinaryIO, or TextIO")
    text_known = isinstance(source, io.TextIOBase)
    if text_known and (format is None or document_iri is None):
        raise ValueError("TextIO sources require explicit format and document_iri")
    if not text_known and document_iri is None:
        raise ValueError("stream sources require document_iri")
    data, digest, codepoints, text = _read_stream(source, limits, cancellation_token, started)
    if text and (format is None or document_iri is None):
        raise ValueError("TextIO sources require explicit format and document_iri")
    return SourcePayload(
        data,
        digest,
        DigestKind.NORMALIZED_TEXT if text else DigestKind.EXACT_BYTES,
        len(data),
        codepoints if text else None,
        None,
        None,
        text,
    )


def _read_stream(
    stream: object,
    limits: ParseLimits,
    token: CancellationToken | None,
    started: float,
) -> tuple[bytes, bytes, int, bool]:
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    total = 0
    codepoints = 0
    text: bool | None = None
    while True:
        _check(token, limits, started)
        chunk = stream.read(64 * 1024)  # type: ignore[attr-defined]
        if chunk in (b"", ""):
            break
        if not isinstance(chunk, (bytes, str)):
            raise TypeError("stream.read() must return bytes or str")
        current_text = isinstance(chunk, str)
        if text is None:
            text = current_text
        elif text != current_text:
            raise TypeError("stream.read() changed between bytes and str")
        if isinstance(chunk, str):
            codepoints += len(chunk)
            try:
                encoded = chunk.encode("utf-8")
            except UnicodeEncodeError as error:
                raise OntologySyntaxError(
                    "text source contains an unpaired Unicode surrogate",
                    code="SOURCE_UNICODE",
                ) from error
        else:
            encoded = chunk
        total += len(encoded)
        limits.enforce("max_source_bytes", total)
        digest.update(encoded)
        chunks.append(encoded)
    return b"".join(chunks), digest.digest(), codepoints, bool(text)


def _read_binary(
    stream: BinaryIO,
    limits: ParseLimits,
    token: CancellationToken | None,
    started: float,
) -> tuple[bytes, bytes]:
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    total = 0
    while True:
        _check(token, limits, started)
        chunk = stream.read(64 * 1024)
        if chunk == b"":
            break
        if not isinstance(chunk, bytes):
            raise TypeError("binary stream.read() must return bytes")
        total += len(chunk)
        limits.enforce("max_source_bytes", total)
        digest.update(chunk)
        chunks.append(chunk)
    return b"".join(chunks), digest.digest()


def iter_chunks(data: bytes, size: int = 64 * 1024) -> Iterator[bytes]:
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise ValueError("size must be a positive integer")
    for offset in range(0, len(data), size):
        yield data[offset : offset + size]


def _check(
    token: CancellationToken | None,
    limits: ParseLimits,
    started: float,
) -> None:
    if token is not None:
        token.check()
    deadline = limits.deadline_seconds
    if deadline is not None and time.monotonic() - started >= deadline:
        raise ResourceLimitError(
            "resource limit deadline_seconds exceeded",
            limit="deadline_seconds",
            observed=time.monotonic() - started,
            allowed=deadline,
        )


__all__ = ["DocumentSource", "SourcePayload", "acquire_source", "iter_chunks"]
