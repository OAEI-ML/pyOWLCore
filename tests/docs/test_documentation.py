from __future__ import annotations

import ast
import re
import runpy
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote

import pyowl_core

ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "CHANGELOG.md",
    ROOT / "MIGRATION.md",
    *sorted((ROOT / "docs").glob("*.md")),
)
PYTHON_FENCE = re.compile(r"```python[^\n]*\n(.*?)```", re.DOTALL)
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
FORBIDDEN_IMPORTS = frozenset(
    {
        "deeponto",
        "jpype",
        "mowl",
        "owlapi",
        "py4j",
        "robot",
    }
)


def _python_snippets(path: Path) -> Iterator[str]:
    yield from PYTHON_FENCE.findall(path.read_text(encoding="utf-8"))


def test_documentation_internal_links_resolve_inside_the_repository() -> None:
    failures: list[str] = []
    root = ROOT.resolve()
    for document in DOCUMENTS:
        for raw_target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
            target = raw_target.split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            target = unquote(target.strip("<>"))
            resolved = (document.parent / target).resolve()
            if not resolved.is_relative_to(root):
                relative = document.relative_to(ROOT)
                failures.append(f"{relative}: link escapes repository: {raw_target}")
            elif not resolved.exists():
                failures.append(f"{document.relative_to(ROOT)}: missing link target: {raw_target}")
    assert failures == []


def test_every_python_fence_compiles() -> None:
    compiled = 0
    for document in DOCUMENTS:
        for position, snippet in enumerate(_python_snippets(document), start=1):
            compile(snippet, f"{document}#python-{position}", "exec")
            compiled += 1
    assert compiled >= 2


def test_readme_quickstart_executes_with_the_public_python_api() -> None:
    snippets = tuple(_python_snippets(ROOT / "README.md"))
    assert len(snippets) == 1
    namespace: dict[str, Any] = {}
    exec(compile(snippets[0], "README.md#quick-start", "exec"), namespace)
    snapshot = cast(pyowl_core.OntologySnapshot, namespace["snapshot"])
    assert snapshot.is_complete
    assert snapshot.capabilities.backend == "python"
    assert len(tuple(snapshot.iter_axioms())) == 1


def test_parse_once_example_executes_and_preserves_view_identity() -> None:
    example = ROOT / "docs" / "examples" / "parse_once.py"
    namespace = runpy.run_path(str(example))
    demonstrate = cast(
        Callable[
            [],
            tuple[
                pyowl_core.OntologyView,
                pyowl_core.OntologyOverlay,
                pyowl_core.OntologyComposite,
            ],
        ],
        namespace["demonstrate"],
    )
    source, overlay, composite = demonstrate()
    assert overlay.base is source
    assert next(member.view for member in composite.members) is source


def test_examples_import_no_java_or_consumer_runtime() -> None:
    example = ROOT / "docs" / "examples" / "parse_once.py"
    tree = ast.parse(example.read_text(encoding="utf-8"), filename=str(example))
    imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    assert imports.isdisjoint(FORBIDDEN_IMPORTS)
    assert imports <= {"__future__", "dataclasses", "pyowl_core"}
