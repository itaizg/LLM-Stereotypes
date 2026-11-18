"""Per-exam stage runs equal one all-exam run (spec 002, SC-001 and SC-011).

The Airflow DAG cannot run in the same interpreter as the pipeline, so its claim
"the same work as a hand run" is checked in two halves that share one file,
``tests/fixtures/airflow_acquisition_argv.json``: ``tests/airflow`` asserts the DAG
issues exactly those commands, and this module runs those commands through the CLI
and compares them with what a maintainer would type.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
import pytest
from psycopg import sql

import pipeline.__main__ as cli
import pipeline.web as pipeline_web
from pipeline.manifests import read_ledger, replay_ledger

if TYPE_CHECKING:
    from conftest import DatasetBuilder, FakeClock, Web

ARGV_FILE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "airflow_acquisition_argv.json"
)
HAND_RUN = [["catalog"], ["discover"], ["fetch"]]


def _argv_from_the_dag_contract() -> tuple[list[str], list[list[str]]]:
    doc = json.loads(ARGV_FILE.read_text(encoding="utf-8"))
    return doc["exams"], doc["argv"]


def _serve_the_dataset(dataset: DatasetBuilder, web: Web) -> None:
    """Answer every approved source with a PDF and every seed with one new link."""
    ledger = replay_ledger(read_ledger(dataset.ledger_path))
    for source in ledger.sources.values():
        web.add(
            source.url,
            b"%PDF synthetic " + source.source_id.encode(),
            headers={"content-type": "application/pdf"},
        )
    for exam_id, exam in dataset.catalog["exams"].items():
        host = exam["domains"][0]
        web.add(
            f"https://{host}/archive",
            f'<html><a href="https://{host}/new/{exam_id}.pdf">new</a></html>',
            headers={"content-type": "text/html"},
        )
        web.add(
            f"https://{host}/news",
            "<html></html>",
            headers={"content-type": "text/html"},
        )


def _events(ledger: Path) -> Counter[str]:
    """The ledger as a multiset of events, without ``seq`` and ``at``."""
    return Counter(
        json.dumps({"kind": e.kind, "fields": dict(e.fields)}, sort_keys=True)
        for e in read_ledger(ledger)
    )


def _row_counts(dsn: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    with psycopg.connect(dsn, autocommit=True) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
                " ORDER BY table_name"
            )
        ]
        for name in tables:
            row = conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name))
            ).fetchone()
            assert row is not None
            counts[name] = row[0]
    return counts


class _Environment:
    """Everything one run needs: a ledger, a schema and a blob root of its own."""

    def __init__(self, dataset: DatasetBuilder, dsn: str, root: Path) -> None:
        self.dataset = dataset
        self.dsn = dsn
        self.root = root
        root.mkdir(parents=True)
        self.ledger = root / "ledger.jsonl"
        self.ledger.write_text(
            dataset.ledger_path.read_text(encoding="utf-8"), encoding="utf-8"
        )

    def run(
        self, commands: Sequence[Sequence[str]], monkeypatch: pytest.MonkeyPatch
    ) -> list[int]:
        monkeypatch.setenv("PIPELINE_DSN", self.dsn)
        monkeypatch.setenv("PIPELINE_BLOB_ROOT", str(self.root / "blobs"))
        return [
            cli.main(
                [
                    *argv,
                    "--catalog",
                    str(self.dataset.catalog_path),
                    "--ledger",
                    str(self.ledger),
                ]
            )
            for argv in commands
        ]


@pytest.fixture
def offline_pipeline(
    dataset: DatasetBuilder,
    web: Web,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point the CLI at the in-memory website and the virtual clock."""
    _serve_the_dataset(dataset, web)
    monkeypatch.setattr(pipeline_web, "new_client", web.client)
    monkeypatch.setattr(pipeline_web, "SystemClock", lambda: clock)


@pytest.mark.usefixtures("offline_pipeline")
class TestPerExamRunsEqualOneAllExamRun:
    def test_the_argv_file_names_the_datasets_exams(
        self, dataset: DatasetBuilder
    ) -> None:
        exams, argv = _argv_from_the_dag_contract()

        assert exams == sorted(dataset.catalog["exams"])
        assert argv[0] == ["catalog"]
        assert argv[1:] == [
            [*stage, "--exam", exam]
            for exam in exams
            for stage in (["discover"], ["candidates"], ["fetch"])
        ]

    def test_same_ledger_events_and_same_rows(
        self,
        dataset: DatasetBuilder,
        new_schema: Callable[[], str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, per_exam = _argv_from_the_dag_contract()
        by_exam = _Environment(dataset, new_schema(), tmp_path / "per-exam")
        by_hand = _Environment(dataset, new_schema(), tmp_path / "all-exams")

        assert by_exam.run(per_exam, monkeypatch) == [0] * len(per_exam)
        assert by_hand.run(HAND_RUN, monkeypatch) == [0, 0, 0]

        assert _events(by_exam.ledger) == _events(by_hand.ledger)
        assert _row_counts(by_exam.dsn) == _row_counts(by_hand.dsn)
        # The runs must have done something, or the comparison proves nothing.
        assert sum(_events(by_exam.ledger).values()) > sum(
            _events(dataset.ledger_path).values()
        )

    def test_a_repeated_run_adds_no_ledger_line_and_a_third_changes_no_row(
        self,
        dataset: DatasetBuilder,
        new_schema: Callable[[], str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # `catalog` runs first, so the tables trail the ledger by one run: the
        # second `catalog` brings them up to date with what the first run's
        # discover found. The ledger itself is settled after the first run.
        _, per_exam = _argv_from_the_dag_contract()
        env = _Environment(dataset, new_schema(), tmp_path / "repeat")
        ok = [0] * len(per_exam)

        assert env.run(per_exam, monkeypatch) == ok
        settled = env.ledger.read_text(encoding="utf-8").splitlines()

        assert env.run(per_exam, monkeypatch) == ok
        assert env.ledger.read_text(encoding="utf-8").splitlines() == settled
        rows = _row_counts(env.dsn)

        assert env.run(per_exam, monkeypatch) == ok
        assert env.ledger.read_text(encoding="utf-8").splitlines() == settled
        assert _row_counts(env.dsn) == rows
