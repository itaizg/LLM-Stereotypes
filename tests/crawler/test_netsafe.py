"""Network safety (FR-008, research R7): no request reaches a non-public target."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from crawler_fakes import FakeResolver, ReplayWeb, VirtualClock, safe_client

from crawler.contracts import ReasonCode
from crawler.netsafe import Refused, SafeResponse, is_public
from crawler.slots import HostSlots

PAGE = "https://exams.example/paper.pdf"


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("23.1.2.3", True),
        ("2606:4700::1", True),
        ("10.0.0.1", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("100.100.100.200", False),
        ("100.64.0.1", False),
        ("224.0.0.1", False),
        ("0.0.0.0", False),
        ("::1", False),
        ("fe80::1", False),
        ("fd00:ec2::254", False),
        ("ff02::1", False),
        ("::ffff:10.0.0.1", False),
        ("::ffff:23.1.2.3", True),
        ("not-an-ip", False),
    ],
)
def test_the_address_policy(address: str, public: bool) -> None:
    assert is_public(address) is public


def fetch(
    web: ReplayWeb,
    resolver: FakeResolver,
    clock: VirtualClock,
    url: str,
    slots: HostSlots | None = None,
    max_bytes: int = 1024,
) -> SafeResponse:
    async def go() -> SafeResponse:
        async with web.client() as client:
            safe = safe_client(client, resolver, slots, clock)
            return await safe.get(url, max_bytes=max_bytes, timeout_s=10)

    return asyncio.run(go())


def refusal(
    web: ReplayWeb, resolver: FakeResolver, clock: VirtualClock, url: str
) -> ReasonCode:
    with pytest.raises(Refused) as caught:
        fetch(web, resolver, clock, url)
    return caught.value.reason


def test_a_public_page_is_fetched_at_its_checked_address(
    web: ReplayWeb, resolver: FakeResolver, clock: VirtualClock
) -> None:
    web.add(PAGE, b"%PDF-1.7 synthetic")
    response = fetch(web, resolver, clock, PAGE)
    assert response.status == 200
    assert web.log == [PAGE]
    # The connection went to the IP that was checked, not a fresh DNS lookup.
    assert web.peers == [asyncio.run(resolver.resolve("exams.example"))[0]]


@pytest.mark.parametrize("address", ["10.0.0.1", "127.0.0.1", "169.254.169.254"])
def test_a_private_host_is_refused_without_a_request(
    web: ReplayWeb, clock: VirtualClock, address: str
) -> None:
    resolver = FakeResolver({"inside.example": [address]})
    web.add("https://inside.example/", b"secret")
    assert refusal(web, resolver, clock, "https://inside.example/") is (
        ReasonCode.NON_PUBLIC_ADDRESS
    )
    assert web.log == []


def test_one_private_address_among_several_refuses_the_host(
    web: ReplayWeb, clock: VirtualClock
) -> None:
    resolver = FakeResolver({"mixed.example": ["23.1.1.1", "10.1.1.1"]})
    assert refusal(web, resolver, clock, "https://mixed.example/") is (
        ReasonCode.NON_PUBLIC_ADDRESS
    )


def test_an_ip_literal_is_checked_too(
    web: ReplayWeb, resolver: FakeResolver, clock: VirtualClock
) -> None:
    assert (
        refusal(web, resolver, clock, "http://[::1]/") is ReasonCode.NON_PUBLIC_ADDRESS
    )
    assert refusal(web, resolver, clock, "http://127.0.0.1/") is (
        ReasonCode.NON_PUBLIC_ADDRESS
    )


def test_every_redirect_hop_is_checked(web: ReplayWeb, clock: VirtualClock) -> None:
    resolver = FakeResolver({"inside.example": ["10.0.0.1"]})
    web.redirect(PAGE, "https://mirror.example/p.pdf")
    web.redirect("https://mirror.example/p.pdf", "https://inside.example/meta")
    assert refusal(web, resolver, clock, PAGE) is ReasonCode.NON_PUBLIC_ADDRESS
    assert web.log == [PAGE, "https://mirror.example/p.pdf"]


def test_redirects_stop_after_five_hops(
    web: ReplayWeb, resolver: FakeResolver, clock: VirtualClock
) -> None:
    for n in range(7):
        web.redirect(f"https://loop.example/{n}", f"https://loop.example/{n + 1}")
    assert refusal(web, resolver, clock, "https://loop.example/0") is (
        ReasonCode.TOO_MANY_REDIRECTS
    )
    assert len(web.log) == 6


def test_five_hops_are_followed(
    web: ReplayWeb, resolver: FakeResolver, clock: VirtualClock
) -> None:
    for n in range(5):
        web.redirect(f"https://hop.example/{n}", f"https://hop.example/{n + 1}")
    web.add("https://hop.example/5", b"ok")
    response = fetch(web, resolver, clock, "https://hop.example/0")
    assert (response.final_url, response.hops) == ("https://hop.example/5", 5)


@pytest.mark.parametrize(
    "url", ["ftp://a.example/x", "file:///etc/passwd", "javascript:x"]
)
def test_other_schemes_are_refused(
    web: ReplayWeb, resolver: FakeResolver, clock: VirtualClock, url: str
) -> None:
    assert refusal(web, resolver, clock, url) is ReasonCode.BAD_SCHEME
    assert web.log == []


def test_the_read_is_bounded(
    web: ReplayWeb, resolver: FakeResolver, clock: VirtualClock
) -> None:
    web.add(PAGE, b"x" * 5000)
    response = fetch(web, resolver, clock, PAGE, max_bytes=100)
    assert len(response.body) == 100 and response.truncated
    web.add(PAGE, b"x" * 50)
    assert not fetch(web, resolver, clock, PAGE).truncated


def test_transport_errors_become_reason_codes(
    web: ReplayWeb, resolver: FakeResolver, clock: VirtualClock
) -> None:
    web.raises[PAGE] = httpx.ConnectTimeout("slow")
    assert refusal(web, resolver, clock, PAGE) is ReasonCode.TIMEOUT
    web.raises[PAGE] = httpx.ConnectError("refused")
    assert refusal(web, resolver, clock, PAGE) is ReasonCode.CONNECTION_ERROR


def test_requests_wait_for_their_host_slot(
    web: ReplayWeb, resolver: FakeResolver, clock: VirtualClock, slots: HostSlots
) -> None:
    web.add(PAGE, b"a")
    first = fetch(web, resolver, clock, PAGE, slots=slots)
    second = fetch(web, resolver, clock, PAGE, slots=slots)
    assert clock.sleeps == [5.0]
    assert first.observed_at is not None and second.observed_at is not None
    assert (second.observed_at - first.observed_at).total_seconds() == 5.0
