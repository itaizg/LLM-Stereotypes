"""Host spacing for crawler requests: the shared slot reservation (FR-010, research R6).

Every request reserves its host's next slot in ``host_requests`` with one atomic SQL
statement (``pipeline.store.reserve_host_slot``), then sleeps until the slot. Because
the downloader reserves through the same table, requests to one host stay at least
``interval_s`` apart across crawler runs and ``pipeline fetch`` alike. The reserved slot
time is the observation time stored on a proposal (``observed_at``).

Within the process, one request per host waits at a time (so queued requests to a slow
host do not hold the concurrency permits), and at most ``concurrency`` requests are in
flight across hosts.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import datetime

import psycopg

from pipeline.store import reserve_host_slot
from pipeline.web import Clock

Reserve = Callable[[str, float], tuple[float, datetime]]


def postgres_reserve(conn: psycopg.Connection) -> Reserve:
    """A ``Reserve`` over one connection, timed by the database clock."""

    def reserve(host: str, interval_s: float) -> tuple[float, datetime]:
        return reserve_host_slot(conn, host, interval_s)

    return reserve


class HostSlots:
    def __init__(
        self, reserve: Reserve, clock: Clock, interval_s: float, concurrency: int
    ) -> None:
        self._reserve = reserve
        self._clock = clock
        self._interval_s = interval_s
        self._inflight = asyncio.Semaphore(concurrency)
        self._reserving = asyncio.Lock()
        self._hosts: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def request(self, host: str) -> AsyncGenerator[datetime]:
        async with self._hosts.setdefault(host, asyncio.Lock()), self._inflight:
            # One reservation at a time: the connection is not shared across threads.
            async with self._reserving:
                wait_s, slot = await asyncio.to_thread(
                    self._reserve, host, self._interval_s
                )
            if wait_s > 0:
                await self._clock.sleep(wait_s)
            yield slot
