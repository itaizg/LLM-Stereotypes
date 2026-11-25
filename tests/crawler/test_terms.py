"""``read_terms``: restrictive terms are flagged, never failed (FR-019)."""

from __future__ import annotations

import asyncio

import pytest
from crawler_fakes import (
    FakeResolver,
    ReplayWeb,
    VirtualClock,
    make_services,
    make_state,
)

from crawler.contracts import TermsInput, TermsResult, ToolError
from crawler.probe import RobotsCache
from crawler.runstate import RunState
from crawler.terms import read_terms, restrictive_excerpt

HOME = "https://site.example/"


def terms(web: ReplayWeb, state: RunState) -> TermsResult | ToolError:
    async def go() -> TermsResult | ToolError:
        async with web.client() as client:
            services = make_services(client, FakeResolver(), VirtualClock())
            return await read_terms(
                TermsInput(domain="Site.Example"), state, services, RobotsCache()
            )

    return asyncio.run(go())


def test_a_linked_restrictive_page_is_flagged_with_an_excerpt(web: ReplayWeb) -> None:
    web.add(HOME, '<a href="/legal/terms">Terms of Use</a>')
    web.add(
        "https://site.example/legal/terms",
        "<p>" + "Welcome. " * 50 + "Copying for research is prohibited without "
        "prior written consent.</p>",
    )
    state = make_state()
    result = terms(web, state)
    assert isinstance(result, TermsResult)
    assert result.restrictive and result.terms_url == "https://site.example/legal/terms"
    assert "prohibited" in result.excerpt and len(result.excerpt) <= 300
    # Cached per domain: a second call sends nothing.
    sent = len(web.log)
    assert terms(web, state) == result and len(web.log) == sent


def test_a_fallback_path_is_tried(web: ReplayWeb) -> None:
    web.add("https://site.example/terms", "<p>You may use this content freely.</p>")
    result = terms(web, make_state())
    assert isinstance(result, TermsResult) and not result.restrictive
    assert result.terms_url == "https://site.example/terms"


def test_no_terms_found_is_not_a_failure(web: ReplayWeb) -> None:
    result = terms(web, make_state())
    assert isinstance(result, TermsResult)
    assert (result.terms_url, result.restrictive) == (None, False)


@pytest.mark.parametrize(
    ("text", "restrictive"),
    [
        ("Automated scraping of this site is not allowed.", True),
        ("You may not reproduce any material without permission.", True),
        ("É proibida a reprodução sem autorização.", True),
        ("무단 복제를 금지합니다.", True),
        ("All papers are free to download and share.", False),
    ],
)
def test_restrictive_wording(text: str, restrictive: bool) -> None:
    assert restrictive_excerpt(text)[0] is restrictive
