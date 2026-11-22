import os
import tempfile

os.environ.update({
    "SIMULATION_MODE": "true", "JOB_BACKEND": "inline", "API_TOKEN": "test-token",
    "DATABASE_URL": "sqlite:///:memory:", "S3_ENDPOINT_URL": "", "REDIS_URL": "",
    "LOCAL_STORE_DIR": tempfile.mkdtemp(prefix="incubrix-test-"), "MAX_INLINE_BACKOFF_SECONDS": "3",
    "RATE_LIMIT_PER_MINUTE": "0",
})

import pytest
from fastapi.testclient import TestClient

from app.config import reset_settings
from app.db import init_db, reset_db_engine
from app.mocks.platforms import reset_mocks

H = {"Authorization": "Bearer test-token"}


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    reset_settings()
    reset_db_engine()
    init_db()
    reset_mocks()
    import app.main as m
    m._hits.clear()
    yield


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


_n = {"i": 0}


def uid(prefix="k"):
    _n["i"] += 1
    return f"{prefix}-{_n['i']}-{os.urandom(3).hex()}"


def make_dest(client, connector="discourse", **over):
    body = {"name": f"{connector} test", "url": f"https://{connector}.example", "platform_key": connector,
            "surface": {"discourse": "topic", "lemmy": "post", "devto": "article", "ghost": "post",
                        "webhook": "partner_post", "assisted": "manual_post"}[connector],
            "connector_key": connector, "self_promo_allowed": True, "options": {}}
    if connector == "lemmy":
        body["options"] = {"community": "test"}
    body.update(over)
    r = client.post("/api/v1/destinations", json=body, headers={**H, "Idempotency-Key": uid("dest")})
    assert r.status_code in (200, 201), r.text
    return r.json()


def publish(client, dest_id, rendition=None, key=None):
    rend = {"title": "A sufficiently long test title", "body": "Hello world body text"}
    rend.update(rendition or {})
    key = key or uid("pub")
    r = client.post("/api/v1/publish", json={"destination_id": dest_id, "rendition": rend},
                    headers={**H, "Idempotency-Key": key})
    return r


def job(client, job_id):
    return client.get(f"/api/v1/jobs/{job_id}", headers=H).json()
