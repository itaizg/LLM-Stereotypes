"""``python -m crawler run | approve | trigger | review | status`` (contracts/cli.md).

``run`` asks the maintainer to approve the budget on the terminal before anything that
costs money (FR-001, constitution HITL): without a TTY, or on any answer but ``y``, it
exits 3 and writes no database rows, only a ``summary.json`` that says so (spec 004).
``run --approval standing`` skips the prompt and relies on the 30-day approval that
``approve`` stored. Required keys are named when missing, never printed.

Exit codes: 0 completed; 1 failed; 2 usage error; 3 budget not approved; 4 busy
(another run holds the run lock); 5 escalated (five consecutive failures of one tool);
6 stopped at a limit.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb

from crawler import approval
from crawler.config import (
    RUN_ID_PATTERN,
    RunConfig,
    catalog_gaps,
    load_overrides,
    new_run_id,
)
from crawler.contracts import Provider, RunStatus
from crawler.graph import ChatModel, Crawler, Tool, build_tools, chat_model
from crawler.lexicon import LexiconError, load_lexicon
from crawler.memory import CrawlerMemory
from crawler.netsafe import SafeClient, SystemResolver
from crawler.runstate import Classifier, Database, LanguageDetector, RunState, Services
from crawler.slots import HostSlots, postgres_reserve
from crawler.tracing import open_trace, redact_secrets, secret_values
from pipeline.manifests import DEFAULT_CATALOG, load_catalog
from pipeline.store import ConnectionFailed, apply_schema, connect
from pipeline.web import SystemClock, new_client

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / "results" / "crawler"
TODO = REPO / "TODO.md"

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_ESCALATED, EXIT_LIMIT = 0, 1, 2, 5, 6
EXIT_NOT_APPROVED, EXIT_BUSY = 3, 4
KEY_ENV = {Provider.TAVILY: "TAVILY_API_KEY", Provider.BRAVE: "BRAVE_API_KEY"}


# One run at a time: the key of the Postgres advisory lock (spec 004 FR-003).
RUN_LOCK = "crawler.run"


class UsageError(Exception):
    pass


class Terminated(BaseException):
    """Raised inside the run by the SIGTERM handler, so the failure path records it.

    A ``BaseException`` on purpose, like ``KeyboardInterrupt``: the agent's own retry
    loops catch ``Exception`` and must not swallow a stop request.
    """


def _on_sigterm(signum: int, frame: FrameType | None) -> None:
    raise Terminated("terminated by SIGTERM")


def results_root(env: Mapping[str, str]) -> Path:
    """Where run artifacts go: ``CRAWLER_RESULTS_DIR``, else ``results/crawler``.

    Configurable so a containerized run can write to a mounted volume (spec 004
    FR-005a); ``RESULTS`` stays the one default.
    """
    configured = env.get("CRAWLER_RESULTS_DIR")
    return Path(configured) if configured else RESULTS


def write_summary(out_dir: Path, summary: Mapping[str, object]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )


def refusal_summary(run_id: str, status: str, reason: str) -> dict[str, object]:
    """The summary of a run that was refused before it spent anything."""
    return {
        "run_id": run_id,
        "status": status,
        "reason": reason,
        "stop_limit": None,
        "escalated_tool": None,
        "queries_used": 0,
        "iterations_used": 0,
        "cost_usd": 0.0,
        "completeness": 0.0,
        "proposals": 0,
        "passed": 0,
    }


@dataclass
class Factory:
    """How ``run`` builds what it talks to; tests replace each piece with a fake."""

    model: Callable[[RunConfig, Mapping[str, str], dict[str, Tool]], ChatModel]
    classifier: Callable[[RunConfig, Mapping[str, str]], Classifier]
    detector: Callable[[], LanguageDetector]
    memory: Callable[[Mapping[str, str]], CrawlerMemory]
    http: Callable[[], httpx.AsyncClient]
    resolver: Callable[[], Any]
    clock: Callable[[], Any]


def _real_model(
    config: RunConfig, env: Mapping[str, str], tools: dict[str, Tool]
) -> ChatModel:
    spec = config.agent_model
    return chat_model(
        spec.model, spec.max_output_tokens, env["ANTHROPIC_API_KEY"], tools
    )


def _real_classifier(config: RunConfig, env: Mapping[str, str]) -> Classifier:
    from crawler.classify import ClaudeClassifier

    return ClaudeClassifier(config.classifier_model, env["ANTHROPIC_API_KEY"])


def _real_detector() -> LanguageDetector:
    from crawler.sample import LinguaDetector

    return LinguaDetector()


def _real_memory(env: Mapping[str, str]) -> CrawlerMemory:
    from crawler.memory import open_memory

    return open_memory(env)


DEFAULT_FACTORY = Factory(
    model=_real_model,
    classifier=_real_classifier,
    detector=_real_detector,
    memory=_real_memory,
    http=new_client,
    resolver=SystemResolver,
    clock=SystemClock,
)


def _ask_tty(prompt: str) -> str | None:
    if not sys.stdin.isatty():
        return None
    try:
        return input(prompt)
    except EOFError:
        return None


def build_config(args: argparse.Namespace, run_id: str) -> RunConfig:
    overrides: dict[str, object] = (
        load_overrides(Path(args.config)) if args.config else {}
    )
    if getattr(args, "refresh", False):
        overrides["refresh"] = True
    if getattr(args, "provider", None):
        overrides["primary_provider"] = args.provider
    exams = tuple(sorted(load_catalog(DEFAULT_CATALOG).exams))
    overrides.setdefault("exams_first", exams)
    overrides.setdefault("gaps", catalog_gaps(TODO))
    return RunConfig.model_validate({**overrides, "run_id": run_id})


def describe(config: RunConfig) -> str:
    b, a, c = config.budget, config.agent_model, config.classifier_model
    return (
        f"Crawler run {config.run_id}\n"
        f"  budget: {b.max_queries} searches, ${b.max_cost_usd:.2f} of model spend, "
        f"{b.max_iterations} turns (stops at the first; no automatic retry)\n"
        f"  agent model: {a.model} (${a.price_in_usd_per_mtok}/"
        f"${a.price_out_usd_per_mtok} per Mtok in/out)\n"
        f"  classifier: {c.model} (${c.price_in_usd_per_mtok}/"
        f"${c.price_out_usd_per_mtok} per Mtok), at most "
        f"${config.classifier_max_cost_usd:.2f} per call\n"
        f"  search: {config.primary_provider} first; refresh: {config.refresh}\n"
        f"  exams first: {', '.join(config.exams_first) or 'none'}"
    )


def _require(env: Mapping[str, str], config: RunConfig) -> dict[Provider, str]:
    if not env.get("PIPELINE_DSN"):
        raise UsageError("PIPELINE_DSN is not set")
    if not env.get("ANTHROPIC_API_KEY"):
        raise UsageError("ANTHROPIC_API_KEY is not set")
    keys = {p: env[v] for p, v in KEY_ENV.items() if env.get(v)}
    if config.primary_provider not in keys:
        raise UsageError(f"{KEY_ENV[config.primary_provider]} is not set")
    return keys


def _status_of(state: RunState) -> tuple[RunStatus, int]:
    if state.escalated_tool is not None:
        return RunStatus.ESCALATED, EXIT_ESCALATED
    if state.stop_limit is not None:
        return RunStatus.STOPPED_AT_LIMIT, EXIT_LIMIT
    return RunStatus.COMPLETED, EXIT_OK


def reopened_count(conn: psycopg.Connection, run_id: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM crawler_proposals WHERE run_id = %s "
        "AND labels->'flags' ? 'reopened_after_approval'",
        (run_id,),
    ).fetchone()
    return int(row[0]) if row else 0


async def _crawl(
    state: RunState,
    config: RunConfig,
    env: Mapping[str, str],
    factory: Factory,
    conn: psycopg.Connection,
    slot_conn: psycopg.Connection,
    keys: dict[Provider, str],
    out_dir: Path,
) -> RunState:
    tracing = open_trace(config.run_id, out_dir, env)
    try:
        async with (
            factory.http() as client,
            httpx.AsyncClient(timeout=30) as search_client,
        ):
            clock = factory.clock()
            slots = HostSlots(
                postgres_reserve(slot_conn),
                clock,
                config.host_interval_s,
                config.concurrency,
            )
            services = Services(
                safe=SafeClient(client, factory.resolver(), slots),
                lexicon=load_lexicon(),
                tracing=tracing,
                db=Database(conn),
                search_client=search_client,
                search_keys=keys,
                classifier=factory.classifier(config, env),
                detector=factory.detector(),
            )
            tools = build_tools(state, services)
            model = factory.model(config, env, tools)
            return await Crawler(state, services, model, tools).run()
    finally:
        tracing.shutdown()


def _refuse(
    env: Mapping[str, str], run_id: str, status: str, reason: str, message: str
) -> None:
    print(f"crawler: {message}; nothing was run", file=sys.stderr)
    write_summary(results_root(env) / run_id, refusal_summary(run_id, status, reason))


def _try_run_lock(conn: psycopg.Connection) -> bool:
    row = conn.execute(
        "SELECT pg_try_advisory_lock(hashtext(%s))", (RUN_LOCK,)
    ).fetchone()
    return bool(row and row[0])


def _install_sigterm() -> Callable[[], None]:
    """Make SIGTERM raise inside the run; return a function that undoes it."""
    try:
        previous = signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:  # not the main thread: nothing to install
        return lambda: None

    def restore() -> None:
        signal.signal(signal.SIGTERM, previous)

    return restore


def cmd_run(
    args: argparse.Namespace,
    env: Mapping[str, str],
    ask: Callable[[str], str | None],
    factory: Factory,
    now: Callable[[], datetime],
) -> int:
    standing = args.approval == "standing"
    run_id = args.run_id or new_run_id(now())
    try:
        if args.run_id and not RUN_ID_PATTERN.match(args.run_id):
            raise UsageError(f"--run-id must look like {new_run_id(now())}")
        config = build_config(args, run_id)
        load_lexicon()
        keys = _require(env, config)
    except (UsageError, LexiconError, ValueError) as error:
        print(f"crawler: {error}", file=sys.stderr)
        return EXIT_USAGE
    print(describe(config), file=sys.stderr)
    secrets = secret_values(env)
    try:
        conn = connect(env["PIPELINE_DSN"])
        slot_conn = connect(env["PIPELINE_DSN"])
    except ConnectionFailed as error:
        print(f"crawler: {error}", file=sys.stderr)
        return EXIT_FAILED
    out_dir = results_root(env) / run_id
    with conn, slot_conn:
        apply_schema(conn)
        approval_id: str | None = None
        if standing:
            checked = approval.check(conn, config, now())
            if isinstance(checked, approval.Refusal):
                _refuse(
                    env,
                    run_id,
                    "not_approved",
                    checked.reason,
                    f"budget not approved: {checked.message()}",
                )
                return EXIT_NOT_APPROVED
            approval_id = checked.approval_id
            approved_by, approved_at = checked.approved_by, checked.approved_at
        else:
            answer = ask("Approve this budget? [y/N] ")
            if (answer or "").strip().lower() != "y":
                _refuse(
                    env,
                    run_id,
                    "not_approved",
                    "refused_at_prompt",
                    "budget not approved",
                )
                return EXIT_NOT_APPROVED
            approved_at = now()
            approved_by = env.get("USER") or getpass.getuser()
        if (
            args.run_id
            and conn.execute(
                "SELECT 1 FROM crawler_runs WHERE run_id = %s", (run_id,)
            ).fetchone()
        ):
            print(f"crawler: run {run_id} already exists", file=sys.stderr)
            return EXIT_USAGE
        if not _try_run_lock(conn):
            _refuse(
                env, run_id, "busy", "run_in_progress", "another run is in progress"
            )
            return EXIT_BUSY
        config = config.model_copy(
            update={"approved_by": approved_by, "approved_at": approved_at}
        )
        memory = factory.memory(env)
        conn.execute(
            "INSERT INTO crawler_runs (run_id, config, approved_by, approved_at, "
            "status, memory_available, started_at, approval_id) "
            "VALUES (%s, %s, %s, %s, 'running', %s, %s, %s)",
            (
                run_id,
                Jsonb(config.model_dump(mode="json")),
                approved_by,
                approved_at,
                memory.available,
                approved_at,
                approval_id,
            ),
        )
        state = RunState(config=config, memory=memory, now=now)
        state.queries_tried |= memory.queries_tried()
        state.dead_hosts |= set(memory.dead_hosts(now()))
        status, code = RunStatus.FAILED, EXIT_FAILED
        terminated = False
        restore_sigterm = _install_sigterm()
        try:
            asyncio.run(
                _crawl(state, config, env, factory, conn, slot_conn, keys, out_dir)
            )
            status, code = _status_of(state)
        except (Exception, Terminated) as error:
            terminated = isinstance(error, Terminated)
            print(
                f"crawler: failed: {redact_secrets(repr(error), secrets)}",
                file=sys.stderr,
            )
        finally:
            restore_sigterm()
        conn.execute(
            "UPDATE crawler_runs SET status = %s, stop_limit = %s, queries_used = %s, "
            "iterations_used = %s, cost_usd = %s, completeness = %s, "
            "provider_switches = %s, ended_at = %s WHERE run_id = %s",
            (
                str(status),
                str(state.stop_limit) if state.stop_limit else None,
                state.queries_used,
                state.iterations_used,
                round(state.cost_usd, 4),
                state.completeness,
                Jsonb([s.as_json() for s in state.provider_switches]),
                now(),
                run_id,
            ),
        )
        reopened = reopened_count(conn, run_id)
    summary = {
        "run_id": run_id,
        "status": str(status),
        "stop_limit": str(state.stop_limit) if state.stop_limit else None,
        "escalated_tool": state.escalated_tool,
        "queries_used": state.queries_used,
        "iterations_used": state.iterations_used,
        "cost_usd": round(state.cost_usd, 4),
        "completeness": state.completeness,
        "proposals": len(state.written),
        "passed": sum(1 for *_, s in state.written.values() if s == "passed"),
        "reopened": reopened,
        "provider_switches": [s.as_json() for s in state.provider_switches],
        "memory_available": state.memory.available,
        "approval_id": approval_id,
        "terminated": terminated,
    }
    write_summary(out_dir, summary)
    print(json.dumps(summary), file=sys.stderr)
    if reopened:
        print(
            f"crawler: {reopened} approved source(s) changed and are suspended until "
            "re-reviewed; run: pipeline import-decisions --reopen-only",
            file=sys.stderr,
        )
    return code


def cmd_approve(
    args: argparse.Namespace,
    env: Mapping[str, str],
    ask: Callable[[str], str | None],
    now: Callable[[], datetime],
) -> int:
    if not env.get("PIPELINE_DSN"):
        print("crawler: PIPELINE_DSN is not set", file=sys.stderr)
        return EXIT_USAGE
    if args.revoke:
        with connect(env["PIPELINE_DSN"]) as conn:
            apply_schema(conn)
            revoked = approval.revoke(conn, now())
        print("revoked: standing approval" if revoked else "revoked: none was active")
        return EXIT_OK
    try:
        config = build_config(args, new_run_id(now()))
    except (ValueError, OSError) as error:
        print(f"crawler: {error}", file=sys.stderr)
        return EXIT_USAGE
    print(describe(config), file=sys.stderr)
    answer = ask(
        f"Approve this standing budget for {approval.APPROVAL_DAYS} days? [y/N] "
    )
    if (answer or "").strip().lower() != "y":
        print("crawler: not approved; nothing was stored", file=sys.stderr)
        return EXIT_NOT_APPROVED
    with connect(env["PIPELINE_DSN"]) as conn:
        apply_schema(conn)
        made = approval.create(
            conn, config, env.get("USER") or getpass.getuser(), now()
        )
    print(f"approval_id: {made.approval_id}")
    print(f"expires_at: {made.expires_at.isoformat()}")
    return EXIT_OK


def cmd_status(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    if not env.get("PIPELINE_DSN"):
        print("crawler: PIPELINE_DSN is not set", file=sys.stderr)
        return EXIT_USAGE
    with connect(env["PIPELINE_DSN"]) as conn:
        apply_schema(conn)
        if args.run_id is None:
            rows = conn.execute(
                "SELECT run_id, status, stop_limit, queries_used, cost_usd, "
                "completeness "
                "FROM crawler_runs ORDER BY started_at DESC LIMIT 20"
            ).fetchall()
            for row in rows:
                print("\t".join("" if v is None else str(v) for v in row))
            print(f"runs: count={len(rows)}")
            return EXIT_OK
        run = conn.execute(
            "SELECT status, stop_limit, queries_used, iterations_used, cost_usd, "
            "completeness, memory_available FROM crawler_runs WHERE run_id = %s",
            (args.run_id,),
        ).fetchone()
        if run is None:
            print(f"crawler: unknown run {args.run_id}", file=sys.stderr)
            return EXIT_USAGE
        names = (
            "status",
            "stop_limit",
            "queries_used",
            "iterations_used",
            "cost_usd",
            "completeness",
            "memory_available",
        )
        for name, value in zip(names, run, strict=True):
            print(f"{name}: {value}")
        counts = conn.execute(
            "SELECT validation_status, coalesce(reason, '-'), count(*) "
            "FROM crawler_proposals WHERE run_id = %s GROUP BY 1, 2 ORDER BY 1, 2",
            (args.run_id,),
        ).fetchall()
        for status, reason, count in counts:
            print(f"{status}\t{reason}\t{count}")
    return EXIT_OK


def cmd_review(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    from crawler.review import write_review

    if not env.get("PIPELINE_DSN"):
        print("crawler: PIPELINE_DSN is not set", file=sys.stderr)
        return EXIT_USAGE
    with connect(env["PIPELINE_DSN"]) as conn:
        apply_schema(conn)
        out = (
            Path(args.out)
            if args.out
            else results_root(env) / args.run_id / "review.html"
        )
        try:
            groups = write_review(conn, args.run_id, out)
        except LookupError as error:
            print(f"crawler: {error}", file=sys.stderr)
            return EXIT_USAGE
    print(f"review: {out} groups={groups}")
    return EXIT_OK


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="python -m crawler")
    commands = top.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run", help="run the crawler (asks to approve the budget)"
    )
    run.add_argument("--config")
    run.add_argument("--refresh", action="store_true")
    run.add_argument("--provider", choices=[p.value for p in Provider])
    run.add_argument(
        "--approval",
        choices=["tty", "standing"],
        default="tty",
        help="approve at the terminal, or rely on the stored standing approval",
    )
    run.add_argument("--run-id", help="use this run id (the trigger picks it)")
    approve = commands.add_parser(
        "approve", help="give or revoke a 30-day standing budget approval"
    )
    approve.add_argument("--config")
    approve.add_argument("--revoke", action="store_true")
    trigger = commands.add_parser("trigger", help="serve the local run trigger")
    trigger.add_argument("--port", type=int, default=8765)
    review = commands.add_parser("review", help="render a run's review page")
    review.add_argument("run_id")
    review.add_argument("--out")
    status = commands.add_parser("status", help="list runs, or one run's summary")
    status.add_argument("run_id", nargs="?")
    return top


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    ask: Callable[[str], str | None] = _ask_tty,
    factory: Factory = DEFAULT_FACTORY,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    environment = os.environ if env is None else env
    try:
        args = parser().parse_args(argv)
    except SystemExit as stop:
        return EXIT_USAGE if stop.code else EXIT_OK
    if args.command == "run":
        return cmd_run(args, environment, ask, factory, now)
    if args.command == "approve":
        return cmd_approve(args, environment, ask, now)
    if args.command == "trigger":
        from crawler.trigger import serve

        return serve(args.port, environment)
    if args.command == "review":
        return cmd_review(args, environment)
    return cmd_status(args, environment)
