"""Network safety for every crawler request (FR-008, research R7).

A URL from a search result or a redirect is untrusted. Before each hop the crawler:
1. accepts only ``http`` and ``https``;
2. resolves the host and refuses it if *any* address is not publicly routable
   (private, loopback, link-local, multicast, reserved, shared, or a cloud metadata
   address), so DNS cannot point it inward;
3. connects to the address it checked, never resolving the name a second time: the
   request goes to the IP, with the original ``Host`` header and TLS server name;
4. follows at most ``MAX_REDIRECTS`` hops, checking each one again, robots.txt
   included when the caller passes ``allow``.

Every refusal is a typed ``Refused`` carrying its reason code, and no request is sent to
a refused target.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from crawler.config import MAX_REDIRECTS
from crawler.contracts import ReasonCode
from pipeline.web import USER_AGENT

REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

# Cloud metadata endpoints. Most are link-local or unique-local anyway; they are listed
# so the rule does not depend on how a future Python classifies them.
METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("169.254.170.2"),
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)


class Refused(Exception):
    """A request the crawler will not send, or one that failed, with its reason."""

    def __init__(self, reason: ReasonCode, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else str(reason))
        self.reason = reason
        self.detail = detail


class Resolver(Protocol):
    async def resolve(self, host: str) -> list[str]: ...


class SystemResolver:
    """DNS through the event loop's ``getaddrinfo`` (no extra dependency)."""

    async def resolve(self, host: str) -> list[str]:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError as error:
            raise Refused(ReasonCode.CONNECTION_ERROR, "dns") from error
        return sorted({str(info[4][0]) for info in infos})


class SlotGate(Protocol):
    """Host spacing: entering waits for the host's slot and yields its time."""

    def request(self, host: str) -> AbstractAsyncContextManager[datetime]: ...


def is_public(address: str) -> bool:
    """True only for a globally routable unicast address that is not a metadata IP."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip in METADATA_ADDRESSES or ip.is_multicast:
        return False
    return ip.is_global


@dataclass(frozen=True)
class SafeResponse:
    url: str
    final_url: str
    status: int
    headers: httpx.Headers
    body: bytes
    truncated: bool
    hops: int
    latency_s: float
    observed_at: datetime | None


def _pinned_url(url: str, address: str) -> str:
    parts = urlsplit(url)
    ip = ipaddress.ip_address(address)
    host = f"[{ip}]" if ip.version == 6 else str(ip)
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path or "/", parts.query, ""))


class SafeClient:
    """GET through the address policy, host spacing and a bounded read."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        resolver: Resolver,
        slots: SlotGate | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._slots = slots
        self._monotonic = monotonic

    async def check(self, url: str) -> str:
        """Return the address to connect to for ``url``, or raise ``Refused``."""
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            raise Refused(ReasonCode.BAD_SCHEME, parts.scheme or "none")
        host = parts.hostname
        if not host:
            raise Refused(ReasonCode.BAD_SCHEME, "no host")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            addresses = await self._resolver.resolve(host)
        else:
            addresses = [str(literal)]
        if not addresses:
            raise Refused(ReasonCode.CONNECTION_ERROR, "no address")
        if not all(is_public(a) for a in addresses):
            raise Refused(ReasonCode.NON_PUBLIC_ADDRESS, host)
        return addresses[0]

    async def get(
        self,
        url: str,
        *,
        max_bytes: int,
        timeout_s: float,
        headers: dict[str, str] | None = None,
        allow: Callable[[str], Awaitable[bool]] | None = None,
    ) -> SafeResponse:
        """GET ``url``; ``allow`` (robots.txt) is asked before every hop is sent."""
        start = self._monotonic()
        current = url
        observed_at: datetime | None = None
        for hops in range(MAX_REDIRECTS + 1):
            address = await self.check(current)
            if allow is not None and not await allow(current):
                raise Refused(ReasonCode.BLOCKED, current)
            host = (urlsplit(current).hostname or "").lower()
            if self._slots is not None:
                async with self._slots.request(host) as slot:
                    observed_at = slot
                    status, got, body, truncated = await self._send(
                        current, address, max_bytes, timeout_s, headers
                    )
            else:
                status, got, body, truncated = await self._send(
                    current, address, max_bytes, timeout_s, headers
                )
            location = got.get("location")
            if status in REDIRECT_CODES and location:
                current = urljoin(current, location)
                continue
            return SafeResponse(
                url=url,
                final_url=current,
                status=status,
                headers=got,
                body=body,
                truncated=truncated,
                hops=hops,
                latency_s=max(0.0, self._monotonic() - start),
                observed_at=observed_at,
            )
        raise Refused(ReasonCode.TOO_MANY_REDIRECTS, url)

    async def _send(
        self,
        url: str,
        address: str,
        max_bytes: int,
        timeout_s: float,
        headers: dict[str, str] | None,
    ) -> tuple[int, httpx.Headers, bytes, bool]:
        parts = urlsplit(url)
        host_header = parts.netloc.rsplit("@", 1)[-1]
        request = self._client.build_request(
            "GET",
            _pinned_url(url, address),
            headers={"Host": host_header, "User-Agent": USER_AGENT, **(headers or {})},
            extensions={
                "sni_hostname": parts.hostname or "",
                "timeout": dict.fromkeys(
                    ("connect", "read", "write", "pool"), timeout_s
                ),
            },
        )
        try:
            async with asyncio.timeout(timeout_s):
                response = await self._client.send(request, stream=True)
                try:
                    chunks: list[bytes] = []
                    size = 0
                    truncated = False
                    async for chunk in response.aiter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > max_bytes:
                            truncated = True
                            break
                    body = b"".join(chunks)[:max_bytes]
                finally:
                    await response.aclose()
        except TimeoutError as error:
            raise Refused(ReasonCode.TIMEOUT, parts.hostname or "") from error
        except httpx.TimeoutException as error:
            raise Refused(ReasonCode.TIMEOUT, parts.hostname or "") from error
        except httpx.TransportError as error:
            raise Refused(ReasonCode.CONNECTION_ERROR, type(error).__name__) from error
        return response.status_code, response.headers, body, truncated
