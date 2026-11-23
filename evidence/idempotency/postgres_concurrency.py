# Evidence script (re-run from the repo root with the project venv). Output of the last run is in the matching .txt file.
import os, sys, tempfile, threading, time, json
os.environ.update({"DATABASE_URL": "postgresql+psycopg://t:t@localhost:55432/t", "SIMULATION_MODE": "true", "JOB_BACKEND": "inline", "API_TOKEN": "pgtest-token", "S3_ENDPOINT_URL": "", "LOCAL_STORE_DIR": tempfile.mkdtemp(), "RATE_LIMIT_PER_MINUTE": "0"})
sys.path.insert(0, ".")
from fastapi.testclient import TestClient
from app.main import app
from app.mocks.platforms import STORES
from app.db import get_engine
H = {"Authorization": "Bearer pgtest-token"}
print("engine:", get_engine().dialect.name)
with TestClient(app) as c:
    d = c.post("/api/v1/destinations", json={"name": "pg", "url": "https://x.example", "platform_key": "discourse", "surface": "topic", "connector_key": "discourse"}, headers={**H, "Idempotency-Key": "pg-d1"}).json()
    rend = {"title": "A sufficiently long test title", "body": "hello"}
    out = {}
    def storm(label, fn, n=16):
        res = []
        ts = [threading.Thread(target=lambda: res.append(fn())) for _ in range(n)]
        [t.start() for t in ts]; [t.join() for t in ts]
        out[label] = res
        return res
    body = {"rendition": rend, "destination": d}
    r = storm("contract publish x16", lambda: c.post("/connector/v1/discourse/publish", json=body, headers={**H, "Idempotency-Key": "pg-contract-1"}))
    print("contract publish, 16 concurrent same key: statuses", sorted({x.status_code for x in r}), "| distinct bodies", len({x.text for x in r}), "| owners (no replay header)", sum("Idempotent-Replay" not in x.headers for x in r), "| platform posts created", len(STORES["discourse"]["posts"]))
    r = storm("faults x16", lambda: c.post("/api/v1/simulation/faults/ghost", json={"down": True}, headers={**H, "Idempotency-Key": "pg-faults-1"}))
    print("faults, 16 concurrent same key: statuses", sorted({x.status_code for x in r}), "| owners", sum("Idempotent-Replay" not in x.headers for x in r))
    r = storm("disable x16", lambda: c.post("/api/v1/connectors/lemmy/disable", headers={**H, "Idempotency-Key": "pg-dis-1"}))
    print("disable, 16 concurrent same key: statuses", sorted({x.status_code for x in r}), "| owners", sum("Idempotent-Replay" not in x.headers for x in r))
    r = storm("publish job x16", lambda: c.post("/api/v1/publish", json={"destination_id": d["id"], "rendition": {**rend, "title": "Job level concurrent publish title"}}, headers={**H, "Idempotency-Key": "pg-job-1"}))
    print("publish JOB, 16 concurrent same key: statuses", sorted({x.status_code for x in r}), "| distinct job ids", len({x.json()["job_id"] for x in r}))
    print("platform posts after everything:", len(STORES["discourse"]["posts"]), "(expected 2: one contract publish + one job publish)")
    r = c.post("/api/v1/files/from-url", json={"url": "http://169.254.169.254/latest/meta-data/"}, headers={**H, "Idempotency-Key": "pg-fu"})
    print("from-url metadata address:", r.status_code, r.json()["error"]["code"])
    print("405 envelope on postgres app:", c.get("/api/v1/publish", headers=H).json()["error"]["code"])
    cert = c.post(f"/api/v1/destinations/{d['id']}/certify", headers={**H, "Idempotency-Key": "pg-cert"}).json()
    j = c.get(f"/api/v1/jobs/{cert['job_id']}", headers=H).json()
    print("certify on postgres:", j["status"], "| manifest kind:", j["result"]["manifest"]["kind"])
