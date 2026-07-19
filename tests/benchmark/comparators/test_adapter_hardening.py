from __future__ import annotations

import copy
import hashlib
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pyowl_core import DocumentFormat, load_snapshot
from tools.benchmark.comparators.adapters import (
    ADAPTER_RESULT_SCHEMA,
    RAW_INVENTORY_SCHEMA,
    TIMED_VALIDATION_SCHEMA,
    AdapterRequest,
    _validate_external_result,
    default_options,
    options_digest,
    raw_inventory_digest,
    run_bounded_subprocess,
    run_external_adapter,
    sanitize_failure,
)
from tools.benchmark.comparators.common_contract import (
    build_core_common_contract,
    validate_common_contract,
)
from tools.benchmark.comparators.manifest import load_comparator_manifest
from tools.benchmark.manifest import generated_bytes, load_manifest


def test_adapter_request_recomputes_options_digest_and_rejects_format_drift() -> None:
    corpus, source, options = _tiny_input()
    digest = options_digest(options)

    with pytest.raises(ValueError, match="options differ"):
        AdapterRequest(
            corpus_id=corpus.id,
            source=source,
            source_sha256=corpus.sha256,
            format=corpus.format,
            options=options,
            options_sha256="0" * 64,
            input_mode="resident-bytes",
            process_mode="steady-process",
        )

    with pytest.raises(ValueError, match="format differs"):
        AdapterRequest(
            corpus_id=corpus.id,
            source=source,
            source_sha256=corpus.sha256,
            format=DocumentFormat.OWL_XML,
            options=options,
            options_sha256=digest,
            input_mode="resident-bytes",
            process_mode="steady-process",
        )


def test_bounded_subprocess_enforces_time_and_output_ceilings() -> None:
    timed = run_bounded_subprocess(
        (sys.executable, "-c", "import time; time.sleep(2)"),
        b"",
        timeout=0.05,
        max_stdout_bytes=64,
        max_stderr_bytes=64,
    )
    assert timed.timed_out is True
    assert len(timed.stdout) <= 64
    assert len(timed.stderr) <= 64

    oversized = run_bounded_subprocess(
        (sys.executable, "-c", "import sys; sys.stdout.write('x' * 100000)"),
        b"",
        timeout=2,
        max_stdout_bytes=64,
        max_stderr_bytes=64,
    )
    assert oversized.output_limit == "stdout"
    assert len(oversized.stdout) == 64


def test_external_adapter_timeout_is_an_error_not_an_unbounded_hang(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    corpus, source, options = _tiny_input()
    runner = tmp_path / "sleeping-runner"
    runner.write_text(
        f"#!{sys.executable}\nimport time\ntime.sleep(2)\n",
        encoding="utf-8",
    )
    runner.chmod(0o700)
    pin = replace(
        load_comparator_manifest().by_id("horned-owl-raw"),
        runner_pin_state="complete",
        runner_revision="test-runner",
        runner_sha256=_file_sha256(str(runner)),
    )
    assert pin.launcher_env is not None
    monkeypatch.setenv(
        pin.launcher_env,
        str(runner),
    )
    request = AdapterRequest(
        corpus_id=corpus.id,
        source=source,
        source_sha256=corpus.sha256,
        format=corpus.format,
        options=options,
        options_sha256=options_digest(options),
        input_mode="resident-bytes",
        process_mode="fresh-process",
    )

    result = run_external_adapter(pin, request, timeout_seconds=0.05)

    assert result["status"] == "error"
    assert "exceeded" in result["reason"]
    assert len(result["reason"]) <= 1_000


def test_external_steady_mode_is_not_mislabeled_as_a_fresh_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus, source, options = _tiny_input()
    pin = replace(
        load_comparator_manifest().by_id("horned-owl-raw"),
        runner_pin_state="complete",
        runner_revision="test-runner",
        runner_sha256=_file_sha256(sys.executable),
    )
    assert pin.launcher_env is not None
    monkeypatch.setenv(pin.launcher_env, sys.executable)
    request = AdapterRequest(
        corpus_id=corpus.id,
        source=source,
        source_sha256=corpus.sha256,
        format=corpus.format,
        options=options,
        options_sha256=options_digest(options),
        input_mode="resident-bytes",
        process_mode="steady-process",
    )

    result = run_external_adapter(pin, request)

    assert result["status"] == "not-run"
    assert "audited persistent lifecycle" in result["reason"]


def test_failure_diagnostics_are_redacted_flattened_and_bounded() -> None:
    rendered = sanitize_failure(
        "token=abc Bearer def /Users/person/private https://example.invalid/x\x00" + "z" * 200,
        limit=100,
    )

    assert len(rendered) <= 100
    assert "abc" not in rendered
    assert "def" not in rendered
    assert "/Users/person" not in rendered
    assert "example.invalid" not in rendered
    assert "\x00" not in rendered
    assert "<redacted>" in rendered
    assert "<path>" in rendered
    assert "<url>" in rendered


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda value: value["metrics"].pop("object_count"), "object_count"),
        (
            lambda value: value["metrics"].__setitem__("wall_ns", 1.5),
            "must be an integer",
        ),
        (
            lambda value: value["timed_validation"].__setitem__("full_contract_validation", False),
            "full contract validation",
        ),
        (
            lambda value: value["artifact"].__setitem__("runner_sha256", "b" * 64),
            "runner_sha256",
        ),
    ),
)
def test_external_success_requires_complete_bounded_evidence(
    mutation: Any,
    message: str,
) -> None:
    pin, request, result = _valid_external_result()
    mutation(result)

    with pytest.raises((TypeError, ValueError), match=message):
        _validate_external_result(pin, request, result)


def test_external_success_accepts_pinned_artifacts_and_timed_contract_attestation() -> None:
    pin, request, result = _valid_external_result()

    protocol = request.protocol_dict(pin)
    assert protocol["schema"] == "pyowl-core/comparator-adapter-request/v2"
    assert protocol["document_iri"] == request.document_iri
    assert protocol["expected_artifact_sha256"] == pin.artifact_sha256
    assert protocol["expected_runner_sha256"] == pin.runner_sha256
    _validate_external_result(pin, request, result)


def test_raw_inventory_requires_integer_counts_and_its_canonical_digest() -> None:
    pin, request, result = _valid_raw_external_result()
    _validate_external_result(pin, request, result)

    fractional = copy.deepcopy(result)
    fractional["raw_inventory"]["axiom_count"] = 1.5
    with pytest.raises(TypeError, match="must be an integer"):
        _validate_external_result(pin, request, fractional)

    unauthenticated = copy.deepcopy(result)
    unauthenticated["raw_inventory"]["inventory_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="canonical scalar preimage"):
        _validate_external_result(pin, request, unauthenticated)


def _tiny_input() -> tuple[Any, bytes, Any]:
    corpus = load_manifest().by_id("generated-tiny-functional")
    return corpus, generated_bytes(corpus), default_options(corpus.format)


def _valid_external_result() -> tuple[Any, AdapterRequest, dict[str, Any]]:
    corpus, source, options = _tiny_input()
    pin = replace(
        load_comparator_manifest().by_id("horned-owl-common"),
        runner_pin_state="complete",
        runner_revision="independent-adapter-v1",
        runner_sha256="a" * 64,
    )
    request = AdapterRequest(
        corpus_id=corpus.id,
        source=source,
        source_sha256=hashlib.sha256(source).hexdigest(),
        format=corpus.format,
        options=options,
        options_sha256=options_digest(options),
        input_mode="resident-bytes",
        process_mode="steady-process",
    )
    contract = build_core_common_contract(
        load_snapshot(source, options=options),
        corpus_id=request.corpus_id,
        source_sha256=request.source_sha256,
        options_sha256=request.options_sha256,
    )
    validate_common_contract(contract)
    result: dict[str, Any] = {
        "schema": ADAPTER_RESULT_SCHEMA,
        "lane": pin.id,
        "implementation": pin.implementation,
        "boundary": pin.boundary,
        "status": "ok",
        "reason": None,
        "corpus_id": request.corpus_id,
        "source_sha256": request.source_sha256,
        "options_sha256": request.options_sha256,
        "input_mode": request.input_mode,
        "process_mode": request.process_mode,
        "contract": contract,
        "raw_inventory": None,
        "metrics": {
            "wall_ns": 100,
            "cpu_ns": 90,
            "load_ns": 40,
            "common_adapter_ns": 50,
            "rss_peak_before_bytes": 1_000,
            "rss_peak_after_bytes": 1_100,
            "rss_peak_increment_bytes": 100,
            "temporary_bytes": 0,
            "object_count": 10,
        },
        "timed_validation": {
            "schema": TIMED_VALIDATION_SCHEMA,
            "inside_timed_envelope": True,
            "full_contract_validation": True,
            "contract_sha256": contract["contract_sha256"],
            "validation_ns": 10,
        },
        "artifact": {
            "pin_state": pin.pin_state,
            "version": pin.version,
            "revision": pin.revision,
            "artifact": pin.artifact,
            "artifact_sha256": pin.artifact_sha256,
            "features": list(pin.features),
            "allocator": pin.allocator,
            "thread_ceiling": pin.thread_ceiling,
            "runner_revision": pin.runner_revision,
            "runner_sha256": pin.runner_sha256,
        },
    }
    return pin, request, copy.deepcopy(result)


def _valid_raw_external_result() -> tuple[Any, AdapterRequest, dict[str, Any]]:
    _common_pin, request, result = _valid_external_result()
    pin = replace(
        load_comparator_manifest().by_id("horned-owl-raw"),
        runner_pin_state="complete",
        runner_revision="independent-raw-runner-v1",
        runner_sha256="b" * 64,
    )
    counts = {
        "axiom_count": 4,
        "annotation_count": 0,
        "import_count": 0,
        "entity_count": 3,
        "diagnostic_count": 0,
    }
    inventory = {
        "schema": RAW_INVENTORY_SCHEMA,
        "model_kind": "horned-model-ready",
        **counts,
        "inventory_sha256": raw_inventory_digest(**counts),
    }
    result.update(
        {
            "lane": pin.id,
            "implementation": pin.implementation,
            "boundary": pin.boundary,
            "contract": None,
            "raw_inventory": inventory,
            "timed_validation": None,
        }
    )
    result["metrics"].pop("common_adapter_ns")
    result["artifact"] = {
        "pin_state": pin.pin_state,
        "version": pin.version,
        "revision": pin.revision,
        "artifact": pin.artifact,
        "artifact_sha256": pin.artifact_sha256,
        "features": list(pin.features),
        "allocator": pin.allocator,
        "thread_ceiling": pin.thread_ceiling,
        "runner_revision": pin.runner_revision,
        "runner_sha256": pin.runner_sha256,
    }
    return pin, request, result


def _file_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
