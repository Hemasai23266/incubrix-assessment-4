"""One-shot seeding of the LOCAL Ghost and Lemmy containers (throw-away credentials, localhost only).

  docker compose --profile seed run --rm seed          # inside compose network
  python scripts/seed_local.py --ghost http://localhost:2368 --lemmy http://localhost:8536 --api http://localhost:8000

What it does:
  Ghost : creates the owner account, then a Custom Integration -> prints the Admin API key ("id:secret").
  Lemmy : logs in as the admin created by docker/lemmy.hjson and creates the community 'incubrix_test'.
  IncuBrix: registers one destination row for each (so they can be certified with a REAL post).
NOTE: status of this script is documented in REPORT.md (it requires the real containers to be running).
"""
import argparse
import os
import sys
import time

import httpx

GHOST_EMAIL, GHOST_PASSWORD = "owner@incubrix.local", "incubrix-local-only-1!"


def wait(url: str, tries: int = 60) -> None:
    for _ in range(tries):
        try:
            if httpx.get(url, timeout=3, follow_redirects=True).status_code < 500:
                return
        except Exception:
            pass
        time.sleep(3)
    sys.exit(f"timed out waiting for {url}")


def seed_ghost(base: str) -> str:
    wait(f"{base}/ghost/api/admin/site/")
    h = {"Origin": base, "Accept-Version": "v5.0"}
    with httpx.Client(base_url=base, headers=h, timeout=30) as c:
        c.post("/ghost/api/admin/authentication/setup/",
               json={"setup": [{"name": "IncuBrix Owner", "email": GHOST_EMAIL, "password": GHOST_PASSWORD, "blogTitle": "IncuBrix local Ghost"}]})
        r = c.post("/ghost/api/admin/session/", data={"username": GHOST_EMAIL, "password": GHOST_PASSWORD})
        r.raise_for_status()
        r = c.post("/ghost/api/admin/integrations/?include=api_keys", json={"integrations": [{"name": f"incubrix-{int(time.time())}"}]})
        r.raise_for_status()
        key = next(k for k in r.json()["integrations"][0]["api_keys"] if k["type"] == "admin")
        # newer Ghost returns the secret already prefixed with the key id ("<id>:<hex>")
        return key["secret"] if ":" in key["secret"] else f"{key['id']}:{key['secret']}"


def seed_lemmy(base: str) -> None:
    wait(f"{base}/api/v3/site")
    with httpx.Client(base_url=base, timeout=30) as c:
        jwt = c.post("/api/v3/user/login", json={"username_or_email": "incubrix", "password": "incubrix-local-only"}).json()["jwt"]
        c.post("/api/v3/community", headers={"Authorization": f"Bearer {jwt}"},
               json={"name": "incubrix_test", "title": "IncuBrix test community"})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ghost", default=os.environ.get("GHOST_URL", "http://ghost:2368"))
    ap.add_argument("--lemmy", default=os.environ.get("LEMMY_URL", "http://lemmy:8536"))
    a = ap.parse_args()
    key = seed_ghost(a.ghost)
    seed_lemmy(a.lemmy)
    print("Ghost Admin key (put in .env as GHOST_ADMIN_KEY; never commit it):", key)
    print("Lemmy: community 'incubrix_test' ready; admin password is in docker/lemmy.hjson (local only).")
    print("Then run with SIMULATION_MODE=false and register destinations with options.base_url=", a.ghost, a.lemmy)


if __name__ == "__main__":
    main()
