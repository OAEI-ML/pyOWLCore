from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pyowl_core import BackendPreference, DocumentFormat, LoadOptions, parse_document
from tools.benchmark import manifest as manifest_module
from tools.benchmark.manifest import (
    Corpus,
    CorpusCounts,
    ManifestError,
    generated_bytes,
    load_manifest,
    prepare_corpus,
    verify_generated,
    verify_prepared,
)
from tools.benchmark.metrics import summarize
from tools.benchmark.regression import RegressionDataError, compare_reports
from tools.benchmark.synthetic import equivalent_source


def test_manifest_covers_required_lanes_and_locks_every_generated_input() -> None:
    manifest = load_manifest()
    tiers = {corpus.tier for corpus in manifest.corpora}
    families = {family for corpus in manifest.corpora for family in corpus.families}

    assert tiers >= {
        "tiny",
        "small",
        "medium",
        "large",
        "composite",
        "synthetic",
        "adversarial",
    }
    assert families >= {
        "constructors",
        "biomedical",
        "imports",
        "annotation-list-heavy",
        "oaei-composite",
        "synthetic",
        "adversarial",
    }
    for corpus in manifest.corpora:
        assert len(corpus.sha256) == 64
        assert corpus.license_url.startswith("https://")
        if corpus.source == "generated":
            verify_generated(corpus)
            assert hashlib.sha256(generated_bytes(corpus)).hexdigest() == corpus.sha256


def test_equivalent_generated_syntaxes_have_identical_public_structure() -> None:
    documents = []
    for format in DocumentFormat:
        source = equivalent_source(format, 32)
        documents.append(
            parse_document(
                source,
                format=format,
                options=LoadOptions(backend=BackendPreference.PYTHON),
            )
        )

    assert {document.document_fingerprint.hex for document in documents} == {
        documents[0].document_fingerprint.hex
    }
    assert {len(document.axioms) for document in documents} == {63}
    assert {len(document.signature(include_builtins=False)) for document in documents} == {32}


def test_generated_preparation_is_offline_atomic_and_fail_closed(tmp_path: Path) -> None:
    corpus = load_manifest().by_id("generated-tiny-functional")
    path = prepare_corpus(corpus, tmp_path)
    verify_prepared(corpus, path)

    path.write_bytes(path.read_bytes() + b"tamper")
    with pytest.raises(ManifestError, match=r"expected .* bytes"):
        verify_prepared(corpus, path)
    with pytest.raises(ManifestError, match="SHA-256 mismatch"):
        verify_generated(replace(corpus, sha256="0" * 64))


def test_archive_preparation_verifies_archive_member_and_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ontology = b"Ontology(<urn:archive> Declaration(Class(<urn:C>)))\n"
    mapping = b"source\ttarget\n"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("safe/data.ofn", ontology)
        archive.writestr("safe/map.tsv", mapping)
    artifact = stream.getvalue()
    corpus = Corpus(
        id="archive-test",
        tier="composite",
        families=("oaei-composite",),
        source="archive-member",
        format=DocumentFormat.FUNCTIONAL,
        revision="fixture-v1",
        sha256=hashlib.sha256(ontology).hexdigest(),
        counts=CorpusCounts(len(ontology), 2, 1, 1, 0, "fixture-exact"),
        license="CC0-1.0",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        acquired="2026-07-17",
        redistribution="generated",
        url="https://example.org/archive.zip",
        artifact_sha256=hashlib.sha256(artifact).hexdigest(),
        artifact_bytes=len(artifact),
        archive_member="safe/data.ofn",
        mapping_member="safe/map.tsv",
        mapping_sha256=hashlib.sha256(mapping).hexdigest(),
        mapping_bytes=len(mapping),
        mapping_rows=1,
    )
    monkeypatch.setattr(manifest_module, "_download", lambda *_args: artifact)

    path = prepare_corpus(corpus, tmp_path)

    assert path.read_bytes() == ontology
    assert (tmp_path / "archive-test.mappings.tsv").read_bytes() == mapping


def test_archive_preparation_rejects_any_unsafe_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ontology = b"Ontology(<urn:archive>)\n"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("safe/data.ofn", ontology)
        archive.writestr("../escape", b"hostile")
    artifact = stream.getvalue()
    corpus = Corpus(
        id="unsafe-archive-test",
        tier="composite",
        families=("oaei-composite",),
        source="archive-member",
        format=DocumentFormat.FUNCTIONAL,
        revision="fixture-v1",
        sha256=hashlib.sha256(ontology).hexdigest(),
        counts=CorpusCounts(len(ontology), 1, 0, 0, 0, "fixture-exact"),
        license="CC0-1.0",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        acquired="2026-07-17",
        redistribution="generated",
        url="https://example.org/archive.zip",
        artifact_sha256=hashlib.sha256(artifact).hexdigest(),
        artifact_bytes=len(artifact),
        archive_member="safe/data.ofn",
    )
    monkeypatch.setattr(manifest_module, "_download", lambda *_args: artifact)

    with pytest.raises(ManifestError, match="unsafe archive member"):
        prepare_corpus(corpus, tmp_path)


def test_metrics_and_regression_thresholds_are_deterministic_and_fail_closed() -> None:
    distribution = summarize((1, 2, 3, 4, 5))
    assert distribution.count == 5
    assert distribution.median == 3
    assert distribution.p95 == pytest.approx(4.8)
    assert distribution.median_ci95_low <= distribution.median <= distribution.median_ci95_high
    with pytest.raises(ValueError, match="at least one"):
        summarize(())

    baseline = _report(100.0, 100.0, 100.0)
    passing = _report(110.0, 110.0, 115.0)
    comparison = compare_reports(baseline, passing)
    assert comparison.passed

    failing = _report(110.01, 100.0, 100.0)
    assert not compare_reports(baseline, failing).passed
    with pytest.raises(RegressionDataError, match="fingerprint changed"):
        changed = _report(100.0, 100.0, 100.0)
        changed["scenarios"][0]["output"]["fingerprint"] = "changed"
        compare_reports(baseline, changed)
    with pytest.raises(RegressionDataError, match="manifest hash changed"):
        changed_manifest = _report(100.0, 100.0, 100.0)
        changed_manifest["corpus_manifest_sha256"] = "other"
        compare_reports(baseline, changed_manifest)
    with pytest.raises(RegressionDataError, match="machine/runtime comparison key changed"):
        changed_machine = _report(100.0, 100.0, 100.0)
        changed_machine["environment"]["comparison_key"] = "other-machine"
        compare_reports(baseline, changed_machine)


def _report(wall_median: float, rss_median: float, wall_p95: float) -> dict[str, Any]:
    return {
        "schema": "pyowl-core/performance-run/v1",
        "corpus_manifest_sha256": "manifest",
        "environment": {"git_commit": "commit", "comparison_key": "machine"},
        "methodology": {
            "cache_state": "resident",
            "warmups": 1,
            "repetitions": 20,
            "safety_defaults": True,
        },
        "scenarios": [
            {
                "id": "query",
                "kind": "query",
                "status": "ok",
                "required": True,
                "metrics": {
                    "wall_ns": {"median": wall_median, "p95": wall_p95},
                    "rss_peak_bytes": {"median": rss_median},
                },
                "output": {"fingerprint": "same"},
            }
        ],
    }
