"""Print a data fingerprint of the ORIGINAL compose project (counts only; no secrets)."""
import json
import subprocess
import sys
import pathlib

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.connectors.platforms import ghost_jwt  # noqa: E402


def psql(sql: str) -> str:
    return subprocess.run(["docker", "compose", "exec", "-T", "db", "psql", "-U", "incubrix", "-d", "incubrix", "-tA", "-c", sql],
                          capture_output=True, text=True, cwd=ROOT).stdout.strip()


out = {t: int(psql(f"select count(*) from {t}") or -1) for t in ["destinations", "jobs", "posts", "certifications", "idempotency_records", "rule_events"]}
key = next(l.split("=", 1)[1] for l in (ROOT / ".env").read_text().splitlines() if l.startswith("GHOST_ADMIN_KEY="))
g = httpx.get("http://localhost:2368/ghost/api/admin/posts/?limit=all&fields=title", headers={"Authorization": "Ghost " + ghost_jwt(key), "Accept-Version": "v5.0"}, timeout=20).json()
out["ghost_posts"] = sorted(p["title"] for p in g["posts"])
c = httpx.Client(base_url="http://localhost:8536", timeout=20)
jwt = c.post("/api/v3/user/login", json={"username_or_email": "incubrix", "password": "incubrix-local-only"}).json()["jwt"]
lp = c.get("/api/v3/post/list", params={"community_name": "incubrix_test", "limit": 50, "show_deleted": "true"} if False else {"community_name": "incubrix_test", "limit": 50}, headers={"Authorization": f"Bearer {jwt}"}).json()["posts"]
out["lemmy_live_posts"] = len([p for p in lp if not p["post"]["deleted"]])
out["lemmy_site"] = c.get("/api/v3/site").json()["site_view"]["site"]["name"]
out["ghost_owner_exists"] = httpx.get("http://localhost:2368/ghost/api/admin/authentication/setup/", headers={"Accept-Version": "v5.0"}, timeout=20).json()["setup"][0]["status"]
print(json.dumps(out, indent=2))
