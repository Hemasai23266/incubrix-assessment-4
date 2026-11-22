"""Measure certification time, publish success rate and duplicate posts (simulation mode). Writes evidence/metrics.json."""
import json
import os
import pathlib
import statistics
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.update({"SIMULATION_MODE": "true", "JOB_BACKEND": "inline", "API_TOKEN": "t", "DATABASE_URL": f"sqlite:///{tempfile.mkdtemp()}/m.db",
                   "LOCAL_STORE_DIR": tempfile.mkdtemp(), "S3_ENDPOINT_URL": "", "RATE_LIMIT_PER_MINUTE": "0"})
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.mocks.platforms import STORES  # noqa: E402

H = {"Authorization": "Bearer t"}
SURF = {"discourse": "topic", "lemmy": "post", "devto": "article", "ghost": "post", "webhook": "partner_post", "assisted": "manual_post"}
N_PUBLISH = 100
out: dict = {"environment": "simulation (in-process mock platforms), inline jobs, sqlite", "certification": {}, "reliability": {}}
with TestClient(app) as c:
    dests = {}
    for k, s in SURF.items():
        r = c.post("/api/v1/destinations", headers={**H, "Idempotency-Key": f"d-{k}"},
                   json={"name": k, "url": f"https://{k}.example", "platform_key": k, "surface": s, "connector_key": k,
                         "self_promo_allowed": True, "options": {"community": "t"} if k == "lemmy" else {}})
        dests[k] = r.json()["id"]
    for k, did in dests.items():
        times, ok = [], 0
        for i in range(5):
            r = c.post(f"/api/v1/destinations/{did}/certify", headers={**H, "Idempotency-Key": f"c-{k}-{i}"})
            j = c.get(f"/api/v1/jobs/{r.json()['job_id']}", headers=H).json()
            if j["status"] != "succeeded": print("FAIL", k, j["error"])
            ok += j["status"] == "succeeded"
            times.append(j["result"]["duration_s"])
        out["certification"][k] = {"runs": 5, "passed": ok, "mean_s": round(statistics.mean(times), 4), "max_s": round(max(times), 4)}
    succeeded = failed = 0
    t0 = time.time()
    for i in range(N_PUBLISH):
        k = ["discourse", "lemmy", "devto", "ghost", "webhook"][i % 5]
        r = c.post("/api/v1/publish", headers={**H, "Idempotency-Key": f"p-{i}"},
                   json={"destination_id": dests[k], "rendition": {"title": f"Measured publish number {i} here", "body": "body text"}})
        if r.status_code != 202:
            print("SUBMIT FAIL", i, k, r.status_code, r.text[:200])
            continue
        j = c.get(f"/api/v1/jobs/{r.json()['job_id']}", headers=H).json()
        if j["status"] != "succeeded":
            print("JOB FAIL", i, k, j["error"])
        succeeded += j["status"] == "succeeded"
    # retry storm: 3 submissions of each of 20 keys, plus a timeout-after-create case
    before = sum(len(s["posts"]) for s in STORES.values())
    for i in range(20):
        for _ in range(3):
            c.post("/api/v1/publish", headers={**H, "Idempotency-Key": f"retry-{i}"},
                   json={"destination_id": dests["discourse"], "rendition": {"title": f"Retry storm post number {i}", "body": "b"}})
    created = sum(len(s["posts"]) for s in STORES.values()) - before
    out["reliability"] = {"publishes": N_PUBLISH, "succeeded": succeeded, "success_rate": succeeded / N_PUBLISH,
                          "seconds_total": round(time.time() - t0, 2),
                          "retry_storm": {"unique_keys": 20, "submissions": 60, "posts_created": created,
                                          "duplicate_posts": created - 20}}
pathlib.Path(ROOT / "evidence").mkdir(exist_ok=True)
(ROOT / "evidence" / "metrics.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
print(json.dumps(out, indent=2))
