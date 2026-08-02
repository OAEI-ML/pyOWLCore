"""Bounded acquisition tests for checksum-pinned benchmark corpora."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.benchmark import manifest as manifest_module
from tools.benchmark.manifest import ManifestError, load_manifest, prepare_corpus


def test_default_download_ceiling_comes_from_the_exact_manifest_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    corpus = load_manifest().by_id("oaei-bioml-ncit-2026")
    observed: list[int] = []

    def stop_after_observing(_url: str, _timeout: float, limit: int) -> bytes:
        observed.append(limit)
        raise ManifestError("bounded test stop")

    monkeypatch.setattr(manifest_module, "_download", stop_after_observing)
    with pytest.raises(ManifestError, match="bounded test stop"):
        prepare_corpus(corpus, tmp_path)

    assert observed == [corpus.counts.bytes]


def test_explicit_download_ceiling_can_only_tighten_the_manifest_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    corpus = load_manifest().by_id("oaei-bioml-ncit-2026")
    observed: list[int] = []

    def stop_after_observing(_url: str, _timeout: float, limit: int) -> bytes:
        observed.append(limit)
        raise ManifestError("bounded test stop")

    monkeypatch.setattr(manifest_module, "_download", stop_after_observing)
    with pytest.raises(ManifestError, match="bounded test stop"):
        prepare_corpus(corpus, tmp_path, max_download_bytes=123)

    assert observed == [123]
