# IncuBrix Long-Tail Distribution Connector Service

Publishes one piece of content to many small destinations (forums, communities, dev blogs, partner sites) through
**one connector per platform type** and **one catalogue row per destination**. Adding forum number 41 is a single
`POST /api/v1/destinations`, not new code. Built for IncuBrix Engineering Assessment 4.

Every destination is checked against its posting rules before each post, every post is idempotent, and a destination
only counts as certified after a real test post was published, confirmed live, read back, deleted and the evidence stored.

## Architecture

```
            client / IncuBrix runtime
                     |  HTTPS  (Bearer token, Idempotency-Key, X-Correlation-Id)
                     v
  +----------------------------------------------+        +-----------+
  | FastAPI  /api/v1/*   /connector/v1/{k}/{m}   |------->| Postgres  |  destinations, jobs, posts,
  |  - auth, error envelope, idempotency          |        +-----------+  idempotency, rule events,
  |  - POST returns 202 + job_id                  |        +-----------+  certifications
  +-------------------+--------------------------+   +--->| Redis/RQ  |
                      | enqueue                      |    +-----------+
                      v                              |    +-----------+
  +----------------------------------------------+   +--->|  S3 store |  evidence, assist packs, uploads
  | RQ worker  (app/jobs.py -> app/runtime.py)    |        +-----------+
  |   validate -> RULES ENGINE -> idempotent post |
  |   -> connector.publish -> persist -> manifest |
  +-------------------+--------------------------+
                      v  (connectors return results only; they never touch the DB)
   discourse | lemmy | devto | ghost | webhook (HMAC) | assisted (pack, human posts)
                      v
   real platform  --or--  in-process / compose mock platforms (SIMULATION_MODE=true)
```

| Path | What lives there |
|---|---|
| `app/connectors/` | `base.py` contract + error mapping, `platforms.py` (6 connectors), `manifests/*.yaml`, `registry.py` |
| `app/runtime.py` | the only layer that writes to the DB: publish flow, certification, kill switch, 5-failure stop |
| `app/rules.py` | frequency cap, self-promotion, link policy, AI disclosure, destination status |
| `app/jobs.py` | RQ jobs, progress, HMAC-signed callbacks |
| `app/mocks/platforms.py` | mock Discourse/Lemmy/Dev.to/Ghost/partner site with fault injection |
| `cli/batch_runner.py` | batch runner; `samples/` holds 12 inputs |
| `tests/` | unit + integration + end-to-end + hard cases (162 tests, no API keys) |

## Quick start (under 10 commands)

```bash
cp .env.example .env                       # 1. settings (no real keys needed)
docker compose up --build -d               # 2. api, worker, postgres, redis, s3, mocks, ghost, lemmy
curl localhost:8000/ready                  # 3. {"status":"ready",...}
docker compose --profile test run --rm tests        # 4. all tests, no API keys
python -m cli.batch_runner samples --out results    # 5. batch runner (needs: pip install httpx)
open http://localhost:8000/docs            # 6. interactive API docs
```

Local, no Docker: `python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt && .venv/bin/pytest -q`.
`SIMULATION_MODE=true` (the default) routes every connector to mock platforms through the **same HTTP code path** a real
platform would use; nothing leaves the machine.

### Real local instances (Ghost, Lemmy): explicit real mode
`docker compose up` always starts in **simulation mode** (mock platforms; nothing can reach a real platform). To post to the
local Ghost and Lemmy containers you must opt in, on purpose:

```bash
docker compose -f docker-compose.yml -f docker-compose.real.yml up -d api worker
docker compose -f docker-compose.yml -f docker-compose.real.yml --profile seed run --rm seed   # repeatable, prints no secrets
curl -H "Authorization: Bearer $API_TOKEN" -X POST -H "Idempotency-Key: demo-1" localhost:8000/api/v1/destinations/local-ghost/certify
```
The seed creates the Ghost owner + Admin key and the Lemmy community `incubrix_test`, writes the credentials to the `localcreds`
volume (mode 0600, mounted **read-only** into api/worker, never printed) and registers `local-ghost` / `local-lemmy`
(fixed ids and idempotency keys, so reruns create no duplicates). `credentials_ref` names are resolved from that volume first,
then from environment variables. Go back to simulation with a plain `docker compose up -d`.
Ghost runs with `security__staffDeviceVerification=false` **for local development only** (no mail server exists, so the
verification email would make every repeat login fail); never disable it on a production Ghost.

### Free single-container demo
`deploy/render/` + `render.yaml` + `docs/RENDER_DEPLOY.md` describe an all-in-one, **simulation-only** image (SQLite + Redis + RQ worker +
API in one container) for a free host such as Render. It is separate from `docker-compose.yml`, tested locally, and **not deployed**.
`python scripts/test_render_demo.py` runs 49 checks against it.

### Callback URL safety (SSRF protection)
`callback_url` is checked when the job is submitted (`422 CALLBACK_URL_REJECTED`) and again, authoritatively, when the callback is sent:
HTTPS only, port 443 or 8443 (`CALLBACK_ALLOWED_PORTS`), no credentials in the URL, no `localhost`/internal suffixes/single-label names, no odd IP spellings, and
every address the host resolves to must be globally routable (loopback, private, link-local incl. the cloud metadata address, CGNAT, multicast and IPv6-embedded
forms are refused). The name is resolved once and the connection goes to that validated address (Host header and TLS name check use the original host name), which
defeats DNS rebinding. Redirects are never followed, proxies from the environment are ignored and the response is never read. Signing is unchanged:
`X-IncuBrix-Signature: sha256=HMAC_SHA256(CALLBACK_SIGNING_SECRET, raw_body)`. `CALLBACK_ALLOW_PRIVATE` / `CALLBACK_ALLOW_HTTP` exist for local development only; the demo image refuses to start with them.

### Local-only credentials (NOT production credentials)
These are throw-away values that exist only so the local test containers can start. They protect nothing, the containers
are reachable only on your machine / the compose network, and they must never be reused anywhere else:

| Where | Value | Used for |
|---|---|---|
| `docker/lemmy.hjson`, `scripts/seed_local.py` | Lemmy admin `incubrix` / `incubrix-local-only`; DB password `lemmy-local-only` | local Lemmy test instance |
| `scripts/seed_local.py` | Ghost owner `owner@incubrix.local` / `incubrix-local-only-1!` | local Ghost test instance |
| `.env.example` | `S3_ACCESS_KEY=local-access-key`, `S3_SECRET_KEY=local-secret-key`, `API_TOKEN=change-me-admin-token` | local storage and the demo API token |

Real secrets never go in the repository: platform credentials are referenced by **name** (`credentials_ref`) and resolved from the
read-only credentials volume or environment variables. The Ghost Admin key is generated per instance by the seed, stored with
mode 0600 in that volume, and never printed or logged. A public deployment must set its own `API_TOKEN` and
`CALLBACK_SIGNING_SECRET` (see section "Configuration").

## API (all under `/api/v1/`, Bearer token)

| Route | Purpose |
|---|---|
| `POST /destinations` | add a destination = one catalogue row (idempotent) |
| `GET /destinations`, `GET /destinations/{id}` | catalogue |
| `POST /destinations/{id}/certify` | job: publish test post -> confirm live -> status -> delete -> verify -> evidence |
| `GET /destinations/{id}/certifications` | stored evidence links |
| `POST /destinations/{id}/check` | dry-run of the posting rules and format limits |
| `POST /destinations/{id}/revoke` | partner revoked permission |
| `POST /publish` | job: queue a post (needs `Idempotency-Key`) |
| `GET /jobs/{job_id}` | `queued | running | succeeded | failed`, `progress 0-100`, `result`, `links` |
| `POST /connectors/{key}/disable|enable` | kill switch |
| `POST /files`, `GET /files/{key}` | upload in / download out (S3-compatible storage) |
| `POST /simulation/faults/{instance}` | flip a mock failure (simulation mode only) |
| `GET /health`, `GET /ready` | liveness / readiness (no auth) |
| `POST /connector/v1/{connector_key}/{method}` | Appendix A contract: validate, publish, get_status, delete, fetch_metrics, refresh_auth, build_assist_pack |

`openapi.yaml` (3.1) is generated from the app (`python scripts/export_openapi.py`) and a test fails if it is stale.

### Jobs and callbacks
`POST /publish` -> `202 {"job_id": ...}`. Optional `callback_url`: the finished job is POSTed there with
`X-IncuBrix-Signature: sha256=HMAC_SHA256(CALLBACK_SIGNING_SECRET, raw_body)`.

### Idempotency
Same key + same request = the same job, the work is never done twice. Same key + different request = `422 IDEMPOTENCY_KEY_REUSED`.
After a retryable failure (e.g. a timeout) the same key re-runs the same job; the `posts` row is unique per key and the key is
also sent to the platform, so a post created before the timeout is reconciled rather than duplicated.

## Error format and codes

```json
{"error": {"code": "DEST_SPEC_VIOLATION", "message": "...", "retryable": false, "details": {}}}
```

| Code | Retry? | Meaning |
|---|---|---|
| `DEST_SPEC_VIOLATION` | no | content breaks the destination's format limits (field-level reasons in `details.violations`) |
| `DEST_RULE_FREQUENCY_CAP` | later | cap reached; `details.reschedule_at` says when |
| `CONNECTION_AUTH_EXPIRED` | after reconnect | token expired/revoked |
| `PLATFORM_RATE_LIMIT` | yes | 429; `details.retry_after_seconds`/`retry_at` (short resets are slept through once) |
| `PLATFORM_QUOTA_EXHAUSTED` | next window | daily quota used |
| `PLATFORM_CONTENT_REJECTED` | no | platform refused; its message is in `details.platform_message` |
| `PLATFORM_UNAVAILABLE` | yes | 5xx / timeout; calls stop after 5 consecutive failures |
| `CONNECTOR_DISABLED` | no | kill switch on |
| `DEST_RULE_VIOLATION` | no | self-promotion / link policy / AI disclosure / automation not allowed |
| `DEST_NOT_CERTIFIED` | no | destination demoted after failed certification |
| `DEST_PERMISSION_REVOKED` | no | partner revoked permission |
| `CONTRACT_VIOLATION` | no | platform changed its response (renamed field); certification fails and the destination is demoted |
| `CALLBACK_URL_REJECTED` | no | `callback_url` failed the outbound-request safety checks (see below) |
| `QUEUE_UNAVAILABLE` | yes | job queue (Redis) down: HTTP 503 with `Retry-After: 5`; retry with the same `Idempotency-Key` re-runs the same job |
| `IDEMPOTENCY_KEY_REQUIRED` / `IDEMPOTENCY_KEY_REUSED` | no | idempotency problems |
| `UNAUTHORIZED`, `NOT_FOUND`, `VALIDATION_ERROR`, `RATE_LIMITED`, `INTERNAL_ERROR` | - | service level |

## Configuration
Everything comes from environment variables; see `.env.example` (every setting is listed there). Credentials are never stored:
a destination row holds only `credentials_ref`, the **name** of an environment variable.

## Tests
`pytest -q` runs 162 tests offline: contract tests for all six connectors, every hard case, rules, idempotency
(including concurrent requests and timeout-after-publish), secret redaction, OpenAPI freshness and the batch runner.

## What is not built
Advanced and Outstanding levels (CSV import, AI rules capture, relevance scoring, tailoring, extra connectors, recommender,
metrics beyond three connectors' basic `fetch_metrics`, recertification scheduler, 150-destination research) are **not** implemented.
See REPORT.md.
