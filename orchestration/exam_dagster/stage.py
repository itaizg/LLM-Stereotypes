"""Run one pipeline stage as a Dagster step.

Every asset goes through ``run_stage``, so the exit-code policy lives in one place
(research A1/A2). The pipeline is a subprocess, never an import: the CLI is the
contract (write lock, exit codes, "a step runs exactly what a maintainer types"), and
keeping it thin is FR-001. Same interpreter, so no second environment is needed.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypedDict

import dagster as dg

PIPELINE_WRITER = "pipeline_writer"

# The one retry policy. Exit 1 (unexpected) and exit 4 (write lock held) retry; a lock
# wait of three minutes outlasts a hand-run review without stalling a run.
RETRY_POLICY = dg.RetryPolicy(max_retries=2, delay=180)

EXIT_RETRY = frozenset({1, 4})

# A stage that never returns would hold the one writer slot forever and never reach the
# retry policy. Six hours is far above the longest polite fetch; a stage that exceeds it
# is killed and counted as an unexpected failure (exit 1, retried). Stages resume, so a
# retry loses only the unfinished item.
STAGE_TIMEOUT_SECONDS = 6 * 60 * 60


def project_root() -> str:
    """``PIPELINE_PROJECT`` (compose mounts the repo there), else this checkout."""
    configured = os.environ.get("PIPELINE_PROJECT")
    return configured or str(Path(__file__).resolve().parents[2])


def command_prefix() -> tuple[str, ...]:
    """``python -m pipeline`` in the interpreter that runs Dagster."""
    return (sys.executable, "-m", "pipeline")


class RetryableStageError(Exception):
    """Exit 1 or 4: the step may succeed if tried again (``RETRY_POLICY`` does that)."""


class StageResult(TypedDict):
    """What a step reports as metadata: small, with no text from the corpus."""

    stage: str
    exam: str | None
    summary: str


@dataclass(frozen=True)
class Completed:
    """The three facts about a finished command that the policy needs."""

    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    """Runs a command in ``cwd``; tests replace it so no subprocess starts."""

    def __call__(self, command: Sequence[str], *, cwd: str) -> Completed: ...


def _text(output: str | bytes | None) -> str:
    if output is None:
        return ""
    return output if isinstance(output, str) else output.decode("utf-8", "replace")


def _subprocess_runner(command: Sequence[str], *, cwd: str) -> Completed:
    # The environment is inherited unchanged: the DSN and tokens reach the pipeline
    # only through it, and are never read or logged here (FR-015).
    # The command is a fixed argv list built from constants and catalog exam ids (no
    # shell), so there is nothing to inject into.
    try:
        proc = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
            list(command),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=STAGE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run has already killed the child, which releases its write lock.
        stderr = _text(exc.stderr) + f"stage timed out after {STAGE_TIMEOUT_SECONDS}s\n"
        return Completed(1, _text(exc.stdout), stderr)
    return Completed(proc.returncode, proc.stdout, proc.stderr)


RUNNER: Runner = _subprocess_runner

_log = logging.getLogger(__name__)


def summary_line(stdout: str, prefix: str) -> str:
    """The stage's last ``<prefix>: …`` line, or an empty string if it printed none."""
    marker = f"{prefix}:"
    lines = [ln for ln in stdout.splitlines() if ln.startswith(marker)]
    return lines[-1] if lines else ""


def run_stage(
    argv: Sequence[str],
    *,
    exam: str | None = None,
    logger: logging.Logger | dg.DagsterLogManager | None = None,
) -> StageResult:
    """Run ``pipeline <argv>`` and turn its exit code into a step outcome."""
    return run_stage_output(argv, exam=exam, logger=logger)[0]


def run_stage_output(
    argv: Sequence[str],
    *,
    exam: str | None = None,
    logger: logging.Logger | dg.DagsterLogManager | None = None,
) -> tuple[StageResult, list[str]]:
    """``run_stage`` that also returns the stdout lines, for the listing commands.

    Exit 0 succeeds. Exits 1 and 4 raise ``RetryableStageError``. Exits 2, 3 and any
    code outside the CLI contract raise ``dg.Failure(allow_retries=False)``, which
    fails the step without using the remaining retries: a usage error or an integrity
    refusal will not fix itself.
    """
    log = logger or _log
    command = (*command_prefix(), *argv)
    stage = argv[0]
    log.info("pipeline %s", " ".join(argv))
    done = RUNNER(command, cwd=project_root())
    for line in done.stdout.splitlines():
        log.info("%s", line)
    for line in done.stderr.splitlines():
        log.warning("%s", line)
    log.info("exit=%d", done.returncode)

    if done.returncode == 0:
        result = StageResult(
            stage=stage, exam=exam, summary=summary_line(done.stdout, stage)
        )
        return result, done.stdout.splitlines()
    message = f"pipeline {stage} exited {done.returncode}"
    if done.returncode in EXIT_RETRY:
        raise RetryableStageError(message)
    raise dg.Failure(description=message, allow_retries=False)


# The only lines the assets parse (contracts/pipeline-cli-additions.md). Changing one
# of these output formats is a breaking change: it must update this parser and its
# tests in the same pull request.
_CANDIDATE_LINE = re.compile(r"^candidate (\S+) exam=(\S+) domain=(\S+)$")


def listed_ids(lines: Sequence[str], summary_prefix: str) -> list[str]:
    """The ids a listing command printed above its ``<prefix>: …`` summary line."""
    marker = f"{summary_prefix}:"
    return [ln for ln in lines if ln and not ln.startswith(marker)]


def pending_ids(lines: Sequence[str]) -> list[str]:
    """Source ids from ``candidate <id> exam=<exam> domain=<domain>`` lines."""
    return [m[1] for ln in lines if (m := _CANDIDATE_LINE.match(ln))]
