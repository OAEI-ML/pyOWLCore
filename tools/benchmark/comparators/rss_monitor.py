"""Out-of-process RSS interval sampling for in-process comparator calls."""

from __future__ import annotations

import multiprocessing
from contextlib import suppress
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Any, Final, cast

from .rss_interval import (
    DEFAULT_RSS_SAMPLE_INTERVAL_SECONDS,
    RSS_INTERVAL_SCHEMA,
    CurrentRssIntervalSampler,
    RssIntervalError,
    RssIntervalEvidence,
)

_MONITOR_PROTOCOL: Final = "pyowl-core/comparator-rss-monitor/v1"
_MONITOR_TIMEOUT_SECONDS: Final = 10.0
_MONITOR_CLEANUP_TIMEOUT_SECONDS: Final = 1.0
_MAX_U64: Final = 2**64 - 1


class SubprocessRssIntervalSampler:
    """Sample a target PID from a helper process, independent of its GIL."""

    def __init__(
        self,
        pid: int,
        *,
        sample_interval_seconds: float = DEFAULT_RSS_SAMPLE_INTERVAL_SECONDS,
        timeout_seconds: float = _MONITOR_TIMEOUT_SECONDS,
    ) -> None:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("RSS monitor pid must be a positive integer")
        if (
            isinstance(sample_interval_seconds, bool)
            or not isinstance(sample_interval_seconds, (int, float))
            or not 0 < float(sample_interval_seconds) <= 1.0
        ):
            raise ValueError("RSS monitor sample interval must be in (0, 1] seconds")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < float(timeout_seconds) <= 60.0
        ):
            raise ValueError("RSS monitor timeout must be in (0, 60] seconds")
        self._pid = pid
        self._sample_interval_seconds = float(sample_interval_seconds)
        self._timeout_seconds = float(timeout_seconds)
        self._connection: Connection | None = None
        self._child_connection: Connection | None = None
        self._process: BaseProcess | None = None
        self._process_started = False
        self._process_closed = False
        self._started = False
        self._stopped = False

    def start(self) -> None:
        """Start the helper and wait until its quiescent sample is captured."""

        if self._started:
            raise RssIntervalError("RSS monitor cannot be started twice")
        if self._stopped:
            raise RssIntervalError("RSS monitor cannot start after it was aborted")
        self._started = True
        try:
            self._create_process()
            process = self._require_process()
            self._process_started = True
            process.start()
            self._close_child_connection()
            message = self._receive("startup")
            if message != {
                "protocol": _MONITOR_PROTOCOL,
                "state": "ready",
            }:
                raise RssIntervalError("RSS monitor startup acknowledgement is invalid")
        except RssIntervalError:
            self._stopped = True
            self._terminate()
            raise
        except Exception as error:
            self._stopped = True
            self._terminate()
            raise RssIntervalError("RSS monitor process could not start") from error

    def stop(self) -> RssIntervalEvidence:
        """Stop the helper and return its complete interval evidence."""

        if not self._started:
            raise RssIntervalError("RSS monitor was not started")
        if self._stopped:
            raise RssIntervalError("RSS monitor cannot be stopped twice")
        self._stopped = True
        connection = self._require_connection()
        try:
            connection.send(
                {
                    "protocol": _MONITOR_PROTOCOL,
                    "command": "stop",
                }
            )
        except (BrokenPipeError, EOFError, OSError, ValueError) as error:
            self._terminate()
            raise RssIntervalError("RSS monitor stop request failed") from error
        message = self._receive("result")
        self._join()
        return _evidence(message, expected_pid=self._pid)

    def abort(self) -> None:
        """Terminate an unfinished helper without accepting partial evidence."""

        if self._process_closed and self._connection is None and self._child_connection is None:
            return
        self._stopped = True
        self._terminate()

    def _receive(self, phase: str) -> dict[str, Any]:
        connection = self._require_connection()
        try:
            available = connection.poll(self._timeout_seconds)
        except (EOFError, OSError, ValueError) as error:
            self._terminate()
            raise RssIntervalError(f"RSS monitor {phase} response failed") from error
        if not available:
            self._terminate()
            raise RssIntervalError(f"RSS monitor {phase} timed out")
        try:
            value = connection.recv()
        except (EOFError, OSError, ValueError) as error:
            self._terminate()
            raise RssIntervalError(f"RSS monitor {phase} response failed") from error
        if not isinstance(value, dict):
            self._terminate()
            raise RssIntervalError(f"RSS monitor {phase} response is invalid")
        message = cast(dict[str, Any], value)
        if message.get("protocol") != _MONITOR_PROTOCOL:
            self._terminate()
            raise RssIntervalError(f"RSS monitor {phase} protocol differs")
        if message.get("state") == "error":
            self._terminate()
            reason = message.get("reason")
            raise RssIntervalError(
                reason if isinstance(reason, str) and reason else f"RSS monitor {phase} failed"
            )
        return message

    def _join(self) -> None:
        process = self._require_process()
        try:
            process.join(self._timeout_seconds)
            alive = process.is_alive()
            code = process.exitcode
        except (AssertionError, OSError, ValueError) as error:
            self._terminate()
            raise RssIntervalError("RSS monitor process could not be joined") from error
        if alive:
            self._terminate()
            raise RssIntervalError("RSS monitor process did not exit")
        self._dispose_process()
        if code != 0:
            raise RssIntervalError(f"RSS monitor process exited {code}")

    def _terminate(self) -> None:
        process = self._process
        try:
            if process is not None and not self._process_closed:
                alive = _is_alive(process) if self._process_started else False
                if alive is not False:
                    with suppress(AssertionError, AttributeError, OSError, ValueError):
                        process.terminate()
                    with suppress(AssertionError, OSError, ValueError):
                        process.join(_MONITOR_CLEANUP_TIMEOUT_SECONDS)
                    alive = _is_alive(process)
                if alive is not False:
                    with suppress(AssertionError, AttributeError, OSError, ValueError):
                        process.kill()
                    with suppress(AssertionError, OSError, ValueError):
                        process.join(_MONITOR_CLEANUP_TIMEOUT_SECONDS)
                    alive = _is_alive(process)
                if alive is not False:
                    raise RssIntervalError("RSS monitor process survived termination")
                with suppress(AssertionError, OSError, ValueError):
                    process.join(0)
                try:
                    process.close()
                except (OSError, ValueError) as error:
                    raise RssIntervalError("RSS monitor process cleanup failed") from error
                self._process_closed = True
        finally:
            self._close_connections()

    def _create_process(self) -> None:
        parent: Connection | None = None
        child: Connection | None = None
        try:
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe(duplex=True)
            process = context.Process(
                target=_monitor,
                args=(child, self._pid, self._sample_interval_seconds),
                name=f"pyowl-rss-monitor-{self._pid}",
                daemon=True,
            )
        except Exception as error:
            if parent is not None:
                with suppress(OSError):
                    parent.close()
            if child is not None:
                with suppress(OSError):
                    child.close()
            raise RssIntervalError("RSS monitor process could not be created") from error
        self._connection = parent
        self._child_connection = child
        self._process = process

    def _require_connection(self) -> Connection:
        connection = self._connection
        if connection is None:
            raise RssIntervalError("RSS monitor connection is unavailable")
        return connection

    def _require_process(self) -> BaseProcess:
        process = self._process
        if process is None:
            raise RssIntervalError("RSS monitor process is unavailable")
        return process

    def _dispose_process(self) -> None:
        process = self._require_process()
        try:
            process.close()
        except (OSError, ValueError) as error:
            self._close_connections()
            raise RssIntervalError("RSS monitor process cleanup failed") from error
        self._process_closed = True
        self._close_connections()

    def _close_connections(self) -> None:
        connection = self._connection
        if connection is not None:
            with suppress(OSError, ValueError):
                connection.close()
            self._connection = None
        self._close_child_connection()

    def _close_child_connection(self) -> None:
        child = self._child_connection
        if child is not None:
            with suppress(OSError, ValueError):
                child.close()
            self._child_connection = None


def _monitor(
    connection: Connection,
    pid: int,
    sample_interval_seconds: float,
) -> None:
    sampler: CurrentRssIntervalSampler | None = None
    sampler_running = False
    try:
        sampler = CurrentRssIntervalSampler(
            pid,
            sample_interval_seconds=sample_interval_seconds,
        )
        sampler.start()
        sampler_running = True
        connection.send(
            {
                "protocol": _MONITOR_PROTOCOL,
                "state": "ready",
            }
        )
        request = connection.recv()
        if request != {
            "protocol": _MONITOR_PROTOCOL,
            "command": "stop",
        }:
            raise RssIntervalError("RSS monitor stop request is invalid")
        sampler_running = False
        evidence = sampler.stop()
        connection.send(
            {
                "protocol": _MONITOR_PROTOCOL,
                "state": "complete",
                "evidence": evidence.to_dict(),
            }
        )
    except Exception as error:
        if sampler is not None and sampler_running:
            sampler_running = False
            with suppress(RssIntervalError):
                sampler.stop()
        with suppress(BrokenPipeError, EOFError, OSError, ValueError):
            connection.send(
                {
                    "protocol": _MONITOR_PROTOCOL,
                    "state": "error",
                    "reason": f"{type(error).__name__}: {error}",
                }
            )
        raise SystemExit(2) from error
    finally:
        with suppress(OSError, ValueError):
            connection.close()


def _evidence(message: dict[str, Any], *, expected_pid: int) -> RssIntervalEvidence:
    if set(message) != {"protocol", "state", "evidence"} or message.get("state") != "complete":
        raise RssIntervalError("RSS monitor result response is invalid")
    raw = message.get("evidence")
    if not isinstance(raw, dict):
        raise RssIntervalError("RSS monitor result lacks evidence")
    evidence = cast(dict[str, Any], raw)
    required = {
        "schema",
        "source",
        "pid",
        "quiescent_current_bytes",
        "interval_peak_bytes",
        "incremental_peak_bytes",
        "sample_count",
        "maximum_sample_gap_ns",
    }
    if set(evidence) != required or evidence.get("schema") != RSS_INTERVAL_SCHEMA:
        raise RssIntervalError("RSS monitor evidence fields differ")
    source = evidence.get("source")
    if not isinstance(source, str) or not source:
        raise RssIntervalError("RSS monitor evidence source is invalid")
    integers = {
        name: _u64(evidence.get(name), f"RSS monitor evidence {name}")
        for name in required - {"schema", "source"}
    }
    if integers["pid"] != expected_pid:
        raise RssIntervalError("RSS monitor evidence pid differs")
    if integers["sample_count"] < 2:
        raise RssIntervalError("RSS monitor evidence counters are invalid")
    baseline = integers["quiescent_current_bytes"]
    peak = integers["interval_peak_bytes"]
    increment = integers["incremental_peak_bytes"]
    if peak < baseline or increment != peak - baseline:
        raise RssIntervalError("RSS monitor evidence is internally inconsistent")
    return RssIntervalEvidence(
        source=source,
        pid=integers["pid"],
        quiescent_current_bytes=baseline,
        interval_peak_bytes=peak,
        incremental_peak_bytes=increment,
        sample_count=integers["sample_count"],
        maximum_sample_gap_ns=integers["maximum_sample_gap_ns"],
    )


def _u64(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_U64:
        raise RssIntervalError(f"{name} must be an unsigned 64-bit integer")
    return value


def _is_alive(process: BaseProcess) -> bool | None:
    try:
        return process.is_alive()
    except (AssertionError, OSError, ValueError):
        return None


__all__ = ["SubprocessRssIntervalSampler"]
