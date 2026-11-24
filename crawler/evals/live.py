"""Live eval (FR-035, SC-001, SC-002): probe and sample a hand-labelled URL set.

It runs by hand, never in CI: it makes real requests and paid classifier calls, so it
asks for the same budget approval as ``crawler run``. Each URL goes through
``probe_url`` and, when that passes, ``sample_content``; the crawler's verdict is
``passed`` exactly when checks 1-3 pass. Precision and recall are computed against the
labels and written to ``results/crawler/evals/<run_id>.json``. Results are reported as
measured: a missed threshold is reported as missed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

from crawler.cli import (
    DEFAULT_FACTORY,
    EXIT_OK,
    EXIT_USAGE,
    RESULTS,
    Factory,
    UsageError,
    _ask_tty,
    describe,
)
from crawler.config import RunConfig, new_run_id
from crawler.contracts import CheckResult, ProbeResult, Provider, UrlInput
from crawler.lexicon import load_lexicon
from crawler.netsafe import SafeClient
from crawler.probe import RobotsCache, probe_url
from crawler.runstate import Database, RunState, Services
from crawler.sample import sample_content
from crawler.slots import HostSlots, postgres_reserve
from crawler.tracing import open_trace
from pipeline.store import apply_schema, connect

LIVE_SET = Path(__file__).with_name("live_set.yaml")
PRECISION_TARGET = 0.90
RECALL_TARGET = 0.80


@dataclass(frozen=True)
class Labelled:
    url: str
    expected: str


def load_live_set(path: Path = LIVE_SET) -> list[Labelled]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    items = [Labelled(str(u["url"]), str(u["expected"])) for u in raw["urls"]]
    bad = [i.url for i in items if i.expected not in {"passed", "failed"}]
    if bad:
        raise ValueError(f"expected must be passed or failed: {bad}")
    return items


def score(rows: Sequence[tuple[str, str]]) -> dict[str, float | int | None]:
    """Precision and recall of ``passed`` over (expected, got) pairs."""
    tp = sum(1 for e, g in rows if e == "passed" and g == "passed")
    fp = sum(1 for e, g in rows if e == "failed" and g == "passed")
    positives = sum(1 for e, _ in rows if e == "passed")
    predicted = tp + fp
    return {
        "true_positives": tp,
        "false_positives": fp,
        "positives": positives,
        "precision": round(tp / predicted, 3) if predicted else None,
        "recall": round(tp / positives, 3) if positives else None,
    }


async def _evaluate(
    items: Sequence[Labelled], state: RunState, services: Services
) -> list[dict[str, object]]:
    robots = RobotsCache()
    rows: list[dict[str, object]] = []
    for item in items:
        verdict, reason = "failed", None
        probe = await probe_url(UrlInput(url=item.url), state, services, robots)
        if isinstance(probe, ProbeResult) and probe.reachable is CheckResult.PASSED:
            result = await sample_content(
                UrlInput(url=item.url), state, services, robots
            )
            if hasattr(result, "mcq"):
                ok = (
                    getattr(result, "downloadable", None) is CheckResult.PASSED
                    and getattr(result, "mcq", None) is CheckResult.PASSED
                )
                verdict = "passed" if ok else "failed"
                reason = getattr(result, "reason", None)
            else:
                reason = getattr(result, "error", None)
        else:
            reason = getattr(probe, "reason", None) or getattr(probe, "error", None)
        rows.append(
            {
                "url": item.url,
                "expected": item.expected,
                "got": verdict,
                "reason": None if reason is None else str(reason),
            }
        )
    return rows


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    ask: Callable[[str], str | None] = _ask_tty,
    factory: Factory = DEFAULT_FACTORY,
) -> int:
    environment = os.environ if env is None else env
    parser = argparse.ArgumentParser(prog="python -m crawler.evals.live")
    parser.add_argument("--set", default=str(LIVE_SET))
    args = parser.parse_args(argv)
    run_id = new_run_id()
    config = RunConfig(run_id=run_id, refresh=True)
    try:
        items = load_live_set(Path(args.set))
        for name in ("PIPELINE_DSN", "ANTHROPIC_API_KEY"):
            if not environment.get(name):
                raise UsageError(f"{name} is not set")
    except (UsageError, ValueError, OSError) as error:
        print(f"crawler.evals.live: {error}", file=sys.stderr)
        return EXIT_USAGE
    print(describe(config) + f"\n  live set: {len(items)} URLs", file=sys.stderr)
    if (ask("Approve this budget? [y/N] ") or "").strip().lower() != "y":
        print(
            "crawler.evals.live: budget not approved; nothing was run", file=sys.stderr
        )
        return EXIT_USAGE
    out_dir = RESULTS / "evals"
    with (
        connect(environment["PIPELINE_DSN"]) as conn,
        connect(environment["PIPELINE_DSN"]) as slot_conn,
    ):
        apply_schema(conn)
        state = RunState(config=config, memory=factory.memory(environment))
        tracing = open_trace(run_id, out_dir / run_id, environment)

        async def go() -> list[dict[str, object]]:
            async with (
                factory.http() as client,
                httpx.AsyncClient(timeout=30) as search,
            ):
                services = Services(
                    safe=SafeClient(
                        client,
                        factory.resolver(),
                        HostSlots(
                            postgres_reserve(slot_conn),
                            factory.clock(),
                            config.host_interval_s,
                            config.concurrency,
                        ),
                    ),
                    lexicon=load_lexicon(),
                    tracing=tracing,
                    db=Database(conn),
                    search_client=search,
                    search_keys={Provider.TAVILY: ""},
                    classifier=factory.classifier(config, environment),
                    detector=factory.detector(),
                )
                return await _evaluate(items, state, services)

        try:
            rows = asyncio.run(go())
        finally:
            tracing.shutdown()
    metrics = score([(str(r["expected"]), str(r["got"])) for r in rows])
    report = {
        "run_id": run_id,
        "at": datetime.now(UTC).isoformat(),
        "cost_usd": round(state.cost_usd, 4),
        "metrics": metrics,
        "targets": {"precision": PRECISION_TARGET, "recall": RECALL_TARGET},
        "rows": rows,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{run_id}.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(metrics))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
