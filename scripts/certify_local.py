"""Certify destinations against the REAL local Ghost and Lemmy containers (SIMULATION_MODE=false).

  python scripts/certify_local.py          # needs `docker compose up` + `docker compose --profile seed run --rm seed`
Reads GHOST_ADMIN_KEY from .env; the Lemmy password is the local throw-away one from docker/lemmy.hjson.
Writes the evidence JSON to evidence/local_instances/.
"""
import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for line in (ROOT / ".env").read_text().splitlines():
    if line.startswith("GHOST_ADMIN_KEY="):
        os.environ["GHOST_ADMIN_KEY"] = line.split("=", 1)[1]
os.environ.update({"SIMULATION_MODE": "false", "JOB_BACKEND": "inline", "API_TOKEN": "local-token",
                   "DATABASE_URL": f"sqlite:///{tempfile.mkdtemp()}/local.db", "LOCAL_STORE_DIR": str(ROOT / "evidence" / "local_instances_store"),
                   "S3_ENDPOINT_URL": "", "LEMMY_PASSWORD": "incubrix-local-only"})

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

H = {"Authorization": "Bearer local-token"}
OUT = ROOT / "evidence" / "local_instances"
OUT.mkdir(parents=True, exist_ok=True)

DESTS = [
    {"name": "Local Ghost", "url": "http://localhost:2368", "platform_key": "ghost", "surface": "post", "connector_key": "ghost",
     "credentials_ref": "GHOST_ADMIN_KEY", "self_promo_allowed": True, "options": {"base_url": "http://localhost:2368"}},
    {"name": "Local Lemmy (incubrix_test)", "url": "http://localhost:8536", "platform_key": "lemmy", "surface": "post",
     "connector_key": "lemmy", "credentials_ref": "LEMMY_PASSWORD", "self_promo_allowed": True,
     "options": {"base_url": "http://localhost:8536", "community": "incubrix_test", "username": "incubrix"}},
]

results = []
with TestClient(app) as c:
    for i, d in enumerate(DESTS):
        r = c.post("/api/v1/destinations", json=d, headers={**H, "Idempotency-Key": f"local-dest-{i}"})
        did = r.json()["id"]
        r = c.post(f"/api/v1/destinations/{did}/certify", headers={**H, "Idempotency-Key": f"local-cert-{i}"})
        j = c.get(f"/api/v1/jobs/{r.json()['job_id']}", headers=H).json()
        res = j["result"] or {}
        results.append({"destination": d["name"], "job_status": j["status"], "error": j["error"], "steps": res.get("steps"),
                        "duration_s": res.get("duration_s")})
        (OUT / f"certification_{d['platform_key']}.json").write_text(json.dumps({"job": j}, indent=2, default=str), encoding="utf-8")
        print(d["name"], "->", j["status"], "in", res.get("duration_s"), "s", j["error"] or "")
        for s in res.get("steps") or []:
            print("   ", s["step"], "OK" if s["ok"] else "FAIL", s.get("error", {}).get("message", ""))
(OUT / "summary.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
