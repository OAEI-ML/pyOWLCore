"""Atomic snapshot publication and content-addressed wire cache."""

from __future__ import annotations

import os
import secrets
import stat
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pyowl_core.cancellation import CancellationToken
from pyowl_core.document.document import Fingerprint
from pyowl_core.document.snapshot import OntologyView
from pyowl_core.exceptions import WireCorruptionError, WireError, WireVersionError
from pyowl_core.limits import ParseLimits

from ._binary import Guard
from .codec import encode_snapshot
from .mapping import MappedOntologySnapshot, open_snapshot
from .reference import validate_reference_file


class DurabilityPolicy(str, Enum):
    """How far successful publication is forced to stable storage."""

    NONE = "none"
    DATA = "data"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class CacheEntry:
    structural_fingerprint: Fingerprint
    wire_fingerprint: Fingerprint
    path: Path


@dataclass(frozen=True, slots=True)
class CacheGCReport:
    examined_files: int
    removed_files: int
    reclaimed_bytes: int
    active_files_skipped: int
    retained_bytes: int


_ACTIVE_LOCK = threading.Lock()
_ACTIVE_PATHS: dict[Path, int] = {}


def write_snapshot(
    snapshot: OntologyView,
    path: str | os.PathLike[str],
    *,
    atomic: bool = True,
    durability: DurabilityPolicy = DurabilityPolicy.DATA,
    limits: ParseLimits | None = None,
    cancellation_token: CancellationToken | None = None,
) -> Fingerprint:
    """Encode, validate, and publish a snapshot without exposing partial bytes."""

    if not isinstance(atomic, bool):
        raise TypeError("atomic must be bool")
    if not isinstance(durability, DurabilityPolicy):
        raise TypeError("durability must be DurabilityPolicy")
    selected_limits = ParseLimits() if limits is None else limits
    if not isinstance(selected_limits, ParseLimits):
        raise TypeError("limits must be ParseLimits or None")
    encoded = encode_snapshot(
        snapshot,
        limits=selected_limits,
        cancellation_token=cancellation_token,
    )
    target = Path(os.fspath(path))
    _publish_bytes(
        encoded,
        target,
        atomic=atomic,
        durability=durability,
        limits=selected_limits,
        cancellation_token=cancellation_token,
    )
    return Fingerprint("sha256", 1, encoded[56:88])


class WireCache:
    """Validated, content-addressed PYOCORE cache with safe quarantine/GC."""

    __slots__ = ("durability", "limits", "root")

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        limits: ParseLimits | None = None,
        durability: DurabilityPolicy = DurabilityPolicy.DATA,
    ) -> None:
        if not isinstance(durability, DurabilityPolicy):
            raise TypeError("durability must be DurabilityPolicy")
        selected_limits = ParseLimits() if limits is None else limits
        if not isinstance(selected_limits, ParseLimits):
            raise TypeError("limits must be ParseLimits or None")
        self.root = Path(os.fspath(root)).resolve(strict=False)
        self.limits = selected_limits
        self.durability = durability

    def publish(
        self,
        snapshot: OntologyView,
        *,
        cancellation_token: CancellationToken | None = None,
    ) -> CacheEntry:
        encoded = encode_snapshot(
            snapshot,
            limits=self.limits,
            cancellation_token=cancellation_token,
        )
        structural = snapshot.structural_fingerprint
        wire = Fingerprint("sha256", 1, encoded[56:88])
        target = self._entry_path(structural, wire)
        self._prepare_directory(structural)
        lock = _CacheLock(target.with_suffix(".lock"), self.limits, cancellation_token)
        with lock:
            if target.exists():
                try:
                    existing = open_snapshot(target, limits=self.limits, verify=True)
                    if existing.structural_fingerprint != structural:
                        raise WireCorruptionError("cache entry structural key mismatch")
                    if isinstance(existing, MappedOntologySnapshot):
                        existing.close()
                    return CacheEntry(structural, wire, target)
                except (WireError, OSError):
                    self._quarantine(target)
            _publish_bytes(
                encoded,
                target,
                atomic=True,
                durability=self.durability,
                limits=self.limits,
                cancellation_token=cancellation_token,
            )
        return CacheEntry(structural, wire, target)

    def open(
        self,
        structural_fingerprint: Fingerprint,
        *,
        wire_fingerprint: Fingerprint | None = None,
        verify: bool = True,
    ) -> MappedOntologySnapshot:
        _require_fingerprint(structural_fingerprint, "structural_fingerprint")
        candidates: tuple[Path, ...]
        if wire_fingerprint is not None:
            _require_fingerprint(wire_fingerprint, "wire_fingerprint")
            candidates = (self._entry_path(structural_fingerprint, wire_fingerprint),)
        else:
            directory = self._structural_directory(structural_fingerprint)
            candidates = (
                tuple(sorted(directory.glob("*.pyocore")))
                if directory.is_dir() and not directory.is_symlink()
                else ()
            )
        for candidate in candidates:
            if not _recognized_entry(self.root, candidate) or candidate.is_symlink():
                continue
            try:
                opened = open_snapshot(candidate, limits=self.limits, verify=verify)
                if not isinstance(opened, MappedOntologySnapshot):
                    raise AssertionError("mmap open did not return MappedOntologySnapshot")
                if opened.structural_fingerprint != structural_fingerprint:
                    opened.close()
                    raise WireCorruptionError("cache entry structural key mismatch")
                expected_wire = bytes.fromhex(candidate.stem)
                if opened._mapped_state.inspected.image.header.file_digest != expected_wire:
                    opened.close()
                    raise WireCorruptionError("cache entry wire key mismatch")
                _mark_active(candidate, 1)

                def release_active(selected: Path = candidate) -> None:
                    _mark_active(selected, -1)

                opened._on_close(release_active)
                return opened
            except (WireCorruptionError, WireVersionError, OSError):
                self._quarantine(candidate)
        raise KeyError(structural_fingerprint.hex)

    def get_or_publish(
        self,
        snapshot: OntologyView,
        *,
        cancellation_token: CancellationToken | None = None,
    ) -> MappedOntologySnapshot:
        entry = self.publish(snapshot, cancellation_token=cancellation_token)
        return self.open(
            entry.structural_fingerprint,
            wire_fingerprint=entry.wire_fingerprint,
        )

    def collect(self, *, maximum_bytes: int | None = None) -> CacheGCReport:
        maximum = self.limits.max_disk_cache_bytes if maximum_bytes is None else maximum_bytes
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
            raise ValueError("maximum_bytes must be a nonnegative integer or None")
        candidates: list[tuple[int, Path, int]] = []
        examined = 0
        active_skipped = 0
        retained = 0
        if not self.root.exists():
            return CacheGCReport(0, 0, 0, 0, 0)
        for directory, names, filenames in os.walk(self.root, followlinks=False):
            base = Path(directory)
            names[:] = [
                name
                for name in names
                if not (base / name).is_symlink() and name != ".quarantine"
            ]
            for filename in filenames:
                candidate = base / filename
                if not _recognized_entry(self.root, candidate):
                    continue
                examined += 1
                try:
                    info = candidate.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                normalized = candidate.absolute()
                with _ACTIVE_LOCK:
                    active = _ACTIVE_PATHS.get(normalized, 0) > 0
                if active:
                    active_skipped += 1
                    retained += info.st_size
                    continue
                retained += info.st_size
                candidates.append((info.st_mtime_ns, candidate, info.st_size))
        removed = 0
        reclaimed = 0
        for _mtime, candidate, size in sorted(candidates):
            if retained <= maximum:
                break
            try:
                candidate.unlink()
            except OSError:
                continue
            removed += 1
            reclaimed += size
            retained -= size
        return CacheGCReport(examined, removed, reclaimed, active_skipped, retained)

    def _structural_directory(self, fingerprint: Fingerprint) -> Path:
        return self.root / "wire-v1" / "model-v1" / fingerprint.hex

    def _entry_path(self, structural: Fingerprint, wire: Fingerprint) -> Path:
        return self._structural_directory(structural) / f"{wire.hex}.pyocore"

    def _prepare_directory(self, structural: Fingerprint) -> None:
        try:
            self.root.mkdir(mode=0o700, parents=True)
        except FileExistsError:
            info = self.root.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise OSError("snapshot cache root is not a safe directory") from None
        current = self.root
        for part in ("wire-v1", "model-v1", structural.hex):
            current = current / part
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                info = current.lstat()
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise OSError(
                        "snapshot cache path contains a non-directory or symlink"
                    ) from None

    def _quarantine(self, candidate: Path) -> None:
        if not _recognized_entry(self.root, candidate) or candidate.is_symlink():
            return
        try:
            info = candidate.lstat()
        except OSError:
            return
        if not stat.S_ISREG(info.st_mode):
            return
        quarantine = self.root / ".quarantine"
        try:
            quarantine.mkdir(mode=0o700)
        except FileExistsError:
            info = quarantine.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                return
        destination = quarantine / f"{candidate.stem}.{secrets.token_hex(8)}.corrupt"
        try:
            os.replace(candidate, destination)
        except OSError:
            return


class _CacheLock:
    __slots__ = ("_fd", "_limits", "_path", "_token")

    def __init__(
        self,
        path: Path,
        limits: ParseLimits,
        token: CancellationToken | None,
    ) -> None:
        self._path = path
        self._limits = limits
        self._token = token
        self._fd = -1

    def __enter__(self) -> _CacheLock:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._fd = os.open(
            self._path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        started = time.monotonic()
        deadline = 30.0
        if self._limits.deadline_seconds is not None:
            deadline = min(deadline, self._limits.deadline_seconds)
        while True:
            if self._token is not None:
                self._token.check()
            try:
                _lock_fd(self._fd)
                break
            except BlockingIOError:
                if time.monotonic() - started >= deadline:
                    os.close(self._fd)
                    self._fd = -1
                    raise TimeoutError(
                        "timed out waiting for snapshot cache publication lock"
                    ) from None
                time.sleep(0.025)
        metadata = f"{os.getpid()}\n{time.time_ns()}\n{secrets.token_hex(16)}\n".encode("ascii")
        os.ftruncate(self._fd, 0)
        os.write(self._fd, metadata)
        return self

    def __exit__(self, *_error: object) -> None:
        if self._fd >= 0:
            try:
                _unlock_fd(self._fd)
            finally:
                os.close(self._fd)
                self._fd = -1


def _publish_bytes(
    encoded: bytes,
    target: Path,
    *,
    atomic: bool,
    durability: DurabilityPolicy,
    limits: ParseLimits,
    cancellation_token: CancellationToken | None,
) -> None:
    if not target.parent.is_dir():
        raise FileNotFoundError(target.parent)
    guard = Guard(limits, cancellation_token)
    temporary: Path | None = None
    fd = -1
    try:
        if atomic:
            fd, name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            temporary = Path(name)
            os.fchmod(fd, 0o600)
        else:
            if target.is_symlink():
                raise OSError("refusing to overwrite a symlink with atomic=False")
            fd = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
        _write_all(fd, encoded, guard)
        if durability in (DurabilityPolicy.DATA, DurabilityPolicy.FULL):
            os.fsync(fd)
        os.close(fd)
        fd = -1
        completed = temporary if temporary is not None else target
        validate_reference_file(completed)
        validated = open_snapshot(completed, mmap=False, limits=limits, verify=True)
        if validated.structural_fingerprint.digest == bytes(0):
            raise AssertionError("unreachable zero structural fingerprint")
        if temporary is not None:
            os.replace(temporary, target)
            temporary = None
        if durability is DurabilityPolicy.FULL:
            directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            with suppress(FileNotFoundError):
                temporary.unlink()


def _write_all(fd: int, encoded: bytes, guard: Guard) -> None:
    view = memoryview(encoded)
    offset = 0
    try:
        while offset < len(view):
            guard.check(offset)
            written = os.write(fd, view[offset : offset + 1024 * 1024])
            if written <= 0:
                raise OSError("snapshot wire write made no progress")
            offset += written
        guard.check(force=True)
    finally:
        view.release()


def _recognized_entry(root: Path, candidate: Path) -> bool:
    try:
        relative = candidate.absolute().relative_to(root.absolute())
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    parts = relative.parts
    if len(parts) != 4 or parts[:2] != ("wire-v1", "model-v1"):
        return False
    structural, filename = parts[2:]
    wire = filename.removesuffix(".pyocore")
    return (
        filename.endswith(".pyocore")
        and len(structural) == 64
        and len(wire) == 64
        and _is_hex(structural)
        and _is_hex(wire)
    )


def _is_hex(value: str) -> bool:
    return all(character in "0123456789abcdef" for character in value)


def _mark_active(path: Path, change: int) -> None:
    normalized = path.absolute()
    with _ACTIVE_LOCK:
        selected = _ACTIVE_PATHS.get(normalized, 0) + change
        if selected > 0:
            _ACTIVE_PATHS[normalized] = selected
        else:
            _ACTIVE_PATHS.pop(normalized, None)


def _require_fingerprint(value: Fingerprint, name: str) -> None:
    if not isinstance(value, Fingerprint):
        raise TypeError(f"{name} must be Fingerprint")


def _lock_fd(fd: int) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - exercised by Windows CI
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        except OSError as error:
            raise BlockingIOError from error
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - exercised by Windows CI
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


__all__ = [
    "CacheEntry",
    "CacheGCReport",
    "DurabilityPolicy",
    "WireCache",
    "write_snapshot",
]
