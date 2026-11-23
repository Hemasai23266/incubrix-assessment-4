"""Secure URL input: download a file from a URL the CALLER supplied and store it (Integration Standard point 7).

The service must never be turned into a proxy for internal networks (SSRF), so every protection is enforced on the CONNECTION, not
just on the URL text:

1. http/https only; port allow-list (MEDIA_FETCH_ALLOWED_PORTS); no credentials in the URL; no internal/odd host spellings.
2. The host name is resolved ONCE per hop; every returned address must be globally routable; the TCP connection then goes to that
   validated address (Host header and TLS name check use the original host name), so DNS rebinding cannot swap in a private address.
3. Redirects are followed manually, at most MEDIA_FETCH_MAX_REDIRECTS times; EVERY hop is validated again from scratch (scheme, port,
   DNS, addresses). A redirect from https to http is refused.
4. Timeouts: connect, read, and an overall deadline (a trickling server cannot hold a worker).
5. Hard size limit (MEDIA_FETCH_MAX_BYTES): Content-Length is checked first and the stream is cut off the moment the limit is passed.
   Content-Encoding other than identity is refused, so a compressed bomb cannot expand beyond the limit.
6. Content type allow-list (MEDIA_FETCH_ALLOWED_TYPES) AND the downloaded bytes must match the declared type (magic numbers).
7. The stored file name is built here (sanitised, content-addressed folder); nothing from the URL or the remote server becomes a path.
8. Logs and results never contain the URL's query string, fragment or credentials: only scheme, host and path of the original URL.
9. The request carries no cookies and no Authorization header (a fresh client; the service's own credentials are never forwarded);
   proxies from the environment are ignored.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from app import netguard, storage
from app.config import get_settings
from app.errors import AppError
from app.logging_setup import log

logger = logging.getLogger("incubrix.fetcher")
for _name in ("httpx", "httpcore"):  # their INFO/DEBUG lines contain the full request URL incl. the query string
    logging.getLogger(_name).setLevel(logging.WARNING)
TRANSPORT: httpx.BaseTransport | None = None  # tests inject a MockTransport; production leaves it None
VERIFY: bool | str = True  # tests may point this at a private CA file
CHUNK = 64 * 1024
USER_AGENT = "IncuBrix-Fetcher/1.0"
EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp", "video/mp4": ".mp4", "text/plain": ".txt"}
ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}


@dataclass
class FetchResult:
    key: str
    download_url: str
    size: int
    content_type: str
    sha256: str
    filename: str
    source: str
    redirects: int
    seconds: float


def sanitized_source(url: str) -> str:
    """scheme://host[:port]/path only: never the query string, fragment or credentials (they may hold tokens)."""
    try:
        p = urlsplit(url)
        host = p.hostname or "?"
        port = f":{p.port}" if p.port else ""
        return f"{p.scheme}://{host}{port}{(p.path or '/')[:120]}"
    except ValueError:
        return "(unparseable url)"


def detect_type(data: bytes) -> str | None:
    """Content sniffing by magic numbers; None when the bytes are not one of the supported kinds."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        return "video/mp4"
    if data and b"\x00" not in data[:8192]:
        try:
            data[:65536].decode("utf-8")
            return "text/plain"
        except UnicodeDecodeError:
            return None
    return None


def safe_filename(candidate: str | None, content_type: str) -> str:
    """Never trust a remote or caller-supplied name: strip any directory part, keep a conservative character set, force the extension."""
    name = (candidate or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9._-]", "_", name)).lstrip(".")[:80]
    ext = EXT.get(content_type, "")
    stem = name.rsplit(".", 1)[0] if "." in name else name
    stem = stem.strip("._-") or "download"
    return f"{stem}{ext}"


def _allowed_types() -> set[str]:
    return {t.strip().lower() for t in get_settings().media_fetch_allowed_types.split(",") if t.strip()}


def _reject(reason: str, **details) -> AppError:
    return AppError("MEDIA_URL_REJECTED", f"URL rejected: {reason}", {"reason": reason, **details}, 422)


def _failed(message: str, retryable: bool = True, **details) -> AppError:
    return AppError("MEDIA_FETCH_FAILED", message, details, 502, retryable=retryable)


def check_url(url: str) -> netguard.Target:
    """Submission-time check (also the first step of every hop)."""
    try:
        return netguard.validate_fetch_url(url)
    except netguard.UrlRejected as e:
        raise _reject(e.reason)


def fetch_to_storage(url: str, filename: str | None, storage_prefix: str = "fetched") -> FetchResult:
    s = get_settings()
    started = time.monotonic()
    deadline = started + s.media_fetch_total_timeout
    max_bytes = s.media_fetch_max_bytes
    timeout = httpx.Timeout(connect=s.media_fetch_connect_timeout, read=s.media_fetch_read_timeout, write=5.0, pool=5.0)
    current, previous_scheme = url, None
    redirects = 0
    log(logger, logging.INFO, "fetch started", source=sanitized_source(url))

    while True:
        target = check_url(current)  # every hop starts from scratch: scheme, port, DNS, all addresses
        if previous_scheme == "https" and target.scheme == "http":
            raise _reject("redirect from https to http is refused")
        host_header = target.host if target.port == (443 if target.scheme == "https" else 80) else f"{target.host}:{target.port}"
        headers = {"Host": host_header, "User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"}
        ext = {"sni_hostname": target.host} if target.scheme == "https" else {}
        location = None
        last_error: Exception | None = None
        for ip in target.ips[:3]:
            try:
                with httpx.Client(transport=TRANSPORT, timeout=timeout, follow_redirects=False, trust_env=False, verify=VERIFY) as client:
                    with client.stream("GET", netguard._pinned_url(target, ip), headers=headers, extensions=ext) as r:
                        if 300 <= r.status_code < 400:
                            location = r.headers.get("location")
                            if not location:
                                raise _failed(f"remote redirect without a Location header (HTTP {r.status_code})", retryable=False)
                            break
                        if r.status_code != 200:
                            raise _failed(f"remote answered HTTP {r.status_code}", retryable=r.status_code >= 500 or r.status_code == 429,
                                          remote_status=r.status_code)
                        return _store(r, current, filename, max_bytes, deadline, redirects, started)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                last_error = e
                continue
            except httpx.TimeoutException as e:
                raise _failed(f"timed out ({type(e).__name__})")
            except httpx.TransportError as e:
                raise _failed(f"network error ({type(e).__name__})")
        else:
            raise _failed(f"could not connect ({type(last_error).__name__ if last_error else 'no address'})")

        redirects += 1
        if redirects > s.media_fetch_max_redirects:
            raise _reject(f"more than {s.media_fetch_max_redirects} redirects")
        previous_scheme = target.scheme
        current = urljoin(current, location)  # relative redirects resolve against the current URL, then are validated like any URL


def _store(r: httpx.Response, source_url: str, filename: str | None, max_bytes: int, deadline: float, redirects: int, started: float) -> FetchResult:
    enc = (r.headers.get("content-encoding") or "identity").strip().lower()
    if enc != "identity":
        raise AppError("MEDIA_TYPE_REJECTED", "compressed (Content-Encoding) responses are not accepted", {"content_encoding": enc[:30]}, 422)
    declared = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    declared = ALIASES.get(declared, declared)
    if declared not in _allowed_types():
        raise AppError("MEDIA_TYPE_REJECTED", f"content type '{declared[:60] or 'missing'}' is not allowed", {"allowed": sorted(_allowed_types())}, 422)
    cl = r.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > max_bytes:
        raise AppError("MEDIA_TOO_LARGE", f"remote file is {cl} bytes; the limit is {max_bytes}", {"limit_bytes": max_bytes}, 413)
    buf = bytearray()
    for chunk in r.iter_raw():
        buf += chunk
        if len(buf) > max_bytes:
            raise AppError("MEDIA_TOO_LARGE", f"remote file exceeds the {max_bytes}-byte limit", {"limit_bytes": max_bytes}, 413)
        if time.monotonic() > deadline:
            raise _failed("overall download deadline exceeded")
    data = bytes(buf)
    if not data:
        raise AppError("MEDIA_TYPE_REJECTED", "the remote file is empty", {}, 422)
    detected = detect_type(data)
    if detected != declared:
        raise AppError("MEDIA_TYPE_REJECTED", f"downloaded bytes look like '{detected or 'unknown'}' but the server declared '{declared}'",
                       {"declared": declared, "detected": detected}, 422)
    digest = hashlib.sha256(data).hexdigest()
    path = urlsplit(source_url).path
    name = safe_filename(filename or path, declared)
    key = f"fetched/{digest[:16]}/{name}"
    url = storage.put_bytes(key, data, declared)
    seconds = round(time.monotonic() - started, 4)
    log(logger, logging.INFO, "fetch stored", source=sanitized_source(source_url), size=len(data), content_type=declared, redirects=redirects)
    return FetchResult(key=key, download_url=url, size=len(data), content_type=declared, sha256=digest, filename=name,
                       source=sanitized_source(source_url), redirects=redirects, seconds=seconds)


def run_fetch_job(payload: dict, started: float) -> dict:
    """Worker side of POST /api/v1/files/from-url: download, store, and return the result with the standard manifest."""
    from app import runtime
    s = get_settings()
    r = fetch_to_storage(payload["url"], payload.get("filename"))
    manifest = runtime.make_manifest(
        "media_fetch", started, [],
        providers=[{"name": "http-fetch", "mode": "live", "version": f"httpx {httpx.__version__}"}],
        rights={"licence": "unknown: remote content supplied by the caller; the caller is responsible for the right to use it",
                "ai_generated": None, "disclosure_included": None},
        extra_settings={"max_bytes": s.media_fetch_max_bytes, "allowed_types": sorted(_allowed_types()),
                        "allowed_ports": sorted(netguard._ports_from(s.media_fetch_allowed_ports)), "max_redirects": s.media_fetch_max_redirects,
                        "timeouts_s": {"connect": s.media_fetch_connect_timeout, "read": s.media_fetch_read_timeout, "total": s.media_fetch_total_timeout}},
        step_timings={"download_and_store_s": r.seconds})
    return {"status": "stored", "key": r.key, "download_url": r.download_url, "size": r.size, "content_type": r.content_type,
            "sha256": r.sha256, "filename": r.filename, "source": r.source, "redirects": r.redirects, "manifest": manifest}
