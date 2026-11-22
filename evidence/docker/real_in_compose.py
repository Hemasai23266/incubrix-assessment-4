import json, os, sys, tempfile
os.environ.update({"SIMULATION_MODE": "false", "JOB_BACKEND": "inline", "API_TOKEN": "t",
                   "DATABASE_URL": "sqlite:///" + tempfile.mkdtemp() + "/x.db", "S3_ENDPOINT_URL": "http://s3:8333",
                   "S3_ACCESS_KEY": "local-access-key", "S3_SECRET_KEY": "local-secret-key"})
assert os.environ.get("GHOST_ADMIN_KEY"), "GHOST_ADMIN_KEY missing in container env"
from fastapi.testclient import TestClient
from app.main import app
H = {"Authorization": "Bearer t"}
D = [("ghost", {"name": "Ghost via compose network", "url": "http://ghost:2368", "platform_key": "ghost", "surface": "post", "connector_key": "ghost",
      "credentials_ref": "GHOST_ADMIN_KEY", "self_promo_allowed": True, "options": {"base_url": "http://ghost:2368"}}),
     ("lemmy", {"name": "Lemmy via compose network", "url": "http://lemmy:8536", "platform_key": "lemmy", "surface": "post", "connector_key": "lemmy",
      "credentials_ref": "LEMMY_PASSWORD", "self_promo_allowed": True,
      "options": {"base_url": "http://lemmy:8536", "community": "incubrix_test", "username": "incubrix"}})]
out = []
with TestClient(app) as c:
    for i, (k, d) in enumerate(D):
        did = c.post("/api/v1/destinations", json=d, headers={**H, "Idempotency-Key": f"dc-{k}"}).json()["id"]
        # 1) publish -> status -> delete through the HTTP API (not the certification harness)
        r = c.post("/api/v1/publish", headers={**H, "Idempotency-Key": f"pub-{k}"},
                   json={"destination_id": did, "rendition": {"title": "Compose-network local test post (will be deleted)", "body": "Local test content only."}})
        j = c.get(f"/api/v1/jobs/{r.json()['job_id']}", headers=H).json()
        rec = {"connector": k, "publish_job": j["status"], "error": j["error"]}
        if j["status"] == "succeeded":
            pid = j["result"]["platform_post_id"]
            rec["live_url"], rec["platform_post_id"] = j["result"]["live_url"], pid
            conn = {"base_url": d["options"]["base_url"], "token": os.environ.get("GHOST_ADMIN_KEY") if k == "ghost" else None,
                    "username": "incubrix", "password": os.environ.get("LEMMY_PASSWORD")}
            h = {}
            st = c.post(f"/connector/v1/{k}/get_status", json={"platform_post_id": pid, "connection": conn, "destination": d}, headers=H)
            rec["status_call"] = st.json()
            dl = c.post(f"/connector/v1/{k}/delete", json={"platform_post_id": pid, "connection": conn, "destination": d}, headers=H)
            rec["delete_call"] = dl.json()
            st2 = c.post(f"/connector/v1/{k}/get_status", json={"platform_post_id": pid, "connection": conn, "destination": d}, headers=H)
            rec["status_after_delete"] = st2.json() if st2.status_code == 200 else {"http": st2.status_code, "body": st2.json()}
        # 2) full certification
        cr = c.post(f"/api/v1/destinations/{did}/certify", headers={**H, "Idempotency-Key": f"cert-{k}"})
        cj = c.get(f"/api/v1/jobs/{cr.json()['job_id']}", headers=H).json()
        rec["certification"] = cj["status"]; rec["certification_error"] = cj["error"]
        rec["certification_steps"] = [(s["step"], s["ok"]) for s in (cj["result"] or {}).get("steps", [])]
        out.append(rec)
json.dump(out, open("/out/05_real_publish_via_compose_network.json", "w"), indent=2, default=str)
print(json.dumps(out, indent=2, default=str))
