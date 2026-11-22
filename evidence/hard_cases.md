# Hard cases: what the system does, and the test that proves it

Run all of them: `pytest -q tests/test_hard_cases.py` (offline, no API keys). Raw result: `evidence/pytest_report.txt`.

| # | Situation | What the system does | Test (tests/test_hard_cases.py) |
|---|---|---|---|
| 1 | Publish times out, retried with the same idempotency key | The platform had already created the post (mock `[[drop]]`). Job 1 fails `PLATFORM_UNAVAILABLE`, `retryable: true`, `details.state = "unknown"`, `retry_with_same_idempotency_key: true`; the `posts` row is kept in state `unknown`. Retrying with the same key re-runs the **same job id**; the key is also sent to the platform which returns the existing post. Result: 1 post on the platform, 1 row in `posts`. | `test_timeout_then_retry_same_key_creates_one_post` |
| 1b | Same key twice / concurrently / different body | Same job returned (`idempotent_replay: true`); 6 concurrent requests produce 1 job and 1 post; different body with the same key -> `422 IDEMPOTENCY_KEY_REUSED`. | `test_same_key_twice_...`, `test_concurrent_requests_same_key_one_post`, `test_same_key_different_request_rejected` |
| 2a | 429 with a reset time | Reset <= `MAX_INLINE_BACKOFF_SECONDS`: sleeps the reset time once, retries, success, manifest warning "backed off". Longer reset: job fails `PLATFORM_RATE_LIMIT`, `retryable`, `details.retry_after_seconds=120`, `details.retry_at`. Daily scope -> `PLATFORM_QUOTA_EXHAUSTED`. | `test_429_short_reset_backs_off_and_retries`, `test_429_long_reset_is_surfaced_with_retry_at`, `test_daily_quota_exhausted` |
| 2b | Expired token | `CONNECTION_AUTH_EXPIRED`, `retryable:false`; also when the credential env var itself holds an expired token. | `test_expired_token`, `test_expired_token_via_credentials_env` |
| 2c | Content rejected by platform | `PLATFORM_CONTENT_REJECTED`; the platform's message is returned in `details.platform_message`. | `test_content_rejected_returns_platform_message` |
| 3a | Title / body longer than allowed | `DEST_SPEC_VIOLATION` (no retry) with field-level reasons (`field: title`, "title is 250 chars, limit 200"); nothing reaches the platform. | `test_title_too_long`, `test_body_too_long` |
| 3b | Media the destination does not accept | `DEST_SPEC_VIOLATION`, `field: media[0]`, lists the allowed types. | `test_unsupported_media_type` |
| 4a | Second post inside the frequency cap | Blocked before posting; job `failed` with `DEST_RULE_FREQUENCY_CAP` (`retryable`), result `status: rescheduled` and `reschedule_at` = when the oldest counted post leaves the window. | `test_second_post_inside_weekly_cap_is_rescheduled` |
| 4b | Promotional post to a no-promotion community | Blocked: `DEST_RULE_VIOLATION`, result `status: blocked`, message names the rule. Also link policy (`forbidden`, `max:N`) and AI disclosure. | `test_promotional_post_to_no_promo_destination_blocked`, `test_link_policy_forbidden_and_max`, `test_ai_disclosure_required` |
| 5 | Platform renames an API field | Certification fails with `CONTRACT_VIOLATION` naming the missing field; the destination is **demoted** (`status: demoted`, `certified: false`, `level: L0`); later publishes are blocked `DEST_NOT_CERTIFIED`. A half-created test post is cleaned up. Tested for discourse, devto, ghost, lemmy, webhook. | `test_renamed_field_fails_certification_and_demotes[*]`, `test_failed_certification_leaves_no_test_post_behind` |
| 6a | CSV with duplicates, bad URLs, mixed languages | **Not implemented** (Advanced level not built). Marked "No" in the form. | - |
| 6b | Partner revokes permission | `POST /destinations/{id}/revoke` blocks all posting (`DEST_PERMISSION_REVOKED`); a platform-side 403 maps to `CONNECTION_AUTH_EXPIRED`. | `test_partner_revokes_permission`, `test_partner_revokes_at_platform_side` |
| 7a | Non-English community | Hindi title/body published unchanged, UTF-8 preserved. | `test_non_english_community` |
| 7b | Destination requires AI disclosure | AI-generated content without `rendition.disclosure` is blocked with the exact line to add; with the disclosure it posts; human-written content is unaffected. | `test_ai_disclosure_required` |
| extra | Kill switch; platform down | `CONNECTOR_DISABLED`; after 5 consecutive `PLATFORM_UNAVAILABLE` the service stops calling the platform (verified: the 6th call never reaches it). | `test_connector_kill_switch`, `test_stops_calling_after_five_consecutive_failures` |

Limits worth knowing: the exactly-once guarantee after a timeout relies on the post-row plus the platform honouring the
`Idempotency-Key` (the mock does; real Discourse/Lemmy/Dev.to/Ghost do not). Against a real platform a timed-out publish stays in
state `unknown` and a human must reconcile it. This is stated in REPORT.md.
