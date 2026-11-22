"""Connector contract (Appendix A) and Integration Standard checks."""
import hashlib
import hmac
import json
import logging

import pytest

from app.connectors.base import load_manifest
from app.connectors.registry import get_registry
from app.errors import ERROR_CODES
from app.mocks.platforms import STORES
from tests.conftest import H, job, make_dest, publish, uid

CORE = ["discourse", "lemmy", "devto", "ghost", "webhook", "assisted"]
METHODS = ["validate", "publish", "get_status", "delete", "fetch_metrics", "refresh_auth", "build_assist_pack"]


def test_every_connector_has_a_valid_manifest():
    for k in CORE:
        m = load_manifest(k)
        assert m["key"] == k and m["tier"] in {"native", "protocol", "bridge", "feed", "assisted"}
        assert m["surfaces"][0]["spec"] and "error_map" in m and "rate_limits" in m


def test_every_connector_implements_all_contract_methods():
    for k, c in get_registry().items():
        for m in METHODS:
            assert callable(getattr(c, m)), (k, m)


def test_shared_error_codes_present():
    for code in ["DEST_SPEC_VIOLATION", "DEST_RULE_FREQUENCY_CAP", "CONNECTION_AUTH_EXPIRED", "PLATFORM_RATE_LIMIT",
                 "PLATFORM_QUOTA_EXHAUSTED", "PLATFORM_CONTENT_REJECTED", "PLATFORM_UNAVAILABLE", "CONNECTOR_DISABLED"]:
        assert code in ERROR_CODES
    assert all(c == c.upper() and " " not in c for c in ERROR_CODES)


def call(client, key, method, body):
    return client.post(f"/connector/v1/{key}/{method}", json=body, headers=H)


@pytest.mark.parametrize("key", ["discourse", "lemmy", "devto", "ghost", "webhook"])
def test_contract_http_roundtrip(client, key):
    d = make_dest(client, key)
    rend = {"title": "A sufficiently long test title", "body": "hello"}
    v = call(client, key, "validate", {"rendition": rend, "destination": d}).json()
    assert v == {"ok": True, "violations": []}
    p = call(client, key, "publish", {"rendition": rend, "destination": d, "idempotency_key": uid()})
    assert p.status_code == 200, p.text
    pj = p.json()
    assert set(pj) >= {"platform_post_id", "live_url", "status"}
    s = call(client, key, "get_status", {"platform_post_id": pj["platform_post_id"], "destination": d}).json()
    assert set(s) >= {"status", "live_url"}
    dl = call(client, key, "delete", {"platform_post_id": pj["platform_post_id"], "destination": d}).json()
    assert dl["deleted"] is True


def test_contract_validate_reports_violations(client):
    d = make_dest(client, "lemmy")
    v = call(client, "lemmy", "validate", {"rendition": {"title": "x" * 300}, "destination": d}).json()
    assert v["ok"] is False and v["violations"][0]["field"] == "title"


def test_contract_assist_pack_shape(client):
    d = make_dest(client, "assisted", rules_url="https://example.org/rules", options={"submit_url": "https://example.org/submit"})
    r = call(client, "assisted", "build_assist_pack",
             {"rendition": {"title": "Launch post", "body": "text", "link": "https://incubrix.example",
                            "media": [{"url": "http://x/a.png", "content_type": "image/png", "filename": "a.png"}]}, "destination": d}).json()
    assert r["caption"] and r["deep_link"] == "https://example.org/submit" and len(r["files"]) == 2
    assert any("rules" in c.lower() for c in r["checklist"])


def test_contract_metrics_normalised(client):
    d = make_dest(client, "discourse")
    p = call(client, "discourse", "publish", {"rendition": {"title": "A sufficiently long test title", "body": "b"},
                                              "destination": d, "idempotency_key": uid()}).json()
    m = call(client, "discourse", "fetch_metrics", {"platform_post_id": p["platform_post_id"], "destination": d}).json()
    assert set(m) == {"views", "likes", "comments", "shares", "saves", "watch_time_s", "clicks"}
    assert m["views"] == 7 and m["likes"] == 2


def test_contract_unknown_method_and_connector(client):
    assert call(client, "discourse", "nope", {}).status_code == 404
    r = call(client, "nonexistent", "validate", {})
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"


def test_assisted_destination_produces_pack_never_posts(client):
    d = make_dest(client, "assisted", automation_allowed=False, disclosure_required=True,
                  options={"submit_url": "https://example.org/submit"})
    r = publish(client, d["id"], {"link": "https://incubrix.example"})
    j = job(client, r.json()["job_id"])
    assert j["status"] == "succeeded" and j["result"]["status"] == "assist_pack_ready"
    assert j["result"]["assist_pack"]["checklist"] and j["result"]["download_url"]
    assert j["links"]["download"]
    assert all(not s["posts"] for s in STORES.values())  # nothing was automated


def test_automation_forbidden_destination_blocked_on_native_connector(client):
    d = make_dest(client, "discourse", automation_allowed=False)
    j = job(client, publish(client, d["id"]).json()["job_id"])
    assert j["error"]["code"] == "DEST_RULE_VIOLATION"


# ---- Integration Standard ------------------------------------------------------------------------
def test_error_envelope_shape(client):
    r = client.get("/api/v1/destinations")  # no auth
    assert r.status_code == 401
    e = r.json()["error"]
    assert set(e) == {"code", "message", "retryable", "details"} and e["code"] == "UNAUTHORIZED"
    r = client.post("/api/v1/destinations", json={"name": "x"}, headers={**H, "Idempotency-Key": "k"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_health_ready_and_routes_versioned(client):
    assert client.get("/health").status_code == 200
    rd = client.get("/ready")
    assert rd.status_code == 200 and rd.json()["checks"]["database"] == "ok"
    from app.main import app
    for r in app.routes:
        p = getattr(r, "path", "")
        assert p.startswith(("/api/v1/", "/connector/v1/")) or p in ("/health", "/ready", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"), p


def test_job_lifecycle_fields(client):
    d = make_dest(client, "discourse")
    r = publish(client, d["id"])
    assert r.status_code == 202 and r.headers["Location"].startswith("/api/v1/jobs/")
    j = job(client, r.json()["job_id"])
    assert j["status"] in {"queued", "running", "succeeded", "failed"} and j["progress"] == 100
    assert "links" in j and j["result"]["manifest"]


def test_output_manifest_contents(client):
    d = make_dest(client, "ghost")
    m = job(client, publish(client, d["id"], {"ai_generated": True, "disclosure": "AI assisted"}).json()["job_id"])["result"]["manifest"]
    for k in ["connector", "providers", "settings", "seed", "timings", "cost_estimate_usd", "warnings", "rights"]:
        assert k in m
    assert m["rights"]["ai_generated"] is True and m["connector"]["key"] == "ghost"


def test_callback_is_signed_with_hmac(client, monkeypatch):
    sent = {}

    def fake_safe_post(url, body, headers, **kw):
        sent.update(url=url, body=body, headers=headers)
        return 200

    import app.jobs as jobs
    monkeypatch.setattr(jobs.netguard, "safe_post", fake_safe_post)
    monkeypatch.setattr(jobs.netguard, "_resolve", lambda host, port: ["93.184.216.34"])
    d = make_dest(client, "discourse")
    r = client.post("/api/v1/publish", headers={**H, "Idempotency-Key": uid()},
                    json={"destination_id": d["id"], "rendition": {"title": "A sufficiently long test title", "body": "hello"},
                          "callback_url": "https://callback.example/hook"})
    assert r.status_code == 202
    want = "sha256=" + hmac.new(b"change-me-callback-secret", sent["body"], hashlib.sha256).hexdigest()
    assert sent["headers"]["X-IncuBrix-Signature"] == want
    assert json.loads(sent["body"])["status"] == "succeeded"
    assert sent["url"] == "https://callback.example/hook"


def test_file_upload_and_download(client):
    r = client.post("/api/v1/files", files={"file": ("pic.png", b"\x89PNGdata", "image/png")},
                    headers={**H, "Idempotency-Key": uid()})
    assert r.status_code == 201
    key = r.json()["key"]
    g = client.get(f"/api/v1/files/{key}", headers=H)
    assert g.status_code == 200 and g.content == b"\x89PNGdata"
    assert client.get("/api/v1/files/../../etc/passwd", headers=H).status_code in (404, 400)


def test_certification_evidence_is_stored(client):
    d = make_dest(client, "discourse")
    j = job(client, client.post(f"/api/v1/destinations/{d['id']}/certify", headers={**H, "Idempotency-Key": uid()}).json()["job_id"])
    assert j["status"] == "succeeded"
    steps = [s["step"] for s in j["result"]["steps"]]
    assert steps == ["validate", "publish", "get_status_live", "delete", "verify_deleted"]
    ev = client.get(j["result"]["evidence_url"].replace("http://localhost:8000", ""), headers=H).json()
    assert ev["status"] == "passed" and ev["duration_s"] < 600
    assert STORES["discourse"]["posts"][next(iter(STORES["discourse"]["posts"]))]["deleted_at"]
    dest = client.get(f"/api/v1/destinations/{d['id']}", headers=H).json()
    assert dest["certified"] and dest["level"] == "L3" and dest["last_certified_at"]


def test_adding_another_forum_is_one_catalogue_row_no_code(client):
    """Brief must-have 3: a second Discourse forum is just another row pointing at another instance."""
    n_connectors = len(get_registry())
    d2 = make_dest(client, "discourse", name="Forum B", options={"mock_instance": "discourse-b"})
    j = job(client, client.post(f"/api/v1/destinations/{d2['id']}/certify", headers={**H, "Idempotency-Key": uid()}).json()["job_id"])
    assert j["status"] == "succeeded"
    assert len(STORES["discourse-b"]["posts"]) == 1 and len(STORES["discourse"]["posts"]) == 0
    assert len(get_registry()) == n_connectors


def test_secrets_never_logged(client, monkeypatch, caplog):
    monkeypatch.setenv("MY_SECRET_REF", "ultra-secret-token-9f8e7d")
    caplog.set_level(logging.INFO)
    from app.logging_setup import JsonFormatter
    fmt = JsonFormatter()
    d = make_dest(client, "discourse", credentials_ref="MY_SECRET_REF")
    job(client, publish(client, d["id"]).json()["job_id"])
    text = "\n".join(fmt.format(r) for r in caplog.records)
    assert "ultra-secret-token-9f8e7d" not in text
    log = logging.getLogger("t")
    rec = log.makeRecord("t", logging.INFO, "f", 1, "Authorization: Bearer abcdefghijkl api_key=SECRET123 password=hunter2", (), None)
    out = fmt.format(rec)
    assert "abcdefghijkl" not in out and "SECRET123" not in out and "hunter2" not in out


def test_logs_are_json_with_required_fields(client, capsys):
    from app.logging_setup import setup_logging
    setup_logging()
    client.get("/health", headers={"X-Correlation-Id": "corr-123"})
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
    assert lines and all({"timestamp", "level", "job_id", "correlation_id"} <= set(l) for l in lines)
    assert any(l["correlation_id"] == "corr-123" for l in lines)


def test_correlation_id_echoed(client):
    r = client.get("/health", headers={"X-Correlation-Id": "abc-1"})
    assert r.headers["X-Correlation-Id"] == "abc-1"


def test_no_secrets_in_repository():
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1]
    pat = re.compile(r"(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16})")
    for p in root.rglob("*"):
        if p.is_file() and p.suffix in {".py", ".yaml", ".yml", ".md", ".env", ".example", ".toml", ".txt"} \
                and ".venv" not in p.parts and "evidence" not in p.parts:
            assert not pat.search(p.read_text(errors="ignore")), p
    assert not (root / ".env").exists() or ".env" in (root / ".gitignore").read_text()
