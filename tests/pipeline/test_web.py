"""Tests for the network layer: backoff, conditional GETs, and link extraction.

``Fetcher`` runs against an in-memory website (``httpx.MockTransport``) and a
virtual clock, so no test touches the network or waits (FR-027). The stage
tests in ``test_fetch.py`` cover how these results reach the ledger.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from itertools import pairwise
from typing import TYPE_CHECKING

import httpx
import pytest

from pipeline.web import Fetched, Fetcher, extract_links, new_client

if TYPE_CHECKING:
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
