"""Owned POSIX subprocess-group capture and fail-closed teardown."""

from __future__ import annotations

import errno
import os
import select
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProcessGroupCleanupError(RuntimeError):
    """An owned subprocess group could not be proven free of live descendants."""


@dataclass(slots=True)
class _ProcessExitObserver:
    """Observe leader exit without releasing its PID/PGID identity."""

    pid: int
    owner_pid: int
    kqueue: Any | None = None
    exited: bool = False

    @classmethod
    def capture(cls, process: subprocess.Popen[bytes]) -> _ProcessExitObserver:
        if os.name != "posix":
            raise ProcessGroupCleanupError(
                "subprocess tree containment is unsupported on this platform"
            )
        observer = cls(pid=process.pid, owner_pid=os.getpid())
        if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
            return observer
        select_api = vars(select)
        if "kqueue" in select_api and "KQ_NOTE_EXIT" in select_api:
            kqueue = select_api["kqueue"]
            kevent = select_api["kevent"]
            queue = kqueue()
            change = kevent(
                process.pid,
                filter=select_api["KQ_FILTER_PROC"],
                flags=select_api["KQ_EV_ADD"] | select_api["KQ_EV_ENABLE"],
                fflags=select_api["KQ_NOTE_EXIT"],
            )
            try:
                events = queue.control([change], 1, 0)
            except OSError as error:
                if error.errno != errno.ESRCH:
                    queue.close()
                    raise ProcessGroupCleanupError(
                        "subprocess exit observer could not be registered"
                    ) from error
                # A just-created, unreaped Popen child still reserves its PID.
                # Darwin can report ESRCH when registration races its exit.
                observer.exited = True
            else:
                observer.exited = bool(events)
            observer.kqueue = queue
            return observer
        raise ProcessGroupCleanupError(
            "non-reaping subprocess exit observation is unsupported on this POSIX platform"
        )

    def observe(self, process: subprocess.Popen[bytes]) -> bool:
        if os.getpid() != self.owner_pid:
            raise ProcessGroupCleanupError("subprocess exit observer used by a non-owner process")
        if process.returncode is not None:
            return True
        if self.exited:
            return True
        if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
            try:
                result = os.waitid(
                    os.P_PID,
                    self.pid,
                    os.WEXITED | os.WNOHANG | os.WNOWAIT,
                )
            except ChildProcessError as error:
                raise ProcessGroupCleanupError(
                    "subprocess leader was reaped outside its owned lifecycle"
                ) from error
            self.exited = result is not None
            return self.exited
        if self.kqueue is None:
            raise ProcessGroupCleanupError("subprocess exit observer is unavailable")
        try:
            self.exited = bool(self.kqueue.control([], 1, 0))
        except OSError as error:
            if error.errno == errno.ESRCH:
                self.exited = True
            else:
                raise ProcessGroupCleanupError("subprocess exit observer failed") from error
        return self.exited

    def close(self) -> None:
        if self.kqueue is not None:
            self.kqueue.close()
            self.kqueue = None


@dataclass(slots=True)
class OwnedProcessGroup:
    """A POSIX session/process group captured while its leader PID is reserved."""

    pgid: int
    owner_pid: int
    observer: _ProcessExitObserver | None = None
    extinct: bool = False

    def require_owner(self) -> None:
        """Reject inherited tokens before any process or group operation."""

        if os.getpid() != self.owner_pid:
            raise ProcessGroupCleanupError("owned subprocess group used by a non-owner process")

    def exists(self) -> bool:
        self.require_owner()
        if self.extinct:
            return False
        try:
            os.killpg(self.pgid, 0)
        except ProcessLookupError:
            self.extinct = True
            return False
        except PermissionError as error:
            raise ProcessGroupCleanupError(
                "owned subprocess group identity could not be verified"
            ) from error
        return True

    def signal(self, signal_number: int) -> bool:
        self.require_owner()
        if self.extinct:
            return False
        try:
            os.killpg(self.pgid, signal_number)
        except ProcessLookupError:
            self.extinct = True
            return False
        except PermissionError as error:
            raise ProcessGroupCleanupError(
                "owned subprocess group could not be signalled"
            ) from error
        return True

    def close(self) -> None:
        if self.observer is not None:
            self.observer.close()


def capture_process_group(process: subprocess.Popen[bytes]) -> OwnedProcessGroup:
    """Capture a new POSIX session and its non-reaping leader observer."""

    captured = provisional_process_group(process)
    try:
        pgid = os.getpgid(process.pid)
        session_id = os.getsid(process.pid)
    except ProcessLookupError:
        # start_new_session=True binds the intended PID/PGID even when the
        # just-created leader has already become an unreaped zombie.
        return captured
    except PermissionError as error:
        captured.close()
        raise ProcessGroupCleanupError("subprocess group identity could not be captured") from error
    if pgid != process.pid or session_id != process.pid:
        captured.close()
        raise ProcessGroupCleanupError("subprocess did not own its requested POSIX session")
    return captured


def provisional_process_group(process: subprocess.Popen[bytes]) -> OwnedProcessGroup:
    """Reserve the intended new-session token before identity verification."""

    return OwnedProcessGroup(
        pgid=process.pid,
        owner_pid=os.getpid(),
        observer=_ProcessExitObserver.capture(process),
    )


def observe_process_exit(
    process: subprocess.Popen[bytes],
    *,
    process_group: OwnedProcessGroup | None,
) -> bool:
    """Report leader exit without reaping or freeing its numeric group identity."""

    if process_group is not None:
        process_group.require_owner()
    if process.returncode is not None:
        return True
    if process_group is None or process_group.observer is None:
        raise ProcessGroupCleanupError("owned non-reaping exit observer is unavailable")
    return process_group.observer.observe(process)


def terminate_process(
    process: subprocess.Popen[bytes],
    *,
    process_group: OwnedProcessGroup | None,
    grace_seconds: float,
) -> None:
    """Terminate an owned tree before reaping its leader."""

    if process_group is not None:
        process_group.require_owner()
    if process.returncode is not None:
        if process_group is not None:
            process_group.close()
            if not process_group.extinct:
                raise ProcessGroupCleanupError(
                    "subprocess leader was reaped before its group could be cleaned safely"
                )
        return
    if process_group is None:
        _terminate_uncontained_leader(process, grace_seconds)
        return

    proof_error: ProcessGroupCleanupError | None = None
    proof_error_is_terminal = False
    exited = False
    descendants_error: ProcessGroupCleanupError | None = None
    lifecycle_error: ProcessGroupCleanupError | None = None
    reap_error: ProcessGroupCleanupError | None = None
    try:
        try:
            process_group.signal(signal.SIGTERM)
        except ProcessGroupCleanupError as error:
            proof_error = error
            leader_exited_after_signal = observe_process_exit(
                process,
                process_group=process_group,
            )
            proof_error_is_terminal = not leader_exited_after_signal
            if not leader_exited_after_signal:
                direct_error = _signal_leader(process, signal.SIGTERM)
                if direct_error is not None:
                    proof_error = _combined_cleanup_error(proof_error, direct_error)
                    proof_error_is_terminal = True

        exited = _wait_for_process_exit_without_reaping(
            process,
            process_group=process_group,
            timeout=grace_seconds,
        )
        if not exited:
            try:
                process_group.signal(signal.SIGKILL)
            except ProcessGroupCleanupError as error:
                proof_error = _combined_cleanup_error(proof_error, error)
                proof_error_is_terminal = True
                direct_error = _signal_leader(process, signal.SIGKILL)
                if direct_error is not None:
                    proof_error = _combined_cleanup_error(proof_error, direct_error)
            exited = _wait_for_process_exit_without_reaping(
                process,
                process_group=process_group,
                timeout=grace_seconds,
            )

        if exited:
            try:
                _remove_live_descendants(
                    process_group,
                    grace_seconds=grace_seconds,
                    send_term=False,
                )
            except ProcessGroupCleanupError as error:
                descendants_error = error
        else:
            lifecycle_error = ProcessGroupCleanupError(
                "subprocess group could not be attributed before leader reap"
            )
    finally:
        try:
            _reap_leader(process, grace_seconds)
        except ProcessGroupCleanupError as error:
            reap_error = error
        finally:
            process_group.close()

    if reap_error is not None:
        raise _combined_cleanup_error(proof_error, reap_error)
    if descendants_error is not None:
        raise descendants_error
    if lifecycle_error is not None:
        raise _combined_cleanup_error(proof_error, lifecycle_error)
    if proof_error is not None and (proof_error_is_terminal or not process_group.extinct):
        raise proof_error


def cleanup_exited_process_group(
    process: subprocess.Popen[bytes],
    *,
    process_group: OwnedProcessGroup | None,
    grace_seconds: float,
    descendants_observed: bool = False,
) -> bool:
    """Remove descendants while the exited leader still reserves its PGID."""

    if process_group is None:
        try:
            process.wait(timeout=grace_seconds)
        finally:
            raise ProcessGroupCleanupError(
                "clean subprocess exit lacks process-tree containment evidence"
            )
    process_group.require_owner()
    if process.returncode is not None:
        process_group.close()
        raise ProcessGroupCleanupError(
            "subprocess leader was reaped before clean group attribution"
        )
    if not observe_process_exit(process, process_group=process_group):
        raise ProcessGroupCleanupError("subprocess leader has not exited")

    cleanup_error: ProcessGroupCleanupError | None = None
    discovered_descendants = False
    try:
        discovered_descendants = _remove_live_descendants(
            process_group,
            grace_seconds=grace_seconds,
            send_term=True,
        )
    except ProcessGroupCleanupError as error:
        cleanup_error = error
    reap_error: ProcessGroupCleanupError | None = None
    try:
        _reap_leader(process, grace_seconds)
    except ProcessGroupCleanupError as error:
        reap_error = error
    finally:
        process_group.close()
    if reap_error is not None:
        raise _combined_cleanup_error(cleanup_error, reap_error)
    if cleanup_error is not None:
        raise cleanup_error
    return not (descendants_observed or discovered_descendants)


def _remove_live_descendants(
    process_group: OwnedProcessGroup,
    *,
    grace_seconds: float,
    send_term: bool,
) -> bool:
    descendants_observed, live_descendants = _descendant_state(process_group)
    if not live_descendants:
        process_group.extinct = True
        return descendants_observed

    if send_term:
        process_group.signal(signal.SIGTERM)
        live_descendants = _wait_for_live_descendants(
            process_group,
            timeout=grace_seconds,
        )
    if live_descendants:
        process_group.signal(signal.SIGKILL)
        live_descendants = _wait_for_live_descendants(
            process_group,
            timeout=grace_seconds,
        )
    if live_descendants:
        raise ProcessGroupCleanupError("subprocess group survived termination")
    process_group.extinct = True
    return True


def _wait_for_live_descendants(
    process_group: OwnedProcessGroup,
    *,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        _observed, live_descendants = _descendant_state(process_group)
        if not live_descendants:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(0.005, remaining))


def _descendant_state(process_group: OwnedProcessGroup) -> tuple[bool, bool]:
    if sys.platform.startswith("linux"):
        return _linux_descendant_state(process_group.pgid)
    if sys.platform == "darwin":
        try:
            os.killpg(process_group.pgid, 0)
        except ProcessLookupError:
            return False, False
        except PermissionError:
            # Darwin reports EPERM for a process group containing only zombies.
            # The observed leader is already a zombie, so there is no live member.
            return False, False
        return True, True
    raise ProcessGroupCleanupError(
        "live descendant attribution is unsupported on this POSIX platform"
    )


def _linux_descendant_state(pgid: int) -> tuple[bool, bool]:
    descendants_observed = False
    live_descendants = False
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError as error:
        raise ProcessGroupCleanupError("Linux process-group attribution failed") from error
    for entry in entries:
        if not entry.name.isdecimal() or int(entry.name) == pgid:
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, UnicodeError) as error:
            raise ProcessGroupCleanupError("Linux process-group attribution failed") from error
        closing_parenthesis = stat.rfind(")")
        fields = stat[closing_parenthesis + 2 :].split()
        if closing_parenthesis < 0 or len(fields) < 3:
            raise ProcessGroupCleanupError("Linux process-group attribution was malformed")
        try:
            observed_pgid = int(fields[2])
        except ValueError as error:
            raise ProcessGroupCleanupError(
                "Linux process-group attribution was malformed"
            ) from error
        if observed_pgid != pgid:
            continue
        descendants_observed = True
        if fields[0] != "Z":
            live_descendants = True
    return descendants_observed, live_descendants


def _wait_for_process_exit_without_reaping(
    process: subprocess.Popen[bytes],
    *,
    process_group: OwnedProcessGroup,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while not observe_process_exit(process, process_group=process_group):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.005, remaining))
    return True


def _terminate_uncontained_leader(
    process: subprocess.Popen[bytes],
    grace_seconds: float,
) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired as error:
            raise ProcessGroupCleanupError("subprocess leader survived termination") from error


def _reap_leader(process: subprocess.Popen[bytes], timeout: float) -> None:
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_error = _signal_leader(process, signal.SIGKILL)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            survived = ProcessGroupCleanupError("subprocess leader survived termination")
            raise _combined_cleanup_error(kill_error, survived) from error
        if kill_error is not None:
            raise kill_error from kill_error.__cause__


def _signal_leader(
    process: subprocess.Popen[bytes],
    signal_number: int,
) -> ProcessGroupCleanupError | None:
    try:
        os.kill(process.pid, signal_number)
    except OSError as error:
        result = ProcessGroupCleanupError("subprocess leader could not be signalled directly")
        result.__cause__ = error
        return result
    return None


def _combined_cleanup_error(
    first: ProcessGroupCleanupError | None,
    second: ProcessGroupCleanupError,
) -> ProcessGroupCleanupError:
    if first is None:
        return second
    return ProcessGroupCleanupError(f"{first}; additionally: {second}")


__all__ = [
    "OwnedProcessGroup",
    "ProcessGroupCleanupError",
    "capture_process_group",
    "cleanup_exited_process_group",
    "observe_process_exit",
    "provisional_process_group",
    "terminate_process",
]
