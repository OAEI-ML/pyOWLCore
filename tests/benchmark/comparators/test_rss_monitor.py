from __future__ import annotations

import multiprocessing
import os
from typing import Any, cast

import pytest

import tools.benchmark.comparators.rss_monitor as rss_monitor_module
from tools.benchmark.comparators.rss_interval import (
    RSS_INTERVAL_SCHEMA,
    RssIntervalError,
    RssIntervalEvidence,
)
from tools.benchmark.comparators.rss_monitor import SubprocessRssIntervalSampler


def test_subprocess_sampler_observes_and_reaps_current_process_monitor() -> None:
    sampler = SubprocessRssIntervalSampler(os.getpid(), timeout_seconds=5.0)
    helper_pids: set[int] = set()
    try:
        sampler.start()
        helper_pids = _monitor_child_pids()
        assert helper_pids
        evidence = sampler.stop()
    finally:
        sampler.abort()

    assert evidence.to_dict()["schema"] == RSS_INTERVAL_SCHEMA
    assert evidence.pid == os.getpid()
    assert evidence.sample_count >= 2
    assert evidence.interval_peak_bytes >= evidence.quiescent_current_bytes
    assert evidence.incremental_peak_bytes == (
        evidence.interval_peak_bytes - evidence.quiescent_current_bytes
    )
    assert helper_pids.isdisjoint(_monitor_child_pids())


def test_subprocess_sampler_lifecycle_is_single_use() -> None:
    sampler = SubprocessRssIntervalSampler(os.getpid(), timeout_seconds=5.0)
    try:
        sampler.prepare()
        with pytest.raises(RssIntervalError, match="prepared twice"):
            sampler.prepare()
        sampler.start()
        with pytest.raises(RssIntervalError, match="started twice"):
            sampler.start()
        sampler.stop()
    finally:
        sampler.abort()
    with pytest.raises(RssIntervalError, match="stopped twice"):
        sampler.stop()


def test_subprocess_sampler_abort_before_start_is_idempotent_and_terminal() -> None:
    sampler = SubprocessRssIntervalSampler(os.getpid())

    sampler.abort()
    sampler.abort()

    with pytest.raises(RssIntervalError, match="cannot prepare after it was aborted"):
        sampler.prepare()
    with pytest.raises(RssIntervalError, match="cannot start after it was aborted"):
        sampler.start()


def test_subprocess_sampler_reaps_helper_after_sampling_startup_failure() -> None:
    missing_pid = 2**31 - 1
    helper_name = f"pyowl-rss-monitor-{missing_pid}"
    sampler = SubprocessRssIntervalSampler(missing_pid, timeout_seconds=5.0)

    try:
        with pytest.raises(RssIntervalError, match="RSS"):
            sampler.start()
    finally:
        sampler.abort()

    assert helper_name not in {child.name for child in multiprocessing.active_children()}


@pytest.mark.parametrize("pid", [0, -1, True, 1.0])
def test_subprocess_sampler_rejects_invalid_pid_before_allocating_resources(pid: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        SubprocessRssIntervalSampler(pid)  # type: ignore[arg-type]


@pytest.mark.parametrize("sample_interval", [0, -1, True, 1.01, float("nan"), "fast"])
def test_subprocess_sampler_rejects_invalid_sample_interval(sample_interval: object) -> None:
    with pytest.raises(ValueError, match="sample interval"):
        SubprocessRssIntervalSampler(
            os.getpid(),
            sample_interval_seconds=sample_interval,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("timeout", [0, -1, True, 60.01, float("nan"), "slow"])
def test_subprocess_sampler_rejects_invalid_timeout(timeout: object) -> None:
    with pytest.raises(ValueError, match="timeout"):
        SubprocessRssIntervalSampler(
            os.getpid(),
            timeout_seconds=timeout,  # type: ignore[arg-type]
        )


def test_subprocess_sampler_kills_and_closes_an_unresponsive_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _FakeConnection(
        incoming=[
            {"protocol": rss_monitor_module._MONITOR_PROTOCOL, "state": "prepared"},
            {"protocol": rss_monitor_module._MONITOR_PROTOCOL, "state": "ready"},
        ]
    )
    child = _FakeConnection()
    process = _FakeProcess(terminate_stops=False)
    _install_fake_context(monkeypatch, parent=parent, child=child, process=process)
    sampler = SubprocessRssIntervalSampler(os.getpid())

    sampler.start()
    sampler.abort()
    sampler.abort()

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.join_timeouts == [
        rss_monitor_module._MONITOR_CLEANUP_TIMEOUT_SECONDS,
        rss_monitor_module._MONITOR_CLEANUP_TIMEOUT_SECONDS,
        0,
    ]
    assert process.closed is True
    assert parent.closed is True
    assert child.closed is True


def test_subprocess_sampler_prepare_keeps_helper_idle_until_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _FakeConnection(
        incoming=[{"protocol": rss_monitor_module._MONITOR_PROTOCOL, "state": "prepared"}]
    )
    child = _FakeConnection()
    process = _FakeProcess()
    _install_fake_context(monkeypatch, parent=parent, child=child, process=process)
    sampler = SubprocessRssIntervalSampler(os.getpid())

    sampler.prepare()

    assert process.alive is True
    assert parent.sent == []

    parent.incoming.append({"protocol": rss_monitor_module._MONITOR_PROTOCOL, "state": "ready"})
    sampler.start()

    assert parent.sent == [
        {
            "protocol": rss_monitor_module._MONITOR_PROTOCOL,
            "command": "start",
        }
    ]
    sampler.abort()


def test_subprocess_sampler_poll_failure_closes_and_reaps_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _FakeConnection(poll_error=OSError("injected poll failure"))
    child = _FakeConnection()
    process = _FakeProcess()
    _install_fake_context(monkeypatch, parent=parent, child=child, process=process)
    sampler = SubprocessRssIntervalSampler(os.getpid())

    with pytest.raises(RssIntervalError, match="prepare response failed"):
        sampler.start()
    sampler.abort()

    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.closed is True
    assert parent.closed is True
    assert child.closed is True


def test_monitor_error_after_completion_does_not_stop_sampler_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    samplers: list[_FakeIntervalSampler] = []

    def create_sampler(
        pid: int,
        *,
        sample_interval_seconds: float,
    ) -> _FakeIntervalSampler:
        assert connection.sent == [
            {
                "protocol": rss_monitor_module._MONITOR_PROTOCOL,
                "state": "prepared",
            }
        ]
        sampler = _FakeIntervalSampler(pid, sample_interval_seconds=sample_interval_seconds)
        samplers.append(sampler)
        return sampler

    monkeypatch.setattr(rss_monitor_module, "CurrentRssIntervalSampler", create_sampler)
    connection = _CompleteSendFailureConnection()

    with pytest.raises(SystemExit) as raised:
        rss_monitor_module._monitor(cast(Any, connection), os.getpid(), 0.001)

    assert raised.value.code == 2
    assert len(samplers) == 1
    assert samplers[0].stop_calls == 1
    assert connection.closed is True
    assert any(
        isinstance(message, dict) and message.get("state") == "error" for message in connection.sent
    )


def test_monitor_evidence_pid_must_match_target() -> None:
    target_pid = os.getpid()
    evidence = RssIntervalEvidence(
        source="test-current-rss",
        pid=target_pid + 1,
        quiescent_current_bytes=100,
        interval_peak_bytes=125,
        incremental_peak_bytes=25,
        sample_count=2,
        maximum_sample_gap_ns=1,
    )

    with pytest.raises(RssIntervalError, match="pid differs"):
        rss_monitor_module._evidence(
            {
                "protocol": rss_monitor_module._MONITOR_PROTOCOL,
                "state": "complete",
                "evidence": evidence.to_dict(),
            },
            expected_pid=target_pid,
        )


def _monitor_child_pids() -> set[int]:
    return {
        pid
        for child in multiprocessing.active_children()
        if child.name.startswith("pyowl-rss-monitor-")
        if (pid := child.pid) is not None
    }


class _FakeConnection:
    def __init__(
        self,
        *,
        incoming: list[object] | None = None,
        poll_error: BaseException | None = None,
    ) -> None:
        self.incoming = [] if incoming is None else incoming
        self.poll_error = poll_error
        self.sent: list[object] = []
        self.closed = False

    def poll(self, _timeout: float) -> bool:
        if self.poll_error is not None:
            raise self.poll_error
        return bool(self.incoming)

    def recv(self) -> object:
        if not self.incoming:
            raise EOFError("no fake message")
        return self.incoming.pop(0)

    def send(self, value: object) -> None:
        self.sent.append(value)

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, *, terminate_stops: bool = True) -> None:
        self.pid: int | None = 424_242
        self.exitcode: int | None = None
        self.terminate_stops = terminate_stops
        self.alive = False
        self.terminate_calls = 0
        self.kill_calls = 0
        self.join_timeouts: list[float | None] = []
        self.closed = False

    def start(self) -> None:
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.terminate_stops:
            self.alive = False
            self.exitcode = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self.alive = False
        self.exitcode = -9

    def join(self, timeout: float | None = None) -> None:
        self.join_timeouts.append(timeout)

    def close(self) -> None:
        if self.alive:
            raise ValueError("cannot close a live fake process")
        self.closed = True


class _FakeContext:
    def __init__(
        self,
        *,
        parent: _FakeConnection,
        child: _FakeConnection,
        process: _FakeProcess,
    ) -> None:
        self.parent = parent
        self.child = child
        self.process = process

    def Pipe(self, *, duplex: bool) -> tuple[_FakeConnection, _FakeConnection]:
        assert duplex is True
        return self.parent, self.child

    def Process(self, **_kwargs: object) -> _FakeProcess:
        return self.process


def _install_fake_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    parent: _FakeConnection,
    child: _FakeConnection,
    process: _FakeProcess,
) -> None:
    context = _FakeContext(parent=parent, child=child, process=process)

    def get_context(method: str | None = None) -> Any:
        assert method == "spawn"
        return context

    monkeypatch.setattr(multiprocessing, "get_context", get_context)


class _FakeIntervalSampler:
    def __init__(self, pid: int, *, sample_interval_seconds: float) -> None:
        self.pid = pid
        self.sample_interval_seconds = sample_interval_seconds
        self.stop_calls = 0

    def start(self) -> None:
        pass

    def stop(self) -> RssIntervalEvidence:
        self.stop_calls += 1
        return RssIntervalEvidence(
            source="fake-current-rss",
            pid=self.pid,
            quiescent_current_bytes=100,
            interval_peak_bytes=125,
            incremental_peak_bytes=25,
            sample_count=2,
            maximum_sample_gap_ns=1,
        )


class _CompleteSendFailureConnection(_FakeConnection):
    def __init__(self) -> None:
        super().__init__(
            incoming=[
                {
                    "protocol": rss_monitor_module._MONITOR_PROTOCOL,
                    "command": "start",
                },
                {
                    "protocol": rss_monitor_module._MONITOR_PROTOCOL,
                    "command": "stop",
                },
            ]
        )

    def send(self, value: object) -> None:
        if isinstance(value, dict) and value.get("state") == "complete":
            raise OSError("injected completion send failure")
        super().send(value)
