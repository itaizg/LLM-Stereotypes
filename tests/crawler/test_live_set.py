"""The live eval's labelled set and its scoring (FR-035, SC-001, SC-002)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from crawler.config import RunConfig
from crawler.evals.live import LIVE_SET, load_live_set, score
from crawler.graph import ChatModel, Tool


def test_the_set_holds_urls_and_labels_only() -> None:
    items = load_live_set()
    assert {i.expected for i in items} == {"passed", "failed"}
    assert all(i.url.startswith("https://") for i in items)
    assert len({i.url for i in items}) == len(items)
    text = LIVE_SET.read_text(encoding="utf-8")
    assert "body:" not in text and "pdf_lines" not in text


def test_bad_labels_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "set.yaml"
    path.write_text(
        "urls:\n  - {url: 'https://a.test/', expected: maybe}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="passed or failed"):
        load_live_set(path)


def test_precision_and_recall() -> None:
    rows = [
        ("passed", "passed"),
        ("passed", "passed"),
        ("passed", "failed"),
        ("failed", "passed"),
        ("failed", "failed"),
    ]
    metrics = score(rows)
    assert metrics["precision"] == pytest.approx(0.667)
    assert metrics["recall"] == pytest.approx(0.667)
    assert score([("failed", "failed")])["precision"] is None


def test_the_live_eval_needs_approval_and_writes_its_report(
    crawler_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end against fakes: every URL 404s, so nothing passes."""
    import json

    import httpx
    from crawler_fakes import FakeClassifier, FakeDetector, FakeResolver, VirtualClock

    from crawler import cli
    from crawler.evals import live
    from crawler.memory import NullMemory

    def model(
        config: RunConfig, env: Mapping[str, str], tools: dict[str, Tool]
    ) -> ChatModel:
        raise AssertionError("the live eval never calls the agent model")

    factory = cli.Factory(
        model=model,
        classifier=lambda config, env: FakeClassifier(),
        detector=FakeDetector,
        memory=lambda env: NullMemory(),
        http=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(404))
        ),
        resolver=FakeResolver,
        clock=VirtualClock,
    )
    monkeypatch.setattr(live, "RESULTS", tmp_path)
    labelled = tmp_path / "set.yaml"
    labelled.write_text(
        "urls:\n  - {url: 'https://a.test/p.pdf', expected: passed}\n"
        "  - {url: 'https://b.test/', expected: failed}\n",
        encoding="utf-8",
    )
    env = {"PIPELINE_DSN": crawler_dsn, "ANTHROPIC_API_KEY": "k"}
    args = ["--set", str(labelled)]
    assert live.main(args, env=env, ask=lambda _: "n", factory=factory) == 3
    assert live.main(args, env={}, ask=lambda _: "y", factory=factory) == 2
    assert live.main(args, env=env, ask=lambda _: "y", factory=factory) == 0
    [report] = (tmp_path / "evals").glob("cr_*.json")
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["metrics"]["recall"] == 0.0 and data["metrics"]["precision"] is None
    assert [row["got"] for row in data["rows"]] == ["failed", "failed"]
