"""``web_search``: providers, limits, failover and the shared query budget (FR-007a)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from crawler_fakes import FakeResolver, VirtualClock, make_services, make_state

from crawler.contracts import (
    Provider,
    SearchResult,
    ToolError,
    ToolErrorCode,
    WebSearchInput,
)
from crawler.runstate import RunState
from crawler.search import web_search


class Api:
    """Both search APIs, answering from a per-provider script of statuses."""

    def __init__(self, script: dict[str, list[int | Exception]] | None = None) -> None:
        self.script = script or {}
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        provider = "tavily" if "tavily" in request.url.host else "brave"
        self.calls.append(provider)
        queue = self.script.get(provider, [])
        step = queue.pop(0) if queue else 200
        if isinstance(step, Exception):
            raise step
        if step != 200:
            return httpx.Response(step, json={})
        if provider == "tavily":
            assert request.headers["authorization"] == "Bearer t"
            query = json.loads(request.content)["query"]
            rows = [
                {"url": f"https://a.test/{n}", "title": "T" * 300, "content": query}
                for n in range(25)
            ]
            return httpx.Response(200, json={"results": rows})
        assert request.headers["x-subscription-token"] == "b"
        assert request.url.params["search_lang"] == "pt"
        rows = [{"url": "https://b.test/1", "title": "B", "description": "snippet"}]
        return httpx.Response(200, json={"web": {"results": rows}})


def run(
    api: Api,
    state: RunState,
    query: str = "exam pdf",
    lang: str | None = "pt-BR",
    keys: dict[Provider, str] | None = None,
) -> SearchResult | ToolError:
    async def go() -> SearchResult | ToolError:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(api.handler)
        ) as client:
            services = make_services(client, FakeResolver(), VirtualClock(), keys=keys)
            return await web_search(
                WebSearchInput(query=query, lang=lang), state, services
            )

    return asyncio.run(go())


def test_results_are_capped_and_snippets_dropped() -> None:
    state = make_state()
    result = run(Api(), state)
    assert isinstance(result, SearchResult)
    assert len(result.hits) == 20
    assert all(len(hit.title) <= 120 for hit in result.hits)
    assert "content" not in result.model_dump_json()
    assert state.queries_used == 1 and len(state.found) == 20


def test_timeouts_and_server_errors_are_retried_twice() -> None:
    api = Api({"tavily": [503, httpx.ReadTimeout("slow")]})
    state = make_state()
    assert isinstance(run(api, state), SearchResult)
    assert api.calls == ["tavily", "tavily", "tavily"]
    assert state.queries_used == 1


def test_quota_switches_provider_for_the_rest_of_the_run() -> None:
    api = Api({"tavily": [429]})
    state = make_state()
    result = run(api, state)
    assert isinstance(result, SearchResult) and result.provider is Provider.BRAVE
    assert [s.as_json()["to"] for s in state.provider_switches] == ["brave"]
    assert state.queries_used == 2  # both sends count toward the one budget
    run(api, state, query="another query")
    assert api.calls == ["tavily", "brave", "brave"]


def test_both_providers_down_is_an_error() -> None:
    api = Api({"tavily": [500, 500, 500], "brave": [500, 500, 500]})
    result = run(api, make_state())
    assert isinstance(result, ToolError)
    assert result.error is ToolErrorCode.PROVIDERS_UNAVAILABLE


def test_the_query_budget_is_shared_and_enforced() -> None:
    state = make_state(budget={"max_queries": 1})
    assert isinstance(run(Api(), state), SearchResult)
    refused = run(Api(), state, query="second")
    assert isinstance(refused, ToolError)
    assert refused.error is ToolErrorCode.QUERY_BUDGET_EXHAUSTED
    assert state.stop_limit is not None


@pytest.mark.parametrize("refresh", [False, True])
def test_a_known_query_is_skipped_unless_refresh(refresh: bool) -> None:
    state = make_state(refresh=refresh)
    state.queries_tried.add("exam pdf")
    result = run(Api(), state, query="Exam  PDF")
    assert isinstance(result, ToolError) is (not refresh)


def test_a_missing_key_leaves_only_the_other_provider() -> None:
    result = run(Api(), make_state(), keys={Provider.BRAVE: "b"})
    assert isinstance(result, SearchResult) and result.provider is Provider.BRAVE
    assert isinstance(run(Api(), make_state(), keys={}), ToolError)
