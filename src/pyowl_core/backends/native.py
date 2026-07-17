"""Validated Python adapter to the optional private Rust extension."""

from __future__ import annotations

import importlib
import math
import os
import platform
import re
import struct
import sysconfig
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from pyowl_core.cancellation import CancellationToken
from pyowl_core.exceptions import (
    BackendProtocolError,
    BackendUnavailableError,
    OntologySyntaxError,
    OperationCancelledError,
    ResourceLimitError,
    UnsupportedSyntaxError,
    WireCorruptionError,
    WireLimitError,
    WireVersionError,
)
from pyowl_core.limits import ParseLimits

if TYPE_CHECKING:
    from pyowl_core.document.snapshot import OntologySnapshot, OntologyView
    from pyowl_core.io.formats.common import ParsedOntology
    from pyowl_core.model.axioms import AxiomNode

_ABI_VERSION = 1
_MODEL_SCHEMA_VERSION = 1
_WIRE_FORMAT_VERSION = (1, 0)
_CONFIG = struct.Struct("<8sHHI37Q")
_RECEIPT = struct.Struct("<8sIIHHIQ32sIQ")
_CONFIG_MAGIC = b"PYNCONF\0"
_RECEIPT_MAGIC = b"PYNVAL1\0"
_PARSE_REQUEST = struct.Struct("<8sHHQ")
_PARSE_RESULT_HEADER = struct.Struct("<8sHHQ")
_PARSE_REQUEST_MAGIC = b"PYNFSS1\0"
_PARSE_RESULT_MAGIC = b"PYNFSSR1"
_INDEX_SOURCE_HEADER = struct.Struct("<8sHHQ")
_INDEX_RESULT_HEADER = struct.Struct("<8sHHQ")
_INDEX_SOURCE_MAGIC = b"PYNIDXS1"
_INDEX_REQUEST_MAGIC = b"PYNIDXQ1"
_INDEX_RESULT_MAGIC = b"PYNIDXR1"
_CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_EXTENSION_NAME = "pyowl_core._native"
_FOUNDATION_FEATURES = frozenset(
    {
        "canonical-model-v1",
        "cancellation",
        "deadlines",
        "gil-release",
        "owned-buffers",
        "panic-containment",
        "safe-rust",
        "wire-v1",
    }
)


class _NativeCancellation(Protocol):
    @property
    def cancelled(self) -> bool: ...

    def cancel(self) -> bool: ...


class _Extension(Protocol):
    ABI_VERSION: int
    MODEL_SCHEMA_VERSION: int
    WIRE_FORMAT_VERSION: tuple[int, int]
    FEATURES: tuple[str, ...]
    _NativeError: type[Exception]
    _Cancellation: Callable[[float | None], _NativeCancellation]

    def version(self) -> tuple[str, int]: ...

    def self_test(self) -> None: ...

    def validate_canonical(
        self, data: object, config: object, cancel: _NativeCancellation | None = None
    ) -> bytes: ...

    def validate_wire(
        self, data: object, config: object, cancel: _NativeCancellation | None = None
    ) -> bytes: ...

    def roundtrip_wire(
        self, data: object, config: object, cancel: _NativeCancellation | None = None
    ) -> bytes: ...

    def parse_document(
        self, data: object, config: object, cancel: _NativeCancellation | None = None
    ) -> bytes: ...

    def build_index(
        self, data: object, request: object, cancel: _NativeCancellation | None = None
    ) -> bytes: ...


class _Subinterpreters(Protocol):
    def get_current(self) -> int: ...

    def get_main(self) -> int: ...


@dataclass(frozen=True, slots=True)
class NativeProbe:
    available: bool
    reason: str | None
    version: str | None
    features: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NativeValidation:
    wire_minor: int
    feature_flags: int
    total_length: int
    file_digest: bytes
    section_count: int
    total_rows: int


@dataclass(frozen=True, slots=True)
class NativeAxiomPartition:
    postings: dict[type[AxiomNode], tuple[AxiomNode, ...]]
    canonical_sizes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _CachedRuntime:
    key: tuple[int, int]
    probe: NativeProbe
    extension: _Extension | None


_probe_lock = threading.Lock()
_cached_runtime: _CachedRuntime | None = None


def _after_fork_child() -> None:
    global _cached_runtime, _probe_lock
    _probe_lock = threading.Lock()
    _cached_runtime = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)


def probe(capability: str | None = None, *, refresh: bool = False) -> NativeProbe:
    """Probe ABI/self-test compatibility once, then apply one capability gate."""

    if capability is not None and (not isinstance(capability, str) or not capability):
        raise ValueError("capability must be a nonempty string or None")
    runtime = _runtime(refresh=refresh)
    result = runtime.probe
    if result.available and capability is not None and capability not in result.features:
        return NativeProbe(
            False,
            f"installed native backend lacks required capability {capability!r}",
            result.version,
            result.features,
        )
    return result


def require(capability: str) -> _Extension:
    """Return a compatible extension or raise before operation work starts."""

    result = probe(capability)
    runtime = _runtime()
    if not result.available or runtime.extension is None:
        raise BackendUnavailableError(
            f"native backend unavailable: {result.reason or 'unknown compatibility failure'}",
            code=(
                "NATIVE_CAPABILITY_UNAVAILABLE"
                if runtime.probe.available
                else "NATIVE_BACKEND_UNAVAILABLE"
            ),
        )
    return runtime.extension


def validate_canonical(
    data: object,
    *,
    limits: ParseLimits | None = None,
    cancellation_token: CancellationToken | None = None,
) -> bytes:
    extension = require("canonical-model-v1")
    selected = _coerce_limits(limits)
    config = _encode_config(selected, cancellation_token, verify=True)
    with _relay(extension, selected, cancellation_token) as cancel:
        return _call(extension, lambda: extension.validate_canonical(data, config, cancel))


def validate_wire(
    data: object,
    *,
    limits: ParseLimits | None = None,
    verify: bool = True,
    cancellation_token: CancellationToken | None = None,
) -> NativeValidation:
    extension = require("wire-v1")
    selected = _coerce_limits(limits)
    if not isinstance(verify, bool):
        raise TypeError("verify must be bool")
    config = _encode_config(selected, cancellation_token, verify=verify)
    with _relay(extension, selected, cancellation_token) as cancel:
        receipt = _call(extension, lambda: extension.validate_wire(data, config, cancel))
    return _decode_receipt(receipt)


def roundtrip_wire(
    data: object,
    *,
    limits: ParseLimits | None = None,
    verify: bool = True,
    cancellation_token: CancellationToken | None = None,
) -> bytes:
    extension = require("wire-v1")
    selected = _coerce_limits(limits)
    if not isinstance(verify, bool):
        raise TypeError("verify must be bool")
    config = _encode_config(selected, cancellation_token, verify=verify)
    with _relay(extension, selected, cancellation_token) as cancel:
        result = _call(extension, lambda: extension.roundtrip_wire(data, config, cancel))
    if not isinstance(result, bytes):
        raise BackendProtocolError(
            "native wire operation returned a non-bytes result",
            code="NATIVE_RESULT_TYPE",
        )
    return result


def parse_functional(
    data: bytes,
    *,
    limits: ParseLimits | None = None,
    allow_swrl: bool = False,
    cancellation_token: CancellationToken | None = None,
) -> ParsedOntology:
    """Parse one complete Functional-Style document through the native capability."""

    extension = require("parse-functional-v1")
    selected = _coerce_limits(limits)
    if not isinstance(data, bytes):
        raise TypeError("native Functional Syntax source must be bytes")
    if not isinstance(allow_swrl, bool):
        raise TypeError("allow_swrl must be bool")
    selected.enforce("max_source_bytes", len(data))
    request = (
        _PARSE_REQUEST.pack(
            _PARSE_REQUEST_MAGIC,
            1,
            int(allow_swrl),
            len(data),
        )
        + data
    )
    config = _encode_config(selected, cancellation_token, verify=False)
    with _relay(extension, selected, cancellation_token) as cancel:
        result = _call_parse(
            extension,
            lambda: extension.parse_document(request, config, cancel),
        )
    return _decode_parsed_functional(result, selected)


def partition_axioms(
    axioms: tuple[AxiomNode, ...],
    *,
    limits: ParseLimits | None = None,
    cancellation_token: CancellationToken | None = None,
) -> NativeAxiomPartition:
    """Build exact-constructor postings through one coarse native operation."""

    extension = require("index-axiom-types-v1")
    selected = _coerce_limits(limits)
    if not isinstance(axioms, tuple):
        raise TypeError("axioms must be a tuple")
    from pyowl_core.model import canonical_bytes
    from pyowl_core.model.axioms import AxiomNode

    if not all(isinstance(value, AxiomNode) for value in axioms):
        raise TypeError("axioms must contain AxiomNode values")
    selected.enforce("max_index_rows", len(axioms))
    source = bytearray(_INDEX_SOURCE_HEADER.pack(_INDEX_SOURCE_MAGIC, 1, 0, len(axioms)))
    canonical_sizes: list[int] = []
    for ordinal, axiom in enumerate(axioms, 1):
        if cancellation_token is not None and (ordinal % selected.cancellation_check_interval == 0):
            cancellation_token.check()
        encoded = canonical_bytes(axiom, limits=selected)
        canonical_sizes.append(len(encoded))
        source.extend(struct.pack("<Q", len(encoded)))
        source.extend(encoded)
        selected.enforce("max_index_bytes", len(source))
    config = _encode_config(selected, cancellation_token, verify=False)
    request = _INDEX_REQUEST_MAGIC + config
    with _relay(extension, selected, cancellation_token) as cancel:
        result = _call_index(
            extension,
            lambda: extension.build_index(source, request, cancel),
        )
    return NativeAxiomPartition(
        _decode_axiom_partition(result, axioms, selected),
        tuple(canonical_sizes),
    )


def encode_snapshot(
    snapshot: OntologyView,
    *,
    limits: ParseLimits | None = None,
    cancellation_token: CancellationToken | None = None,
) -> bytes:
    """Cross the native boundary only through frozen canonical wire bytes."""

    from pyowl_core.wire.codec import encode_snapshot as python_encode

    encoded = python_encode(snapshot, limits=limits, cancellation_token=cancellation_token)
    native = roundtrip_wire(encoded, limits=limits, cancellation_token=cancellation_token)
    if native != encoded:
        raise BackendProtocolError(
            "native wire encoder diverged from canonical Python bytes",
            code="NATIVE_WIRE_PARITY",
        )
    return native


def decode_snapshot(
    data: object,
    *,
    limits: ParseLimits | None = None,
    verify: bool = True,
    cancellation_token: CancellationToken | None = None,
) -> OntologySnapshot:
    """Validate natively, then validate/materialize the result in core Python."""

    from pyowl_core.wire.codec import decode_snapshot as python_decode

    native = roundtrip_wire(
        data,
        limits=limits,
        verify=verify,
        cancellation_token=cancellation_token,
    )
    return python_decode(
        native,
        limits=limits,
        verify=verify,
        cancellation_token=cancellation_token,
    )


def _runtime(*, refresh: bool = False) -> _CachedRuntime:
    global _cached_runtime
    key = (os.getpid(), _interpreter_id())
    with _probe_lock:
        retained = _cached_runtime
        if not refresh and retained is not None and retained.key == key:
            return retained
        policy_reason = _runtime_policy_reason()
        if policy_reason is not None:
            selected = _CachedRuntime(key, NativeProbe(False, policy_reason, None, ()), None)
        else:
            selected = _load_runtime(key)
        _cached_runtime = selected
        return selected


def _load_runtime(key: tuple[int, int]) -> _CachedRuntime:
    try:
        module = importlib.import_module(_EXTENSION_NAME)
    except (ImportError, ModuleNotFoundError):
        return _unavailable(key, "native extension is not installed")
    except OSError:
        return _unavailable(key, "native extension could not be loaded")
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return _unavailable(key, "native extension import failed")
    extension = cast(_Extension, module)
    try:
        features = _validate_metadata(extension)
        extension.self_test()
        version = extension.version()
        if (
            not isinstance(version, tuple)
            or len(version) != 2
            or not isinstance(version[0], str)
            or not version[0]
            or version[1] != _ABI_VERSION
        ):
            return _unavailable(key, "native extension returned invalid version metadata")
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        code = _private_error_code(extension, error)
        suffix = "" if code is None else f" ({code})"
        return _unavailable(key, f"native extension self-test failed{suffix}")
    probe_result = NativeProbe(True, None, version[0], features)
    return _CachedRuntime(key, probe_result, extension)


def _validate_metadata(extension: _Extension) -> tuple[str, ...]:
    if extension.ABI_VERSION != _ABI_VERSION:
        raise ValueError("native ABI mismatch")
    if extension.MODEL_SCHEMA_VERSION != _MODEL_SCHEMA_VERSION:
        raise ValueError("native model schema mismatch")
    if extension.WIRE_FORMAT_VERSION != _WIRE_FORMAT_VERSION:
        raise ValueError("native wire version mismatch")
    features = extension.FEATURES
    if (
        not isinstance(features, tuple)
        or not all(isinstance(value, str) and value for value in features)
        or tuple(sorted(set(features))) != features
        or not _FOUNDATION_FEATURES.issubset(features)
    ):
        raise ValueError("native feature ledger is invalid")
    return features


def _runtime_policy_reason() -> str | None:
    if platform.python_implementation() != "CPython":
        return "native extension is supported only on approved CPython builds"
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        return "native extension is not approved for free-threaded CPython"
    if _interpreter_id() != 0:
        return "native extension is not approved in subinterpreters"
    return None


def _interpreter_id() -> int:
    try:
        interpreters = cast(
            _Subinterpreters,
            importlib.import_module("_xxsubinterpreters"),
        )

        current = int(interpreters.get_current())
        main = int(interpreters.get_main())
    except (ImportError, AttributeError, RuntimeError):
        return 0
    return 0 if current == main else current + 1


def _unavailable(key: tuple[int, int], reason: str) -> _CachedRuntime:
    return _CachedRuntime(key, NativeProbe(False, reason, None, ()), None)


def _coerce_limits(limits: ParseLimits | None) -> ParseLimits:
    if limits is None:
        return ParseLimits()
    if not isinstance(limits, ParseLimits):
        raise TypeError("limits must be ParseLimits or None")
    return limits


def _encode_config(
    limits: ParseLimits,
    cancellation_token: CancellationToken | None,
    *,
    verify: bool,
) -> bytes:
    if cancellation_token is not None and not isinstance(cancellation_token, CancellationToken):
        raise TypeError("cancellation_token must be CancellationToken or None")
    if cancellation_token is not None:
        cancellation_token.check()
    deadline = limits.deadline_seconds
    if cancellation_token is not None and cancellation_token.remaining_seconds is not None:
        remaining = cancellation_token.remaining_seconds
        deadline = remaining if deadline is None else min(deadline, remaining)
    deadline_ns = 0
    if deadline is not None:
        if not math.isfinite(deadline) or deadline <= 0:
            raise OperationCancelledError("operation deadline exceeded", reason="deadline exceeded")
        deadline_ns = min(0xFFFF_FFFF_FFFF_FFFF, max(1, math.ceil(deadline * 1_000_000_000)))
    memory = 0 if limits.max_memory_bytes is None else _u64_limit(limits.max_memory_bytes)
    values = (
        _u64_limit(limits.max_source_bytes),
        _u64_limit(limits.max_documents),
        _u64_limit(limits.max_total_source_bytes),
        _u64_limit(limits.max_axioms),
        _u64_limit(limits.max_terms),
        _u64_limit(limits.max_nesting_depth),
        _u64_limit(limits.max_rdf_list_length),
        _u64_limit(limits.max_literal_bytes),
        _u64_limit(limits.max_iri_bytes),
        _u64_limit(limits.max_prefixes),
        _u64_limit(limits.max_import_depth),
        _u64_limit(limits.max_redirects),
        _u64_limit(limits.max_diagnostics),
        memory,
        deadline_ns,
        _u64_limit(limits.max_triples),
        _u64_limit(limits.max_strings),
        _u64_limit(limits.max_annotations),
        _u64_limit(limits.max_rule_atoms),
        _u64_limit(limits.max_sequence_arity),
        _u64_limit(limits.max_catalog_rewrites),
        _u64_limit(limits.max_resolver_attempts),
        _u64_limit(limits.max_concurrent_fetches),
        _u64_limit(limits.max_source_map_entries),
        _u64_limit(limits.max_origin_entries),
        _u64_limit(limits.max_overlay_depth),
        _u64_limit(limits.max_delta_entries),
        _u64_limit(limits.max_composite_members),
        _u64_limit(limits.max_index_rows),
        _u64_limit(limits.max_index_bytes),
        _u64_limit(limits.max_wire_rows),
        _u64_limit(limits.max_wire_bytes),
        _u64_limit(limits.max_temporary_bytes),
        _u64_limit(limits.max_disk_cache_bytes),
        _u64_limit(limits.max_decompressed_bytes),
        _u64_limit(limits.max_canonical_work),
        _u64_limit(limits.cancellation_check_interval),
    )
    return _CONFIG.pack(
        _CONFIG_MAGIC,
        1,
        int(verify),
        0,
        *values,
    )


def _u64_limit(value: int) -> int:
    return min(value, 0xFFFF_FFFF_FFFF_FFFF)


class _Relay:
    __slots__ = ("_cancel", "_stop", "_thread", "_token")

    def __init__(
        self,
        extension: _Extension,
        limits: ParseLimits,
        token: CancellationToken | None,
    ) -> None:
        deadline = limits.deadline_seconds
        if token is not None and token.remaining_seconds is not None:
            deadline = (
                token.remaining_seconds
                if deadline is None
                else min(deadline, token.remaining_seconds)
            )
        self._cancel = extension._Cancellation(deadline)
        self._stop = threading.Event()
        self._token = token
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _NativeCancellation:
        if self._token is not None:
            thread = threading.Thread(
                target=self._watch,
                name="pyowl-core-native-cancel",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        return self._cancel

    def __exit__(self, *_error: object) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)

    def _watch(self) -> None:
        token = self._token
        if token is None:
            return
        while not self._stop.wait(0.001):
            if token.cancelled:
                self._cancel.cancel()
                return


def _relay(
    extension: _Extension,
    limits: ParseLimits,
    token: CancellationToken | None,
) -> _Relay:
    return _Relay(extension, limits, token)


def _call(extension: _Extension, operation: Callable[[], bytes]) -> bytes:
    try:
        return operation()
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        code = _private_error_code(extension, error)
        if code is None:
            raise BackendProtocolError(
                "native backend raised an unrecognized exception",
                code="NATIVE_EXCEPTION",
            ) from error
        message = _private_error_message(error)
        if code == "NATIVE_CANCELLED":
            raise OperationCancelledError(message, code=code) from error
        if code in {"NATIVE_DEADLINE", "NATIVE_WIRE_LIMIT"}:
            raise WireLimitError(message, code=code) from error
        if code == "NATIVE_WIRE_VERSION":
            raise WireVersionError(message, code=code) from error
        if code == "NATIVE_WIRE_CORRUPTION":
            raise WireCorruptionError(message, code=code) from error
        if code == "NATIVE_CAPABILITY_UNAVAILABLE":
            raise BackendUnavailableError(message, code=code) from error
        raise BackendProtocolError(message, code=code) from error


def _call_parse(extension: _Extension, operation: Callable[[], bytes]) -> bytes:
    try:
        result = operation()
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        code = _private_error_code(extension, error)
        if code is None:
            raise BackendProtocolError(
                "native parser raised an unrecognized exception",
                code="NATIVE_EXCEPTION",
            ) from error
        message = _private_error_message(error)
        if code == "NATIVE_CANCELLED":
            raise OperationCancelledError(message, code=code) from error
        if code in {"NATIVE_DEADLINE", "NATIVE_WIRE_LIMIT"}:
            raise ResourceLimitError(message, code=code) from error
        if code in {"NATIVE_FORMAT_ENCODING", "NATIVE_FORMAT_SYNTAX"}:
            raise OntologySyntaxError(message, code=code) from error
        if code == "NATIVE_EXTENSION_DISABLED":
            raise UnsupportedSyntaxError(message, code=code) from error
        if code == "NATIVE_CAPABILITY_UNAVAILABLE":
            raise BackendUnavailableError(message, code=code) from error
        raise BackendProtocolError(message, code=code) from error
    if not isinstance(result, bytes):
        raise BackendProtocolError(
            "native parser returned a non-bytes result",
            code="NATIVE_RESULT_TYPE",
        )
    return result


def _call_index(extension: _Extension, operation: Callable[[], bytes]) -> bytes:
    try:
        result = operation()
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        code = _private_error_code(extension, error)
        if code is None:
            raise BackendProtocolError(
                "native index raised an unrecognized exception",
                code="NATIVE_EXCEPTION",
            ) from error
        message = _private_error_message(error)
        if code == "NATIVE_CANCELLED":
            raise OperationCancelledError(message, code=code) from error
        if code in {"NATIVE_DEADLINE", "NATIVE_WIRE_LIMIT"}:
            raise ResourceLimitError(message, code=code) from error
        if code == "NATIVE_CAPABILITY_UNAVAILABLE":
            raise BackendUnavailableError(message, code=code) from error
        raise BackendProtocolError(message, code=code) from error
    if not isinstance(result, bytes):
        raise BackendProtocolError(
            "native index returned a non-bytes result",
            code="NATIVE_RESULT_TYPE",
        )
    return result


class _ResultReader:
    __slots__ = ("_data", "_framing_code", "_offset")

    def __init__(self, data: bytes, *, framing_code: str = "NATIVE_PARSE_FRAMING") -> None:
        if not isinstance(data, bytes):
            raise BackendProtocolError(
                "native parser result is not bytes",
                code="NATIVE_RESULT_TYPE",
            )
        self._data = data
        self._framing_code = framing_code
        self._offset = 0

    def take(self, size: int) -> bytes:
        end = self._offset + size
        if size < 0 or end < self._offset or end > len(self._data):
            raise BackendProtocolError(
                "native parser result is truncated",
                code=self._framing_code,
            )
        result = self._data[self._offset : end]
        self._offset = end
        return result

    def u8(self) -> int:
        return self.take(1)[0]

    def u64(self) -> int:
        return int.from_bytes(self.take(8), "little")

    def varint(self) -> int:
        value = 0
        shift = 0
        start = self._offset
        while self._offset < len(self._data):
            byte = self.u8()
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                encoded = bytearray()
                remaining = value
                while True:
                    selected = remaining & 0x7F
                    remaining >>= 7
                    encoded.append(selected | (0x80 if remaining else 0))
                    if not remaining:
                        break
                if self._data[start : self._offset] != bytes(encoded):
                    raise BackendProtocolError(
                        "native parser result has a nonminimal integer",
                        code=self._framing_code,
                    )
                return value
            shift += 7
            if shift > 63:
                raise BackendProtocolError(
                    "native parser result integer is too large",
                    code=self._framing_code,
                )
        raise BackendProtocolError(
            "native parser result is truncated",
            code=self._framing_code,
        )

    def frame(self) -> bytes:
        return self.take(self.varint())

    def text(self) -> str:
        try:
            return self.frame().decode("utf-8")
        except UnicodeError as error:
            raise BackendProtocolError(
                "native parser returned invalid UTF-8",
                code=self._framing_code,
            ) from error

    def finish(self) -> None:
        if self._offset != len(self._data):
            raise BackendProtocolError(
                "native parser result contains trailing bytes",
                code=self._framing_code,
            )

    def require_count(self, count: int, minimum_bytes: int) -> None:
        if count > (len(self._data) - self._offset) // minimum_bytes:
            raise BackendProtocolError(
                "native result count exceeds remaining framing",
                code=self._framing_code,
            )


def _decode_parsed_functional(data: bytes, limits: ParseLimits) -> ParsedOntology:
    from pyowl_core.diagnostics import SourceSpan
    from pyowl_core.document import OntologyID
    from pyowl_core.io.formats.common import ParsedOntology
    from pyowl_core.model import IRI, Annotation, StructuralNode, decode_canonical
    from pyowl_core.model.axioms import AxiomNode

    reader = _ResultReader(data)
    magic, schema, format_tag, decoded_codepoints = _PARSE_RESULT_HEADER.unpack(
        reader.take(_PARSE_RESULT_HEADER.size)
    )
    if magic != _PARSE_RESULT_MAGIC or schema != 1 or format_tag != 4:
        raise BackendProtocolError(
            "native parser result has incompatible metadata",
            code="NATIVE_PARSE_VERSION",
        )

    def node() -> StructuralNode:
        payload = reader.frame()
        try:
            return decode_canonical(payload, limits=limits)
        except ResourceLimitError:
            raise
        except Exception as error:
            raise BackendProtocolError(
                "native parser returned invalid canonical model data",
                code="NATIVE_PARSE_MODEL",
            ) from error

    def optional_iri() -> IRI | None:
        marker = reader.u8()
        if marker == 0:
            return None
        if marker != 1:
            raise BackendProtocolError(
                "native parser returned an invalid optional value",
                code="NATIVE_PARSE_FRAMING",
            )
        value = node()
        if not isinstance(value, IRI):
            raise BackendProtocolError(
                "native parser returned a non-IRI ontology identifier",
                code="NATIVE_PARSE_MODEL",
            )
        return value

    ontology_iri = optional_iri()
    version_iri = optional_iri()
    if version_iri is not None and ontology_iri is None:
        raise BackendProtocolError(
            "native parser returned a version IRI without an ontology IRI",
            code="NATIVE_PARSE_MODEL",
        )
    import_count = reader.u64()
    reader.require_count(import_count, 1)
    imports: list[IRI] = []
    for _ in range(import_count):
        value = node()
        if not isinstance(value, IRI):
            raise BackendProtocolError(
                "native parser returned a non-IRI import",
                code="NATIVE_PARSE_MODEL",
            )
        imports.append(value)

    def spanned(
        expected: type[StructuralNode] | tuple[type[StructuralNode], ...],
        maximum_name: str | None,
    ) -> list[tuple[StructuralNode, SourceSpan]]:
        count = reader.u64()
        reader.require_count(count, 33)
        if maximum_name is not None:
            limits.enforce(maximum_name, count)
        values: list[tuple[StructuralNode, SourceSpan]] = []
        for _ in range(count):
            byte_start = reader.u64()
            byte_end = reader.u64()
            line = reader.u64()
            column = reader.u64()
            if byte_end < byte_start or line < 1 or column < 1:
                raise BackendProtocolError(
                    "native parser returned an invalid source span",
                    code="NATIVE_PARSE_FRAMING",
                )
            value = node()
            if not isinstance(value, expected):
                raise BackendProtocolError(
                    "native parser returned a value in the wrong result partition",
                    code="NATIVE_PARSE_MODEL",
                )
            values.append(
                (
                    value,
                    SourceSpan(
                        byte_start=byte_start,
                        byte_end=byte_end,
                        line_start=line,
                        column_start=column,
                    ),
                )
            )
        return values

    from pyowl_core.extensions.swrl import SWRLRule

    annotations = spanned(Annotation, None)
    axioms = spanned(AxiomNode, "max_axioms")
    extensions = spanned(SWRLRule, None)
    prefix_count = reader.u64()
    reader.require_count(prefix_count, 2)
    prefixes: list[tuple[str, str]] = []
    previous_prefix: tuple[str, str] | None = None
    for _ in range(prefix_count):
        prefix = reader.text()
        iri_value = reader.text()
        selected_prefix = (prefix, iri_value)
        if previous_prefix is not None and selected_prefix <= previous_prefix:
            raise BackendProtocolError(
                "native parser prefixes are not canonical",
                code="NATIVE_PARSE_FRAMING",
            )
        previous_prefix = selected_prefix
        prefixes.append(selected_prefix)
    reader.finish()
    occurrences = sorted(
        (*annotations, *axioms, *extensions),
        key=lambda item: (
            -1 if item[1].byte_start is None else item[1].byte_start,
            -1 if item[1].byte_end is None else item[1].byte_end,
        ),
    )
    return ParsedOntology(
        OntologyID(ontology_iri, version_iri),
        tuple(imports),
        tuple(cast(Annotation, value) for value, _span in annotations),
        tuple(cast(AxiomNode, value) for value, _span in axioms),
        tuple(value for value, _span in extensions),
        tuple(prefixes),
        tuple(occurrences),
        decoded_codepoint_length=decoded_codepoints,
    )


def _decode_axiom_partition(
    data: bytes,
    axioms: tuple[AxiomNode, ...],
    limits: ParseLimits,
) -> dict[type[AxiomNode], tuple[AxiomNode, ...]]:
    from pyowl_core.model.axioms import AxiomNode
    from pyowl_core.model.registry import SPEC_BY_TAG

    reader = _ResultReader(data, framing_code="NATIVE_INDEX_FRAMING")
    magic, schema, reserved, group_count = _INDEX_RESULT_HEADER.unpack(
        reader.take(_INDEX_RESULT_HEADER.size)
    )
    if magic != _INDEX_RESULT_MAGIC or schema != 1 or reserved != 0:
        raise BackendProtocolError(
            "native index result has incompatible metadata",
            code="NATIVE_INDEX_VERSION",
        )
    limits.enforce("max_index_rows", len(axioms))
    if group_count > len(axioms):
        raise BackendProtocolError(
            "native index result has too many groups",
            code="NATIVE_INDEX_FRAMING",
        )
    postings: dict[type[AxiomNode], tuple[AxiomNode, ...]] = {}
    seen: set[int] = set()
    previous_tag = -1
    for _ in range(group_count):
        tag = reader.u64()
        count = reader.u64()
        if tag <= previous_tag or count < 1:
            raise BackendProtocolError(
                "native index groups are not canonical",
                code="NATIVE_INDEX_FRAMING",
            )
        previous_tag = tag
        if count > len(axioms) - len(seen):
            raise BackendProtocolError(
                "native index posting exceeds remaining rows",
                code="NATIVE_INDEX_FRAMING",
            )
        reader.require_count(count, 8)
        spec = SPEC_BY_TAG.get(tag)
        if spec is None or not issubclass(spec.constructor, AxiomNode):
            raise BackendProtocolError(
                "native index result contains a non-axiom tag",
                code="NATIVE_INDEX_MODEL",
            )
        selected: list[AxiomNode] = []
        previous_ordinal = -1
        for _ in range(count):
            ordinal = reader.u64()
            if ordinal <= previous_ordinal or ordinal >= len(axioms) or ordinal in seen:
                raise BackendProtocolError(
                    "native index result contains an invalid row ordinal",
                    code="NATIVE_INDEX_FRAMING",
                )
            value = axioms[ordinal]
            if type(value) is not spec.constructor:
                raise BackendProtocolError(
                    "native index tag disagrees with the Python model",
                    code="NATIVE_INDEX_MODEL",
                )
            previous_ordinal = ordinal
            seen.add(ordinal)
            selected.append(value)
        postings[spec.constructor] = tuple(selected)
    reader.finish()
    if len(seen) != len(axioms):
        raise BackendProtocolError(
            "native index result omitted rows",
            code="NATIVE_INDEX_FRAMING",
        )
    return postings


def _private_error_code(extension: _Extension, error: Exception) -> str | None:
    if not isinstance(error, extension._NativeError) or len(error.args) != 2:
        return None
    code = error.args[0]
    return code if isinstance(code, str) and _CODE.fullmatch(code) else None


def _private_error_message(error: Exception) -> str:
    message = error.args[1] if len(error.args) == 2 else None
    if not isinstance(message, str) or not message or len(message) > 200:
        return "native backend reported an invalid error payload"
    return "".join(character if character.isprintable() else "?" for character in message)


def _decode_receipt(data: bytes) -> NativeValidation:
    if not isinstance(data, bytes) or len(data) != _RECEIPT.size:
        raise BackendProtocolError(
            "native validation receipt has invalid framing",
            code="NATIVE_RECEIPT_FRAMING",
        )
    (
        magic,
        abi,
        model,
        major,
        minor,
        flags,
        length,
        digest,
        sections,
        rows,
    ) = _RECEIPT.unpack(data)
    if (
        magic != _RECEIPT_MAGIC
        or abi != _ABI_VERSION
        or model != _MODEL_SCHEMA_VERSION
        or major != 1
    ):
        raise BackendProtocolError(
            "native validation receipt has incompatible metadata",
            code="NATIVE_RECEIPT_VERSION",
        )
    return NativeValidation(minor, flags, length, digest, sections, rows)


def _reset_probe_cache_for_tests() -> None:
    global _cached_runtime
    with _probe_lock:
        _cached_runtime = None


__all__ = [
    "NativeAxiomPartition",
    "NativeProbe",
    "NativeValidation",
    "decode_snapshot",
    "encode_snapshot",
    "parse_functional",
    "partition_axioms",
    "probe",
    "require",
    "roundtrip_wire",
    "validate_canonical",
    "validate_wire",
]
