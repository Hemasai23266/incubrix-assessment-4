"""Screenshots of REAL local Ghost and Lemmy test posts: live, then after deletion (headless Microsoft Edge).

  python scripts/capture_screenshots.py

For each platform: publish one clearly-labelled test post with the project's connector, screenshot its public URL while it is
live, delete it, screenshot the same URL again, and confirm deletion through the connector. The post is deleted in a `finally`
block even if a screenshot fails. Pages are public, so no token or cookie appears in any image.
Lemmy has no web UI in our compose file (no lemmy-ui), so the Lemmy screenshots show the public API response for the post.
"""
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
OUT = ROOT / "evidence" / "certification"

os.environ.update({"SIMULATION_MODE": "false", "S3_ENDPOINT_URL": "", "DATABASE_URL": "sqlite:///:memory:"})
for line in (ROOT / ".env").read_text().splitlines():
    if line.startswith("GHOST_ADMIN_KEY="):
        ghost_key = line.split("=", 1)[1]

from app.connectors.registry import get_connector  # noqa: E402
from app.errors import AppError  # noqa: E402

REND = {"title": "IncuBrix certification screenshot test post", "body": "Local test content. This post is deleted seconds after this screenshot.",
        "tags": ["test"], "media": []}
TARGETS = {
    "ghost": {"conn": {"base_url": "http://localhost:2368", "token": ghost_key}, "dest": {"surface": "post", "options": {}},
              "page": lambda post_id, live: live, "what_live": "Ghost post as published on the local Ghost site (public page)",
              "what_gone": "the same Ghost URL after deletion (Ghost's not-found page)"},
    "lemmy": {"conn": {"base_url": "http://localhost:8536", "username": "incubrix", "password": "incubrix-local-only"},
              "dest": {"surface": "post", "options": {"community": "incubrix_test"}},
              "page": lambda post_id, live: f"http://localhost:8536/api/v3/post?id={post_id}",
              "what_live": "Lemmy public API response for the post while live (no lemmy-ui in our compose; this is the API JSON)",
              "what_gone": "the same Lemmy API URL after deletion (anonymous request: post no longer retrievable)"},
}


def shoot(url: str, path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    prof = tempfile.mkdtemp(prefix="edge-prof-")
    subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={prof}", "--window-size=1280,760",
                    "--virtual-time-budget=6000", f"--screenshot={path}", url], capture_output=True, timeout=90)
    if not path.exists() or path.stat().st_size < 2000:
        raise RuntimeError(f"screenshot failed for {url}")


shots = []
for key, t in TARGETS.items():
    c = get_connector(key)
    conn, dest = t["conn"], t["dest"]
    d = OUT / key / "local-instance" / "screenshots"
    post_id = None
    try:
        pub = c.publish(REND, dest, conn, f"shot-{key}-{int(time.time())}")
        post_id, live = pub["platform_post_id"], pub["live_url"]
        status = c.get_status(post_id, conn)
        url = t["page"](post_id, live)
        f1 = d / f"{key}_01_live_{datetime.now(timezone.utc):%H%M%S}.png"
        shoot(url, f1)
        shots.append({"connector": key, "file": str(f1.relative_to(OUT)), "what": t["what_live"], "url_shown": url, "platform_status": status["status"]})
    finally:
        if post_id:
            c.delete(post_id, conn)
    try:
        gone = c.get_status(post_id, conn)["status"]
    except AppError as e:
        gone = f"gone (HTTP {e.details.get('platform_status')})"
    f2 = d / f"{key}_02_after_delete_{datetime.now(timezone.utc):%H%M%S}.png"
    shoot(url, f2)
    shots.append({"connector": key, "file": str(f2.relative_to(OUT)), "what": t["what_gone"], "url_shown": url, "connector_status_after_delete": gone})
    print(key, "live+deleted screenshots captured; status after delete:", gone)

for s in shots:
    p = OUT / s["file"]
    s["bytes"], s["sha256"] = p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest()
    s["captured_utc"] = datetime.now(timezone.utc).isoformat()
(OUT / "screenshots_manifest.json").write_text(json.dumps({"captured_utc": datetime.now(timezone.utc).isoformat(), "tool": "headless Microsoft Edge", "shots": shots}, indent=2), encoding="utf-8")
print("manifest written:", len(shots), "screenshots")
