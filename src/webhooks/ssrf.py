"""SSRF guard for tenant-supplied webhook URLs.

Tenants choose where we send HTTP requests, so without this a tenant could aim us at
http://169.254.169.254/ (cloud credentials) or at services on our private network.

- Only http(s) on ports 80/443, and no user:password@ in the URL.
- The hostname is resolved by us, and EVERY address it resolves to must be public.
- DNS rebinding: a hostname can resolve to a public IP when checked and a private one a
  moment later when connected to. So `resolve()` returns a Target pinned to the IP we
  validated; the caller connects to that IP, sending the original Host header and TLS SNI,
  so the certificate is still verified against the hostname.
- Redirects are never followed (the worker's client has follow_redirects=False).
"""

import asyncio
import ipaddress
import os
import socket
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit

# Dev-only escape hatch: compose sets it to the demo receiver. Never set it in production.
ALLOW_HOSTS = frozenset(
    h.strip().lower() for h in os.environ.get("SSRF_ALLOW_HOSTS", "").split(",") if h.strip()
)


class BlockedURL(ValueError):
    """The URL points somewhere we must never send requests. Not worth retrying."""


class UnresolvableHost(OSError):
    """DNS lookup failed. May be temporary, so the worker retries."""


@dataclass(frozen=True)
class Target:
    url: str  # what to connect to: the validated IP, unless the host is allowlisted
    host: str  # Host header: the hostname (and port) the tenant registered
    sni: str | None  # TLS server name, so the cert is checked against the hostname


def check_static(url: str) -> SplitResult:
    """Checks that need no DNS. Raises ValueError (which pydantic turns into a 422)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("url must be http(s) with a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("url must not contain credentials")
    if parts.port not in (None, 80, 443):  # .port raises ValueError for junk like :99999
        raise ValueError("only ports 80 and 443 are allowed")
    return parts


def is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # ::ffff:127.0.0.1 is 127.0.0.1 in disguise; judge the embedded IPv4 address. Python
    # 3.13 does this itself, older releases may not, so don't depend on the version.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    # is_global excludes loopback, private, link-local (169.254/16, fe80::/10), CGNAT,
    # unique-local (fc00::/7), unspecified (0.0.0.0, ::) and reserved ranges.
    # ponytail: 6to4/Teredo IPv6 can also embed IPv4; unwrap them if IPv6 egress is enabled
    return ip.is_global and not ip.is_multicast


async def _getaddrinfo(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def resolve(url: str) -> Target:
    try:
        parts = check_static(url)
    except ValueError as exc:
        raise BlockedURL(str(exc)) from exc
    host = parts.hostname or ""
    host_header = host if parts.port is None else f"{host}:{parts.port}"
    if host in ALLOW_HOSTS:
        return Target(url, host_header, None)

    try:
        addrs = await _getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError as exc:
        raise UnresolvableHost(f"cannot resolve {host}") from exc
    ips = [ipaddress.ip_address(a.split("%")[0]) for a in addrs]  # drop IPv6 zone ids
    if not ips:
        raise UnresolvableHost(f"cannot resolve {host}")
    for ip in ips:
        if not is_public(ip):
            raise BlockedURL(f"{host} resolves to non-public address {ip}")

    netloc = f"[{ips[0]}]" if ips[0].version == 6 else str(ips[0])
    if parts.port is not None:
        netloc += f":{parts.port}"
    return Target(
        urlunsplit((parts.scheme, netloc, parts.path, parts.query, "")), host_header, host
    )
