"""Tests for the network layer: backoff, conditional GETs, and link extraction.

``Fetcher`` runs against an in-memory website (``httpx.MockTransport``) and a
virtual clock, so no test touches the network or waits (FR-027). The stage
tests in ``test_fetch.py`` cover how these results reach the ledger.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Sequence
from itertools import pairwise
from typing import TYPE_CHECKING

import httpx
import pytest

from pipeline.store import PostgresHostLedger, load_host_requests
from pipeline.web import Fetched, Fetcher, extract_links, new_client, tls_context

if TYPE_CHECKING:
    import psycopg
    from conftest import FakeClock, Web

HOST = "kice.exams.test"
URL = f"https://{HOST}/p.pdf"
OTHER = f"https://{HOST}/q.pdf"
ROBOTS = f"https://{HOST}/robots.txt"
ALLOWED = frozenset({HOST})
INTERVAL = 5.0


def get_all(
    web: Web, clock: FakeClock, urls: Sequence[str], **validators: str
) -> list[Fetched]:
    """GET each URL with one Fetcher, concurrently, as one command run would."""

    async def go() -> list[Fetched]:
        async with web.client() as client:
            fetcher = Fetcher(client, clock, lambda _host: INTERVAL)
            return list(
                await asyncio.gather(
                    *(fetcher.get(u, ALLOWED, **validators) for u in urls)
                )
            )

    return asyncio.run(go())


def get(web: Web, clock: FakeClock, url: str = URL, **validators: str) -> Fetched:
    return get_all(web, clock, [url], **validators)[0]


class TestSpacingAcrossInvocations:
    """SC-006: the per-host interval holds between two commands, not just within one.

    Each ``Fetcher`` below is one command run. They share a schema, the virtual
    monotonic clock, and a wall clock derived from it, so nothing waits for real.
    """

    EPOCH = 1_700_000_000.0  # wall time at virtual time zero

    def wall(self, clock: FakeClock) -> float:
        return self.EPOCH + clock.now()

    def run_command(
        self, web: Web, clock: FakeClock, db: psycopg.Connection, url: str = URL
    ) -> Fetched:
        """One command: a new Fetcher with its own ledger, which reads the table."""

        async def go() -> Fetched:
            async with web.client() as client:
                fetcher = Fetcher(
                    client,
                    clock,
                    lambda _host: INTERVAL,
                    PostgresHostLedger(db, lambda: self.wall(clock)),
                )
                return await fetcher.get(url, ALLOWED)

        return asyncio.run(go())

    def test_the_next_command_waits_out_the_rest_of_the_interval(
        self, web: Web, clock: FakeClock, db: psycopg.Connection
    ) -> None:
        self.run_command(web, clock, db)
        last = web.calls[-1][1]
        clock.advance(2)
        slept = len(clock.sleeps)
        assert self.run_command(web, clock, db, OTHER).outcome == "http_error"
        # The first request of the second command is robots.txt.
        assert web.calls[len(web.calls) - 2] == (ROBOTS, last + INTERVAL)
        assert clock.sleeps[slept] == INTERVAL - 2

    def test_a_host_used_longer_ago_than_the_interval_is_not_delayed(
        self, web: Web, clock: FakeClock, db: psycopg.Connection
    ) -> None:
        self.run_command(web, clock, db)
        clock.advance(INTERVAL + 1)
        slept = len(clock.sleeps)
        self.run_command(web, clock, db, OTHER)
        # Only the in-process wait between robots.txt and the page remains.
        assert clock.sleeps[slept:] == [INTERVAL]

    def test_a_last_request_in_the_future_counts_as_now(
        self, web: Web, clock: FakeClock, db: psycopg.Connection
    ) -> None:
        db.execute(
            "INSERT INTO host_requests VALUES (%s, to_timestamp(%s))",
            (HOST, self.wall(clock) + 3600),
        )
        self.run_command(web, clock, db)
        # One interval before robots.txt (not an hour), then the in-process one.
        assert clock.sleeps == [INTERVAL, INTERVAL]

    def test_another_host_is_not_delayed(
        self, web: Web, clock: FakeClock, db: psycopg.Connection
    ) -> None:
        db.execute(
            "INSERT INTO host_requests VALUES (%s, to_timestamp(%s))",
            ("other.exams.test", self.wall(clock)),
        )
        self.run_command(web, clock, db)
        # Only the in-process wait between robots.txt and the page.
        assert clock.sleeps == [INTERVAL]

    def test_robots_txt_requests_are_recorded(
        self, web: Web, clock: FakeClock, db: psycopg.Connection
    ) -> None:
        web.add(ROBOTS, "User-agent: *\nDisallow: /", headers={})
        assert self.run_command(web, clock, db).outcome == "refused_robots"
        assert web.urls() == [ROBOTS]
        assert load_host_requests(db)[HOST] == pytest.approx(self.wall(clock))

    def test_retried_requests_are_recorded(
        self, web: Web, clock: FakeClock, db: psycopg.Connection
    ) -> None:
        web.add_sequence(URL, (503, {}, b""), (200, {}, b"ok"))
        assert self.run_command(web, clock, db).outcome == "fetched"
        assert len(web.times(URL)) == 2
        assert load_host_requests(db)[HOST] == pytest.approx(
            self.EPOCH + web.times(URL)[-1]
        )

    def test_without_a_ledger_nothing_is_stored(
        self, web: Web, clock: FakeClock, db: psycopg.Connection
    ) -> None:
        get(web, clock)
        assert load_host_requests(db) == {}


class SpyLedger:
    """A ledger that notes how many requests had been sent at each ``reserve``."""

    def __init__(self, web: Web) -> None:
        self._web = web
        self.sent_at_reserve: list[int] = []

    def reserve(self, host: str, interval_s: float) -> float:
        self.sent_at_reserve.append(len(self._web.calls))
        return 0.0


class TestReservation:
    def test_the_slot_is_recorded_before_each_request_is_sent(
        self, web: Web, clock: FakeClock
    ) -> None:
        """A process killed mid-request must leave a time for the next command."""
        ledger = SpyLedger(web)

        async def go() -> Fetched:
            async with web.client() as client:
                fetcher = Fetcher(client, clock, lambda _host: INTERVAL, ledger)
                return await fetcher.get(URL, ALLOWED)

        asyncio.run(go())
        # Two requests (robots.txt, then the page), each reserved while still unsent.
        assert ledger.sent_at_reserve == [0, 1]


class TestBackoff:
    def test_a_429_waits_for_retry_after_then_succeeds(
        self, web: Web, clock: FakeClock
    ) -> None:
        web.add_sequence(URL, (429, {"retry-after": "30"}, b""), (200, {}, b"ok"))
        result = get(web, clock)
        assert (result.outcome, result.body) == ("fetched", b"ok")
        first, second = web.times(URL)
        assert second - first == 30

    def test_a_503_without_retry_after_backs_off_exponentially_then_gives_up(
        self, web: Web, clock: FakeClock
    ) -> None:
        web.add(URL, status=503)
        result = get(web, clock)
        assert (result.outcome, result.status) == ("http_error", 503)
        times = web.times(URL)
        assert [b - a for a, b in pairwise(times)] == [2 * INTERVAL, 4 * INTERVAL]

    def test_a_retry_after_longer_than_the_cap_gives_up_at_once(
        self, web: Web, clock: FakeClock
    ) -> None:
        web.add(URL, status=429, headers={"retry-after": "86400"})
        result = get(web, clock)
        assert (result.outcome, result.status) == ("http_error", 429)
        assert len(web.times(URL)) == 1
        assert max(clock.sleeps, default=0) <= INTERVAL

    def test_an_http_date_retry_after_is_measured_against_the_date_header(
        self, web: Web, clock: FakeClock
    ) -> None:
        busy = {
            "date": "Wed, 21 Oct 2026 07:28:00 GMT",
            "retry-after": "Wed, 21 Oct 2026 07:28:45 GMT",
        }
        web.add_sequence(URL, (503, busy, b""), (200, {}, b"ok"))
        assert get(web, clock).outcome == "fetched"
        first, second = web.times(URL)
        assert second - first == 45

    def test_retry_after_pauses_every_url_on_the_host(
        self, web: Web, clock: FakeClock
    ) -> None:
        web.add(ROBOTS, status=404)
        web.add_sequence(URL, (429, {"retry-after": "60"}, b""), (200, {}, b"p"))
        web.add(OTHER, b"q")
        results = get_all(web, clock, [URL, OTHER])
        assert [r.outcome for r in results] == ["fetched", "fetched"]
        busy_at = web.times(URL)[0]
        assert web.times(OTHER)[0] >= busy_at + 60

    def test_a_rate_limited_robots_txt_forbids_everything(
        self, web: Web, clock: FakeClock
    ) -> None:
        web.add(ROBOTS, status=429)
        web.add(URL, b"ok")
        assert get(web, clock).outcome == "refused_robots"
        assert URL not in web.urls()

    def test_a_timeout_is_retried(self, web: Web, clock: FakeClock) -> None:
        web.add_sequence(URL, httpx.ReadTimeout("slow"), (200, {}, b"ok"))
        assert get(web, clock).outcome == "fetched"
        assert len(web.times(URL)) == 2

    def test_repeated_timeouts_are_an_http_error(
        self, web: Web, clock: FakeClock
    ) -> None:
        web.add_sequence(URL, httpx.ConnectTimeout("down"))
        assert get(web, clock).outcome == "http_error"
        assert len(web.times(URL)) == 3

    def test_the_cli_client_sets_an_explicit_timeout_and_no_redirects(self) -> None:
        client = new_client()
        try:
            assert (client.timeout.read, client.timeout.connect) == (60.0, 10.0)
            assert client.follow_redirects is False
        finally:
            asyncio.run(client.aclose())


class TestConditionalGet:
    def test_validators_are_sent_and_a_304_is_not_modified(
        self, web: Web, clock: FakeClock
    ) -> None:
        web.add(URL, status=304, headers={"etag": '"v2"'})
        result = get(
            web,
            clock,
            etag='"v1"',
            last_modified="Wed, 21 Oct 2026 07:28:00 GMT",
        )
        assert (result.outcome, result.etag) == ("not_modified", '"v2"')
        sent = [r for r in web.requests if str(r.url) == URL][-1].headers
        assert sent["if-none-match"] == '"v1"'
        assert sent["if-modified-since"] == "Wed, 21 Oct 2026 07:28:00 GMT"

    def test_a_plain_get_sends_no_validators(self, web: Web, clock: FakeClock) -> None:
        web.add(URL, b"ok", headers={"etag": '"v1"', "last-modified": "x"})
        result = get(web, clock)
        assert (result.etag, result.last_modified) == ('"v1"', "x")
        sent = [r for r in web.requests if str(r.url) == URL][-1].headers
        assert "if-none-match" not in sent
        assert "if-modified-since" not in sent

    def test_a_304_to_an_unconditional_request_is_an_error(
        self, web: Web, clock: FakeClock
    ) -> None:
        web.add(URL, status=304)
        assert get(web, clock).outcome == "http_error"


class TestLinkExtraction:
    PAGE = "https://kice.exams.test/archive/index.html"

    @pytest.mark.parametrize(
        ("label", "codec", "name", "header"),
        [
            # Each name holds a character that only the superset codec has, so a
            # decoder that trusted the narrow label would garble it.
            ("euc-kr", "cp949", "2024_갂.pdf", None),
            ("gb2312", "gb18030", "数学_镕.pdf", "gb2312"),
            ("shift_jis", "cp932", "数学①.pdf", None),
        ],
        ids=["korean-meta", "chinese-header", "japanese-meta"],
    )
    def test_legacy_encodings_decode_like_a_browser(
        self, label: str, codec: str, name: str, header: str | None
    ) -> None:
        meta = "" if header else f'<meta charset="{label}">'
        page = f'<html><head>{meta}</head><body><a href="/files/{name}">x</a>'
        links = extract_links(self.PAGE, page.encode(codec), "text/html", header)
        assert links == [f"https://kice.exams.test/files/{name}"]

    def test_utf8_without_any_declaration_still_works(self) -> None:
        page = '<a href="/files/정답.pdf">정답</a>'.encode()
        assert extract_links(self.PAGE, page, "text/html") == [
            "https://kice.exams.test/files/정답.pdf"
        ]

    def test_base_href_changes_how_relative_links_resolve(self) -> None:
        page = b'<base href="https://kice.exams.test/docs/"><a href="p.pdf">p</a>'
        assert extract_links(self.PAGE, page, "text/html") == [
            "https://kice.exams.test/docs/p.pdf"
        ]

    def test_area_iframe_and_frame_links_are_collected_in_page_order(self) -> None:
        page = (
            b'<iframe src="/viewer/a.pdf"></iframe>'
            b'<map><area href="/maps/b.pdf"></map>'
            b'<frameset><frame src="/frames/c.html"></frameset>'
            b'<a href=" /d.pdf ">d</a>'
        )
        assert extract_links(self.PAGE, page, "text/html") == [
            "https://kice.exams.test/viewer/a.pdf",
            "https://kice.exams.test/maps/b.pdf",
            "https://kice.exams.test/frames/c.html",
            "https://kice.exams.test/d.pdf",
        ]

    def test_malformed_html_still_yields_its_links(self) -> None:
        page = b'<p><a href="/a.pdf">a<div><a href=/b.pdf>b<table><a href="c.pdf">'
        assert extract_links(self.PAGE, page, "text/html") == [
            "https://kice.exams.test/a.pdf",
            "https://kice.exams.test/b.pdf",
            "https://kice.exams.test/archive/c.pdf",
        ]


class TestTlsContext:
    """Missing intermediates are supplied, without becoming trust anchors."""

    def test_the_committed_intermediates_are_loaded(self) -> None:
        names = {
            value
            for cert in tls_context().get_ca_certs()
            for rdn in cert["subject"]
            for key, value in rdn
            if key == "commonName"
        }
        assert "RNP ICPEdu GR46 OV TLS CA 2025" in names

    def test_a_chain_must_still_end_at_a_root(self) -> None:
        context = tls_context()
        assert not context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname
