"""Egress guard: every browser connection goes through the local proxy here.

Why a proxy instead of Playwright's route interception:
- route handlers **do not see redirect targets** (tested: a 302 to an internal address got through 3 out of 3 times),
  and redirects of subresources and page-JS fetch() slip past the same way.
- "We resolve DNS" and "the browser connects" are two separate lookups; DNS rebinding can swap in an internal IP between them.

The proxy collapses all of this into one checkpoint: **resolve DNS once -> validate every IP -> connect to that IP directly**.
Redirects, subresources, fetch and WebSocket are all new connections and get checked again; there is no second
lookup, so rebinding has nothing to exploit.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from collections import deque
from urllib.parse import urlsplit

_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
_HEAD_LIMIT = 64 * 1024
_CONNECT_TIMEOUT_S = 15


class Blocked(Exception):
    """Target is an internal/reserved address, or the URL itself is not allowed."""


def is_blocked_ip(ip_str: str) -> bool:
    """Whether the IP is private, loopback, link-local (incl. cloud metadata), reserved, multicast or unspecified.

    >>> [is_blocked_ip(x) for x in ("127.0.0.1", "10.1.2.3", "192.168.0.1", "169.254.169.254")]
    [True, True, True, True]
    >>> [is_blocked_ip(x) for x in ("::1", "fe80::1", "::ffff:127.0.0.1", "0.0.0.0")]
    [True, True, True, True]
    >>> is_blocked_ip("93.184.216.34")
    False
    """
    try:
        ip = ipaddress.ip_address(ip_str.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified)


def _normalize_ipv4(host: str) -> str | None:
    """Normalize obfuscated IPv4 forms (decimal, hex, octal, short form).

    >>> [_normalize_ipv4(h) for h in ("2130706433", "0x7f000001", "017700000001", "127.1")]
    ['127.0.0.1', '127.0.0.1', '127.0.0.1', '127.0.0.1']
    >>> _normalize_ipv4("example.com") is None
    True
    """
    try:
        return str(ipaddress.ip_address(socket.inet_aton(host)))
    except (OSError, ValueError):
        return None


def _allowed() -> set[str]:
    """Tests only: exact host:port entries in GLOVEBOX_ALLOW_HOSTS skip the internal-network check."""
    return {x.strip().lower() for x in os.environ.get("GLOVEBOX_ALLOW_HOSTS", "").split(",") if x.strip()}


def check_url(url: str) -> None:
    """Pre-check without DNS, so obviously disallowed URLs get a clear error before any request.

    >>> check_url("https://example.com/a")
    >>> for u in ("file:///etc/passwd", "javascript:alert(1)", "http://localhost/", "http://2130706433/", "http://[::1]/"):
    ...     try:
    ...         check_url(u)
    ...     except Blocked as e:
    ...         print(str(e)[:24])
    Only http/https URLs are
    Only http/https URLs are
    Hostname "localhost" poi
    Target IP 127.0.0.1 is i
    Target IP ::1 is interna
    """
    p = urlsplit(url)
    if p.scheme.lower() not in ("http", "https"):
        raise Blocked(f"Only http/https URLs are accepted, got: {p.scheme or '(no scheme)'}")
    host = (p.hostname or "").lower()
    if not host:
        raise Blocked("URL has no hostname")
    port = p.port or (443 if p.scheme.lower() == "https" else 80)
    if f"{host}:{port}" in _allowed():
        return
    if host in _BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        raise Blocked(f"Hostname \"{host}\" points to this machine")
    literal = _normalize_ipv4(host) or host
    if is_blocked_ip(literal):
        raise Blocked(f"Target IP {literal} is internal/reserved")


async def resolve_checked(host: str, port: int) -> str:
    """Resolve DNS once and validate **every** address; return the IP to connect to. Any internal address rejects the whole host."""
    host = host.strip("[]").lower()
    if f"{host}:{port}" in _allowed():
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return infos[0][4][0]
    if host in _BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        raise Blocked(f"Hostname \"{host}\" points to this machine")
    literal = _normalize_ipv4(host)
    if literal:
        host = literal
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise Blocked(f"Cannot resolve host \"{host}\": {e}") from None
    ips = list(dict.fromkeys(i[4][0] for i in infos))
    bad = [ip for ip in ips if is_blocked_ip(ip)]
    if bad:
        raise Blocked(f"\"{host}\" resolves to internal/reserved IP {bad[0]}")
    return ips[0]


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except (ConnectionError, OSError):
            pass


class EgressProxy:
    """Minimal HTTP proxy listening only on 127.0.0.1 (CONNECT + plain HTTP forwarding)."""

    def __init__(self) -> None:
        self._server: asyncio.base_events.Server | None = None
        self.port = 0
        # Recently blocked targets (host, reason), so open() can explain why a page wouldn't load
        self.blocked: deque[tuple[str, str]] = deque(maxlen=50)

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None

    def reason_for(self, host: str) -> str | None:
        host = host.lower()
        for h, why in reversed(self.blocked):
            if h == host:
                return why
        return None

    async def _handle(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        try:
            head = await cr.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            cw.close()
            return
        if len(head) > _HEAD_LIMIT:
            cw.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            cw.close()
            return
        try:
            if method.upper() == "CONNECT":
                host, _, port_s = target.rpartition(":")
                port = int(port_s)
                ip = await resolve_checked(host, port)
                ur, uw = await asyncio.wait_for(asyncio.open_connection(ip, port), _CONNECT_TIMEOUT_S)
                cw.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await cw.drain()
            else:
                u = urlsplit(target)
                if u.scheme.lower() != "http" or not u.hostname:
                    raise Blocked(f"Proxy only forwards http URLs: {target[:80]}")
                port = u.port or 80
                ip = await resolve_checked(u.hostname, port)
                ur, uw = await asyncio.wait_for(asyncio.open_connection(ip, port), _CONNECT_TIMEOUT_S)
                path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                # Force Connection: close: keep-alive would let the next request reuse this already-checked upstream and skip the check
                kept = [h for h in lines[1:] if h and h.split(":", 1)[0].strip().lower()
                        not in ("proxy-connection", "connection", "keep-alive", "proxy-authorization")]
                out = "\r\n".join([f"{method} {path} {version}", *kept, "Connection: close", "", ""])
                uw.write(out.encode("latin-1"))
                await uw.drain()
        except (Blocked, ValueError) as e:
            host = (urlsplit(target).hostname or target.rpartition(":")[0]).strip("[]").lower()
            self.blocked.append((host, str(e)))
            body = f"Blocked by glovebox: {e}".encode()
            cw.write(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain; charset=utf-8\r\n"
                     + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await cw.drain()
            cw.close()
            return
        except (OSError, asyncio.TimeoutError):
            cw.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            await cw.drain()
            cw.close()
            return
        await asyncio.gather(_pipe(cr, uw), _pipe(ur, cw))
