from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import tools.benchmark.comparators.runner as runner_module
import tools.benchmark.report as report_module
from pyowl_core.backends import native
from tools.benchmark.report import ReportError, canonical_json_bytes, collect_environment


def test_operator_machine_observations_are_recorded_and_hash_the_legacy_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_environment(monkeypatch, cpu_model=None)

    environment = collect_environment(
        tmp_path,
        reference_cpu_model="Approved CPU",
        reference_storage="NVMe device; warmed file cache",
        reference_power_mode="fixed performance",
    )

    assert environment["cpu"]["model"] == "Approved CPU"
    assert environment["storage"] == "NVMe device; warmed file cache"
    assert environment["power_mode"] == "fixed performance"
    assert environment["machine_observation_sources"] == {
        "cpu_model": "operator-supplied",
        "storage": "operator-supplied",
        "power_mode": "operator-supplied",
    }
    comparison_fields = {
        key: environment[key]
        for key in (
            "platform",
            "cpu",
            "memory",
            "python",
            "rust",
            "power_mode",
            "storage",
        )
    }
    comparison_fields["native_available"] = True
    comparison_fields["native_features"] = ["retained"]
    assert (
        environment["comparison_key"]
        == hashlib.sha256(canonical_json_bytes(comparison_fields)).hexdigest()
    )


def test_reference_machine_match_requires_observed_cpu_and_operator_storage_power(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_environment(monkeypatch, cpu_model="Approved CPU")
    environment = collect_environment(
        tmp_path,
        reference_storage="NVMe device; warmed file cache",
        reference_power_mode="fixed performance",
    )
    platform = cast(dict[str, object], environment["platform"])
    machine = SimpleNamespace(
        os=" ".join(str(platform[name]) for name in ("system", "release", "machine")),
        cpu=f"{os.cpu_count()} logical CPUs; Approved CPU",
        memory_bytes=32 * 1024**3,
        storage="NVMe device; warmed file cache",
        power_mode="fixed performance",
    )

    evidence = runner_module._reference_machine_evidence(
        cast(Any, SimpleNamespace(reference_machine=machine)),
        environment,
    )

    assert evidence["matches"] is True
    assert evidence["field_matches"] == {
        "os": True,
        "cpu": True,
        "memory_bytes": True,
        "storage": True,
        "power_mode": True,
    }
    without_operator_provenance = dict(environment)
    without_operator_provenance["machine_observation_sources"] = {
        "cpu_model": "platform-probe",
        "storage": "not-measured",
        "power_mode": "not-measured",
    }
    rejected = runner_module._reference_machine_evidence(
        cast(Any, SimpleNamespace(reference_machine=machine)),
        without_operator_provenance,
    )
    assert rejected["matches"] is False
    rejected_fields = cast(dict[str, bool], rejected["field_matches"])
    assert rejected_fields["storage"] is False
    assert rejected_fields["power_mode"] is False


def test_operator_cpu_model_cannot_override_a_successful_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_environment(monkeypatch, cpu_model="Probed CPU")

    with pytest.raises(ReportError, match="differs from the platform probe"):
        collect_environment(tmp_path, reference_cpu_model="Different CPU")


@pytest.mark.parametrize(
    "value",
    (
        "",
        " leading",
        "trailing ",
        "line\nbreak",
        "NVMe\u0085cache",
        "NVMe\u202ecache",
        "\ud800",
        "x" * 4_097,
        1,
    ),
)
def test_operator_machine_observations_reject_ambiguous_or_unbounded_values(
    value: object,
    tmp_path: Path,
) -> None:
    with pytest.raises(ReportError, match="reference storage"):
        collect_environment(tmp_path, reference_storage=cast(Any, value))


def test_cli_propagates_explicit_reference_observations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def run(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"comparative_complete": False, "contract_valid": True}

    monkeypatch.setattr(runner_module, "run_comparator_baseline", run)

    assert (
        runner_module.main(
            (
                "--reference-cpu-model",
                "Approved CPU",
                "--reference-storage",
                "NVMe device; warmed file cache",
                "--reference-power-mode",
                "fixed performance",
                "--allow-partial",
            )
        )
        == 0
    )
    assert captured["reference_cpu_model"] == "Approved CPU"
    assert captured["reference_storage"] == "NVMe device; warmed file cache"
    assert captured["reference_power_mode"] == "fixed performance"
    assert '"contract_valid": true' in capsys.readouterr().out


def test_programmatic_run_rejects_reference_observations_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_start(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("persistent lifecycle started before observation validation")

    monkeypatch.setattr(runner_module, "_start_persistent_lifecycles", unexpected_start)
    with pytest.raises(
        runner_module.ComparatorRunError,
        match="reference_storage: reference storage",
    ):
        runner_module.run_comparator_baseline(reference_storage="")


def _stub_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cpu_model: str | None,
) -> None:
    def command(
        command: tuple[str, ...],
        *,
        cwd: Path,
        required: bool,
    ) -> str | None:
        del cwd, required
        if command[:2] == ("git", "status"):
            return ""
        return "captured"

    monkeypatch.setattr(report_module, "_command", command)
    monkeypatch.setattr(report_module, "_cpu_model", lambda: cpu_model)
    monkeypatch.setattr(report_module, "_physical_memory_bytes", lambda: 32 * 1024**3)
    monkeypatch.setattr(report_module, "_native_artifact", lambda _root: None)
    monkeypatch.setattr(report_module, "_tool_versions", lambda: {})
    monkeypatch.setattr(
        native,
        "probe",
        lambda: SimpleNamespace(
            available=True,
            reason=None,
            version="test",
            features=("retained",),
        ),
    )
