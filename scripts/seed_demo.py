"""Seed the demo catalogue (simulation mode) and certify the six Core connectors against the MOCK platforms.

Runs once at container start (supervisord `seed` program) and is safe to run again: every row has a fixed id and a fixed
Idempotency-Key, so repeats never duplicate anything. Prints no secrets. Everything it creates is simulation-only.
  python scripts/seed_demo.py [--api http://127.0.0.1:10000] [--no-certify]
"""
import argparse
import os
import sys
import time

import httpx

SURFACE = {"discourse": "topic", "lemmy": "post", "devto": "article", "ghost": "post", "webhook": "partner_post", "assisted": "manual_post"}


def row(did: str, name: str, connector: str, **kw) -> dict:
    base = {"id": did, "name": name, "url": f"https://{did}.demo.invalid", "platform_key": connector, "surface": SURFACE[connector],
            "connector_key": connector, "self_promo_allowed": True, "options": {}}
    base.update(kw)
    return base


CATALOGUE = [
    # the six Core connectors (certified at start-up against the MOCK platforms)
    row("demo-discourse", "Demo Discourse forum (mock)", "discourse", category="forum", audience_tags=["ai", "saas"], language="en",
        link_policy="allowed"),
    row("demo-discourse-b", "Demo Discourse forum B (mock): same connector, one more row", "discourse", options={"mock_instance": "discourse-b"}),
    row("demo-lemmy", "Demo Lemmy community (mock)", "lemmy", options={"community": "incubrix_test"}, audience_tags=["creators"]),
    row("demo-devto", "Demo Dev.to (mock)", "devto", audience_tags=["ai", "founders"]),
    row("demo-ghost", "Demo Ghost blog (mock)", "ghost", audience_tags=["marketing"]),
    row("demo-webhook", "Demo partner site, HMAC webhook (mock)", "webhook"),
    row("demo-assisted", "Demo community that bans bots: assisted pack", "assisted", automation_allowed=False, disclosure_required=True,
        rules_url="https://example.org/community/rules", options={"submit_url": "https://example.org/community/submit"}),
    # rule demonstrations for reviewers (see docs/RENDER_DEPLOY.md)
    row("demo-weekly-cap", "Demo forum: 1 post per week", "discourse", frequency_cap={"max_posts": 1, "window_hours": 168}),
    row("demo-no-promo", "Demo community: no self-promotion", "lemmy", self_promo_allowed=False, options={"community": "incubrix_test"}),
    row("demo-ai-disclosure", "Demo forum: AI disclosure required", "discourse", disclosure_required=True),
    row("demo-no-links", "Demo community: no links allowed", "lemmy", link_policy="forbidden", options={"community": "incubrix_test"}),
    row("demo-non-english", "Demo non-English forum (Hindi)", "discourse", language="hi", geo="IN"),
]
CERTIFY = ["demo-discourse", "demo-discourse-b", "demo-lemmy", "demo-devto", "demo-ghost", "demo-webhook", "demo-assisted"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=f"http://127.0.0.1:{os.environ.get('PORT', '10000')}")
    ap.add_argument("--no-certify", action="store_true")
    a = ap.parse_args()
    token = os.environ["API_TOKEN"]
    with httpx.Client(base_url=a.api, headers={"Authorization": f"Bearer {token}"}, timeout=30) as c:
        for _ in range(120):
            try:
                if c.get("/ready").status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            print("seed: API never became ready", file=sys.stderr)
            return 1
        mode = c.get("/ready").json().get("mode")
        if mode != "simulation":
            print(f"seed: refusing to seed because API mode is '{mode}', not simulation", file=sys.stderr)
            return 1
        created = existing = 0
        for r in CATALOGUE:
            resp = c.post("/api/v1/destinations", json=r, headers={"Idempotency-Key": f"demo-seed-{r['id']}"})
            created += resp.status_code == 201
            existing += resp.status_code == 200
            if resp.status_code not in (200, 201):
                print(f"seed: {r['id']} -> HTTP {resp.status_code} {resp.text[:120]}", file=sys.stderr)
                return 1
        print(f"seed: catalogue ready ({created} created, {existing} already present)")
        if not a.no_certify:
            jobs = {}
            for did in CERTIFY:
                resp = c.post(f"/api/v1/destinations/{did}/certify", headers={"Idempotency-Key": f"demo-seed-certify-{did}"})
                jobs[did] = resp.json().get("job_id")
            ok = 0
            for did, jid in jobs.items():
                for _ in range(90):
                    j = c.get(f"/api/v1/jobs/{jid}").json()
                    if j["status"] in ("succeeded", "failed"):
                        break
                    time.sleep(1)
                ok += j["status"] == "succeeded"
                print(f"seed: certify {did}: {j['status']}")
            print(f"seed: {ok}/{len(jobs)} certifications passed (mock platforms, labelled in the evidence)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
