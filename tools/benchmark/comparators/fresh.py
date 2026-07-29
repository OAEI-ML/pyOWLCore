"""Framed one-shot completion barrier for fresh comparator subprocesses."""

from __future__ import annotations

import hashlib
import json
import math
import os
import selectors
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, BinaryIO, cast

from .process_group import (
    OwnedProcessGroup,
    capture_process_group,
    cleanup_exited_process_group,
    observe_process_exit,
    provisional_process_group,
    terminate_process,
)

FRESH_PROTOCOL_SCHEMA = "pyowl-core/comparator-fresh-runner/v1"
FRESH_REQUEST_SCHEMA = "pyowl-core/comparator-fresh-request/v1"
FRESH_COMPLETED_SCHEMA = "pyowl-core/comparator-fresh-completed/v1"
FRESH_PUBLISH_SCHEMA = "pyowl-core/comparator-fresh-publish/v1"
FRESH_RESPONSE_SCHEMA = "pyowl-core/comparator-fresh-response/v1"

MAX_FRAME_HEADER_BYTES = 32
MAX_FRESH_ENVELOPE_OVERHEAD_BYTES = 64 * 1024
MAX_FRESH_CONTROL_FRAME_BYTES = 64 * 1024
_IO_CHUNK_BYTES = 64 * 1024
_SHA256_CHARACTERS = frozenset("0123456789abcdef")
_REQUEST_FIELDS = frozenset({"schema", "protocol", "sequence", "request"})
_COMPLETED_FIELDS = frozenset({"schema", "protocol", "sequence", "pid", "ontology_instance_id"})
_PUBLISH_FIELDS = _COMPLETED_FIELDS
_RESPONSE_FIELDS = frozenset({"schema", "protocol", "sequence", "ontology_instance_id", "result"})


class FreshRunnerError(RuntimeError):
    """A fresh comparator subprocess violated its lifecycle or wire contract."""


@dataclass(frozen=True, slots=True)
class FreshExchangeEvidence:
    """Validated result and parent-observed completion-boundary evidence."""

    result: dict[str, Any]
    parent_wall_ns: int
    parent_cpu_ns: int
    request_bytes: int
    stdout_bytes: int
    stderr_bytes: int
    runner_pid: int
    ontology_instance_id: str


def run_fresh_subprocess(
    command: Sequence[str],
    request: Mapping[str, object],
    *,
    timeout: float,
    max_request_bytes: int,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> FreshExchangeEvidence:
    """Run one framed request and stop parent clocks at authenticated completion."""

    _validate_command(command)
    timeout_seconds = _positive_timeout(timeout, "timeout")
    request_limit = _positive_limit(max_request_bytes, "request")
    stdout_limit = _positive_limit(max_stdout_bytes, "stdout")
    stderr_limit = _positive_limit(max_stderr_bytes, "stderr")
    request_value = dict(request)
    nested_payload = _json_bytes(request_value, "fresh adapter request")
    if len(nested_payload) > request_limit:
        raise FreshRunnerError("fresh nested adapter request exceeds its byte limit")
    request_frame = _encode_frame(
        {
            "schema": FRESH_REQUEST_SCHEMA,
            "protocol": FRESH_PROTOCOL_SCHEMA,
            "sequence": 0,
            "request": request_value,
        },
        max_payload_bytes=request_limit + MAX_FRESH_ENVELOPE_OVERHEAD_BYTES,
        name="fresh request",
    )

    parent_cpu_start = time.process_time_ns()
    parent_wall_start = time.perf_counter_ns()
    try:
        process = subprocess.Popen(
            tuple(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=None if env is None else dict(env),
            bufsize=0,
            close_fds=True,
            start_new_session=os.name == "posix",
        )
    except OSError as error:
        raise FreshRunnerError(f"fresh runner could not start: {error}") from error
    try:
        process_group = capture_process_group(process)
    except Exception as error:
        capture_cleanup_error: Exception | None = None
        fallback_group: OwnedProcessGroup | None = None
        try:
            fallback_group = provisional_process_group(process)
        except Exception as fallback_error:
            capture_cleanup_error = fallback_error
        try:
            try:
                terminate_process(
                    process,
                    process_group=fallback_group,
                    grace_seconds=1.0,
                )
            except Exception as teardown_error:
                capture_cleanup_error = teardown_error
        finally:
            _close_process_pipes(process)
        detail = f"fresh runner process-group setup failed: {error}"
        if capture_cleanup_error is not None:
            detail = f"{detail}; cleanup failed: {capture_cleanup_error}"
        raise FreshRunnerError(detail) from error

    try:
        exchange = _FreshProcess(
            process,
            process_group=process_group,
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=stdout_limit,
            max_stderr_bytes=stderr_limit,
        )
    except Exception as error:
        pipe_cleanup_error: Exception | None = None
        try:
            try:
                _terminate_process(process, process_group=process_group)
            except Exception as teardown_error:
                pipe_cleanup_error = teardown_error
        finally:
            _close_process_pipes(process)
        detail = f"fresh runner pipe setup failed: {error}"
        if pipe_cleanup_error is not None:
            detail = f"{detail}; cleanup failed: {pipe_cleanup_error}"
        raise FreshRunnerError(detail) from error
    try:
        completed_payload = exchange.exchange(
            request_frame,
            response_name="fresh completed acknowledgement",
            max_response_payload_bytes=MAX_FRESH_CONTROL_FRAME_BYTES,
            close_stdin_after_write=False,
        )
        completed = _json_object(completed_payload, "fresh completed acknowledgement")
        ontology_instance_id = _validate_completed(completed, pid=process.pid)
        parent_wall_ns = time.perf_counter_ns() - parent_wall_start
        parent_cpu_ns = time.process_time_ns() - parent_cpu_start

        exchange.reject_early_stdout()
        publish_frame = _encode_frame(
            {
                "schema": FRESH_PUBLISH_SCHEMA,
                "protocol": FRESH_PROTOCOL_SCHEMA,
                "sequence": 0,
                "pid": process.pid,
                "ontology_instance_id": ontology_instance_id,
            },
            max_payload_bytes=MAX_FRESH_CONTROL_FRAME_BYTES,
            name="fresh publish",
        )
        response_payload = exchange.exchange(
            publish_frame,
            response_name="fresh response",
            max_response_payload_bytes=stdout_limit,
            close_stdin_after_write=True,
        )
        response = _json_object(response_payload, "fresh response")
        result = _validate_response(
            response,
            ontology_instance_id=ontology_instance_id,
        )
        exchange.wait_for_clean_exit()
        return FreshExchangeEvidence(
            result=result,
            parent_wall_ns=parent_wall_ns,
            parent_cpu_ns=parent_cpu_ns,
            request_bytes=len(request_frame) + len(publish_frame),
            stdout_bytes=exchange.stdout_bytes,
            stderr_bytes=exchange.stderr_bytes,
            runner_pid=process.pid,
            ontology_instance_id=ontology_instance_id,
        )
    except Exception as error:
        exchange_cleanup_error: Exception | None = None
        try:
            exchange.fail()
        except Exception as teardown_error:
            exchange_cleanup_error = teardown_error
        detail = str(error)
        if exchange_cleanup_error is not None:
            detail = f"{detail}; cleanup failed: {exchange_cleanup_error}"
        stderr = exchange.stderr_text()
        if stderr:
            detail = f"{detail}; stderr: {stderr}"
        raise FreshRunnerError(detail) from error
    finally:
        exchange.close()


def read_fresh_request(
    *,
    max_request_bytes: int,
    stream: BinaryIO | None = None,
) -> dict[str, Any]:
    """Read and strictly validate the single framed fresh request."""

    request_limit = _positive_limit(max_request_bytes, "request")
    source = sys.stdin.buffer if stream is None else stream
    payload = _read_frame_blocking(
        source,
        max_payload_bytes=request_limit + MAX_FRESH_ENVELOPE_OVERHEAD_BYTES,
        name="fresh request",
    )
    outer = _json_object(payload, "fresh request")
    if set(outer) != _REQUEST_FIELDS:
        raise FreshRunnerError("fresh request fields differ from schema v1")
    for name, expected in (
        ("schema", FRESH_REQUEST_SCHEMA),
        ("protocol", FRESH_PROTOCOL_SCHEMA),
    ):
        if outer.get(name) != expected:
            raise FreshRunnerError(f"fresh request {name} differs")
    if not _is_u64(outer.get("sequence")) or outer.get("sequence") != 0:
        raise FreshRunnerError("fresh request sequence must be unsigned integer zero")
    request = outer.get("request")
    if not isinstance(request, dict):
        raise FreshRunnerError("fresh nested adapter request must be an object")
    nested_payload = _json_bytes(request, "fresh nested adapter request")
    if len(nested_payload) > request_limit:
        raise FreshRunnerError("fresh nested adapter request exceeds its byte limit")
    return cast(dict[str, Any], request)


def publish_fresh_result(
    result: Mapping[str, object],
    *,
    max_request_bytes: int,
    max_response_bytes: int,
    input_stream: BinaryIO | None = None,
    output_stream: BinaryIO | None = None,
) -> None:
    """Publish completion, require an authenticated release, then serialize result."""

    _positive_limit(max_request_bytes, "request")
    response_limit = _positive_limit(max_response_bytes, "response")
    if not isinstance(result, Mapping):
        raise TypeError("fresh result must be an object")
    source = sys.stdin.buffer if input_stream is None else input_stream
    target = sys.stdout.buffer if output_stream is None else output_stream
    pid = os.getpid()
    ontology_instance_id = _ontology_instance_id(pid)
    _write_frame_blocking(
        target,
        {
            "schema": FRESH_COMPLETED_SCHEMA,
            "protocol": FRESH_PROTOCOL_SCHEMA,
            "sequence": 0,
            "pid": pid,
            "ontology_instance_id": ontology_instance_id,
        },
        max_payload_bytes=MAX_FRESH_CONTROL_FRAME_BYTES,
        name="fresh completed acknowledgement",
    )
    publish_payload = _read_frame_blocking(
        source,
        max_payload_bytes=MAX_FRESH_CONTROL_FRAME_BYTES,
        name="fresh publish",
    )
    publish = _json_object(publish_payload, "fresh publish")
    _validate_publish(
        publish,
        pid=pid,
        ontology_instance_id=ontology_instance_id,
    )
    if source.read(1) != b"":
        raise FreshRunnerError("fresh runner received trailing input after publish")
    # Deliberately construct and serialize the response only after publish and EOF.
    _write_frame_blocking(
        target,
        {
            "schema": FRESH_RESPONSE_SCHEMA,
            "protocol": FRESH_PROTOCOL_SCHEMA,
            "sequence": 0,
            "ontology_instance_id": ontology_instance_id,
            "result": dict(result),
        },
        max_payload_bytes=response_limit,
        name="fresh response",
    )


class _FreshProcess:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        process_group: OwnedProcessGroup | None,
        timeout_seconds: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> None:
        self.process = process
        self.process_group = process_group
        self.deadline = time.monotonic() + timeout_seconds
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.stdout_buffer = bytearray()
        self.stderr = bytearray()
        self.stdout_bytes = 0
        self._stdin_closed = False
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is None:
                raise FreshRunnerError("fresh runner lacks a required pipe")
            os.set_blocking(stream.fileno(), False)

    @property
    def stderr_bytes(self) -> int:
        return len(self.stderr)

    def exchange(
        self,
        outgoing: bytes,
        *,
        response_name: str,
        max_response_payload_bytes: int,
        close_stdin_after_write: bool,
    ) -> bytes:
        if observe_process_exit(self.process, process_group=self.process_group):
            raise FreshRunnerError(f"fresh runner exited before {response_name}")
        stdin = self._pipe(self.process.stdin, "stdin")
        stdout = self._pipe(self.process.stdout, "stdout")
        stderr = self._pipe(self.process.stderr, "stderr")
        write_offset = 0
        response: bytes | None = None
        stdout_eof = False
        with selectors.DefaultSelector() as selector:
            selector.register(stdout, selectors.EVENT_READ, "stdout")
            selector.register(stderr, selectors.EVENT_READ, "stderr")
            selector.register(stdin, selectors.EVENT_WRITE, "stdin")
            while response is None or write_offset < len(outgoing):
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise FreshRunnerError(f"{response_name} timed out")
                events = selector.select(min(remaining, 0.1))
                if not events:
                    if (
                        observe_process_exit(
                            self.process,
                            process_group=self.process_group,
                        )
                        and stdout_eof
                    ):
                        raise FreshRunnerError(f"fresh runner exited before {response_name}")
                    continue
                for key, _events in events:
                    if key.data == "stdin":
                        try:
                            written = os.write(
                                stdin.fileno(),
                                outgoing[write_offset:],
                            )
                        except BlockingIOError:
                            continue
                        except BrokenPipeError as error:
                            raise FreshRunnerError("fresh runner closed stdin") from error
                        if written <= 0:
                            raise FreshRunnerError("fresh request write made no progress")
                        write_offset += written
                        if write_offset == len(outgoing):
                            selector.unregister(stdin)
                            if close_stdin_after_write:
                                stdin.close()
                                self._stdin_closed = True
                    elif key.data == "stdout":
                        chunk = _read_nonblocking(stdout.fileno())
                        if chunk == b"":
                            stdout_eof = True
                        elif chunk is not None:
                            self._append_stdout(chunk)
                            if write_offset < len(outgoing):
                                raise FreshRunnerError(
                                    f"{response_name} arrived before its release frame"
                                )
                            candidate = self._extract_frame(
                                response_name,
                                max_payload_bytes=max_response_payload_bytes,
                            )
                            if candidate is not None:
                                response = candidate
                    else:
                        chunk = _read_nonblocking(stderr.fileno())
                        if chunk not in {None, b""}:
                            self._append_stderr(chunk)
                if stdout_eof and response is None:
                    raise FreshRunnerError(f"fresh runner closed stdout before {response_name}")
        assert response is not None
        return response

    def reject_early_stdout(self) -> None:
        if self.stdout_buffer:
            raise FreshRunnerError("fresh runner emitted a response before publish")
        stdout = self._pipe(self.process.stdout, "stdout")
        while True:
            chunk = _read_nonblocking(stdout.fileno())
            if chunk is None:
                break
            if chunk == b"":
                raise FreshRunnerError("fresh runner exited before publish")
            self._append_stdout(chunk)
            raise FreshRunnerError("fresh runner emitted a response before publish")
        self._drain_stderr()
        if observe_process_exit(self.process, process_group=self.process_group):
            raise FreshRunnerError("fresh runner exited before publish")

    def wait_for_clean_exit(self) -> None:
        if not self._stdin_closed:
            raise FreshRunnerError("fresh runner stdin remained open after publish")
        stdout = self._pipe(self.process.stdout, "stdout")
        while not observe_process_exit(
            self.process,
            process_group=self.process_group,
        ):
            if time.monotonic() >= self.deadline:
                raise FreshRunnerError("fresh runner clean exit timed out")
            chunk = _read_nonblocking(stdout.fileno())
            if chunk not in {None, b""}:
                self._append_stdout(chunk)
                raise FreshRunnerError("fresh runner emitted trailing response output")
            self._drain_stderr()
            time.sleep(0.005)
        self._drain_stderr()
        chunk = _read_nonblocking(stdout.fileno())
        if chunk not in {None, b""}:
            self._append_stdout(chunk)
            raise FreshRunnerError("fresh runner emitted trailing response output")
        if not cleanup_exited_process_group(
            self.process,
            process_group=self.process_group,
            grace_seconds=1.0,
        ):
            raise FreshRunnerError("fresh runner left descendant processes after clean exit")
        if self.process.returncode != 0:
            raise FreshRunnerError(
                f"fresh runner exited {self.process.returncode} after its response"
            )

    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", "replace")

    def fail(self) -> None:
        _terminate_process(self.process, process_group=self.process_group)
        with suppress(OSError, FreshRunnerError):
            self._drain_stderr()

    def close(self) -> None:
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                with suppress(OSError):
                    stream.close()

    def _extract_frame(self, name: str, *, max_payload_bytes: int) -> bytes | None:
        newline = self.stdout_buffer.find(b"\n")
        if newline < 0:
            if len(self.stdout_buffer) > MAX_FRAME_HEADER_BYTES:
                raise FreshRunnerError(f"{name} frame header exceeds its byte limit")
            return None
        if newline == 0 or newline > MAX_FRAME_HEADER_BYTES:
            raise FreshRunnerError(f"{name} frame header is invalid")
        header = bytes(self.stdout_buffer[:newline])
        if any(value < ord("0") or value > ord("9") for value in header):
            raise FreshRunnerError(f"{name} frame length is not decimal")
        if len(header) > 1 and header.startswith(b"0"):
            raise FreshRunnerError(f"{name} frame length is not canonical")
        payload_bytes = int(header)
        if payload_bytes < 1 or payload_bytes > max_payload_bytes:
            raise FreshRunnerError(f"{name} frame exceeds its byte limit")
        total = newline + 1 + payload_bytes + 1
        if total > self.max_stdout_bytes:
            raise FreshRunnerError("fresh runner exceeded its stdout byte limit")
        if len(self.stdout_buffer) < total:
            return None
        if self.stdout_buffer[total - 1] != ord("\n"):
            raise FreshRunnerError(f"{name} frame lacks its terminal newline")
        payload = bytes(self.stdout_buffer[newline + 1 : total - 1])
        del self.stdout_buffer[:total]
        if self.stdout_buffer:
            raise FreshRunnerError("fresh runner emitted extra response output")
        return payload

    def _append_stdout(self, chunk: bytes) -> None:
        self.stdout_bytes += len(chunk)
        if self.stdout_bytes > self.max_stdout_bytes:
            raise FreshRunnerError("fresh runner exceeded its stdout byte limit")
        self.stdout_buffer.extend(chunk)

    def _append_stderr(self, chunk: bytes) -> None:
        remaining = self.max_stderr_bytes - len(self.stderr)
        if len(chunk) > remaining:
            if remaining > 0:
                self.stderr.extend(chunk[:remaining])
            raise FreshRunnerError("fresh runner exceeded its stderr byte limit")
        self.stderr.extend(chunk)

    def _drain_stderr(self) -> None:
        stderr = self._pipe(self.process.stderr, "stderr")
        while True:
            chunk = _read_nonblocking(stderr.fileno())
            if chunk in {None, b""}:
                return
            self._append_stderr(chunk)

    @staticmethod
    def _pipe(stream: IO[Any] | None, name: str) -> IO[Any]:
        if stream is None:
            raise FreshRunnerError(f"fresh runner lacks {name} pipe")
        return stream


def _validate_completed(value: Mapping[str, Any], *, pid: int) -> str:
    if set(value) != _COMPLETED_FIELDS:
        raise FreshRunnerError("fresh completed fields differ from schema v1")
    for name, expected in (
        ("schema", FRESH_COMPLETED_SCHEMA),
        ("protocol", FRESH_PROTOCOL_SCHEMA),
    ):
        if value.get(name) != expected:
            raise FreshRunnerError(f"fresh completed {name} differs")
    if not _is_u64(value.get("sequence")) or value.get("sequence") != 0:
        raise FreshRunnerError("fresh completed sequence must be unsigned integer zero")
    observed_pid = value.get("pid")
    if not _is_u64(observed_pid) or observed_pid != pid:
        raise FreshRunnerError("fresh completed pid differs from the subprocess")
    ontology_instance_id = value.get("ontology_instance_id")
    if not _is_sha256(ontology_instance_id):
        raise FreshRunnerError("fresh completed ontology_instance_id must be lowercase SHA-256")
    expected_instance_id = _ontology_instance_id(pid)
    if ontology_instance_id != expected_instance_id:
        raise FreshRunnerError("fresh completed ontology_instance_id differs")
    return cast(str, ontology_instance_id)


def _validate_publish(
    value: Mapping[str, Any],
    *,
    pid: int,
    ontology_instance_id: str,
) -> None:
    if set(value) != _PUBLISH_FIELDS:
        raise FreshRunnerError("fresh publish fields differ from schema v1")
    for name, expected in (
        ("schema", FRESH_PUBLISH_SCHEMA),
        ("protocol", FRESH_PROTOCOL_SCHEMA),
    ):
        if value.get(name) != expected:
            raise FreshRunnerError(f"fresh publish {name} differs")
    if not _is_u64(value.get("sequence")) or value.get("sequence") != 0:
        raise FreshRunnerError("fresh publish sequence must be unsigned integer zero")
    observed_pid = value.get("pid")
    if not _is_u64(observed_pid) or observed_pid != pid:
        raise FreshRunnerError("fresh publish pid differs")
    observed_instance_id = value.get("ontology_instance_id")
    if not _is_sha256(observed_instance_id):
        raise FreshRunnerError("fresh publish ontology_instance_id must be lowercase SHA-256")
    if observed_instance_id != ontology_instance_id:
        raise FreshRunnerError("fresh publish ontology_instance_id differs")


def _validate_response(
    value: Mapping[str, Any],
    *,
    ontology_instance_id: str,
) -> dict[str, Any]:
    if set(value) != _RESPONSE_FIELDS:
        raise FreshRunnerError("fresh response fields differ from schema v1")
    for name, expected in (
        ("schema", FRESH_RESPONSE_SCHEMA),
        ("protocol", FRESH_PROTOCOL_SCHEMA),
    ):
        if value.get(name) != expected:
            raise FreshRunnerError(f"fresh response {name} differs")
    if not _is_u64(value.get("sequence")) or value.get("sequence") != 0:
        raise FreshRunnerError("fresh response sequence must be unsigned integer zero")
    observed_instance_id = value.get("ontology_instance_id")
    if not _is_sha256(observed_instance_id):
        raise FreshRunnerError("fresh response ontology_instance_id must be lowercase SHA-256")
    if observed_instance_id != ontology_instance_id:
        raise FreshRunnerError(
            "fresh response ontology_instance_id differs from completed acknowledgement"
        )
    result = value.get("result")
    if not isinstance(result, dict):
        raise FreshRunnerError("fresh response result must be an object")
    return cast(dict[str, Any], result)


def _read_frame_blocking(
    stream: BinaryIO,
    *,
    max_payload_bytes: int,
    name: str,
) -> bytes:
    header = stream.readline(MAX_FRAME_HEADER_BYTES + 2)
    if not header:
        raise FreshRunnerError(f"{name} input closed before its frame")
    if not header.endswith(b"\n") or len(header) - 1 > MAX_FRAME_HEADER_BYTES:
        raise FreshRunnerError(f"{name} frame header is invalid")
    raw_length = header[:-1]
    if not raw_length or any(value < ord("0") or value > ord("9") for value in raw_length):
        raise FreshRunnerError(f"{name} frame length is not decimal")
    if len(raw_length) > 1 and raw_length.startswith(b"0"):
        raise FreshRunnerError(f"{name} frame length is not canonical")
    payload_bytes = int(raw_length)
    if payload_bytes < 1 or payload_bytes > max_payload_bytes:
        raise FreshRunnerError(f"{name} frame exceeds its byte limit")
    payload = _read_exact(stream, payload_bytes)
    if len(payload) != payload_bytes or _read_exact(stream, 1) != b"\n":
        raise FreshRunnerError(f"{name} frame is truncated")
    return payload


def _write_frame_blocking(
    stream: BinaryIO,
    value: Mapping[str, object],
    *,
    max_payload_bytes: int,
    name: str,
) -> None:
    frame = _encode_frame(value, max_payload_bytes=max_payload_bytes, name=name)
    written = stream.write(frame)
    if written is not None and written != len(frame):
        raise FreshRunnerError(f"{name} frame write was truncated")
    stream.flush()


def _encode_frame(
    value: Mapping[str, object],
    *,
    max_payload_bytes: int,
    name: str,
) -> bytes:
    payload = _json_bytes(value, name)
    if len(payload) < 1 or len(payload) > max_payload_bytes:
        raise FreshRunnerError(f"{name} exceeds its byte limit")
    return str(len(payload)).encode("ascii") + b"\n" + payload + b"\n"


def _json_bytes(value: object, name: str) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise FreshRunnerError(f"{name} is not canonical JSON") from error


def _json_object(payload: bytes, name: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise FreshRunnerError(f"{name} is not valid JSON") from error
    if not isinstance(value, dict):
        raise FreshRunnerError(f"{name} must be a JSON object")
    return cast(dict[str, Any], value)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for name, item in pairs:
        if name in value:
            raise ValueError(f"duplicate JSON field: {name}")
        value[name] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _ontology_instance_id(pid: int) -> str:
    return hashlib.sha256(f"{pid}:0:0".encode("ascii")).hexdigest()


def _validate_command(command: Sequence[str]) -> None:
    if (
        isinstance(command, (str, bytes))
        or not command
        or any(not isinstance(value, str) or not value for value in command)
    ):
        raise ValueError("fresh runner command must contain nonempty strings")


def _positive_timeout(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"fresh runner {name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"fresh runner {name} must be finite and positive")
    return result


def _positive_limit(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"fresh runner {name} limit must be a positive integer")
    return value


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _SHA256_CHARACTERS for character in value)
    )


def _is_u64(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and 0 <= value <= 2**64 - 1


def _read_nonblocking(descriptor: int) -> bytes | None:
    try:
        return os.read(descriptor, _IO_CHUNK_BYTES)
    except BlockingIOError:
        return None


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


def _terminate_process(
    process: subprocess.Popen[bytes],
    *,
    process_group: OwnedProcessGroup | None = None,
) -> None:
    owned_group = process_group
    if owned_group is None and process.returncode is None:
        owned_group = capture_process_group(process)
    terminate_process(
        process,
        process_group=owned_group,
        grace_seconds=1.0,
    )


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            with suppress(OSError):
                stream.close()


__all__ = [
    "FRESH_COMPLETED_SCHEMA",
    "FRESH_PROTOCOL_SCHEMA",
    "FRESH_PUBLISH_SCHEMA",
    "FRESH_REQUEST_SCHEMA",
    "FRESH_RESPONSE_SCHEMA",
    "MAX_FRESH_CONTROL_FRAME_BYTES",
    "MAX_FRESH_ENVELOPE_OVERHEAD_BYTES",
    "FreshExchangeEvidence",
    "FreshRunnerError",
    "publish_fresh_result",
    "read_fresh_request",
    "run_fresh_subprocess",
]
