from __future__ import annotations

import os
import threading
import time

import pytest

from tools.benchmark.comparators.rss_interval import (
    RSS_INTERVAL_SCHEMA,
    CurrentRssIntervalSampler,
    RssIntervalError,
    RssIntervalEvidence,
    current_rss_source,
    read_current_rss_bytes,
)


def test_platform_current_rss_backend_observes_this_process() -> None:
    assert current_rss_source() in {
        "darwin-proc-pidinfo-current-rss",
        "linux-proc-statm-current-rss",
    }
    assert read_current_rss_bytes(os.getpid()) > 0


def test_interval_sampler_reports_quiescent_peak_count_and_gap() -> None:
    observed = iter((1_000, 1_250, 1_500, 1_200, 1_100))

    def current_rss(pid: int) -> int:
        assert pid == os.getpid()
        return next(observed, 1_100)

    sampler = CurrentRssIntervalSampler(
        os.getpid(),
        sample_interval_seconds=0.001,
        reader=current_rss,
        source="deterministic-test-current-rss",
    )
    sampler.start()
    time.sleep(0.004)
    evidence = sampler.stop()

    assert evidence.to_dict() == {
        "schema": RSS_INTERVAL_SCHEMA,
        "source": "deterministic-test-current-rss",
        "pid": os.getpid(),
        "quiescent_current_bytes": 1_000,
        "interval_peak_bytes": 1_500,
        "incremental_peak_bytes": 500,
        "sample_count": evidence.sample_count,
        "maximum_sample_gap_ns": evidence.maximum_sample_gap_ns,
    }
    assert evidence.sample_count >= 3
    assert evidence.maximum_sample_gap_ns > 0


def test_interval_sampler_fails_closed_after_a_polling_error() -> None:
    calls = 0

    def failing_reader(_pid: int) -> int:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("injected read failure")
        return 1_000

    sampler = CurrentRssIntervalSampler(
        os.getpid(),
        sample_interval_seconds=0.001,
        reader=failing_reader,
        source="failing-test-current-rss",
    )
    sampler.start()
    time.sleep(0.003)

    with pytest.raises(RssIntervalError, match="current RSS read failed"):
        sampler.stop()


def test_interval_sampler_stops_polling_before_the_terminal_sample() -> None:
    poll_started = threading.Event()
    release_poll = threading.Event()
    calls = 0
    sampler: CurrentRssIntervalSampler

    def current_rss(_pid: int) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            poll_started.set()
            assert release_poll.wait(1.0)
        if calls == 3:
            assert sampler._stop_event.is_set()
        return 1_000 + calls

    sampler = CurrentRssIntervalSampler(
        os.getpid(),
        sample_interval_seconds=0.001,
        reader=current_rss,
        source="ordered-test-current-rss",
    )
    sampler.start()
    assert poll_started.wait(1.0)
    result: list[object] = []

    def stop() -> None:
        try:
            result.append(sampler.stop())
        except Exception as error:
            result.append(error)

    stopper = threading.Thread(target=stop)
    stopper.start()
    assert sampler._stop_event.wait(1.0)
    release_poll.set()
    stopper.join(1.0)

    assert stopper.is_alive() is False
    assert len(result) == 1
    assert isinstance(result[0], RssIntervalEvidence)
    assert result[0].sample_count == 3


def test_interval_sampler_start_failure_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenThread:
        def __init__(self, **_kwargs: object) -> None:
            self.joined = False

        def start(self) -> None:
            raise RuntimeError("injected start failure")

        def is_alive(self) -> bool:
            return False

        def join(self, _timeout: float) -> None:
            self.joined = True

    monkeypatch.setattr(threading, "Thread", BrokenThread)
    sampler = CurrentRssIntervalSampler(
        os.getpid(),
        reader=lambda _pid: 1_000,
        source="start-failure-test-current-rss",
    )

    with pytest.raises(RssIntervalError, match="thread could not start"):
        sampler.start()
    assert sampler._stop_event.is_set()
    with pytest.raises(RssIntervalError, match="stopped twice"):
        sampler.stop()


@pytest.mark.parametrize("pid", [0, -1, True, 1.0])
def test_interval_sampler_rejects_invalid_pid(pid: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        CurrentRssIntervalSampler(pid)  # type: ignore[arg-type]
