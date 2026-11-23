"""Integration Standard point 4: EVERY POST route requires Idempotency-Key (400 IDEMPOTENCY_KEY_REQUIRED without it). Formerly optional on the six state-setting/read-only
POST routes (connector enable/disable, destination revoke/check, simulation faults, connector contract) accept it and replay the first answer."""
import threading
import time
from datetime import timedelta

import pytest

from app.db import IdempotentResponse, session_scope, utcnow
from app.mocks.platforms import FAULTS, STORES
from tests.conftest import H, job, make_dest, publish, uid

REND = {"title": "A sufficiently long test title", "body": "hello"}


def post(client, path, body=None, key=None, **kw):
    headers = {**H, **({"Idempotency-Key": key} if key is not None else {"X-No-Auto-Key": "1"})}
    return client.post(path, json=body if body is not None else {}, headers=headers, **kw)


@pytest.fixture
def dest(client):
    return make_dest(client, "discourse")


def six_routes(dest):
    return [
        ("disable", "/api/v1/connectors/discourse/disable", {}),
        ("enable", "/api/v1/connectors/discourse/enable", {}),
        ("revoke", f"/api/v1/destinations/{dest['id']}/revoke", {}),
        ("check", f"/api/v1/destinations/{dest['id']}/check", REND),
        ("faults", "/api/v1/simulation/faults/discourse", {"rename": True}),
        ("contract", "/connector/v1/discourse/validate", {"rendition": REND, "destination": {"surface": "topic"}}),
    ]


def test_missing_key_is_rejected_on_every_post_route(client, dest):
    for name, url, body in six_routes(dest):
        r = post(client, url, body)
        assert r.status_code == 400 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED", (name, r.status_code, r.text)
        assert r.json()["error"]["retryable"] is False
    assert post(client, "/api/v1/destinations", {"name": "n", "url": "https://x.example", "platform_key": "discourse", "connector_key": "discourse"}).json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    r = client.post("/api/v1/files", files={"file": ("a.txt", b"x", "text/plain")}, headers={**H, "X-No-Auto-Key": "1"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert post(client, "/api/v1/publish", {"destination_id": dest["id"], "rendition": REND}).json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert post(client, f"/api/v1/destinations/{dest['id']}/certify").json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert post(client, "/api/v1/maintenance/cleanup-test-posts").json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert post(client, "/api/v1/files/from-url", {"url": "https://example.com/a.png"}).json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


BAD_KEYS = ["has space", "tab\tkey", "x" * 201, "ключ-unicode", "emoji-\U0001F600"]


@pytest.mark.parametrize("bad", BAD_KEYS, ids=["space", "tab", "201chars", "unicode", "emoji"])
def test_invalid_keys_are_rejected_on_every_post_route(client, dest, bad):
    routes = six_routes(dest) + [("publish", "/api/v1/publish", {"destination_id": dest["id"], "rendition": REND}),
                                 ("certify", f"/api/v1/destinations/{dest['id']}/certify", {}),
                                 ("from-url", "/api/v1/files/from-url", {"url": "https://example.com/a.png"}),
                                 ("create", "/api/v1/destinations", {"name": "n", "url": "https://x.example", "platform_key": "discourse", "connector_key": "discourse"})]
    for name, url, body in routes:
        try:
            r = client.post(url, json=body, headers={**H, "Idempotency-Key": bad})
        except (UnicodeError, ValueError):  # values the HTTP client itself refuses to send never reach the server
            continue
        assert r.status_code == 400 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_INVALID", (name, r.status_code, r.text)


def test_disable_enable_replay_does_not_run_the_work_again(client):
    k1, k2 = uid("dis"), uid("en")
    first = post(client, "/api/v1/connectors/discourse/disable", key=k1)
    assert first.json() == {"key": "discourse", "disabled": True}
    post(client, "/api/v1/connectors/discourse/enable", key=k2)
    replay = post(client, "/api/v1/connectors/discourse/disable", key=k1)
    assert replay.json() == first.json() and replay.headers["Idempotent-Replay"] == "true"
    states = {c["key"]: c["disabled"] for c in client.get("/api/v1/connectors", headers=H).json()["connectors"]}
    assert states["discourse"] is False  # the replay did NOT disable it again
    assert post(client, "/api/v1/connectors/discourse/enable", key=k2).headers["Idempotent-Replay"] == "true"


def test_revoke_replay_is_not_reapplied(client, dest):
    key = uid("rev")
    first = post(client, f"/api/v1/destinations/{dest['id']}/revoke", key=key)
    assert first.json()["status"] == "revoked"
    from app.db import Destination
    db = session_scope()
    d = db.get(Destination, dest["id"])
    d.status = "active"
    db.commit()
    db.close()
    replay = post(client, f"/api/v1/destinations/{dest['id']}/revoke", key=key)
    assert replay.headers["Idempotent-Replay"] == "true" and replay.json() == first.json()
    assert client.get(f"/api/v1/destinations/{dest['id']}", headers=H).json()["status"] == "active"


def test_faults_replay_is_not_reapplied(client):
    k1, k2 = uid("f1"), uid("f2")
    first = post(client, "/api/v1/simulation/faults/ghost", {"rename": True}, key=k1)
    assert first.json()["faults"]["rename"] is True
    post(client, "/api/v1/simulation/faults/ghost", {"rename": False}, key=k2)
    replay = post(client, "/api/v1/simulation/faults/ghost", {"rename": True}, key=k1)
    assert replay.headers["Idempotent-Replay"] == "true" and replay.json() == first.json()
    assert FAULTS["ghost"]["rename"] is False


def test_check_replay_runs_the_rules_once(client, dest, monkeypatch):
    import app.rules as rules
    calls = []
    real = rules.evaluate
    monkeypatch.setattr(rules, "evaluate", lambda *a, **k: calls.append(1) or real(*a, **k))
    key = uid("chk")
    a = post(client, f"/api/v1/destinations/{dest['id']}/check", REND, key=key)
    b = post(client, f"/api/v1/destinations/{dest['id']}/check", REND, key=key)
    assert a.json() == b.json() and b.headers["Idempotent-Replay"] == "true" and len(calls) == 1


def test_contract_publish_replay_creates_one_platform_post(client, dest):
    body = {"rendition": REND, "destination": dest}
    hk0 = {**H, "Idempotency-Key": uid("pub")}
    a = client.post("/connector/v1/discourse/publish", json=body, headers=hk0)
    b = client.post("/connector/v1/discourse/publish", json=body, headers=hk0)
    assert a.status_code == b.status_code == 200 and a.json() == b.json() and b.headers["Idempotent-Replay"] == "true"
    assert len(STORES["discourse"]["posts"]) == 1
    hk = uid("hdr")
    body2 = {"rendition": {**REND, "title": "Another sufficiently long title"}, "destination": dest}
    c = client.post("/connector/v1/discourse/publish", json=body2, headers={**H, "Idempotency-Key": hk})
    d = client.post("/connector/v1/discourse/publish", json=body2, headers={**H, "Idempotency-Key": hk})
    assert c.json() == d.json() and d.headers["Idempotent-Replay"] == "true"
    assert len(STORES["discourse"]["posts"]) == 2


def test_same_key_with_a_different_request_is_rejected_on_every_route(client, dest):
    for url, body1, body2 in [(f"/api/v1/destinations/{dest['id']}/check", REND, {"title": "A different sufficiently long title"}),
                              ("/api/v1/simulation/faults/ghost", {"rename": True}, {"rename": False})]:
        key = uid("reuse")
        assert post(client, url, body1, key=key).status_code == 200
        r = post(client, url, body2, key=key)
        assert r.status_code == 422 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    key = uid("reuse-contract")
    body, hk = {"rendition": REND, "destination": dest}, {**H, "Idempotency-Key": key}
    assert client.post("/connector/v1/discourse/publish", json=body, headers=hk).status_code == 200
    r = client.post("/connector/v1/discourse/publish", json={**body, "rendition": {**REND, "title": "Totally different title here"}}, headers=hk)
    assert r.status_code == 422 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert len(STORES["discourse"]["posts"]) == 1


def test_concurrent_identical_requests_run_the_work_exactly_once(file_db, client, dest):
    body, hk = {"rendition": REND, "destination": dest}, {**H, "Idempotency-Key": uid("conc")}
    results = []

    def go():
        results.append(client.post("/connector/v1/discourse/publish", json=body, headers=hk))

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(r.status_code == 200 for r in results), [r.text for r in results if r.status_code != 200]
    assert len({r.text for r in results}) == 1
    assert sum(1 for r in results if "Idempotent-Replay" not in r.headers) == 1  # exactly one owner
    assert len(STORES["discourse"]["posts"]) == 1


def test_concurrent_faults_requests_apply_once(file_db, client):
    key, results = uid("cf"), []
    ts = [threading.Thread(target=lambda: results.append(post(client, "/api/v1/simulation/faults/lemmy", {"down": True}, key=key))) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert [r.status_code for r in results] == [200] * 6 and sum("Idempotent-Replay" not in r.headers for r in results) == 1


def test_a_failed_request_releases_the_key_so_the_retry_can_run(client, dest):
    key = uid("fail")
    bad, hk = {"rendition": {"title": "x" * 400, "body": "b"}, "destination": dest}, {**H, "Idempotency-Key": key}
    for _ in range(2):  # an error is not a stored answer: the key stays usable
        r = client.post("/connector/v1/discourse/publish", json=bad, headers=hk)
        assert r.status_code == 422 and r.json()["error"]["code"] == "DEST_SPEC_VIOLATION"
    ok = client.post("/connector/v1/discourse/publish", json={**bad, "rendition": REND}, headers=hk)
    assert ok.status_code == 200 and "Idempotent-Replay" not in ok.headers


def test_same_key_on_different_routes_does_not_collide(client):
    key = uid("shared")
    assert post(client, "/api/v1/connectors/discourse/disable", key=key).status_code == 200
    r = post(client, "/api/v1/connectors/lemmy/disable", key=key)
    assert r.status_code == 200 and "Idempotent-Replay" not in r.headers
    assert post(client, "/api/v1/connectors/discourse/enable", key=key).status_code == 200


def test_in_progress_claim_waits_then_answers_409_retryable(client, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m.get_settings(), "idempotency_wait_seconds", 0.3)
    key = uid("busy")
    db = session_scope()
    db.add(IdempotentResponse(scope="disable:discourse", key=key, request_hash=m._hash({"key": "discourse"}), state="in_progress"))
    db.commit()
    db.close()
    t0 = time.time()
    r = post(client, "/api/v1/connectors/discourse/disable", key=key)
    err = r.json()["error"]
    assert r.status_code == 409 and err["code"] == "IDEMPOTENCY_IN_PROGRESS" and err["retryable"] is True and time.time() - t0 < 3


def test_stale_in_progress_claim_from_a_dead_request_is_released(client):
    import app.main as m
    key = uid("stale")
    db = session_scope()
    db.add(IdempotentResponse(scope="disable:discourse", key=key, request_hash=m._hash({"key": "discourse"}), state="in_progress",
                              created_at=utcnow() - timedelta(minutes=5)))
    db.commit()
    db.close()
    r = post(client, "/api/v1/connectors/discourse/disable", key=key)
    assert r.status_code == 200 and r.json()["disabled"] is True


def test_job_routes_keep_their_behaviour(client, dest):
    key = uid("job")
    r1, r2 = publish(client, dest["id"], key=key), publish(client, dest["id"], key=key)
    assert r1.json()["job_id"] == r2.json()["job_id"] and r2.json()["idempotent_replay"] is True
    assert job(client, r1.json()["job_id"])["status"] == "succeeded" and len(STORES["discourse"]["posts"]) == 1


def test_contract_route_requires_the_header_for_every_method_including_publish(client, dest):
    body = {"rendition": REND, "destination": {"surface": "topic"}}
    for method in ("validate", "get_status", "delete", "build_assist_pack"):
        r = client.post(f"/connector/v1/discourse/{method}", json=body, headers={**H, "X-No-Auto-Key": "1"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED", (method, r.text)
    r = client.post("/connector/v1/discourse/publish", json=body, headers={**H, "X-No-Auto-Key": "1"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    # the Appendix A body field is NOT a substitute for the header
    r = client.post("/connector/v1/discourse/publish", json={**body, "idempotency_key": uid("body")}, headers={**H, "X-No-Auto-Key": "1"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert len(STORES["discourse"]["posts"]) == 0
    ok = client.post("/connector/v1/discourse/publish", json=body, headers={**H, "Idempotency-Key": uid("hdr")})
    assert ok.status_code == 200 and ok.json()["status"] == "published"


def test_upload_limit_comes_from_settings(client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "upload_max_bytes", 10)
    r = client.post("/api/v1/files", files={"file": ("a.bin", b"x" * 11, "application/octet-stream")}, headers={**H, "Idempotency-Key": uid("up")})
    assert r.status_code == 413 and r.json()["error"]["details"] == {"size": 11, "limit": 10}
    ok = client.post("/api/v1/files", files={"file": ("a.bin", b"x" * 10, "application/octet-stream")}, headers={**H, "Idempotency-Key": uid("up")})
    assert ok.status_code == 201


def test_openapi_marks_the_header_required_on_every_post_operation_and_all_posts_reject_a_missing_header(client):
    spec = client.get("/openapi.json").json()
    posts = [(p, ops["post"]) for p, ops in spec["paths"].items() if "post" in ops]
    assert len(posts) == 12
    for path, op in posts:
        hs = [x for x in op.get("parameters", []) if x["name"].lower() == "idempotency-key"]
        assert hs and hs[0]["required"] is True, path
