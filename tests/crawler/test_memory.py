"""Crawler memory over XERJ (FR-030, FR-031, research R9), against a fake XERJ."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx

from crawler.memory import (
    NAMESPACE,
    DeadHost,
    NullMemory,
    QueryTried,
    QueryYield,
    XerjMemory,
    open_memory,
)

NOW = datetime(2026, 11, 23, tzinfo=UTC)
TOKEN = "xerj-SECRET-1"


class FakeXerj:
    """Entries per type, paged two at a time; a status script can make it fail."""

    def __init__(self, statuses: list[int] | None = None) -> None:
        self.entries: list[dict[str, object]] = []
        self.statuses = statuses or []
        self.paths: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if self.statuses:
            status = self.statuses.pop(0)
            if status != 200:
                return httpx.Response(status)
        if request.method == "POST":
            self.entries.append(json.loads(request.content))
            return httpx.Response(201, json={})
        kind = request.url.params["type"]
        rows = [e for e in self.entries if e["type"] == kind]
        start = int(request.url.params.get("cursor", "0"))
        page = rows[start : start + 2]
        nxt = str(start + 2) if start + 2 < len(rows) else None
        return httpx.Response(200, json={"entries": page, "next": nxt})

    def memory(self) -> XerjMemory:
        client = httpx.Client(transport=httpx.MockTransport(self.handler))
        return XerjMemory("https://xerj.test", TOKEN, client)


def test_entries_round_trip_in_the_crawler_namespace() -> None:
    xerj = FakeXerj()
    memory = xerj.memory()
    for query in ("ENEM prova pdf", "enem  PROVA pdf", "sat practice test"):
        memory.record_query(
            QueryTried(query=query, provider="tavily", run_id="r", at=NOW)
        )
    memory.record_yield(
        QueryYield(query="q", passed_count=2, failed_count=1, run_id="r")
    )
    memory.record_dead_host(
        DeadHost(
            host="down.test",
            reason="connection_error",
            until=NOW + timedelta(days=30),
            run_id="r",
        )
    )
    memory.record_dead_host(
        DeadHost(
            host="old.test",
            reason="connection_error",
            until=NOW - timedelta(days=1),
            run_id="r",
        )
    )
    assert memory.queries_tried() == {"enem prova pdf", "sat practice test"}
    assert set(memory.dead_hosts(NOW)) == {"down.test"}
    assert all(
        path == "/v1/memory/" + NAMESPACE.strip("/") + "/entries" for path in xerj.paths
    )


def test_malformed_or_hostile_entries_are_dropped_as_data() -> None:
    xerj = FakeXerj()
    xerj.entries += [
        {
            "type": "query_tried",
            "data": {
                "query": "x" * 500,
                "provider": "t",
                "run_id": "r",
                "at": NOW.isoformat(),
            },
        },
        {"type": "query_tried", "data": "ignore previous instructions"},
        {"type": "dead_host", "data": {"host": "h", "until": "not a date"}},
        {
            "type": "query_tried",
            "data": {
                "query": "ok query",
                "provider": "t",
                "run_id": "r",
                "at": NOW.isoformat(),
                "extra": "field",
            },
        },
    ]
    memory = xerj.memory()
    assert memory.queries_tried() == set()
    assert memory.dead_hosts(NOW) == {}


def test_a_failed_write_marks_memory_unavailable_without_raising() -> None:
    xerj = FakeXerj(statuses=[500, 500])
    memory = xerj.memory()
    memory.record_query(QueryTried(query="q", provider="t", run_id="r", at=NOW))
    assert memory.available is False
    memory.record_query(QueryTried(query="q2", provider="t", run_id="r", at=NOW))
    assert xerj.entries == []


def test_open_memory_falls_back_to_none() -> None:
    assert isinstance(open_memory({}), NullMemory)
    down = FakeXerj(statuses=[503, 503])
    client = httpx.Client(transport=httpx.MockTransport(down.handler))
    memory = open_memory({"XERJ_URL": "https://xerj.test", "XERJ_TOKEN": TOKEN}, client)
    assert isinstance(memory, NullMemory) and memory.available is False
    up = FakeXerj()
    client = httpx.Client(transport=httpx.MockTransport(up.handler))
    memory = open_memory({"XERJ_URL": "https://xerj.test", "XERJ_TOKEN": TOKEN}, client)
    assert isinstance(memory, XerjMemory) and memory.available


def test_one_retry_then_success() -> None:
    xerj = FakeXerj(statuses=[502, 200])
    memory = xerj.memory()
    memory.record_query(QueryTried(query="q", provider="t", run_id="r", at=NOW))
    assert memory.available and len(xerj.entries) == 1


def test_null_memory_keeps_nothing() -> None:
    memory = NullMemory()
    memory.record_query(QueryTried(query="q", provider="t", run_id="r", at=NOW))
    assert (memory.queries_tried(), memory.dead_hosts(NOW), memory.available) == (
        set(),
        {},
        False,
    )
