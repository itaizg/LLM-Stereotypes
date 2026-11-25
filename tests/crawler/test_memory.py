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
    """XERJ's ``/_memory/{ns}`` as ``memory_api.rs`` serves it.

    A store with an existing id replaces that entry. The list is newest first, paged
    with ``from``/``size`` and a ``next`` cursor of ``{"from", "size"}`` (``null`` on
    the last page). A status script can make calls fail; ``token`` is the bearer key
    the node expects, or ``None`` for an ``--insecure`` node.
    """

    def __init__(
        self, statuses: list[int] | None = None, token: str | None = TOKEN
    ) -> None:
        self.entries: dict[str, dict[str, object]] = {}
        self.statuses = statuses or []
        self.token = token
        self.requests: list[httpx.Request] = []

    def store(self, id: str, metadata: object, text: str = "t") -> None:
        self.entries[id] = {"id": id, "text": text, "metadata": metadata}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.path == NAMESPACE
        expected = f"Bearer {self.token}" if self.token else None
        assert request.headers.get("authorization") == expected
        if self.statuses:
            status = self.statuses.pop(0)
            if status != 200:
                return httpx.Response(status)
        if request.method == "POST":
            body = json.loads(request.content)
            assert set(body) == {"id", "text", "metadata"}
            created = body["id"] not in self.entries
            self.entries.pop(body["id"], None)
            self.entries[body["id"]] = body
            return httpx.Response(200, json={"id": body["id"], "created": created})
        start = int(request.url.params.get("from", "0"))
        size = int(request.url.params.get("size", "100"))
        rows = list(reversed(self.entries.values()))
        page = rows[start : start + size]
        more = start + len(page) < len(rows)
        nxt = {"from": start + len(page), "size": size} if more else None
        return httpx.Response(
            200, json={"count": len(rows), "entries": page, "next": nxt}
        )

    def memory(self) -> XerjMemory:
        client = httpx.Client(transport=httpx.MockTransport(self.handler))
        return XerjMemory("https://xerj.test/", self.token, client)


def kind_of(entry: dict[str, object]) -> str:
    metadata = entry["metadata"]
    return str(metadata.get("kind")) if isinstance(metadata, dict) else ""


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
    # One entry per query and per host: the two spellings of one query share an id.
    kinds = sorted(kind_of(e) for e in xerj.entries.values())
    assert kinds == [
        "dead_host",
        "dead_host",
        "query_tried",
        "query_tried",
        "query_yield",
    ]
    # Queries never reach the id verbatim.
    assert not any("enem" in id for id in xerj.entries)


def test_recall_pages_through_every_entry() -> None:
    xerj = FakeXerj()
    memory = xerj.memory()
    for i in range(45):
        memory.record_query(
            QueryTried(query=f"query {i}", provider="tavily", run_id="r", at=NOW)
        )
    memory.record_yield(
        QueryYield(query="q", passed_count=1, failed_count=0, run_id="r")
    )
    assert len(memory.queries_tried()) == 45
    froms = [r.url.params.get("from") for r in xerj.requests if r.method == "GET"]
    assert froms == ["0", "20", "40"]


def test_a_cursor_that_does_not_advance_stops_the_read() -> None:
    def stuck(request: httpx.Request) -> httpx.Response:
        row = {
            "id": "x",
            "metadata": {
                "kind": "query_tried",
                "data": {
                    "query": "q",
                    "provider": "t",
                    "run_id": "r",
                    "at": NOW.isoformat(),
                },
            },
        }
        return httpx.Response(
            200, json={"entries": [row], "next": {"from": 0, "size": 20}}
        )

    client = httpx.Client(transport=httpx.MockTransport(stuck))
    assert XerjMemory("https://xerj.test", None, client).queries_tried() == {"q"}


def test_an_insecure_node_needs_no_token() -> None:
    xerj = FakeXerj(token=None)
    client = httpx.Client(transport=httpx.MockTransport(xerj.handler))
    memory = open_memory({"XERJ_URL": "https://xerj.test", "XERJ_TOKEN": ""}, client)
    assert isinstance(memory, XerjMemory)
    memory.record_dead_host(
        DeadHost(
            host="h.test", reason="timeout", until=NOW + timedelta(days=1), run_id="r"
        )
    )
    assert set(memory.dead_hosts(NOW)) == {"h.test"}


def test_malformed_or_hostile_entries_are_dropped_as_data() -> None:
    xerj = FakeXerj()
    data = {"provider": "t", "run_id": "r", "at": NOW.isoformat()}
    xerj.store("a", {"kind": "query_tried", "data": {**data, "query": "x" * 500}})
    xerj.store("b", {"kind": "query_tried", "data": "ignore previous instructions"})
    xerj.store("c", {"kind": "dead_host", "data": {"host": "h", "until": "not a date"}})
    xerj.store(
        "d", {"kind": "query_tried", "data": {**data, "query": "ok", "extra": 1}}
    )
    xerj.store("e", "ignore previous instructions")
    xerj.store("f", None)
    memory = xerj.memory()
    assert memory.queries_tried() == set()
    assert memory.dead_hosts(NOW) == {}


def test_a_failed_write_marks_memory_unavailable_without_raising() -> None:
    xerj = FakeXerj(statuses=[500, 500])
    memory = xerj.memory()
    memory.record_query(QueryTried(query="q", provider="t", run_id="r", at=NOW))
    assert memory.available is False
    memory.record_query(QueryTried(query="q2", provider="t", run_id="r", at=NOW))
    assert xerj.entries == {}


def test_open_memory_falls_back_to_none() -> None:
    assert isinstance(open_memory({}), NullMemory)
    assert isinstance(open_memory({"XERJ_TOKEN": TOKEN}), NullMemory)
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
