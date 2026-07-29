"""Parent-side current-RSS sampling for one persistent comparator request."""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

RSS_INTERVAL_SCHEMA: Final = "pyowl-core/comparator-rss-interval/v1"
DEFAULT_RSS_SAMPLE_INTERVAL_SECONDS: Final = 0.001
_MAX_U64: Final = 2**64 - 1

CurrentRssReader = Callable[[int], int]


class RssIntervalError(RuntimeError):
    """Current RSS could not be sampled as complete interval evidence."""


@dataclass(frozen=True, slots=True)
class RssIntervalEvidence:
    """Observed current-RSS envelope for one prepared/execute interval."""

    source: str
    pid: int
    quiescent_current_bytes: int
    interval_peak_bytes: int
    incremental_peak_bytes: int
    sample_count: int
    maximum_sample_gap_ns: int

    def to_dict(self) -> dict[str, int | str]:
        return {
            "schema": RSS_INTERVAL_SCHEMA,
            "source": self.source,
            "pid": self.pid,
            "quiescent_current_bytes": self.quiescent_current_bytes,
            "interval_peak_bytes": self.interval_peak_bytes,
            "incremental_peak_bytes": self.incremental_peak_bytes,
            "sample_count": self.sample_count,
            "maximum_sample_gap_ns": self.maximum_sample_gap_ns,
        }


class CurrentRssIntervalSampler:
    """Poll a target process from a parent thread without changing the target."""

    def __init__(
        self,
        pid: int,
        *,
        sample_interval_seconds: float = DEFAULT_RSS_SAMPLE_INTERVAL_SECONDS,
        reader: CurrentRssReader | None = None,
        source: str | None = None,
    ) -> None:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("RSS interval pid must be a positive integer")
        if (
            isinstance(sample_interval_seconds, bool)
            or not isinstance(sample_interval_seconds, (int, float))
            or not 0 < float(sample_interval_seconds) <= 1.0
        ):
            raise ValueError("RSS sample interval must be in (0, 1] seconds")
        selected_reader, selected_source = (
            (reader, source)
            if reader is not None
            else (read_current_rss_bytes, current_rss_source())
        )
        if selected_reader is None:
            raise TypeError("RSS interval reader must be callable")
        if not isinstance(selected_source, str) or not selected_source:
            raise ValueError("RSS interval source must be a nonempty string")
        self._pid = pid
        self._sample_interval_seconds = float(sample_interval_seconds)
        self._reader = selected_reader
        self._source = selected_source
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._started = False
        self._stopped = False
        self._quiescent_current_bytes = 0
        self._interval_peak_bytes = 0
        self._sample_count = 0
        self._maximum_sample_gap_ns = 0
        self._last_sample_ns = 0

    def start(self) -> None:
        """Capture the quiescent baseline and begin parent-side polling."""

        if self._started:
            raise RssIntervalError("RSS interval sampler cannot be started twice")
        self._started = True
        value, observed_ns = self._read()
        self._quiescent_current_bytes = value
        self._interval_peak_bytes = value
        self._sample_count = 1
        self._last_sample_ns = observed_ns
        self._thread = threading.Thread(
            target=self._poll,
            name=f"pyowl-rss-interval-{self._pid}",
            daemon=True,
        )
        try:
            self._thread.start()
        except RuntimeError as error:
            self._stop_event.set()
            self._stopped = True
            if self._thread.is_alive():
                self._thread.join(timeout=max(1.0, self._sample_interval_seconds * 10.0))
            self._error = error
            raise RssIntervalError("RSS interval sampler thread could not start") from error

    def stop(self) -> RssIntervalEvidence:
        """Stop polling, take a terminal sample, and return exact evidence."""

        if not self._started:
            raise RssIntervalError("RSS interval sampler was not started")
        if self._stopped:
            raise RssIntervalError("RSS interval sampler cannot be stopped twice")
        self._stopped = True
        self._stop_event.set()
        thread = self._thread
        if thread is None:
            raise RssIntervalError("RSS interval sampler lacks its polling thread")
        thread.join(timeout=max(1.0, self._sample_interval_seconds * 10.0))
        if thread.is_alive():
            raise RssIntervalError("RSS interval sampler thread did not stop")
        if self._error is not None:
            raise RssIntervalError(
                f"RSS interval sampler failed: {type(self._error).__name__}: {self._error}"
            ) from self._error
        self._sample()
        with self._lock:
            peak = self._interval_peak_bytes
            baseline = self._quiescent_current_bytes
            count = self._sample_count
            maximum_gap = self._maximum_sample_gap_ns
        if count < 2:
            raise RssIntervalError("RSS interval sampler produced fewer than two samples")
        return RssIntervalEvidence(
            source=self._source,
            pid=self._pid,
            quiescent_current_bytes=baseline,
            interval_peak_bytes=peak,
            incremental_peak_bytes=peak - baseline,
            sample_count=count,
            maximum_sample_gap_ns=maximum_gap,
        )

    def _poll(self) -> None:
        try:
            while not self._stop_event.wait(self._sample_interval_seconds):
                self._sample()
        except Exception as error:
            self._error = error
            self._stop_event.set()

    def _sample(self) -> None:
        value, observed_ns = self._read()
        with self._lock:
            gap = observed_ns - self._last_sample_ns
            if gap < 0:
                raise RssIntervalError("RSS sample clock moved backwards")
            self._maximum_sample_gap_ns = max(self._maximum_sample_gap_ns, gap)
            self._interval_peak_bytes = max(self._interval_peak_bytes, value)
            self._sample_count += 1
            self._last_sample_ns = observed_ns

    def _read(self) -> tuple[int, int]:
        try:
            value = self._reader(self._pid)
        except Exception as error:
            raise RssIntervalError(
                f"current RSS read failed: {type(error).__name__}: {error}"
            ) from error
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_U64:
            raise RssIntervalError("current RSS reader returned an invalid byte count")
        return value, time.monotonic_ns()


def current_rss_source() -> str:
    """Return the platform source used by :func:`read_current_rss_bytes`."""

    if sys.platform == "darwin":
        return "darwin-proc-pidinfo-current-rss"
    if sys.platform.startswith("linux"):
        return "linux-proc-statm-current-rss"
    raise RssIntervalError(f"current RSS sampling is unsupported on {sys.platform}")


def read_current_rss_bytes(pid: int) -> int:
    """Read one process's current resident bytes through a platform API."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("current RSS pid must be a positive integer")
    if sys.platform == "darwin":
        return _darwin_current_rss_bytes(pid)
    if sys.platform.startswith("linux"):
        return _linux_current_rss_bytes(pid)
    raise RssIntervalError(f"current RSS sampling is unsupported on {sys.platform}")


class _DarwinProcTaskInfo(ctypes.Structure):
    _fields_ = [
        ("virtual_size", ctypes.c_uint64),
        ("resident_size", ctypes.c_uint64),
        ("total_user", ctypes.c_uint64),
        ("total_system", ctypes.c_uint64),
        ("threads_user", ctypes.c_uint64),
        ("threads_system", ctypes.c_uint64),
        ("policy", ctypes.c_int32),
        ("faults", ctypes.c_int32),
        ("pageins", ctypes.c_int32),
        ("cow_faults", ctypes.c_int32),
        ("messages_sent", ctypes.c_int32),
        ("messages_received", ctypes.c_int32),
        ("syscalls_mach", ctypes.c_int32),
        ("syscalls_unix", ctypes.c_int32),
        ("context_switches", ctypes.c_int32),
        ("thread_count", ctypes.c_int32),
        ("running_thread_count", ctypes.c_int32),
        ("priority", ctypes.c_int32),
    ]


_DARWIN_PROC_PIDTASKINFO: Final = 4


def _darwin_current_rss_bytes(pid: int) -> int:
    proc_pidinfo = _darwin_proc_pidinfo()
    info = _DarwinProcTaskInfo()
    expected = ctypes.sizeof(info)
    ctypes.set_errno(0)
    observed = proc_pidinfo(
        pid,
        _DARWIN_PROC_PIDTASKINFO,
        0,
        ctypes.byref(info),
        expected,
    )
    if observed != expected:
        error_number = ctypes.get_errno()
        detail = os.strerror(error_number) if error_number else "short proc_pidinfo result"
        raise RssIntervalError(f"Darwin proc_pidinfo failed: {detail}")
    return int(info.resident_size)


@lru_cache(maxsize=1)
def _darwin_proc_pidinfo() -> Any:
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    except OSError as error:
        raise RssIntervalError("Darwin libproc is unavailable") from error
    proc_pidinfo = libproc.proc_pidinfo
    proc_pidinfo.argtypes = (
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    )
    proc_pidinfo.restype = ctypes.c_int
    return proc_pidinfo


def _linux_current_rss_bytes(pid: int) -> int:
    try:
        fields = (Path("/proc") / str(pid) / "statm").read_text(encoding="ascii").split()
    except OSError as error:
        raise RssIntervalError("Linux proc statm is unavailable") from error
    if len(fields) < 2 or not fields[1].isdigit():
        raise RssIntervalError("Linux proc statm has invalid resident pages")
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError) as error:
        raise RssIntervalError("Linux page size is unavailable") from error
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size <= 0:
        raise RssIntervalError("Linux page size is invalid")
    resident = int(fields[1]) * page_size
    if resident > _MAX_U64:
        raise RssIntervalError("Linux current RSS exceeds unsigned 64-bit range")
    return resident


__all__ = [
    "DEFAULT_RSS_SAMPLE_INTERVAL_SECONDS",
    "RSS_INTERVAL_SCHEMA",
    "CurrentRssIntervalSampler",
    "RssIntervalError",
    "RssIntervalEvidence",
    "current_rss_source",
    "read_current_rss_bytes",
]
