# devto: certification run (mock)

* **Result: PASS**  (job status `succeeded`)
* **Environment:** MOCK (in-process mock platform, simulation mode)
* **Connector:** `devto` v1.0.0, tier `native`
* **Started (UTC):** 2026-11-22T18:24:21.492756+00:00  |  **Harness duration:** 0.0242 s
* **Destination after run:** certified=True, level=L3, status=active
* **Code commit:** `4c8b43d`

## Steps

| # | step | result | seconds | what it proves |
|---|---|---|---|---|
| 1 | validate | PASS | 0.0 | content passes the connector's format checks |
| 2 | publish | PASS | 0.0045 | a real test post was created (platform id + URL returned) |
| 3 | get_status_live | PASS | 0.0026 | the post is confirmed live and its status read back |
| 4 | delete | PASS | 0.003 | the test post was deleted (Dev.to: unpublished) |
| 5 | verify_deleted | PASS | 0.002 | the platform confirms the post is gone / unpublished / deleted |

## Dev.to delete behaviour (important)
Dev.to's public API has **no DELETE endpoint for articles**. The connector's `delete()` sends `PUT /api/articles/{id}` with `{"article": {"published": false}}`, i.e. it **unpublishes** the article; it is not removed from the author's drafts. The `verify_deleted` step therefore confirms `status == "unpublished"`, not that the article is gone. This was exercised against the **mock** Dev.to only; it has not been run against dev.to itself.
