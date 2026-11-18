"""Fixtures for the orchestration tests.

They run in the project environment with the ``orchestration`` extra and need no
scheduler, no daemon and no network: assets are materialised in-process on an ephemeral
instance, and every stage command is answered by ``FakeRunner``.

There is deliberately no ``tests/dagster/__init__.py``: with pytest's default
import mode it could make this directory a package named ``dagster``, shadowing the
real one.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from fakes import FakeRunner

REPO = Path(__file__).resolve().parents[2]
ORCHESTRATION = REPO / "orchestration"


def _dagster_is_installed() -> bool:
    """True only for the real package. A namespace package has no ``origin``."""
    spec = importlib.util.find_spec("dagster")
    return spec is not None and spec.origin is not None


if not _dagster_is_installed():
    # Without the extra the directory is skipped; in CI a missing Dagster must fail
    # the job, never pass it by skipping.
    if os.environ.get("CI") == "true":
        raise RuntimeError("Dagster must be importable in CI (tests/dagster)")
    collect_ignore_glob = ["test_*.py"]
elif str(ORCHESTRATION) not in sys.path:
    # `dagster dev -d orchestration` puts this folder on sys.path; so do the tests.
    sys.path.insert(0, str(ORCHESTRATION))


@pytest.fixture
def fake_runner(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    """Replace the real subprocess runner for the length of one test."""
    from exam_dagster import stage

    runner = FakeRunner()
    monkeypatch.setattr(stage, "RUNNER", runner)
    return runner
