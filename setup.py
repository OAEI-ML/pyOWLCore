"""Optional native build hook; project metadata lives in pyproject.toml."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext
from setuptools.command.sdist import sdist

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pyowl_build import (  # noqa: E402
    NativeBuildMode,
    build_native_extension,
    build_reproducible_sdist,
    is_native_build_command,
    normalize_native_extension,
    parse_native_build_mode,
)

BUILD_MODE = parse_native_build_mode()
PREBUILT_NATIVE = (
    build_native_extension(ROOT, BUILD_MODE)
    if BUILD_MODE is not NativeBuildMode.PURE and is_native_build_command()
    else None
)


class RustBuildExt(build_ext):
    """Copy the already-built PyO3 cdylib to its interpreter-tagged path."""

    def build_extension(self, extension: Extension) -> None:
        if extension.name != "pyowl_core._native" or PREBUILT_NATIVE is None:
            super().build_extension(extension)
            return
        destination = Path(self.get_ext_fullpath(extension.name))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PREBUILT_NATIVE, destination)
        normalize_native_extension(destination)


class ReproducibleSdist(sdist):
    """Honor ``SOURCE_DATE_EPOCH`` for tar and gzip metadata."""

    def make_archive(
        self,
        base_name: str | os.PathLike[str],
        format: str,
        root_dir: str | os.PathLike[str] | bytes | os.PathLike[bytes] | None = None,
        base_dir: str | None = None,
        owner: str | None = None,
        group: str | None = None,
    ) -> str:
        raw_epoch = os.environ.get("SOURCE_DATE_EPOCH")
        filesystem_root = None if root_dir is None else os.fspath(root_dir)
        if (
            format != "gztar"
            or raw_epoch is None
            or base_dir is None
            or isinstance(filesystem_root, bytes)
        ):
            return super().make_archive(
                base_name,
                format,
                root_dir,
                base_dir,
                owner,
                group,
            )
        try:
            epoch = int(raw_epoch)
        except ValueError as error:
            raise RuntimeError("SOURCE_DATE_EPOCH must be a non-negative integer") from error
        return build_reproducible_sdist(
            base_name,
            base_dir,
            epoch=epoch,
            root_dir=filesystem_root,
        )


extension_modules = (
    [Extension("pyowl_core._native", sources=[])] if PREBUILT_NATIVE is not None else []
)

setup(
    ext_modules=extension_modules,
    cmdclass={"build_ext": RustBuildExt, "sdist": ReproducibleSdist},
)
