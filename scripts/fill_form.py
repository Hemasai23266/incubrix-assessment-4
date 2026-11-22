"""Fill a DRAFT copy of the Submission Form with what is actually verified. Personal details, URLs and the
declaration are left blank for the submitter. Output: Assessment4_DRAFT.xlsx (gitignored)."""
import pathlib

import openpyxl

ROOT = pathlib.Path(__file__).resolve().parents[1]
src = next(ROOT.glob("*Submission_Form.xlsx"))
wb = openpyxl.load_workbook(src)

T = "tests/test_hard_cases.py"
MUST = {  # row: (Met?, evidence, notes)
    6: ("Yes", "tests/test_contract_and_standard.py (contract, manifests, error codes); app/connectors/; app/connectors/manifests/*.yaml",
        "Appendix A methods over POST /connector/v1/{key}/{method}. refresh_auth is declared but returns 501 (all Core platforms use long-lived keys)."),
    7: ("Partial", "evidence/local_instances/summary.json (real Ghost + Lemmy); evidence/metrics.json (all six in simulation); tests/test_smoke.py",
        "Ghost and Lemmy certified against REAL local instances. Discourse, Dev.to and webhook were certified against mock servers only (no real instance set up); assisted is a pack, not a post."),
    8: ("Yes", "tests/test_contract_and_standard.py::test_adding_another_forum_is_one_catalogue_row_no_code; README.md 'API'",
        "Second Discourse forum = one POST /api/v1/destinations row; test certifies it, connector count unchanged."),
    9: ("Yes", "app/rules.py; tests/test_rules_matrix.py; evidence/rule_safety.json; " + T,
        "Frequency cap, self-promo, link policy, AI disclosure run before every post; blocked or rescheduled with a reason. My own 16-case set: 7 caught, 0 missed, 0 false positives."),
    10: ("Yes", "evidence/local_instances/ (test posts deleted in ~1-3 s); evidence/hard_cases.md; app/runtime.py certify()",
         "Test posts only to local containers or in-process mocks, deleted inside certification (limit 600 s, cleanup endpoint as safety net). 429 backs off using Retry-After / reset time. No scraping, no real accounts."),
    11: ("Yes", T + "::test_timeout_then_retry_same_key_creates_one_post, ::test_concurrent_requests_same_key_one_post; evidence/metrics.json",
         "0 duplicates in a 60-submission retry storm. Caveat: after a real timeout the platform must honour the idempotency header, otherwise the post stays 'unknown' (REPORT.md section 3)."),
    12: ("Yes", "tests/test_contract_and_standard.py::test_secrets_never_logged, ::test_no_secrets_in_repository; app/logging_setup.py; .env.example",
         "Credentials are env var NAMES in the destination row; logs redact tokens; SIMULATION_MODE=true runs everything against mock platforms."),
    13: ("Partial", "docker-compose.yml; evidence/batch_run_docker/index.json; scripts/seed_local.py; scripts/certify_local.py",
         "docker compose up starts api, worker, Postgres, Redis, S3, mocks, Ghost and Lemmy and passed here on the dev machine. NOT yet run on a clean machine; real Ghost/Lemmy posts need the one-time seed step (docker compose --profile seed run --rm seed)."),
    14: ("No", "(Integration Standard points: see sheet 4)",
         "Live demo is NOT deployed yet, so this cannot be claimed. Integration Standard is mostly met (sheet 4)."),
}
LEVELS = {
    5: ("Partial", "see sheet 2", "Must-haves 2, 8 are Partial and 9 (live demo) is No."),
    6: ("Partial", "evidence/local_instances/*.json; evidence/metrics.json", "Full run (publish, live URL, status, delete, verify) passes for Ghost and Lemmy on real local instances and for all six on mocks; Discourse, Dev.to, webhook not on real instances."),
    7: ("Yes", T + "::test_expired_token, ::test_429_*, ::test_content_rejected_*, ::test_daily_quota_exhausted", "Expired token -> CONNECTION_AUTH_EXPIRED; 429 -> PLATFORM_RATE_LIMIT (or QUOTA_EXHAUSTED); rejected -> PLATFORM_CONTENT_REJECTED with the platform message; 5xx -> PLATFORM_UNAVAILABLE."),
}
for r in range(8, 17):
    LEVELS[r] = ("No", "", "Not built (see REPORT.md section 3).")
INTEG = {
    5: ("Yes", "docker-compose.yml; .env.example; README.md; `docker compose --profile test run --rm tests` -> evidence/docker_tests_summary.txt (162 passed, no API keys)"),
    6: ("Yes", "openapi.yaml (3.1, generated); tests/test_openapi_and_batch.py fails if stale; all routes under /api/v1/ or /connector/v1/"),
    7: ("Yes", "app/jobs.py; tests/test_contract_and_standard.py::test_job_lifecycle_fields, ::test_callback_is_signed_with_hmac"),
    8: ("Yes", T + "::test_same_key_twice_returns_same_job_and_one_post, ::test_same_key_different_request_rejected"),
    9: ("Yes", "app/errors.py; README.md error table; tests/test_contract_and_standard.py::test_error_envelope_shape"),
    10: ("Yes", "app/config.py; .env.example (every setting)"),
    11: ("Partial", "app/storage.py; POST /api/v1/files, GET /api/v1/files/{key}; tests::test_file_upload_and_download. Note: file upload is supported; URL inputs are accepted only as media references (not downloaded)."),
    12: ("Yes", "app/logging_setup.py; tests/test_contract_and_standard.py::test_logs_are_json_with_required_fields; GET /health, GET /ready"),
    13: ("Yes", "every publish/certify result carries `manifest` (tests::test_output_manifest_contents)"),
    14: ("Yes", "cli/batch_runner.py; samples/ (12 inputs); evidence/batch_run_docker/index.json (12/12)"),
}
METRICS = {
    5: ("6 connector types certified in simulation; 2 on real local instances (Ghost, Lemmy)", "Sim: 5 runs per connector, mean 0.003-0.053 s (in-process). Real Ghost 1.2-2.6 s, Lemmy 1.6-2.9 s. Windows 11 laptop, Docker Desktop.", "evidence/metrics.json; evidence/local_instances/"),
    6: ("Simulation 100/100 publishes succeeded; 0 duplicates in 60 submissions over 20 keys", "scripts/measure.py, inline jobs, in-process mocks", "evidence/metrics.json"),
    7: ("7 violations caught, 0 missed, 0 false positives, 9 legitimate allowed", "MY 16-case labelled set (not IncuBrix's)", "evidence/rule_safety.json; tests/test_rules_matrix.py"),
    8: ("Not measured (Advanced not built)", "", ""),
    9: ("Not measured (Outstanding not built)", "", ""),
    10: ("0.00 USD per 1,000 publications", "No paid API or model is called", "SOURCES.md"),
}
HARD = {
    5: ("Yes", "Retry with the same key returns the SAME job; platform had already created the post, the retry reconciles it: 1 post, 1 row.", T + "::test_timeout_then_retry_same_key_creates_one_post"),
    6: ("Yes", "429 short reset: sleeps the reset time and retries once (manifest warning); long reset: PLATFORM_RATE_LIMIT with retry_at; expired token: CONNECTION_AUTH_EXPIRED; rejected: PLATFORM_CONTENT_REJECTED + platform message.", T + " (test_429_*, test_expired_token*, test_content_rejected_*)"),
    7: ("Yes", "DEST_SPEC_VIOLATION with field-level reasons (title/body length, media type); nothing sent to the platform.", T + "::test_title_too_long, ::test_body_too_long, ::test_unsupported_media_type"),
    8: ("Yes", "Cap: job fails DEST_RULE_FREQUENCY_CAP, result 'rescheduled' + reschedule_at. No-promo: blocked DEST_RULE_VIOLATION with the reason.", T + "::test_second_post_inside_weekly_cap_is_rescheduled, ::test_promotional_post_to_no_promo_destination_blocked"),
    9: ("Yes", "Certification fails CONTRACT_VIOLATION naming the missing field; destination demoted and later publishes blocked DEST_NOT_CERTIFIED; half-created test post cleaned up. Tested on 5 connectors.", T + "::test_renamed_field_fails_certification_and_demotes"),
    10: ("Partial", "Partner revocation: handled (revoke endpoint + platform-side 403). CSV with duplicates/bad URLs/mixed languages: NOT implemented.", T + "::test_partner_revokes_permission"),
    11: ("Yes", "Non-English (Hindi) content published unchanged. AI disclosure required: blocked with the exact line to add; with disclosure it posts.", T + "::test_non_english_community, ::test_ai_disclosure_required"),
}

ws = wb["2_Must_haves"]
for r, (a, b, c) in MUST.items():
    ws[f"C{r}"], ws[f"D{r}"], ws[f"E{r}"] = a, b, c
ws = wb["3_Levels"]
for r, (a, b, c) in LEVELS.items():
    ws[f"C{r}"], ws[f"D{r}"], ws[f"E{r}"] = a, b, c
ws = wb["4_Integration"]
for r, (a, b) in INTEG.items():
    ws[f"C{r}"], ws[f"D{r}"] = a, b
ws = wb["5_Metrics"]
for r, (a, b, c) in METRICS.items():
    ws[f"C{r}"], ws[f"D{r}"], ws[f"E{r}"] = a, b, c
ws = wb["6_Hard_cases"]
for r, (a, b, c) in HARD.items():
    ws[f"C{r}"], ws[f"D{r}"], ws[f"E{r}"] = a, b, c
ws = wb["1_Your_Details"]
ws["B15"] = "Windows 11 laptop + Docker Desktop (CPU only, no GPU)"
ws["B16"] = "None. 0.00 USD."
ws["B17"] = "Claude Code (Claude Sonnet 5.5) for scaffolding, code, tests, docs - see AI_USE.md"
wb.save(ROOT / "Assessment4_DRAFT.xlsx")
print("saved Assessment4_DRAFT.xlsx")
