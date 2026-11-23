> **HISTORICAL (kept unchanged below).** The table, including the "68 tests" figure, records the Docker verification done on EARLIER code. It is not the latest result. Latest verified results (code commit 5084bb1, 2026-11-23): host **403 passed** in `evidence/pytest_report.txt`, Docker **403 passed** in `evidence/docker_tests_summary.txt`. Index of all evidence: `evidence/INDEX.md`.

# Docker verification (updated after changes 1-4 and Option A). Statuses = what was actually executed.

| # | Check | Status | Evidence |
|---|---|---|---|
| 1 | Original stack: all services up/healthy, /health /ready, Redis, Postgres, S3, Ghost, Lemmy | PASS | 01_health_inspection.txt |
| 2 | Tests: 68 passed on host and 68 in Docker (`--profile test`, offline) | PASS | 16_*.txt |
| 3 | Root cause of non-repeatable seed (Ghost device-verification email -> 500) | DIAGNOSED | 04_*.txt |
| 4 | Fix on isolated throw-away Ghost (3 runs) | PASS | 08_*.txt |
| 5 | Changes applied: env_file order, Ghost dev-only setting, credentials volume (read-only in api/worker), repeatable seed, docker-compose.real.yml | DONE | git diff |
| 6 | Seed x3 on the original stack (real override): 2nd/3rd reuse the key, 2 local destinations, no duplicates | PASS | 12_*.txt, 13_*.txt |
| 7 | Real publish -> status -> delete -> verify via the ACTUAL compose API + RQ worker, local Ghost and Lemmy | PASS | 14_*.txt/json |
| 8 | Test posts deleted (Ghost only 'Coming soon', Lemmy none) | PASS | 15_*.txt |
| 9 | Credentials: files 0600, mounts read-only, key secret absent from logs, API responses, evidence, git, container env | PASS | 15_*.txt, 22_*.txt |
| 10 | Option A: stop original (no -v) -> `inn-clean` project, fresh volumes, other ports | PASS | 18-20_*.txt |
| 11 | Clean start: all 10 services up, healthy, 0 Lemmy restarts, Ghost+Lemmy fresh | PASS | 20_clean_health.txt |
| 12 | Clean seed x4 (2 default/simulation, 2 real): repeatable, destinations registered once | PASS | 21_*.txt |
| 13 | Clean project: real publish/status/delete on Ghost+Lemmy, 68 tests in Docker, creds protected | PASS | 22_*.txt |
| 14 | Only `inn-clean` removed; original volumes intact; original restarted; data snapshots identical | PASS | 23_*.txt, 24/25_*.txt |
| 15 | Run on a DIFFERENT (never-used) machine | NOT RUN | - |

Incident (disclosed): the first seed run used a stale image and printed three throw-away Ghost admin keys into a scratch evidence file.
The file was deleted before any commit, the three integrations were revoked in Ghost, and a pattern scan of evidence/ and git found no keys.
