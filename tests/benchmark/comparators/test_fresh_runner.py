from __future__ import annotations

import hashlib
import io
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import tools.benchmark.comparators.fresh as fresh_module
import tools.benchmark.comparators.process_group as process_group_module
import tools.benchmark.comparators.worker as worker_module
from tests.benchmark.comparators._fresh_runner_fixture import write_fresh_runner
from tools.benchmark.comparators.fresh import (
    FRESH_COMPLETED_SCHEMA,
    FRESH_PROTOCOL_SCHEMA,
    FRESH_PUBLISH_SCHEMA,
    FRESH_REQUEST_SCHEMA,
    FRESH_RESPONSE_SCHEMA,
    FreshRunnerError,
    publish_fresh_result,
    read_fresh_request,
    run_fresh_subprocess,
)


def test_fresh_exchange_authenticates_completion_before_publishing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = write_fresh_runner(tmp_path)
    events: list[str] = []
    cpu_values = iter((10, 20))
    wall_values = iter((100, 200))
    original_validate = fresh_module._validate_completed
    original_reject = fresh_module._FreshProcess.reject_early_stdout
    original_encode = fresh_module._encode_frame

    def cpu_clock() -> int:
        events.append("cpu-clock")
        return next(cpu_values)

    def wall_clock() -> int:
        events.append("wall-clock")
        return next(wall_values)

    def validate(value: dict[str, Any], *, pid: int) -> str:
        result = original_validate(value, pid=pid)
        events.append("completed-validated")
        return result

    def reject(exchange: Any) -> None:
        events.append("early-output-check")
        original_reject(exchange)

    def encode(
        value: dict[str, object],
        *,
        max_payload_bytes: int,
        name: str,
    ) -> bytes:
        if name == "fresh publish":
            events.append("publish-constructed")
        return original_encode(
            value,
            max_payload_bytes=max_payload_bytes,
            name=name,
        )

    monkeypatch.setattr(fresh_module.time, "process_time_ns", cpu_clock)
    monkeypatch.setattr(fresh_module.time, "perf_counter_ns", wall_clock)
    monkeypatch.setattr(fresh_module, "_validate_completed", validate)
    monkeypatch.setattr(fresh_module._FreshProcess, "reject_early_stdout", reject)
    monkeypatch.setattr(fresh_module, "_encode_frame", encode)

    evidence = _run(executable, {"value": 7})

    assert evidence.result == {"accepted": {"value": 7}}
    assert evidence.parent_wall_ns == 100
    assert evidence.parent_cpu_ns == 10
    assert evidence.runner_pid > 0
    assert (
        evidence.ontology_instance_id
        == hashlib.sha256(f"{evidence.runner_pid}:0:0".encode("ascii")).hexdigest()
    )
    assert evidence.request_bytes > len(b'{"value":7}')
    assert evidence.stdout_bytes > len(json.dumps(evidence.result))
    assert evidence.stderr_bytes == 0
    endpoint = max(
        index for index, event in enumerate(events) if event in {"wall-clock", "cpu-clock"}
    )
    assert events.index("completed-validated") < endpoint
    assert endpoint < events.index("early-output-check")
    assert endpoint < events.index("publish-constructed")


@pytest.mark.parametrize(
    "mode",
    (
        "completed-wrong-schema",
        "completed-wrong-protocol",
        "completed-bool-sequence",
        "completed-float-sequence",
        "completed-negative-sequence",
        "completed-wrong-sequence",
        "completed-bool-pid",
        "completed-float-pid",
        "completed-negative-pid",
        "completed-wrong-pid",
        "completed-token-type",
        "completed-invalid-token",
        "completed-wrong-token",
        "completed-extra",
        "completed-missing",
        "completed-non-object",
        "completed-invalid-json",
        "completed-duplicate-json",
    ),
)
def test_fresh_exchange_rejects_malformed_completed_frames(
    tmp_path: Path,
    mode: str,
) -> None:
    executable = write_fresh_runner(tmp_path, mode=mode)

    with pytest.raises(FreshRunnerError):
        _run(executable, {"value": 1}, timeout=3.0)


@pytest.mark.parametrize(
    "mode",
    (
        "response-wrong-schema",
        "response-wrong-protocol",
        "response-bool-sequence",
        "response-float-sequence",
        "response-negative-sequence",
        "response-wrong-sequence",
        "response-token-type",
        "response-invalid-token",
        "response-wrong-token",
        "response-extra",
        "response-missing",
        "response-result-non-object",
        "response-duplicate-json",
    ),
)
def test_fresh_exchange_rejects_malformed_response_frames(
    tmp_path: Path,
    mode: str,
) -> None:
    executable = write_fresh_runner(tmp_path, mode=mode)

    with pytest.raises(FreshRunnerError):
        _run(executable, {"value": 1})


@pytest.mark.parametrize(
    "mode",
    (
        "response-before-publish",
        "partial-response-before-publish",
        "extra-output",
        "late-output",
        "nonzero-after-response",
        "hang-after-response",
    ),
)
def test_fresh_exchange_requires_publish_order_clean_exit_and_no_extra_output(
    tmp_path: Path,
    mode: str,
) -> None:
    executable = write_fresh_runner(tmp_path, mode=mode)

    with pytest.raises(FreshRunnerError):
        _run(executable, {"value": 1}, timeout=3.0)


@pytest.mark.parametrize(
    "mode",
    (
        "hang",
        "partial-header",
        "partial-body",
        "oversize-control",
        "stderr-oversize",
        "nondecimal-header",
        "noncanonical-header",
        "zero-payload",
        "missing-terminal-newline",
    ),
)
def test_fresh_exchange_bounds_every_pipe_phase(
    tmp_path: Path,
    mode: str,
) -> None:
    executable = write_fresh_runner(tmp_path, mode=mode)

    with pytest.raises(FreshRunnerError):
        _run(
            executable,
            {"value": 1},
            timeout=0.1,
            max_stderr_bytes=64,
        )


def test_fresh_pipe_setup_failure_reaps_child_and_closes_every_pipe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = write_fresh_runner(tmp_path, mode="hang")
    original_popen = fresh_module.subprocess.Popen
    started: list[subprocess.Popen[bytes]] = []

    def popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = original_popen(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(fresh_module.subprocess, "Popen", popen)
    monkeypatch.setattr(
        fresh_module.os,
        "set_blocking",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("injected pipe setup failure")),
    )
    monkeypatch.setattr(
        fresh_module.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(PermissionError("injected permission failure")),
    )

    with pytest.raises(FreshRunnerError, match="pipe setup failed"):
        _run(executable, {"value": 1})

    assert len(started) == 1
    process = started[0]
    assert process.poll() is not None
    assert all(
        stream is not None and stream.closed
        for stream in (process.stdin, process.stdout, process.stderr)
    )


def test_fresh_cleanup_does_not_touch_an_already_reaped_extinct_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    group_signals: list[tuple[int, int]] = []

    class ExitedProcess:
        pid = 123
        returncode = 0

        def poll(self) -> None:
            return None

        def wait(self, *, timeout: float) -> int:
            waits.append(timeout)
            return 0

    process_group = process_group_module.OwnedProcessGroup(
        pgid=123,
        owner_pid=os.getpid(),
        extinct=True,
    )
    monkeypatch.setattr(
        fresh_module.os,
        "killpg",
        lambda pgid, signal_number: group_signals.append((pgid, signal_number)),
    )

    fresh_module._terminate_process(ExitedProcess(), process_group=process_group)

    assert waits == []
    assert group_signals == []


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "fork"),
    reason="requires POSIX process groups",
)
def test_fresh_clean_exit_kills_surviving_descendants(tmp_path: Path) -> None:
    executable = write_fresh_runner(tmp_path, mode="clean-exit-with-descendant")

    with pytest.raises(FreshRunnerError, match="left descendant processes"):
        _run(executable, {"value": 1}, timeout=10.0)


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "fork"),
    reason="requires POSIX process groups",
)
def test_process_group_capture_survives_a_leader_that_exits_before_capture() -> None:
    child = (
        "import os,signal,time\n"
        "ready_read,ready_write=os.pipe()\n"
        "pid=os.fork()\n"
        "if pid == 0:\n"
        " os.close(ready_read)\n"
        " signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        " os.write(ready_write,b'x')\n"
        " os.close(ready_write)\n"
        " time.sleep(10)\n"
        " os._exit(0)\n"
        "os.close(ready_write)\n"
        "os.read(ready_read,1)\n"
        "print(pid,flush=True)\n"
        "os._exit(0)\n"
    )
    process = subprocess.Popen(
        (sys.executable, "-c", child),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    assert process.stdout is not None
    assert int(process.stdout.readline()) > 0
    time.sleep(0.05)

    process_group = process_group_module.capture_process_group(process)
    assert process_group is not None
    process_group_module.terminate_process(
        process,
        process_group=process_group,
        grace_seconds=0.05,
    )

    assert process.poll() == 0
    assert process_group.extinct is True


def test_extinct_process_group_token_is_never_signalled_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = subprocess.Popen(
        (sys.executable, "-c", "pass"),
        start_new_session=os.name == "posix",
    )
    process_group = process_group_module.capture_process_group(process)
    assert process.wait(timeout=1.0) == 0
    if process_group is None:
        return
    assert process_group.exists() is False
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_group_module.os,
        "killpg",
        lambda pgid, signal_number: signals.append((pgid, signal_number)),
    )

    process_group_module.terminate_process(
        process,
        process_group=process_group,
        grace_seconds=0.01,
    )

    assert signals == []


def test_fresh_request_envelope_has_separate_bounded_overhead(tmp_path: Path) -> None:
    executable = write_fresh_runner(tmp_path)
    request = {"value": "x" * 32}
    nested_bytes = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()

    evidence = _run(
        executable,
        request,
        max_request_bytes=len(nested_bytes),
    )

    assert evidence.result == {"accepted": request}
    assert evidence.request_bytes > len(nested_bytes)
    with pytest.raises(FreshRunnerError, match="nested adapter request exceeds"):
        _run(
            executable,
            request,
            max_request_bytes=len(nested_bytes) - 1,
        )


def test_fresh_runner_blocks_after_completed_until_publish_and_eof(tmp_path: Path) -> None:
    executable = write_fresh_runner(tmp_path)
    process = subprocess.Popen(
        (str(executable),),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    request = {
        "schema": FRESH_REQUEST_SCHEMA,
        "protocol": FRESH_PROTOCOL_SCHEMA,
        "sequence": 0,
        "request": {"value": 1},
    }
    process.stdin.write(_frame(request))
    process.stdin.flush()
    completed = _read_frame(process.stdout)

    readable, _, _ = select.select([process.stdout], [], [], 0.05)
    assert readable == []
    publish = {
        "schema": FRESH_PUBLISH_SCHEMA,
        "protocol": FRESH_PROTOCOL_SCHEMA,
        "sequence": 0,
        "pid": process.pid,
        "ontology_instance_id": completed["ontology_instance_id"],
    }
    process.stdin.write(_frame(publish))
    process.stdin.flush()
    readable, _, _ = select.select([process.stdout], [], [], 0.05)
    assert readable == []
    process.stdin.close()

    response = _read_frame(process.stdout)
    assert response["schema"] == FRESH_RESPONSE_SCHEMA
    assert response["ontology_instance_id"] == completed["ontology_instance_id"]
    assert process.wait(timeout=1.0) == 0


def test_fresh_runner_rejects_trailing_input_without_a_response(tmp_path: Path) -> None:
    executable = write_fresh_runner(tmp_path)
    process = subprocess.Popen(
        (str(executable),),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(
        _frame(
            {
                "schema": FRESH_REQUEST_SCHEMA,
                "protocol": FRESH_PROTOCOL_SCHEMA,
                "sequence": 0,
                "request": {"value": 1},
            }
        )
    )
    process.stdin.flush()
    completed = _read_frame(process.stdout)
    process.stdin.write(
        _frame(
            {
                "schema": FRESH_PUBLISH_SCHEMA,
                "protocol": FRESH_PROTOCOL_SCHEMA,
                "sequence": 0,
                "pid": process.pid,
                "ontology_instance_id": completed["ontology_instance_id"],
            }
        )
        + b"x"
    )
    process.stdin.close()

    assert process.stdout.read() == b""
    assert process.wait(timeout=1.0) == 5


def test_post_publish_serialization_is_outside_parent_completion_clock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = write_fresh_runner(tmp_path, mode="post-publish-serialization-burst")
    monkeypatch.setattr(fresh_module.time, "process_time_ns", iter((10, 20)).__next__)
    monkeypatch.setattr(fresh_module.time, "perf_counter_ns", iter((100, 200)).__next__)

    evidence = _run(executable, {"value": 1}, timeout=5.0)

    assert evidence.parent_wall_ns == 100
    assert evidence.parent_cpu_ns == 10
    assert evidence.result["serialization_bytes"] >= 64 * 1024 * 1024


def test_child_helpers_accept_short_pipe_reads_and_require_publish_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = {
        "schema": FRESH_REQUEST_SCHEMA,
        "protocol": FRESH_PROTOCOL_SCHEMA,
        "sequence": 0,
        "request": {"value": 1},
    }
    short_stream = _ShortReadBytesIO(_frame(request))

    assert read_fresh_request(max_request_bytes=128, stream=short_stream) == {"value": 1}

    pid = 123
    monkeypatch.setattr(fresh_module.os, "getpid", lambda: pid)
    instance_id = hashlib.sha256(b"123:0:0").hexdigest()
    publish = {
        "schema": FRESH_PUBLISH_SCHEMA,
        "protocol": FRESH_PROTOCOL_SCHEMA,
        "sequence": 0,
        "pid": pid,
        "ontology_instance_id": instance_id,
    }
    output = io.BytesIO()
    publish_fresh_result(
        {"value": 2},
        max_request_bytes=128,
        max_response_bytes=1024,
        input_stream=_ShortReadBytesIO(_frame(publish)),
        output_stream=output,
    )
    frames = _all_frames(output.getvalue())

    assert [value["schema"] for value in frames] == [
        FRESH_COMPLETED_SCHEMA,
        FRESH_RESPONSE_SCHEMA,
    ]
    assert frames[0]["ontology_instance_id"] == frames[1]["ontology_instance_id"]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema", "wrong"),
        ("protocol", "wrong"),
        ("sequence", True),
        ("sequence", 0.0),
        ("sequence", -1),
        ("sequence", 1),
        ("request", []),
    ),
)
def test_child_request_helper_rejects_strict_field_and_type_mismatches(
    field: str,
    value: object,
) -> None:
    request: dict[str, object] = {
        "schema": FRESH_REQUEST_SCHEMA,
        "protocol": FRESH_PROTOCOL_SCHEMA,
        "sequence": 0,
        "request": {"value": 1},
    }
    request[field] = value

    with pytest.raises(FreshRunnerError):
        read_fresh_request(max_request_bytes=1024, stream=io.BytesIO(_frame(request)))


@pytest.mark.parametrize("mutation", ("extra", "missing", "duplicate", "nonfinite"))
def test_child_request_helper_rejects_shape_and_json_ambiguity(mutation: str) -> None:
    request: dict[str, object] = {
        "schema": FRESH_REQUEST_SCHEMA,
        "protocol": FRESH_PROTOCOL_SCHEMA,
        "sequence": 0,
        "request": {"value": 1},
    }
    if mutation == "extra":
        request["extra"] = True
        wire = _frame(request)
    elif mutation == "missing":
        del request["request"]
        wire = _frame(request)
    elif mutation == "duplicate":
        raw = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        marker = b'"sequence":0'
        wire = _payload_frame(raw.replace(marker, marker + b"," + marker, 1))
    else:
        raw = (
            b'{"protocol":"pyowl-core/comparator-fresh-runner/v1",'
            b'"request":{"value":NaN},'
            b'"schema":"pyowl-core/comparator-fresh-request/v1","sequence":0}'
        )
        wire = _payload_frame(raw)

    with pytest.raises(FreshRunnerError):
        read_fresh_request(max_request_bytes=1024, stream=io.BytesIO(wire))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema", "wrong"),
        ("protocol", "wrong"),
        ("sequence", True),
        ("sequence", 0.0),
        ("sequence", -1),
        ("sequence", 1),
        ("pid", True),
        ("pid", 123.0),
        ("pid", -1),
        ("pid", 124),
        ("ontology_instance_id", 7),
        ("ontology_instance_id", "A" * 64),
        ("ontology_instance_id", "0" * 64),
    ),
)
def test_child_publish_helper_rejects_strict_mismatches_without_response(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    pid = 123
    monkeypatch.setattr(fresh_module.os, "getpid", lambda: pid)
    publish: dict[str, object] = {
        "schema": FRESH_PUBLISH_SCHEMA,
        "protocol": FRESH_PROTOCOL_SCHEMA,
        "sequence": 0,
        "pid": pid,
        "ontology_instance_id": hashlib.sha256(b"123:0:0").hexdigest(),
    }
    publish[field] = value
    output = io.BytesIO()

    with pytest.raises(FreshRunnerError):
        publish_fresh_result(
            {"value": 2},
            max_request_bytes=1024,
            max_response_bytes=1024,
            input_stream=io.BytesIO(_frame(publish)),
            output_stream=output,
        )

    assert [value["schema"] for value in _all_frames(output.getvalue())] == [FRESH_COMPLETED_SCHEMA]


@pytest.mark.parametrize("mutation", ("extra", "missing", "duplicate", "trailing"))
def test_child_publish_helper_rejects_shape_or_trailing_input_without_response(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    pid = 123
    monkeypatch.setattr(fresh_module.os, "getpid", lambda: pid)
    publish: dict[str, object] = {
        "schema": FRESH_PUBLISH_SCHEMA,
        "protocol": FRESH_PROTOCOL_SCHEMA,
        "sequence": 0,
        "pid": pid,
        "ontology_instance_id": hashlib.sha256(b"123:0:0").hexdigest(),
    }
    if mutation == "extra":
        publish["extra"] = True
        wire = _frame(publish)
    elif mutation == "missing":
        del publish["ontology_instance_id"]
        wire = _frame(publish)
    elif mutation == "duplicate":
        raw = json.dumps(publish, sort_keys=True, separators=(",", ":")).encode()
        marker = b'"sequence":0'
        wire = _payload_frame(raw.replace(marker, marker + b"," + marker, 1))
    else:
        wire = _frame(publish) + b"x"
    output = io.BytesIO()

    with pytest.raises(FreshRunnerError):
        publish_fresh_result(
            {"value": 2},
            max_request_bytes=1024,
            max_response_bytes=1024,
            input_stream=io.BytesIO(wire),
            output_stream=output,
        )

    assert [value["schema"] for value in _all_frames(output.getvalue())] == [FRESH_COMPLETED_SCHEMA]


def test_worker_finalizes_rss_and_child_cpu_after_full_result_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class RecordingMetrics(dict[str, int]):
        def __setitem__(self, key: str, value: int) -> None:
            events.append(f"set:{key}:{value}")
            super().__setitem__(key, value)

    result: dict[str, Any] = {
        "status": "ok",
        "completion_allocation": bytearray(1024),
        "metrics": RecordingMetrics(
            {
                "cpu_ns": 40,
                "rss_peak_before_bytes": 100,
                "rss_peak_after_bytes": 110,
                "rss_peak_increment_bytes": 10,
            }
        ),
    }
    usage = type("Usage", (), {"ru_maxrss": 180})()
    monkeypatch.setattr(worker_module.sys, "platform", "darwin")

    def getrusage(*_args: object) -> object:
        events.append("rss-sampled")
        return usage

    def process_time() -> int:
        events.append("cpu-sampled")
        return 900

    monkeypatch.setattr(worker_module.resource, "getrusage", getrusage)
    monkeypatch.setattr(worker_module.time, "process_time_ns", process_time)

    worker_module._finalize_completion_metrics(result)

    assert result["metrics"]["rss_peak_after_bytes"] == 180
    assert result["metrics"]["rss_peak_increment_bytes"] == 80
    assert result["metrics"]["startup_to_ready_cpu_ns"] == 900
    assert result["completion_allocation"]
    assert events.index("set:startup_to_ready_cpu_ns:0") < events.index("rss-sampled")
    assert events.index("rss-sampled") < events.index("cpu-sampled")
    assert events.index("cpu-sampled") < events.index("set:startup_to_ready_cpu_ns:900")


class _ShortReadBytesIO(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        return super().read(1 if size < 0 else min(size, 1))


def _run(
    executable: Path,
    request: dict[str, object],
    *,
    timeout: float = 5.0,
    max_request_bytes: int = 1024,
    max_stderr_bytes: int = 1024,
) -> fresh_module.FreshExchangeEvidence:
    return run_fresh_subprocess(
        (str(executable),),
        request,
        timeout=timeout,
        max_request_bytes=max_request_bytes,
        max_stdout_bytes=128 * 1024**2,
        max_stderr_bytes=max_stderr_bytes,
    )


def _frame(value: object) -> bytes:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return _payload_frame(payload)


def _payload_frame(payload: bytes) -> bytes:
    return str(len(payload)).encode() + b"\n" + payload + b"\n"


def _read_frame(stream: Any) -> dict[str, Any]:
    size = int(stream.readline().rstrip(b"\n"))
    payload = stream.read(size)
    assert stream.read(1) == b"\n"
    value = json.loads(payload)
    assert isinstance(value, dict)
    return value


def _all_frames(value: bytes) -> list[dict[str, Any]]:
    stream = io.BytesIO(value)
    frames: list[dict[str, Any]] = []
    while stream.tell() < len(value):
        frames.append(_read_frame(stream))
    return frames
