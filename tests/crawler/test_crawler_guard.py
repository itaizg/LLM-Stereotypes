"""Structural guards for the crawler package (plan "Structure Decision", FR-023).

1. Dependencies run one way: ``pipeline/``, ``harness/`` and ``orchestration/`` never
   import ``crawler``.
2. The crawler never edits the catalog, approves anything or downloads a full file:
   no crawler module writes the ledger or ``catalog.yaml``, imports the blob store, or
   uses the import logic of ``pipeline.decisions`` (only its model).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CRAWLER = REPO / "crawler"


def python_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*.py")):
        # iCloud leaves "name 2.py" conflict copies; they are never tracked.
        if not path.stem[-1:].isdigit() or " " not in path.stem:
            yield path


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


@pytest.mark.parametrize("package", ["pipeline", "harness", "orchestration"])
def test_no_other_package_imports_the_crawler(package: str) -> None:
    offenders = [
        str(path.relative_to(REPO))
        for path in python_files(REPO / package)
        if any(
            m == "crawler" or m.startswith("crawler.") for m in imported_modules(path)
        )
    ]
    assert offenders == []


FORBIDDEN_IMPORTS = (
    "pipeline.manifests.append_event",
    "pipeline.blobs",
    "pipeline.acquire",
    "pipeline.decisions.import_decisions",
)
FORBIDDEN_NAMES = ("catalog.yaml", "ledger.jsonl", "append_event")


def test_the_crawler_cannot_write_the_ledger_or_the_catalog() -> None:
    offenders: list[str] = []
    for path in python_files(CRAWLER):
        modules = imported_modules(path)
        for forbidden in FORBIDDEN_IMPORTS:
            if any(m == forbidden or m.startswith(forbidden + ".") for m in modules):
                offenders.append(f"{path.name} imports {forbidden}")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and any(name in node.value for name in FORBIDDEN_NAMES[:2])
            ):
                offenders.append(f"{path.name} names {node.value!r}")
            if isinstance(node, (ast.Name, ast.Attribute)):
                name = node.id if isinstance(node, ast.Name) else node.attr
                if name == "append_event":
                    offenders.append(f"{path.name} calls append_event")
    assert offenders == []
