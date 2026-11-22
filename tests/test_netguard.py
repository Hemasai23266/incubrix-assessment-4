"""Callback URL SSRF protection and the retryable 503 for an unavailable queue."""
import http.server
import json
import shutil
import ssl
import subprocess
import threading

import httpx
import pytest

from app import netguard
from app.config import get_settings
from app.netguard import CallbackRejected, ip_is_public, safe_post, validate_callback_url
from tests.conftest import H, job, make_dest, publish, uid

PUBLIC = "93.184.216.34"


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: [PUBLIC])


# ---- URLs that must be refused ------------------------------------------------------------------------------------------
BAD_URLS = [
    ("plain http", "http://example.com/hook"),
    ("file scheme", "file:///etc/passwd"),
    ("ftp scheme", "ftp://example.com/x"),
    ("gopher scheme", "gopher://example.com/_x"),
    ("javascript", "javascript:alert(1)"),
    ("credentials in URL", "https://user:pass@example.com/hook"),
    ("user only", "https://admin@example.com/hook"),
    ("no host", "https:///hook"),
    ("localhost", "https://localhost/hook"),
    ("localhost trailing dot", "https://localhost./hook"),
    ("LOCALHOST upper", "https://LOCALHOST/hook"),
    ("subdomain of localhost", "https://evil.localhost/hook"),
    ("loopback v4", "https://127.0.0.1/hook"),
    ("loopback 127.1.2.3", "https://127.1.2.3/hook"),
    ("loopback v6", "https://[::1]/hook"),
    ("unspecified", "https://0.0.0.0/hook"),
    ("unspecified v6", "https://[::]/hook"),
    ("private 10/8", "https://10.0.0.5/hook"),
    ("private 172.16/12", "https://172.16.0.9/hook"),
    ("private 172.31", "https://172.31.255.254/hook"),
    ("private 192.168/16", "https://192.168.1.1/hook"),
    ("link-local", "https://169.254.10.10/hook"),
    ("cloud metadata IP", "https://169.254.169.254/latest/meta-data/"),
    ("cloud metadata IP with port", "https://169.254.169.254:443/computeMetadata/v1/"),
    ("GCP metadata name", "https://metadata.google.internal/computeMetadata/v1/"),
    ("short metadata name", "https://metadata/computeMetadata/v1/"),
    ("Azure/AWS-style IPv6 metadata", "https://[fd00:ec2::254]/latest/"),
    ("CGNAT 100.64/10", "https://100.64.0.1/hook"),
    ("benchmark range 198.18/15", "https://198.18.0.1/hook"),
    ("IPv6 unique local", "https://[fc00::1]/hook"),
    ("IPv6 link-local", "https://[fe80::1]/hook"),
    ("IPv4-mapped loopback", "https://[::ffff:127.0.0.1]/hook"),
    ("IPv4-mapped private", "https://[::ffff:10.0.0.1]/hook"),
    ("IPv4-mapped hex loopback", "https://[::ffff:7f00:1]/hook"),
    ("NAT64 of loopback", "https://[64:ff9b::7f00:1]/hook"),
    ("6to4 of private", "https://[2002:0a00:0001::1]/hook"),
    ("Teredo", "https://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/hook"),
    ("decimal IP", "https://2130706433/hook"),
    ("hex IP", "https://0x7f000001/hook"),
    ("hex dotted", "https://0x7f.0x0.0x0.0x1/hook"),
    ("octal IP", "https://0177.0.0.1/hook"),
    ("mixed hex/dec", "https://0x7f.1/hook"),
    ("internal suffix .internal", "https://db.internal/hook"),
    ("internal suffix .local", "https://printer.local/hook"),
    ("kubernetes-style", "https://api.default.svc.cluster.local/hook"),
    ("compose service redis", "https://redis/hook"),
    ("compose service mocks", "https://mocks/hook"),
    ("compose service db", "https://db/hook"),
    ("port 9100", "https://example.com:9100/hook"),
    ("redis port", "https://example.com:6379/hook"),
    ("port 80 over https", "https://example.com:80/hook"),
    ("whitespace in URL", "https://example.com/ho ok"),
    ("newline injection", "https://example.com/hook\r\nHost: evil"),
    ("backslash trick", "https://example.com\\@169.254.169.254/"),
    ("empty", ""),
    ("too long", "https://example.com/" + "a" * 2100),
]


@pytest.mark.parametrize("label,url", BAD_URLS, ids=[b[0] for b in BAD_URLS])
def test_unsafe_callback_urls_are_refused(label, url, public_dns):
    with pytest.raises(CallbackRejected):
        validate_callback_url(url)


def test_hostname_resolving_to_private_address_is_refused(monkeypatch):
    for ip in ["127.0.0.1", "10.1.2.3", "169.254.169.254", "192.168.0.10", "::1", "fd00::5", "100.64.1.1"]:
        monkeypatch.setattr(netguard, "_resolve", lambda host, port, ip=ip: [ip])
        with pytest.raises(CallbackRejected):
            validate_callback_url("https://innocent-looking.example.com/hook")


def test_mixed_public_and_private_answers_are_refused(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: [PUBLIC, "10.0.0.1"])
    with pytest.raises(CallbackRejected):
        validate_callback_url("https://example.com/hook")


def test_unresolvable_host_is_refused(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: [])
    with pytest.raises(CallbackRejected):
        validate_callback_url("https://does-not-exist.example.com/hook")


# ---- URLs that must keep working ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("url", ["https://example.com/hook", "https://hooks.example.org:8443/a/b?x=1&y=2", "https://EXAMPLE.com./hook",
                                 "https://sub.domain.example.co.uk/path"])
def test_legitimate_public_https_callbacks_pass(url, public_dns):
    t = validate_callback_url(url)
    assert t.scheme == "https" and t.ips == [PUBLIC]


def test_public_ipv4_and_ipv6_literals_pass():
    assert validate_callback_url("https://93.184.216.34/hook").ips == ["93.184.216.34"]
    assert validate_callback_url("https://[2606:2800:220:1:248:1893:25c8:1946]/hook").scheme == "https"
    assert ip_is_public("8.8.8.8") and ip_is_public("2001:4860:4860::8888")
    assert not any(ip_is_public(x) for x in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "0.0.0.0", "224.0.0.1", "240.0.0.1", "::1", "::", "ff02::1"])


def test_opt_in_http_and_dev_flag(monkeypatch, public_dns):
    s = get_settings()
    monkeypatch.setattr(s, "callback_allow_http", True)
    assert validate_callback_url("http://example.com/hook").scheme == "http"
    monkeypatch.setattr(s, "callback_allow_http", False)
    monkeypatch.setattr(s, "callback_allow_private", True)  # dev-only switch
    monkeypatch.setattr(netguard, "_resolve", lambda h, p: ["127.0.0.1"])
    assert validate_callback_url("http://localhost:9999/hook").ips == ["127.0.0.1"]


# ---- DNS rebinding: resolve once, connect to the validated address ------------------------------------------------------
def test_connection_is_pinned_to_the_validated_address_and_signed_headers_are_sent(monkeypatch):
    calls = []

    def resolver(host, port):
        calls.append(host)
        return [PUBLIC]  # an attacker could answer differently on a second lookup

    monkeypatch.setattr(netguard, "_resolve", resolver)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["host_header"], seen["sni"] = str(request.url), request.headers["host"], request.extensions.get("sni_hostname")
        seen["sig"], seen["body"] = request.headers["x-incubrix-signature"], request.content
        return httpx.Response(200, content=b"internal response that must not be read")

    status = safe_post("https://hooks.example.com/path?a=1", b'{"ok":true}', {"X-IncuBrix-Signature": "sha256=abc", "Content-Type": "application/json"},
                       transport=httpx.MockTransport(handler))
    assert status == 200
    assert calls == ["hooks.example.com"]  # exactly ONE lookup: no second resolution between check and connect
    assert seen["url"] == f"https://{PUBLIC}/path?a=1"  # connected to the IP (default port 443 is implicit), not the name
    assert seen["host_header"] == "hooks.example.com" and seen["sni"] == "hooks.example.com"
    assert seen["sig"] == "sha256=abc" and seen["body"] == b'{"ok":true}'


def test_dns_rebinding_to_private_address_is_blocked_at_send_time(monkeypatch):
    answers = iter([[PUBLIC], ["169.254.169.254"]])  # first answer public (passes submission), second answer is the metadata IP
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: next(answers))
    validate_callback_url("https://rebind.example.com/hook")  # submission-time check passes
    called = []
    with pytest.raises(CallbackRejected):
        safe_post("https://rebind.example.com/hook", b"{}", {}, transport=httpx.MockTransport(lambda r: called.append(r) or httpx.Response(200)))
    assert called == []  # no request left the process


def test_redirects_are_not_followed(monkeypatch, public_dns):
    hits = []

    def handler(request):
        hits.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})

    assert safe_post("https://example.com/hook", b"{}", {}, transport=httpx.MockTransport(handler)) == 302
    assert len(hits) == 1  # the redirect target was never requested


def test_environment_proxy_is_ignored(monkeypatch, public_dns):
    monkeypatch.setenv("HTTPS_PROXY", "http://169.254.169.254:3128")
    assert safe_post("https://example.com/hook", b"{}", {}, transport=httpx.MockTransport(lambda r: httpx.Response(204))) == 204


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI not available")
def test_real_tls_is_pinned_to_the_ip_but_certificate_is_verified_against_the_hostname(tmp_path, monkeypatch):
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt), "-days", "1",
                    "-subj", "/CN=callback.test", "-addext", "subjectAltName=DNS:callback.test"], check=True, capture_output=True)
    received = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received["host"], received["body"] = self.headers["Host"], self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    s = get_settings()
    monkeypatch.setattr(s, "callback_allow_private", True)  # lets the test target 127.0.0.1; production refuses loopback
    monkeypatch.setattr(netguard, "_resolve", lambda host, p: ["127.0.0.1"])
    try:
        assert safe_post(f"https://callback.test:{port}/hook", b'{"x":1}', {}, verify=str(crt)) == 200
        assert received["host"] == f"callback.test:{port}" and received["body"] == b'{"x":1}'
        with pytest.raises(httpx.ConnectError):  # same server, same cert, but the URL host does not match the certificate
            safe_post(f"https://other.test:{port}/hook", b"{}", {}, verify=str(crt))
    finally:
        srv.shutdown()


# ---- through the API ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/api/v1/connectors", "https://169.254.169.254/latest/meta-data/", "https://localhost/x",
                                 "https://redis/x", "http://example.com/x", "file:///etc/passwd"])
def test_api_rejects_unsafe_callback_url_at_submission(client, url, public_dns):
    d = make_dest(client, "discourse")
    r = client.post("/api/v1/publish", headers={**H, "Idempotency-Key": uid()},
                    json={"destination_id": d["id"], "rendition": {"title": "A sufficiently long test title", "body": "b"}, "callback_url": url})
    assert r.status_code == 422 and r.json()["error"]["code"] == "CALLBACK_URL_REJECTED" and r.json()["error"]["retryable"] is False
    rc = client.post(f"/api/v1/destinations/{d['id']}/certify", headers={**H, "Idempotency-Key": uid()}, json={"callback_url": url})
    assert rc.status_code == 422 and rc.json()["error"]["code"] == "CALLBACK_URL_REJECTED"


def test_blocked_callback_does_not_fail_the_job_and_sends_nothing(client, monkeypatch):
    answers = []

    def resolver(host, port):  # public at submission time, private by the time the callback is sent (DNS rebinding)
        answers.append(host)
        return [PUBLIC] if len(answers) == 1 else ["10.0.0.9"]

    monkeypatch.setattr(netguard, "_resolve", resolver)
    called = []
    monkeypatch.setattr(httpx.Client, "stream", lambda *a, **k: called.append(1))
    d = make_dest(client, "discourse")
    r = client.post("/api/v1/publish", headers={**H, "Idempotency-Key": uid()},
                    json={"destination_id": d["id"], "rendition": {"title": "A sufficiently long test title", "body": "b"}, "callback_url": "https://example.com/hook"})
    assert r.status_code == 202
    assert len(answers) == 2  # once at submission, once (independently) at send time
    assert called == []  # nothing was sent anywhere
    assert job(client, r.json()["job_id"])["status"] == "succeeded"  # a blocked callback never fails the job


# ---- queue unavailable -> retryable 503 ------------------------------------------------------------------------------------
def test_queue_down_returns_retryable_503_and_same_key_retry_reruns_the_same_job(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "job_backend", "rq")
    monkeypatch.setattr(s, "redis_url", "redis://127.0.0.1:1/0")  # nothing listens here: connection refused
    d = make_dest(client, "discourse")
    key = uid("q")
    r = publish(client, d["id"], key=key)
    assert r.status_code == 503
    e = r.json()["error"]
    assert e["code"] == "QUEUE_UNAVAILABLE" and e["retryable"] is True and e["details"]["retry_with_same_idempotency_key"] is True
    assert r.headers["Retry-After"] == "5"
    jid = e["details"]["job_id"]
    stuck = client.get(f"/api/v1/jobs/{jid}", headers=H).json()
    assert stuck["status"] == "failed" and stuck["error"]["code"] == "QUEUE_UNAVAILABLE"  # not left 'queued' forever
    monkeypatch.setattr(s, "job_backend", "inline")  # queue recovers
    r2 = publish(client, d["id"], key=key)
    assert r2.status_code == 202 and r2.json()["job_id"] == jid  # same job id re-run
    assert job(client, jid)["status"] == "succeeded"
    from app.mocks.platforms import STORES
    assert len(STORES["discourse"]["posts"]) == 1
