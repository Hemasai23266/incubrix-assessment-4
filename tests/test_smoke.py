from tests.conftest import H, job, make_dest, publish, uid


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_publish_and_certify_all_connectors(client):
    for c in ["discourse", "lemmy", "devto", "ghost", "webhook", "assisted"]:
        d = make_dest(client, c)
        r = client.post(f"/api/v1/destinations/{d['id']}/certify", headers={**H, "Idempotency-Key": uid()})
        assert r.status_code == 202
        j = job(client, r.json()["job_id"])
        assert j["status"] == "succeeded", (c, j)
