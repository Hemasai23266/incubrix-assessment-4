"""Outbound-request safety for job callbacks (SSRF protection).

`callback_url` is supplied by API callers, so the server must never be tricked into calling internal services. Rules:

* HTTPS only (HTTP only when CALLBACK_ALLOW_HTTP=true); port must be in CALLBACK_ALLOWED_PORTS (default 443, 8443).
* No credentials in the URL, no whitespace/control characters or backslashes, at most 2048 characters.
* Host names: no `localhost`, no internal suffixes (.local, .internal, .localdomain, .lan, .home, .corp, ...), no single-label
  names (compose service names such as `redis` or `db`), no non-canonical IP spellings (decimal, hex, octal).
* DNS: every address the name resolves to must be globally routable. Loopback, private (10/8, 172.16/12, 192.168/16), link-local
  (169.254/16 incl. cloud metadata 169.254.169.254), CGNAT, multicast, reserved, unspecified and IPv6 equivalents are refused;
  IPv4-mapped / NAT64 / 6to4 / Teredo IPv6 forms are unwrapped or refused. One private address among several is enough to refuse.
* DNS rebinding: the name is resolved once, validated, and the connection is then made to THAT address (the original host name is
  only used for the Host header and TLS SNI/certificate check), so a second, different DNS answer is never used.
* Redirects are never followed, environment proxies are ignored, and the response body is never read or stored.

CALLBACK_ALLOW_PRIVATE / MEDIA_FETCH_ALLOW_PRIVATE switch the address checks off for local development only; the single-container demo image refuses to start with them.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.config import get_settings

MAX_URL_LEN = 2048
BLOCKED_NAMES = {"localhost", "metadata", "instance-data", "ip6-localhost", "ip6-loopback", "broadcasthost"}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".lan", ".home", ".corp", ".intranet", ".private",
                    ".home.arpa", ".svc", ".cluster.local")
_DANGEROUS_CHARS = re.compile(r"[\s\x00-\x1f\x7f\\]")
_NON_CANONICAL_IP = re.compile(r"^[0-9a-fx.]+$")  # digits/hex/dots only: must then be a canonical dotted-quad
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")
_TEREDO = ipaddress.ip_network("2001::/32")


class UrlRejected(Exception):
    """A URL failed the outbound-request safety checks (used for callbacks and for media URL fetching)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


CallbackRejected = UrlRejected  # original name, kept for existing imports


@dataclass
class Target:
    url: str
    scheme: str
    host: str
    port: int
    path_query: str
    ips: list[str]


def _ports_from(csv: str) -> set[int]:
    return {int(p.strip()) for p in csv.split(",") if p.strip().isdigit()}


def _allowed_ports() -> set[int]:
    return _ports_from(get_settings().callback_allowed_ports)


def ip_is_public(ip_text: str) -> bool:
    """True only for a globally routable unicast address (after unwrapping IPv6-embedded IPv4)."""
    try:
        ip = ipaddress.ip_address(ip_text.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip_is_public(str(ip.ipv4_mapped))
        if ip in _NAT64:
            return ip_is_public(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
        if ip in _SIX_TO_FOUR:
            return ip_is_public(str(ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)))
        if ip in _TEREDO:
            return False
    return bool(ip.is_global) and not (ip.is_multicast or ip.is_loopback or ip.is_link_local or ip.is_private
                                       or ip.is_reserved or ip.is_unspecified)


def _resolve(host: str, port: int) -> list[str]:
    """getaddrinfo with a hard timeout (a hanging resolver must not hang the API)."""
    result: list = []

    def work():
        try:
            result.extend(info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError:
            pass

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(get_settings().callback_dns_timeout)
    return list(dict.fromkeys(result))  # de-duplicated, order kept


def _validate(url: str, *, label: str, allow_http: bool, ports: set[int], dev: bool) -> Target:
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LEN:
        raise UrlRejected(f"{label} is empty or longer than 2048 characters")
    if _DANGEROUS_CHARS.search(url):
        raise UrlRejected(f"{label} contains whitespace, control characters or backslashes")
    try:
        parts = urlsplit(url)
        port_explicit = parts.port
    except ValueError:
        raise UrlRejected(f"{label} is not a valid URL")
    scheme = (parts.scheme or "").lower()
    if scheme not in ("https", "http") or (scheme == "http" and not (allow_http or dev)):
        raise UrlRejected(f"{label} must use {'http or https' if allow_http else 'https'}")
    if parts.username is not None or parts.password is not None:
        raise UrlRejected(f"{label} must not contain credentials")
    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise UrlRejected(f"{label} has no host")
    port = port_explicit or (443 if scheme == "https" else 80)
    allowed = set(ports) | ({80} if scheme == "http" and allow_http else set())
    if not dev and port not in allowed:
        raise UrlRejected(f"{label} port {port} is not allowed (allowed: {sorted(allowed)})")

    is_ip = True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        is_ip = False
    if not dev:
        if host in BLOCKED_NAMES or host.endswith(BLOCKED_SUFFIXES):
            raise UrlRejected(f"{label} host is an internal or local name")
        if not is_ip and "." not in host:
            raise UrlRejected(f"{label} host is a single-label (internal) name")
        if not is_ip and _NON_CANONICAL_IP.match(host):
            raise UrlRejected(f"{label} host is a non-canonical IP address spelling")

    ips = [host] if is_ip else _resolve(host, port)
    if not ips:
        raise UrlRejected(f"{label} host does not resolve")
    if not dev and not all(ip_is_public(ip) for ip in ips):
        raise UrlRejected(f"{label} host resolves to a non-public address")
    path_query = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return Target(url=url, scheme=scheme, host=host, port=port, path_query=path_query, ips=ips)


def validate_callback_url(url: str) -> Target:
    s = get_settings()
    return _validate(url, label="callback_url", allow_http=s.callback_allow_http, ports=_allowed_ports(), dev=s.callback_allow_private)


def validate_fetch_url(url: str) -> Target:
    """Same rules for URLs the SERVICE is asked to download (http or https, own port allow-list)."""
    s = get_settings()
    return _validate(url, label="url", allow_http=s.media_fetch_allow_http, ports=_ports_from(s.media_fetch_allowed_ports), dev=s.media_fetch_allow_private)


def _pinned_url(t: Target, ip: str) -> str:
    ip_part = f"[{ip}]" if ":" in ip else ip
    return f"{t.scheme}://{ip_part}:{t.port}{t.path_query}"


def safe_post(url: str, body: bytes, headers: dict[str, str], *, timeout: float = 5.0,
              transport: httpx.BaseTransport | None = None, verify: bool | str = True) -> int:
    """Validate, resolve once, then POST to the validated address. Returns the HTTP status; raises CallbackRejected or httpx errors."""
    t = validate_callback_url(url)
    default_port = 443 if t.scheme == "https" else 80
    host_header = t.host if t.port == default_port else f"{t.host}:{t.port}"
    last: Exception | None = None
    for ip in t.ips[:3]:
        try:
            with httpx.Client(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False, verify=verify) as c:
                req_headers = {**headers, "Host": host_header}
                ext = {"sni_hostname": t.host} if t.scheme == "https" else {}
                with c.stream("POST", _pinned_url(t, ip), content=body, headers=req_headers, extensions=ext) as r:
                    return r.status_code  # the body is deliberately never read
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last = e
    raise last or httpx.ConnectError("no address could be reached")
