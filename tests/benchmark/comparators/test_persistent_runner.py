from __future__ import annotations

import hashlib
import json
import os
import signal
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
    PERSISTENT_PROTOCOL_SCHEMA,
    PersistentExternalRunner,
    PersistentRunnerError,
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
    assert first_transport["persistent_runner_pid"] == second_transport[
        "persistent_runner_pid"
    ]
    assert first_transport["persistent_sequence"] == 0
    assert second_transport["persistent_sequence"] == 1
    assert first_transport["ontology_instance_id"] != second_transport[
        "ontology_instance_id"
    ]
    assert audit["status"] == "pass"
    assert audit["startup_ns"] > 0
    assert audit["request_count"] == audit["response_count"] == 2
    assert audit["unique_ontology_instance_count"] == 2
    assert audit["shutdown"] == "clean-exit"
    assert audit["handshake"]["artifact"]["runner_sha256"] == pin.runner_sha256


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
    assert lifecycle["runner_pid"] == row["samples"][0]["transport_metrics"][
        "persistent_runner_pid"
    ]


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
