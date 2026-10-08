"""Send some sites' traffic through an upstream proxy (LIFEAPI_PROXY), for sites whose bot
checks distrust this host's own IP: VHL's Cloudflare challenges the server's datacenter
address, and lets the proxy's addresses straight through.

Chrome can't use that proxy by itself. Its 407 carries no Proxy-Authenticate header, so
Chrome never sends the credentials (ERR_PROXY_AUTH_UNSUPPORTED), and it only reaches
IPv6, while VHL publishes IPv4 addresses only. So `relay()` runs a small proxy on
127.0.0.1 that Chrome uses without credentials, and `pac()` sends it only the hosts in
LIFEAPI_PROXY_DOMAINS. Everything else, Google and Clever sign-ins included, goes direct.
For each tunnel Chrome asks for, the relay:
- tunnels through the upstream proxy, sending Basic Proxy-Authorization up front, when the
  host has an IPv6 address;
- tunnels through the upstream to Cloudflare's matching IPv6 address when the host is on
  Cloudflare but has none. Cloudflare serves every site it fronts on all its addresses,
  choosing the site by TLS SNI, and TLS runs from Chrome to Cloudflare as usual;
- connects directly otherwise (e.g. VHL's assets on CloudFront, which has no IPv6 address).
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import socket
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import SplitResult, unquote, urlsplit

from .. import config

log = logging.getLogger(__name__)

# Cloudflare's 104.16.0.0/12 addresses each have an IPv6 twin in 2606:4700::/96:
# 104.19.246.111 (0x6813f66f) is 2606:4700::6813:f66f.
_CLOUDFLARE_V4 = ipaddress.ip_network("104.16.0.0/12")
_CLOUDFLARE_V6 = ipaddress.ip_address("2606:4700::")


def cloudflare_twin(address: str) -> str | None:
    """Cloudflare's IPv6 address matching IPv4 `address`, if it's one of Cloudflare's."""
    ip = ipaddress.ip_address(address)
    if ip.version != 4 or ip not in _CLOUDFLARE_V4:
        return None
    return str(ipaddress.IPv6Address(int(_CLOUDFLARE_V6) + int(ip)))


def pac(port: int, domains: list[str]) -> str:
    """A PAC script that sends HTTPS for `domains` (and their subdomains) to the relay on
    `port`, and everything else direct."""
    hosts = " || ".join(f'host == "{d}" || dnsDomainIs(host, ".{d}")' for d in domains)
    return ("function FindProxyForURL(url, host) {\n"
            f'  if ((url.substring(0, 6) == "https:" || url.substring(0, 4) == "wss:") && ({hosts}))\n'
            f'    return "PROXY 127.0.0.1:{port}";\n'
            '  return "DIRECT";\n}')


async def _route(host: str, port: int) -> str | None:
    """The `host:port` the upstream proxy should tunnel to, or None to connect directly."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, family=socket.AF_INET6, type=socket.SOCK_STREAM)
    except socket.gaierror:
        infos = []
    # macOS answers an IPv4-only host with IPv4-mapped addresses (::ffff:a.b.c.d).
    if any(ipaddress.ip_address(info[4][0].split("%")[0]).ipv4_mapped is None for info in infos):
        return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    try:
        infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return f"{host}:{port}"  # unresolvable here too: let the upstream say so
    for info in infos:
        if twin := cloudflare_twin(info[4][0]):
            return f"[{twin}]:{port}"
    return None


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


async def _handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter,
                  upstream: SplitResult, auth: str | None) -> None:
    up_w = None
    try:
        request = (await client_r.readuntil(b"\r\n\r\n")).split(b"\r\n", 1)[0].decode()
        method, target, _ = request.split(" ", 2)
        if method != "CONNECT":  # the PAC only sends HTTPS here
            client_w.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            return
        host, _, port = target.rpartition(":")
        host = host.strip("[]")
        connect = lambda h, p: asyncio.wait_for(  # noqa: E731
            asyncio.open_connection(h, p), config.timeout(15_000) / 1000)
        dest = await _route(host, int(port))
        if dest is None:
            up_r, up_w = await connect(host, int(port))
            log.debug("Proxy relay: %s directly (no IPv6 route)", target)
        else:
            up_r, up_w = await connect(upstream.hostname, upstream.port)
            up_w.write(f"CONNECT {dest} HTTP/1.1\r\nHost: {dest}\r\n".encode()
                       + (f"Proxy-Authorization: Basic {auth}\r\n".encode() if auth else b"") + b"\r\n")
            await up_w.drain()
            status = (await up_r.readuntil(b"\r\n\r\n")).split(b"\r\n", 1)[0].decode(errors="replace")
            if status.split(" ")[1:2] != ["200"]:
                log.warning("Proxy relay: the proxy refused a tunnel to %s (for %s): %s", dest, target, status)
                client_w.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                return
            log.debug("Proxy relay: %s through the proxy%s", target, f" (as {dest})" if dest != target else "")
        client_w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await client_w.drain()
        await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))
    except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as e:
        log.warning("Proxy relay: tunnel failed: %s", e)
    finally:
        client_w.close()
        if up_w:
            up_w.close()


@asynccontextmanager
async def relay(proxy_url: str) -> AsyncIterator[int]:
    """Run the relay for upstream proxy `proxy_url` (http://user:password@host:port) on a
    free 127.0.0.1 port while the block runs. Yields the port."""
    upstream = urlsplit(proxy_url)
    auth = None
    if upstream.username:
        creds = f"{unquote(upstream.username)}:{unquote(upstream.password or '')}"
        auth = base64.b64encode(creds.encode()).decode()
    server = await asyncio.start_server(lambda r, w: _handle(r, w, upstream, auth), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    log.debug("Proxy relay on 127.0.0.1:%d for %s, through %s:%s",
              port, ", ".join(config.PROXY_DOMAINS), upstream.hostname, upstream.port)
    try:
        yield port
    finally:
        # Not wait_closed(): it waits for every connection, and the browser is gone by now.
        server.close()
