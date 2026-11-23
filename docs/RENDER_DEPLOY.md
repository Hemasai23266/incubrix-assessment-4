# Free single-container demo (Render): instructions and honest limits

**Status: prepared and tested locally in Docker only. NOT deployed.** Nothing in this repository creates an account, pushes code,
or deploys anything; every step below is done by you, deliberately.

The demo image (`deploy/render/Dockerfile.render`) runs **simulation mode only**. It refuses to start with `SIMULATION_MODE=false`,
with a missing, short or example `API_TOKEN`, with an example `CALLBACK_SIGNING_SECRET`, or with a non-SQLite database URL. It contains no
real-platform credentials and cannot publish to any real community.

## 1. How it differs from the full stack
| Part | Full stack (`docker-compose.yml`) | Free single-container demo |
|---|---|---|
| Containers | 10 (api, worker, Postgres, Redis, S3, mocks, Ghost, Lemmy, pict-rs, lemmy-db) | **1**, processes managed by supervisord |
| Database | PostgreSQL 16 (named volume) | **SQLite file** in `/tmp` (WAL, busy timeout, `BEGIN IMMEDIATE`) |
| Queue | Redis container + RQ `Worker` (forks per job) | Redis **inside** the container (AOF persistence) + RQ **`SimpleWorker`** (one long-lived process) |
| File storage | S3-compatible (SeaweedFS) | **Local disk** (`/tmp/incubrix/store`), same download links |
| Real Ghost / Lemmy instances | yes (real-mode override) | **no**, mock platforms only |
| Data persistence | Docker volumes survive restarts | **Ephemeral**: lost whenever the host recreates the container |
| Orphaned-job recovery | n/a | on worker start, jobs still `queued` or `running` in the DB but not in the queue are re-enqueued (`app/recovery.py`) |
| Demo data | created by you | catalogue of 12 destinations plus 7 mock certifications are **seeded automatically at start-up** |

Why `SimpleWorker`: the simulation-mode mock platforms keep their state in memory. A forked per-job worker forgets it after every job,
so a retry could not find the post its timed-out first attempt created. One long-lived worker process keeps it.

## 2. Files
`deploy/render/Dockerfile.render`, `entrypoint.sh` (fail-closed checks), `supervisord.conf` (redis, api, worker, seed), `run_worker.py`,
`scripts/seed_demo.py`, `scripts/test_render_demo.py` (local test driver), `scripts/smoke_test_deployed.py`, `render.yaml`
(Render blueprint), `.gitattributes` (keeps shell scripts LF), `app/recovery.py`.

## 3. Test it locally (no account, no cost)
```bash
docker build -f deploy/render/Dockerfile.render -t incubrix-render-demo .
python scripts/test_render_demo.py     # 49 checks, about 4 minutes; test-only secrets; writes evidence/render-demo/test_results.json
```

## 4. Deploying it (YOU do these steps; none has been done)
1. Create a **private** GitHub repository and push this repo (it must never contain `.env`; it is already git-ignored).
2. Create a Render account (a card may or may not be requested; check on the signup page, and do not upgrade anything).
3. *New, then Blueprint*: select the repository (Render reads `render.yaml`), plan **Free**. Render generates `API_TOKEN` and
   `CALLBACK_SIGNING_SECRET`. Read `API_TOKEN` in the service's *Environment* tab (do not paste it in chats or commits).
4. Wait for the first deploy (the image build takes a few minutes), then run:
   `python scripts/smoke_test_deployed.py https://<service>.onrender.com` (it asks for the token with hidden input; or set the
   `INCUBRIX_API_TOKEN` environment variable; or pass the token as a second argument).
   Use the service's **`API_TOKEN`**, not `CALLBACK_SIGNING_SECRET`. The token is never printed.
5. In the Submission Form: demo URL = the Render URL, token = `API_TOKEN`, cold-start time = what the smoke test printed for the first request.

### If the smoke test fails
| Output | Meaning | What to do |
|---|---|---|
| `FAIL  token check` + "rejected the token" (exit code 2) | the API answered 401 | copy `API_TOKEN` again (Render, Environment, reveal); not `CALLBACK_SIGNING_SECRET`; no spaces, quotes or angle brackets; use the new value if the service was recreated |
| `FAIL  demo catalogue present [n/12 ...]` | the start-up seed has not finished or did not run (it runs after boot and can lag on the free plan's small CPU) | the script already waits up to 2 minutes; if it still fails, look for `seed: catalogue ready` and `seed: 7/7 certifications passed` in the Render logs and tell the developer what the logs show |
| `FAIL  rate limited (HTTP 429)` | more than 300 requests in a minute from your address | wait a minute and run it again |
| first `GET /health` takes about a minute | cold start after the service slept | normal on the free plan; note the time for the form |

## 5. Free-tier behaviour you must disclose
* **Cold starts:** Render's free web services spin down after about 15 minutes without traffic and take up to about a minute to wake
  (Render's documentation; not measured by us). Locally the container is ready about 3 s after start and the seed finishes about 4 s later.
* **Ephemeral data:** a spin-down or redeploy creates a fresh container, so jobs, posts and uploads vanish; the catalogue is re-seeded
  automatically. Verified locally by starting a fresh container (old job id gives 404, 12 destinations are back).
  `docker restart` of the *same* container keeps data (verified), which is not what Render does on wake.
* **Resources:** about 140-155 MB RAM after the full test load (Render free is documented at 512 MB; confirm the current limit on Render).
* **Rate limit:** 300 requests per minute per client IP, in-process (verified: 178 of 420 concurrent requests were limited).

## 6. Reviewer guide: triggering each hard case on the demo (simulation mode)
Send `Authorization: Bearer <API_TOKEN>` and a unique `Idempotency-Key` on every POST. Use destination ids from `GET /api/v1/destinations`.

| Situation | How |
|---|---|
| Timeout after the platform created the post, then retry | `POST /api/v1/publish` to `demo-discourse` with a title of 15+ characters ending in `[[drop]]`. Job 1 fails `PLATFORM_UNAVAILABLE`. Repeat the same request (same key): the same job succeeds, with 1 post. |
| 429 with a reset time | title containing `[[429]]` (short reset: backed off and retried) or `[[429long]]` (120 s reset surfaced with `retry_at`). `[[quota]]` gives `PLATFORM_QUOTA_EXHAUSTED`. |
| Content rejected | title containing `[[reject]]` gives `PLATFORM_CONTENT_REJECTED` with the platform message |
| Expired token | `POST /api/v1/simulation/faults/{instance}` with `{"auth_expired": true}` (instances: discourse, discourse-b, lemmy, devto, ghost, partner-site, partner-site-b), then publish; turn it off afterwards |
| Platform renames a field | `{"rename": true}` on an instance, then `POST /api/v1/destinations/{id}/certify` fails `CONTRACT_VIOLATION` and the destination is demoted. Turn it off and certify again to recover. |
| Partner revokes permission | `POST /api/v1/destinations/demo-webhook/revoke` (or `{"revoked": true}` on `partner-site`) |
| Title or body too long, unsupported media | a 300-char title, or `media: [{"content_type": "video/mp4"}]` to a Discourse destination gives `DEST_SPEC_VIOLATION` |
| Frequency cap | publish twice to `demo-weekly-cap`; the second is `DEST_RULE_FREQUENCY_CAP` with `reschedule_at` |
| Promotional post to a no-promotion community | `"is_promotional": true` to `demo-no-promo` gives `DEST_RULE_VIOLATION` |
| AI-content disclosure | `"ai_generated": true` to `demo-ai-disclosure` without `disclosure` is blocked; add `"disclosure"` and it posts |
| Non-English community | publish Hindi text to `demo-non-english` |
| Callback URL safety | send `callback_url` = `https://169.254.169.254/...`, `http://...`, `https://localhost/...`: all give `422 CALLBACK_URL_REJECTED`; a public HTTPS URL is accepted and called with a signed `X-IncuBrix-Signature` |
| URL input | `POST /api/v1/files/from-url` with a public https image URL and an `Idempotency-Key`; then poll the job. `http://169.254.169.254/...`, `http://localhost/...` and private addresses are rejected with `422 MEDIA_URL_REJECTED` |
| Idempotent replay on admin routes | an `Idempotency-Key` is REQUIRED on every POST; send the same key twice to `/api/v1/connectors/{key}/disable`: the second answer carries `Idempotent-Replay: true` |
| Standard error envelope | `GET /api/v1/publish` returns `405 METHOD_NOT_ALLOWED` in the same envelope |
| Assisted (no automation) | publish to `demo-assisted` for a ready-to-post pack and checklist; nothing is posted |

The switches are shared by everyone using the demo, so turn yours off when you are done.

## 7. Known limits (also in REPORT.md)
* Live mode (real platforms) is not enabled here, and its platform `base_url` values are not SSRF-guarded (see README).
* Mock platforms only: this demo proves the service, not real-platform behaviour.
* SQLite serialises writers (`BEGIN IMMEDIATE`): fine for a demo, not for production load.
* If Redis is down at submit time the API returns a retryable `503 QUEUE_UNAVAILABLE` (`Retry-After: 5`) and marks the job failed-but-retryable;
  retrying with the same `Idempotency-Key` re-runs the same job (verified). The worker also re-enqueues orphaned jobs when it starts.
* `callback_url` is restricted to public HTTPS addresses (SSRF protection with DNS pinning; see README). Reviewers must use a public HTTPS endpoint,
  for example a request-bin style service; `http://`, localhost, private and link-local addresses are rejected with `422 CALLBACK_URL_REJECTED`.
* Mock state lives in the worker's memory, so it resets when the container restarts.
