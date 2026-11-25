"""The run trigger over real HTTP on a loopback port, with a fake launcher (T010)."""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Generator
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from crawler import trigger
from crawler.trigger import (
    EXIT_TO_OUTCOME,
    StartRequest,
    Trigger,
    make_server,
    serve,
)

TOKEN = "t" * 40


class FakeProcess:
    """A child that exits when told to, or never (to exercise the deadline)."""

    def __init__(self, exit_code: int | None, ignores_term: bool = False) -> None:
        self.pid = 4242
        self.signals: list[str] = []
        self._code = exit_code
        self._ignores_term = ignores_term
        self._done = threading.Event()
        if exit_code is not None:
            self._done.set()

    def wait(self, timeout: float | None = None) -> int:
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("fake", timeout or 0)
        assert self._code is not None
        return self._code

    def terminate(self) -> None:
        self.signals.append("TERM")
        if not self._ignores_term:
            self._code = -15
            self._done.set()

    def kill(self) -> None:
        self.signals.append("KILL")
        self._code = -9
        self._done.set()


class FakeLauncher:
    def __init__(self, process: FakeProcess, summary: dict[str, object] | None = None):
        self.process = process
        self.summary = summary
        self.started: list[tuple[str, str | None]] = []

    def start(self, run_id: str, config: str | None, out_dir: Path) -> FakeProcess:
        self.started.append((run_id, config))
        if self.summary is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "summary.json").write_text(json.dumps(self.summary))
        return self.process


Serve = Callable[..., tuple[str, FakeLauncher]]


@pytest.fixture
def serving(tmp_path: Path) -> Generator[Serve]:
    servers: list[ThreadingHTTPServer] = []

    def start(
        process: FakeProcess,
        summary: dict[str, object] | None = None,
        *,
        early_wait_s: float = 1.0,
        max_runtime_s: float = 60.0,
        kill_grace_s: float = 30.0,
    ) -> tuple[str, FakeLauncher]:
        launcher = FakeLauncher(process, summary)
        trigger = Trigger(
            launcher,
            tmp_path,
            max_runtime_s=max_runtime_s,
            early_wait_s=early_wait_s,
            kill_grace_s=kill_grace_s,
        )
        server = make_server(trigger, TOKEN, 0)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_address[1]}", launcher

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def call(
    url: str,
    method: str = "GET",
    body: object = None,
    token: str | None = TOKEN,
) -> tuple[int, dict[str, object]]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def test_healthz_needs_no_token(serving: Serve) -> None:
    base, _ = serving(FakeProcess(0))
    assert call(f"{base}/healthz", token=None) == (200, {"ok": True})


@pytest.mark.parametrize("token", [None, "wrong", TOKEN + "x"])
def test_a_missing_or_wrong_token_is_refused(serving: Serve, token: str | None) -> None:
    base, launcher = serving(FakeProcess(0))
    assert call(f"{base}/runs", "POST", {}, token)[0] == 401
    assert call(f"{base}/runs/cr_20261125T020000_abcd", token=token)[0] == 401
    assert launcher.started == []


def test_a_short_token_stops_the_server_starting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert serve(0, {"CRAWLER_TRIGGER_TOKEN": "short"}) == 2
    assert serve(0, {}) == 2
    assert "CRAWLER_TRIGGER_TOKEN" in capsys.readouterr().err


def test_a_run_still_going_is_accepted(serving: Serve) -> None:
    base, launcher = serving(FakeProcess(None), early_wait_s=0.1)
    status, body = call(f"{base}/runs", "POST", {})
    assert status == 202 and body["status"] == "running"
    assert launcher.started[0][0] == body["run_id"]
    assert body["deadline_at"]


@pytest.mark.parametrize(
    ("code", "http", "outcome"),
    [(3, 403, "not_approved"), (4, 409, "skipped"), (2, 422, "failed")],
)
def test_early_refusals_map_to_http_codes(
    serving: Serve, code: int, http: int, outcome: str
) -> None:
    summary = {"status": "not_approved", "reason": "expired"} if code == 3 else None
    base, _ = serving(FakeProcess(code), summary)
    status, body = call(f"{base}/runs", "POST", {})
    assert (status, body["outcome"]) == (http, outcome)
    if code == 3:
        assert body["reason"] == "expired"
    if code == 4:
        assert body["reason"] == "run_in_progress"


def test_a_fast_finish_returns_the_status_body(serving: Serve) -> None:
    base, _ = serving(FakeProcess(0), {"status": "completed", "cost_usd": 1.5})
    status, body = call(f"{base}/runs", "POST", {})
    assert status == 200
    assert body["outcome"] == "completed" and body["cost_usd"] == 1.5


def test_unknown_fields_and_bad_configs_are_422(serving: Serve) -> None:
    base, launcher = serving(FakeProcess(0))
    assert call(f"{base}/runs", "POST", {"extra": 1})[0] == 422
    assert call(f"{base}/runs", "POST", {"config": "/etc/passwd"})[0] == 422
    assert call(f"{base}/runs", "POST", {"config": "../x.yaml"})[0] == 422
    assert launcher.started == []


def test_a_config_path_is_passed_to_the_launcher(serving: Serve) -> None:
    base, launcher = serving(FakeProcess(0))
    call(f"{base}/runs", "POST", {"config": "configs/night.yaml"})
    assert launcher.started[0][1] == "configs/night.yaml"


def test_unknown_run_ids_are_404(serving: Serve) -> None:
    base, _ = serving(FakeProcess(0))
    assert call(f"{base}/runs/cr_20261125T020000_abcd")[0] == 404
    assert call(f"{base}/runs/..%2Fsecret")[0] == 404


def test_status_survives_a_trigger_restart(serving: Serve, tmp_path: Path) -> None:
    base, _ = serving(FakeProcess(None), early_wait_s=0.1)
    _, started = call(f"{base}/runs", "POST", {})
    run_id = str(started["run_id"])
    assert call(f"{base}/runs/{run_id}")[1]["status"] == "running"
    # The run finishes while the trigger is down; a new instance answers from files.
    (tmp_path / run_id / "summary.json").write_text(
        json.dumps({"status": "completed", "cost_usd": 2.0})
    )
    new_base, _ = serving(FakeProcess(0))
    status, body = call(f"{new_base}/runs/{run_id}")
    assert status == 200
    assert body["status"] == "finished" and body["outcome"] == "completed"
    assert body["cost_usd"] == 2.0


def test_the_deadline_sends_term_then_kill(serving: Serve, tmp_path: Path) -> None:
    process = FakeProcess(None, ignores_term=True)
    base, _ = serving(process, early_wait_s=3.0, max_runtime_s=0.1, kill_grace_s=0.1)
    status, body = call(f"{base}/runs", "POST", {})
    assert status == 200 and body["outcome"] == "timed_out"
    assert process.signals == ["TERM", "KILL"]
    record = json.loads((tmp_path / str(body["run_id"]) / "trigger.json").read_text())
    assert record["outcome"] == "timed_out"


def test_a_run_that_obeys_term_is_still_timed_out(serving: Serve) -> None:
    process = FakeProcess(None)
    base, _ = serving(process, early_wait_s=3.0, max_runtime_s=0.1)
    _, body = call(f"{base}/runs", "POST", {})
    assert body["outcome"] == "timed_out" and process.signals == ["TERM"]


def test_the_exit_code_mapping_matches_the_data_model() -> None:
    assert EXIT_TO_OUTCOME == {
        0: "completed",
        1: "failed",
        2: "failed",
        3: "not_approved",
        4: "skipped",
        5: "escalated",
        6: "stopped_at_limit",
    }
    assert trigger.TIMED_OUT == "timed_out"
    assert StartRequest().config is None
