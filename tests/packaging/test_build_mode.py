from __future__ import annotations

import gzip
import os
import struct
import tarfile
from pathlib import Path

import pytest

import pyowl_build

ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("auto", pyowl_build.NativeBuildMode.AUTO),
        ("0", pyowl_build.NativeBuildMode.PURE),
        ("1", pyowl_build.NativeBuildMode.REQUIRED),
    ],
)
def test_build_mode_accepts_only_the_three_documented_values(
    value: str,
    expected: pyowl_build.NativeBuildMode,
) -> None:
    assert pyowl_build.parse_native_build_mode(value) is expected


@pytest.mark.parametrize("value", ["", "AUTO", "false", "true", "2", " 0"])
def test_invalid_build_mode_fails_closed(value: str) -> None:
    with pytest.raises(RuntimeError, match="must be exactly one of"):
        pyowl_build.parse_native_build_mode(value)


def test_default_build_mode_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYOWL_CORE_BUILD_NATIVE", raising=False)
    assert pyowl_build.parse_native_build_mode() is pyowl_build.NativeBuildMode.AUTO
    monkeypatch.setenv("PYOWL_CORE_BUILD_NATIVE", "0")
    assert pyowl_build.parse_native_build_mode() is pyowl_build.NativeBuildMode.PURE


def test_only_artifact_build_commands_trigger_native_compilation() -> None:
    assert pyowl_build.is_native_build_command(("bdist_wheel",))
    assert pyowl_build.is_native_build_command(("build_ext", "--inplace"))
    assert not pyowl_build.is_native_build_command(("egg_info",))
    assert not pyowl_build.is_native_build_command(("sdist",))


def test_forced_pure_never_probes_cargo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> str | None:
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(pyowl_build.shutil, "which", forbidden)
    assert (
        pyowl_build.build_native_extension(tmp_path, pyowl_build.NativeBuildMode.PURE)
        is None
    )


def test_auto_falls_back_but_required_mode_fails_without_cargo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(pyowl_build.shutil, "which", lambda *args, **kwargs: None)
    environment = {"PATH": ""}
    assert (
        pyowl_build.build_native_extension(
            tmp_path,
            pyowl_build.NativeBuildMode.AUTO,
            environment=environment,
        )
        is None
    )
    assert "complete pure-Python artifact" in capsys.readouterr().err
    with pytest.raises(RuntimeError, match="Cargo is not available"):
        pyowl_build.build_native_extension(
            tmp_path,
            pyowl_build.NativeBuildMode.REQUIRED,
            environment=environment,
        )


def test_native_build_remaps_source_registry_and_target_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cargo = tmp_path / "cargo"
    cargo.write_text("", encoding="utf-8")
    monkeypatch.setattr(pyowl_build.shutil, "which", lambda *args, **kwargs: str(cargo))
    captured: dict[str, str] = {}

    def run(command: list[str], *, cwd: Path, env: dict[str, str], check: bool) -> None:
        assert command[0] == str(cargo)
        assert cwd == tmp_path
        assert check
        captured.update(env)

    monkeypatch.setattr(pyowl_build.subprocess, "run", run)
    environment = {
        "PATH": str(tmp_path),
        "CARGO_HOME": str(tmp_path / "cargo home"),
        "CARGO_TARGET_DIR": str(tmp_path / "target"),
        "RUSTFLAGS": "-C opt-level=2",
    }
    artifact = pyowl_build.native_artifact_path(tmp_path, environment)
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"native")
    result = pyowl_build.build_native_extension(
        tmp_path,
        pyowl_build.NativeBuildMode.REQUIRED,
        environment=environment,
    )

    assert result == artifact
    assert "RUSTFLAGS" not in captured
    flags = captured["CARGO_ENCODED_RUSTFLAGS"].split("\x1f")
    assert flags[:2] == ["-C", "opt-level=2"]
    assert f"--remap-path-prefix={tmp_path.resolve()}=/rust/pyowl-core" in flags
    assert any(flag.endswith("=/rust/cargo-registry") for flag in flags)
    assert any(flag.endswith("=/rust/target") for flag in flags)


def test_native_artifact_path_honours_target_configuration(tmp_path: Path) -> None:
    environment = {
        "CARGO_TARGET_DIR": "cargo-output",
        "CARGO_BUILD_TARGET": "aarch64-unknown-linux-gnu",
    }
    assert pyowl_build.native_artifact_path(
        tmp_path,
        environment,
        platform="linux",
    ) == (
        tmp_path
        / "cargo-output"
        / "aarch64-unknown-linux-gnu"
        / "release"
        / "lib_native.so"
    )


def test_macos_native_build_keeps_proc_macro_link_flags_loadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cargo = tmp_path / "cargo"
    cargo.write_text("", encoding="utf-8")
    monkeypatch.setattr(pyowl_build.shutil, "which", lambda *args, **kwargs: str(cargo))
    monkeypatch.setattr(pyowl_build.sys, "platform", "darwin")
    captured: dict[str, str] = {}

    def run(command: list[str], *, cwd: Path, env: dict[str, str], check: bool) -> None:
        assert command[0] == str(cargo)
        assert cwd == tmp_path
        assert check
        captured.update(env)

    monkeypatch.setattr(pyowl_build.subprocess, "run", run)
    environment = {"PATH": str(tmp_path), "CARGO_TARGET_DIR": str(tmp_path / "target")}
    artifact = pyowl_build.native_artifact_path(tmp_path, environment, platform="darwin")
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"native")

    assert pyowl_build.build_native_extension(
        tmp_path,
        pyowl_build.NativeBuildMode.REQUIRED,
        environment=environment,
    ) == artifact
    flags = captured["CARGO_ENCODED_RUSTFLAGS"].split("\x1f")
    assert "link-arg=-Wl,-no_uuid" not in flags


def test_native_cdylib_disables_nondeterministic_macos_linker_uuid() -> None:
    build_script = (ROOT / "native" / "build.rs").read_text(encoding="utf-8")

    assert 'env::var("CARGO_CFG_TARGET_OS")' in build_script
    assert 'println!("cargo:rustc-cdylib-link-arg=-Wl,-no_uuid")' in build_script


def test_macos_extension_install_name_is_reproducible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension = tmp_path / "_native.cpython-310-darwin.so"
    identifier = b"@rpath/_native.so\0"
    command_size = 24 + len(identifier)
    command_size += -command_size % 8
    header = struct.pack(
        "<IiiIIIII",
        0xFEEDFACF,
        0,
        0,
        6,
        1,
        command_size,
        0,
        0,
    )
    command = struct.pack("<IIIIII", 0xD, command_size, 24, 123456789, 0, 0)
    extension.write_bytes(header + command + identifier.ljust(command_size - 24, b"\0"))
    calls: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(
        pyowl_build.shutil,
        "which",
        lambda command, **kwargs: "/usr/bin/install_name_tool",
    )

    def capture(
        command: list[str],
        *,
        env: dict[str, str],
        check: bool,
    ) -> None:
        assert check
        calls.append((command, env))

    monkeypatch.setattr(pyowl_build.subprocess, "run", capture)
    environment = {"PATH": "/usr/bin"}
    pyowl_build.normalize_native_extension(
        extension,
        environment,
        platform="darwin",
    )
    assert calls == [
        (
            [
                "/usr/bin/install_name_tool",
                "-id",
                "@rpath/_native.cpython-310-darwin.so",
                str(extension),
            ],
            environment,
        )
    ]
    assert struct.unpack_from("<I", extension.read_bytes(), 32 + 12) == (0,)


def test_non_macos_extension_needs_no_install_name_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pyowl_build.shutil,
        "which",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()),
    )
    pyowl_build.normalize_native_extension(tmp_path / "_native.so", platform="linux")


def test_source_archive_normalizes_gzip_tar_metadata_and_order(tmp_path: Path) -> None:
    source = tmp_path / "pyowl_core-0.1.0"
    nested = source / "package"
    nested.mkdir(parents=True)
    regular = nested / "module.py"
    executable = source / "build-tool"
    regular.write_text("VALUE = 1\n", encoding="utf-8")
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    epoch = 1_735_689_600

    first = Path(
        pyowl_build.build_reproducible_sdist(
            tmp_path / "first",
            source.name,
            epoch=epoch,
            root_dir=tmp_path,
        )
    )
    os.utime(source, (epoch + 100, epoch + 100))
    os.utime(regular, (epoch + 200, epoch + 200))
    second = Path(
        pyowl_build.build_reproducible_sdist(
            tmp_path / "second",
            source.name,
            epoch=epoch,
            root_dir=tmp_path,
        )
    )

    assert first.read_bytes() == second.read_bytes()
    assert int.from_bytes(first.read_bytes()[4:8], "little") == epoch
    with (
        gzip.open(first, "rb") as compressed,
        tarfile.open(fileobj=compressed, mode="r:") as archive,
    ):
        members = archive.getmembers()
    assert [member.name for member in members] == sorted(
        (member.name for member in members),
        key=lambda name: (name.count("/"), name),
    )
    assert all(member.mtime == epoch for member in members)
    assert all(
        (member.uid, member.gid, member.uname, member.gname) == (0, 0, "", "")
        for member in members
    )
    modes = {member.name: member.mode for member in members}
    assert modes[f"{source.name}/package/module.py"] == 0o644
    assert modes[f"{source.name}/build-tool"] == 0o755
