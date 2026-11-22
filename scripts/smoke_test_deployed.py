"""End-to-end smoke test against a DEPLOYED demo URL (simulation mode only; no real platform is ever contacted).

  python scripts/smoke_test_deployed.py https://<your-service>.onrender.com              # asks for the token (hidden input)
  set INCUBRIX_API_TOKEN=...   (PowerShell: $env:INCUBRIX_API_TOKEN="...")               # or pass it via the environment
  python scripts/smoke_test_deployed.py https://<your-service>.onrender.com <API_TOKEN>  # or as the second argument

Use the service's API_TOKEN (Render dashboard > Environment), NOT CALLBACK_SIGNING_SECRET. The token is never printed.
Every request carries a unique Idempotency-Key, so re-running is safe. The first request may take about a minute on a sleeping
free host (cold start); the script reports how long it took and waits up to 2 minutes for the demo catalogue to be seeded.
Exit codes: 0 all passed, 1 some check failed, 2 the token was rejected (401) or the arguments were wrong.
"""
from __future__ import annotations

import getpass
import os
import sys
import time
import uuid
from typing import Callable

import httpx

REJECTED_HELP = ("The API rejected the token (HTTP 401). Use the service's API_TOKEN (Render dashboard > Environment, 'reveal'), "
                 "not CALLBACK_SIGNING_SECRET; copy it without spaces, quotes or angle brackets; and if the service was recreated "
                 "use the new value.")


def clean_token(raw: str) -> str:
    return raw.strip().strip("\"'<>").strip()


def run(base: str, token: str, *, client_factory: Callable[[dict | None], httpx.Client] | None = None,
        sleep: Callable[[float], None] = time.sleep, catalogue_wait: float = 120.0, startup_wait: float = 180.0,
        out: Callable[[str], None] = print) -> int:
    base = base.rstrip("/")
    cleaned = clean_token(token)
    if cleaned != token:
        out("note: the token had surrounding spaces/quotes/brackets; they were removed")
    token = cleaned
    if len(token) < 16:
        out(f"warning: the token is only {len(token)} characters long; the real API_TOKEN is much longer (wrong value?)")

    def make(headers):
        if client_factory:
            return client_factory(headers)
        return httpx.Client(base_url=base, headers=headers, timeout=120)

    anon, c = make(None), make({"Authorization": f"Bearer {token}"})
    run_id = uuid.uuid4().hex[:8]
    fails = 0

    def check(name: str, fn: Callable[[], tuple[bool, str]]) -> bool:
        nonlocal fails
        try:
            ok, detail = fn()
        except Exception as e:  # a failing check must produce a FAIL line, never a traceback
            ok, detail = False, f"{type(e).__name__}: {str(e)[:100]}"
        fails += not ok
        out(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
        return ok

    def body(r: httpx.Response) -> dict:
        try:
            j = r.json()
            return j if isinstance(j, dict) else {}
        except Exception:
            return {}

    def err_code(r: httpx.Response) -> str:
        return str((body(r).get("error") or {}).get("code", "?"))

    def wait(jid: str, t: float = 120.0) -> dict:
        t0 = time.time()
        j: dict = {}
        while time.time() - t0 < t:
            j = body(c.get(f"/api/v1/jobs/{jid}"))
            if j.get("status") in ("succeeded", "failed"):
                return j
            sleep(1)
        return j

    def get_retry(client: httpx.Client, path: str, **kw) -> httpx.Response:
        """GET that rides out a cold start: connection resets/timeouts and 502/503/504 are retried every 3 s for up to startup_wait."""
        attempts = max(1, int(startup_wait // 3)) + 1
        last = ""
        for i in range(attempts):
            try:
                r = client.get(path, **kw)
                if r.status_code not in (502, 503, 504):
                    return r
                last = f"HTTP {r.status_code}"
            except httpx.TransportError as e:
                last = type(e).__name__
            if i < attempts - 1:
                if i % 5 == 0:
                    out(f"      ... service not answering yet ({last}); waiting for it to start")
                sleep(3)
        raise RuntimeError(f"no usable answer after about {startup_wait:.0f} s (last: {last})")

    # ---- unauthenticated checks -------------------------------------------------------------------------------------
    t0 = time.time()
    check("GET /health (cold-start wait included)", lambda: ((r := get_retry(anon, "/health", timeout=180)).status_code == 200, f"{time.time() - t0:.1f} s"))

    def ready():
        r = anon.get("/ready", timeout=60)
        j = body(r)
        checks = j.get("checks") or {}
        return r.status_code == 200 and j.get("mode") == "simulation" and all(v == "ok" for v in checks.values()), f"mode={j.get('mode')} {checks}"
    check("GET /ready: simulation mode, database/redis/storage ok", ready)
    check("no token -> 401 error envelope", lambda: ((r := anon.get("/api/v1/destinations")).status_code == 401 and err_code(r) == "UNAUTHORIZED", ""))

    # ---- first authenticated call: tell the user clearly if the token is wrong -----------------------------------------
    try:
        r = get_retry(c, "/api/v1/destinations")
    except Exception as e:
        out(f"FAIL  could not reach the API: {type(e).__name__}: {str(e)[:120]}")
        out("\nstopped: no further checks were run")
        return 1
    if r.status_code == 401:
        out("FAIL  token check")
        out(REJECTED_HELP)
        out("\nstopped: no further checks were run")
        return 2
    if r.status_code == 429:
        out("FAIL  rate limited (HTTP 429): wait a minute and run the script again")
        return 1
    if r.status_code != 200:
        out(f"FAIL  GET /api/v1/destinations returned HTTP {r.status_code} ({err_code(r)}); the service may still be starting: wait and retry")
        return 1
    out("PASS  token accepted by the API")

    # ---- demo catalogue: the seed runs at start-up and can lag behind /ready on a cold, slow instance -------------------------
    def catalogue():
        t_start, n = time.time(), 0
        while True:
            n = len([d for d in body(c.get("/api/v1/destinations")).get("destinations", []) if str(d.get("id", "")).startswith("demo-")])
            if n >= 12 or time.time() - t_start >= catalogue_wait:
                break
            out(f"      ... waiting for the demo seed ({n}/12 demo destinations so far)")
            sleep(5)
        hint = "" if n >= 12 else " (the seed may not have run: look for 'seed: catalogue ready' in the Render logs)"
        return n >= 12, f"{n}/12 demo destinations{hint}"
    check("demo catalogue present", catalogue)

    # ---- the real checks ---------------------------------------------------------------------------------------------------
    holder: dict = {}

    def publish():
        r = c.post("/api/v1/publish", json={"destination_id": "demo-ghost", "rendition": {"title": "Smoke test publish title", "body": "hello"}},
                   headers={"Idempotency-Key": f"smoke-{run_id}"})
        holder["pub"] = r
        return r.status_code == 202 and "job_id" in body(r), f"HTTP {r.status_code}"
    check("POST /publish -> 202 + job_id", publish)

    def job_ok():
        j = wait(body(holder["pub"])["job_id"])
        return j.get("status") == "succeeded" and "manifest" in (j.get("result") or {}), str(j.get("status"))
    check("job succeeded with output manifest", job_ok)

    def replay():
        r2 = c.post("/api/v1/publish", json={"destination_id": "demo-ghost", "rendition": {"title": "Smoke test publish title", "body": "hello"}},
                    headers={"Idempotency-Key": f"smoke-{run_id}"})
        return body(r2).get("job_id") == body(holder["pub"]).get("job_id") and body(r2).get("idempotent_replay") is True, ""
    check("same Idempotency-Key -> same job", replay)

    def certify():
        r = c.post("/api/v1/destinations/demo-lemmy/certify", headers={"Idempotency-Key": f"smoke-cert-{run_id}"})
        j = wait(body(r)["job_id"])
        return j.get("status") == "succeeded", str(j.get("status"))
    check("certification job (publish->status->delete->verify, MOCK platform)", certify)

    def too_long():
        r = c.post("/api/v1/publish", json={"destination_id": "demo-lemmy", "rendition": {"title": "x" * 300, "body": "b"}}, headers={"Idempotency-Key": f"smoke-long-{run_id}"})
        j = wait(body(r)["job_id"])
        return (j.get("error") or {}).get("code") == "DEST_SPEC_VIOLATION", str((j.get("error") or {}).get("code"))
    check("over-long title -> DEST_SPEC_VIOLATION", too_long)

    def ssrf():
        r = c.post("/api/v1/publish", json={"destination_id": "demo-ghost", "rendition": {"title": "Smoke test callback check", "body": "b"}, "callback_url": "https://169.254.169.254/latest/meta-data/"},
                   headers={"Idempotency-Key": f"smoke-ssrf-{run_id}"})
        return r.status_code == 422 and err_code(r) == "CALLBACK_URL_REJECTED", f"HTTP {r.status_code} {err_code(r)}"
    check("unsafe callback_url (cloud metadata address) -> 422 CALLBACK_URL_REJECTED", ssrf)

    def files():
        up = c.post("/api/v1/files", files={"file": ("smoke.txt", b"smoke-bytes", "text/plain")}, headers={"Idempotency-Key": f"smoke-up-{run_id}"})
        return up.status_code == 201 and c.get(f"/api/v1/files/{body(up)['key']}").content == b"smoke-bytes", f"HTTP {up.status_code}"
    check("file upload + download", files)

    out(f"\n{'ALL PASSED' if not fails else str(fails) + ' FAILED'}")
    return 1 if fails else 0


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3) or not argv[1].startswith(("http://", "https://")):
        print(__doc__)
        return 2
    token = argv[2] if len(argv) == 3 else os.environ.get("INCUBRIX_API_TOKEN", "")
    if not token:
        if not sys.stdin.isatty():
            print("No token: pass it as the second argument, set INCUBRIX_API_TOKEN, or run in a terminal to be asked.")
            return 2
        token = getpass.getpass("API_TOKEN (input is hidden): ")
    try:
        return run(argv[1], token)
    except KeyboardInterrupt:
        print("interrupted")
        return 1
    except Exception as e:  # last line of defence: a clear message instead of a traceback
        print(f"unexpected error ({type(e).__name__}): {str(e)[:150]}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
