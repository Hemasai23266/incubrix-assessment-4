"""Seed the LOCAL Ghost and Lemmy containers (throw-away credentials, localhost/compose network only).

  docker compose --profile seed run --rm seed

Safe to run repeatedly:
  Ghost : owner is created once; an existing Admin key stored in CREDENTIALS_DIR is validated and reused,
          otherwise a new Custom Integration is created. The key is written to CREDENTIALS_DIR/GHOST_ADMIN_KEY
          (mode 0600) and is NEVER printed.
  Lemmy : community 'incubrix_test' is created only if missing; its local password goes to CREDENTIALS_DIR/LEMMY_PASSWORD.
  IncuBrix: when the API runs in LIVE mode (docker-compose.real.yml) the two destinations are registered with fixed ids
          and fixed idempotency keys, so reruns never create duplicates. In simulation mode registration is skipped.
"""
import argparse
import os
import pathlib
import sys
import time

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

GHOST_EMAIL, GHOST_PASSWORD = "owner@incubrix.local", "incubrix-local-only-1!"
LEMMY_USER, LEMMY_PASSWORD, LEMMY_COMMUNITY = "incubrix", "incubrix-local-only", "incubrix_test"


def creds_dir() -> pathlib.Path | None:
    d = os.environ.get("CREDENTIALS_DIR", "")
    return pathlib.Path(d) if d else None


def write_secret(name: str, value: str) -> None:
    d = creds_dir()
    if d is None:
        return
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text(value, encoding="utf-8")
    os.chmod(f, 0o600)


def read_secret(name: str) -> str:
    d = creds_dir()
    f = d / name if d else None
    return f.read_text(encoding="utf-8").strip() if f and f.is_file() else ""


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
    """Create the owner (once) and a NEW Admin API key. Returns 'id:secret'."""
    wait(f"{base}/ghost/api/admin/site/")
    h = {"Origin": base, "Accept-Version": "v5.0"}
    with httpx.Client(base_url=base, headers=h, timeout=30) as c:
        c.post("/ghost/api/admin/authentication/setup/",  # 403 when already set up: expected and ignored
               json={"setup": [{"name": "IncuBrix Owner", "email": GHOST_EMAIL, "password": GHOST_PASSWORD, "blogTitle": "IncuBrix local Ghost"}]})
        r = c.post("/ghost/api/admin/session/", data={"username": GHOST_EMAIL, "password": GHOST_PASSWORD})
        r.raise_for_status()
        r = c.post("/ghost/api/admin/integrations/?include=api_keys", json={"integrations": [{"name": f"incubrix-{int(time.time())}"}]})
        r.raise_for_status()
        key = next(k for k in r.json()["integrations"][0]["api_keys"] if k["type"] == "admin")
        # newer Ghost returns the secret already prefixed with the key id ("<id>:<hex>")
        return key["secret"] if ":" in key["secret"] else f"{key['id']}:{key['secret']}"


def ghost_key_valid(base: str, key: str) -> bool:
    from app.connectors.platforms import ghost_jwt
    try:
        r = httpx.get(f"{base}/ghost/api/admin/posts/?limit=1", headers={"Authorization": f"Ghost {ghost_jwt(key)}", "Accept-Version": "v5.0"}, timeout=15)
        return r.status_code == 200
    except Exception:
        return False


def ensure_ghost(base: str) -> str:
    wait(f"{base}/ghost/api/admin/site/")
    existing = read_secret("GHOST_ADMIN_KEY")
    if existing and ghost_key_valid(base, existing):
        return "Ghost: existing admin key is still valid; reused (nothing created)"
    write_secret("GHOST_ADMIN_KEY", seed_ghost(base))
    return "Ghost: owner ready, admin key created and stored in the credentials volume"


def ensure_lemmy(base: str) -> str:
    wait(f"{base}/api/v3/site")
    with httpx.Client(base_url=base, timeout=30) as c:
        jwt = c.post("/api/v3/user/login", json={"username_or_email": LEMMY_USER, "password": LEMMY_PASSWORD}).json()["jwt"]
        h = {"Authorization": f"Bearer {jwt}"}
        exists = c.get("/api/v3/community", params={"name": LEMMY_COMMUNITY}, headers=h).status_code == 200
        if not exists:
            c.post("/api/v3/community", headers=h, json={"name": LEMMY_COMMUNITY, "title": "IncuBrix test community"}).raise_for_status()
    write_secret("LEMMY_PASSWORD", LEMMY_PASSWORD)
    return f"Lemmy: community '{LEMMY_COMMUNITY}' {'already existed' if exists else 'created'}"


def register_destinations(api: str, token: str, ghost: str, lemmy: str) -> list[str]:
    msgs = []
    with httpx.Client(base_url=api, headers={"Authorization": f"Bearer {token}"}, timeout=30) as c:
        mode = c.get("/ready").json().get("mode")
        if mode != "live":
            return [f"IncuBrix API is in '{mode}' mode: local destinations NOT registered (use docker-compose.real.yml)"]
        rows = [
            ("local-ghost", {"id": "local-ghost", "name": "Local Ghost (compose)", "url": ghost, "platform_key": "ghost", "surface": "post",
                             "connector_key": "ghost", "credentials_ref": "GHOST_ADMIN_KEY", "self_promo_allowed": True,
                             "options": {"base_url": ghost}}),
            ("local-lemmy", {"id": "local-lemmy", "name": "Local Lemmy (compose)", "url": lemmy, "platform_key": "lemmy", "surface": "post",
                             "connector_key": "lemmy", "credentials_ref": "LEMMY_PASSWORD", "self_promo_allowed": True,
                             "options": {"base_url": lemmy, "community": LEMMY_COMMUNITY, "username": LEMMY_USER}}),
        ]
        for did, body in rows:
            r = c.post("/api/v1/destinations", json=body, headers={"Idempotency-Key": f"seed-{did}"})
            msgs.append(f"destination {did}: HTTP {r.status_code} ({'created' if r.status_code == 201 else 'already registered' if r.status_code == 200 else r.text[:80]})")
        n = len([d for d in c.get("/api/v1/destinations").json()["destinations"] if d["id"] in ("local-ghost", "local-lemmy")])
        msgs.append(f"catalogue rows for local destinations: {n} (expected 2)")
    return msgs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ghost", default=os.environ.get("GHOST_URL", "http://ghost:2368"))
    ap.add_argument("--lemmy", default=os.environ.get("LEMMY_URL", "http://lemmy:8536"))
    ap.add_argument("--api", default=os.environ.get("API_URL", ""))
    a = ap.parse_args()
    print(ensure_ghost(a.ghost))
    print(ensure_lemmy(a.lemmy))
    if a.api:
        for m in register_destinations(a.api, os.environ.get("API_TOKEN", "change-me-admin-token"), a.ghost, a.lemmy):
            print(m)
    print("done (no secret values are printed)")


if __name__ == "__main__":
    main()
