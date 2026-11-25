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

import hashlib
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
    """``CrawlerMemory`` over XERJ's agent-memory REST API, namespace ``agent-crawler``.

    The wire shape follows XERJ's ``memory_api.rs`` (checked 2026-11-24):

    - store: ``POST {url}/_memory/agent-crawler`` with ``{id, text, metadata}``. The
      typed entry travels in ``metadata`` as ``{"kind", "data"}``; ``text`` is a short
      human-readable line, so the namespace also reads well through XERJ's own search.
      Storing an existing ``id`` replaces it, so ids are stable per fact: one
      ``query_tried`` per query, one ``dead_host`` per host, one ``query_yield`` per
      query and run. The namespace grows with distinct facts, not with runs.
    - recall: ``GET {url}/_memory/agent-crawler?from=&size=``, newest first, paged by
      the ``next`` cursor. ``_recall`` is not used: it needs a non-empty text query or
      a vector, and the crawler wants every entry of a kind, so entries are filtered by
      ``metadata.kind`` here.
    - auth: ``Authorization: Bearer <key>`` when a token is set. A node started with
      ``--insecure`` has no auth, so the token is optional.

    Limits (FR-004): a 5 s timeout, one retry, 20 entries per page and at most 1 MB
    read per recall. Recalled entries are validated; a malformed one is dropped. A
    failed write marks memory unavailable for the rest of the run instead of failing
    the run, because memory is an optimisation (US4).
    """

    PAGE = 20

    def __init__(
        self, url: str, token: str | None = None, client: httpx.Client | None = None
    ) -> None:
        self._base = url.rstrip("/") + NAMESPACE
        self._client = client or httpx.Client(timeout=MEMORY_TIMEOUT_S)
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
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
                    self._base,
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
        offset = 0
        read = 0
        while True:
            params = {"from": str(offset), "size": str(self.PAGE)}
            response = self._request("GET", params=params)
            read += len(response.content)
            if read > MEMORY_MAX_BYTES:
                break
            body = response.json()
            if not isinstance(body, dict):
                break
            rows = body.get("entries")
            for row in rows if isinstance(rows, list) else []:
                meta = row.get("metadata") if isinstance(row, dict) else None
                if isinstance(meta, dict) and meta.get("kind") == kind:
                    data = meta.get("data")
                    if isinstance(data, dict):
                        found.append(data)
            nxt = body.get("next")
            following = nxt.get("from") if isinstance(nxt, dict) else None
            # A cursor that does not move forward would loop forever: stop instead.
            if not isinstance(following, int) or following <= offset:
                break
            offset = following
        return found

    def check(self) -> None:
        """One small read, so an unreachable XERJ is noticed before the run starts."""
        self._request("GET", params={"size": "1"})

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

    def _remember(self, kind: str, key: str, text: str, entry: Entry) -> None:
        if not self.available:
            return
        body: dict[str, object] = {
            "id": f"{kind}-{_digest(key)}",
            "text": text,
            "metadata": {"kind": kind, "data": entry.model_dump(mode="json")},
        }
        try:
            self._request("POST", json=body)
        except (httpx.HTTPError, RuntimeError):
            self.available = False

    def record_query(self, entry: QueryTried) -> None:
        key = query_key(entry.query)
        self._remember("query_tried", key, f"query tried: {key}", entry)

    def record_yield(self, entry: QueryYield) -> None:
        key = query_key(entry.query)
        text = (
            f"query yield: {key} passed={entry.passed_count} "
            f"failed={entry.failed_count}"
        )
        self._remember("query_yield", f"{entry.run_id}\n{key}", text, entry)

    def record_dead_host(self, entry: DeadHost) -> None:
        text = f"dead host: {entry.host} ({entry.reason})"
        self._remember("dead_host", entry.host, text, entry)


def _digest(key: str) -> str:
    """A short id-safe digest, so queries and hosts never reach the id verbatim."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def open_memory(
    env: Mapping[str, str], client: httpx.Client | None = None
) -> CrawlerMemory:
    """XERJ when ``XERJ_URL`` is set and it answers, else none.

    ``XERJ_TOKEN`` is optional: a local node started with ``--insecure`` has no auth.
    """
    url, token = env.get("XERJ_URL"), env.get("XERJ_TOKEN") or None
    if not url:
        return NullMemory()
    memory = XerjMemory(url, token, client)
    try:
        memory.check()
    except (httpx.HTTPError, RuntimeError, ValueError):
        return NullMemory()
    return memory
