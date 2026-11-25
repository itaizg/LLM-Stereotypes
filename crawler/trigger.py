"""The local run trigger: n8n's one way to start and watch a crawler run (spec 004).

A small stdlib HTTP server bound to 127.0.0.1. It starts ``crawler run --approval
standing`` as a child process, enforces the maximum run time, and reports status from
files, so a restart of the trigger loses nothing (research R7). The contract
(contracts/trigger-api.md) does not say where the crawler runs; a later Kubernetes Job
launcher can serve the same requests (todo.md).

The trigger never spends money and never decides what the crawler does. It maps exit
codes to outcomes in one place, ``EXIT_TO_OUTCOME``.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from crawler.cli import REPO, results_root
from crawler.config import RUN_ID_PATTERN, new_run_id

HOST = "127.0.0.1"
MIN_TOKEN_CHARS = 32
DEFAULT_MAX_RUNTIME_S = 10_800.0
EARLY_EXIT_WAIT_S = 10.0
KILL_GRACE_S = 30.0

# The one mapping from exit code to outcome (data-model.md).
EXIT_TO_OUTCOME: dict[int, str] = {
    0: "completed",
    1: "failed",
    2: "failed",
    3: "not_approved",
    4: "skipped",
    5: "escalated",
    6: "stopped_at_limit",
}
TIMED_OUT = "timed_out"
UNKNOWN_OUTCOME = "failed"


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: str | None = None


class Process(Protocol):
    """What the trigger needs from a started run."""

    pid: int

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class Launcher(Protocol):
    def start(self, run_id: str, config: str | None, out_dir: Path) -> Process: ...


class _GroupProcess:
    """A child in its own process group, so one signal reaches everything it spawned."""

    def __init__(self, popen: subprocess.Popen[bytes]) -> None:
        self._popen = popen
        self.pid = popen.pid

    def wait(self, timeout: float | None = None) -> int:
        return self._popen.wait(timeout)

    def terminate(self) -> None:
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        self._signal(signal.SIGKILL)

    def _signal(self, number: signal.Signals) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.pid, number)


class SubprocessLauncher:
    """Runs the crawler on this machine; its console output goes to ``console.log``."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = dict(env)

    def start(self, run_id: str, config: str | None, out_dir: Path) -> Process:
        command = [
            sys.executable,
            "-m",
            "crawler",
            "run",
            "--approval",
            "standing",
            "--run-id",
            run_id,
        ]
        if config:
            command += ["--config", config]
        out_dir.mkdir(parents=True, exist_ok=True)
        with (out_dir / "console.log").open("ab") as log:
            popen = subprocess.Popen(
                command,
                cwd=REPO,
                env=self._env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return _GroupProcess(popen)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _last_crawler_line(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "the run could not start"
    ours = [line for line in lines if line.startswith("crawler:")]
    return ours[-1] if ours else "the run could not start"


class Trigger:
    """The server's behaviour, separate from HTTP so it can be tested directly."""

    def __init__(
        self,
        launcher: Launcher,
        root: Path,
        *,
        max_runtime_s: float = DEFAULT_MAX_RUNTIME_S,
        early_wait_s: float = EARLY_EXIT_WAIT_S,
        kill_grace_s: float = KILL_GRACE_S,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._launcher = launcher
        self._root = root
        self._max_runtime_s = max_runtime_s
        self._early_wait_s = early_wait_s
        self._kill_grace_s = kill_grace_s
        self._now = now

    def _trigger_file(self, run_id: str) -> Path:
        return self._root / run_id / "trigger.json"

    def _write(self, run_id: str, record: Mapping[str, object]) -> None:
        path = self._trigger_file(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def start(self, request: StartRequest) -> tuple[int, dict[str, object]]:
        config = request.config
        if config is not None and not _safe_config_path(config):
            return HTTPStatus.UNPROCESSABLE_ENTITY, {
                "outcome": "failed",
                "reason": "config must be a relative path inside the repository",
            }
        started = self._now()
        run_id = new_run_id(started)
        deadline = started + timedelta(seconds=self._max_runtime_s)
        out_dir = self._root / run_id
        process = self._launcher.start(run_id, config, out_dir)
        record: dict[str, object] = {
            "run_id": run_id,
            "pid": process.pid,
            "started_at": _iso(started),
            "deadline_at": _iso(deadline),
            "ended_at": None,
            "exit_code": None,
            "outcome": None,
        }
        self._write(run_id, record)
        finished = threading.Event()
        threading.Thread(
            target=self._watch,
            args=(process, record, finished),
            name=f"watch-{run_id}",
            daemon=True,
        ).start()
        if not finished.wait(self._early_wait_s):
            return HTTPStatus.ACCEPTED, {
                "run_id": run_id,
                "status": "running",
                "deadline_at": _iso(deadline),
            }
        return self._early_response(run_id, record)

    def _early_response(
        self, run_id: str, record: Mapping[str, object]
    ) -> tuple[int, dict[str, object]]:
        outcome, code = record["outcome"], record["exit_code"]
        out_dir = self._root / run_id
        if code == 3:
            summary = _read_json(out_dir / "summary.json") or {}
            return HTTPStatus.FORBIDDEN, {
                "run_id": run_id,
                "outcome": outcome,
                "reason": summary.get("reason", "not_approved"),
            }
        if code == 4:
            return HTTPStatus.CONFLICT, {
                "run_id": run_id,
                "outcome": outcome,
                "reason": "run_in_progress",
            }
        if code == 2:
            return HTTPStatus.UNPROCESSABLE_ENTITY, {
                "run_id": run_id,
                "outcome": outcome,
                "reason": _last_crawler_line(out_dir / "console.log"),
            }
        return HTTPStatus.OK, self.status(run_id)[1]

    def _watch(
        self, process: Process, record: dict[str, object], finished: threading.Event
    ) -> None:
        """Wait for the run; at the deadline stop it (SIGTERM, then SIGKILL)."""
        timed_out = False
        try:
            try:
                code = process.wait(self._max_runtime_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.terminate()
                try:
                    code = process.wait(self._kill_grace_s)
                except subprocess.TimeoutExpired:
                    process.kill()
                    code = process.wait()
            outcome = (
                TIMED_OUT if timed_out else EXIT_TO_OUTCOME.get(code, UNKNOWN_OUTCOME)
            )
            record.update(ended_at=_iso(self._now()), exit_code=code, outcome=outcome)
            self._write(str(record["run_id"]), record)
        finally:
            finished.set()

    def status(self, run_id: str) -> tuple[int, dict[str, object]]:
        record = _read_json(self._trigger_file(run_id))
        if record is None:
            return HTTPStatus.NOT_FOUND, {"error": "unknown run"}
        summary = _read_json(self._root / run_id / "summary.json") or {}
        outcome = record.get("outcome")
        if outcome is None and summary:
            # The trigger restarted mid-run: the run's own summary is the answer.
            reported = summary.get("status")
            outcome = "skipped" if reported == "busy" else reported
        finished = outcome is not None
        return HTTPStatus.OK, {
            "run_id": run_id,
            "status": "finished" if finished else "running",
            "outcome": outcome,
            "exit_code": record.get("exit_code"),
            "started_at": record.get("started_at"),
            "ended_at": record.get("ended_at"),
            "deadline_at": record.get("deadline_at"),
            "summary_path": str(self._root / run_id / "summary.json"),
            "cost_usd": summary.get("cost_usd"),
            "trace_export": summary.get("trace_export"),
        }


def _safe_config_path(config: str) -> bool:
    path = Path(config)
    return bool(config) and not path.is_absolute() and ".." not in path.parts


def make_handler(trigger: Trigger, token: str) -> type[BaseHTTPRequestHandler]:
    expected = f"Bearer {token}".encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "crawler-trigger"

        def log_message(self, format: str, *args: object) -> None:
            return  # access logs would carry run ids but no secrets; keep quiet

        def _send(self, status: int, body: Mapping[str, object]) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _authorised(self) -> bool:
            given = self.headers.get("Authorization", "").encode()
            return hmac.compare_digest(given, expected)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._send(HTTPStatus.OK, {"ok": True})
                return
            if not self._authorised():
                self._send(HTTPStatus.UNAUTHORIZED, {})
                return
            match = re.fullmatch(r"/runs/([^/]+)", self.path)
            if not match or not RUN_ID_PATTERN.match(match[1]):
                self._send(HTTPStatus.NOT_FOUND, {"error": "unknown run"})
                return
            status, body = trigger.status(match[1])
            self._send(status, body)

        def do_POST(self) -> None:
            if not self._authorised():
                self._send(HTTPStatus.UNAUTHORIZED, {})
                return
            if self.path != "/runs":
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                request = StartRequest.model_validate_json(raw or b"{}")
            except ValidationError:
                self._send(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {"outcome": "failed", "reason": "invalid request body"},
                )
                return
            status, body = trigger.start(request)
            self._send(status, body)

    return Handler


def make_server(trigger: Trigger, token: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((HOST, port), make_handler(trigger, token))


def serve(port: int, env: Mapping[str, str]) -> int:
    """Run the trigger until interrupted. Exit 2 when the token is missing or short."""
    token = env.get("CRAWLER_TRIGGER_TOKEN", "")
    if len(token) < MIN_TOKEN_CHARS:
        print(
            f"crawler: CRAWLER_TRIGGER_TOKEN must be set, {MIN_TOKEN_CHARS}+ "
            "characters",
            file=sys.stderr,
        )
        return 2
    try:
        max_runtime = float(env.get("CRAWLER_MAX_RUNTIME_S", DEFAULT_MAX_RUNTIME_S))
    except ValueError:
        print("crawler: CRAWLER_MAX_RUNTIME_S must be a number", file=sys.stderr)
        return 2
    trigger = Trigger(
        SubprocessLauncher(env), results_root(env), max_runtime_s=max_runtime
    )
    server = make_server(trigger, token, port)
    print(f"crawler trigger listening on {HOST}:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
