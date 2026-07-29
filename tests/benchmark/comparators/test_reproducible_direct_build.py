from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.benchmark.comparators import build_direct_runner as build_module
from tools.benchmark.comparators.build_direct_runner import (
    DirectRunnerBuildError,
    build_direct_runner,
    direct_runner_artifact,
    reproducible_environment,
)
from tools.benchmark.comparators.reproducible_rustc import (
    _local_metadata,
    _replace_cargo_metadata,
)


def _root_with_wrapper(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    wrapper = root / "tools/benchmark/comparators/reproducible_rustc.py"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    wrapper.chmod(0o755)
    return root


def test_direct_build_environment_remaps_every_host_path_and_darwin_uuid(
    tmp_path: Path,
) -> None:
    root = _root_with_wrapper(tmp_path)
    target = tmp_path / "target"
    cargo_home = tmp_path / "cargo-home"

    selected = reproducible_environment(
        root,
        target,
        environ={"CARGO_HOME": str(cargo_home), "PATH": "/bin"},
        platform="darwin",
    )

    flags = selected["CARGO_ENCODED_RUSTFLAGS"].split("\x1f")
    assert flags == [
        f"--remap-path-prefix={target.resolve()}=/rust/target",
        f"--remap-path-prefix={root.resolve()}=/rust/pyowl-core",
        f"--remap-path-prefix={cargo_home.resolve() / 'registry' / 'src'}=/rust/cargo-registry",
        f"--remap-path-prefix={cargo_home.resolve() / 'git' / 'checkouts'}=/rust/cargo-git",
        "-C",
        "link-arg=-Wl,-no_uuid",
    ]
    assert selected["RUSTC_WRAPPER"] == str(
        root / "tools/benchmark/comparators/reproducible_rustc.py"
    )
    assert selected["PYOWL_CORE_DIRECT_REPRO_ROOT"] == str(root.resolve())
    assert selected["CARGO_INCREMENTAL"] == "0"
    assert selected["CARGO_TARGET_DIR"] == str(target.resolve())


def test_windows_build_keeps_path_remaps_but_does_not_spawn_python_wrapper(
    tmp_path: Path,
) -> None:
    root = tmp_path / "checkout"
    target = tmp_path / "target"

    selected = reproducible_environment(
        root,
        target,
        environ={"CARGO_HOME": str(tmp_path / "cargo-home")},
        platform="win32",
    )

    flags = selected["CARGO_ENCODED_RUSTFLAGS"].split("\x1f")
    assert all("no_uuid" not in flag for flag in flags)
    assert "RUSTC_WRAPPER" not in selected
    assert "PYOWL_CORE_DIRECT_REPRO_ROOT" not in selected
    assert direct_runner_artifact(target, platform="win32").name.endswith(".exe")


@pytest.mark.parametrize(
    "variable",
    (
        "CARGO_BUILD_RUSTC_WRAPPER",
        "CARGO_ENCODED_RUSTFLAGS",
        "RUSTC",
        "RUSTC_WORKSPACE_WRAPPER",
        "RUSTC_WRAPPER",
        "RUSTFLAGS",
    ),
)
def test_direct_build_rejects_external_compiler_seams(
    tmp_path: Path,
    variable: str,
) -> None:
    root = _root_with_wrapper(tmp_path)

    with pytest.raises(DirectRunnerBuildError, match=variable):
        reproducible_environment(
            root,
            tmp_path / "target",
            environ={variable: "host-controlled"},
            platform="darwin",
        )


def test_wrapper_replaces_only_cargo_metadata_and_preserves_artifact_suffix() -> None:
    arguments = [
        "--crate-name",
        "_native",
        "-C",
        "metadata=host-path-dependent",
        "-C",
        "extra-filename=-cargo-expected",
    ]

    assert _replace_cargo_metadata(arguments, "stable") == [
        "--crate-name",
        "_native",
        "-C",
        "extra-filename=-cargo-expected",
        "-C",
        "metadata=stable",
    ]


@pytest.mark.parametrize(
    "arguments",
    (
        ["--crate-name", "_native"],
        [
            "--crate-name",
            "_native",
            "-C",
            "metadata=first",
            "-Cmetadata=second",
        ],
    ),
)
def test_wrapper_fails_closed_on_missing_or_multiple_cargo_metadata(
    arguments: list[str],
) -> None:
    with pytest.raises(RuntimeError, match="exactly one Cargo metadata option"):
        _replace_cargo_metadata(arguments, "stable")


def test_wrapper_metadata_is_independent_of_checkout_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CARGO_PKG_VERSION", "0.1.0-dev.0")
    arguments = [
        "--crate-name=_native",
        "--crate-type",
        "rlib",
        "--cfg",
        'feature="comparator"',
        "-C",
        "metadata=ignored",
    ]
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_metadata = _local_metadata(arguments, first / "native", first.resolve())
    second_metadata = _local_metadata(arguments, second / "native", second.resolve())

    assert first_metadata is not None
    assert first_metadata == second_metadata
    assert len(first_metadata) == 16


def test_wrapper_rejects_an_unexpected_local_manifest(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    arguments = ["--crate-name", "unexpected", "-C", "metadata=ignored"]

    with pytest.raises(RuntimeError, match="unexpected local Cargo manifest"):
        _local_metadata(arguments, root / "other-crate", root.resolve())


def test_build_selects_and_verifies_rustup_toolchain_without_plus_syntax(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root_with_wrapper(tmp_path)
    manifest = root / "benchmarks/comparators/runners/direct/Cargo.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("[package]\nname = 'fixture'\n", encoding="utf-8")
    tool_bin = tmp_path / "toolchain/bin"
    tool_bin.mkdir(parents=True)
    rustup = tool_bin / "rustup"
    cargo = tool_bin / "cargo"
    rustc = tool_bin / "rustc"
    for executable in (rustup, cargo, rustc):
        executable.write_text("fixture\n", encoding="utf-8")
        executable.chmod(0o755)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_run(
        command: list[str] | tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        selected_command = tuple(command)
        calls.append((selected_command, kwargs))
        if selected_command[0] == str(rustup):
            selected = cargo if selected_command[-1] == "cargo" else rustc
            return subprocess.CompletedProcess(selected_command, 0, f"{selected}\n", "")
        if selected_command[1:] == ("--version", "--verbose"):
            tool = "cargo" if selected_command[0] == str(cargo) else "rustc"
            return subprocess.CompletedProcess(
                selected_command,
                0,
                f"{tool} 1.97.1 (fixture)\nrelease: 1.97.1\n",
                "",
            )
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        artifact = Path(environment["CARGO_TARGET_DIR"]) / "release/pyowl-core-direct-comparator"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"runner")
        return subprocess.CompletedProcess(selected_command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    artifact = build_direct_runner(
        root,
        target_dir=tmp_path / "target",
        environ={
            "CARGO_HOME": str(tmp_path / "cargo-home"),
            "PATH": str(tool_bin),
        },
        platform="darwin",
    )

    assert artifact.read_bytes() == b"runner"
    build_command, build_options = calls[-1]
    assert build_command == (
        str(cargo),
        "build",
        "--locked",
        "--release",
        "--manifest-path",
        str(manifest),
    )
    assert not any(argument.startswith("+") for argument in build_command)
    build_environment = build_options["env"]
    assert isinstance(build_environment, dict)
    assert build_environment["RUSTUP_TOOLCHAIN"] == "1.97.1"
    assert build_environment["RUSTC"] == str(rustc)


def test_build_rejects_a_mismatched_resolved_toolchain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root_with_wrapper(tmp_path)
    manifest = root / "benchmarks/comparators/runners/direct/Cargo.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("[package]\nname = 'fixture'\n", encoding="utf-8")
    executable = tmp_path / "tool"
    executable.write_text("fixture\n", encoding="utf-8")
    executable.chmod(0o755)

    monkeypatch.setattr(build_module, "_rustup_executable", lambda _environ: executable)
    monkeypatch.setattr(
        build_module,
        "_resolved_tool",
        lambda *_args, **_kwargs: executable,
    )
    monkeypatch.setattr(
        build_module,
        "_captured_command",
        lambda *_args, **_kwargs: "release: 1.97.0\n",
    )

    with pytest.raises(DirectRunnerBuildError, match=r"must be exactly 1\.97\.1"):
        build_direct_runner(
            root,
            target_dir=tmp_path / "target",
            environ={"CARGO_HOME": str(tmp_path / "cargo-home")},
            platform="darwin",
        )
