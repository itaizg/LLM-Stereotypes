"""``probe_url``: is the URL reachable *for us* (check 1, FR-011, FR-009)?

The probe reads robots.txt for the crawler's user agent first; a disallowed path is
``blocked`` and the page itself is never requested (FR-012). Otherwise it reads at most
64 KB of the page through ``netsafe`` and looks for login, paywall and CAPTCHA signals.
It records its result in the run state, which is what unlocks ``sample_content`` for
that URL (research R4).

It refuses, without sending anything, a URL whose last failure was permanent or whose
transient re-check is not due yet (FR-016a, ``crawler_url_status``), and a host memory
marks dead (US4), unless the run has ``refresh`` set.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import psycopg

from crawler.config import (
    MAX_TRANSIENT_ATTEMPTS,
    PROBE_MAX_BYTES,
    PROBE_RETRIES,
    PROBE_TIMEOUT_S,
    RECHECK_AFTER_DAYS,
)
from crawler.contracts import (
    CheckResult,
    ProbeResult,
    ReasonCode,
    TicketInput,
    ToolError,
    ToolErrorCode,
    UrlInput,
    domain_of,
    normalise_url,
)
from crawler.netsafe import Refused, SafeClient, SafeResponse
from crawler.runstate import RunState, Services
from pipeline.web import USER_AGENT

DESCRIPTION = (
    "Check whether a URL is reachable for us: follows redirects safely, checks "
    "robots.txt, and looks for login, paywall or CAPTCHA signals. Reads at most 64 KB."
)

ROBOTS_MAX_BYTES = 512 * 1024

_LOGIN_PATH = re.compile(r"/(log-?in|sign-?in|auth|account/login)\b", re.I)
_PASSWORD = re.compile(rb"<input[^>]+type=[\"']?password", re.I)
_CAPTCHA = re.compile(
    rb"g-recaptcha|h-captcha|hcaptcha|cf-challenge|cf_chl_|captcha", re.I
)
_PAYWALL = re.compile(
    rb"paywall|subscribe to (continue|read)|subscribers only|assine para "
    rb"(ler|continuar)",
    re.I,
)

TRANSIENT = frozenset(
    {ReasonCode.TIMEOUT, ReasonCode.HTTP_5XX, ReasonCode.CONNECTION_ERROR}
)


@dataclass(frozen=True)
class UrlStatus:
    last_reason: str | None
    last_reason_class: str | None
    transient_attempts: int
    last_checked_at: datetime
    last_size_bytes: int | None
    last_etag: str | None


def read_url_status(conn: psycopg.Connection, normalised_url: str) -> UrlStatus | None:
    row = conn.execute(
        "SELECT last_reason, last_reason_class, transient_attempts, last_checked_at, "
        "last_size_bytes, last_etag FROM crawler_url_status WHERE normalised_url = %s",
        (normalised_url,),
    ).fetchone()
    return None if row is None else UrlStatus(*row)


class RobotsCache:
    """robots.txt per origin, read through ``netsafe`` and the host slots."""

    def __init__(self) -> None:
        self._rules: dict[str, RobotFileParser | bool] = {}

    async def allowed(self, safe: SafeClient, url: str) -> bool:
        """False when robots.txt disallows ``url``; ``Refused`` if it cannot be read."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        rules = self._rules.get(origin)
        if rules is None:
            rules = await self._read(safe, origin)
            self._rules[origin] = rules
        if isinstance(rules, bool):
            return rules
        return rules.can_fetch(USER_AGENT, url)

    def checker(self, safe: SafeClient) -> Callable[[str], Awaitable[bool]]:
        """The per-hop ``allow`` hook for ``SafeClient.get``."""

        async def allow(url: str) -> bool:
            return await self.allowed(safe, url)

        return allow

    @staticmethod
    async def _read(safe: SafeClient, origin: str) -> RobotFileParser | bool:
        response = await safe.get(
            f"{origin}/robots.txt",
            max_bytes=ROBOTS_MAX_BYTES,
            timeout_s=PROBE_TIMEOUT_S,
        )
        if response.status in {401, 403}:
            return False  # as urllib.robotparser: access to robots.txt denied
        if 400 <= response.status < 500:
            return True  # no robots.txt
        if response.status >= 500:
            raise Refused(ReasonCode.HTTP_5XX, "robots.txt")
        parser = RobotFileParser()
        parser.parse(response.body.decode("utf-8", errors="replace").splitlines())
        return parser


def refusal(
    status: UrlStatus | None, now: datetime, refresh: bool
) -> ToolErrorCode | None:
    """FR-016a: skip a permanent failure, and a transient one until it is due."""
    if status is None or refresh or status.last_reason is None:
        return None
    if status.last_reason_class == "permanent":
        return ToolErrorCode.SKIPPED_PERMANENT_FAILURE
    due = status.last_checked_at + timedelta(days=RECHECK_AFTER_DAYS)
    if status.transient_attempts >= MAX_TRANSIENT_ATTEMPTS or now < due:
        return ToolErrorCode.SKIPPED_RECHECK_WAIT
    return None


def _signals(response: SafeResponse) -> ReasonCode | None:
    body = response.body
    if response.status == 402 or _PAYWALL.search(body):
        return ReasonCode.PAYWALL
    if _CAPTCHA.search(body) and response.status in {200, 403, 429}:
        return ReasonCode.CAPTCHA
    if response.status == 401 or _LOGIN_PATH.search(urlsplit(response.final_url).path):
        return ReasonCode.LOGIN_WALL
    if _PASSWORD.search(body):
        return ReasonCode.LOGIN_WALL
    return None


def _failed(
    url: str,
    reason: ReasonCode,
    *,
    robots_allowed: bool,
    latency_s: float = 0.0,
    status: int | None = None,
    final_url: str | None = None,
    hops: int = 0,
) -> ProbeResult:
    return ProbeResult(
        url=url,
        final_url=final_url or url,
        status=status,
        hops=hops,
        robots_allowed=robots_allowed,
        login_signal=reason is ReasonCode.LOGIN_WALL,
        paywall_signal=reason is ReasonCode.PAYWALL,
        captcha_signal=reason is ReasonCode.CAPTCHA,
        latency_s=latency_s,
        reachable=CheckResult.FAILED,
        reason=reason,
    )


async def _get_with_retries(
    safe: SafeClient, url: str, robots: RobotsCache
) -> SafeResponse:
    last: Refused | None = None
    for _attempt in range(PROBE_RETRIES + 1):
        try:
            response = await safe.get(
                url,
                max_bytes=PROBE_MAX_BYTES,
                timeout_s=PROBE_TIMEOUT_S,
                allow=robots.checker(safe),
            )
        except Refused as refused:
            if refused.reason not in TRANSIENT:
                raise
            last = refused
            continue
        if response.status >= 500:
            last = Refused(ReasonCode.HTTP_5XX, str(response.status))
            continue
        return response
    raise last if last is not None else Refused(ReasonCode.CONNECTION_ERROR)


async def probe_url(
    args: UrlInput, state: RunState, services: Services, robots: RobotsCache
) -> ProbeResult | ToolError:
    url = args.url
    norm = normalise_url(url)
    host = domain_of(url)
    if host in state.dead_hosts and not state.config.refresh:
        state.skips.append(f"dead_host:{host}")
        return ToolError(error=ToolErrorCode.SKIPPED_DEAD_HOST, retryable=False)
    status = await services.db.call(lambda conn: read_url_status(conn, norm))
    skip = refusal(status, state.now(), state.config.refresh)
    if skip is not None:
        state.skips.append(f"{skip}:{norm}")
        return ToolError(error=skip, retryable=False)
    state.started.add(norm)
    state.found.setdefault(norm, TicketInput(url=url))
    try:
        allowed = await robots.allowed(services.safe, url)
    except Refused as refused:
        result = _failed(url, refused.reason, robots_allowed=False)
        state.probes[norm] = result
        return result
    if not allowed:
        result = _failed(url, ReasonCode.BLOCKED, robots_allowed=False)
        state.probes[norm] = result
        return result
    try:
        response = await _get_with_retries(services.safe, url, robots)
    except Refused as refused:
        result = _failed(
            url, refused.reason, robots_allowed=refused.reason is not ReasonCode.BLOCKED
        )
        state.probes[norm] = result
        return result
    reason = _signals(response)
    if reason is None and 400 <= response.status < 500:
        reason = ReasonCode.HTTP_4XX
    if reason is not None:
        result = _failed(
            url,
            reason,
            robots_allowed=True,
            latency_s=response.latency_s,
            status=response.status,
            final_url=response.final_url,
            hops=response.hops,
        )
        state.probes[norm] = result
        return result
    length = response.headers.get("content-length")
    size = int(length) if length and length.isdigit() else None
    etag = response.headers.get("etag")
    unchanged = (
        not state.config.refresh
        and status is not None
        and status.last_reason is None
        and size is not None
        and etag is not None
        and status.last_size_bytes == size
        and status.last_etag == etag
    )
    result = ProbeResult(
        url=url,
        final_url=response.final_url,
        status=response.status,
        content_type=(response.headers.get("content-type") or "")[:100] or None,
        size_bytes=size,
        etag=etag[:200] if etag else None,
        hops=response.hops,
        robots_allowed=True,
        latency_s=response.latency_s,
        reachable=CheckResult.PASSED,
        unchanged=unchanged,
    )
    state.probes[norm] = result
    return result
