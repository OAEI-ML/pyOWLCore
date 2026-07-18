from __future__ import annotations

from pathlib import Path

import pytest

from tools.packaging.release_tag import ReleaseTagError, version_from_tag


def _project(tmp_path: Path, version: str = "0.1.0.dev0") -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[project]\nversion = "{version}"\n', encoding="utf-8")
    return path


def test_current_pep440_development_tag_matches_exactly(tmp_path: Path) -> None:
    assert version_from_tag("v0.1.0.dev0", _project(tmp_path)) == "0.1.0.dev0"


@pytest.mark.parametrize(
    "tag",
    ["v0.1.0dev0", "v0.1.0", "0.1.0.dev0", "v0.1.0/../../main", "v0.1.0 dev0"],
)
def test_normalized_mismatched_or_unsafe_tag_is_rejected(tmp_path: Path, tag: str) -> None:
    with pytest.raises(ReleaseTagError):
        version_from_tag(tag, _project(tmp_path))


def test_stable_tag_is_supported(tmp_path: Path) -> None:
    assert version_from_tag("v1.0.0", _project(tmp_path, "1.0.0")) == "1.0.0"
