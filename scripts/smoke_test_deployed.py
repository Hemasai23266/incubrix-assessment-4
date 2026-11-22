"""End-to-end smoke test against a DEPLOYED demo URL (simulation mode only; no real platform is ever contacted).

  python scripts/smoke_test_deployed.py https://<your-service>.onrender.com <API_TOKEN>

Every request carries a unique Idempotency-Key, so re-running is safe. The first request may take ~1 minute on a sleeping free
host (cold start); the script reports how long it took.
"""
import sys
import time
import uuid

import httpx

if len(sys.argv) != 3:
    sys.exit(__doc__)
base, token = sys.argv[1].rstrip("/"), sys.argv[2]
c = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=120)
run = uuid.uuid4().hex[:8]
fails = 0


def check(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)


def wait(jid, t=90):
    t0 = time.time()
    while time.time() - t0 < t:
        j = c.get(f"/api/v1/jobs/{jid}").json()
        if j["status"] in ("succeeded", "failed"):
            return j
        time.sleep(1)
    return j


t0 = time.time()
r = httpx.get(f"{base}/health", timeout=180)
check("GET /health (cold-start wait included)", r.status_code == 200, f"{time.time() - t0:.1f} s")
rd = httpx.get(f"{base}/ready", timeout=30).json()
check("GET /ready: simulation mode, database/redis/storage ok", rd.get("mode") == "simulation" and all(v == "ok" for v in rd["checks"].values()), str(rd["checks"]))
check("no token -> 401 error envelope", httpx.get(f"{base}/api/v1/destinations").json()["error"]["code"] == "UNAUTHORIZED")
check("demo catalogue present", len([d for d in c.get("/api/v1/destinations").json()["destinations"] if d["id"].startswith("demo-")]) >= 12)
r = c.post("/api/v1/publish", json={"destination_id": "demo-ghost", "rendition": {"title": "Smoke test publish title", "body": "hello"}}, headers={"Idempotency-Key": f"smoke-{run}"})
check("POST /publish -> 202 + job_id", r.status_code == 202)
j = wait(r.json()["job_id"])
check("job succeeded with output manifest", j["status"] == "succeeded" and "manifest" in j["result"], j["status"])
r2 = c.post("/api/v1/publish", json={"destination_id": "demo-ghost", "rendition": {"title": "Smoke test publish title", "body": "hello"}}, headers={"Idempotency-Key": f"smoke-{run}"})
check("same Idempotency-Key -> same job", r2.json()["job_id"] == r.json()["job_id"] and r2.json()["idempotent_replay"])
cj = wait(c.post("/api/v1/destinations/demo-lemmy/certify", headers={"Idempotency-Key": f"smoke-cert-{run}"}).json()["job_id"])
check("certification job (publish->status->delete->verify, MOCK platform)", cj["status"] == "succeeded", cj["status"])
bad = wait(c.post("/api/v1/publish", json={"destination_id": "demo-lemmy", "rendition": {"title": "x" * 300, "body": "b"}}, headers={"Idempotency-Key": f"smoke-long-{run}"}).json()["job_id"])
check("over-long title -> DEST_SPEC_VIOLATION", bad["error"]["code"] == "DEST_SPEC_VIOLATION")
ssrf = c.post("/api/v1/publish", json={"destination_id": "demo-ghost", "rendition": {"title": "Smoke test callback check", "body": "b"}, "callback_url": "https://169.254.169.254/latest/meta-data/"},
              headers={"Idempotency-Key": f"smoke-ssrf-{run}"})
check("unsafe callback_url (cloud metadata address) -> 422 CALLBACK_URL_REJECTED", ssrf.status_code == 422 and ssrf.json()["error"]["code"] == "CALLBACK_URL_REJECTED")
up = c.post("/api/v1/files", files={"file": ("smoke.txt", b"smoke-bytes", "text/plain")}, headers={"Idempotency-Key": f"smoke-up-{run}"})
check("file upload + download", up.status_code == 201 and c.get(f"/api/v1/files/{up.json()['key']}").content == b"smoke-bytes")
print(f"\n{'ALL PASSED' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
