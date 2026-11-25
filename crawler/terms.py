"""``read_terms``: find a site's terms of use and flag restrictive ones (FR-019).

A source is never failed for its terms. A restrictive page (scraping, copying or
research use forbidden without consent) adds the ``restrictive_terms`` flag and a short
excerpt for the reviewer, who decides. Results are cached per domain per run.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from crawler.config import TERMS_MAX_BYTES, TERMS_RETRIES, TERMS_TIMEOUT_S
from crawler.contracts import EXCERPT_MAX, TermsInput, TermsResult, ToolError, clip
from crawler.lexicon import normalise
from crawler.netsafe import Refused, SafeResponse
from crawler.probe import RobotsCache
from crawler.runstate import RunState, Services
from pipeline.web import decode_html

DESCRIPTION = (
    "Find the site's terms of use or licence page and say whether it restricts "
    "scraping, copying or research use."
)

_TERMS_LINK = re.compile(
    r"terms|conditions|legal|licen[cs]e|copyright|termos|condições|direitos autorais|"
    r"이용약관|저작권|利用規約|著作権|使用条款|服务条款|版权|תנאי שימוש|זכויות יוצרים",
    re.I,
)
FALLBACK_PATHS = ("/terms", "/terms-of-use", "/legal", "/termos-de-uso")
_RESTRICTIVE = re.compile(
    r"scrap|crawl|spider|automated (?:means|access|tools)|data[ -]?mining|"
    r"(?:may not|must not|prohibited|not permitted|forbidden|without (?:the )?(?:prior "
    r")?"
    r"(?:written )?(?:consent|permission)).{0,80}(?:cop|reproduc|redistribut|research|"
    r"download)|"
    r"(?:cop|reproduc|research|download).{0,80}(?:prohibited|not permitted|forbidden|"
    r"without (?:the )?(?:prior )?(?:written )?(?:consent|permission))|"
    r"proibid|sem autorização|금지|무단|禁止|未经许可|אסור|ללא אישור",
    re.I | re.S,
)


def restrictive_excerpt(text: str) -> tuple[bool, str]:
    flat = " ".join(text.split())
    match = _RESTRICTIVE.search(flat)
    if match is None:
        return False, clip(flat, EXCERPT_MAX)
    start = max(0, match.start() - 100)
    return True, clip(flat[start : start + EXCERPT_MAX], EXCERPT_MAX)


def _terms_links(response: SafeResponse) -> list[str]:
    soup = BeautifulSoup(decode_html(response.body), "html.parser")
    found: list[str] = []
    for anchor in soup.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        href = anchor.get("href")
        label = f"{anchor.get_text(' ', strip=True)} {href}"
        if isinstance(href, str) and _TERMS_LINK.search(normalise(label)):
            absolute = urljoin(response.final_url, href)
            if urlsplit(absolute).hostname == urlsplit(response.final_url).hostname:
                found.append(absolute)
    return found


async def _get(
    services: Services, url: str, robots: RobotsCache
) -> SafeResponse | None:
    for _attempt in range(TERMS_RETRIES + 1):
        try:
            response = await services.safe.get(
                url,
                max_bytes=TERMS_MAX_BYTES,
                timeout_s=TERMS_TIMEOUT_S,
                allow=robots.checker(services.safe),
            )
        except Refused:
            continue
        if response.status < 500:
            return response if response.status == 200 else None
    return None


async def read_terms(
    args: TermsInput, state: RunState, services: Services, robots: RobotsCache
) -> TermsResult | ToolError:
    domain = args.domain.lower()
    cached = state.terms.get(domain)
    if cached is not None:
        return cached
    home = await _get(services, f"https://{domain}/", robots)
    candidates = _terms_links(home) if home is not None else []
    candidates += [f"https://{domain}{path}" for path in FALLBACK_PATHS]
    result = TermsResult(domain=domain, terms_url=None)
    for url in candidates[:4]:
        page = await _get(services, url, robots)
        if page is None:
            continue
        soup = BeautifulSoup(decode_html(page.body), "html.parser")
        for node in soup(["script", "style", "noscript"]):
            node.decompose()
        restrictive, excerpt = restrictive_excerpt(soup.get_text(" "))
        result = TermsResult(
            domain=domain,
            terms_url=page.final_url,
            excerpt=excerpt,
            restrictive=restrictive,
        )
        break
    state.terms[domain] = result
    return result
