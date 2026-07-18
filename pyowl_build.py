"""Small PEP 517 build helper for the optional private Rust extension."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path


class NativeBuildMode(str, Enum):
    """Exact values accepted by ``PYOWL_CORE_BUILD_NATIVE``."""

    AUTO = "auto"
    PURE = "0"
    REQUIRED = "1"


_NATIVE_BUILD_COMMANDS = frozenset(
    {
        "bdist",
        "bdist_wheel",
        "build",
        "build_ext",
        "editable_wheel",
        "install",
    }
)


def parse_native_build_mode(value: str | None = None) -> NativeBuildMode:
    """Parse the build mode without accepting aliases or case variants."""

    selected = os.environ.get("PYOWL_CORE_BUILD_NATIVE", "auto") if value is None else value
    try:
        return NativeBuildMode(selected)
    except ValueError as error:
        choices = ", ".join(repr(item.value) for item in NativeBuildMode)
        raise RuntimeError(
            f"PYOWL_CORE_BUILD_NATIVE must be exactly one of {choices}; got {selected!r}"
        ) from error


def is_native_build_command(argv: Sequence[str] | None = None) -> bool:
    """Return whether a setup invocation is producing an installed artifact."""

    arguments = sys.argv[1:] if argv is None else argv
    return any(argument in _NATIVE_BUILD_COMMANDS for argument in arguments)


def native_artifact_path(
    root: Path,
    environment: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
) -> Path:
    """Return Cargo's expected cdylib output for the configured target."""

    selected = os.environ if environment is None else environment
    raw_target_dir = selected.get("CARGO_TARGET_DIR")
    target_dir = root / "native" / "target" if raw_target_dir is None else Path(raw_target_dir)
    if not target_dir.is_absolute():
        target_dir = root / target_dir
    target = selected.get("CARGO_BUILD_TARGET")
    if target:
        target_dir /= target
    system = sys.platform if platform is None else platform
    if system == "win32":
        filename = "_native.dll"
    elif system == "darwin":
        filename = "lib_native.dylib"
    else:
        filename = "lib_native.so"
    return target_dir / "release" / filename


def build_native_extension(
    root: Path,
    mode: NativeBuildMode,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path | None:
    """Build the extension or return ``None`` only for an allowed fallback."""

    if mode is NativeBuildMode.PURE:
        return None
    selected = dict(os.environ if environment is None else environment)
    cargo = shutil.which("cargo", path=selected.get("PATH"))
    if cargo is None:
        _native_failure(mode, "Cargo is not available")
        return None

    manifest = root / "native" / "Cargo.toml"
    command = [
        cargo,
        "build",
        "--manifest-path",
        str(manifest),
        "--locked",
        "--release",
    ]
    target = selected.get("CARGO_BUILD_TARGET")
    if target:
        command.extend(("--target", target))
    selected["PYO3_PYTHON"] = sys.executable
    try:
        subprocess.run(command, cwd=root, env=selected, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        _native_failure(mode, f"Cargo build failed: {error}")
        return None

    artifact = native_artifact_path(root, selected)
    if not artifact.is_file():
        _native_failure(mode, f"Cargo did not produce {artifact}")
        return None
    return artifact


def _native_failure(mode: NativeBuildMode, reason: str) -> None:
    message = f"pyowl-core native extension unavailable: {reason}"
    if mode is NativeBuildMode.REQUIRED:
        raise RuntimeError(message)
    print(f"warning: {message}; building the complete pure-Python artifact", file=sys.stderr)
    return None


__all__ = [
    "NativeBuildMode",
    "build_native_extension",
    "is_native_build_command",
    "native_artifact_path",
    "parse_native_build_mode",
]
