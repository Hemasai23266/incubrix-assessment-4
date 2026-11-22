# REPORT: Long-Tail Distribution Connector Service

## 1. Approaches considered and why this one
| Decision | Options | Chosen and why |
|---|---|---|
| Connector shape | one class per destination / one per platform *type* | **One per platform type**; a destination is a catalogue row (`options.base_url`, community, credentials *name*). Adding forum 41 is one `POST /api/v1/destinations`; a test certifies a second Discourse forum with zero code. |
| Who writes the DB | connectors / runtime | **Runtime only** (Appendix A). Connectors take dicts and return dicts, so they are trivially testable and language-agnostic. |
| Mock platforms | stub functions / real HTTP mocks | **HTTP mocks behind the same code path.** Simulation mode sends real `httpx` requests into an in-process ASGI mock (also runnable as a compose container), so header handling, status mapping and JSON parsing are exercised exactly as against a real platform. Fault injection (`[[429]]`, `[[drop]]`, renamed fields, revoked partner) reproduces every hard case. |
| Exactly-once publishing | client-side only / DB only / both | **Three layers**: (1) `(scope, key)` unique idempotency record at the API (same key -> same job, concurrent requests resolved by the unique constraint, enqueue only by the winner); (2) `posts.idempotency_key` unique row in the runtime; (3) the key is also sent to the platform. After a timeout the post row is `unknown` and a retry with the same key re-runs the same job. |
| Jobs | Celery / RQ / threads | **RQ** (small, Redis-backed); `JOB_BACKEND=inline` runs jobs synchronously for tests. |
| Rules | in connectors / separate engine | **Separate engine** (`app/rules.py`) that always runs before a publish and records every evaluation in `rule_events`. A hard block beats a reschedule. |

## 2. What works (all verified by running it)
* **Contract**: `POST /connector/v1/{key}/{method}` for validate, publish, get_status, delete, fetch_metrics, refresh_auth (stub: not supported -> 501), build_assist_pack; manifests for 6 connectors; the 8 shared error codes plus a few service-level codes (documented in README).
* **Six Core connectors**: Discourse, Lemmy, Dev.to, Ghost, HMAC webhook, assisted pack. All six certify in simulation (5 runs each, 5/5 passed). **Ghost and Lemmy were also certified against real local instances** (docker compose containers, `evidence/local_instances/`): publish, live URL confirmed, status, delete, deletion verified, 1.2-2.9 s per run.
* **Rules engine**: frequency cap (rolling window, UTC, test posts excluded, `reschedule_at` returned), self-promotion, link policy (`allowed|forbidden|max:N`), AI disclosure, destination status/automation. On my own 16-case labelled set: 7/7 violations caught, 0 missed, 0 false positives (`evidence/rule_safety.json`). **This is my set, not IncuBrix's hidden one.**
* **Reliability**: 100/100 simulated publishes succeeded; a retry storm of 60 submissions over 20 keys produced exactly 20 posts (0 duplicates); timeout-after-create and 6 concurrent same-key requests also produce one post (`tests/test_hard_cases.py`).
* **Hard cases**: every item in the brief except the CSV one has a test; see `evidence/hard_cases.md`.
* **Integration Standard**: compose stack (Postgres, Redis, RQ worker, SeaweedFS S3, mocks, Ghost, Lemmy) starts with one command and the batch runner passes 12/12 against it (`evidence/batch_run_docker/`); `openapi.yaml` is generated and a test fails if it is stale; 202 + job polling + HMAC callbacks; `Idempotency-Key` on POSTs; JSON error envelope; env-only config; upload + download links; JSON logs with correlation id and job id, secret redaction; `/health`, `/ready`; output manifest on every result; batch runner CLI.
* **Tests**: 62 tests, all offline (`docker compose --profile test run --rm tests` or `pytest -q`), 62 passed.

## 3. What does not work / is missing (honest list)
* **Advanced and Outstanding levels are not built**: CSV catalogue import, AI rules capture, relevance scoring, tailoring, three extra connectors, recommender, recertification scheduler, 150-destination research. `fetch_metrics` exists on Discourse, Lemmy and Dev.to against mocks only, which is not enough to claim the Outstanding item. Those levels are marked "No".
* **Discourse, Dev.to and the webhook connector are certified against mock servers only**, not real instances (no free local Discourse/Dev.to instance was set up). Dev.to has no delete endpoint, so `delete` unpublishes; this is a limit of the platform.
* **Exactly-once after a real timeout** depends on the platform honouring the idempotency header. Real Discourse/Lemmy/Dev.to/Ghost do not, so against them a timed-out publish stays in state `unknown` and needs human reconciliation. The mocks do honour it, which is how the tests pass.
* `refresh_auth` is declared by the contract but not implemented for any connector (returns 501), because the Core platforms use long-lived API keys.
* Frequency-cap time zones: all windows are computed in UTC; per-destination time zones are not modelled.
* **Not done yet (needs the submitter)**: live demo deployment, demo video, screenshots for the certification pack, a clean-machine run of `docker compose up`, commit history (nothing committed yet), private GitHub repository.

## 4. Measured numbers (`evidence/metrics.json`, `evidence/rule_safety.json`)
Hardware: Windows 11 laptop, Docker Desktop, Python 3.11. Simulation numbers are in-process (no network), so they only show the framework overhead.
| Metric | Value |
|---|---|
| Certification, simulation, mean per run (5 runs each) | discourse 0.04 s, lemmy 0.05 s, devto 0.04 s, ghost 0.03 s, webhook 0.03 s, assisted 0.003 s |
| Certification, real local instances | Ghost 1.2-2.6 s, Lemmy 1.6-2.9 s (several runs, all passed) |
| Destinations certified | simulation 6 connector types; real local instances 2 (Ghost, Lemmy) |
| Publish success rate, simulation | 100 / 100 |
| Duplicate posts under retry | 0 (60 submissions, 20 keys) |
| Rule safety (my 16-case set) | 7 caught, 0 missed, 0 false positives |
| Catalogue import, recommender precision | not measured (not built) |
| API / model cost per 1,000 publications | 0.00 USD (no paid service or model is called) |

## 5. What I would do next
1. CSV catalogue import with URL normalisation + de-duplication (the hard case still untested) and row-level error reports.
2. A real Discourse test container and a Dev.to sandbox so the remaining three Core connectors are certified against real software.
3. A reconciliation lookup for `unknown` posts (search the destination by a marker in the body) so timeouts are exactly-once on real platforms.
4. Recertification scheduler (re-run `certify` before 30 days, demote and alert on failure); the demotion path already exists.
5. Relevance scoring and tailoring as pure functions with golden tests, then the recommender and a labelled precision@10 set.
