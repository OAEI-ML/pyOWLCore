"""Optional native build hook; project metadata lives in pyproject.toml."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pyowl_build import (  # noqa: E402
    NativeBuildMode,
    build_native_extension,
    is_native_build_command,
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


extension_modules = (
    [Extension("pyowl_core._native", sources=[])] if PREBUILT_NATIVE is not None else []
)

setup(
    ext_modules=extension_modules,
    cmdclass={"build_ext": RustBuildExt},
)
