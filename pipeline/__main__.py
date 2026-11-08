"""Command-line entry point: ``pipeline <command>`` or ``python -m pipeline <command>``.

This module only parses arguments and dispatches to stage functions, plus
``rebuild``, which merely calls the stages in order. Each stage lives in its own
module and is also callable directly (FR-018). Exit codes are part of the
contract (contracts/cli.md): 0 success, 1 unexpected, 2 usage, 3 integrity
refusal, 4 another writer holds the lock.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from pipeline import acquire, web
from pipeline.blobs import open_blob_store
from pipeline.manifests import load_catalog, read_ledger, replay_ledger
from pipeline.store import (
    ConnectionFailed,
    IntegrityRefusal,
    LockHeld,
    connect,
    redact,
    write_lock,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTEGRITY = 3
EXIT_LOCKED = 4


class UsageError(Exception):
    """The command line or environment is wrong (exit code 2)."""


Handler = Callable[[argparse.Namespace], int]


def dsn_from_env() -> str:
    """The Postgres connection string; environment only, never a flag (FR-028)."""
    dsn = os.environ.get("PIPELINE_DSN")
    if not dsn:
        raise UsageError("PIPELINE_DSN is not set")
    return dsn


def _common() -> argparse.ArgumentParser:
    """Flags every stage accepts, so they work after the command name."""
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--catalog", default="pipeline/catalog.yaml")
    parent.add_argument("--ledger", default="pipeline/ledger.jsonl")
    parent.add_argument("--exam", help="limit the stage to one exam")
    parent.add_argument("--edition", help="limit the stage to one edition")
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pipeline", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    common = _common()

    commands.add_parser(
        "catalog", parents=[common], help="load and validate the catalog"
    )

    discover = commands.add_parser(
        "discover", parents=[common], help="find candidate URLs"
    )
    discover.add_argument("--add-url", help="record a candidate URL found by hand")

    review = commands.add_parser(
        "review", parents=[common], help="approve or reject a candidate"
    )
    review.add_argument("candidate_id")
    decision = review.add_mutually_exclusive_group(required=True)
    decision.add_argument("--approve", action="store_true")
    decision.add_argument("--reject", action="store_true")
    review.add_argument("--role", help="paper, key, key_erratum, english, audio, ...")
    review.add_argument(
        "--published", help="publication date (ISO), required for key_erratum"
    )
    review.add_argument("--reason", help="why a candidate is rejected")

    fetch = commands.add_parser(
        "fetch", parents=[common], help="fetch approved sources"
    )
    fetch.add_argument("source_ids", nargs="*", help="fetch only these candidates")
    fetch.add_argument(
        "--refresh",
        action="store_true",
        help="request already-fetched files again to notice changed content",
    )

    extract = commands.add_parser("extract", parents=[common], help="extract questions")
    extract.add_argument("--workers", type=int, default=os.cpu_count() or 1)

    commands.add_parser("key", parents=[common], help="read official answer keys")

    translate = commands.add_parser(
        "translate", parents=[common], help="add English text"
    )
    translate.add_argument("--offline", action="store_true")

    verify = commands.add_parser("verify", help="hand-check and translation review")
    verify_commands = verify.add_subparsers(dest="verify_command", required=True)
    draw = verify_commands.add_parser("draw", parents=[common])
    draw.add_argument("--seed", type=int, required=True)
    draw.add_argument(
        "--kind", choices=["handcheck", "translation"], default="handcheck"
    )
    draw.add_argument("--language")
    record = verify_commands.add_parser("record", parents=[common])
    record.add_argument("csv")
    verify_commands.add_parser("status", parents=[common])

    commands.add_parser(
        "snapshot", parents=[common], help="publish a versioned snapshot"
    )

    export = commands.add_parser("export", parents=[common], help="export a suite bank")
    export.add_argument("--eligible", action="store_true")
    export.add_argument("--snapshot", required=True)
    export.add_argument("--out")

    rebuild = commands.add_parser(
        "rebuild", parents=[common], help="rebuild from raw files"
    )
    rebuild.add_argument("--schema", required=True)
    rebuild.add_argument("--compare-schema")
    return parser


def _not_implemented(args: argparse.Namespace) -> int:
    raise UsageError(f"{args.command}: not implemented yet")


def _now() -> str:
    """UTC timestamp for ledger audit fields; never part of a hash or an ID."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@contextmanager
def writing() -> Generator[psycopg.Connection]:
    """A connection holding the pipeline write lock for a whole command (R16)."""
    conn = connect(dsn_from_env())
    try:
        with write_lock(conn):
            yield conn
    finally:
        conn.close()


def _catalog(args: argparse.Namespace) -> int:
    with writing() as conn:
        print(acquire.run_catalog(conn, Path(args.catalog), Path(args.ledger)).line())
    return EXIT_OK


def _discover(args: argparse.Namespace) -> int:
    catalog = load_catalog(Path(args.catalog))
    with writing():
        if args.add_url:
            if not args.edition:
                raise UsageError("--add-url needs --edition (it selects the allowlist)")
            try:
                added = acquire.add_manual_url(
                    catalog, Path(args.ledger), args.add_url, args.edition, at=_now()
                )
            except ValueError as error:
                raise UsageError(str(error)) from None
            print(f"discover: {'recorded' if added else 'already known'}")
            return EXIT_OK

        async def crawl() -> acquire.DiscoverSummary:
            async with web.new_client() as client:
                return await acquire.discover(
                    catalog,
                    Path(args.ledger),
                    open_blob_store(),
                    client,
                    web.SystemClock(),
                    at=_now(),
                    exam=args.exam,
                )

        summary = asyncio.run(crawl())
    print(
        f"discover: seeds={summary.seeds_fetched} "
        f"candidates=+{summary.candidates_added} "
        f"rejected_off_allowlist={summary.rejected_off_allowlist} "
        f"refused={summary.refused}"
    )
    return EXIT_OK


def _review(args: argparse.Namespace) -> int:
    ledger = Path(args.ledger)
    source = replay_ledger(read_ledger(ledger)).sources.get(args.candidate_id)
    if source is None:
        raise UsageError(f"unknown candidate: {args.candidate_id}")
    # Show what is being decided before it is written (contracts/cli.md).
    print(f"candidate {source.source_id}\n  url:      {source.url}")
    print(f"  domain:   {source.domain}\n  found on: {source.found_on or '(by hand)'}")
    with writing():
        try:
            recorded = acquire.review(
                ledger,
                args.candidate_id,
                approve=args.approve,
                edition_id=args.edition,
                role=args.role,
                published=args.published,
                reason=args.reason,
                at=_now(),
            )
        except ValueError as error:
            raise UsageError(str(error)) from None
    print("review: recorded" if recorded else "review: unchanged")
    return EXIT_OK


def _fetch(args: argparse.Namespace) -> int:
    catalog = load_catalog(Path(args.catalog))
    with writing():

        async def run() -> acquire.FetchSummary:
            async with web.new_client() as client:
                return await acquire.fetch(
                    catalog,
                    Path(args.ledger),
                    open_blob_store(),
                    client,
                    web.SystemClock(),
                    at=_now(),
                    exam=args.exam,
                    refresh=args.refresh,
                    source_ids=args.source_ids,
                )

        try:
            summary = asyncio.run(run())
        except ValueError as error:
            raise UsageError(str(error)) from None
    print(
        f"fetch: fetched={summary.fetched} unchanged={summary.unchanged} "
        f"refused={summary.refused} errors={summary.errors} skipped={summary.skipped}"
    )
    return EXIT_OK


# Stages register here as they are implemented; the table is the dispatch.
HANDLERS: dict[str, Handler] = {
    "catalog": _catalog,
    "discover": _discover,
    "review": _review,
    "fetch": _fetch,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and return its exit code."""
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exit_request:
        return EXIT_OK if exit_request.code in (0, None) else EXIT_USAGE
    handler = HANDLERS.get(args.command, _not_implemented)
    try:
        return handler(args)
    except UsageError as error:
        print(f"usage: {error}", file=sys.stderr)
        return EXIT_USAGE
    except IntegrityRefusal as error:
        print(f"refused: {error}", file=sys.stderr)
        return EXIT_INTEGRITY
    except LockHeld as error:
        print(f"locked: {error}", file=sys.stderr)
        return EXIT_LOCKED
    except ConnectionFailed as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as error:
        dsn = os.environ.get("PIPELINE_DSN", "")
        message = redact(f"{type(error).__name__}: {error}", dsn) if dsn else str(error)
        print(f"error: {message}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
