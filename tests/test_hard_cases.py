"""The hard cases from the brief, one test each (see evidence/hard_cases.md)."""
import threading

import pytest

from app.mocks.platforms import FAULTS, STORES
from tests.conftest import H, job, make_dest, publish, uid

LONG_TITLE = "A sufficiently long test title"


def run(client, dest, rendition=None, key=None):
    r = publish(client, dest["id"], rendition, key)
    assert r.status_code == 202, r.text
    return job(client, r.json()["job_id"]), r


# ---- 1. timeout + retry with the same idempotency key -------------------------------------------
def test_timeout_then_retry_same_key_creates_one_post(client):
    d = make_dest(client, "discourse")
    key = uid("timeout")
    j1, r1 = run(client, d, {"title": LONG_TITLE + " [[drop]]"}, key)
    assert j1["status"] == "failed"
    assert j1["error"]["code"] == "PLATFORM_UNAVAILABLE" and j1["error"]["retryable"] is True
    assert j1["error"]["details"]["state"] == "unknown"
    assert len(STORES["discourse"]["posts"]) == 1  # the platform DID create it before the timeout
    j2, r2 = run(client, d, {"title": LONG_TITLE + " [[drop]]"}, key)  # client retries with the same key
    assert j2["job_id"] == j1["job_id"]
    assert j2["status"] == "succeeded"
    assert len(STORES["discourse"]["posts"]) == 1  # still exactly one post
    from app.db import Post, session_scope
    db = session_scope()
    assert db.query(Post).filter(Post.idempotency_key == key).count() == 1


def test_same_key_twice_returns_same_job_and_one_post(client):
    d = make_dest(client, "discourse")
    key = uid("dup")
    j1, r1 = run(client, d, key=key)
    j2, r2 = run(client, d, key=key)
    assert j1["job_id"] == j2["job_id"] and r2.json()["idempotent_replay"] is True
    assert len(STORES["discourse"]["posts"]) == 1


def test_same_key_different_request_rejected(client):
    d = make_dest(client, "discourse")
    key = uid("diff")
    run(client, d, key=key)
    r = publish(client, d["id"], {"title": "A completely different long title"}, key)
    assert r.status_code == 422 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_concurrent_requests_same_key_one_post(client):
    d = make_dest(client, "discourse")
    key = uid("conc")
    results = []

    def go():
        results.append(publish(client, d["id"], key=key))

    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ids = {r.json()["job_id"] for r in results if r.status_code == 202}
    assert len(ids) == 1
    assert len(STORES["discourse"]["posts"]) == 1


def test_missing_idempotency_key_rejected(client):
    d = make_dest(client, "discourse")
    r = client.post("/api/v1/publish", json={"destination_id": d["id"], "rendition": {"title": LONG_TITLE}}, headers=H)
    assert r.status_code == 400 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


# ---- 2. 429 with reset time, expired token, rejected content ------------------------------------
def test_429_short_reset_backs_off_and_retries(client):
    d = make_dest(client, "devto")
    j, _ = run(client, d, {"title": LONG_TITLE + " [[429]]"})
    assert j["status"] == "succeeded"
    assert any("backed off" in w for w in j["result"]["manifest"]["warnings"])


def test_429_long_reset_is_surfaced_with_retry_at(client):
    d = make_dest(client, "discourse")
    j, _ = run(client, d, {"title": LONG_TITLE + " [[429long]]"})
    e = j["error"]
    assert e["code"] == "PLATFORM_RATE_LIMIT" and e["retryable"] is True
    assert e["details"]["retry_after_seconds"] == 120 and e["details"]["retry_at"]


def test_daily_quota_exhausted(client):
    d = make_dest(client, "ghost")
    j, _ = run(client, d, {"title": LONG_TITLE + " [[quota]]"})
    assert j["error"]["code"] == "PLATFORM_QUOTA_EXHAUSTED" and j["error"]["retryable"] is True


def test_expired_token(client):
    d = make_dest(client, "discourse")
    client.post("/api/v1/simulation/faults/discourse", json={"auth_expired": True}, headers=H)
    j, _ = run(client, d)
    assert j["error"]["code"] == "CONNECTION_AUTH_EXPIRED" and j["error"]["retryable"] is False


def test_expired_token_via_credentials_env(client, monkeypatch):
    monkeypatch.setenv("MY_DISCOURSE_KEY", "expired-token-123")
    d = make_dest(client, "discourse", credentials_ref="MY_DISCOURSE_KEY")
    j, _ = run(client, d)
    assert j["error"]["code"] == "CONNECTION_AUTH_EXPIRED"


def test_content_rejected_returns_platform_message(client):
    d = make_dest(client, "devto")
    j, _ = run(client, d, {"title": LONG_TITLE + " [[reject]]"})
    assert j["error"]["code"] == "PLATFORM_CONTENT_REJECTED"
    assert "not allowed" in j["error"]["details"]["platform_message"]


# ---- 3. over-long title/body, unsupported media -------------------------------------------------
def test_title_too_long(client):
    d = make_dest(client, "lemmy")
    j, _ = run(client, d, {"title": "x" * 250})
    assert j["error"]["code"] == "DEST_SPEC_VIOLATION" and j["error"]["retryable"] is False
    assert j["error"]["details"]["violations"][0]["field"] == "title"


def test_body_too_long(client):
    d = make_dest(client, "lemmy")
    j, _ = run(client, d, {"body": "b" * 10001})
    assert j["error"]["code"] == "DEST_SPEC_VIOLATION"
    assert any(v["field"] == "body" for v in j["error"]["details"]["violations"])


def test_unsupported_media_type(client):
    d = make_dest(client, "discourse")
    j, _ = run(client, d, {"media": [{"url": "http://x/v.mp4", "content_type": "video/mp4"}]})
    assert j["error"]["code"] == "DEST_SPEC_VIOLATION"
    assert "media[0]" in [v["field"] for v in j["error"]["details"]["violations"]]


# ---- 4. frequency cap / no-promo ----------------------------------------------------------------
def test_second_post_inside_weekly_cap_is_rescheduled(client):
    d = make_dest(client, "discourse", frequency_cap={"max_posts": 1, "window_hours": 168})
    j1, _ = run(client, d)
    assert j1["status"] == "succeeded"
    j2, _ = run(client, d, {"title": "Another sufficiently long title"})
    assert j2["status"] == "failed" and j2["error"]["code"] == "DEST_RULE_FREQUENCY_CAP"
    assert j2["result"]["status"] == "rescheduled" and j2["result"]["reschedule_at"]
    assert len(STORES["discourse"]["posts"]) == 1


def test_promotional_post_to_no_promo_destination_blocked(client):
    d = make_dest(client, "lemmy", self_promo_allowed=False)
    j, _ = run(client, d, {"is_promotional": True})
    assert j["error"]["code"] == "DEST_RULE_VIOLATION" and j["result"]["status"] == "blocked"
    assert "self-promotion" in j["error"]["message"]
    assert len(STORES["lemmy"]["posts"]) == 0


def test_link_policy_forbidden_and_max(client):
    d = make_dest(client, "discourse", link_policy="forbidden")
    j, _ = run(client, d, {"body": "see https://example.com/x"})
    assert j["error"]["code"] == "DEST_RULE_VIOLATION"
    d2 = make_dest(client, "discourse", link_policy="max:1")
    j2, _ = run(client, d2, {"body": "a https://a.com b https://b.com"})
    assert j2["status"] == "failed"
    j3, _ = run(client, d2, {"body": "only https://a.com", "title": "Yet another long enough title"})
    assert j3["status"] == "succeeded"


def test_ai_disclosure_required(client):
    d = make_dest(client, "discourse", disclosure_required=True)
    j, _ = run(client, d, {"ai_generated": True})
    assert j["error"]["code"] == "DEST_RULE_VIOLATION" and "disclosure" in j["error"]["message"].lower()
    j2, _ = run(client, d, {"ai_generated": True, "disclosure": "Written with AI assistance."}, None)
    assert j2["status"] == "succeeded"
    j3, _ = run(client, d, {"ai_generated": False, "title": "Human written long title"})
    assert j3["status"] == "succeeded"


def test_non_english_community(client):
    d = make_dest(client, "discourse", language="hi", name="हिंदी फोरम")
    j, _ = run(client, d, {"title": "नमस्ते दुनिया — यह एक लंबा शीर्षक है", "body": "यह परीक्षण पाठ है", "language": "hi"})
    assert j["status"] == "succeeded"


def test_rule_dry_run_endpoint(client):
    d = make_dest(client, "lemmy", self_promo_allowed=False)
    r = client.post(f"/api/v1/destinations/{d['id']}/check", json={"title": LONG_TITLE, "is_promotional": True}, headers=H)
    assert r.json()["rules"]["action"] == "block"


# ---- 5. renamed field => certification fails and destination is demoted -------------------------
@pytest.mark.parametrize("connector,instance", [("discourse", "discourse"), ("devto", "devto"), ("ghost", "ghost"),
                                                ("lemmy", "lemmy"), ("webhook", "partner-site")])
def test_renamed_field_fails_certification_and_demotes(client, connector, instance):
    d = make_dest(client, connector)
    ok = client.post(f"/api/v1/destinations/{d['id']}/certify", headers={**H, "Idempotency-Key": uid()})
    assert job(client, ok.json()["job_id"])["status"] == "succeeded"
    assert client.get(f"/api/v1/destinations/{d['id']}", headers=H).json()["certified"] is True
    client.post(f"/api/v1/simulation/faults/{instance}", json={"rename": True}, headers=H)
    r = client.post(f"/api/v1/destinations/{d['id']}/certify", headers={**H, "Idempotency-Key": uid()})
    j = job(client, r.json()["job_id"])
    assert j["status"] == "failed" and j["error"]["code"] == "CONTRACT_VIOLATION"
    now = client.get(f"/api/v1/destinations/{d['id']}", headers=H).json()
    assert now["status"] == "demoted" and now["certified"] is False
    blocked, _ = run(client, d)
    assert blocked["error"]["code"] == "DEST_NOT_CERTIFIED"


def test_failed_certification_leaves_no_test_post_behind(client):
    d = make_dest(client, "ghost")
    client.post("/api/v1/simulation/faults/ghost", json={"rename": True}, headers=H)
    client.post(f"/api/v1/destinations/{d['id']}/certify", headers={**H, "Idempotency-Key": uid()})
    assert STORES["ghost"]["posts"] == {}  # cleaned up even though certification failed


# ---- 6. partner revokes / connector kill switch / platform down ---------------------------------
def test_partner_revokes_permission(client):
    d = make_dest(client, "webhook")
    client.post(f"/api/v1/destinations/{d['id']}/revoke", headers=H)
    j, _ = run(client, d)
    assert j["error"]["code"] == "DEST_PERMISSION_REVOKED"


def test_partner_revokes_at_platform_side(client):
    d = make_dest(client, "webhook")
    client.post("/api/v1/simulation/faults/partner-site", json={"revoked": True}, headers=H)
    j, _ = run(client, d)
    assert j["error"]["code"] == "CONNECTION_AUTH_EXPIRED"


def test_connector_kill_switch(client):
    d = make_dest(client, "discourse")
    client.post("/api/v1/connectors/discourse/disable", headers=H)
    j, _ = run(client, d)
    assert j["error"]["code"] == "CONNECTOR_DISABLED"
    client.post("/api/v1/connectors/discourse/enable", headers=H)
    j2, _ = run(client, d)
    assert j2["status"] == "succeeded"


def test_stops_calling_after_five_consecutive_failures(client):
    d = make_dest(client, "discourse")
    client.post("/api/v1/simulation/faults/discourse", json={"down": True}, headers=H)
    for i in range(5):
        j, _ = run(client, d)
        assert j["error"]["code"] == "PLATFORM_UNAVAILABLE"
    calls_before = STORES["discourse"]["calls"]
    j6, _ = run(client, d)
    assert j6["error"]["code"] == "PLATFORM_UNAVAILABLE" and "consecutive" in j6["error"]["message"]
    assert STORES["discourse"]["calls"] == calls_before  # no 6th call reached the platform
