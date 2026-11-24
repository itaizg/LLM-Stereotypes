"""``probe_url``: check 1, robots.txt, signals, re-check policy (FR-009, FR-016a)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import psycopg
import pytest
from crawler_fakes import (
    NOW,
    FakeResolver,
    ReplayWeb,
    VirtualClock,
    make_services,
    make_state,
)

from crawler.contracts import (
    CheckResult,
    ProbeResult,
    ReasonCode,
    ToolError,
    ToolErrorCode,
    UrlInput,
)
from crawler.probe import RobotsCache, UrlStatus, probe_url, refusal
from crawler.runstate import RunState

URL = "https://exams.example/papers/p.pdf"


def probe(
    web: ReplayWeb, conn: psycopg.Connection, state: RunState, url: str = URL
) -> ProbeResult | ToolError:
    async def go() -> ProbeResult | ToolError:
        async with web.client() as client:
            services = make_services(client, FakeResolver(), VirtualClock(), conn)
            return await probe_url(UrlInput(url=url), state, services, RobotsCache())

    return asyncio.run(go())


def test_a_reachable_page_passes_and_unlocks_sampling(
    web: ReplayWeb, crawler_db: psycopg.Connection
) -> None:
    web.add(
        URL,
        b"%PDF-1.7",
        headers={
            "content-type": "application/pdf",
            "content-length": "8",
            "etag": '"v1"',
        },
    )
    state = make_state()
    result = probe(web, crawler_db, state)
    assert isinstance(result, ProbeResult)
    assert result.reachable is CheckResult.PASSED
    assert (result.size_bytes, result.etag) == (8, '"v1"')
    assert state.probes and URL in [p.url for p in state.probes.values()]


def test_a_disallowed_path_is_blocked_without_requesting_it(
    web: ReplayWeb, crawler_db: psycopg.Connection
) -> None:
    web.add("https://exams.example/robots.txt", "User-agent: *\nDisallow: /papers/\n")
    web.add(URL, b"secret")
    result = probe(web, crawler_db, make_state())
    assert isinstance(result, ProbeResult) and result.reason is ReasonCode.BLOCKED
    assert not result.robots_allowed
    assert web.log == ["https://exams.example/robots.txt"]


def test_a_redirect_into_a_disallowed_path_is_blocked(
    web: ReplayWeb, crawler_db: psycopg.Connection
) -> None:
    web.add("https://exams.example/robots.txt", "User-agent: *\nDisallow: /private/\n")
    web.redirect(URL, "https://exams.example/private/p.pdf")
    result = probe(web, crawler_db, make_state())
    assert isinstance(result, ProbeResult) and result.reason is ReasonCode.BLOCKED
    assert "https://exams.example/private/p.pdf" not in web.log


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (200, b'<form><input type="password"></form>', ReasonCode.LOGIN_WALL),
        (401, b"", ReasonCode.LOGIN_WALL),
        (402, b"", ReasonCode.PAYWALL),
        (200, b"<div class='paywall'>Subscribe to continue</div>", ReasonCode.PAYWALL),
        (403, b'<div class="g-recaptcha"></div>', ReasonCode.CAPTCHA),
        (404, b"", ReasonCode.HTTP_4XX),
        (503, b"", ReasonCode.HTTP_5XX),
    ],
)
def test_signals_and_errors_become_reason_codes(
    web: ReplayWeb,
    crawler_db: psycopg.Connection,
    status: int,
    body: bytes,
    reason: ReasonCode,
) -> None:
    web.add(URL, body, status=status)
    result = probe(web, crawler_db, make_state())
    assert isinstance(result, ProbeResult)
    assert (result.reachable, result.reason) == (CheckResult.FAILED, reason)


def test_the_read_is_bounded(web: ReplayWeb, crawler_db: psycopg.Connection) -> None:
    web.add(URL, b"x" * 200_000)
    result = probe(web, crawler_db, make_state())
    assert isinstance(result, ProbeResult) and result.reachable is CheckResult.PASSED


def status(
    reason: str | None, cls: str | None, attempts: int, days_ago: int
) -> UrlStatus:
    return UrlStatus(reason, cls, attempts, NOW - timedelta(days=days_ago), None, None)


def test_the_recheck_policy() -> None:
    assert refusal(None, NOW, refresh=False) is None
    assert refusal(status("blocked", "permanent", 0, 100), NOW, refresh=False) is (
        ToolErrorCode.SKIPPED_PERMANENT_FAILURE
    )
    assert refusal(status("blocked", "permanent", 0, 1), NOW, refresh=True) is None
    assert refusal(status("timeout", "transient", 1, 3), NOW, refresh=False) is (
        ToolErrorCode.SKIPPED_RECHECK_WAIT
    )
    assert refusal(status("timeout", "transient", 1, 8), NOW, refresh=False) is None
    assert refusal(status("timeout", "transient", 3, 30), NOW, refresh=False) is (
        ToolErrorCode.SKIPPED_RECHECK_WAIT
    )
    assert refusal(status(None, None, 0, 0), NOW, refresh=False) is None


def test_a_permanent_failure_is_refused_without_a_request(
    web: ReplayWeb, crawler_db: psycopg.Connection
) -> None:
    crawler_db.execute(
        "INSERT INTO crawler_url_status VALUES (%s, 'blocked', 'permanent', 0, %s)",
        (URL, NOW),
    )
    result = probe(web, crawler_db, make_state())
    assert isinstance(result, ToolError)
    assert result.error is ToolErrorCode.SKIPPED_PERMANENT_FAILURE
    assert web.log == []


def test_a_dead_host_is_skipped_unless_refresh(
    web: ReplayWeb, crawler_db: psycopg.Connection
) -> None:
    state = make_state()
    state.dead_hosts.add("exams.example")
    result = probe(web, crawler_db, state)
    assert (
        isinstance(result, ToolError)
        and result.error is ToolErrorCode.SKIPPED_DEAD_HOST
    )
    assert state.skips == ["dead_host:exams.example"] and web.log == []


def test_an_unchanged_file_is_marked_so(
    web: ReplayWeb, crawler_db: psycopg.Connection
) -> None:
    crawler_db.execute(
        "INSERT INTO crawler_url_status VALUES (%s, NULL, NULL, 0, %s, 8, %s)",
        (URL, NOW - timedelta(days=30), '"v1"'),
    )
    web.add(URL, b"%PDF-1.7", headers={"content-length": "8", "etag": '"v1"'})
    result = probe(web, crawler_db, make_state())
    assert isinstance(result, ProbeResult) and result.unchanged
    web.add(URL, b"%PDF-1.8", headers={"content-length": "8", "etag": '"v2"'})
    changed = probe(web, crawler_db, make_state())
    assert isinstance(changed, ProbeResult) and not changed.unchanged
