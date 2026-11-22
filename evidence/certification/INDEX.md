# Must-have #2: certification evidence index

Generated 2026-11-22T18:30:43.670582+00:00 from the files below by `scripts/package_certification.py --mode index` (code commit `c54ab4f`). Every status in this file is read from the stored run records, not typed by hand.

> **Commit hashes:** run records made before 2026-11-23 cite code commit `4c8b43d`. That commit was rewritten to remove an exposed local session cookie and is now `c54ab4f`; the code is identical, only `evidence/docker/04_ghost_seed_repeat_diagnosis.txt` differs (see `evidence/docker/26_ghost_session_revocation.txt`).

## What was tested and how

* **Harness:** `POST /api/v1/destinations/{id}/certify` (job). Steps: validate -> publish a test post -> `get_status` confirms it is live -> `delete` -> `get_status` confirms it is gone. Evidence JSON is stored by the harness; a failed run demotes the destination.
* **MOCK certification:** the connector's real HTTP code runs against an in-process mock of the platform (`app/mocks/platforms.py`). It proves the connector, error mapping and harness, **not** that the real platform behaves identically.
* **LOCAL-INSTANCE certification:** same harness against real Ghost 5 and Lemmy 0.19 containers started by `docker compose` on this machine.
* **Assisted:** no platform call; the evidence is the generated pack and 9 automated checks.
* **Related earlier evidence:** `evidence/docker/14_*` (publish -> status -> delete through the running compose API and RQ worker on local Ghost and Lemmy) and `evidence/docker/22_*` (same on a fresh `inn-clean` stack).
* All test posts go only to the mock servers or the local containers and are deleted within seconds (limit 600 s).

## Status of the six Core connectors

| Connector | Mock certification (assisted: assisted flow) | Local-instance certification | Screenshots | Files |
|---|---|---|---|---|
| discourse | PASS (0.0527 s) | not run | - | `evidence/certification/discourse/` |
| lemmy | PASS (0.0318 s) | PASS (1.4212 s) | 2 | `evidence/certification/lemmy/` |
| devto | PASS (0.0242 s) | not run | - | `evidence/certification/devto/` |
| ghost | PASS (0.0237 s) | PASS (1.7087 s) | 2 | `evidence/certification/ghost/` |
| webhook | PASS (0.0233 s) | not run | - | `evidence/certification/webhook/` |
| assisted | PASS (0.0083 s) — assisted flow, no platform contacted | n/a (no platform is called) | - | `evidence/certification/assisted/` |

Assisted pack validation: PASS (9/9 checks)

## Cleanup verification

* Local instances after the run: Ghost posts ['Coming soon']; Lemmy live posts [].
* Mock platforms: live records per instance {'discourse': 0, 'discourse-b': 0, 'lemmy': 0, 'devto': 0, 'ghost': 0, 'partner-site': 0, 'partner-site-b': 0}.

## Screenshots

Captured automatically with headless Microsoft Edge by `scripts/capture_screenshots.py` (2026-11-22T18:25:04.872000+00:00), see `screenshots_manifest.json` (sha256 per file).

* `ghost\local-instance\screenshots\ghost_01_live_182453.png`: Ghost post as published on the local Ghost site (public page)
* `ghost\local-instance\screenshots\ghost_02_after_delete_182459.png`: the same Ghost URL after deletion (Ghost's not-found page)
* `lemmy\local-instance\screenshots\lemmy_01_live_182501.png`: Lemmy public API response for the post while live (no lemmy-ui in our compose; this is the API JSON)
* `lemmy\local-instance\screenshots\lemmy_02_after_delete_182503.png`: the same Lemmy API URL after deletion (anonymous request: post no longer retrievable)

## What remains unverified (honest list)

* **discourse**: certified against the MOCK only (no real discourse software/account was used; the brief supplies local Lemmy and Ghost and mocks for the rest).
* **devto**: certified against the MOCK only (no real devto software/account was used; the brief supplies local Lemmy and Ghost and mocks for the rest).
* **webhook**: certified against the MOCK only (no real webhook software/account was used; the brief supplies local Lemmy and Ghost and mocks for the rest).
* Dev.to has no delete endpoint: the evidence proves **unpublish** against the mock, not against dev.to.
* Discourse, Dev.to and webhook were never run against real software; passing the mock proves the connector logic, not platform compatibility.
* Lemmy has no web UI in our compose (no `lemmy-ui`): its screenshots show the public API JSON, not a rendered Lemmy page.
* Runs were made on one Windows machine with cached images; no clean second machine.
* Mock evidence shows `live_url` values such as `http://mock/...` or `https://dev.example/...`; these are mock URLs that do not exist on the internet.

