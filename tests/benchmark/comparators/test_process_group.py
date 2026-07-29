from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from typing import Any, cast

import pytest

import tools.benchmark.comparators.process_group as process_group_module
from tools.benchmark.comparators.process_group import (
    OwnedProcessGroup,
    ProcessGroupCleanupError,
    capture_process_group,
    cleanup_exited_process_group,
    observe_process_exit,
    terminate_process,
)


class _FakeObserver:
    def __init__(self, observations: list[bool]) -> None:
        self._observations = iter(observations)
        self._last = observations[-1]
        self.closed = False

    def observe(self, _process: object) -> bool:
        with suppress(StopIteration):
            self._last = next(self._observations)
        return self._last

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    pid = 123_456

    def __init__(self, *, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.waits: list[float] = []

    def wait(self, *, timeout: float) -> int:
        self.waits.append(timeout)
        self.returncode = 0
        return 0


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_fast_zero_exit_keeps_group_identity_until_clean_reap() -> None:
    process = subprocess.Popen(
        (sys.executable, "-c", "pass"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    time.sleep(0.05)
    process_group = capture_process_group(process)
    try:
        deadline = time.monotonic() + 2.0
        while not observe_process_exit(process, process_group=process_group):
            if time.monotonic() >= deadline:
                raise AssertionError("fast subprocess did not exit")
            time.sleep(0.005)
        assert cleanup_exited_process_group(
            process,
            process_group=process_group,
            grace_seconds=0.2,
        )
        assert process.returncode == 0
        assert process_group.extinct is True
    finally:
        if process.returncode is None:
            terminate_process(
                process,
                process_group=process_group,
                grace_seconds=0.2,
            )
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX observer semantics")
def test_late_kqueue_registration_esrch_means_unreaped_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeQueue:
        def __init__(self) -> None:
            self.closed = False

        def control(
            self,
            _changes: object,
            _max_events: int,
            _timeout: int,
        ) -> list[object]:
            raise OSError(errno.ESRCH, "already exited")

        def close(self) -> None:
            self.closed = True

    queue = FakeQueue()
    monkeypatch.delattr(process_group_module.os, "waitid", raising=False)
    monkeypatch.setattr(process_group_module.select, "kqueue", lambda: queue, raising=False)
    monkeypatch.setattr(
        process_group_module.select,
        "kevent",
        lambda *_args, **_kwargs: object(),
        raising=False,
    )
    for name, value in (
        ("KQ_FILTER_PROC", 1),
        ("KQ_EV_ADD", 2),
        ("KQ_EV_ENABLE", 4),
        ("KQ_NOTE_EXIT", 8),
    ):
        monkeypatch.setattr(process_group_module.select, name, value, raising=False)
    process = cast(subprocess.Popen[bytes], _FakeProcess())

    observer = process_group_module._ProcessExitObserver.capture(process)

    assert observer.exited is True
    assert queue.closed is False
    observer.close()
    assert queue.closed is True


def test_clean_reap_never_probes_a_reusable_group_after_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    observer = _FakeObserver([True])
    process_group = OwnedProcessGroup(
        pgid=process.pid,
        owner_pid=os.getpid(),
        observer=cast(Any, observer),
    )
    reaped = False

    def wait(*, timeout: float) -> int:
        nonlocal reaped
        process.waits.append(timeout)
        process.returncode = 0
        reaped = True
        return 0

    def killpg(_pgid: int, _signal_number: int) -> None:
        if reaped:
            raise AssertionError("numeric PGID was touched after leader reap")

    process.wait = wait  # type: ignore[method-assign]
    monkeypatch.setattr(process_group_module.os, "killpg", killpg)
    monkeypatch.setattr(
        process_group_module,
        "_descendant_state",
        lambda _group: (False, False),
    )

    assert cleanup_exited_process_group(
        cast(subprocess.Popen[bytes], process),
        process_group=process_group,
        grace_seconds=0.1,
    )
    assert reaped is True
    assert observer.closed is True


def test_permission_failure_reaps_closes_and_reports_group_proof_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    observer = _FakeObserver([False, True])
    process_group = OwnedProcessGroup(
        pgid=process.pid,
        owner_pid=os.getpid(),
        observer=cast(Any, observer),
    )
    direct_signals: list[int] = []
    monkeypatch.setattr(
        process_group_module.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(PermissionError("injected")),
    )
    monkeypatch.setattr(
        process_group_module.os,
        "kill",
        lambda _pid, signal_number: direct_signals.append(signal_number),
    )
    monkeypatch.setattr(
        process_group_module,
        "_descendant_state",
        lambda _group: (False, False),
    )

    with pytest.raises(ProcessGroupCleanupError, match="could not be signalled"):
        terminate_process(
            cast(subprocess.Popen[bytes], process),
            process_group=process_group,
            grace_seconds=0.1,
        )

    assert process.returncode == 0
    assert process.waits == [0.1]
    assert direct_signals == [signal.SIGTERM]
    assert observer.closed is True


def test_direct_signal_failure_still_reaps_and_closes_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    observer = _FakeObserver([False, True])
    process_group = OwnedProcessGroup(
        pgid=process.pid,
        owner_pid=os.getpid(),
        observer=cast(Any, observer),
    )
    monkeypatch.setattr(
        process_group_module.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(PermissionError("injected group denial")),
    )
    monkeypatch.setattr(
        process_group_module.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(PermissionError("injected direct denial")),
    )
    monkeypatch.setattr(
        process_group_module,
        "_descendant_state",
        lambda _group: (False, False),
    )

    with pytest.raises(ProcessGroupCleanupError, match="signalled directly"):
        terminate_process(
            cast(subprocess.Popen[bytes], process),
            process_group=process_group,
            grace_seconds=0.1,
        )

    assert process.returncode == 0
    assert process.waits == [0.1]
    assert observer.closed is True


def test_reap_signal_failure_still_retries_wait_and_closes_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowExitProcess(_FakeProcess):
        def wait(self, *, timeout: float) -> int:
            self.waits.append(timeout)
            if len(self.waits) == 1:
                raise subprocess.TimeoutExpired("fixture", timeout)
            self.returncode = 0
            return 0

    process = SlowExitProcess()
    observer = _FakeObserver([False])
    process_group = OwnedProcessGroup(
        pgid=process.pid,
        owner_pid=os.getpid(),
        observer=cast(Any, observer),
    )
    monkeypatch.setattr(process_group_module.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(
        process_group_module.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(PermissionError("injected reap denial")),
    )

    with pytest.raises(ProcessGroupCleanupError, match="signalled directly"):
        terminate_process(
            cast(subprocess.Popen[bytes], process),
            process_group=process_group,
            grace_seconds=0.0,
        )

    assert process.returncode == 0
    assert process.waits == [0.0, 0.0]
    assert observer.closed is True


def test_observer_timeout_never_turns_group_kill_into_extinction_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    observer = _FakeObserver([False])
    process_group = OwnedProcessGroup(
        pgid=process.pid,
        owner_pid=os.getpid(),
        observer=cast(Any, observer),
    )
    group_signals: list[int] = []
    reaped = False

    def wait(*, timeout: float) -> int:
        nonlocal reaped
        process.waits.append(timeout)
        process.returncode = 0
        reaped = True
        return 0

    def killpg(_pgid: int, signal_number: int) -> None:
        if reaped:
            raise AssertionError("numeric PGID was signalled after leader reap")
        group_signals.append(signal_number)

    process.wait = wait  # type: ignore[method-assign]
    monkeypatch.setattr(process_group_module.os, "killpg", killpg)

    with pytest.raises(ProcessGroupCleanupError, match="could not be attributed"):
        terminate_process(
            cast(subprocess.Popen[bytes], process),
            process_group=process_group,
            grace_seconds=0.0,
        )

    assert group_signals == [signal.SIGTERM, signal.SIGKILL]
    assert reaped is True
    assert process_group.extinct is False
    assert observer.closed is True


@pytest.mark.parametrize(
    ("returncode", "extinct"),
    ((None, False), (0, True)),
)
def test_non_owner_token_rejects_operations_without_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int | None,
    extinct: bool,
) -> None:
    process = _FakeProcess(returncode=returncode)
    observer = _FakeObserver([False])
    process_group = OwnedProcessGroup(
        pgid=process.pid,
        owner_pid=os.getpid(),
        observer=cast(Any, observer),
        extinct=extinct,
    )
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(process_group_module.os, "getpid", lambda: process_group.owner_pid + 1)
    monkeypatch.setattr(
        process_group_module.os,
        "killpg",
        lambda pgid, signal_number: signals.append((pgid, signal_number)),
    )

    operations = (
        process_group.exists,
        lambda: process_group.signal(signal.SIGTERM),
        lambda: observe_process_exit(
            cast(subprocess.Popen[bytes], process),
            process_group=process_group,
        ),
        lambda: terminate_process(
            cast(subprocess.Popen[bytes], process),
            process_group=process_group,
            grace_seconds=0.1,
        ),
        lambda: cleanup_exited_process_group(
            cast(subprocess.Popen[bytes], process),
            process_group=process_group,
            grace_seconds=0.1,
        ),
    )
    for operation in operations:
        with pytest.raises(ProcessGroupCleanupError, match="non-owner"):
            operation()

    assert signals == []
    assert process.waits == []
    assert process.returncode == returncode
    assert observer.closed is False


def test_clean_exit_without_process_tree_containment_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    monkeypatch.setattr(process_group_module.os, "name", "nt")

    with pytest.raises(ProcessGroupCleanupError, match="lacks process-tree containment"):
        cleanup_exited_process_group(
            cast(subprocess.Popen[bytes], process),
            process_group=None,
            grace_seconds=0.1,
        )

    assert process.returncode == 0
    assert process.waits == [0.1]
