"""Network guard for the browser tool.

Two layers:
  1. ``validate_input_url`` — rejects anything that is not a plain public https URL.
  2. ``GuardProxy`` — a local CONNECT-only proxy that Chromium is forced to use for
     *every* request (navigation, redirects, subresources, websockets). The proxy
     resolves DNS itself, requires every resolved address to be public, and then
     connects to the vetted IP (no second lookup, so no DNS-rebinding window).
     Plain-http (non-CONNECT) requests and non-443 ports are refused.

Chromium is additionally started with ``--host-resolver-rules`` that make its own
DNS resolution fail, so a request that somehow bypassed the proxy could not resolve.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ALLOWED_PORT = 443
MAX_URL_LENGTH = 2048
BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".lan",
    ".home",
    ".corp",
    ".home.arpa",
    ".localdomain",
)
BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "metadata", "metadata.google.internal"}

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")


class BlockedURLError(ValueError):
    """Raised when a URL or host is not allowed."""


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally routable unicast addresses (incl. embedded IPv4 checks)."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_ip(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return is_public_ip(ip.sixtofour)
        if ip.teredo is not None:
            return False
        if ip in _NAT64_LOCAL:
            return False
        if ip in _NAT64:
            return is_public_ip(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return bool(
        ip.is_global
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_private
        and not ip.is_unspecified
    )


def check_hostname(host: str) -> None:
    """Rejects internal-looking hostnames and non-public IP literals."""
    if not host:
        raise BlockedURLError("URL heeft geen host.")
    h = host.lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(h.strip("[]"))
    except ValueError:
        ip = None
    if ip is not None:
        if not is_public_ip(ip):
            raise BlockedURLError(f"IP-adres {ip} is niet publiek.")
        return
    if h in BLOCKED_HOSTNAMES or h.endswith(BLOCKED_HOST_SUFFIXES):
        raise BlockedURLError(f"Host '{h}' is een interne/lokale naam.")
    if "." not in h:
        raise BlockedURLError(f"Host '{h}' is geen volledig gekwalificeerde publieke domeinnaam.")


def validate_input_url(url: str) -> str:
    """Validates the user-supplied URL. Returns the normalised URL string."""
    if not isinstance(url, str) or not url.strip():
        raise BlockedURLError("Geen URL opgegeven.")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise BlockedURLError(f"URL is langer dan {MAX_URL_LENGTH} tekens.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise BlockedURLError(f"Ongeldige URL: {e}") from None
    if parts.scheme.lower() != "https":
        raise BlockedURLError("Alleen https:// URL's zijn toegestaan.")
    if parts.username or parts.password:
        raise BlockedURLError("URL's met inloggegevens zijn niet toegestaan.")
    if port not in (None, ALLOWED_PORT):
        raise BlockedURLError(f"Alleen poort {ALLOWED_PORT} is toegestaan.")
    check_hostname(parts.hostname or "")
    return url


async def resolve_public(host: str, port: int = ALLOWED_PORT, timeout: float = 5.0) -> str:
    """Resolves ``host`` and returns one vetted public IP. Blocks if ANY address is non-public."""
    check_hostname(host)
    h = host.strip("[]")
    try:
        literal = ipaddress.ip_address(h)
        return str(literal)
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(h, port, type=socket.SOCK_STREAM), timeout=timeout
        )
    except (socket.gaierror, asyncio.TimeoutError) as e:
        raise BlockedURLError(f"DNS-resolutie mislukt voor '{h}': {type(e).__name__}") from None
    addrs = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not is_public_ip(ip):
            raise BlockedURLError(f"Host '{h}' verwijst naar niet-publiek adres {ip}.")
        addrs.append(str(ip))
    if not addrs:
        raise BlockedURLError(f"Geen adressen gevonden voor '{h}'.")
    # Prefer IPv4 (IPv6 egress is not always available in containers).
    addrs.sort(key=lambda a: ":" in a)
    return addrs[0]


@dataclass
class GuardProxy:
    """Local CONNECT-only filtering proxy, one instance per capture."""

    connect_timeout: float = 10.0
    header_timeout: float = 10.0
    blocked: list[dict] = field(default_factory=list)
    allowed_count: int = 0
    _server: asyncio.base_events.Server | None = None
    _writers: set = field(default_factory=set)
    _tasks: set = field(default_factory=set)

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for w in list(self._writers):
            w.close()
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=5)
            except asyncio.TimeoutError:
                pass
        self._server = None

    def _record_block(self, target: str, reason: str) -> None:
        if len(self.blocked) < 50:
            self.blocked.append({"target": target, "reason": reason})
        logger.info("browser guard blocked %s: %s", target, reason)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        self._writers.add(writer)
        upstream_writer = None
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.header_timeout)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
                return
            request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
            parts = request_line.split(" ")
            if len(parts) < 2:
                return
            method, target = parts[0].upper(), parts[1]
            if method != "CONNECT":
                # Plain-http request (absolute-form). Report only scheme+host, never path/query.
                shown = urlsplit(target)
                self._record_block(f"{shown.scheme}://{shown.hostname}", "alleen https toegestaan")
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            host, _, port_s = target.rpartition(":")
            try:
                port = int(port_s)
            except ValueError:
                port = -1
            host = host.strip("[]")
            if port != ALLOWED_PORT:
                self._record_block(target, f"poort {port_s} niet toegestaan")
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            try:
                ip = await resolve_public(host, port)
            except BlockedURLError as e:
                self._record_block(host, str(e))
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            try:
                up_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(ip, port), self.connect_timeout
                )
            except (OSError, asyncio.TimeoutError):
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            self._writers.add(upstream_writer)
            self.allowed_count += 1
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(
                self._pipe(reader, upstream_writer),
                self._pipe(up_reader, writer),
                return_exceptions=True,
            )
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            for w in (writer, upstream_writer):
                if w is not None:
                    w.close()
                    self._writers.discard(w)
            self._tasks.discard(task)

    @staticmethod
    async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await src.read(65536)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        finally:
            try:
                dst.write_eof()
            except (OSError, RuntimeError):
                pass
