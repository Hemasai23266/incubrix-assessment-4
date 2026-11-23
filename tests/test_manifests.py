"""Integration Standard point 9: every job RESULT carries the same JSON manifest (publish, assist pack, certification, cleanup, media fetch,
blocked and rescheduled publishes). A failed job has an error instead of a result."""
import httpx
import pytest

from app import fetcher, netguard, runtime
from app.config import get_settings
from tests.conftest import H, job, make_dest, publish, uid
from tests.test_fetcher import PNG, resp

REQUIRED = {"schema_version", "kind", "service", "connector", "providers", "models", "settings", "seed", "timings", "cost_estimate_usd",
            "cost_basis", "warnings", "rights"}


def check_manifest(m, kind):
    assert REQUIRED <= set(m), sorted(REQUIRED - set(m))
    assert m["schema_version"] == 1 and m["kind"] == kind
    assert m["service"] == {"name": "incubrix-longtail-distribution", "version": runtime.SERVICE_VERSION}
    assert m["models"] == [] and m["seed"] is None  # honest: no AI model, nothing random
    assert m["cost_estimate_usd"] == 0.0 and isinstance(m["cost_basis"], str)
    assert isinstance(m["warnings"], list) and isinstance(m["providers"], list)
    assert m["timings"]["total_s"] >= 0 and isinstance(m["settings"]["simulation_mode"], bool)
    assert {"licence", "ai_generated", "disclosure_included"} <= set(m["rights"])
    return m


def certify(client, dest_id):
    r = client.post(f"/api/v1/destinations/{dest_id}/certify", headers={**H, "Idempotency-Key": uid("c")})
    return job(client, r.json()["job_id"])


def test_publish_result_manifest(client):
    d = make_dest(client, "discourse")
    j = job(client, publish(client, d["id"], {"ai_generated": True, "disclosure": "AI assisted"}).json()["job_id"])
    m = check_manifest(j["result"]["manifest"], "publish")
    assert m["connector"]["key"] == "discourse" and m["connector"]["version"] and m["rights"]["ai_generated"] is True and m["rights"]["disclosure_included"] is True
    p = m["providers"][0]
    assert p["name"] == "mock-platform" and p["mode"] == "simulation" and "mock" in p["version"]


def test_assist_pack_result_manifest(client):
    d = make_dest(client, "assisted", automation_allowed=False, options={"submit_url": "https://example.org/submit"})
    j = job(client, publish(client, d["id"]).json()["job_id"])
    m = check_manifest(j["result"]["manifest"], "publish")
    assert m["connector"]["key"] == "assisted" and any("manually" in w for w in m["warnings"])


def test_certification_result_manifest_has_per_step_timings(client):
    d = make_dest(client, "ghost")
    j = certify(client, d["id"])
    assert j["status"] == "succeeded"
    m = check_manifest(j["result"]["manifest"], "certification")
    assert m["connector"]["key"] == "ghost" and m["warnings"] == []
    assert set(m["timings"]["steps"]) == {"validate", "publish", "get_status_live", "delete", "verify_deleted"}
    assert m["rights"]["ai_generated"] is False and "synthetic" in m["rights"]["licence"]
    assert m["settings"]["environment"] == "simulation"


def test_failed_certification_result_still_has_a_manifest_with_the_reason(client):
    d = make_dest(client, "ghost")
    client.post("/api/v1/simulation/faults/ghost", json={"rename": True}, headers=H)
    j = certify(client, d["id"])
    assert j["status"] == "failed" and j["error"]["code"] == "CONTRACT_VIOLATION"
    m = check_manifest(j["result"]["manifest"], "certification")
    assert m["warnings"] == ["certification failed: CONTRACT_VIOLATION"]


def test_assisted_certification_manifest(client):
    d = make_dest(client, "assisted", automation_allowed=False, options={"submit_url": "https://example.org/submit"})
    m = check_manifest(certify(client, d["id"])["result"]["manifest"], "certification")
    assert m["connector"]["tier"] == "assisted" and list(m["timings"]["steps"]) == ["build_assist_pack"]


def test_cleanup_result_manifest(client):
    r = client.post("/api/v1/maintenance/cleanup-test-posts", headers={**H, "Idempotency-Key": uid("m")})
    j = job(client, r.json()["job_id"])
    assert j["status"] == "succeeded" and j["result"]["deleted"] == 0
    m = check_manifest(j["result"]["manifest"], "cleanup")
    assert m["connector"] is None and m["providers"] == [] and m["settings"]["max_test_post_lifetime_seconds"] == get_settings().max_test_post_lifetime_seconds


def test_blocked_publish_result_has_a_manifest(client):
    d = make_dest(client, "lemmy", self_promo_allowed=False)
    j = job(client, publish(client, d["id"], {"is_promotional": True}).json()["job_id"])
    assert j["status"] == "failed" and j["result"]["status"] == "blocked"
    m = check_manifest(j["result"]["manifest"], "publish")
    assert m["connector"]["key"] == "lemmy" and "self-promotion" in m["warnings"][0] and m["outcome"] == "not sent to the platform"


def test_rescheduled_publish_result_has_a_manifest(client):
    d = make_dest(client, "discourse", frequency_cap={"max_posts": 1, "window_hours": 168})
    publish(client, d["id"])
    j = job(client, publish(client, d["id"], {"title": "Another sufficiently long title"}).json()["job_id"])
    assert j["result"]["status"] == "rescheduled" and j["result"]["reschedule_at"]
    m = check_manifest(j["result"]["manifest"], "publish")
    assert "Frequency cap" in m["warnings"][0]


def test_media_fetch_result_manifest(client, monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr(fetcher, "TRANSPORT", httpx.MockTransport(lambda r: resp(200, {"content-type": "image/png"}, PNG)))
    r = client.post("/api/v1/files/from-url", json={"url": "https://cdn.example.com/a.png"}, headers={**H, "Idempotency-Key": uid("f")})
    m = check_manifest(job(client, r.json()["job_id"])["result"]["manifest"], "media_fetch")
    assert m["connector"] is None and m["providers"][0]["name"] == "http-fetch" and m["rights"]["ai_generated"] is None
    assert m["settings"]["allowed_ports"] == [80, 443, 8080, 8443] and "image/png" in m["settings"]["allowed_types"]


def test_failed_jobs_carry_an_error_envelope_instead_of_a_result(client):
    d = make_dest(client, "lemmy")
    j = job(client, publish(client, d["id"], {"title": "x" * 300}).json()["job_id"])
    assert j["status"] == "failed" and j["result"] is None and set(j["error"]) == {"code", "message", "retryable", "details"}


def test_manifest_never_invents_values_in_live_mode(monkeypatch):
    monkeypatch.setattr(get_settings(), "simulation_mode", False)
    m = runtime.make_manifest("publish", 0.0, ["w"], connector_key="devto", rendition={"ai_generated": False})
    check_manifest(m, "publish")
    assert m["providers"] == [{"name": "devto", "mode": "live", "version": None}]  # unknown platform version stays null, not made up
    assert m["models"] == [] and m["cost_estimate_usd"] == 0.0 and m["warnings"] == ["w"]


@pytest.mark.parametrize("kind", ["publish", "certification", "cleanup", "media_fetch"])
def test_manifest_builder_is_consistent_for_every_kind(kind):
    import time
    m = runtime.make_manifest(kind, time.time() - 1.5, ["note"], extra_settings={"x": 1}, step_timings={"a": 0.1})
    check_manifest(m, kind)
    assert 1.4 <= m["timings"]["total_s"] <= 3 and m["timings"]["steps"] == {"a": 0.1} and m["settings"]["x"] == 1
