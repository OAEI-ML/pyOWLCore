"""Build the direct comparator with a path-independent Rust invocation."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

TOOLCHAIN = "1.97.1"
_BUILD_CONTRACT = "pyowl-core-direct-runner-v8"
_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST = Path("benchmarks/comparators/runners/direct/Cargo.toml")
_RUNNER_DIRECTORY = _MANIFEST.parent
_WRAPPER = Path("tools/benchmark/comparators/reproducible_rustc.py")
_HOSTILE_ENVIRONMENT = (
    "CARGO_BUILD_RUSTC_WRAPPER",
    "CARGO_ENCODED_RUSTFLAGS",
    "RUSTC",
    "RUSTC_WORKSPACE_WRAPPER",
    "RUSTC_WRAPPER",
    "RUSTFLAGS",
)


class DirectRunnerBuildError(RuntimeError):
    """Raised when a reproducible direct-runner build cannot be guaranteed."""


def _selected_target_dir(root: Path, target_dir: Path | None) -> Path:
    if target_dir is None:
        return (root / _RUNNER_DIRECTORY / "target").resolve()
    if not target_dir.is_absolute():
        target_dir = root / target_dir
    return target_dir.resolve()


def reproducible_environment(
    root: Path,
    target_dir: Path,
    *,
    environ: Mapping[str, str],
    platform: str,
) -> dict[str, str]:
    """Return the fail-closed environment for one direct-runner build."""

    selected = dict(environ)
    conflicts = tuple(name for name in _HOSTILE_ENVIRONMENT if selected.get(name))
    if conflicts:
        joined = ", ".join(conflicts)
        raise DirectRunnerBuildError(
            f"reproducible direct-runner build rejects externally supplied flags: {joined}"
        )

    selected_root = root.resolve()
    selected_target = target_dir.resolve()
    cargo_home = Path(selected.get("CARGO_HOME", Path.home() / ".cargo")).expanduser()
    rust_flags = [
        # Cargo fingerprints encoded Rust flags. Keep the build-contract token
        # compiler-visible so an existing target directory cannot reuse local
        # crate artifacts produced by an older metadata-wrapper contract.
        f"--cfg={_BUILD_CONTRACT.replace('-', '_')}",
        f"--remap-path-prefix={selected_target}=/rust/target",
        f"--remap-path-prefix={selected_root}=/rust/pyowl-core",
        f"--remap-path-prefix={cargo_home.resolve() / 'registry' / 'src'}=/rust/cargo-registry",
        f"--remap-path-prefix={cargo_home.resolve() / 'git' / 'checkouts'}=/rust/cargo-git",
    ]
    if platform == "darwin":
        rust_flags.extend(("-C", "link-arg=-Wl,-no_uuid"))

    selected["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(rust_flags)
    selected["CARGO_INCREMENTAL"] = "0"
    selected["CARGO_TARGET_DIR"] = os.fspath(selected_target)
    selected["RUSTUP_TOOLCHAIN"] = TOOLCHAIN
    if platform != "win32":
        wrapper = selected_root / _WRAPPER
        if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
            raise DirectRunnerBuildError(
                f"reproducible rustc wrapper is missing or not executable: {wrapper}"
            )
        selected["PYOWL_CORE_DIRECT_REPRO_ROOT"] = os.fspath(selected_root)
        selected["RUSTC_WRAPPER"] = os.fspath(wrapper)
    return selected


def direct_runner_artifact(target_dir: Path, *, platform: str) -> Path:
    suffix = ".exe" if platform == "win32" else ""
    return target_dir / "release" / f"pyowl-core-direct-comparator{suffix}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rustup_executable(environ: Mapping[str, str]) -> Path:
    located = shutil.which("rustup", path=environ.get("PATH"))
    candidates = [Path(located)] if located is not None else []
    cargo_home = Path(environ.get("CARGO_HOME", Path.home() / ".cargo")).expanduser()
    executable = "rustup.exe" if os.name == "nt" else "rustup"
    candidates.extend((cargo_home / "bin" / executable, Path.home() / ".cargo/bin" / executable))
    for candidate in candidates:
        # Homebrew exposes ``rustup`` as a symlink to the multi-call
        # ``rustup-init`` binary. Preserve argv[0] so the binary dispatches as
        # rustup instead of entering installer mode.
        selected = candidate.absolute()
        if selected.is_file() and os.access(selected, os.X_OK):
            return selected
    raise DirectRunnerBuildError("rustup is not available")


def _captured_command(
    command: Sequence[str],
    *,
    cwd: Path,
    environ: Mapping[str, str],
) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=dict(environ),
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise DirectRunnerBuildError(f"toolchain command failed: {error}") from error
    return completed.stdout


def _resolved_tool(
    rustup: Path,
    tool: str,
    *,
    root: Path,
    environ: Mapping[str, str],
) -> Path:
    output = _captured_command(
        (os.fspath(rustup), "which", "--toolchain", TOOLCHAIN, tool),
        cwd=root,
        environ=environ,
    )
    lines = output.splitlines()
    if len(lines) != 1:
        raise DirectRunnerBuildError(f"rustup returned an invalid {tool} path")
    selected = Path(lines[0]).resolve()
    if not selected.is_file() or not os.access(selected, os.X_OK):
        raise DirectRunnerBuildError(f"rustup selected an unusable {tool}: {selected}")
    return selected


def _verify_tool_release(
    executable: Path,
    tool: str,
    *,
    root: Path,
    environ: Mapping[str, str],
) -> None:
    output = _captured_command(
        (os.fspath(executable), "--version", "--verbose"),
        cwd=root,
        environ=environ,
    )
    releases = tuple(
        line.partition(":")[2].strip()
        for line in output.splitlines()
        if line.startswith("release:")
    )
    if releases != (TOOLCHAIN,):
        raise DirectRunnerBuildError(
            f"{tool} release must be exactly {TOOLCHAIN}, got {releases!r}"
        )


def _verified_toolchain(
    root: Path,
    environ: Mapping[str, str],
) -> tuple[Path, Path]:
    rustup = _rustup_executable(environ)
    cargo = _resolved_tool(rustup, "cargo", root=root, environ=environ)
    rustc = _resolved_tool(rustup, "rustc", root=root, environ=environ)
    _verify_tool_release(cargo, "Cargo", root=root, environ=environ)
    _verify_tool_release(rustc, "rustc", root=root, environ=environ)
    return cargo, rustc


def build_direct_runner(
    root: Path = _ROOT,
    *,
    target_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
    platform: str = sys.platform,
) -> Path:
    """Build and return the reproducible direct-runner artifact."""

    selected_root = root.resolve()
    manifest = selected_root / _MANIFEST
    if not manifest.is_file():
        raise DirectRunnerBuildError(f"direct-runner manifest is missing: {manifest}")
    selected_target = _selected_target_dir(selected_root, target_dir)
    selected = reproducible_environment(
        selected_root,
        selected_target,
        environ=os.environ if environ is None else environ,
        platform=platform,
    )
    cargo, rustc = _verified_toolchain(selected_root, selected)
    selected["RUSTC"] = os.fspath(rustc)
    command = (
        os.fspath(cargo),
        "build",
        "--locked",
        "--release",
        "--manifest-path",
        os.fspath(manifest),
    )
    try:
        subprocess.run(command, cwd=selected_root, env=selected, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise DirectRunnerBuildError(f"Cargo build failed: {error}") from error

    artifact = direct_runner_artifact(selected_target, platform=platform)
    if not artifact.is_file():
        raise DirectRunnerBuildError(f"Cargo did not produce {artifact}")
    return artifact


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=_ROOT,
        help="source-tree root (defaults to the repository containing this module)",
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        help="Cargo target directory (relative paths are resolved from --root)",
    )
    parser.add_argument(
        "--print-sha256",
        action="store_true",
        help="print the completed artifact SHA-256 and path",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        artifact = build_direct_runner(arguments.root, target_dir=arguments.target_dir)
    except DirectRunnerBuildError as error:
        raise SystemExit(str(error)) from error
    if arguments.print_sha256:
        print(f"{_sha256_file(artifact)}  {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
