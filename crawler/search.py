"""``web_search``: Tavily and Brave over plain ``httpx``, with failover (FR-007a, R3).

Each query sent counts one toward the run's 500-query budget, whichever provider takes
it. When the provider in use is unavailable or out of quota, it is marked down for the
rest of the run, the switch is recorded, and the query goes to the other provider. Only
URLs and titles come back: snippets are dropped, so page text never reaches the agent
model (R12).
"""

from __future__ import annotations

import asyncio

import httpx

from crawler.config import SEARCH_MAX_RESULTS, SEARCH_RETRIES, SEARCH_TIMEOUT_S
from crawler.contracts import (
    TITLE_MAX,
    Provider,
    SearchHit,
    SearchResult,
    TicketInput,
    ToolError,
    ToolErrorCode,
    WebSearchInput,
    clip,
    normalise_url,
)
from crawler.memory import query_key
from crawler.runstate import ProviderSwitch, RunState, Services

TAVILY_URL = "https://api.tavily.com/search"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
QUOTA_CODES = frozenset({402, 429, 432, 433})

DESCRIPTION = (
    "Search the web for pages or PDFs that may contain exam-style multiple-choice "
    "questions. Returns ranked URLs and titles only."
)


class ProviderDown(Exception):
    """The provider failed in a way that switching to the other one may fix."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _other(provider: Provider) -> Provider:
    return Provider.BRAVE if provider is Provider.TAVILY else Provider.TAVILY


async def _call(
    client: httpx.AsyncClient,
    provider: Provider,
    key: str,
    query: str,
    lang: str | None,
) -> list[tuple[str, str]]:
    if provider is Provider.TAVILY:
        response = await client.post(
            TAVILY_URL,
            json={"query": query, "max_results": SEARCH_MAX_RESULTS},
            headers={"Authorization": f"Bearer {key}"},
            timeout=SEARCH_TIMEOUT_S,
        )
    else:
        params = {"q": query, "count": str(SEARCH_MAX_RESULTS)}
        if lang:
            params["search_lang"] = lang.split("-", 1)[0]
        response = await client.get(
            BRAVE_URL,
            params=params,
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
            timeout=SEARCH_TIMEOUT_S,
        )
    if response.status_code in QUOTA_CODES:
        raise ProviderDown(f"quota_{response.status_code}")
    if response.status_code >= 500:
        raise ProviderDown(f"http_{response.status_code}")
    if response.status_code != 200:
        raise ProviderDown(f"http_{response.status_code}")
    try:
        data = response.json()
    except ValueError:
        raise ProviderDown("bad_json") from None
    rows = (
        data.get("results")
        if provider is Provider.TAVILY
        else ((data.get("web") or {}).get("results"))
    )
    hits: list[tuple[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("url"), str):
            hits.append((row["url"], str(row.get("title") or "")))
    return hits[:SEARCH_MAX_RESULTS]


async def _with_retries(
    client: httpx.AsyncClient,
    provider: Provider,
    key: str,
    query: str,
    lang: str | None,
) -> list[tuple[str, str]]:
    """Retry timeouts and 5xx on one provider; quota errors switch at once."""
    last = "unavailable"
    for attempt in range(SEARCH_RETRIES + 1):
        try:
            return await _call(client, provider, key, query, lang)
        except httpx.TimeoutException:
            last = "timeout"
        except httpx.TransportError:
            last = "connection_error"
        except ProviderDown as down:
            if down.reason.startswith("quota") or not down.reason.startswith("http_5"):
                raise
            last = down.reason
        if attempt < SEARCH_RETRIES:
            await asyncio.sleep(0)
    raise ProviderDown(last)


async def web_search(
    args: WebSearchInput, state: RunState, services: Services
) -> SearchResult | ToolError:
    key = query_key(args.query)
    if key in state.queries_tried and not state.config.refresh:
        state.skips.append(f"query:{key}")
        return ToolError(error=ToolErrorCode.SKIPPED_KNOWN_QUERY, retryable=False)
    order: list[Provider] = []
    preferred = args.provider or state.provider or state.config.primary_provider
    for candidate in (preferred, _other(preferred)):
        if candidate not in state.providers_down and candidate in services.search_keys:
            order.append(candidate)
    if not order:
        return ToolError(error=ToolErrorCode.PROVIDERS_UNAVAILABLE, retryable=False)
    for provider in order:
        if not await state.reserve_query():
            return ToolError(
                error=ToolErrorCode.QUERY_BUDGET_EXHAUSTED, retryable=False
            )
        try:
            rows = await _with_retries(
                services.search_client,
                provider,
                services.search_keys[provider],
                args.query,
                args.lang,
            )
        except ProviderDown as down:
            state.providers_down.add(provider)
            other = _other(provider)
            if other not in state.providers_down and other in services.search_keys:
                state.provider_switches.append(
                    ProviderSwitch(
                        at=state.now().isoformat(),
                        from_=provider,
                        to=other,
                        reason=down.reason,
                    )
                )
                state.provider = other
            continue
        state.queries_tried.add(key)
        state.task_queries.append(args.query)
        hits = []
        for rank, (url, title) in enumerate(rows, start=1):
            hits.append(
                SearchHit(
                    url=url,
                    title=clip(title, TITLE_MAX),
                    rank=rank,
                    provider=provider,
                    query=args.query[:200],
                )
            )
            state.found.setdefault(
                normalise_url(url),
                TicketInput(url=url, query=args.query[:200], provider=provider),
            )
        return SearchResult(
            hits=tuple(hits), provider=provider, queries_used=state.queries_used
        )
    return ToolError(error=ToolErrorCode.PROVIDERS_UNAVAILABLE, retryable=False)
