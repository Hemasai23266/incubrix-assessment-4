# Evidence index

Maps every major claim in `REPORT.md`, the nine must-haves and the ten Integration Standard points to the files that back them.
Paths are relative to the repository root. `tests/...::name` is a pytest test. Every path and test name in this file was checked against the repository on 2026-11-23 (137 references, 0 missing).

**Honesty notes (read first)**
* Latest full test results (code commit `5084bb1`, 2026-11-23): host **403 passed** (`evidence/pytest_report.txt`), Docker **403 passed** (`evidence/docker_tests_summary.txt`). These two files are the raw output of those runs.
* Re-run on 2026-11-23 on commit `5084bb1` (code unchanged since; only documentation and evidence files differ): the 49-check demo-image suite, **49/49 passed** (`evidence/render-demo/07_build_commit_5084bb1.log`, `evidence/render-demo/08_test_run_commit_5084bb1.log`, `evidence/render-demo/test_results.json`), and `scripts/measure.py` (`evidence/metrics.json`). `evidence/rule_safety.json` is rewritten by `tests/test_rules_matrix.py::test_rule_safety_set` on every test run; it came out identical (7 caught, 0 missed, 0 false positives, 9 legitimate allowed).
* Kept as history, made on EARLIER code and NOT regenerated: the 68-test table in `evidence/docker/00_SUMMARY.md` and its numbered files; the compose-stack batch run `evidence/batch_run_docker/` (12/12, 2026-11-22, before the Idempotency-Key header became required everywhere); the real local-instance runs `evidence/local_instances/`, `evidence/local_instances_store/`, `evidence/certification/` and `evidence/local_store/` (2026-11-22); `evidence/render-demo/test_results_2026-11-22_earlier_code.json` and `00_build.log` to `06_*` (2026-11-22/23, earlier code); `evidence/metrics_2026-11-22_earlier_code.json`. They are true for the code they ran on.
* Not in the repository: the output of the deployed smoke test against the live demo (it needs the token and has not been re-run on `5084bb1`), and the unauthenticated live checks quoted in `REPORT.md` section 3 (typed commands, nothing saved).
* The Advanced and Outstanding levels are not built, so no evidence exists for them (`REPORT.md` section 3).

## 1. Must-haves (the brief lists nine)

| # | Must-have | Evidence |
|---|---|---|
| 1 | Exact connector contract | `tests/test_contract_and_standard.py::test_every_connector_has_a_valid_manifest`, `::test_every_connector_implements_all_contract_methods`, `::test_shared_error_codes_present`, `::test_contract_http_roundtrip`, `::test_contract_unknown_method_and_connector`; the HTTP route `POST /connector/v1/{key}/{method}` in `app/main.py`; `openapi.yaml`; batch run over all connectors: `evidence/batch_run_docker/index.json` (earlier code) |
| 2 | Six Core connectors certified | `evidence/certification/INDEX.md` (status table, how each run was made); per-connector run records under `evidence/certification/{discourse,lemmy,devto,ghost,webhook,assisted}/`; real local Ghost and Lemmy runs: `evidence/local_instances/summary.json`, `evidence/local_instances/certification_ghost.json`, `evidence/local_instances/certification_lemmy.json`; screenshots: `evidence/certification/screenshots_manifest.json`; clean-up check: `evidence/certification/local_instances_leftover_check.json`. LIMIT: Discourse, Dev.to and the webhook were certified against mock servers only |
| 3 | Configuration, not code | `tests/test_contract_and_standard.py::test_adding_another_forum_is_one_catalogue_row_no_code` (a second Discourse forum is one catalogue row, zero code) |
| 4 | Rules checked before every post | `evidence/rule_safety.json` (my 16-case labelled set: 7 caught, 0 missed, 0 false positives; NOT the hidden set); `tests/test_rules_matrix.py::test_rule_safety_set`, `::test_frequency_cap_window_expiry`; `tests/test_hard_cases.py::test_second_post_inside_weekly_cap_is_rescheduled`, `::test_promotional_post_to_no_promo_destination_blocked`, `::test_link_policy_forbidden_and_max`, `::test_ai_disclosure_required`; sample outputs `evidence/batch_run_docker/09_promo_blocked.result.json`, `evidence/batch_run_docker/10_ai_disclosure_missing.result.json` |
| 5 | Never spam, never cheat | test posts only go to mocks or local containers: `evidence/certification/INDEX.md` (section "Cleanup verification"), `evidence/certification/local_instances_leftover_check.json`, `evidence/docker/06_leftover_test_posts_check.txt`, `evidence/docker/15_credentials_and_cleanup_checks.txt`; 10-minute deletion limit and 429 back-off: `tests/test_hard_cases.py::test_failed_certification_leaves_no_test_post_behind`, `::test_429_short_reset_backs_off_and_retries`, `::test_429_long_reset_is_surfaced_with_retry_at`, `::test_stops_calling_after_five_consecutive_failures` |
| 6 | Safe retries | `evidence/hard_cases.md` (rows 1 and 1b); `tests/test_hard_cases.py::test_timeout_then_retry_same_key_creates_one_post`, `::test_same_key_twice_returns_same_job_and_one_post`, `::test_concurrent_requests_same_key_one_post`; `evidence/idempotency/postgres_concurrency.txt` (16 concurrent same-key requests on real PostgreSQL: one platform post); `evidence/metrics.json` (re-run on `5084bb1`; retry storm: 60 submissions, 20 keys, 20 posts). LIMIT: a real platform that ignores idempotency keys can leave a post in state `unknown` after a timeout (`REPORT.md` section 3) |
| 7 | Secrets protected, simulation mode | `tests/test_contract_and_standard.py::test_secrets_never_logged`, `::test_no_secrets_in_repository`; `tests/test_credentials.py`; `evidence/docker/15_credentials_and_cleanup_checks.txt`, `evidence/docker/27_post_rewrite_secret_scan.txt`, `evidence/docker/26_ghost_session_revocation.txt` (a local Ghost session cookie was exposed earlier, removed by rewriting history and revoked); simulation mode is the default and what every test and the public demo use |
| 8 | Local test instances (`docker compose up`) | `evidence/docker/00_SUMMARY.md` (checks 1, 7, 10-13 are about the compose stack, local Ghost and Lemmy), `evidence/docker/01_health_inspection.txt`, `evidence/docker/14_real_publish_status_delete_via_compose_api.txt`, `evidence/docker/20_clean_health.txt`, `evidence/docker/22_clean_real_publish_tests_creds.txt`. LIMIT: run on one machine plus a fresh-volume project, not on a separate clean machine |
| 9 | Integration Standard and live demo | section 3 below; demo image checks `evidence/render-demo/test_results.json` and `evidence/render-demo/08_test_run_commit_5084bb1.log` (local Docker, 49/49 on commit `5084bb1`; earlier run `evidence/render-demo/04_final_verification.txt`); the live demo is `https://incubrix-longtail-demo-01m4.onrender.com`; `scripts/smoke_test_deployed.py` is the check to run against it (its output is not stored here). Staying up for the whole review period is the submitter's commitment, not something the repository can prove |

## 2. Claims in `REPORT.md`

| REPORT.md claim | Evidence |
|---|---|
| One connector per platform type; one catalogue row per destination (section 1) | `tests/test_contract_and_standard.py::test_adding_another_forum_is_one_catalogue_row_no_code` |
| Mock platforms behind the same HTTP code path; fault injection (section 1) | `app/mocks/platforms.py`; `tests/test_recovery_and_sqlite.py::test_fault_switches_are_shared_between_processes`; `tests/test_hard_cases.py` |
| Idempotency in three layers (section 1) | `evidence/idempotency/post_route_audit.txt`, `evidence/idempotency/postgres_concurrency.txt`; `tests/test_idempotency_routes.py`; `tests/test_hard_cases.py::test_same_key_different_request_rejected` |
| Rules engine, 7/7 caught, 0 missed (section 2) | `evidence/rule_safety.json`; `tests/test_rules_matrix.py::test_rule_safety_set` |
| Six Core connectors certify in simulation, 5 runs each, 5/5 (section 2) | `evidence/metrics.json` (key `certification`, commit `5084bb1`; earlier run `evidence/metrics_2026-11-22_earlier_code.json`); `evidence/certification/INDEX.md` |
| Ghost and Lemmy certified on real local instances, 1.2-2.9 s (sections 2 and 4) | `evidence/local_instances/summary.json`; `evidence/certification/INDEX.md` |
| 100/100 simulated publishes; retry storm gives 0 duplicates (sections 2 and 4) | `evidence/metrics.json` (key `reliability`) |
| Every hard case except the CSV one has a test (section 2) | `evidence/hard_cases.md`; `tests/test_hard_cases.py`; sample inputs/outputs in `samples/` and `evidence/batch_run_docker/` |
| Batch runner passed 12/12 against the compose stack (section 2; run on earlier code) | `evidence/batch_run_docker/index.json`; `cli/batch_runner.py`; `tests/test_openapi_and_batch.py::test_batch_runner_runs_samples_in_process` |
| 403 tests, all offline, all pass (section 2) | `evidence/pytest_report.txt` (host), `evidence/docker_tests_summary.txt` (Docker) |
| SSRF matrix, DNS rebinding and real-TLS tests (section 2) | `tests/test_netguard.py`, `tests/test_fetcher.py` |
| Live-mode `base_url` is not SSRF-guarded (section 3) | stated limit; documented in `README.md`; no test can prove an absence |
| Licensing questions (section 3) | `SOURCES.md` section 5 |
| Advanced and Outstanding not built (section 3) | none: nothing was built |
| Discourse, Dev.to and webhook certified on mocks only (section 3) | `evidence/certification/INDEX.md` |
| Free single-container demo, deployed on Render, 49/49 checks in local Docker (sections 3 and 3b) | `evidence/render-demo/08_test_run_commit_5084bb1.log`, `evidence/render-demo/test_results.json`; `docs/RENDER_DEPLOY.md`. The deployed instance itself: see the note on unsaved live checks above |
| Measured numbers (section 4) | `evidence/metrics.json`, `evidence/rule_safety.json` |
| Cost 0.00 USD per 1,000 publications (section 4) | no paid service or model is called: the manifest field `cost_estimate_usd` in `tests/test_manifests.py` |

## 3. Integration Standard (ten points)

| # | Point | Evidence |
|---|---|---|
| 1 | One-command start and test | `docker-compose.yml`, `.env.example`; `evidence/docker_tests_summary.txt` (all tests in Docker); `tests/test_env_example.py::test_every_settings_field_is_listed_in_env_example`, `::test_every_environment_variable_read_directly_in_app_is_listed`; `evidence/docker/00_SUMMARY.md` |
| 2 | Documented HTTP API | `openapi.yaml`; `tests/test_openapi_and_batch.py::test_openapi_yaml_matches_running_app`; `tests/test_idempotency_routes.py::test_openapi_marks_the_header_required_on_every_post_operation_and_all_posts_reject_a_missing_header` |
| 3 | Background jobs | `tests/test_contract_and_standard.py::test_job_lifecycle_fields`, `::test_callback_is_signed_with_hmac`; `tests/test_netguard.py` (callback URL safety) |
| 4 | Safe retries (Idempotency-Key on every POST) | `evidence/idempotency/post_route_audit.txt` (12 of 12 POST routes require the header); `evidence/idempotency/postgres_concurrency.txt`; `tests/test_idempotency_routes.py::test_missing_key_is_rejected_on_every_post_route`, `::test_concurrent_identical_requests_run_the_work_exactly_once` |
| 5 | Clear errors | `app/errors.py`; `README.md` (error table); `tests/test_error_envelope.py`; `tests/test_contract_and_standard.py::test_error_envelope_shape` |
| 6 | Configuration | `app/config.py`, `.env.example`; `tests/test_env_example.py`. LIMIT: a few small internal constants remain in code |
| 7 | Files in, files out | `app/storage.py`, `app/fetcher.py`; `tests/test_fetcher.py`; `tests/test_contract_and_standard.py::test_file_upload_and_download`. LIMITS: a media URL inside a publish rendition stays a reference; live-mode `base_url` not guarded |
| 8 | Logs and health | `tests/test_contract_and_standard.py::test_logs_are_json_with_required_fields`, `::test_correlation_id_echoed`, `::test_health_ready_and_routes_versioned`; `app/logging_setup.py` |
| 9 | Output manifest | `app/runtime.py` (`make_manifest`); `tests/test_manifests.py`; `tests/test_contract_and_standard.py::test_output_manifest_contents`. LIMIT: job results only, not every synchronous response |
| 10 | Batch runner | `cli/batch_runner.py`; `evidence/batch_run_docker/index.json` (12/12, earlier code); `samples/`; `tests/test_openapi_and_batch.py::test_batch_runner_runs_samples_in_process` |
