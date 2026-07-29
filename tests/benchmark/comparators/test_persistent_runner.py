from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

import tools.benchmark.comparators.persistent as persistent_module
import tools.benchmark.comparators.runner as runner_module
from tests.benchmark.comparators._persistent_runner_fixture import write_persistent_runner
from tools.benchmark.comparators.adapters import AdapterRequest, default_options, options_digest
from tools.benchmark.comparators.manifest import (
    ComparatorManifest,
    ComparatorPin,
    load_comparator_manifest,
)
from tools.benchmark.comparators.persistent import (
    PERSISTENT_COMPLETED_SCHEMA,
    PERSISTENT_EXECUTE_SCHEMA,
    PERSISTENT_PREPARED_SCHEMA,
    PERSISTENT_PROTOCOL_SCHEMA,
    PERSISTENT_PUBLISH_SCHEMA,
    PERSISTENT_REQUEST_SCHEMA,
    PersistentExternalRunner,
    PersistentRunnerError,
)
from tools.benchmark.comparators.rss_interval import (
    RSS_INTERVAL_SCHEMA,
    RssIntervalError,
    RssIntervalEvidence,
)
from tools.benchmark.manifest import generated_bytes, load_manifest

_TEST_HANDSHAKE_TIMEOUT_SECONDS = 2.0


def test_persistent_runner_reuses_one_verified_process_with_fresh_instances(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path)
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=1.0,
        shutdown_timeout_seconds=1.0,
    )
    request = _steady_request()

    first = runner.run(request)
    second = runner.run(request)
    audit = runner.close()

    assert first["status"] == second["status"] == "ok"
    first_transport = cast(dict[str, Any], first["transport_metrics"])
    second_transport = cast(dict[str, Any], second["transport_metrics"])
    assert first_transport["persistent_protocol"] == PERSISTENT_PROTOCOL_SCHEMA
    assert first_transport["persistent_runner_pid"] == second_transport["persistent_runner_pid"]
    assert first_transport["persistent_sequence"] == 0
    assert second_transport["persistent_sequence"] == 1
    assert first_transport["ontology_instance_id"] != second_transport["ontology_instance_id"]
    for transport in (first_transport, second_transport):
        rss_interval = cast(dict[str, Any], transport["rss_interval"])
        assert set(rss_interval) == {
            "schema",
            "source",
            "pid",
            "quiescent_current_bytes",
            "interval_peak_bytes",
            "incremental_peak_bytes",
            "sample_count",
            "maximum_sample_gap_ns",
        }
        assert rss_interval["schema"] == RSS_INTERVAL_SCHEMA
        assert rss_interval["pid"] == transport["persistent_runner_pid"]
        assert rss_interval["interval_peak_bytes"] >= rss_interval["quiescent_current_bytes"]
        assert rss_interval["incremental_peak_bytes"] == (
            rss_interval["interval_peak_bytes"] - rss_interval["quiescent_current_bytes"]
        )
        assert rss_interval["sample_count"] >= 2
        assert rss_interval["maximum_sample_gap_ns"] >= 0
    assert audit["status"] == "pass"
    assert audit["startup_ns"] > 0
    assert audit["request_count"] == audit["response_count"] == 2
    assert audit["unique_ontology_instance_count"] == 2
    assert audit["shutdown"] == "clean-exit"
    assert audit["handshake"]["artifact"]["runner_sha256"] == pin.runner_sha256
    assert audit["handshake"]["prepared_schema"] == PERSISTENT_PREPARED_SCHEMA
    assert audit["handshake"]["execute_schema"] == PERSISTENT_EXECUTE_SCHEMA
    assert audit["handshake"]["completed_schema"] == PERSISTENT_COMPLETED_SCHEMA
    assert audit["handshake"]["publish_schema"] == PERSISTENT_PUBLISH_SCHEMA


def test_persistent_runner_samples_only_after_the_prepared_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path, mode="rss-burst")
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=1.0,
        shutdown_timeout_seconds=1.0,
    )

    result = runner.run(_steady_request())
    audit = runner.close()
    interval = cast(dict[str, Any], result["transport_metrics"])["rss_interval"]

    assert result["status"] == "ok"
    assert interval["incremental_peak_bytes"] > 0
    assert interval["sample_count"] > 2
    assert audit["status"] == "pass"


def test_persistent_runner_stops_measurement_before_publish_and_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path)
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=1.0,
        shutdown_timeout_seconds=1.0,
    )
    request = _steady_request()
    events: list[str] = []
    wall_values = iter((100, 200))
    cpu_values = iter((10, 20))

    def wall_clock() -> int:
        events.append("wall-clock")
        return next(wall_values)

    def cpu_clock() -> int:
        events.append("cpu-clock")
        return next(cpu_values)

    class RecordingSampler:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def prepare(self) -> None:
            events.append("sampler-prepare")

        def start(self) -> None:
            events.append("sampler-start")

        def stop(self) -> RssIntervalEvidence:
            events.append("sampler-stop")
            return RssIntervalEvidence(
                source="test-reader",
                pid=self.pid,
                quiescent_current_bytes=100,
                interval_peak_bytes=120,
                incremental_peak_bytes=20,
                sample_count=2,
                maximum_sample_gap_ns=1,
            )

        def abort(self) -> None:
            events.append("sampler-abort")

    original_exchange = runner._exchange_frame
    original_validate_completed = runner._validate_completed

    def recording_exchange(
        value: dict[str, object],
        **kwargs: Any,
    ) -> tuple[bytes, int, int]:
        events.append(f"exchange:{value['schema']}")
        return original_exchange(value, **kwargs)

    def recording_validate_completed(value: dict[str, Any]) -> str:
        instance_id = original_validate_completed(value)
        events.append("completed-validated")
        return instance_id

    monkeypatch.setattr(time, "perf_counter_ns", wall_clock)
    monkeypatch.setattr(time, "process_time_ns", cpu_clock)
    monkeypatch.setattr(persistent_module, "SubprocessRssIntervalSampler", RecordingSampler)
    monkeypatch.setattr(runner, "_exchange_frame", recording_exchange)
    monkeypatch.setattr(runner, "_validate_completed", recording_validate_completed)

    result = runner.run(request)
    audit = runner.close()

    transport = cast(dict[str, Any], result["transport_metrics"])
    assert transport["parent_wall_ns"] == 100
    assert transport["parent_cpu_ns"] == 10
    clock_indexes = [
        index for index, event in enumerate(events) if event in {"wall-clock", "cpu-clock"}
    ]
    assert events.index("sampler-prepare") < min(clock_indexes)
    assert events.index(f"exchange:{PERSISTENT_REQUEST_SCHEMA}") < events.index("sampler-start")
    assert events.index("completed-validated") < max(clock_indexes)
    assert events.index("sampler-stop") > max(clock_indexes)
    assert events.index("sampler-stop") < events.index(f"exchange:{PERSISTENT_PUBLISH_SCHEMA}")
    assert "sampler-abort" not in events
    assert audit["status"] == "pass"


def test_persistent_runner_aborts_rss_monitor_when_execute_exchange_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path)
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
        shutdown_timeout_seconds=0.1,
    )
    events: list[str] = []
    exchanges = 0
    original_exchange = runner._exchange_frame

    def failing_exchange(
        value: dict[str, object],
        **kwargs: Any,
    ) -> tuple[bytes, int, int]:
        nonlocal exchanges
        exchanges += 1
        if exchanges == 2:
            raise PersistentRunnerError("injected execute exchange failure")
        return original_exchange(value, **kwargs)

    class RecordingSampler:
        def __init__(self, pid: int) -> None:
            assert pid == runner._process.pid

        def prepare(self) -> None:
            events.append("prepare")

        def start(self) -> None:
            events.append("start")

        def stop(self) -> RssIntervalEvidence:
            raise AssertionError("failed execute exchange must not accept RSS evidence")

        def abort(self) -> None:
            events.append("abort")

    monkeypatch.setattr(runner, "_exchange_frame", failing_exchange)
    monkeypatch.setattr(persistent_module, "SubprocessRssIntervalSampler", RecordingSampler)

    result = runner.run(_steady_request())
    audit = runner.close()

    assert result["status"] == "error"
    assert "injected execute exchange failure" in result["reason"]
    assert events == ["prepare", "start", "abort"]
    assert audit["status"] == "error"
    assert audit["shutdown"] == "terminated-after-error"
    assert runner._process.poll() is not None


def test_persistent_runner_aborts_prepared_rss_monitor_when_request_exchange_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path)
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
        shutdown_timeout_seconds=0.1,
    )
    events: list[str] = []

    def failing_exchange(
        _value: dict[str, object],
        **_kwargs: Any,
    ) -> tuple[bytes, int, int]:
        raise PersistentRunnerError("injected request exchange failure")

    class RecordingSampler:
        def __init__(self, pid: int) -> None:
            assert pid == runner._process.pid

        def prepare(self) -> None:
            events.append("prepare")

        def start(self) -> None:
            raise AssertionError("sampling must not start before the prepared boundary")

        def stop(self) -> RssIntervalEvidence:
            raise AssertionError("failed request exchange cannot produce RSS evidence")

        def abort(self) -> None:
            events.append("abort")

    monkeypatch.setattr(runner, "_exchange_frame", failing_exchange)
    monkeypatch.setattr(persistent_module, "SubprocessRssIntervalSampler", RecordingSampler)

    result = runner.run(_steady_request())
    audit = runner.close()

    assert result["status"] == "error"
    assert "injected request exchange failure" in result["reason"]
    assert events == ["prepare", "abort"]
    assert audit["status"] == "error"
    assert audit["shutdown"] == "terminated-after-error"
    assert runner._process.poll() is not None


def test_persistent_runner_aborts_rss_monitor_when_prepared_validation_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path, mode="prepared-wrong-schema")
    _set_launcher(monkeypatch, pin, executable)
    events: list[str] = []

    class RecordingSampler:
        def __init__(self, pid: int) -> None:
            assert pid > 0

        def prepare(self) -> None:
            events.append("prepare")

        def start(self) -> None:
            raise AssertionError("sampling must not start after an invalid prepared response")

        def stop(self) -> RssIntervalEvidence:
            raise AssertionError("invalid prepared response cannot produce RSS evidence")

        def abort(self) -> None:
            events.append("abort")

    monkeypatch.setattr(persistent_module, "SubprocessRssIntervalSampler", RecordingSampler)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
        shutdown_timeout_seconds=0.1,
    )

    result = runner.run(_steady_request())
    audit = runner.close()

    assert result["status"] == "error"
    assert "prepared schema differs" in result["reason"]
    assert events == ["prepare", "abort"]
    assert audit["status"] == "error"
    assert audit["shutdown"] == "terminated-after-error"
    assert runner._process.poll() is not None


def test_post_publish_serialization_allocation_is_excluded_from_rss_interval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path, mode="post-publish-serialization-burst")
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=5.0,
        shutdown_timeout_seconds=1.0,
    )

    result = runner.run(_steady_request())
    audit = runner.close()

    interval = cast(dict[str, Any], result["transport_metrics"])["rss_interval"]
    serialization_bytes = result["metrics"]["object_count"]
    assert serialization_bytes == 64 * 1024 * 1024
    assert interval["incremental_peak_bytes"] < serialization_bytes // 2
    assert audit["status"] == "pass"


def test_persistent_runner_fails_closed_when_rss_sampling_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path)
    _set_launcher(monkeypatch, pin, executable)
    events: list[str] = []

    class BrokenSampler:
        def __init__(self, pid: int) -> None:
            assert pid > 0

        def prepare(self) -> None:
            events.append("prepare")

        def start(self) -> None:
            raise RssIntervalError("injected RSS sampling failure")

        def abort(self) -> None:
            events.append("abort")

    monkeypatch.setattr(persistent_module, "SubprocessRssIntervalSampler", BrokenSampler)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
        shutdown_timeout_seconds=0.1,
    )

    result = runner.run(_steady_request())
    audit = runner.close()

    assert result["status"] == "error"
    assert "injected RSS sampling failure" in result["reason"]
    assert audit["status"] == "error"
    assert audit["shutdown"] == "terminated-after-error"
    assert runner._process.poll() is not None
    assert events == ["prepare", "abort"]


def test_persistent_runner_fails_closed_when_rss_sampling_cannot_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path)
    _set_launcher(monkeypatch, pin, executable)
    stop_calls = 0
    abort_calls = 0

    class BrokenSampler:
        def __init__(self, pid: int) -> None:
            assert pid > 0

        def prepare(self) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            nonlocal stop_calls
            stop_calls += 1
            raise RssIntervalError("injected RSS sampling completion failure")

        def abort(self) -> None:
            nonlocal abort_calls
            abort_calls += 1

    monkeypatch.setattr(persistent_module, "SubprocessRssIntervalSampler", BrokenSampler)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
        shutdown_timeout_seconds=0.1,
    )

    result = runner.run(_steady_request())
    audit = runner.close()

    assert result["status"] == "error"
    assert "injected RSS sampling completion failure" in result["reason"]
    assert audit["status"] == "error"
    assert audit["shutdown"] == "terminated-after-error"
    assert runner._process.poll() is not None
    assert stop_calls == 1
    assert abort_calls == 1


@pytest.mark.parametrize(
    ("mode", "message"),
    (
        ("crash", "closed stdout"),
        ("hang", "timed out"),
        ("oversize", "stdout limit"),
        ("stderr-oversize", "stderr limit"),
        ("malformed", "JSON object"),
        ("invalid-json", "valid JSON"),
        ("duplicate-json-field", "valid JSON"),
        ("cross-request", "another request"),
        ("boolean-sequence", "another request"),
        ("float-sequence", "another request"),
        ("result-float-thread-ceiling", "thread_ceiling differs"),
        ("response-wrong-schema", "response schema differs"),
        ("response-wrong-protocol", "response protocol differs"),
        ("response-invalid-instance", "must be lowercase SHA-256"),
        ("response-extra-field", "response fields differ"),
        ("response-missing-instance", "response fields differ"),
        ("early-clean-exit", "closed stdout"),
        ("partial-header", "timed out"),
        ("partial-body", "timed out"),
    ),
)
def test_persistent_runner_fails_closed_on_adversarial_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    message: str,
) -> None:
    pin, executable = _pinned_runner(tmp_path, mode=mode)
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.1,
        shutdown_timeout_seconds=0.1,
        max_stdout_bytes=64 * 1024,
        max_stderr_bytes=64,
    )

    result = runner.run(_steady_request())
    audit = runner.close()

    assert result["status"] == "error"
    assert message in result["reason"]
    assert audit["status"] == "error"
    assert audit["shutdown"] == "terminated-after-error"


@pytest.mark.parametrize(
    ("mode", "message"),
    (
        ("wrong-handshake", "handshake lane differs"),
        ("wrong-completed-handshake", "handshake completed_schema differs"),
        ("wrong-publish-handshake", "handshake publish_schema differs"),
        ("handshake-hang", "timed out"),
        ("handshake-partial-header", "timed out"),
        ("handshake-partial-body", "timed out"),
        ("handshake-stderr-oversize", "stderr limit"),
        ("forged-pid", "handshake pid differs"),
        ("float-pid", "handshake pid differs"),
        ("forged-artifact-sha", "artifact SHA-256 differs"),
        ("forged-runner-revision", "runner_revision differs"),
        ("forged-runner-sha", "runner_sha256 differs"),
        ("not-fresh", "fresh_ontology_per_request differs"),
        ("numeric-fresh", "fresh_ontology_per_request differs"),
        ("float-thread-ceiling", "artifact thread_ceiling differs"),
    ),
)
def test_persistent_runner_rejects_bad_or_missing_handshake_before_samples(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    message: str,
) -> None:
    pin, executable = _pinned_runner(tmp_path, mode=mode)
    _set_launcher(monkeypatch, pin, executable)

    with pytest.raises(PersistentRunnerError, match=message):
        PersistentExternalRunner.open(
            pin,
            handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
            timeout_seconds=0.05,
            shutdown_timeout_seconds=0.05,
            max_stderr_bytes=64,
        )


def test_persistent_pipe_setup_failure_reaps_child_and_closes_pipes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path)
    _set_launcher(monkeypatch, pin, executable)
    original_popen = persistent_module.subprocess.Popen
    started: list[subprocess.Popen[bytes]] = []

    def popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = original_popen(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(persistent_module.subprocess, "Popen", popen)
    monkeypatch.setattr(
        persistent_module.os,
        "set_blocking",
        lambda *_args: (_ for _ in ()).throw(OSError("injected pipe setup failure")),
    )

    with pytest.raises(PersistentRunnerError, match="injected pipe setup failure"):
        PersistentExternalRunner.open(
            pin,
            handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
            timeout_seconds=0.5,
        )

    assert len(started) == 1
    process = started[0]
    assert process.poll() is not None
    assert all(
        stream is not None and stream.closed
        for stream in (process.stdin, process.stdout, process.stderr)
    )


def test_persistent_process_group_capture_failure_reaps_child_and_closes_pipes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path)
    _set_launcher(monkeypatch, pin, executable)
    original_popen = persistent_module.subprocess.Popen
    started: list[subprocess.Popen[bytes]] = []

    def popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = original_popen(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(persistent_module.subprocess, "Popen", popen)
    monkeypatch.setattr(
        persistent_module,
        "capture_process_group",
        lambda _process: (_ for _ in ()).throw(RuntimeError("injected capture failure")),
    )

    with pytest.raises(PersistentRunnerError, match="process-group setup failed"):
        PersistentExternalRunner.open(
            pin,
            handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
            timeout_seconds=0.5,
        )

    assert len(started) == 1
    process = started[0]
    assert process.poll() is not None
    assert all(
        stream is not None and stream.closed
        for stream in (process.stdin, process.stdout, process.stderr)
    )


@pytest.mark.parametrize(
    ("mode", "message"),
    (
        ("prepared-wrong-schema", "prepared schema differs"),
        ("prepared-wrong-protocol", "prepared protocol differs"),
        ("prepared-wrong-sequence", "prepared sequence differs"),
        ("prepared-float-sequence", "prepared sequence differs"),
        ("prepared-wrong-pid", "prepared pid differs"),
        ("prepared-float-pid", "prepared pid differs"),
        ("prepared-extra-field", "prepared fields differ"),
    ),
)
def test_persistent_runner_rejects_invalid_prepared_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    message: str,
) -> None:
    pin, executable = _pinned_runner(tmp_path, mode=mode)
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
        shutdown_timeout_seconds=0.1,
    )

    result = runner.run(_steady_request())
    audit = runner.close()

    assert result["status"] == "error"
    assert message in result["reason"]
    assert audit["status"] == "error"
    assert audit["request_count"] == 0
    assert audit["response_count"] == 0


@pytest.mark.parametrize(
    ("mode", "message"),
    (
        ("completed-wrong-schema", "completed schema differs"),
        ("completed-wrong-protocol", "completed protocol differs"),
        ("completed-wrong-sequence", "completed sequence differs"),
        ("completed-bool-sequence", "completed sequence differs"),
        ("completed-float-sequence", "completed sequence differs"),
        ("completed-negative-sequence", "completed sequence differs"),
        ("completed-wrong-pid", "completed pid differs"),
        ("completed-bool-pid", "completed pid differs"),
        ("completed-float-pid", "completed pid differs"),
        ("completed-negative-pid", "completed pid differs"),
        ("completed-invalid-instance", "must be lowercase SHA-256"),
        ("completed-extra-field", "completed fields differ"),
        ("completed-missing-pid", "completed fields differ"),
    ),
)
def test_persistent_runner_rejects_invalid_completed_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    message: str,
) -> None:
    pin, executable = _pinned_runner(tmp_path, mode=mode)
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
        shutdown_timeout_seconds=0.1,
    )

    result = runner.run(_steady_request())
    audit = runner.close()

    assert result["status"] == "error"
    assert message in result["reason"]
    assert audit["status"] == "error"
    assert audit["request_count"] == 0
    assert audit["response_count"] == 0


def test_persistent_runner_binds_response_to_completed_instance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path, mode="response-instance-mismatch")
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
        shutdown_timeout_seconds=0.1,
    )

    result = runner.run(_steady_request())
    audit = runner.close()

    assert result["status"] == "error"
    assert "differs from completed acknowledgement" in result["reason"]
    assert audit["status"] == "error"
    assert audit["request_count"] == 0
    assert audit["response_count"] == 0


def test_persistent_runner_rejects_response_before_publish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path, mode="response-before-publish")
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
        shutdown_timeout_seconds=0.1,
    )

    result = runner.run(_steady_request())
    audit = runner.close()

    assert result["status"] == "error"
    assert "output" in result["reason"]
    assert audit["status"] == "error"
    assert audit["request_count"] == 0
    assert audit["response_count"] == 0


@pytest.mark.parametrize(
    ("mode", "outgoing_schema"),
    (
        ("prepared-before-request-write-completes", PERSISTENT_REQUEST_SCHEMA),
        ("completed-before-execute-write-completes", PERSISTENT_EXECUTE_SCHEMA),
        ("response-before-publish-write-completes", PERSISTENT_PUBLISH_SCHEMA),
    ),
)
def test_persistent_runner_rejects_response_before_corresponding_frame_is_fully_written(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    outgoing_schema: str,
) -> None:
    pin, executable = _pinned_runner(tmp_path, mode=mode)
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=2.0,
        shutdown_timeout_seconds=0.1,
    )
    original_exchange = runner._exchange_frame

    def oversized_exchange(
        value: dict[str, object],
        **kwargs: Any,
    ) -> tuple[bytes, int, int]:
        outgoing = dict(value)
        if outgoing.get("schema") == outgoing_schema:
            outgoing["ordering_probe"] = "x" * (2 * 1024 * 1024)
        return original_exchange(outgoing, **kwargs)

    monkeypatch.setattr(runner, "_exchange_frame", oversized_exchange)

    result = runner.run(_steady_request())
    audit = runner.close()

    assert result["status"] == "error"
    assert "before its request frame was fully written" in result["reason"]
    assert audit["status"] == "error"
    assert audit["request_count"] == 0
    assert audit["response_count"] == 0


@pytest.mark.parametrize("mode", ["extra-output", "late-output"])
def test_persistent_runner_rejects_extra_or_late_cross_frame_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
) -> None:
    pin, executable = _pinned_runner(tmp_path, mode=mode)
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
        shutdown_timeout_seconds=0.1,
    )

    first = runner.run(_steady_request())
    time.sleep(0.1)
    second = runner.run(_steady_request())
    audit = runner.close()

    assert "error" in {cast(str, first["status"]), cast(str, second["status"])}
    assert audit["status"] == "error"
    assert "output" in cast(str, audit["reason"])


def test_persistent_runner_rejects_reused_ontology_instance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path, mode="reuse-instance")
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
    )

    assert runner.run(_steady_request())["status"] == "ok"
    result = runner.run(_steady_request())
    audit = runner.close()

    assert result["status"] == "error"
    assert "reused an ontology instance" in result["reason"]
    assert audit["status"] == "error"


def test_persistent_runner_rejects_replayed_response_sequence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path, mode="replay-sequence")
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
    )

    assert runner.run(_steady_request())["status"] == "ok"
    result = runner.run(_steady_request())
    audit = runner.close()

    assert result["status"] == "error"
    assert "another request" in result["reason"]
    assert audit["response_count"] == 1
    assert audit["status"] == "error"


def test_persistent_runner_rejects_bytes_emitted_between_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path, mode="between-response-bytes")
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
    )

    assert runner.run(_steady_request())["status"] == "ok"
    time.sleep(0.1)
    result = runner.run(_steady_request())
    audit = runner.close()

    assert result["status"] == "error"
    assert "late or unsolicited output" in result["reason"]
    assert audit["status"] == "error"


def test_persistent_runner_shutdown_timeout_uses_termination_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path, mode="shutdown-hang")
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
        shutdown_timeout_seconds=0.05,
    )
    assert runner.run(_steady_request())["status"] == "ok"

    audit = runner.close()

    assert audit["status"] == "error"
    assert "timed out" in audit["reason"]
    assert runner._process.poll() is not None


@pytest.mark.parametrize(
    ("mode", "message"),
    (
        ("shutdown-float-sequence", "sequence differs"),
        ("shutdown-float-pid", "pid differs"),
    ),
)
def test_persistent_runner_rejects_noninteger_shutdown_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    message: str,
) -> None:
    pin, executable = _pinned_runner(tmp_path, mode=mode)
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
        shutdown_timeout_seconds=0.1,
    )
    assert runner.run(_steady_request())["status"] == "ok"

    audit = runner.close()

    assert audit["status"] == "error"
    assert message in audit["reason"]


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "fork"),
    reason="requires POSIX process groups",
)
def test_persistent_clean_exit_kills_surviving_descendants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path, mode="shutdown-clean-exit-descendant")
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
        shutdown_timeout_seconds=0.05,
    )
    assert runner.run(_steady_request())["status"] == "ok"

    audit = runner.close()

    assert audit["status"] == "error"
    assert "left descendant processes" in audit["reason"]
    assert runner._process.poll() == 0
    assert runner._process_group is not None
    assert runner._process_group.extinct is True


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process signals")
def test_persistent_runner_shutdown_escalates_to_kill_when_term_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path, mode="shutdown-ignore-term")
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
        shutdown_timeout_seconds=0.05,
    )
    assert runner.run(_steady_request())["status"] == "ok"

    audit = runner.close()

    assert audit["status"] == "error"
    assert "timed out" in audit["reason"]
    assert runner._process.returncode == -signal.SIGKILL


def test_persistent_runner_pid_guard_never_signals_from_a_forked_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path)
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
    )
    owner_pid = runner._owner_pid

    with monkeypatch.context() as context:
        context.setattr(cast(Any, persistent_module).os, "getpid", lambda: owner_pid + 1)
        result = runner.run(_steady_request())

    assert result["status"] == "error"
    assert "non-owner PID" in result["reason"]
    assert runner._process.poll() is None
    assert runner.close()["status"] == "error"
    assert runner._process.poll() is not None


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_persistent_runner_rejects_real_forked_client_without_killing_owner_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path)
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
    )
    read_descriptor, write_descriptor = os.pipe()

    child_pid = os.fork()
    if child_pid == 0:  # pragma: no branch - the child exits below
        os.close(read_descriptor)
        child_result = runner.run(_steady_request())
        child_audit = runner.close()
        child_evidence = json.dumps(
            {
                "result_status": child_result["status"],
                "result_reason": child_result["reason"],
                "shutdown": child_audit["shutdown"],
            },
            sort_keys=True,
        ).encode("utf-8")
        os.write(write_descriptor, child_evidence)
        os.close(write_descriptor)
        os._exit(0)

    os.close(write_descriptor)
    try:
        evidence_chunks: list[bytes] = []
        while chunk := os.read(read_descriptor, 4096):
            evidence_chunks.append(chunk)
        waited_pid, wait_status = os.waitpid(child_pid, 0)
    finally:
        os.close(read_descriptor)
    evidence_bytes = b"".join(evidence_chunks)
    parsed_evidence = cast(dict[str, Any], json.loads(evidence_bytes))

    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(wait_status) == 0
    assert parsed_evidence["result_status"] == "error"
    assert "non-owner PID" in parsed_evidence["result_reason"]
    assert parsed_evidence["shutdown"] == "fork-detached"
    assert runner._process.poll() is None
    assert runner.run(_steady_request())["status"] == "ok"
    assert runner.close()["status"] == "pass"


def test_persistent_runner_request_size_is_bounded_before_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path)
    _set_launcher(monkeypatch, pin, executable)
    runner = PersistentExternalRunner.open(
        pin,
        handshake_timeout_seconds=_TEST_HANDSHAKE_TIMEOUT_SECONDS,
        timeout_seconds=0.5,
        max_request_bytes=128,
    )

    result = runner.run(_steady_request())
    audit = runner.close()

    assert result["status"] == "error"
    assert "request exceeds" in result["reason"]
    assert audit["status"] == "error"


def test_baseline_uses_complete_pinned_persistent_runner_for_steady_lane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin, executable = _pinned_runner(tmp_path)
    _set_launcher(monkeypatch, pin, executable)
    manifest = load_comparator_manifest()
    manifest = replace(
        manifest,
        comparators=tuple(pin if value.id == pin.id else value for value in manifest.comparators),
    )

    def checked_manifest(**_: object) -> ComparatorManifest:
        return manifest

    monkeypatch.setattr(runner_module, "check_comparator_contract", checked_manifest)
    report = runner_module.run_comparator_baseline(
        corpus_ids=("generated-tiny-functional",),
        comparator_ids=(pin.id,),
        process_modes=("steady-process",),
        input_modes=("resident-bytes",),
        warmups=1,
        repetitions=2,
        seed=9,
    )
    row = cast(list[dict[str, Any]], report["lanes"])[0]
    lifecycle = cast(list[dict[str, Any]], report["persistent_runner_lifecycles"])[0]

    assert row["status"] == "ok"
    assert all(sample["status"] == "ok" for sample in row["samples"])
    assert report["not_run_required"] == []
    assert lifecycle["status"] == "pass"
    assert lifecycle["request_count"] == 3
    assert (
        lifecycle["runner_pid"] == row["samples"][0]["transport_metrics"]["persistent_runner_pid"]
    )


def _pinned_runner(
    directory: Path,
    *,
    mode: str = "normal",
) -> tuple[ComparatorPin, Path]:
    base = load_comparator_manifest().by_id("horned-owl-raw")
    prototype = replace(
        base,
        runner_pin_state="complete",
        runner_revision="persistent-test-runner-v1",
        runner_sha256="0" * 64,
    )
    executable = write_persistent_runner(directory, prototype, mode=mode)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    return replace(prototype, runner_sha256=digest), executable


def _set_launcher(
    monkeypatch: pytest.MonkeyPatch,
    pin: ComparatorPin,
    executable: Path,
) -> None:
    assert pin.launcher_env is not None
    monkeypatch.setenv(pin.launcher_env, str(executable))


def _steady_request() -> AdapterRequest:
    corpus = load_manifest().by_id("generated-tiny-functional")
    source = generated_bytes(corpus)
    options = default_options(corpus.format)
    return AdapterRequest(
        corpus_id=corpus.id,
        source=source,
        source_sha256=corpus.sha256,
        format=corpus.format,
        options=options,
        options_sha256=options_digest(options),
        input_mode="resident-bytes",
        process_mode="steady-process",
    )
