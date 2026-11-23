"""Integration Standard point 7: URL input is fetched by the service. Every protection is tested, including the malicious cases.
Nothing here contacts a real private or metadata endpoint: hosts resolve through a fake resolver and requests go to a MockTransport or
to a throw-away server on 127.0.0.1 (only with the explicit development switch)."""
import http.server
import json
import logging
import pathlib
import re
import threading
import time

import httpx
import pytest

from app import fetcher, netguard, storage
from app.config import get_settings
from app.errors import AppError
from app.logging_setup import JsonFormatter
from tests.conftest import H, job, uid

PUBLIC = "93.184.216.34"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBP" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
TXT = "hello from a remote text file हिन्दी".encode()
SAMPLES = {"image/png": PNG, "image/jpeg": JPEG, "image/gif": GIF, "image/webp": WEBP, "video/mp4": MP4, "text/plain": TXT}


def resp(status=200, headers=None, content=b""):
    """A mock response that STREAMS like a real one (httpx.Response(content=bytes) is pre-read and cannot be iterated)."""
    if isinstance(content, (bytes, bytearray)):
        return httpx.Response(status, headers=headers or {}, stream=httpx.ByteStream(bytes(content)))
    return httpx.Response(status, headers=headers or {}, content=content)  # an iterator: genuinely streamed


class Env:
    def __init__(self):
        self.dns: dict[str, list[str]] = {}
        self.dns_calls: list[str] = []
        self.requests: list[httpx.Request] = []
        self.handler = lambda request: resp(200, headers={"content-type": "image/png"}, content=PNG)

    def resolve(self, host, port):
        self.dns_calls.append(host)
        return list(self.dns.get(host, [PUBLIC]))

    def transport(self):
        def handle(request):
            self.requests.append(request)
            return self.handler(request)
        return httpx.MockTransport(handle)


@pytest.fixture
def env(monkeypatch):
    e = Env()
    monkeypatch.setattr(netguard, "_resolve", e.resolve)
    monkeypatch.setattr(fetcher, "TRANSPORT", e.transport())
    return e


def err_code(url, filename=None):
    with pytest.raises(AppError) as ei:
        fetcher.fetch_to_storage(url, filename)
    return ei.value


# ===================================================================== happy path and the request that is actually sent
def test_download_is_pinned_to_the_validated_ip_and_sends_no_internal_headers(env):
    r = fetcher.fetch_to_storage("https://img.example.com/pics/cat.png?size=large", None)
    assert r.content_type == "image/png" and r.size == len(PNG) and r.key.startswith("fetched/") and r.key.endswith("/cat.png")
    assert storage.get_bytes(r.key)[0] == PNG
    req = env.requests[0]
    assert req.url.host == PUBLIC and req.url.path == "/pics/cat.png" and req.url.query == b"size=large"  # connected to the IP, not the name
    assert req.headers["host"] == "img.example.com" and req.extensions["sni_hostname"] == "img.example.com"
    assert req.headers["accept-encoding"] == "identity" and req.headers["user-agent"] == fetcher.USER_AGENT
    for forbidden in ("authorization", "cookie", "proxy-authorization", "x-incubrix-signature", "x-correlation-id"):
        assert forbidden not in req.headers
    assert env.dns_calls == ["img.example.com"]  # resolved exactly once


@pytest.mark.parametrize("ctype", sorted(SAMPLES))
def test_each_allowed_type_downloads_when_declared_type_and_bytes_agree(env, ctype):
    env.handler = lambda r: resp(200, headers={"content-type": ctype}, content=SAMPLES[ctype])
    res = fetcher.fetch_to_storage("https://cdn.example.com/file", "My File!.bin")
    assert res.content_type == ctype and res.filename.endswith({"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
                                                                 "video/mp4": ".mp4", "text/plain": ".txt"}[ctype])


def test_content_type_parameters_and_jpg_alias_are_accepted(env):
    env.handler = lambda r: resp(200, headers={"content-type": "Image/JPG; charset=binary"}, content=JPEG)
    assert fetcher.fetch_to_storage("https://cdn.example.com/a", None).content_type == "image/jpeg"


def test_ipv6_literal_is_pinned_with_brackets(env):
    fetcher.fetch_to_storage("https://[2606:2800:220:1:248:1893:25c8:1946]/x.png", None)
    assert env.requests[0].url.host == "2606:2800:220:1:248:1893:25c8:1946"


def test_http_is_allowed_by_default_but_can_be_switched_off(env, monkeypatch):
    assert fetcher.fetch_to_storage("http://cdn.example.com/x.png", None).size == len(PNG)
    monkeypatch.setattr(get_settings(), "media_fetch_allow_http", False)
    assert err_code("http://cdn.example.com/x.png").code == "MEDIA_URL_REJECTED"
    assert fetcher.fetch_to_storage("https://cdn.example.com/x.png", None).size == len(PNG)


# ===================================================================== SSRF: addresses, names, schemes, ports
BAD = [
    ("file scheme", "file:///etc/passwd"), ("ftp", "ftp://example.com/x"), ("gopher", "gopher://example.com/_x"), ("data URI", "data:image/png;base64,AAAA"),
    ("javascript", "javascript:alert(1)"), ("credentials", "https://user:pass@example.com/x.png"), ("user only", "https://admin@example.com/x.png"),
    ("no host", "https:///x.png"), ("localhost", "http://localhost/x.png"), ("localhost dot", "http://localhost./x.png"), ("LOCALHOST", "http://LOCALHOST/x"),
    ("sub of localhost", "http://a.localhost/x"), ("loopback", "http://127.0.0.1/x.png"), ("loopback range", "http://127.1.2.3/x"),
    ("loopback 8080", "http://127.0.0.1:8080/x"), ("loopback v6", "http://[::1]/x"), ("any address", "http://0.0.0.0/x"), ("any v6", "http://[::]/x"),
    ("10/8", "http://10.0.0.5/x"), ("172.16/12", "http://172.16.0.9/x"), ("172.31", "http://172.31.255.254/x"), ("192.168/16", "http://192.168.1.1/x"),
    ("link-local", "http://169.254.10.10/x"), ("metadata IP", "http://169.254.169.254/latest/meta-data/"),
    ("metadata IP :80", "http://169.254.169.254:80/latest/meta-data/iam/security-credentials/"), ("metadata IP :8080", "http://169.254.169.254:8080/"),
    ("metadata https", "https://169.254.169.254/computeMetadata/v1/"), ("gcp name", "http://metadata.google.internal/computeMetadata/v1/"),
    ("short metadata name", "http://metadata/x"), ("aws ipv6 metadata", "http://[fd00:ec2::254]/latest/"), ("azure/oracle metadata", "http://169.254.169.254/metadata/instance"),
    ("alibaba metadata", "http://100.100.100.200/latest/meta-data/"), ("CGNAT", "http://100.64.0.1/x"), ("198.18/15", "http://198.18.0.1/x"),
    ("doc range", "http://192.0.2.1/x"), ("multicast", "http://224.0.0.1/x"), ("broadcast", "http://255.255.255.255/x"), ("reserved 240/4", "http://240.0.0.1/x"),
    ("ULA", "http://[fc00::1]/x"), ("link-local v6", "http://[fe80::1]/x"), ("v4-mapped loopback", "http://[::ffff:127.0.0.1]/x"),
    ("v4-mapped private", "http://[::ffff:10.0.0.1]/x"), ("v4-mapped hex", "http://[::ffff:7f00:1]/x"), ("NAT64 loopback", "http://[64:ff9b::7f00:1]/x"),
    ("6to4 private", "http://[2002:0a00:0001::1]/x"), ("teredo", "http://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/x"),
    ("decimal IP", "http://2130706433/x"), ("hex IP", "http://0x7f000001/x"), ("hex dotted", "http://0x7f.0x0.0x0.0x1/x"), ("octal", "http://0177.0.0.1/x"),
    ("mixed", "http://0x7f.1/x"), ("short form", "http://127.1/x"), (".internal", "http://db.internal/x"), (".local", "http://printer.local/x"),
    ("k8s", "http://api.default.svc.cluster.local/x"), ("single label redis", "http://redis/x"), ("single label db", "http://db/x"), ("single label mocks", "http://mocks/x"),
    ("port 22", "http://example.com:22/x"), ("port 25", "http://example.com:25/x"), ("port 3306", "http://example.com:3306/x"), ("port 5432", "http://example.com:5432/x"),
    ("port 6379", "http://example.com:6379/x"), ("port 9100", "http://example.com:9100/x"), ("port 8000", "http://example.com:8000/x"),
    ("port 11211", "http://example.com:11211/x"),
    ("space", "http://example.com/a b"), ("newline", "http://example.com/a\r\nHost: evil"), ("backslash", "http://example.com\\@169.254.169.254/"),
    ("tab", "http://example.com/a\tb"), ("empty", ""), ("too long", "http://example.com/" + "a" * 2100), ("bad port", "http://example.com:99999/x"),
    ("non-numeric port", "http://example.com:abc/x"),
]


@pytest.mark.parametrize("label,url", BAD, ids=[b[0] for b in BAD])
def test_unsafe_urls_are_rejected_before_any_connection(env, label, url):
    e = err_code(url)
    assert e.code == "MEDIA_URL_REJECTED" and e.retryable is False and e.status_code == 422
    assert env.requests == []  # nothing was sent anywhere
    for secret in ("pass", "admin"):
        if "@" in url:
            assert secret not in e.message  # credentials in the URL are never echoed back


@pytest.mark.parametrize("ips", [["127.0.0.1"], ["10.1.2.3"], ["169.254.169.254"], ["192.168.0.10"], ["::1"], ["fd00::5"], ["100.64.1.1"], ["fe80::1"],
                                 [PUBLIC, "10.0.0.1"], ["10.0.0.1", PUBLIC], [PUBLIC, "::1"], ["::ffff:127.0.0.1"], []],
                         ids=["loopback", "private", "metadata", "lan", "v6 loopback", "ULA", "CGNAT", "v6 link-local", "mixed public+private", "mixed private+public",
                              "mixed v4+v6 loopback", "mapped loopback", "unresolvable"])
def test_a_name_that_resolves_to_any_non_public_address_is_rejected(env, ips):
    env.dns["sneaky.example.com"] = ips
    assert err_code("https://sneaky.example.com/x.png").code == "MEDIA_URL_REJECTED"
    assert env.requests == []


# ===================================================================== redirects: every hop is validated again
def redirect_to(location, status=302):
    return lambda r: httpx.Response(status, headers={"location": location})


@pytest.mark.parametrize("target", ["http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:8000/api/v1/destinations", "http://localhost/admin",
                                    "http://[::1]/", "http://10.0.0.5/", "file:///etc/passwd", "ftp://example.com/x", "http://redis:6379/",
                                    "https://example.com:22/x", "http://metadata.google.internal/", "http://2130706433/", "//169.254.169.254/x"],
                         ids=["metadata", "loopback", "localhost", "v6 loopback", "private", "file", "ftp", "compose service", "ssh port", "gcp name", "decimal ip", "scheme-relative metadata"])
def test_redirect_to_a_forbidden_target_is_refused_without_connecting(env, target):
    env.handler = redirect_to(target)
    e = err_code("http://good.example.com/start.png")
    assert e.code == "MEDIA_URL_REJECTED"
    assert len(env.requests) == 1  # only the first hop was ever contacted


def test_redirect_to_a_name_resolving_privately_is_refused(env):
    env.dns["alias.example.com"] = ["10.0.0.5"]
    env.handler = redirect_to("http://alias.example.com/x.png")
    assert err_code("http://good.example.com/start.png").code == "MEDIA_URL_REJECTED" and len(env.requests) == 1


def test_dns_rebinding_between_hops_is_caught(env, monkeypatch):
    answers = iter([[PUBLIC], ["169.254.169.254"]])  # first lookup public, the second (next hop, same name) is the metadata address
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: next(answers))
    env.handler = redirect_to("http://rebind.example.com/next.png")
    assert err_code("http://rebind.example.com/start.png").code == "MEDIA_URL_REJECTED"
    assert len(env.requests) == 1


def test_a_relative_redirect_is_followed_after_validation(env):
    def handler(r):
        return httpx.Response(302, headers={"location": "/final/pic.png"}) if r.url.path == "/start" else resp(200, headers={"content-type": "image/png"}, content=PNG)
    env.handler = handler
    res = fetcher.fetch_to_storage("https://cdn.example.com/start", None)
    assert res.redirects == 1 and len(env.requests) == 2 and env.requests[1].url.path == "/final/pic.png"
    assert env.dns_calls == ["cdn.example.com", "cdn.example.com"]  # the second hop resolved and validated again


def test_redirect_chain_is_bounded(env):
    env.handler = lambda r: httpx.Response(302, headers={"location": "/loop"})
    e = err_code("https://cdn.example.com/loop")
    assert e.code == "MEDIA_URL_REJECTED" and "redirects" in e.message
    assert len(env.requests) == get_settings().media_fetch_max_redirects + 1


def test_https_to_http_downgrade_is_refused(env):
    env.handler = redirect_to("http://cdn.example.com/x.png")
    e = err_code("https://cdn.example.com/start")
    assert e.code == "MEDIA_URL_REJECTED" and "https to http" in e.message and len(env.requests) == 1


def test_redirect_without_location_is_a_non_retryable_failure(env):
    env.handler = lambda r: httpx.Response(302)
    e = err_code("https://cdn.example.com/x")
    assert e.code == "MEDIA_FETCH_FAILED" and e.retryable is False


# ===================================================================== size limits
def stream_of(n_chunks, chunk=1024, counter=None):
    def gen():
        for i in range(n_chunks):
            if counter is not None:
                counter.append(chunk)
            yield (PNG + b"\x00" * (chunk - len(PNG))) if i == 0 else b"\x00" * chunk
    return gen()


def test_oversized_content_length_is_refused_before_reading_the_body(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "media_fetch_max_bytes", 4096)
    consumed = []
    env.handler = lambda r: resp(200, headers={"content-type": "image/png", "content-length": "999999"}, content=stream_of(1000, counter=consumed))
    e = err_code("https://cdn.example.com/big.png")
    assert e.code == "MEDIA_TOO_LARGE" and e.status_code == 413 and e.retryable is False and consumed == []


def test_unbounded_stream_without_content_length_is_cut_off_at_the_limit(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "media_fetch_max_bytes", 4096)
    consumed = []
    env.handler = lambda r: resp(200, headers={"content-type": "image/png"}, content=stream_of(10_000, counter=consumed))
    assert err_code("https://cdn.example.com/endless.png").code == "MEDIA_TOO_LARGE"
    assert sum(consumed) <= 4096 + 2048  # stopped within a couple of chunks of the limit instead of downloading 10 MB


def test_a_lying_content_length_cannot_bypass_the_limit(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "media_fetch_max_bytes", 2048)
    env.handler = lambda r: resp(200, headers={"content-type": "image/png", "content-length": "10"}, content=PNG * 100)
    # httpx itself refuses a body longer than the declared length; either way the service must not store it
    with pytest.raises((AppError, httpx.HTTPError)):
        fetcher.fetch_to_storage("https://cdn.example.com/x.png", None)


def test_size_exactly_at_the_limit_is_accepted_and_one_byte_more_is_not(env, monkeypatch):
    body = PNG + b"\x00" * (4096 - len(PNG))
    monkeypatch.setattr(get_settings(), "media_fetch_max_bytes", 4096)
    env.handler = lambda r: resp(200, headers={"content-type": "image/png"}, content=body)
    assert fetcher.fetch_to_storage("https://cdn.example.com/a.png", None).size == 4096
    env.handler = lambda r: resp(200, headers={"content-type": "image/png"}, content=body + b"\x00")
    assert err_code("https://cdn.example.com/a.png").code == "MEDIA_TOO_LARGE"


# ===================================================================== content type and content validation
@pytest.mark.parametrize("headers,body,why", [
    ({"content-type": "text/html"}, b"<html><script>alert(1)</script></html>", "html"),
    ({"content-type": "application/octet-stream"}, PNG, "octet-stream"),
    ({"content-type": "application/pdf"}, b"%PDF-1.4", "pdf"),
    ({"content-type": "image/svg+xml"}, b"<svg onload='alert(1)'/>", "svg (script vector)"),
    ({"content-type": "application/x-sh"}, b"#!/bin/sh\nrm -rf /", "shell script"),
    ({}, PNG, "missing type"),
    ({"content-type": "image/png", "content-encoding": "gzip"}, PNG, "gzip (decompression bomb)"),
    ({"content-type": "image/png", "content-encoding": "br"}, PNG, "brotli"),
])
def test_disallowed_content_types_and_encodings_are_rejected(env, headers, body, why):
    env.handler = lambda r: resp(200, headers=headers, content=body)
    e = err_code("https://cdn.example.com/x")
    assert e.code == "MEDIA_TYPE_REJECTED" and e.retryable is False, why


@pytest.mark.parametrize("declared,body", [("image/png", JPEG), ("image/jpeg", PNG), ("image/gif", b"<html>not a gif</html>"), ("video/mp4", PNG),
                                           ("text/plain", b"binary\x00\x01\x02 data"), ("text/plain", b"\xff\xfe\xfa\xfb invalid utf8"), ("image/png", b"")])
def test_declared_type_must_match_the_downloaded_bytes(env, declared, body):
    env.handler = lambda r: resp(200, headers={"content-type": declared}, content=body)
    assert err_code("https://cdn.example.com/x").code == "MEDIA_TYPE_REJECTED"


def test_nothing_is_stored_when_validation_fails(env):
    before = sorted(pathlib.Path(get_settings().local_store_dir).rglob("*"))
    env.handler = lambda r: resp(200, headers={"content-type": "image/png"}, content=JPEG)
    err_code("https://cdn.example.com/x")
    assert sorted(pathlib.Path(get_settings().local_store_dir).rglob("*")) == before


# ===================================================================== remote errors and timeouts
@pytest.mark.parametrize("status,retryable", [(500, True), (502, True), (503, True), (429, True), (404, False), (403, False), (401, False), (410, False)])
def test_remote_http_errors_map_to_retryable_or_not(env, status, retryable):
    env.handler = lambda r: httpx.Response(status)
    e = err_code("https://cdn.example.com/x")
    assert e.code == "MEDIA_FETCH_FAILED" and e.retryable is retryable and e.details["remote_status"] == status


def test_read_timeout_is_a_retryable_failure(env):
    def boom(r):
        raise httpx.ReadTimeout("slow", request=r)
    env.handler = boom
    e = err_code("https://cdn.example.com/x")
    assert e.code == "MEDIA_FETCH_FAILED" and e.retryable is True and "timed out" in e.message


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ConnectTimeout])
def test_connection_failures_try_each_validated_address_then_fail(env, exc):
    env.dns["multi.example.com"] = [PUBLIC, "93.184.216.35", "93.184.216.36", "93.184.216.37"]

    def boom(r):
        raise exc("no route", request=r)
    env.handler = boom
    e = err_code("https://multi.example.com/x")
    assert e.code == "MEDIA_FETCH_FAILED" and e.retryable is True
    assert [rq.url.host for rq in env.requests] == [PUBLIC, "93.184.216.35", "93.184.216.36"]  # at most 3 validated addresses, never a name lookup


def test_a_second_address_is_used_when_the_first_is_unreachable(env):
    env.dns["multi.example.com"] = ["93.184.216.50", "93.184.216.51"]

    def handler(r):
        if r.url.host == "93.184.216.50":
            raise httpx.ConnectError("down", request=r)
        return resp(200, headers={"content-type": "image/png"}, content=PNG)
    env.handler = handler
    assert fetcher.fetch_to_storage("https://multi.example.com/x.png", None).size == len(PNG)


def test_a_transport_level_error_is_a_retryable_failure(env):
    def boom(r):
        raise httpx.RemoteProtocolError("garbage", request=r)
    env.handler = boom
    e = err_code("https://cdn.example.com/x")
    assert e.code == "MEDIA_FETCH_FAILED" and e.retryable is True


# ===================================================================== real sockets: a local server (development switch only)
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/ok.png":
            self.send_response(200); self.send_header("Content-Type", "image/png"); self.send_header("Content-Length", str(len(PNG))); self.end_headers(); self.wfile.write(PNG)
        elif p == "/big.png":
            self.send_response(200); self.send_header("Content-Type", "image/png"); self.send_header("Content-Length", str(50 * 1024 * 1024)); self.end_headers(); self.wfile.write(PNG)
        elif p == "/endless.png":
            self.send_response(200); self.send_header("Content-Type", "image/png"); self.end_headers()
            try:
                self.wfile.write(PNG)
                for _ in range(100000):
                    self.wfile.write(b"\x00" * 4096)
            except OSError:
                pass
        elif p == "/trickle.png":
            self.send_response(200); self.send_header("Content-Type", "image/png"); self.end_headers()
            try:
                self.wfile.write(PNG)
                for _ in range(200):
                    time.sleep(0.2)
                    self.wfile.write(b"\x00"); self.wfile.flush()
            except OSError:
                pass
        elif p == "/silent":
            time.sleep(3)
        elif p == "/hop1":
            self.send_response(302); self.send_header("Location", "/hop2"); self.end_headers()
        elif p == "/hop2":
            self.send_response(302); self.send_header("Location", "/ok.png"); self.end_headers()
        elif p == "/echo-headers":
            self.send_response(200); self.send_header("Content-Type", "text/plain")
            body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()


@pytest.fixture
def local_server(monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    s = get_settings()
    monkeypatch.setattr(s, "media_fetch_allow_private", True)  # the ONLY way to reach loopback: an explicit development switch
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_real_socket_download(local_server):
    r = fetcher.fetch_to_storage(f"{local_server}/ok.png", None)
    assert r.size == len(PNG) and storage.get_bytes(r.key)[0] == PNG


def test_real_socket_follows_a_chain_of_redirects(local_server):
    assert fetcher.fetch_to_storage(f"{local_server}/hop1", None).redirects == 2


def test_real_socket_oversized_content_length(local_server, monkeypatch):
    monkeypatch.setattr(get_settings(), "media_fetch_max_bytes", 1024 * 1024)
    assert err_code(f"{local_server}/big.png").code == "MEDIA_TOO_LARGE"


def test_real_socket_endless_body_is_cut_off(local_server, monkeypatch):
    monkeypatch.setattr(get_settings(), "media_fetch_max_bytes", 64 * 1024)
    t0 = time.time()
    assert err_code(f"{local_server}/endless.png").code == "MEDIA_TOO_LARGE" and time.time() - t0 < 5


def test_real_socket_trickling_server_hits_the_overall_deadline(local_server, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "media_fetch_total_timeout", 1.0)
    monkeypatch.setattr(s, "media_fetch_read_timeout", 5.0)
    t0 = time.time()
    e = err_code(f"{local_server}/trickle.png")
    assert e.code == "MEDIA_FETCH_FAILED" and e.retryable and "deadline" in e.message and time.time() - t0 < 4  # 40 s of trickle never held the worker


def test_real_socket_silent_server_hits_the_read_timeout(local_server, monkeypatch):
    monkeypatch.setattr(get_settings(), "media_fetch_read_timeout", 0.5)
    t0 = time.time()
    e = err_code(f"{local_server}/silent")
    assert e.code == "MEDIA_FETCH_FAILED" and "timed out" in e.message and time.time() - t0 < 2.5


def test_real_socket_request_carries_only_minimal_headers(local_server, monkeypatch):
    monkeypatch.setattr(get_settings(), "media_fetch_allowed_types", "text/plain")
    r = fetcher.fetch_to_storage(f"{local_server}/echo-headers", None)
    sent = json.loads(storage.get_bytes(r.key)[0])
    assert set(sent) <= {"host", "user-agent", "accept", "accept-encoding", "connection"} and sent["accept-encoding"] == "identity"
    assert not {"authorization", "cookie", "proxy-authorization"} & set(sent)


def test_connection_to_a_closed_port_fails_cleanly(monkeypatch):
    monkeypatch.setattr(get_settings(), "media_fetch_allow_private", True)
    e = err_code("http://127.0.0.1:9/x.png")
    assert e.code == "MEDIA_FETCH_FAILED" and e.retryable is True


def test_loopback_is_unreachable_without_the_development_switch(local_server, monkeypatch):
    monkeypatch.setattr(get_settings(), "media_fetch_allow_private", False)
    assert err_code(f"{local_server}/ok.png").code == "MEDIA_URL_REJECTED"


# ===================================================================== file names and storage keys
@pytest.mark.parametrize("url,given", [
    ("https://cdn.example.com/../../etc/passwd.png", None), ("https://cdn.example.com/a/b/c/..%2f..%2fsecret.png", None),
    ("https://cdn.example.com/x.png", "../../../etc/cron.d/evil.png"), ("https://cdn.example.com/x.png", "..\\..\\windows\\system32\\a.png"),
    ("https://cdn.example.com/x.png", "/etc/passwd"), ("https://cdn.example.com/x.png", "nul\x00byte.png"), ("https://cdn.example.com/x.png", "‮txt.png"),
    ("https://cdn.example.com/x.png", "a" * 500 + ".png"), ("https://cdn.example.com/x.png", ".htaccess"), ("https://cdn.example.com/x.png", "..."),
    ("https://cdn.example.com/x.png", "con.png"), ("https://cdn.example.com/x.png", "name with spaces & symbols!.png"), ("https://cdn.example.com/", None),
])
def test_stored_file_names_are_sanitised_and_confined(env, url, given):
    res = fetcher.fetch_to_storage(url, given)
    assert re.fullmatch(r"fetched/[0-9a-f]{16}/[A-Za-z0-9._-]{1,90}", res.key), res.key
    assert ".." not in res.key.split("/")[1:] and not res.filename.startswith(".") and res.filename.endswith(".png")
    root = pathlib.Path(get_settings().local_store_dir).resolve()
    assert (root / res.key).resolve().is_relative_to(root)


def test_the_extension_follows_the_detected_type_not_the_given_name(env):
    env.handler = lambda r: resp(200, headers={"content-type": "image/jpeg"}, content=JPEG)
    assert fetcher.fetch_to_storage("https://cdn.example.com/x", "evil.php").filename == "evil.jpg"
    assert fetcher.fetch_to_storage("https://cdn.example.com/x", "payload.exe").filename.endswith(".jpg")


# ===================================================================== secrets and logging
def test_query_strings_fragments_and_tokens_never_reach_logs_or_results(env, caplog):
    caplog.set_level(logging.INFO)
    secret = "SUPERSECRETTOKEN123"
    res = fetcher.fetch_to_storage(f"https://cdn.example.com/p/pic.png?token={secret}&sig=abc#frag-{secret}", None)
    text = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    assert secret not in text and "token=" not in text and "sig=abc" not in text
    assert secret not in json.dumps(res.__dict__) and res.source == "https://cdn.example.com/p/pic.png"
    assert any("cdn.example.com" in line for line in text.splitlines())  # the host is logged, the secrets are not


def test_rejections_do_not_echo_url_credentials_or_query_secrets(env):
    e = err_code("https://alice:S3cr3tPassw0rd@example.com/x.png?api_key=KEYKEYKEY")
    blob = json.dumps(e.to_dict())
    assert "S3cr3tPassw0rd" not in blob and "KEYKEYKEY" not in blob and "alice" not in blob
    e2 = err_code("http://169.254.169.254/latest/meta-data/?token=ZZZ-SECRET-ZZZ")
    assert "ZZZ-SECRET-ZZZ" not in json.dumps(e2.to_dict())


def test_sanitized_source_helper():
    assert fetcher.sanitized_source("https://u:p@h.example:8443/a/b.png?x=1#f") == "https://h.example:8443/a/b.png"
    assert fetcher.sanitized_source("http://[::1]:99999/x") == "(unparseable url)"


# ===================================================================== through the API (job, idempotency, manifest)
def post_fetch(client, body, key=None):
    return client.post("/api/v1/files/from-url", json=body, headers={**H, "Idempotency-Key": key or uid("fetch")})


def test_api_rejects_unsafe_urls_synchronously_with_the_envelope(client, env):
    for url in ["http://169.254.169.254/latest/meta-data/", "http://localhost:8000/api/v1/destinations", "file:///etc/passwd", "https://u:p@example.com/x.png", "http://redis:6379/"]:
        r = post_fetch(client, {"url": url})
        assert r.status_code == 422 and r.json()["error"]["code"] == "MEDIA_URL_REJECTED" and r.json()["error"]["retryable"] is False, url
    assert env.requests == []
    r = post_fetch(client, {"url": "https://cdn.example.com/x.png", "callback_url": "https://169.254.169.254/hook"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "CALLBACK_URL_REJECTED"


def test_api_fetch_job_end_to_end_with_manifest_and_download(client, env):
    r = post_fetch(client, {"url": "https://cdn.example.com/pics/cat.png?token=SECRETQ", "filename": "my cat.png"})
    assert r.status_code == 202 and r.headers["Location"].startswith("/api/v1/jobs/")
    j = job(client, r.json()["job_id"])
    assert j["status"] == "succeeded" and j["progress"] == 100, j
    res = j["result"]
    assert res["status"] == "stored" and res["content_type"] == "image/png" and res["size"] == len(PNG) and res["redirects"] == 0
    assert "SECRETQ" not in json.dumps(j) and res["source"] == "https://cdn.example.com/pics/cat.png"
    assert j["links"]["download"] == res["download_url"]
    got = client.get(f"/api/v1/files/{res['key']}", headers=H)
    assert got.status_code == 200 and got.content == PNG
    m = res["manifest"]
    assert m["kind"] == "media_fetch" and m["models"] == [] and m["cost_estimate_usd"] == 0.0 and m["seed"] is None and m["providers"][0]["name"] == "http-fetch"
    assert m["settings"]["max_bytes"] == get_settings().media_fetch_max_bytes and m["rights"]["licence"].startswith("unknown")
    assert m["timings"]["total_s"] >= 0 and "download_and_store_s" in m["timings"]["steps"]
    for req in env.requests:  # the service's own bearer token was NOT forwarded to the remote host
        assert "authorization" not in req.headers


def test_api_fetch_is_idempotent(client, env):
    key = uid("fi")
    a, b = post_fetch(client, {"url": "https://cdn.example.com/a.png"}, key), post_fetch(client, {"url": "https://cdn.example.com/a.png"}, key)
    assert a.json()["job_id"] == b.json()["job_id"] and b.json()["idempotent_replay"] is True and len(env.requests) == 1
    c = post_fetch(client, {"url": "https://cdn.example.com/other.png"}, key)
    assert c.status_code == 422 and c.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_api_fetch_failures_become_failed_jobs_with_clear_errors(client, env, monkeypatch):
    env.handler = lambda r: resp(200, headers={"content-type": "text/html"}, content=b"<html>")
    j = job(client, post_fetch(client, {"url": "https://cdn.example.com/page"}).json()["job_id"])
    assert j["status"] == "failed" and j["error"]["code"] == "MEDIA_TYPE_REJECTED" and j["error"]["retryable"] is False and j["result"] is None
    monkeypatch.setattr(get_settings(), "media_fetch_max_bytes", 16)
    env.handler = lambda r: resp(200, headers={"content-type": "image/png"}, content=PNG)
    j = job(client, post_fetch(client, {"url": "https://cdn.example.com/big.png"}).json()["job_id"])
    assert j["error"]["code"] == "MEDIA_TOO_LARGE"


def test_api_fetch_retry_after_a_transient_failure_reruns_the_same_job(client, env):
    key = uid("retry")
    env.handler = lambda r: httpx.Response(503)
    first = post_fetch(client, {"url": "https://cdn.example.com/a.png"}, key)
    j1 = job(client, first.json()["job_id"])
    assert j1["status"] == "failed" and j1["error"]["code"] == "MEDIA_FETCH_FAILED" and j1["error"]["retryable"] is True
    env.handler = lambda r: resp(200, headers={"content-type": "image/png"}, content=PNG)
    again = post_fetch(client, {"url": "https://cdn.example.com/a.png"}, key)
    assert again.json()["job_id"] == first.json()["job_id"] and job(client, again.json()["job_id"])["status"] == "succeeded"


def test_api_requires_auth_and_valid_body(client):
    assert client.post("/api/v1/files/from-url", json={"url": "https://x.example/a.png"}, headers={"Idempotency-Key": uid()}).status_code == 401
    r = client.post("/api/v1/files/from-url", json={"nope": 1}, headers={**H, "Idempotency-Key": uid()})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_the_demo_image_refuses_the_development_switch():
    root = pathlib.Path(__file__).resolve().parents[1]
    entry = (root / "deploy/render/entrypoint.sh").read_text(encoding="utf-8")
    assert "MEDIA_FETCH_ALLOW_PRIVATE" in entry and "CALLBACK_ALLOW_PRIVATE" in entry
    from app.config import Settings
    s = Settings()
    assert s.media_fetch_allow_private is False and s.media_fetch_max_bytes <= 25 * 1024 * 1024 and s.media_fetch_max_redirects <= 5


def test_log_formatter_redacts_url_query_strings_everywhere():
    fmt = JsonFormatter()
    rec = logging.getLogger("t").makeRecord("t", logging.INFO, "f", 1, 'HTTP Request: GET https://x.example/a/b.png?token=AAA111&k=2#frag "HTTP/1.1 200 OK"', (), None)
    out = fmt.format(rec)
    assert "AAA111" not in out and "k=2" not in out and "frag" not in out and "https://x.example/a/b.png?[REDACTED]" in out
    rec = logging.getLogger("t").makeRecord("t", logging.INFO, "f", 1, "no url here, plain message", (), None)
    assert "plain message" in fmt.format(rec)


def test_http_client_libraries_do_not_log_request_urls(env, caplog):
    caplog.set_level(logging.DEBUG)
    fetcher.fetch_to_storage("https://cdn.example.com/p/pic.png?token=NOLOGTHIS", None)
    assert "NOLOGTHIS" not in caplog.text
