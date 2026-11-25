"""No exam question text in crawler code, fixtures or outputs (SC-009, FR-020).

The corpus guard (``pipeline.corpus``) fingerprints the dataset's question text. The
crawler's package and test fixtures, and the tickets and trace a replay writes, must
not match it. The replay eval separately checks its synthetic stems never leave a run.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import psycopg

from crawler.config import RunConfig
from crawler.evals.replay import load_fixtures, run_replay
from pipeline.corpus import scan

REPO = Path(__file__).resolve().parents[2]


def corpus_hits(paths: list[Path]) -> list[str]:
    roots = [p.resolve().as_posix() for p in paths]
    return [
        hit.path
        for hit in scan(REPO, paths)
        if hit.kind == "corpus_text"
        and any(
            (REPO / hit.path).resolve().as_posix().startswith(r)
            or hit.path.startswith(r)
            for r in roots
        )
    ]


def test_crawler_code_and_fixtures_hold_no_corpus_text() -> None:
    assert corpus_hits([REPO / "crawler", REPO / "tests" / "crawler"]) == []


def test_replay_outputs_hold_no_corpus_text(
    crawler_db: psycopg.Connection, tmp_path: Path
) -> None:
    report = asyncio.run(
        run_replay(
            load_fixtures(Path(__file__).parent / "fixtures" / "replay"),
            crawler_db,
            RunConfig(run_id="cr_20261123T101500_a3f9"),
        )
    )
    out = tmp_path / "outputs"
    out.mkdir()
    (out / "tickets.json").write_text(
        json.dumps([t.model_dump(mode="json") for t in report.tickets]),
        encoding="utf-8",
    )
    (out / "trace.jsonl").write_text(report.trace, encoding="utf-8")
    assert corpus_hits([out]) == []
