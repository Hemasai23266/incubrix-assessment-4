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


class AutoKeyClient(TestClient):
    """Every POST now REQUIRES an Idempotency-Key. Most tests are not about that, so this client adds a fresh key to a POST that has none.
    A test that checks the missing-key behaviour sends the marker header X-No-Auto-Key (it is removed before the request is sent)."""

    def request(self, method, url, **kw):  # type: ignore[override]
        if method.upper() == "POST":
            headers = dict(kw.get("headers") or {})
            if headers.pop("X-No-Auto-Key", None) is None and "Idempotency-Key" not in headers:
                headers["Idempotency-Key"] = uid("auto")
            kw["headers"] = headers
        return super().request(method, url, **kw)


@pytest.fixture
def client():
    from app.main import app
    with AutoKeyClient(app) as c:
        yield c


@pytest.fixture
def file_db(monkeypatch, tmp_path):
    """Concurrency needs real separate connections (as in production). The default in-memory test database shares ONE connection between
    threads, which cannot isolate concurrent transactions, so these tests use a file-backed SQLite database like the demo image does."""
    from app.config import reset_settings
    from app.db import init_db, reset_db_engine
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'idem.db'}")
    reset_settings()
    reset_db_engine()
    init_db()
    yield
    reset_db_engine()


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
