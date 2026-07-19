"""Audited persistent external-runner lifecycle for steady comparator lanes."""

from __future__ import annotations

import json
import math
import os
import selectors
import shlex
import subprocess
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from .adapters import (
    ADAPTER_REQUEST_SCHEMA,
    ADAPTER_RESULT_SCHEMA,
    DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
    MAX_SUBPROCESS_REQUEST_BYTES,
    MAX_SUBPROCESS_STDERR_BYTES,
    MAX_SUBPROCESS_STDOUT_BYTES,
    AdapterRequest,
    _error,
    _external_environment,
    _terminate_process,
    _validate_external_result,
    _verified_runner_command,
    sanitize_failure,
)
from .manifest import ComparatorPin

PERSISTENT_PROTOCOL_SCHEMA = "pyowl-core/comparator-persistent-runner/v1"
PERSISTENT_HANDSHAKE_SCHEMA = "pyowl-core/comparator-persistent-handshake/v1"
PERSISTENT_REQUEST_SCHEMA = "pyowl-core/comparator-persistent-request/v1"
PERSISTENT_RESPONSE_SCHEMA = "pyowl-core/comparator-persistent-response/v1"
PERSISTENT_SHUTDOWN_SCHEMA = "pyowl-core/comparator-persistent-shutdown/v1"
PERSISTENT_SHUTDOWN_ACK_SCHEMA = "pyowl-core/comparator-persistent-shutdown-ack/v1"
PERSISTENT_AUDIT_SCHEMA = "pyowl-core/comparator-persistent-lifecycle/v1"

MAX_PERSISTENT_HANDSHAKE_BYTES = 64 * 1024
MAX_FRAME_HEADER_BYTES = 32
_IO_CHUNK_BYTES = 64 * 1024
_SHA256_CHARACTERS = frozenset("0123456789abcdef")
_HANDSHAKE_FIELDS = frozenset(
    {
        "schema",
        "protocol",
        "lane",
        "implementation",
        "boundary",
        "pid",
        "request_schema",
        "result_schema",
        "fresh_ontology_per_request",
        "artifact",
    }
)
_RESPONSE_FIELDS = frozenset(
    {"schema", "protocol", "sequence", "ontology_instance_id", "result"}
)
_SHUTDOWN_ACK_FIELDS = frozenset({"schema", "protocol", "sequence", "pid"})
_ARTIFACT_FIELDS = frozenset(
    {
        "pin_state",
        "version",
        "revision",
        "artifact",
        "artifact_sha256",
        "features",
        "allocator",
        "thread_ceiling",
        "runner_revision",
        "runner_sha256",
    }
)


class PersistentRunnerUnavailable(RuntimeError):
    """A lane has no complete pinned persistent runner to start."""


class PersistentRunnerError(RuntimeError):
    """A pinned persistent runner violated its lifecycle or wire contract."""


class PersistentExternalRunner:
    """One verified subprocess serving every steady request for exactly one lane."""

    def __init__(
        self,
        pin: ComparatorPin,
        process: subprocess.Popen[bytes],
        *,
        handshake_timeout_seconds: float,
        timeout_seconds: float,
        shutdown_timeout_seconds: float,
        max_request_bytes: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        startup_started_ns: int,
    ) -> None:
        self.pin = pin
        self._process = process
        self._owner_pid = os.getpid()
        self._timeout_seconds = _positive_timeout(timeout_seconds, "response timeout")
        self._shutdown_timeout_seconds = _positive_timeout(
            shutdown_timeout_seconds, "shutdown timeout"
        )
        self._max_request_bytes = _positive_limit(max_request_bytes, "request")
        self._max_stdout_bytes = _positive_limit(max_stdout_bytes, "stdout")
        self._max_stderr_bytes = _positive_limit(max_stderr_bytes, "stderr")
        self._stdout_buffer = bytearray()
        self._stderr = bytearray()
        self._sequence = 0
        self._response_count = 0
        self._seen_ontology_instances: set[str] = set()
        self._failure_reason: str | None = None
        self._closed = False
        self._shutdown_state = "not-started"
        self._handshake: dict[str, Any] | None = None
        self._startup_ns = 0
        self._set_nonblocking_pipes()
        try:
            payload, _frame_bytes, _stderr_delta = self._receive_frame(
                timeout=_positive_timeout(
                    handshake_timeout_seconds, "handshake timeout"
                ),
                max_payload_bytes=MAX_PERSISTENT_HANDSHAKE_BYTES,
            )
            handshake = _json_object(payload, "persistent handshake")
            _validate_handshake(pin, process.pid, handshake)
            if process.poll() is not None:
                raise PersistentRunnerError("runner exited immediately after its handshake")
            self._handshake = handshake
            self._startup_ns = time.perf_counter_ns() - startup_started_ns
        except (OSError, TypeError, ValueError, PersistentRunnerError) as error:
            self._mark_failed(error)
            self._close_pipes()
            raise PersistentRunnerError(sanitize_failure(error)) from error

    @classmethod
    def open(
        cls,
        pin: ComparatorPin,
        *,
        handshake_timeout_seconds: float | None = None,
        timeout_seconds: float = DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
        shutdown_timeout_seconds: float = 5.0,
        max_request_bytes: int = MAX_SUBPROCESS_REQUEST_BYTES,
        max_stdout_bytes: int = MAX_SUBPROCESS_STDOUT_BYTES,
        max_stderr_bytes: int = MAX_SUBPROCESS_STDERR_BYTES,
        cwd: Path | None = None,
    ) -> PersistentExternalRunner:
        """Verify one command, start it once, and authenticate its handshake."""

        if pin.adapter != "external-command" or pin.launcher_env is None:
            raise ValueError(f"{pin.id}: not an external comparator")
        if not pin.artifact_is_runnable:
            raise PersistentRunnerUnavailable("artifact or external runner pin is pending")
        command_text = os.environ.get(pin.launcher_env)
        if not command_text:
            raise PersistentRunnerUnavailable(
                f"launcher environment {pin.launcher_env} is unset"
            )
        try:
            command = tuple(shlex.split(command_text))
        except ValueError as error:
            raise PersistentRunnerError(f"runner command is invalid: {error}") from error
        if not command:
            raise PersistentRunnerUnavailable(
                f"launcher environment {pin.launcher_env} is empty"
            )
        try:
            verified_command = _verified_runner_command(pin, command)
        except (OSError, ValueError) as error:
            raise PersistentRunnerError(str(error)) from error
        _positive_timeout(timeout_seconds, "response timeout")
        resolved_handshake_timeout = (
            timeout_seconds
            if handshake_timeout_seconds is None
            else handshake_timeout_seconds
        )
        _positive_timeout(resolved_handshake_timeout, "handshake timeout")
        _positive_timeout(shutdown_timeout_seconds, "shutdown timeout")
        _positive_limit(max_request_bytes, "request")
        _positive_limit(max_stdout_bytes, "stdout")
        _positive_limit(max_stderr_bytes, "stderr")
        startup_started_ns = time.perf_counter_ns()
        try:
            process = subprocess.Popen(
                verified_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=_external_environment(pin),
                bufsize=0,
                close_fds=True,
                start_new_session=os.name == "posix",
            )
        except OSError as error:
            raise PersistentRunnerError(f"persistent runner could not start: {error}") from error
        return cls(
            pin,
            process,
            handshake_timeout_seconds=resolved_handshake_timeout,
            timeout_seconds=timeout_seconds,
            shutdown_timeout_seconds=shutdown_timeout_seconds,
            max_request_bytes=max_request_bytes,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
            startup_started_ns=startup_started_ns,
        )

    def run(self, request: AdapterRequest) -> dict[str, Any]:
        """Send one steady request and return its fully validated adapter result."""

        if request.process_mode != "steady-process":
            raise ValueError("persistent runner accepts only steady-process requests")
        if self._failure_reason is not None:
            return _error(
                self.pin,
                request,
                PersistentRunnerError(self._failure_reason),
            )
        try:
            self._require_owner()
            self._require_active()
            if self._sequence > 2**64 - 1:
                raise PersistentRunnerError("persistent request sequence is exhausted")
            request_object = {
                "schema": PERSISTENT_REQUEST_SCHEMA,
                "protocol": PERSISTENT_PROTOCOL_SCHEMA,
                "sequence": self._sequence,
                "request": request.protocol_dict(self.pin),
            }
            parent_cpu_start = time.process_time_ns()
            parent_wall_start = time.perf_counter_ns()
            payload, request_frame_bytes, stderr_delta = self._exchange_frame(
                request_object,
                timeout=self._timeout_seconds,
                max_response_bytes=self._max_stdout_bytes,
            )
            parent_wall_ns = time.perf_counter_ns() - parent_wall_start
            parent_cpu_ns = time.process_time_ns() - parent_cpu_start
            response = _json_object(payload, "persistent response")
            result, ontology_instance_id = self._validate_response(request, response)
            self._seen_ontology_instances.add(ontology_instance_id)
            self._sequence += 1
            self._response_count += 1
            transport = result.setdefault("transport_metrics", {})
            if not isinstance(transport, dict):
                raise PersistentRunnerError("transport_metrics must be an object")
            transport.update(
                {
                    "parent_wall_ns": parent_wall_ns,
                    "parent_cpu_ns": parent_cpu_ns,
                    "request_bytes": request_frame_bytes,
                    "stdout_bytes": _frame_wire_size(payload),
                    "stderr_bytes": stderr_delta,
                    "persistent_protocol": PERSISTENT_PROTOCOL_SCHEMA,
                    "persistent_sequence": self._sequence - 1,
                    "persistent_runner_pid": self._process.pid,
                    "ontology_instance_id": ontology_instance_id,
                }
            )
            return result
        except (OSError, TypeError, ValueError, PersistentRunnerError) as error:
            self._mark_failed(error)
            return _error(self.pin, request, PersistentRunnerError(str(error)))

    def close(self) -> dict[str, Any]:
        """Perform the versioned shutdown exchange and return lifecycle evidence."""

        if self._closed:
            return self.audit()
        if os.getpid() != self._owner_pid:
            self._failure_reason = "persistent runner used from a non-owner PID"
            self._shutdown_state = "fork-detached"
            self._closed = True
            self._close_pipes()
            return self.audit()
        try:
            if self._failure_reason is None and self._process.poll() is None:
                shutdown_sequence = self._sequence
                payload, _frame_bytes, _stderr_delta = self._exchange_frame(
                    {
                        "schema": PERSISTENT_SHUTDOWN_SCHEMA,
                        "protocol": PERSISTENT_PROTOCOL_SCHEMA,
                        "sequence": shutdown_sequence,
                    },
                    timeout=self._shutdown_timeout_seconds,
                    max_response_bytes=MAX_PERSISTENT_HANDSHAKE_BYTES,
                    allow_process_exit=True,
                )
                _validate_shutdown_ack(
                    _json_object(payload, "persistent shutdown acknowledgement"),
                    sequence=shutdown_sequence,
                    pid=self._process.pid,
                )
                self._shutdown_state = "acknowledged"
                self._close_stdin()
                self._wait_for_clean_exit(self._shutdown_timeout_seconds)
                self._shutdown_state = "clean-exit"
            elif self._failure_reason is None:
                raise PersistentRunnerError(
                    f"runner exited before shutdown with code {self._process.returncode}"
                )
        except (OSError, TypeError, ValueError, PersistentRunnerError) as error:
            self._mark_failed(error)
        finally:
            if self._process.poll() is None:
                _terminate_process(self._process)
                if self._failure_reason is None:
                    self._failure_reason = "runner required termination after shutdown"
            self._closed = True
            self._close_pipes()
        return self.audit()

    def audit(self) -> dict[str, Any]:
        status = "pass" if self._closed and self._failure_reason is None else "error"
        return {
            "schema": PERSISTENT_AUDIT_SCHEMA,
            "lane": self.pin.id,
            "protocol": PERSISTENT_PROTOCOL_SCHEMA,
            "status": status,
            "reason": self._failure_reason,
            "owner_pid": self._owner_pid,
            "runner_pid": self._process.pid,
            "startup_ns": self._startup_ns,
            "request_count": self._sequence,
            "response_count": self._response_count,
            "unique_ontology_instance_count": len(self._seen_ontology_instances),
            "stderr_bytes": len(self._stderr),
            "shutdown": self._shutdown_state,
            "handshake": self._handshake,
        }

    def _validate_response(
        self,
        request: AdapterRequest,
        response: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str]:
        if set(response) != _RESPONSE_FIELDS:
            raise PersistentRunnerError("persistent response fields differ from schema v1")
        if response.get("schema") != PERSISTENT_RESPONSE_SCHEMA:
            raise PersistentRunnerError("persistent response schema differs")
        if response.get("protocol") != PERSISTENT_PROTOCOL_SCHEMA:
            raise PersistentRunnerError("persistent response protocol differs")
        response_sequence = response.get("sequence")
        if not _is_u64(response_sequence) or response_sequence != self._sequence:
            raise PersistentRunnerError("persistent response belongs to another request")
        ontology_instance_id = response.get("ontology_instance_id")
        if not _is_sha256(ontology_instance_id):
            raise PersistentRunnerError("ontology_instance_id must be lowercase SHA-256")
        instance_id = cast(str, ontology_instance_id)
        if instance_id in self._seen_ontology_instances:
            raise PersistentRunnerError("persistent runner reused an ontology instance")
        result_value = response.get("result")
        if not isinstance(result_value, dict):
            raise PersistentRunnerError("persistent response result must be an object")
        result = cast(dict[str, Any], result_value)
        _validate_external_result(self.pin, request, result)
        if result.get("status") != "ok":
            result["reason"] = sanitize_failure(
                result.get("reason", "persistent external adapter failed")
            )
        return result, instance_id

    def _exchange_frame(
        self,
        value: Mapping[str, object],
        *,
        timeout: float,
        max_response_bytes: int,
        allow_process_exit: bool = False,
    ) -> tuple[bytes, int, int]:
        self._preflight()
        outgoing = _encode_frame(value, max_payload_bytes=self._max_request_bytes)
        stderr_before = len(self._stderr)
        payload = self._pump(
            outgoing=outgoing,
            timeout=timeout,
            max_payload_bytes=max_response_bytes,
        )
        self._reject_immediate_extra_stdout()
        if not allow_process_exit and self._process.poll() is not None:
            raise PersistentRunnerError(
                f"persistent runner exited after response with code {self._process.returncode}"
            )
        return payload, len(outgoing), len(self._stderr) - stderr_before

    def _receive_frame(
        self,
        *,
        timeout: float,
        max_payload_bytes: int,
    ) -> tuple[bytes, int, int]:
        stderr_before = len(self._stderr)
        payload = self._pump(
            outgoing=None,
            timeout=timeout,
            max_payload_bytes=max_payload_bytes,
        )
        self._reject_immediate_extra_stdout()
        return payload, _frame_wire_size(payload), len(self._stderr) - stderr_before

    def _pump(
        self,
        *,
        outgoing: bytes | None,
        timeout: float,
        max_payload_bytes: int,
    ) -> bytes:
        self._require_owner()
        self._require_active()
        stdin = self._required_pipe(self._process.stdin, "stdin")
        stdout = self._required_pipe(self._process.stdout, "stdout")
        stderr = self._required_pipe(self._process.stderr, "stderr")
        deadline = time.monotonic() + _positive_timeout(timeout, "frame timeout")
        write_offset = 0
        response: bytes | None = None
        stdout_eof = False
        with selectors.DefaultSelector() as selector:
            selector.register(stdout, selectors.EVENT_READ, "stdout")
            selector.register(stderr, selectors.EVENT_READ, "stderr")
            if outgoing is not None:
                selector.register(stdin, selectors.EVENT_WRITE, "stdin")
            while response is None or (outgoing is not None and write_offset < len(outgoing)):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PersistentRunnerError("persistent runner response timed out")
                events = selector.select(min(remaining, 0.1))
                if not events:
                    if self._process.poll() is not None:
                        raise PersistentRunnerError(
                            f"persistent runner crashed with code {self._process.returncode}"
                        )
                    continue
                for key, _events in events:
                    if key.data == "stdin":
                        assert outgoing is not None
                        try:
                            written = os.write(stdin.fileno(), outgoing[write_offset:])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError as error:
                            raise PersistentRunnerError("persistent runner closed stdin") from error
                        if written <= 0:
                            raise PersistentRunnerError("persistent request write made no progress")
                        write_offset += written
                        if write_offset == len(outgoing):
                            selector.unregister(stdin)
                    elif key.data == "stdout":
                        chunk = _read_nonblocking(stdout.fileno())
                        if chunk == b"":
                            stdout_eof = True
                        elif chunk is not None:
                            if response is not None:
                                raise PersistentRunnerError(
                                    "persistent runner emitted extra response output"
                                )
                            self._stdout_buffer.extend(chunk)
                            response = self._extract_frame(max_payload_bytes)
                    else:
                        chunk = _read_nonblocking(stderr.fileno())
                        if chunk not in {None, b""}:
                            self._append_stderr(chunk)
                if stdout_eof and response is None:
                    raise PersistentRunnerError("persistent runner closed stdout before response")
        assert response is not None
        return response

    def _extract_frame(self, max_payload_bytes: int) -> bytes | None:
        newline = self._stdout_buffer.find(b"\n")
        if newline < 0:
            if len(self._stdout_buffer) > MAX_FRAME_HEADER_BYTES:
                raise PersistentRunnerError("persistent frame header exceeds its byte limit")
            return None
        if newline == 0 or newline > MAX_FRAME_HEADER_BYTES:
            raise PersistentRunnerError("persistent frame header is invalid")
        header = bytes(self._stdout_buffer[:newline])
        if any(value < ord("0") or value > ord("9") for value in header):
            raise PersistentRunnerError("persistent frame length is not decimal")
        if len(header) > 1 and header.startswith(b"0"):
            raise PersistentRunnerError("persistent frame length is not canonical")
        payload_bytes = int(header)
        if payload_bytes > max_payload_bytes:
            raise PersistentRunnerError("persistent response exceeds its stdout limit")
        total = newline + 1 + payload_bytes + 1
        if len(self._stdout_buffer) < total:
            if len(self._stdout_buffer) > max_payload_bytes + MAX_FRAME_HEADER_BYTES + 2:
                raise PersistentRunnerError("persistent response exceeds its stdout limit")
            return None
        if self._stdout_buffer[total - 1] != ord("\n"):
            raise PersistentRunnerError("persistent frame lacks its terminal newline")
        payload = bytes(self._stdout_buffer[newline + 1 : total - 1])
        del self._stdout_buffer[:total]
        if self._stdout_buffer:
            raise PersistentRunnerError("persistent runner emitted extra response output")
        return payload

    def _preflight(self) -> None:
        self._require_owner()
        self._require_active()
        if self._stdout_buffer:
            raise PersistentRunnerError("persistent runner retained extra response output")
        stdout = self._required_pipe(self._process.stdout, "stdout")
        stderr = self._required_pipe(self._process.stderr, "stderr")
        while True:
            chunk = _read_nonblocking(stdout.fileno())
            if chunk is None:
                break
            if chunk == b"":
                raise PersistentRunnerError("persistent runner closed stdout between requests")
            raise PersistentRunnerError("persistent runner emitted late or unsolicited output")
        self._drain_stderr(stderr.fileno())
        if self._process.poll() is not None:
            raise PersistentRunnerError(
                f"persistent runner exited between requests with code {self._process.returncode}"
            )

    def _reject_immediate_extra_stdout(self) -> None:
        stdout = self._required_pipe(self._process.stdout, "stdout")
        while True:
            chunk = _read_nonblocking(stdout.fileno())
            if chunk is None:
                return
            if chunk == b"":
                return
            raise PersistentRunnerError("persistent runner emitted extra response output")

    def _drain_stderr(self, descriptor: int) -> None:
        while True:
            chunk = _read_nonblocking(descriptor)
            if chunk in {None, b""}:
                return
            self._append_stderr(chunk)

    def _append_stderr(self, chunk: bytes) -> None:
        remaining = self._max_stderr_bytes - len(self._stderr)
        if len(chunk) > remaining:
            if remaining > 0:
                self._stderr.extend(chunk[:remaining])
            raise PersistentRunnerError("persistent runner exceeded its stderr limit")
        self._stderr.extend(chunk)

    def _wait_for_clean_exit(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        stdout = self._required_pipe(self._process.stdout, "stdout")
        stderr = self._required_pipe(self._process.stderr, "stderr")
        while self._process.poll() is None:
            if time.monotonic() >= deadline:
                raise PersistentRunnerError("persistent runner shutdown timed out")
            chunk = _read_nonblocking(stdout.fileno())
            if chunk not in {None, b""}:
                raise PersistentRunnerError("persistent runner emitted output after shutdown ack")
            self._drain_stderr(stderr.fileno())
            time.sleep(0.005)
        self._drain_stderr(stderr.fileno())
        chunk = _read_nonblocking(stdout.fileno())
        if chunk not in {None, b""}:
            raise PersistentRunnerError("persistent runner emitted output after shutdown ack")
        if self._process.returncode != 0:
            raise PersistentRunnerError(
                f"persistent runner shutdown exited {self._process.returncode}"
            )

    def _mark_failed(self, error: BaseException) -> None:
        if self._failure_reason is None:
            detail = sanitize_failure(f"{type(error).__name__}: {error}")
            if self._stderr:
                detail = sanitize_failure(
                    f"{detail}; stderr: {self._stderr.decode('utf-8', 'replace')}"
                )
            self._failure_reason = detail
        self._shutdown_state = "terminated-after-error"
        if os.getpid() == self._owner_pid and self._process.poll() is None:
            _terminate_process(self._process)

    def _set_nonblocking_pipes(self) -> None:
        for name, stream in (
            ("stdin", self._process.stdin),
            ("stdout", self._process.stdout),
            ("stderr", self._process.stderr),
        ):
            pipe = self._required_pipe(stream, name)
            os.set_blocking(pipe.fileno(), False)

    def _require_owner(self) -> None:
        if os.getpid() != self._owner_pid:
            raise PersistentRunnerError("persistent runner used from a non-owner PID")

    def _require_active(self) -> None:
        if self._closed:
            raise PersistentRunnerError("persistent runner lifecycle is closed")
        if self._failure_reason is not None:
            raise PersistentRunnerError(self._failure_reason)
        if self._process.poll() is not None:
            raise PersistentRunnerError(
                f"persistent runner is not active (code {self._process.returncode})"
            )

    @staticmethod
    def _required_pipe(
        stream: Any,
        name: str,
    ) -> Any:
        if stream is None:
            raise PersistentRunnerError(f"persistent runner lacks {name} pipe")
        return stream

    def _close_stdin(self) -> None:
        if self._process.stdin is not None and not self._process.stdin.closed:
            self._process.stdin.close()

    def _close_pipes(self) -> None:
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            if stream is not None and not stream.closed:
                with suppress(OSError):
                    stream.close()


def unavailable_lifecycle_audit(
    pin: ComparatorPin,
    *,
    status: str,
    reason: object,
) -> dict[str, Any]:
    if status not in {"not-run", "error"}:
        raise ValueError("unavailable lifecycle status must be not-run or error")
    return {
        "schema": PERSISTENT_AUDIT_SCHEMA,
        "lane": pin.id,
        "protocol": PERSISTENT_PROTOCOL_SCHEMA,
        "status": status,
        "reason": sanitize_failure(reason),
        "owner_pid": os.getpid(),
        "runner_pid": None,
        "startup_ns": None,
        "request_count": 0,
        "response_count": 0,
        "unique_ontology_instance_count": 0,
        "stderr_bytes": 0,
        "shutdown": "not-started",
        "handshake": None,
    }


def _validate_handshake(
    pin: ComparatorPin,
    pid: int,
    value: Mapping[str, Any],
) -> None:
    if set(value) != _HANDSHAKE_FIELDS:
        raise PersistentRunnerError("persistent handshake fields differ from schema v1")
    expected_handshake: tuple[tuple[str, object], ...] = (
        ("schema", PERSISTENT_HANDSHAKE_SCHEMA),
        ("protocol", PERSISTENT_PROTOCOL_SCHEMA),
        ("lane", pin.id),
        ("implementation", pin.implementation),
        ("boundary", pin.boundary),
        ("request_schema", ADAPTER_REQUEST_SCHEMA),
        ("result_schema", ADAPTER_RESULT_SCHEMA),
    )
    for name, expected in expected_handshake:
        if value.get(name) != expected:
            raise PersistentRunnerError(f"persistent handshake {name} differs")
    observed_pid = value.get("pid")
    if not _is_u64(observed_pid) or observed_pid != pid:
        raise PersistentRunnerError("persistent handshake pid differs")
    if value.get("fresh_ontology_per_request") is not True:
        raise PersistentRunnerError(
            "persistent handshake fresh_ontology_per_request differs"
        )
    artifact = value.get("artifact")
    if not isinstance(artifact, Mapping) or set(artifact) != _ARTIFACT_FIELDS:
        raise PersistentRunnerError("persistent handshake artifact fields differ")
    expected_artifact: tuple[tuple[str, object], ...] = (
        ("pin_state", pin.pin_state),
        ("version", pin.version),
        ("revision", pin.revision),
        ("artifact", pin.artifact),
        ("features", list(pin.features)),
        ("allocator", pin.allocator),
        ("runner_revision", pin.runner_revision),
        ("runner_sha256", pin.runner_sha256),
    )
    for name, expected in expected_artifact:
        if artifact.get(name) != expected:
            raise PersistentRunnerError(f"persistent handshake artifact {name} differs")
    observed_thread_ceiling = artifact.get("thread_ceiling")
    if (
        not _is_u64(observed_thread_ceiling)
        or observed_thread_ceiling != pin.thread_ceiling
    ):
        raise PersistentRunnerError(
            "persistent handshake artifact thread_ceiling differs"
        )
    observed_artifact_sha256 = artifact.get("artifact_sha256")
    if not _is_sha256(observed_artifact_sha256):
        raise PersistentRunnerError("persistent handshake lacks artifact SHA-256")
    if pin.artifact_sha256 is not None and observed_artifact_sha256 != pin.artifact_sha256:
        raise PersistentRunnerError("persistent handshake artifact SHA-256 differs")


def _validate_shutdown_ack(
    value: Mapping[str, Any],
    *,
    sequence: int,
    pid: int,
) -> None:
    if set(value) != _SHUTDOWN_ACK_FIELDS:
        raise PersistentRunnerError("persistent shutdown acknowledgement fields differ")
    for name, expected in (
        ("schema", PERSISTENT_SHUTDOWN_ACK_SCHEMA),
        ("protocol", PERSISTENT_PROTOCOL_SCHEMA),
    ):
        if value.get(name) != expected:
            raise PersistentRunnerError(f"persistent shutdown acknowledgement {name} differs")
    observed_sequence = value.get("sequence")
    if not _is_u64(observed_sequence) or observed_sequence != sequence:
        raise PersistentRunnerError(
            "persistent shutdown acknowledgement sequence differs"
        )
    observed_pid = value.get("pid")
    if not _is_u64(observed_pid) or observed_pid != pid:
        raise PersistentRunnerError("persistent shutdown acknowledgement pid differs")


def _encode_frame(value: Mapping[str, object], *, max_payload_bytes: int) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > max_payload_bytes:
        raise PersistentRunnerError("persistent request exceeds its byte limit")
    return str(len(payload)).encode("ascii") + b"\n" + payload + b"\n"


def _frame_wire_size(payload: bytes) -> int:
    return len(str(len(payload))) + len(payload) + 2


def _json_object(payload: bytes, name: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PersistentRunnerError(f"{name} is not valid JSON") from error
    if not isinstance(value, dict):
        raise PersistentRunnerError(f"{name} must be a JSON object")
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


def _read_nonblocking(descriptor: int) -> bytes | None:
    try:
        return os.read(descriptor, _IO_CHUNK_BYTES)
    except BlockingIOError:
        return None


def _positive_timeout(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"persistent {name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"persistent {name} must be finite and positive")
    return result


def _positive_limit(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"persistent {name} limit must be a positive integer")
    return value


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _SHA256_CHARACTERS for character in value)
    )


def _is_u64(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 0 <= value <= 2**64 - 1
    )


__all__ = [
    "MAX_FRAME_HEADER_BYTES",
    "MAX_PERSISTENT_HANDSHAKE_BYTES",
    "PERSISTENT_AUDIT_SCHEMA",
    "PERSISTENT_HANDSHAKE_SCHEMA",
    "PERSISTENT_PROTOCOL_SCHEMA",
    "PERSISTENT_REQUEST_SCHEMA",
    "PERSISTENT_RESPONSE_SCHEMA",
    "PERSISTENT_SHUTDOWN_ACK_SCHEMA",
    "PERSISTENT_SHUTDOWN_SCHEMA",
    "PersistentExternalRunner",
    "PersistentRunnerError",
    "PersistentRunnerUnavailable",
    "unavailable_lifecycle_audit",
]
