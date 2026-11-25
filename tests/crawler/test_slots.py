"""The crawler's host-slot wrapper (FR-010), on a virtual clock."""

from __future__ import annotations

import asyncio
from datetime import datetime

import psycopg
from crawler_fakes import FakeReserve, VirtualClock

from crawler.slots import HostSlots, postgres_reserve


def run(slots: HostSlots, hosts: list[str]) -> list[datetime]:
    async def one(host: str) -> datetime:
        async with slots.request(host) as slot:
            return slot

    async def go() -> list[datetime]:
        return list(await asyncio.gather(*(one(h) for h in hosts)))

    return asyncio.run(go())


def test_one_host_is_spaced_and_two_hosts_run_together(clock: VirtualClock) -> None:
    slots = HostSlots(FakeReserve(clock), clock, interval_s=5.0, concurrency=8)
    a1, b1, a2 = run(slots, ["a.test", "b.test", "a.test"])
    assert a1 == b1  # different hosts share a moment
    assert (a2 - a1).total_seconds() == 5.0


def test_concurrency_is_capped(clock: VirtualClock) -> None:
    active = 0
    peak = 0
    slots = HostSlots(FakeReserve(clock), clock, interval_s=5.0, concurrency=2)

    async def go() -> None:
        # Each request holds its permit until two are in flight at once, so the
        # result does not depend on how the event loop interleaves the tasks.
        release = asyncio.Event()

        async def one(host: str) -> None:
            nonlocal active, peak
            async with slots.request(host):
                active += 1
                peak = max(peak, active)
                if active == 2:
                    release.set()
                await asyncio.wait_for(release.wait(), timeout=5)
                active -= 1

        await asyncio.gather(*(one(f"h{n}.test") for n in range(6)))

    asyncio.run(go())
    assert peak == 2


def test_the_postgres_reservation_is_used_as_is(crawler_db: psycopg.Connection) -> None:
    reserve = postgres_reserve(crawler_db)
    wait, first = reserve("a.test", 5.0)
    wait2, second = reserve("a.test", 5.0)
    assert wait == 0.0
    assert (second - first).total_seconds() == 5.0
    assert 0 < wait2 <= 5.0
