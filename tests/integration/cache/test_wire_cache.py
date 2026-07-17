from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pyowl_core import DurabilityPolicy, WireCache, encode_snapshot, write_snapshot
from tests.unit.wire.conftest import snapshot


def test_atomic_write_mode_digest_and_crash_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = snapshot("A")
    path = tmp_path / "snapshot.pyocore"
    digest = write_snapshot(source, path, durability=DurabilityPolicy.FULL)
    assert digest.digest == path.read_bytes()[56:88]
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
