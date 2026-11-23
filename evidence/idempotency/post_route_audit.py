# Evidence script (re-run from the repo root with the project venv). Output of the last run is in the matching .txt file.
import os, sys, tempfile, re, json, pathlib
os.environ.update({"SIMULATION_MODE": "true", "JOB_BACKEND": "inline", "API_TOKEN": "audit-token", "DATABASE_URL": "sqlite:///:memory:", "S3_ENDPOINT_URL": "", "LOCAL_STORE_DIR": tempfile.mkdtemp(), "RATE_LIMIT_PER_MINUTE": "0"})
sys.path.insert(0, ".")
from fastapi.testclient import TestClient
from app.main import app
import httpx
from app import netguard, fetcher
netguard._resolve = lambda h, p: ["93.184.216.34"]
png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
fetcher.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "image/png"}, stream=httpx.ByteStream(png)))
H = {"Authorization": "Bearer audit-token"}
posts = sorted((p, m) for p, ops in app.openapi()["paths"].items() for m in ops if m == "post")
print(f"{len(posts)} POST routes in the OpenAPI document:")
with TestClient(app) as c:
    d = c.post("/api/v1/destinations", json={"name": "a", "url": "https://a.example", "platform_key": "discourse", "surface": "topic", "connector_key": "discourse"}, headers={**H, "Idempotency-Key": "d0"}).json()
    rend = {"title": "A sufficiently long test title", "body": "b"}
    bodies = {
        "/api/v1/connectors/{key}/disable": ("/api/v1/connectors/discourse/disable", {}), "/api/v1/connectors/{key}/enable": ("/api/v1/connectors/discourse/enable", {}),
        "/api/v1/destinations": ("/api/v1/destinations", {"name": "b", "url": "https://b.example", "platform_key": "discourse", "surface": "topic", "connector_key": "discourse"}),
        "/api/v1/destinations/{dest_id}/revoke": (f"/api/v1/destinations/{d['id']}/revoke", {}),
        "/api/v1/destinations/{dest_id}/check": (f"/api/v1/destinations/{d['id']}/check", rend),
        "/api/v1/destinations/{dest_id}/certify": (f"/api/v1/destinations/{d['id']}/certify", {}),
        "/api/v1/publish": ("/api/v1/publish", {"destination_id": d["id"], "rendition": rend}),
        "/api/v1/maintenance/cleanup-test-posts": ("/api/v1/maintenance/cleanup-test-posts", {}),
        "/api/v1/simulation/faults/{instance}": ("/api/v1/simulation/faults/discourse", {"rename": False}),
        "/api/v1/files/from-url": ("/api/v1/files/from-url", {"url": "https://cdn.example.com/a.png"}),
        "/connector/v1/{connector_key}/{method}": ("/connector/v1/discourse/validate", {"rendition": rend, "destination": {"surface": "topic"}}),
    }
    rows = []
    for path, _ in posts:
        if path == "/api/v1/files":
            url = path
            r0 = c.post(url, files={"file": ("a.txt", b"x", "text/plain")}, headers=H)
            r1 = c.post(url, files={"file": ("a.txt", b"x", "text/plain")}, headers={**H, "Idempotency-Key": "u1"})
            r2 = c.post(url, files={"file": ("a.txt", b"x", "text/plain")}, headers={**H, "Idempotency-Key": "u1"})
        else:
            url, body = bodies[path]
            if path == "/api/v1/destinations": body = {**body, "name": "b-nokey"}
            r0 = c.post(url, json=body, headers=H)
            if path == "/api/v1/destinations": body = {**body, "name": "b"}
            r1 = c.post(url, json=body, headers={**H, "Idempotency-Key": "k1-" + path[-8:]})
            r2 = c.post(url, json=body, headers={**H, "Idempotency-Key": "k1-" + path[-8:]})
        required = r0.status_code == 400 and r0.json().get("error", {}).get("code") == "IDEMPOTENCY_KEY_REQUIRED"
        same = (r1.json() == r2.json()) if r1.headers["content-type"].startswith("application/json") else None
        replay = r2.headers.get("idempotent-replay") == "true" or (isinstance(r2.json(), dict) and r2.json().get("idempotent_replay") is True)
        rows.append((path, "REQUIRED" if required else "optional", f"no-key HTTP {r0.status_code}", f"key HTTP {r1.status_code}->{r2.status_code}", "replay" if replay else "NO REPLAY", "same body" if same else "body differs"))
    for r in rows: print("  {:<44s} {:<9s} {:<17s} {:<17s} {:<10s} {}".format(*r))
    req = [r[0] for r in rows if r[1] == "REQUIRED"]; opt = [r[0] for r in rows if r[1] != "REQUIRED"]
    print("\nREQUIRED on:", len(req), "| optional (accepted, replayed) on:", len(opt)); print("optional ones:", opt)
