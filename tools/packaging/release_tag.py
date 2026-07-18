"""Validate an exact, shell-safe release tag against project metadata."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

_SAFE_TAG = re.compile(r"v[0-9A-Za-z]+(?:[.!+_-][0-9A-Za-z]+)*")
_PROJECT_VERSION = re.compile(r'^version = "([^"]+)"$', re.MULTILINE)


class ReleaseTagError(ValueError):
    """A release tag is unsafe or does not identify the declared version."""


def version_from_tag(tag: str, pyproject: Path) -> str:
    """Return the declared version only for its exact ``v<version>`` tag."""

    if _SAFE_TAG.fullmatch(tag) is None:
        raise ReleaseTagError("release tag must be a shell-safe v<version> value")
    match = _PROJECT_VERSION.search(pyproject.read_text(encoding="utf-8"))
    if match is None:
        raise ReleaseTagError("pyproject.toml has no literal project version")
    declared = match.group(1)
    if tag != f"v{declared}":
        raise ReleaseTagError(
            f"release tag {tag!r} does not exactly match declared version {declared!r}"
        )
    return declared


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    args = parser.parse_args(argv)
    try:
        version = version_from_tag(args.tag, args.pyproject)
    except (OSError, ReleaseTagError) as error:
        parser.error(str(error))
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
