"""Integration Standard point 5: framework-generated errors use the same JSON envelope as application errors."""
import pytest
from fastapi.testclient import TestClient

from tests.conftest import H, make_dest, uid

ENVELOPE_KEYS = {"code", "message", "retryable", "details"}


def assert_envelope(r, status, code):
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert set(body) == {"error"}, body
    assert set(body["error"]) == ENVELOPE_KEYS, body
    assert body["error"]["code"] == code and body["error"]["code"] == body["error"]["code"].upper()
    assert isinstance(body["error"]["retryable"], bool) and isinstance(body["error"]["details"], dict)
    assert "detail" not in body and "Traceback" not in r.text
    return body["error"]


@pytest.mark.parametrize("method,path", [
    ("get", "/api/v1/publish"), ("put", "/api/v1/publish"), ("delete", "/api/v1/destinations"), ("get", "/api/v1/maintenance/cleanup-test-posts"),
    ("post", "/health"), ("delete", "/ready"), ("get", "/api/v1/files"), ("get", "/connector/v1/discourse/validate"),
    ("post", "/api/v1/jobs/abc"), ("patch", "/api/v1/destinations"),
])
def test_wrong_http_method_returns_405_envelope_with_allow_header(client, method, path):
    r = getattr(client, method)(path, headers=H)
    err = assert_envelope(r, 405, "METHOD_NOT_ALLOWED")
    assert err["retryable"] is False and err["details"]["allowed_methods"]
    assert r.headers["Allow"] and set(m.strip() for m in r.headers["Allow"].split(",")) == set(err["details"]["allowed_methods"])


def test_unknown_route_404_envelope(client):
    err = assert_envelope(client.get("/api/v1/does-not-exist", headers=H), 404, "NOT_FOUND")
    assert err["details"]["path"] == "/api/v1/does-not-exist"
    assert_envelope(client.get("/totally/unknown"), 404, "NOT_FOUND")


def test_wrong_method_is_still_reported_before_authentication_details_leak(client):
    """A wrong method without a token must not reveal anything beyond the envelope."""
    err = assert_envelope(client.get("/api/v1/publish"), 405, "METHOD_NOT_ALLOWED")
    assert "token" not in str(err).lower()


def test_validation_errors_are_envelopes_with_field_reasons(client):
    r = client.post("/api/v1/destinations", json={"name": 5}, headers={**H, "Idempotency-Key": uid()})
    err = assert_envelope(r, 422, "VALIDATION_ERROR")
    assert err["details"]["errors"] and all({"field", "reason"} <= set(e) for e in err["details"]["errors"])
    r = client.post("/api/v1/publish", content=b"{not json", headers={**H, "Idempotency-Key": uid(), "Content-Type": "application/json"})
    assert_envelope(r, 422, "VALIDATION_ERROR")
    r = client.post("/connector/v1/discourse/validate", content=b"[1,2]", headers={**H, "Content-Type": "application/json"})
    assert_envelope(r, 422, "VALIDATION_ERROR")


def test_auth_and_idempotency_errors_keep_their_status_and_codes(client):
    assert_envelope(client.get("/api/v1/destinations"), 401, "UNAUTHORIZED")
    d = make_dest(client, "discourse")
    body = {"destination_id": d["id"], "rendition": {"title": "A sufficiently long test title", "body": "b"}}
    assert_envelope(client.post("/api/v1/publish", json=body, headers={**H, "X-No-Auto-Key": "1"}), 400, "IDEMPOTENCY_KEY_REQUIRED")
    assert_envelope(client.post("/api/v1/publish", json=body, headers={**H, "Idempotency-Key": "has space"}), 400, "IDEMPOTENCY_KEY_INVALID")


def test_rate_limit_response_is_an_envelope(client, monkeypatch):
    from app.config import get_settings
    import app.main as m
    monkeypatch.setattr(get_settings(), "rate_limit_per_minute", 2)
    m._hits.clear()
    codes = [client.get("/api/v1/connectors", headers=H) for _ in range(4)]
    assert_envelope(codes[-1], 429, "RATE_LIMITED")
    assert codes[-1].headers["Retry-After"]


def test_unhandled_exception_returns_500_envelope_without_internals(monkeypatch):
    import app.main as m
    boom = "SECRET-INTERNAL-DETAIL-xyz"
    monkeypatch.setattr(m.storage, "storage_ready", lambda: (_ for _ in ()).throw(RuntimeError(boom)))
    with TestClient(m.app, raise_server_exceptions=False) as c:
        r = c.get("/ready")
    err = assert_envelope(r, 500, "INTERNAL_ERROR")
    assert boom not in r.text and err["retryable"] is False


def test_payload_too_large_upload_keeps_413(client, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "storage", type("S", (), {"put_bytes": staticmethod(lambda *a, **k: "x")}))
    big = b"x" * (25 * 1024 * 1024 + 1)
    r = client.post("/api/v1/files", files={"file": ("big.bin", big, "application/octet-stream")}, headers={**H, "Idempotency-Key": uid()})
    assert r.status_code == 413 and set(r.json()["error"]) == ENVELOPE_KEYS
