"""Command-line entry point: ``pipeline <command>`` or ``python -m pipeline <command>``.

This module only parses arguments and dispatches to stage functions, plus
``rebuild``, which merely calls the stages in order. Each stage lives in its own
module and is also callable directly (FR-018). Exit codes are part of the
contract (contracts/cli.md): 0 success, 1 unexpected, 2 usage, 3 integrity
refusal, 4 another writer holds the lock.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence

from pipeline.store import ConnectionFailed, IntegrityRefusal, LockHeld, redact

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

    commands.add_parser("fetch", parents=[common], help="fetch approved candidates")

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


# Stages register here as they are implemented; the table is the dispatch.
HANDLERS: dict[str, Handler] = {}


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
