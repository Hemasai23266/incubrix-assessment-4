"""Run fresh certifications with the existing harness and package the evidence under evidence/certification/.

  python scripts/package_certification.py --mode mock    # all six connectors against the in-process MOCK platforms
  python scripts/package_certification.py --mode local   # Ghost and Lemmy against the REAL local containers
  python scripts/package_certification.py --mode index   # build the master INDEX.md from what the two runs produced

No credentials are written: the harness evidence contains platform ids, URLs, timings and error bodies only.
"""
import argparse
import hashlib
import json
import os
import pathlib
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "evidence" / "certification"
CONNECTORS = ["discourse", "lemmy", "devto", "ghost", "webhook", "assisted"]
SURFACE = {"discourse": "topic", "lemmy": "post", "devto": "article", "ghost": "post", "webhook": "partner_post", "assisted": "manual_post"}
SECRET_KEYS = {"token", "password", "secret", "authorization", "api_key", "api-key"}


def git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    except Exception:
        return "unknown"


def scrub(obj):
    """Defence in depth: drop anything that looks like a credential before writing evidence."""
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if k.lower() in SECRET_KEYS else scrub(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    return obj


def write_json(path: pathlib.Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(scrub(obj), indent=2, default=str, ensure_ascii=False)
    import re
    if re.search(r"[0-9a-f]{24}:[0-9a-f]{64}", text):
        raise SystemExit(f"refusing to write {path}: looks like it contains a Ghost admin key")
    path.write_text(text, encoding="utf-8")


def setup_env(mode: str) -> None:
    os.environ.update({"JOB_BACKEND": "inline", "API_TOKEN": "cert-token", "S3_ENDPOINT_URL": "", "REDIS_URL": "",
                       "RATE_LIMIT_PER_MINUTE": "0", "DATABASE_URL": f"sqlite:///{tempfile.mkdtemp()}/cert.db",
                       "LOCAL_STORE_DIR": tempfile.mkdtemp(), "PUBLIC_BASE_URL": "http://cert.local"})
    if mode == "mock":
        os.environ["SIMULATION_MODE"] = "true"
    else:
        os.environ["SIMULATION_MODE"] = "false"
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith("GHOST_ADMIN_KEY="):
                os.environ["GHOST_ADMIN_KEY"] = line.split("=", 1)[1]
        os.environ["LEMMY_PASSWORD"] = "incubrix-local-only"  # documented local-only throw-away password


def dest_row(key: str, mode: str) -> dict:
    row = {"name": f"{key} certification target", "url": f"https://{key}.example", "platform_key": key, "surface": SURFACE[key],
           "connector_key": key, "self_promo_allowed": True, "options": {}}
    if mode == "local":
        if key == "ghost":
            row.update(name="Local Ghost (docker compose)", url="http://localhost:2368", credentials_ref="GHOST_ADMIN_KEY",
                       options={"base_url": "http://localhost:2368"})
        elif key == "lemmy":
            row.update(name="Local Lemmy (docker compose)", url="http://localhost:8536", credentials_ref="LEMMY_PASSWORD",
                       options={"base_url": "http://localhost:8536", "community": "incubrix_test", "username": "incubrix"})
    elif key == "lemmy":
        row["options"] = {"community": "incubrix_test"}
    if key == "assisted":
        row.update(automation_allowed=False, rules_url="https://example.org/community/rules", disclosure_required=True,
                   options={"submit_url": "https://example.org/community/submit"})
    return row


def platform_version(key: str) -> str:
    import httpx
    try:
        if key == "ghost":
            return "Ghost " + httpx.get("http://localhost:2368/ghost/api/admin/site/", timeout=10).json()["site"]["version"]
        if key == "lemmy":
            return "Lemmy " + str(httpx.get("http://localhost:8536/api/v3/site", timeout=10).json().get("version", "?"))
    except Exception as e:
        return f"unknown ({type(e).__name__})"
    return "n/a"


def steps_table(steps: list) -> str:
    rows = ["| # | step | result | seconds | what it proves |", "|---|---|---|---|---|"]
    what = {"validate": "content passes the connector's format checks", "publish": "a real test post was created (platform id + URL returned)",
            "get_status_live": "the post is confirmed live and its status read back", "delete": "the test post was deleted (Dev.to: unpublished)",
            "verify_deleted": "the platform confirms the post is gone / unpublished / deleted", "build_assist_pack": "the ready-to-post pack was generated and checked"}
    for i, s in enumerate(steps, 1):
        rows.append(f"| {i} | {s['step']} | {'PASS' if s['ok'] else 'FAIL'} | {s.get('seconds', '')} | {what.get(s['step'], '')} |")
    return "\n".join(rows)


def main_run(mode: str, only: str = "") -> None:
    setup_env(mode)
    from fastapi.testclient import TestClient

    from app.connectors.base import load_manifest
    from app.main import app
    H = {"Authorization": "Bearer cert-token"}
    keys = [only] if only else (CONNECTORS if mode == "mock" else ["ghost", "lemmy"])
    with TestClient(app) as c:
        for key in keys:
            if key == "assisted":
                env_label, sub = "Assisted flow — no platform contacted", "assisted-flow"
            elif mode == "mock":
                env_label, sub = "MOCK (in-process mock platform, simulation mode)", "mock"
            else:
                env_label, sub = "LOCAL INSTANCE (real software in docker compose, simulation off)", "local-instance"
            row = dest_row(key, mode)
            started = datetime.now(timezone.utc)
            did = c.post("/api/v1/destinations", json=row, headers={**H, "Idempotency-Key": f"pkg-dest-{key}-{mode}"}).json()["id"]
            r = c.post(f"/api/v1/destinations/{did}/certify", headers={**H, "Idempotency-Key": f"pkg-cert-{key}-{mode}-{int(started.timestamp())}"})
            job = c.get(f"/api/v1/jobs/{r.json()['job_id']}", headers=H).json()
            res = job["result"] or {}
            evid = {}
            if res.get("evidence_url"):
                evid = c.get(res["evidence_url"].replace("http://cert.local", ""), headers=H).json()
            dest_after = c.get(f"/api/v1/destinations/{did}", headers=H).json()
            manifest = load_manifest(key)
            d = OUT / key / sub
            ok = job["status"] == "succeeded"
            write_json(d / "certification_run.json", {"api_job": job, "harness_evidence_file": evid})
            write_json(d / "environment.json", {
                "connector": {"key": key, "version": manifest["version"], "tier": manifest["tier"], "auth": manifest["auth"],
                              "capabilities": manifest["capabilities"], "surface": SURFACE[key]},
                "certification_environment": env_label, "target_platform_version": platform_version(key) if mode == "local" else "mock (no real software)",
                "destination_row": {k: row[k] for k in ("name", "url", "surface", "connector_key", "automation_allowed") if k in row} | {"options": {k: v for k, v in row["options"].items()}},
                "run_started_utc": started.isoformat(), "run_finished_utc": datetime.now(timezone.utc).isoformat(),
                "harness_duration_s": res.get("duration_s"), "code_commit": git_head(),
                "host": {"python": platform.python_version(), "os": platform.platform()},
                "destination_after": {k: dest_after[k] for k in ("certified", "level", "status", "last_certified_at")}})
            lines = [f"# {key}: certification run ({sub})", "", f"* **Result: {'PASS' if ok else 'FAIL'}**  (job status `{job['status']}`)",
                     f"* **Environment:** {env_label}", f"* **Connector:** `{key}` v{manifest['version']}, tier `{manifest['tier']}`",
                     f"* **Started (UTC):** {started.isoformat()}  |  **Harness duration:** {res.get('duration_s')} s",
                     f"* **Destination after run:** certified={dest_after['certified']}, level={dest_after['level']}, status={dest_after['status']}",
                     f"* **Code commit:** `{git_head()}`", "", "## Steps", "", steps_table(res.get("steps", []))]
            if key == "devto":
                lines += ["", "## Dev.to delete behaviour (important)",
                          "Dev.to's public API has **no DELETE endpoint for articles**. The connector's `delete()` sends `PUT /api/articles/{id}` with "
                          "`{\"article\": {\"published\": false}}`, i.e. it **unpublishes** the article; it is not removed from the author's drafts. The "
                          "`verify_deleted` step therefore confirms `status == \"unpublished\"`, not that the article is gone. This was exercised against "
                          "the **mock** Dev.to only; it has not been run against dev.to itself."]
            if key == "assisted":
                lines += ["", "## Assisted connector: NOT an automated publisher",
                          "Nothing is posted anywhere. The certification builds the ready-to-post pack and checks files, caption, deep link and checklist "
                          "(see `assist_pack.json` and `assist_pack_validation.json`). `publish()` is refused on purpose (`publish_refused.json`)."]
            if not ok:
                lines += ["", "## Failure", "```", json.dumps(job["error"], indent=2), "```"]
            (d / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(f"{key:9s} {sub:15s} -> {job['status']}  ({res.get('duration_s')} s)")

        if mode == "mock" and not only:
            from app.mocks.platforms import FAULTS, STORES
            state = {n: {"posts_left": [p for p in s["posts"].values() if not p.get("deleted_at") and p.get("published", True) and not p.get("deleted") and not p.get("removed")]
                         .__len__(), "records_total": len(s["posts"])} for n, s in STORES.items()}
            write_json(OUT / "mock_platform_state_after_all_runs.json",
                       {"note": "posts_left counts records that are still live (not deleted/unpublished). 0 everywhere = every test post was cleaned up.", "state": state})
        if mode == "mock" and (not only or only == "assisted"):
            # ---- assisted: pack generation through the real contract route + validation
            a = dest_row("assisted", mode)
            rend = {"title": "IncuBrix launches multi-destination video publishing", "body": "Short, honest post text for the community.",
                    "link": "https://incubrix.example/launch", "ai_generated": True, "disclosure": "Drafted with AI assistance, reviewed by a human.",
                    "media": [{"url": "https://incubrix.example/media/demo.png", "content_type": "image/png", "filename": "demo.png"},
                              {"url": "https://incubrix.example/media/demo.mp4", "content_type": "video/mp4", "filename": "demo.mp4"}]}
            pack = c.post("/connector/v1/assisted/build_assist_pack", json={"rendition": rend, "destination": a}, headers={**H, "Idempotency-Key": "pkg-assist-pack"}).json()
            checks = [
                ("files[] present and includes the post text file", bool(pack.get("files")) and any(f["name"] == "post.txt" and f.get("content") for f in pack["files"])),
                ("one file entry per media item (2 media -> 3 files total)", len(pack.get("files", [])) == 3),
                ("media entries keep filename, content type and source URL", all(f.get("source_url") and f.get("content_type") for f in pack.get("files", [])[1:])),
                ("caption equals the post title", pack.get("caption") == rend["title"]),
                ("deep_link is the destination's submit URL", pack.get("deep_link") == a["options"]["submit_url"]),
                ("checklist has the rules link, promo, link-policy, AI-disclosure and cap items", len(pack.get("checklist", [])) >= 6 and any("rules" in x.lower() for x in pack["checklist"])
                 and any("disclosure" in x.lower() for x in pack["checklist"]) and any("promotion" in x.lower() for x in pack["checklist"])),
                ("AI disclosure is flagged as REQUIRED in the checklist for this destination", any("YES" in x and "disclosure" in x.lower() for x in pack.get("checklist", []))),
                ("manual posting instructions present", bool(pack.get("instructions"))),
            ]
            refusal = c.post("/connector/v1/assisted/publish", json={"rendition": rend, "destination": a}, headers={**H, "Idempotency-Key": "pkg-assisted-publish"})
            checks.append(("automated publish() is refused (assisted is not an automated connector)", refusal.status_code == 422 and refusal.json()["error"]["code"] == "DEST_RULE_VIOLATION"))
            d = OUT / "assisted" / "assisted-flow"
            write_json(d / "assist_pack.json", pack)
            write_json(d / "assist_pack_validation.json", {"all_passed": all(v for _, v in checks), "checks": [{"check": n, "passed": v} for n, v in checks]})
            write_json(d / "publish_refused.json", {"http_status": refusal.status_code, "body": refusal.json()})
            print("assisted pack validation:", "PASS" if all(v for _, v in checks) else "FAIL", [n for n, v in checks if not v])
        if mode == "local":
            # ---- leftover check on the real instances
            import httpx

            from app.connectors.platforms import ghost_jwt
            g = httpx.get("http://localhost:2368/ghost/api/admin/posts/?limit=all&fields=title,status",
                          headers={"Authorization": "Ghost " + ghost_jwt(os.environ["GHOST_ADMIN_KEY"]), "Accept-Version": "v5.0"}, timeout=20).json()["posts"]
            lc = httpx.Client(base_url="http://localhost:8536", timeout=20)
            jwt = lc.post("/api/v3/user/login", json={"username_or_email": "incubrix", "password": "incubrix-local-only"}).json()["jwt"]
            lp = lc.get("/api/v3/post/list", params={"community_name": "incubrix_test", "limit": 50}, headers={"Authorization": f"Bearer {jwt}"}).json()["posts"]
            write_json(OUT / "local_instances_leftover_check.json", {
                "checked_utc": datetime.now(timezone.utc).isoformat(),
                "ghost_posts_remaining": [p["title"] for p in g],
                "lemmy_live_posts_remaining": [p["post"]["name"] for p in lp if not p["post"]["deleted"] and not p["post"]["removed"]],
                "note": "Only Ghost's default 'Coming soon' page may remain; no certification test post may remain."})
            print("leftover check written")


def main_index() -> None:
    def load(p):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    L = ["# Must-have #2: certification evidence index", "",
         f"Generated {datetime.now(timezone.utc).isoformat()} from the files below by `scripts/package_certification.py --mode index` (code commit `{git_head()}`). "
         "Every status in this file is read from the stored run records, not typed by hand.", "",
         "> **Commit hashes:** run records made before 2026-11-23 cite code commit `4c8b43d`. That commit was rewritten to remove an exposed local "
         "session cookie and is now `c54ab4f`; the code is identical, only `evidence/docker/04_ghost_seed_repeat_diagnosis.txt` differs "
         "(see `evidence/docker/26_ghost_session_revocation.txt`).", "",
         "## What was tested and how", "",
         "* **Harness:** `POST /api/v1/destinations/{id}/certify` (job). Steps: validate -> publish a test post -> `get_status` confirms it is live -> "
         "`delete` -> `get_status` confirms it is gone. Evidence JSON is stored by the harness; a failed run demotes the destination.",
         "* **MOCK certification:** the connector's real HTTP code runs against an in-process mock of the platform (`app/mocks/platforms.py`). It proves the "
         "connector, error mapping and harness, **not** that the real platform behaves identically.",
         "* **LOCAL-INSTANCE certification:** same harness against real Ghost 5 and Lemmy 0.19 containers started by `docker compose` on this machine.",
         "* **Assisted:** no platform call; the evidence is the generated pack and 9 automated checks.",
         "* **Related earlier evidence:** `evidence/docker/14_*` (publish -> status -> delete through the running compose API and RQ worker on local Ghost and Lemmy) and `evidence/docker/22_*` (same on a fresh `inn-clean` stack).",
         "* All test posts go only to the mock servers or the local containers and are deleted within seconds (limit 600 s).", "",
         "## Status of the six Core connectors", "",
         "| Connector | Mock certification (assisted: assisted flow) | Local-instance certification | Screenshots | Files |", "|---|---|---|---|---|"]
    gaps = []
    for key in CONNECTORS:
        cells = []
        for sub in (("assisted-flow", "local-instance") if key == "assisted" else ("mock", "local-instance")):
            run = load(OUT / key / sub / "certification_run.json")
            if run is None:
                cells.append(("n/a (no platform is called)" if key == "assisted" else "not run") if sub == "local-instance" else "MISSING")
            else:
                j = run["api_job"]
                ok = j["status"] == "succeeded"
                cells.append(f"{'PASS' if ok else 'FAIL'} ({(j['result'] or {}).get('duration_s')} s)" + (" — assisted flow, no platform contacted" if key == "assisted" else ""))
        shots = list((OUT / key / "local-instance" / "screenshots").glob("*.png")) if (OUT / key / "local-instance" / "screenshots").exists() else []
        L.append(f"| {key} | {cells[0]} | {cells[1]} | {len(shots) if shots else '-'} | `evidence/certification/{key}/` |")
        if key in ("discourse", "devto", "webhook") and cells[1] == "not run":
            gaps.append(f"**{key}**: certified against the MOCK only (no real {key} software/account was used; the brief supplies local Lemmy and Ghost and mocks for the rest).")
    assist = load(OUT / "assisted" / "assisted-flow" / "assist_pack_validation.json")
    L += ["", "Assisted pack validation: " + ("PASS (%d/%d checks)" % (sum(c["passed"] for c in assist["checks"]), len(assist["checks"])) if assist else "MISSING"), ""]
    left = load(OUT / "local_instances_leftover_check.json")
    mock_left = load(OUT / "mock_platform_state_after_all_runs.json")
    L += ["## Cleanup verification", "",
          f"* Local instances after the run: Ghost posts {left['ghost_posts_remaining'] if left else 'not checked'}; Lemmy live posts {left['lemmy_live_posts_remaining'] if left else 'not checked'}.",
          f"* Mock platforms: live records per instance {({k: v['posts_left'] for k, v in mock_left['state'].items()} if mock_left else 'not checked')}.", ""]
    shots_manifest = load(OUT / "screenshots_manifest.json")
    L += ["## Screenshots", ""]
    if shots_manifest:
        L += [f"Captured automatically with headless Microsoft Edge by `scripts/capture_screenshots.py` ({shots_manifest['captured_utc']}), see `screenshots_manifest.json` (sha256 per file).", ""]
        for s in shots_manifest["shots"]:
            L.append(f"* `{s['file']}`: {s['what']}")
    else:
        L.append("None captured yet.")
    L += ["", "## What remains unverified (honest list)", ""]
    L += [f"* {g}" for g in gaps]
    L += ["* Dev.to has no delete endpoint: the evidence proves **unpublish** against the mock, not against dev.to.",
          "* Discourse, Dev.to and webhook were never run against real software; passing the mock proves the connector logic, not platform compatibility.",
          "* Lemmy has no web UI in our compose (no `lemmy-ui`): its screenshots show the public API JSON, not a rendered Lemmy page.",
          "* Runs were made on one Windows machine with cached images; no clean second machine.",
          "* Mock evidence shows `live_url` values such as `http://mock/...` or `https://dev.example/...`; these are mock URLs that do not exist on the internet.", ""]
    (OUT / "INDEX.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("INDEX.md written")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["mock", "local", "index"], required=True)
    ap.add_argument("--only", default="", help="re-run a single connector (e.g. assisted)")
    a = ap.parse_args()
    main_index() if a.mode == "index" else main_run(a.mode, a.only)
