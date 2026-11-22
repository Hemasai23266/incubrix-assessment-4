# lemmy: certification run (local-instance)

* **Result: PASS**  (job status `succeeded`)
* **Environment:** LOCAL INSTANCE (real software in docker compose, simulation off)
* **Connector:** `lemmy` v1.0.0, tier `native`
* **Started (UTC):** 2026-11-22T18:24:28.326761+00:00  |  **Harness duration:** 1.4212 s
* **Destination after run:** certified=True, level=L3, status=active
* **Code commit:** `4c8b43d`

## Steps

| # | step | result | seconds | what it proves |
|---|---|---|---|---|
| 1 | validate | PASS | 0.0 | content passes the connector's format checks |
| 2 | publish | PASS | 0.6521 | a real test post was created (platform id + URL returned) |
| 3 | get_status_live | PASS | 0.228 | the post is confirmed live and its status read back |
| 4 | delete | PASS | 0.2339 | the test post was deleted (Dev.to: unpublished) |
| 5 | verify_deleted | PASS | 0.2951 | the platform confirms the post is gone / unpublished / deleted |
