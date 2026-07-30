from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pyowl_core import DurabilityPolicy, WireCache, encode_snapshot, write_snapshot
from pyowl_core.wire import cache as cache_module
from tests.unit.wire.conftest import snapshot


def test_atomic_write_mode_digest_and_crash_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = snapshot("A")
    path = tmp_path / "snapshot.pyocore"
    digest = write_snapshot(source, path, durability=DurabilityPolicy.FULL)
    assert digest.digest == path.read_bytes()[56:88]
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    failed = tmp_path / "failed.pyocore"
    original_replace = os.replace

    def injected_replace(source_path, target_path):  # type: ignore[no-untyped-def]
        if Path(target_path) == failed:
            raise OSError("injected publication crash")
        return original_replace(source_path, target_path)

    monkeypatch.setattr(os, "replace", injected_replace)
    with pytest.raises(OSError, match="injected"):
        write_snapshot(source, failed)
    assert not failed.exists()
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_atomic_write_uses_path_chmod_when_descriptor_chmod_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chmod_calls: list[tuple[object, int]] = []
    original_chmod = cache_module.os.chmod

    def recording_chmod(path: object, mode: int) -> None:
        chmod_calls.append((path, mode))
        original_chmod(path, mode)

    monkeypatch.delattr(cache_module.os, "fchmod", raising=False)
    monkeypatch.setattr(cache_module.os, "chmod", recording_chmod)
    path = tmp_path / "portable.pyocore"

    write_snapshot(snapshot("A"), path, durability=DurabilityPolicy.NONE)

    assert path.is_file()
    assert len(chmod_calls) == 1
    assert chmod_calls[0][1] == 0o600
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_concurrent_publish_converges_and_corruption_is_quarantined(tmp_path: Path) -> None:
    source = snapshot("A", "B")
    cache = WireCache(tmp_path / "cache")
    with ThreadPoolExecutor(max_workers=8) as executor:
        entries = tuple(executor.map(lambda _index: cache.publish(source), range(16)))
    assert len({entry.path for entry in entries}) == 1
    entry = entries[0]
    assert entry.path.read_bytes() == encode_snapshot(source)

    damaged = bytearray(entry.path.read_bytes())
    damaged[-1] ^= 1
    entry.path.write_bytes(damaged)
    rebuilt = cache.publish(source)
    assert rebuilt.path == entry.path
    assert rebuilt.path.read_bytes() == encode_snapshot(source)
    assert tuple((cache.root / ".quarantine").glob("*.corrupt"))


def test_cache_gc_skips_active_mapping_then_reclaims(tmp_path: Path) -> None:
    source = snapshot("A")
    cache = WireCache(tmp_path / "cache")
    entry = cache.publish(source)
    mapped = cache.open(entry.structural_fingerprint, wire_fingerprint=entry.wire_fingerprint)
    first = cache.collect(maximum_bytes=0)
    assert first.active_files_skipped == 1
    assert first.removed_files == 0
    mapped.close()
    second = cache.collect(maximum_bytes=0)
    assert second.removed_files == 1
    assert not entry.path.exists()
