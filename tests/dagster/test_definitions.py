"""The definitions load, and the rules every orchestration module must keep hold.

Covers SC-003 (definitions load) and the rules that do not need a run: no schedule or
sensor, the writer pool, no notifications (FR-027), and no git (FR-017).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import dagster as dg
import pytest
from exam_dagster import stage
from exam_dagster.assets import build_assets

if TYPE_CHECKING:
    from fakes import FakeRunner

PACKAGE = Path(__file__).resolve().parents[2] / "orchestration" / "exam_dagster"
MODULES = sorted(PACKAGE.glob("*.py"))


def _names_in(tree: ast.AST) -> set[str]:
    """Every identifier, attribute, keyword and imported name used in a module."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            found.add(node.arg)
        elif isinstance(node, ast.alias):
            found.add(node.asname or node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
    return found


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@pytest.fixture
def defs(fake_runner: FakeRunner) -> dg.Definitions:
    """The real ``definitions`` module, loaded against a scripted catalog."""
    import importlib
    import sys

    fake_runner.script[("exams",)] = (0, "a\nb\nexams: count=2\n", "")
    sys.modules.pop("exam_dagster.definitions", None)
    module = importlib.import_module("exam_dagster.definitions")
    try:
        return module.defs
    finally:
        sys.modules.pop("exam_dagster.definitions", None)


class TestLoading:
    def test_the_definitions_load_and_validate(self, defs: dg.Definitions) -> None:
        dg.Definitions.validate_loadable(defs)

    def test_the_exams_in_the_catalog_are_the_partitions(
        self, defs: dg.Definitions
    ) -> None:
        graph = defs.resolve_asset_graph()
        keys = graph.get(dg.AssetKey("fetched")).partitions_def
        assert keys is not None
        assert keys.get_partition_keys() == ["a", "b"]

    def test_the_job_covers_the_three_assets(self, defs: dg.Definitions) -> None:
        job = defs.resolve_job_def("exam_acquisition")

        assert {k.to_user_string() for k in job.asset_layer.executable_asset_keys} == {
            "catalog",
            "discovered",
            "fetched",
        }

    def test_it_is_manual_no_schedules_and_no_sensors(
        self, defs: dg.Definitions
    ) -> None:
        assert not list(defs.schedules or [])
        assert not list(defs.sensors or [])


class TestWriterPool:
    def test_every_writing_asset_holds_the_writer_slot(self) -> None:
        built = build_assets(["a"])

        for assets_def in built.assets:
            for spec in assets_def.specs:
                op_def = assets_def.get_op_def_for_asset_key(spec.key)
                assert op_def is not None, spec.key
                assert op_def.pool == stage.PIPELINE_WRITER, spec.key

    def test_both_instance_files_allow_one_writer_at_a_time(self) -> None:
        import yaml

        root = PACKAGE.parent
        for path in (
            root / "dagster_home" / "dagster.yaml",
            root / "docker" / "dagster.yaml",
        ):
            config = yaml.safe_load(path.read_text(encoding="utf-8"))
            pools = config["concurrency"]["pools"]
            # Dagster's default granularity is per step, which is what we want.
            assert pools["default_limit"] == 1, path
            assert pools.get("granularity", "op") == "op", path


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
class TestWhatEveryModuleMayUse:
    def test_no_notifications(self, path: Path) -> None:
        # FR-027: failures are reported in the Dagster UI only.
        forbidden = {
            "slack",
            "email",
            "make_slack_on_run_failure_sensor",
            "run_failure_sensor",
            "run_status_sensor",
            "email_on_failure",
        }

        assert not _names_in(_tree(path)) & forbidden, path.name

    def test_nothing_runs_git(self, path: Path) -> None:
        # FR-017: steps append to the ledger in the checkout; the maintainer commits.
        assert "git" not in _names_in(_tree(path)), path.name

    def test_only_stage_starts_processes(self, path: Path) -> None:
        names = _names_in(_tree(path))

        if path.name != "stage.py":
            assert not names & {"subprocess", "Popen"}, path.name

    def test_the_only_environment_variable_read_is_the_project_root(
        self, path: Path
    ) -> None:
        # FR-015: the DSN and tokens reach the pipeline through the inherited
        # environment; orchestration code never reads them.
        reads = [
            node.args[0].value
            for node in ast.walk(_tree(path))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "getenv"}
            and isinstance(node.func.value, (ast.Attribute, ast.Name))
            and "environ" in ast.unparse(node.func.value) + ast.unparse(node.func)
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ]

        assert set(reads) <= {"PIPELINE_PROJECT"}, path.name
