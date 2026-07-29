#!/usr/bin/env python3
"""Rust compiler wrapper for path-independent direct-runner release builds."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

_ROOT_ENV = "PYOWL_CORE_DIRECT_REPRO_ROOT"
_LOCAL_MANIFESTS = frozenset(
    {
        Path("native"),
        Path("benchmarks/comparators/runners/direct"),
    }
)
_METADATA_DOMAIN = b"pyowl-core-direct-runner-v8\0"


def _option_values(arguments: list[str], option: str) -> tuple[str, ...]:
    values: list[str] = []
    position = 0
    while position < len(arguments):
        argument = arguments[position]
        if argument == option and position + 1 < len(arguments):
            values.append(arguments[position + 1])
            position += 2
            continue
        if argument.startswith(f"{option}="):
            values.append(argument.partition("=")[2])
        position += 1
    return tuple(values)


def _replace_cargo_metadata(arguments: list[str], metadata: str) -> list[str]:
    rewritten: list[str] = []
    replaced = 0
    position = 0
    while position < len(arguments):
        argument = arguments[position]
        if (
            argument == "-C"
            and position + 1 < len(arguments)
            and arguments[position + 1].startswith("metadata=")
        ):
            replaced += 1
            position += 2
            continue
        if argument.startswith("-Cmetadata="):
            replaced += 1
            position += 1
            continue
        rewritten.append(argument)
        position += 1
    if replaced != 1:
        raise RuntimeError(
            f"expected exactly one Cargo metadata option for a local crate, found {replaced}"
        )
    rewritten.extend(("-C", f"metadata={metadata}"))
    return rewritten


def _local_metadata(arguments: list[str], manifest_dir: Path, root: Path) -> str | None:
    try:
        relative_manifest = manifest_dir.resolve().relative_to(root)
    except ValueError:
        return None
    if relative_manifest not in _LOCAL_MANIFESTS:
        raise RuntimeError(
            f"unexpected local Cargo manifest in reproducible build: {relative_manifest}"
        )

    crate_names = _option_values(arguments, "--crate-name")
    if len(crate_names) != 1:
        raise RuntimeError(f"expected exactly one crate name, found {crate_names!r}")
    stable_inputs = (
        relative_manifest.as_posix(),
        crate_names[0],
        os.environ.get("CARGO_PKG_VERSION", ""),
        os.environ.get("TARGET", ""),
        *_option_values(arguments, "--crate-type"),
        *_option_values(arguments, "--cfg"),
    )
    digest = hashlib.sha256(_METADATA_DOMAIN)
    for value in stable_inputs:
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def main() -> int:
    if len(sys.argv) < 2:
        raise RuntimeError("rustc wrapper requires the compiler path")
    source_root = os.environ.get(_ROOT_ENV)
    if not source_root:
        raise RuntimeError(f"{_ROOT_ENV} is required")
    root = Path(source_root).resolve()
    rustc = sys.argv[1]
    arguments = sys.argv[2:]
    manifest = os.environ.get("CARGO_MANIFEST_DIR")
    if manifest is not None:
        metadata = _local_metadata(arguments, Path(manifest), root)
        if metadata is not None:
            arguments = _replace_cargo_metadata(arguments, metadata)
    os.execv(rustc, [rustc, *arguments])
    return 127


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as error:
        print(f"reproducible rustc wrapper: {error}", file=sys.stderr)
        raise SystemExit(2) from error
