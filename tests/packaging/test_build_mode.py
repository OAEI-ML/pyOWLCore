from __future__ import annotations

import struct
from pathlib import Path

import pytest

import pyowl_build


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
