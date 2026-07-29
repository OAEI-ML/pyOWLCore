"""Canonical benchmark report metadata and atomic JSON publication."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pyowl_core import __version__
from pyowl_core.backends import native

_MAX_REFERENCE_OBSERVATION_CHARS = 4_096
_UNMEASURED_POWER_MODE = "not measured; reference operator must record externally"
_UNMEASURED_STORAGE = "not measured; corpus bytes are resident before timed phases"


class ReportError(RuntimeError):
    """Required run metadata cannot be established safely."""


def collect_environment(
    root: Path,
    *,
    reference_cpu_model: str | None = None,
    reference_storage: str | None = None,
    reference_power_mode: str | None = None,
) -> dict[str, Any]:
    """Capture versioned machine/runtime metadata without contacting a network."""

    supplied_cpu_model = validate_reference_observation(
        reference_cpu_model,
        "reference CPU model",
    )
    supplied_storage = validate_reference_observation(reference_storage, "reference storage")
    supplied_power_mode = validate_reference_observation(
        reference_power_mode,
        "reference power mode",
    )
    commit = _command(("git", "rev-parse", "HEAD"), cwd=root, required=True)
    status = _command(("git", "status", "--porcelain"), cwd=root, required=True)
    probe = native.probe()
    native_artifact = _native_artifact(root)
    uname = platform.uname()
    probed_cpu_model = _cpu_model()
    if (
        supplied_cpu_model is not None
        and probed_cpu_model is not None
        and supplied_cpu_model != probed_cpu_model
    ):
        raise ReportError("operator-supplied reference CPU model differs from the platform probe")
    cpu_model = supplied_cpu_model if supplied_cpu_model is not None else probed_cpu_model
    observation_sources = {
        "cpu_model": (
            "operator-supplied"
            if supplied_cpu_model is not None
            else "platform-probe"
            if probed_cpu_model is not None
            else "unavailable"
        ),
        "storage": "operator-supplied" if supplied_storage is not None else "not-measured",
        "power_mode": "operator-supplied" if supplied_power_mode is not None else "not-measured",
    }
    environment: dict[str, Any] = {
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": commit,
        "git_dirty": bool(status),
        "platform": {
            "system": uname.system,
            "release": uname.release,
            "version": uname.version,
            "machine": uname.machine,
            "processor": uname.processor,
            "python_build_platform": platform.platform(),
        },
        "cpu": {
            "logical_count": os.cpu_count(),
            "model": cpu_model,
        },
        "memory": {"physical_bytes": _physical_memory_bytes()},
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": _path_label(Path(sys.executable), root),
            "compiler": platform.python_compiler(),
            "pyowl_core": __version__,
        },
        "rust": {
            "rustc": _command(("rustc", "--version"), cwd=root, required=False),
            "cargo": _command(("cargo", "--version"), cwd=root, required=False),
        },
        "native": {
            "available": probe.available,
            "reason": probe.reason,
            "version": probe.version,
            "features": list(probe.features),
            "artifact": native_artifact,
        },
        "tool_versions": _tool_versions(),
        "machine_observation_sources": observation_sources,
        "power_mode": supplied_power_mode or _UNMEASURED_POWER_MODE,
        "storage": supplied_storage or _UNMEASURED_STORAGE,
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
    comparison_fields["native_available"] = probe.available
    comparison_fields["native_features"] = list(probe.features)
    environment["comparison_key"] = hashlib.sha256(
        canonical_json_bytes(comparison_fields)
    ).hexdigest()
    return environment


def validate_reference_observation(value: object, name: str) -> str | None:
    """Validate one bounded operator-supplied machine observation."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ReportError(f"{name} must be a string")
    if (
        not value
        or value != value.strip()
        or len(value) > _MAX_REFERENCE_OBSERVATION_CHARS
        or any(not character.isprintable() for character in value)
    ):
        raise ReportError(
            f"{name} must be nonempty, trimmed, control-free, and at most "
            f"{_MAX_REFERENCE_OBSERVATION_CHARS} characters"
        )
    return value


def canonical_json_bytes(value: object) -> bytes:
    """Encode deterministic UTF-8 JSON suitable for content hashing."""

    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def write_json(path: Path, value: object) -> str:
    """Atomically publish canonical JSON and return its SHA-256."""

    payload = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    return hashlib.sha256(payload).hexdigest()


def _command(command: tuple[str, ...], *, cwd: Path, required: bool) -> str | None:
    executable = shutil.which(command[0])
    cargo_home_candidate = Path.home() / ".cargo" / "bin" / command[0]
    if executable is None and cargo_home_candidate.is_file():
        executable = str(cargo_home_candidate)
    if executable is None:
        if required:
            raise ReportError(f"required metadata command is unavailable: {command[0]}")
        return None
    try:
        result = subprocess.run(
            (executable, *command[1:]),
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        if required:
            raise ReportError(f"metadata command failed: {' '.join(command)}: {error}") from error
        return None
    if result.returncode != 0:
        if required:
            detail = result.stderr.strip() or f"exit {result.returncode}"
            raise ReportError(f"metadata command failed: {' '.join(command)}: {detail}")
        return None
    return result.stdout.strip()


def _native_artifact(root: Path) -> dict[str, str | int] | None:
    spec = importlib.util.find_spec("pyowl_core._native")
    if spec is None or spec.origin is None:
        return None
    path = Path(spec.origin)
    try:
        payload = path.read_bytes()
    except OSError:
        return {"path": _path_label(path, root), "state": "unreadable"}
    return {
        "path": _path_label(path.resolve(), root),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _path_label(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except (OSError, ValueError):
        return path.name


def _cpu_model() -> str | None:
    if sys.platform == "darwin":
        return _command(
            ("sysctl", "-n", "machdep.cpu.brand_string"), cwd=Path.cwd(), required=False
        )
    cpuinfo = Path("/proc/cpuinfo")
    try:
        for line in cpuinfo.read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("model name") and ":" in line:
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or None


def _physical_memory_bytes() -> int | None:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None
    if not isinstance(pages, int) or not isinstance(page_size, int):
        return None
    return pages * page_size


def _tool_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for distribution in ("mypy", "pytest", "ruff", "tomli"):
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = None
    return versions


__all__ = [
    "ReportError",
    "canonical_json_bytes",
    "collect_environment",
    "validate_reference_observation",
    "write_json",
]
