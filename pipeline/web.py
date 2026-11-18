"""Talking to official websites politely: the network rules and link extraction.

Split from ``acquire.py`` (the 800-line limit, CDR-2026-022) along a real seam:
this module changes when HTTP etiquette changes (robots.txt, rate limits,
backoff, conditional requests, decoding), the stages there when acquisition
rules change. Nothing here touches the ledger, the blob store, or Postgres.

Network rules (FR-008, research R6): only allowlisted hosts, robots.txt
honoured, never faster than the per-host interval, back off when the server
says it is busy, and never follow a redirect off the allowlist.
"""

from __future__ import annotations

import asyncio
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup, Tag, UnicodeDammit
from bs4.dammit import EncodingDetector

__all__ = [
    "USER_AGENT",
    "Clock",
    "Fetched",
    "Fetcher",
    "SystemClock",
    "decode_html",
    "extract_links",
    "new_client",
    "retry_after_seconds",
]

# No personal contact details: the repository URL identifies the project.
USER_AGENT = "LLM-Stereotypes-dataset/1.0 (+https://github.com/itaizg/LLM-Stereotypes)"
MAX_REDIRECTS = 5
# Retries after a 429, a 503, or a transport error (timeout, refused connection).
MAX_RETRIES = 2
# A server asking for a longer pause than this gets an http_error now, not a
# command that silently sleeps for hours.
MAX_RETRY_AFTER = 300.0
# httpx's default is 5 s for every phase, too short for a large PDF from a slow
# government server.
REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_RETRY_CODES = frozenset({429, 503})


# --- polite fetching -----------------------------------------------------------


class Clock(Protocol):
    """Time, injected so rate limiting is testable without waiting."""

    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real clock, used by the CLI; tests inject a virtual one instead."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True)
class Fetched:
    """The result of one policy-checked GET."""

    outcome: str
    status: int | None = None
    body: bytes = b""
    media_type: str | None = None
    charset: str | None = None
    etag: str | None = None
    last_modified: str | None = None


def new_client() -> httpx.AsyncClient:
    """The CLI's HTTP client: redirects are followed by ``Fetcher``, not httpx."""
    return httpx.AsyncClient(follow_redirects=False, timeout=REQUEST_TIMEOUT)


def retry_after_seconds(response: httpx.Response) -> float | None:
    """The pause a 429/503 asks for, or None if it names none we can use.

    An HTTP-date is measured against the response's own ``Date`` header, so the
    result never depends on the local wall clock (and tests stay deterministic).
    """
    value = response.headers.get("retry-after", "").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        delta = parsedate_to_datetime(value) - parsedate_to_datetime(
            response.headers["date"]
        )
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    return max(0.0, delta.total_seconds())


class Fetcher:
    """GET with the allowlist, robots.txt, redirect, and rate-limit rules applied.

    One instance per command run, so robots.txt is fetched once per domain.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        clock: Clock,
        interval: Callable[[str], float],
    ) -> None:
        self._client = client
        self._clock = clock
        self._interval = interval
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        # Set by Retry-After or backoff: no request to the host before this time.
        self._not_before: dict[str, float] = {}
        # True/False mean allow-all/disallow-all (no or unusable robots.txt).
        self._robots: dict[str, RobotFileParser | bool] = {}

    async def _throttled_get(
        self, url: str, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        host = (urlsplit(url).hostname or "").lower()
        async with self._locks.setdefault(host, asyncio.Lock()):
            ready = self._not_before.get(host, float("-inf"))
            last = self._last.get(host)
            if last is not None:
                ready = max(ready, last + self._interval(host))
            wait = ready - self._clock.now()
            if wait > 0:
                await self._clock.sleep(wait)
            try:
                return await self._client.get(
                    url, headers={"User-Agent": USER_AGENT, **(headers or {})}
                )
            finally:
                self._last[host] = self._clock.now()

    async def _polite_get(
        self, url: str, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        """A throttled GET that backs off and retries when the server is busy.

        A 429 or 503 waits for its ``Retry-After`` (or, without one, twice the
        rate-limit interval, doubling per attempt), and the pause applies to the
        whole host, not only this URL. After ``MAX_RETRIES`` the last response is
        returned (or the transport error raised) for the caller to record.
        """
        host = (urlsplit(url).hostname or "").lower()
        attempt = 0
        while True:
            backoff = self._interval(host) * 2 ** (attempt + 1)
            try:
                response = await self._throttled_get(url, headers)
            except httpx.TransportError:
                if attempt >= MAX_RETRIES:
                    raise
                delay = backoff
            else:
                if response.status_code not in _RETRY_CODES or attempt >= MAX_RETRIES:
                    return response
                asked = retry_after_seconds(response)
                if asked is not None and asked > MAX_RETRY_AFTER:
                    return response
                delay = backoff if asked is None else asked
            self._not_before[host] = max(
                self._not_before.get(host, float("-inf")), self._clock.now() + delay
            )
            attempt += 1

    async def _robots_allow(self, url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        rules = self._robots.get(host)
        if rules is None:
            rules = await self._read_robots(f"{parts.scheme}://{parts.netloc}")
            self._robots[host] = rules
        if isinstance(rules, bool):
            return rules
        return rules.can_fetch(USER_AGENT, url)

    async def _read_robots(self, origin: str) -> RobotFileParser | bool:
        """Read robots.txt as ``urllib`` does, but through the rate limiter.

        401/403, 429, or a server error forbids everything (be polite when
        unsure, RFC 9309 section 2.3.1); any other 4xx means there is no
        robots.txt, so nothing is forbidden.
        """
        try:
            response = await self._polite_get(f"{origin}/robots.txt")
        except httpx.HTTPError:
            return False
        code = response.status_code
        if code == 200:
            parser = RobotFileParser()
            parser.parse(response.text.splitlines())
            parser.modified()  # can_fetch refuses until the file counts as read
            return parser
        return not (code in {401, 403, 429} or code >= 500)

    async def get(
        self,
        url: str,
        allowed: frozenset[str],
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> Fetched:
        """Fetch ``url`` if the rules allow; otherwise return the refusal.

        ``etag`` and ``last_modified`` come from an earlier fetch whose bytes are
        still in the blob store. They make the request conditional, and a 304
        comes back as ``not_modified`` for the caller to resolve.
        """
        conditional: dict[str, str] = {}
        if etag:
            conditional["If-None-Match"] = etag
        if last_modified:
            conditional["If-Modified-Since"] = last_modified
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            host = (urlsplit(current).hostname or "").lower()
            if host not in allowed:
                # Off the allowlist from the start is a refused domain; arriving
                # there by redirect usually means a sign-in page.
                return Fetched("refused_domain" if current == url else "login_required")
            if not await self._robots_allow(current):
                return Fetched("refused_robots")
            try:
                response = await self._polite_get(current, conditional)
            except httpx.HTTPError:
                return Fetched("http_error")
            code = response.status_code
            location = response.headers.get("location")
            if code in _REDIRECT_CODES and location:
                current = urljoin(current, location)
                continue
            if code in {401, 403}:
                return Fetched("login_required", code)
            if code == 304 and conditional:
                return Fetched(
                    "not_modified",
                    code,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                )
            if 200 <= code < 300:
                kind = response.headers.get("content-type", "").split(";")[0]
                return Fetched(
                    "fetched",
                    code,
                    response.content,
                    kind.strip().lower() or "application/octet-stream",
                    response.charset_encoding,
                    response.headers.get("etag"),
                    response.headers.get("last-modified"),
                )
            return Fetched("http_error", code)
        return Fetched("http_error")


# --- link extraction -----------------------------------------------------------


# WHATWG Encoding Standard: browsers decode pages labelled with these legacy
# names using the superset, and real pages (KICE, Gaokao, DNC) rely on that.
# Python's narrow codecs would fail on, or mangle, the extra characters.
_ENCODING_ALIASES = {
    "euc-kr": "cp949",
    "ks-c-5601-1987": "cp949",
    "x-windows-949": "cp949",
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "x-gbk": "gb18030",
    "shift-jis": "cp932",
    "sjis": "cp932",
    "x-sjis": "cp932",
    "iso-8859-1": "cp1252",
    "latin1": "cp1252",
    "us-ascii": "cp1252",
    "ascii": "cp1252",
}
_LINK_ATTRIBUTES = {"a": "href", "area": "href", "iframe": "src", "frame": "src"}


def _whatwg(label: str) -> str:
    key = label.strip().lower().replace("_", "-")
    return _ENCODING_ALIASES.get(key, key)


def decode_html(body: bytes, header_charset: str | None = None) -> str:
    """Decode an HTML page the way a browser would.

    The HTTP ``Content-Type`` charset comes first, then the page's own
    ``<meta>`` declaration, then BeautifulSoup's ``UnicodeDammit`` guesses.
    Assuming UTF-8 would garble non-ASCII links on legacy-encoded sites.
    """
    declared = EncodingDetector.find_declared_encoding(body, is_html=True)
    labels = [_whatwg(c) for c in (header_charset, declared) if c]
    dammit = UnicodeDammit(body, known_definite_encodings=labels, is_html=True)
    return dammit.unicode_markup or body.decode("utf-8", errors="replace")


def extract_links(
    base_url: str, body: bytes, media_type: str | None, charset: str | None = None
) -> list[str]:
    """Absolute http(s) links in an HTML page or sitemap, fragments removed.

    HTML links come from ``<a>``, ``<area>``, ``<iframe>``, and ``<frame>``,
    resolved against ``<base href>`` when the page sets one. Links that a page
    builds with JavaScript are not seen; that would need a headless browser.
    """
    raw: list[str] = []
    if "xml" in (media_type or "") or base_url.endswith(".xml"):
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return []
        raw = [
            (node.text or "").strip()
            for node in root.iter()
            if node.tag.rsplit("}", 1)[-1] == "loc"
        ]
    else:
        soup = BeautifulSoup(decode_html(body, charset), "html.parser")
        base = soup.find("base", href=True)
        if isinstance(base, Tag):
            href = base.get("href")
            if isinstance(href, str) and href.strip():
                base_url = urljoin(base_url, href.strip())
        for tag in soup.find_all(list(_LINK_ATTRIBUTES)):
            if not isinstance(tag, Tag):
                continue
            value = tag.get(_LINK_ATTRIBUTES[tag.name])
            if isinstance(value, str) and value.strip():
                raw.append(value.strip())
    links: list[str] = []
    for href in raw:
        absolute, _ = urldefrag(urljoin(base_url, href))
        if urlsplit(absolute).scheme in {"http", "https"} and absolute not in links:
            links.append(absolute)
    return links
