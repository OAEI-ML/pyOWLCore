from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import pytest

from tools.benchmark.comparators.manifest import (
    COMMON_BOUNDARY,
    RAW_HORNED_BOUNDARY,
    REQUIRED_IMPLEMENTATIONS,
    REQUIRED_PHASES,
    ComparatorManifestError,
    load_comparator_manifest,
)
from tools.benchmark.comparators.runner import ComparatorRunError, check_comparator_contract

MANIFEST = Path("benchmarks/comparators/comparators.toml")
HORNED_1_4_0_SHA256 = "877f6118b6f5823bb135d04e36fe2c2d3a2b4493feca8ac09b5fa6e91b9fff9e"
OWLAPI_5_5_1_SHA256 = "747b1a5269fee2992487dcde946f16dfbc14aa458d50854994a0485cf263ce07"


def test_pin_ledger_covers_normative_lanes_and_exact_phases() -> None:
    manifest = check_comparator_contract()

    assert len(manifest.comparators) == 7
    assert {value.implementation for value in manifest.comparators} >= REQUIRED_IMPLEMENTATIONS
    assert tuple(value.phase for value in manifest.timing_fences) == REQUIRED_PHASES
    assert manifest.reference_machine.approval == "pending"

    raw = manifest.by_id("horned-owl-raw")
    common = manifest.by_id("horned-owl-common")
    assert raw.boundary == RAW_HORNED_BOUNDARY
    assert raw.required is True
    assert raw.gating is False
    assert common.boundary == COMMON_BOUNDARY
    assert common.required is True
    assert common.gating is True
    assert raw.artifact_sha256 == HORNED_1_4_0_SHA256
    assert common.artifact_sha256 == HORNED_1_4_0_SHA256

    for field in (
        "version",
        "revision",
        "source_url",
        "artifact",
        "artifact_sha256",
        "features",
        "allocator",
        "thread_ceiling",
    ):
        assert getattr(raw, field) == getattr(common, field)

    for pin in manifest.comparators:
        if pin.boundary == COMMON_BOUNDARY:
            assert pin.required is True
            assert pin.gating is True

    for phase in (
        "document_fingerprint",
        "structural_fingerprint",
        "logical_fingerprint",
        "signature_fingerprint",
        "common_adapter_traversal",
        "common_adapter_digests",
    ):
        assert manifest.fence(phase).lanes[raw.id] == "not-applicable"
        assert manifest.fence(phase).lanes[common.id] == "inside"
    assert set(manifest.fence("equality_assertion").lanes.values()) == {"outside"}


def test_external_runners_are_fail_closed_except_completed_pins() -> None:
    manifest = load_comparator_manifest()
    external = tuple(pin for pin in manifest.comparators if pin.adapter == "external-command")

    assert {pin.id for pin in external} == {
        "pyowl-direct-rust-common",
        "horned-owl-raw",
        "horned-owl-common",
        "py-horned-common",
        "owlapi-common",
    }
    py_horned = manifest.by_id("py-horned-common")
    runner = Path("benchmarks/comparators/runners/py_horned_common.py")
    assert py_horned.runner_pin_state == "complete"
    assert py_horned.runner_revision == "pyowl-core-py-horned-common-runner-v3"
    assert py_horned.runner_sha256 == hashlib.sha256(runner.read_bytes()).hexdigest()
    assert py_horned.artifact_is_runnable is True

    raw_horned = manifest.by_id("horned-owl-raw")
    assert raw_horned.runner_pin_state == "complete"
    assert raw_horned.runner_revision == "pyowl-core-horned-raw-runner-v2"
    assert raw_horned.runner_sha256 == (
        "37b64e372a4f31a19040f8620cdcf1288c3049d0c00e614192d1861429c15bce"
    )
    assert raw_horned.artifact_is_runnable is True

    common_horned = manifest.by_id("horned-owl-common")
    assert common_horned.runner_pin_state == "complete"
    assert common_horned.runner_revision == "pyowl-core-horned-common-runner-v3"
    assert common_horned.runner_sha256 == raw_horned.runner_sha256
    assert common_horned.artifact_is_runnable is True

    direct = manifest.by_id("pyowl-direct-rust-common")
    assert direct.runner_pin_state == "complete"
    assert direct.runner_revision == "pyowl-core-direct-rust-common-runner-v2"
    assert direct.runner_sha256 == (
        "4ffb33b5aa51bda989b114da902060e153d5daf2df6d423bb5fe68bc49be5cf1"
    )
    assert direct.artifact_is_runnable is True

    owlapi = manifest.by_id("owlapi-common")
    owlapi_runner = Path("benchmarks/comparators/runners/owlapi/launcher.sh")
    assert owlapi.artifact_sha256 == OWLAPI_5_5_1_SHA256
    assert owlapi.runner_pin_state == "complete"
    assert owlapi.runner_revision == "pyowl-core-owlapi-common-runner-v2"
    assert owlapi.runner_sha256 == hashlib.sha256(owlapi_runner.read_bytes()).hexdigest()
    assert owlapi.artifact_is_runnable is True

    for pin in external:
        if pin in {py_horned, raw_horned, common_horned, direct, owlapi}:
            continue
        assert pin.runner_pin_state == "pending"
        assert pin.runner_revision
        assert pin.runner_sha256 is None
        assert pin.artifact_is_runnable is False


def test_external_runner_requires_its_own_complete_hash_before_runnable(
    tmp_path: Path,
) -> None:
    source = _manifest_source()
    missing_hash_source = _replace_in_lane(
        source,
        "horned-owl-raw",
        'runner_sha256 = "37b64e372a4f31a19040f8620cdcf1288c3049d0c00e614192d1861429c15bce"',
        "",
    )
    missing_hash = _write_manifest(tmp_path, "missing-runner-hash.toml", missing_hash_source)

    with pytest.raises(ComparatorManifestError, match="runner requires SHA-256"):
        load_comparator_manifest(missing_hash)

    complete_path = _write_manifest(tmp_path, "complete-runner.toml", source)
    raw = load_comparator_manifest(complete_path).by_id("horned-owl-raw")

    assert raw.runner_revision == "pyowl-core-horned-raw-runner-v2"
    assert raw.runner_sha256 == ("37b64e372a4f31a19040f8620cdcf1288c3049d0c00e614192d1861429c15bce")
    assert raw.artifact_is_runnable is True


def test_timing_fence_lane_mapping_is_immutable() -> None:
    lanes = load_comparator_manifest().fence("byte_receipt").lanes

    with pytest.raises(TypeError):
        cast(Any, lanes)["horned-owl-raw"] = "outside"


@pytest.mark.parametrize(
    ("needle", "replacement"),
    (
        ("schema = 1\n", "schema = 1\nunknown_root_field = true\n"),
        (
            '[reference_machine]\nid = "shared-darwin25-x86_64"',
            '[reference_machine]\nunknown_machine_field = true\nid = "shared-darwin25-x86_64"',
        ),
        (
            'id = "pyowl-python-common"\nimplementation = "pyowl-core-python"',
            'id = "pyowl-python-common"\nunknown_pin_field = true\n'
            'implementation = "pyowl-core-python"',
        ),
        (
            '[[timing_fence]]\nphase = "byte_receipt"',
            '[[timing_fence]]\nunknown_fence_field = true\nphase = "byte_receipt"',
        ),
    ),
)
def test_manifest_rejects_unknown_fields(tmp_path: Path, needle: str, replacement: str) -> None:
    changed = _manifest_source().replace(needle, replacement, 1)
    path = _write_manifest(tmp_path, "unknown-field.toml", changed)

    with pytest.raises(ComparatorManifestError, match="unknown fields"):
        load_comparator_manifest(path)


@pytest.mark.parametrize(
    ("lane", "needle", "replacement"),
    (
        (
            "pyowl-python-common",
            'implementation = "pyowl-core-python"',
            'implementation = "pyowl-core-native-wheel"',
        ),
        (
            "pyowl-native-wheel-common",
            'boundary = "common-contract-ready"',
            'boundary = "horned-model-ready"',
        ),
        (
            "pyowl-direct-rust-common",
            'adapter = "external-command"',
            'adapter = "core-python"',
        ),
        ("horned-owl-raw", "required = true", "required = false"),
        ("horned-owl-common", "gating = true", "gating = false"),
        ("py-horned-common", "required = true", "required = false"),
        ("owlapi-common", "gating = true", "gating = false"),
    ),
)
def test_manifest_rejects_normative_lane_policy_weakening(
    tmp_path: Path, lane: str, needle: str, replacement: str
) -> None:
    changed = _replace_in_lane(_manifest_source(), lane, needle, replacement)
    path = _write_manifest(tmp_path, f"weakened-{lane}.toml", changed)

    with pytest.raises(ComparatorManifestError):
        load_comparator_manifest(path)


def test_manifest_rejects_renamed_normative_lane(tmp_path: Path) -> None:
    changed = _replace_in_lane(
        _manifest_source(),
        "owlapi-common",
        'id = "owlapi-common"',
        'id = "owlapi-optional"',
    ).replace("owlapi-common =", "owlapi-optional =")
    path = _write_manifest(tmp_path, "renamed-lane.toml", changed)

    with pytest.raises(ComparatorManifestError, match="normative seven lanes"):
        load_comparator_manifest(path)


def test_manifest_rejects_different_raw_and_common_horned_engine_pins(
    tmp_path: Path,
) -> None:
    changed = _replace_in_lane(
        _manifest_source(),
        "horned-owl-common",
        'version = "1.4.0"',
        'version = "1.4.1"',
    )
    path = _write_manifest(tmp_path, "different-engine-pin.toml", changed)

    with pytest.raises(ComparatorManifestError, match="engine pins differ"):
        load_comparator_manifest(path)


def test_comparator_contract_rejects_changed_corpus_lock(tmp_path: Path) -> None:
    corpus = tmp_path / "corpora.toml"
    corpus.write_text("changed", encoding="utf-8")

    with pytest.raises(ComparatorRunError, match="different corpus manifest"):
        check_comparator_contract(corpus_manifest_path=corpus)


def _manifest_source() -> str:
    return MANIFEST.read_text(encoding="utf-8")


def _write_manifest(tmp_path: Path, name: str, value: str) -> Path:
    path = tmp_path / name
    path.write_text(value, encoding="utf-8")
    return path


def _replace_in_lane(source: str, lane: str, needle: str, replacement: str) -> str:
    marker = f'id = "{lane}"'
    start = source.index(marker)
    end = source.find("\n[[comparator]]", start)
    if end < 0:
        end = source.index("\n[[timing_fence]]", start)
    row = source[start:end]
    if needle not in row:
        raise AssertionError(f"{needle!r} is absent from comparator lane {lane!r}")
    updated = row.replace(needle, replacement, 1)
    return source[:start] + updated + source[end:]
