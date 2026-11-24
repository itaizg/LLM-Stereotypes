"""Crawler memory: queries tried, dead hosts and yield per query, kept across runs.

Memory is code-managed (constitution "Memory access mode"; FR-030): the graph reads it
when a run starts and writes it when each task ends. It is not an LLM tool. Entries are
typed records parsed at the boundary; recalled text is data, never instructions
(FR-031), and it is never put into the prompt.

The real store is XERJ under the namespace ``/_memory/agent-crawler`` (research R9),
behind the ``CrawlerMemory`` Protocol. ``NullMemory`` stands in when no XERJ is
configured or it is unreachable; the run then records ``memory_available = false``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from crawler.config import MEMORY_MAX_BYTES, MEMORY_RETRIES, MEMORY_TIMEOUT_S

NAMESPACE = "/_memory/agent-crawler"


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QueryTried(Entry):
    query: str = Field(max_length=200)
    lang: str | None = Field(default=None, max_length=16)
    provider: str = Field(max_length=16)
    run_id: str = Field(max_length=40)
    at: datetime


class QueryYield(Entry):
    query: str = Field(max_length=200)
    passed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    run_id: str = Field(max_length=40)


class DeadHost(Entry):
    host: str = Field(max_length=253)
    reason: str = Field(max_length=40)
    until: datetime
    run_id: str = Field(max_length=40)


class CrawlerMemory(Protocol):
    available: bool

    def queries_tried(self) -> set[str]: ...

    def dead_hosts(self, now: datetime) -> dict[str, DeadHost]: ...

    def record_query(self, entry: QueryTried) -> None: ...

    def record_yield(self, entry: QueryYield) -> None: ...

    def record_dead_host(self, entry: DeadHost) -> None: ...


def query_key(query: str) -> str:
    """Queries are compared case- and space-insensitively."""
    return " ".join(query.casefold().split())


class NullMemory:
    """No memory: nothing is recalled and nothing is kept."""

    available = False

    def queries_tried(self) -> set[str]:
        return set()

    def dead_hosts(self, now: datetime) -> dict[str, DeadHost]:
        return {}

    def record_query(self, entry: QueryTried) -> None:
        return None

    def record_yield(self, entry: QueryYield) -> None:
        return None

    def record_dead_host(self, entry: DeadHost) -> None:
        return None


class XerjMemory:
    """``CrawlerMemory`` over XERJ's REST API, namespace ``/_memory/agent-crawler``.

    The endpoint shape is an assumption until it is checked against a running XERJ
    (research R9 left it to this adapter): entries are posted to
    ``{url}/v1/memory/{namespace}/entries`` as ``{"type", "data"}`` and listed from
    the same path with ``type``, ``limit`` and ``cursor`` parameters. Everything else
    in the crawler depends only on the Protocol, so correcting the shape touches this
    class alone.

    Limits (FR-004): a 5 s timeout, one retry, 20 entries per page and at most 1 MB
    read per recall. Recalled entries are validated; a malformed one is dropped. A
    failed write marks memory unavailable for the rest of the run instead of failing
    the run, because memory is an optimisation (US4).
    """

    PAGE = 20

    def __init__(
        self, url: str, token: str, client: httpx.Client | None = None
    ) -> None:
        self._base = url.rstrip("/") + "/v1/memory/" + NAMESPACE.strip("/")
        self._client = client or httpx.Client(timeout=MEMORY_TIMEOUT_S)
        self._headers = {"Authorization": f"Bearer {token}"}
        self.available = True

    def _request(
        self,
        method: str,
        params: dict[str, str] | None = None,
        json: dict[str, object] | None = None,
    ) -> httpx.Response:
        last: Exception | None = None
        for _attempt in range(MEMORY_RETRIES + 1):
            try:
                response = self._client.request(
                    method,
                    f"{self._base}/entries",
                    headers=self._headers,
                    timeout=MEMORY_TIMEOUT_S,
                    params=params,
                    json=json,
                )
            except httpx.HTTPError as error:
                last = error
                continue
            if response.status_code >= 500:
                last = httpx.HTTPStatusError(
                    "server error", request=response.request, response=response
                )
                continue
            response.raise_for_status()
            return response
        raise last if last is not None else RuntimeError("memory request failed")

    def _recall(self, kind: str) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        cursor: str | None = None
        read = 0
        while True:
            params = {"type": kind, "limit": str(self.PAGE)}
            if cursor:
                params["cursor"] = cursor
            response = self._request("GET", params=params)
            read += len(response.content)
            if read > MEMORY_MAX_BYTES:
                break
            body = response.json()
            rows = body.get("entries") if isinstance(body, dict) else None
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict) and row.get("type") == kind:
                    data = row.get("data")
                    if isinstance(data, dict):
                        found.append(data)
            cursor = body.get("next") if isinstance(body, dict) else None
            if not isinstance(cursor, str) or not cursor:
                break
        return found

    def check(self) -> None:
        """One small read, so an unreachable XERJ is noticed before the run starts."""
        self._request("GET", params={"type": "query_tried", "limit": "1"})

    def queries_tried(self) -> set[str]:
        keys: set[str] = set()
        for data in self._recall("query_tried"):
            try:
                keys.add(query_key(QueryTried.model_validate(data).query))
            except ValidationError:
                continue
        return keys

    def dead_hosts(self, now: datetime) -> dict[str, DeadHost]:
        hosts: dict[str, DeadHost] = {}
        for data in self._recall("dead_host"):
            try:
                entry = DeadHost.model_validate(data)
            except ValidationError:
                continue
            if entry.until > now:
                hosts[entry.host] = entry
        return hosts

    def _remember(self, kind: str, entry: Entry) -> None:
        if not self.available:
            return
        try:
            self._request(
                "POST", json={"type": kind, "data": entry.model_dump(mode="json")}
            )
        except (httpx.HTTPError, RuntimeError):
            self.available = False

    def record_query(self, entry: QueryTried) -> None:
        self._remember("query_tried", entry)

    def record_yield(self, entry: QueryYield) -> None:
        self._remember("query_yield", entry)

    def record_dead_host(self, entry: DeadHost) -> None:
        self._remember("dead_host", entry)


def open_memory(
    env: Mapping[str, str], client: httpx.Client | None = None
) -> CrawlerMemory:
    """XERJ when ``XERJ_URL`` and ``XERJ_TOKEN`` are set and it answers, else none."""
    url, token = env.get("XERJ_URL"), env.get("XERJ_TOKEN")
    if not url or not token:
        return NullMemory()
    memory = XerjMemory(url, token, client)
    try:
        memory.check()
    except (httpx.HTTPError, RuntimeError, ValueError):
        return NullMemory()
    return memory
