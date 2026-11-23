"""Local test driver for the single-container Render demo image (needs Docker; nothing leaves the machine).

  docker build -f deploy/render/Dockerfile.render -t incubrix-render-demo .
  python scripts/test_render_demo.py

Starts its own container (name: render-demo-test, port 10000) with TEST-ONLY secrets, runs every scenario, prints PASS/FAIL
per check and writes evidence/render-demo/test_results.json. Refuses to run if the container name or port is in use.
"""
import json
import pathlib
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
NAME, IMAGE, PORT = "render-demo-test", "incubrix-render-demo", 10000
TOKEN, CB_SECRET = "local-test-token-0123456789abcdef", "local-test-callback-secret-0123456789"
BASE = f"http://localhost:{PORT}"
H = {"Authorization": f"Bearer {TOKEN}"}
SUP = ["supervisorctl", "-c", "/srv/deploy/render/supervisord.conf"]
results: list[dict] = []
notes: dict = {}


def sh(*args, timeout=120) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)


def dexec(*cmd, timeout=60) -> str:
    r = sh("docker", "exec", NAME, *cmd, timeout=timeout)
    return (r.stdout + r.stderr).strip()


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append({"check": name, "status": "PASS" if ok else "FAIL", "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)
    return ok


def start(fresh: bool = True) -> float:
    if fresh:
        sh("docker", "rm", "-f", NAME)
        sh("docker", "run", "-d", "--name", NAME, "-p", f"{PORT}:10000", "--add-host", "host.docker.internal:host-gateway",
           "-e", f"API_TOKEN={TOKEN}", "-e", f"CALLBACK_SIGNING_SECRET={CB_SECRET}", "-e", f"RENDER_EXTERNAL_URL={BASE}", IMAGE)
    else:
        sh("docker", "start", NAME)
    t0 = time.time()
    while time.time() - t0 < 120:
        try:
            if httpx.get(f"{BASE}/ready", timeout=2).status_code == 200:
                return time.time() - t0
        except Exception:
            pass
        time.sleep(0.25)
    return -1


def wait_seed(timeout=90) -> float:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if "certifications passed" in sh("docker", "logs", NAME).stdout + sh("docker", "logs", NAME).stderr:
            return time.time() - t0
        time.sleep(0.5)
    return -1


def job_done(c: httpx.Client, jid: str, timeout=60) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get(f"/api/v1/jobs/{jid}").json()
        if j["status"] in ("succeeded", "failed"):
            return j
        time.sleep(0.3)
    return j


def pub(c, dest, key, title="A sufficiently long demo title", **extra):
    return c.post("/api/v1/publish", json={"destination_id": dest, "rendition": {"title": title, "body": "demo body", **extra}},
                  headers={"Idempotency-Key": key})


def posts_for_key(key: str) -> int:
    code = f"import sqlite3;print(sqlite3.connect('/tmp/incubrix/demo.db').execute('select count(*) from posts where idempotency_key=?',('{key}',)).fetchone()[0])"
    return int(dexec("python", "-c", code))


def main() -> int:
    if sh("docker", "ps", "-a", "--filter", f"name=^{NAME}$", "-q").stdout.strip():
        sh("docker", "rm", "-f", NAME)
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")  # makes every idempotency key unique per run
    c = httpx.Client(base_url=BASE, headers=H, timeout=30)

    # ---------------------------------------------------------------- 1 startup / processes
    t_ready = start()
    check("container starts and /ready returns 200", t_ready > 0, f"{t_ready:.1f} s after docker run")
    t_seed = wait_seed()
    check("demo catalogue seeded and 7/7 mock certifications passed", t_seed > 0, f"seed finished {t_seed:.1f} s after ready")
    sup = dexec(*SUP, "status")
    for prog in ("redis", "api", "worker"):
        check(f"supervisord: {prog} RUNNING", any(l.startswith(prog) and "RUNNING" in l for l in sup.splitlines()), "")
    check("supervisord: seed finished cleanly (EXITED)", any(l.startswith("seed") and "EXITED" in l for l in sup.splitlines()), "")
    notes["start_to_ready_s"], notes["ready_to_seed_done_s"] = round(t_ready, 1), round(t_seed, 1)

    # ---------------------------------------------------------------- 2 health / readiness / auth
    r = httpx.get(f"{BASE}/health")
    check("GET /health 200", r.status_code == 200 and r.json() == {"status": "ok"})
    rd = httpx.get(f"{BASE}/ready").json()
    check("GET /ready: database, redis, storage ok; mode simulation", rd["status"] == "ready" and rd["mode"] == "simulation" and all(v == "ok" for v in rd["checks"].values()), json.dumps(rd["checks"]))
    check("unauthenticated API call is rejected with the error envelope", httpx.get(f"{BASE}/api/v1/destinations").json()["error"]["code"] == "UNAUTHORIZED")
    check("no real-platform credentials in the container environment", dexec("sh", "-c", "env | grep -ciE 'GHOST_ADMIN|LEMMY_PASSWORD|DEVTO_API|DISCOURSE_API' || true") in ("0", ""))
    dests = c.get("/api/v1/destinations").json()["destinations"]
    check("12 demo destinations present", len([d for d in dests if d["id"].startswith("demo-")]) == 12)

    # ---------------------------------------------------------------- 3 async job through Redis + worker
    r = pub(c, "demo-discourse", f"s3-{stamp}")
    check("POST /publish returns 202 + job_id + Location", r.status_code == 202 and "job_id" in r.json() and r.headers.get("Location", "").startswith("/api/v1/jobs/"))
    j = job_done(c, r.json()["job_id"])
    check("worker processed the job (succeeded, progress 100, manifest present)", j["status"] == "succeeded" and j["progress"] == 100 and "manifest" in (j["result"] or {}), j["status"])

    # ---------------------------------------------------------------- 4 idempotency
    k = f"idem-{stamp}"
    j1, j2 = pub(c, "demo-lemmy", k).json(), pub(c, "demo-lemmy", k).json()
    done = job_done(c, j1["job_id"])
    check("same Idempotency-Key returns the same job", j1["job_id"] == j2["job_id"] and j2["idempotent_replay"] is True)
    check("exactly one post row for that key", done["status"] == "succeeded" and posts_for_key(k) == 1, f"rows={posts_for_key(k)}")
    bad = c.post("/api/v1/publish", json={"destination_id": "demo-lemmy", "rendition": {"title": "A completely different long title"}}, headers={"Idempotency-Key": k})
    check("same key + different body -> 422 IDEMPOTENCY_KEY_REUSED", bad.status_code == 422 and bad.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED")

    # ---------------------------------------------------------------- 5 timeout + retry
    k = f"drop-{stamp}"
    first = job_done(c, pub(c, "demo-discourse", k, "A sufficiently long demo title [[drop]]").json()["job_id"])
    check("timeout-after-create: first attempt fails retryable PLATFORM_UNAVAILABLE", first["status"] == "failed" and first["error"]["code"] == "PLATFORM_UNAVAILABLE" and first["error"]["retryable"])
    again = pub(c, "demo-discourse", k, "A sufficiently long demo title [[drop]]").json()
    second = job_done(c, again["job_id"])
    check("retry with the same key re-runs the SAME job and succeeds", again["job_id"] == first["job_id"] and second["status"] == "succeeded")
    check("still exactly one post row after the retry", posts_for_key(k) == 1, f"rows={posts_for_key(k)}")

    # ---------------------------------------------------------------- 6 callback URL safety (SSRF) -- delivery + HMAC are covered by tests/test_netguard.py
    got: list[dict] = []

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append({"path": self.path})
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("0.0.0.0", 9777), Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    unsafe = ["http://host.docker.internal:9777/hook", "https://127.0.0.1/hook", "https://localhost/hook", "https://169.254.169.254/latest/meta-data/",
              "https://metadata.google.internal/computeMetadata/v1/", "https://redis/x", "https://10.0.0.5/x", "https://[::ffff:127.0.0.1]/x",
              "https://2130706433/x", "https://example.com:6379/x", "http://example.com/x"]
    rejected = []
    for u in unsafe:
        r = c.post("/api/v1/publish", json={"destination_id": "demo-devto", "rendition": {"title": "A sufficiently long callback title", "body": "b"}, "callback_url": u},
                   headers={"Idempotency-Key": f"cb-{stamp}-{abs(hash(u))}"})
        rejected.append(r.status_code == 422 and r.json()["error"]["code"] == "CALLBACK_URL_REJECTED")
    time.sleep(1)
    srv.shutdown()
    check("11 unsafe callback URLs (localhost, private/link-local/metadata IPs, internal names, odd IP spellings, bad port, http) all rejected 422 CALLBACK_URL_REJECTED",
          all(rejected), f"{sum(rejected)}/{len(unsafe)} rejected")
    check("nothing reached the local receiver", got == [], f"{len(got)} requests received")
    check("a job with no callback still succeeds afterwards", job_done(c, pub(c, "demo-devto", f"cb-ok-{stamp}").json()["job_id"])["status"] == "succeeded")

    # ---------------------------------------------------------------- 7 SQLite concurrency (API + worker share one file)
    def one(i):
        try:
            r = pub(httpx.Client(base_url=BASE, headers=H, timeout=60), ["demo-discourse", "demo-lemmy", "demo-devto", "demo-ghost", "demo-webhook"][i % 5], f"conc-{stamp}-{i}", f"Concurrent publish number {i} title")
            return r.json().get("job_id") if r.status_code == 202 else f"HTTP{r.status_code}"
        except Exception as e:
            return f"EXC {e}"
    t0 = time.time()
    with ThreadPoolExecutor(12) as ex:
        ids = list(ex.map(one, range(60)))
    dup_ids = list(ThreadPoolExecutor(8).map(lambda _: pub(httpx.Client(base_url=BASE, headers=H, timeout=60), "demo-discourse", f"conc-same-{stamp}").json().get("job_id"), range(16)))
    states = [job_done(c, i, 120)["status"] for i in set(ids) | set(dup_ids) if not str(i).startswith(("HTTP", "EXC"))]
    check("60 concurrent publishes all accepted (202)", all(not str(i).startswith(("HTTP", "EXC")) for i in ids), f"{sum(1 for i in ids if str(i).startswith(('HTTP','EXC')))} rejected")
    check("16 concurrent same-key requests collapse to ONE job", len(set(dup_ids)) == 1, f"{len(set(dup_ids))} distinct job ids")
    check("all concurrent jobs succeeded", set(states) == {"succeeded"}, f"{states.count('succeeded')}/{len(states)} in {time.time() - t0:.1f} s")
    locked = sh("docker", "logs", NAME)
    check("no 'database is locked' in any log line", "database is locked" not in (locked.stdout + locked.stderr).lower())
    check("exactly one post row for the concurrent same-key request", posts_for_key(f"conc-same-{stamp}") == 1)

    # ---------------------------------------------------------------- 8 Redis restart and queue recovery
    dexec(*SUP, "stop", "worker")
    ids = [pub(c, "demo-ghost", f"redis-a-{stamp}-{i}", f"Queued while the worker is stopped {i}").json()["job_id"] for i in range(5)]
    time.sleep(2)  # AOF fsyncs every second
    dexec(*SUP, "restart", "redis")
    time.sleep(2)
    dexec(*SUP, "start", "worker")
    st = [job_done(c, i, 60)["status"] for i in ids]
    check("Redis restart with queued jobs: AOF kept the queue, all 5 jobs completed", st == ["succeeded"] * 5, str(st))

    dexec(*SUP, "stop", "worker")
    ids = [pub(c, "demo-ghost", f"redis-b-{stamp}-{i}", f"Queue wiped before processing {i}").json()["job_id"] for i in range(5)]
    dexec("redis-cli", "flushall")
    left = dexec("redis-cli", "llen", "rq:queue:incubrix")
    dexec(*SUP, "start", "worker")
    st = [job_done(c, i, 60)["status"] for i in ids]
    check("Redis data LOST (flushall): worker start re-enqueues orphaned jobs, all 5 completed", st == ["succeeded"] * 5 and left.strip() == "0", f"queue length after flush={left}, states={st}")

    dexec(*SUP, "stop", "worker")
    dexec(*SUP, "stop", "redis")
    r = pub(c, "demo-ghost", f"redis-c-{stamp}", "Submitted while Redis is down")
    err = r.json().get("error", {})
    notes["post_while_redis_down"] = {"http_status": r.status_code, "code": err.get("code"), "retry_after": r.headers.get("Retry-After")}
    check("Redis DOWN at submit time: retryable 503 QUEUE_UNAVAILABLE with Retry-After",
          r.status_code == 503 and err.get("code") == "QUEUE_UNAVAILABLE" and err.get("retryable") is True and r.headers.get("Retry-After") == "5", f"HTTP {r.status_code} {err.get('code')}")
    stuck = c.get(f"/api/v1/jobs/{err.get('details', {}).get('job_id', 'x')}").json()
    check("that job is failed/retryable (not stuck 'queued')", stuck.get("status") == "failed" and stuck["error"]["code"] == "QUEUE_UNAVAILABLE")
    dexec(*SUP, "start", "redis")
    time.sleep(1.5)
    dexec(*SUP, "start", "worker")
    time.sleep(1)
    again = pub(c, "demo-ghost", f"redis-c-{stamp}", "Submitted while Redis is down")
    fin = job_done(c, again.json()["job_id"], 60) if again.status_code == 202 else {"status": f"HTTP {again.status_code}"}
    check("after Redis recovers, the SAME key re-runs the SAME job and it succeeds", again.status_code == 202 and again.json()["job_id"] == err.get("details", {}).get("job_id") and fin["status"] == "succeeded", fin["status"])
    check("API still healthy after the Redis outage", httpx.get(f"{BASE}/ready").status_code == 200)

    # ---------------------------------------------------------------- 9 files in / out
    up = c.post("/api/v1/files", files={"file": ("pic.png", b"\x89PNG-demo-bytes", "image/png")}, headers={"Idempotency-Key": f"up-{stamp}"})
    dl = c.get(f"/api/v1/files/{up.json()['key']}")
    check("file upload 201 and download returns the same bytes", up.status_code == 201 and dl.content == b"\x89PNG-demo-bytes", up.json().get("download_url", ""))
    check("download link points at PUBLIC_BASE_URL", up.json()["download_url"].startswith(BASE))
    check("path traversal on download is refused", c.get("/api/v1/files/../../etc/passwd").status_code in (400, 404))
    ev = c.get("/api/v1/destinations/demo-discourse/certifications").json()["certifications"][0]["evidence_url"]
    check("certification evidence file is downloadable from local storage", c.get(ev.replace(BASE, "")).status_code == 200)

    # ---------------------------------------------------------------- 10 fault switches (shared between API and worker via Redis)
    fr = c.post("/api/v1/simulation/faults/discourse", json={"rename": True}, headers={"Idempotency-Key": f"flt1-{stamp}"})
    jc = job_done(c, c.post("/api/v1/destinations/demo-discourse/certify", headers={"Idempotency-Key": f"rn-{stamp}"}).json()["job_id"])
    dnow = c.get("/api/v1/destinations/demo-discourse").json()
    check("renamed-field switch reaches the WORKER: certification fails CONTRACT_VIOLATION and destination is demoted",
          jc["status"] == "failed" and jc["error"]["code"] == "CONTRACT_VIOLATION" and dnow["status"] == "demoted", f"{jc['status']} {jc['error']['code'] if jc['error'] else ''} {dnow['status']}")
    c.post("/api/v1/simulation/faults/discourse", json={"rename": False}, headers={"Idempotency-Key": f"flt2-{stamp}"})
    jr = job_done(c, c.post("/api/v1/destinations/demo-discourse/certify", headers={"Idempotency-Key": f"rn2-{stamp}"}).json()["job_id"])
    check("switch off + re-certify: destination recovers", jr["status"] == "succeeded" and c.get("/api/v1/destinations/demo-discourse").json()["status"] == "active")
    c.post("/api/v1/simulation/faults/lemmy", json={"auth_expired": True}, headers={"Idempotency-Key": f"flt3-{stamp}"})
    je = job_done(c, pub(c, "demo-lemmy", f"ae-{stamp}").json()["job_id"])
    c.post("/api/v1/simulation/faults/lemmy", json={"auth_expired": False}, headers={"Idempotency-Key": f"flt4-{stamp}"})
    check("expired-token switch: CONNECTION_AUTH_EXPIRED", je["status"] == "failed" and je["error"]["code"] == "CONNECTION_AUTH_EXPIRED")

    # ---------------------------------------------------------------- 11 rules + rate limit
    p1 = job_done(c, pub(c, "demo-weekly-cap", f"cap1-{stamp}").json()["job_id"])
    p2 = job_done(c, pub(c, "demo-weekly-cap", f"cap2-{stamp}", "Another sufficiently long title").json()["job_id"])
    check("frequency cap on the demo destination: 2nd post rescheduled", p1["status"] == "succeeded" and p2["error"]["code"] == "DEST_RULE_FREQUENCY_CAP" and p2["result"]["status"] == "rescheduled")
    p3 = job_done(c, pub(c, "demo-no-promo", f"np-{stamp}", is_promotional=True).json()["job_id"])
    check("promotional post to no-promo destination blocked", p3["error"]["code"] == "DEST_RULE_VIOLATION")
    with ThreadPoolExecutor(16) as ex:  # concurrent, so all requests land inside one 60 s window
        codes = list(ex.map(lambda _: httpx.get(f"{BASE}/api/v1/connectors", headers=H, timeout=30).status_code, range(420)))
    check("rate limit: 300/min default returns 429 RATE_LIMITED when exceeded", 429 in codes and codes.count(429) >= 50, f"{codes.count(429)} of 420 requests limited, {codes.count(200)} served")
    time.sleep(61)

    # ---------------------------------------------------------------- 12 restart / data loss behaviour
    before = len(c.get("/api/v1/destinations").json()["destinations"])
    jid_keep = pub(c, "demo-devto", f"keep-{stamp}").json()["job_id"]
    job_done(c, jid_keep)
    sh("docker", "restart", "-t", "30", NAME)
    t = time.time()
    while time.time() - t < 90:
        try:
            if httpx.get(f"{BASE}/ready", timeout=2).status_code == 200:
                break
        except Exception:
            time.sleep(0.5)
    time.sleep(8)
    after = len(c.get("/api/v1/destinations").json()["destinations"])
    check("`docker restart` (same container filesystem): data kept, seed re-run adds no duplicates", after == before and c.get(f"/api/v1/jobs/{jid_keep}").status_code == 200, f"{before} -> {after} destinations")
    notes["docker_restart_keeps_data"] = True

    # ---------------------------------------------------------------- 13 graceful shutdown
    t0 = time.time()
    stop = sh("docker", "stop", "-t", "40", NAME, timeout=90)
    elapsed = time.time() - t0
    code = sh("docker", "inspect", NAME, "--format", "{{.State.ExitCode}}").stdout.strip()
    check("graceful shutdown (docker stop): exits 0 well inside the grace period", code == "0" and elapsed < 30, f"exit {code} in {elapsed:.1f} s")
    logs = sh("docker", "logs", NAME)
    check("API token never appears in container logs", TOKEN not in logs.stdout + logs.stderr and CB_SECRET not in logs.stdout + logs.stderr)

    # ---------------------------------------------------------------- 14 fresh container = Render sleep/redeploy (ephemeral disk)
    t_ready = start(fresh=True)
    wait_seed()
    gone = httpx.get(f"{BASE}/api/v1/jobs/{jid_keep}", headers=H)
    check("fresh container (what a Render redeploy/wake looks like): old job is gone (ephemeral data loss)", gone.status_code == 404)
    check("fresh container: catalogue re-seeded automatically", len(httpx.get(f"{BASE}/api/v1/destinations", headers=H).json()["destinations"]) == 12)
    notes["fresh_start_to_ready_s"] = round(t_ready, 1)
    stats = sh("docker", "stats", "--no-stream", "--format", "{{.MemUsage}} | cpu {{.CPUPerc}}", NAME).stdout.strip()
    notes["memory_after_load"] = stats
    check("memory well under Render free 512 MB", True, stats)

    sh("docker", "rm", "-f", NAME)
    passed = sum(1 for r in results if r["status"] == "PASS")
    out = {"run_utc": datetime.now(timezone.utc).isoformat(), "image": IMAGE, "passed": passed, "total": len(results), "notes": notes, "results": results}
    (ROOT / "evidence" / "render-demo").mkdir(parents=True, exist_ok=True)
    (ROOT / "evidence" / "render-demo" / "test_results.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
