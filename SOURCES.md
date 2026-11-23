# SOURCES

Every third-party component the project uses, with the **version and licence actually found** on 2026-11-23.

**How this was verified** (nothing below is copied from memory or from an earlier version of this file):
* Python packages: `importlib.metadata` of the installed environment (the `License` / `License-Expression` / classifier fields). The Docker
  images install the same pinned `requirements.txt`; the Linux-only extras were read inside the demo image.
* Container images: image labels (`docker image inspect`), the version command inside each image, and, for the Debian packages in the demo
  image, each package's `/usr/share/doc/<package>/copyright` file.
* Redis licensing: redis.io's licence pages and the Debian copyright file shipped with the package (see sections 2, 3 and 5).
* Where a source did not state a licence, the row says **not verified** instead of guessing.

No AI model, dataset, media file, voice or face is used. Cost per 1,000 publications: **0.00 USD** (no paid API or model).

## 1. Python libraries (`requirements.txt`, pinned; versions as installed)
| Library | Version | Licence (from package metadata) | Use |
|---|---|---|---|
| FastAPI | 0.115.6 | MIT (classifier) | HTTP API, OpenAPI 3.1 |
| Starlette | 0.41.3 | BSD-3-Clause | ASGI; `TestClient` runs the mock platforms in-process |
| Uvicorn | 0.34.0 | BSD-3-Clause | server |
| SQLAlchemy | 2.0.36 | MIT | database models |
| psycopg / psycopg-binary | 3.2.3 | **LGPL-3.0** (see section 5) | PostgreSQL driver |
| Pydantic / pydantic-core / pydantic-settings | 2.10.4 / 2.27.2 / 2.7.0 | MIT | validation, settings |
| httpx / httpcore / h11 | 0.28.1 / 1.0.9 / 0.16.0 | BSD-3-Clause / BSD-3-Clause / MIT | outbound HTTP (callbacks, URL fetching, platforms) |
| PyYAML | 6.0.2 | MIT | connector manifests |
| boto3 / botocore / s3transfer / jmespath | 1.35.90 / 1.35.99 / 0.10.4 / 1.1.0 | Apache-2.0 / Apache-2.0 / Apache-2.0 / MIT | S3-compatible storage client |
| RQ | 2.0.0 | BSD-2-Clause | background jobs |
| redis-py | 5.2.1 | MIT | Redis client (a client library; the Redis server is separate, see sections 2 and 3) |
| python-multipart | 0.0.20 | Apache-2.0 | file uploads |
| anyio, click, idna, annotated-types, greenlet, typing-extensions, python-dateutil, six, urllib3, tzdata, iniconfig, packaging, pluggy | various | MIT, BSD-3-Clause, BSD-3-Clause, MIT, MIT AND PSF-2.0, PSF-2.0, BSD and Apache dual, MIT, MIT, Apache-2.0, MIT, Apache-2.0 OR BSD-2-Clause, MIT | transitive dependencies |
| **certifi** | 2026.11.20 | **MPL-2.0** (file-level copyleft; used unmodified, see section 5) | CA bundle for TLS |
| uvloop (Linux image only) | 0.23.0 | MIT (metadata also lists Apache-2.0) | faster event loop (`uvicorn[standard]`) |
| httptools, watchfiles, websockets, python-dotenv | 0.9.0, 1.3.0, 17.4, 1.2.4 | MIT, MIT, BSD-3-Clause, BSD-3-Clause | `uvicorn[standard]` extras |
| pytest | 8.3.4 | MIT | tests (`requirements-dev.txt`) |

Metadata for FastAPI, colorama and setuptools states the licence only as a classifier; the texts of transitive dependencies were not
reviewed one by one.

## 2. Services started by `docker compose up` (separate containers, local throw-away credentials)
| Component (image) | Version found | Licence and where it was read | Role |
|---|---|---|---|
| PostgreSQL (`postgres:16-alpine`) | 16.16 | PostgreSQL Licence (upstream project; the image has no licence label: **not verified from the image**) | database |
| **Redis (`redis:7-alpine`)** | **7.4.12** | **RSALv2 / SSPLv1 (dual), NOT BSD-3.** Redis 7.2 and older were BSD-3; the change started with 7.4 (redis.io licence pages). Neither licence is OSI-approved. The image has no licence label. | job queue |
| SeaweedFS (`chrislusf/seaweedfs:latest`) | 4.54 | **Apache-2.0** (image label `org.opencontainers.image.licenses`) | S3-compatible storage. The `latest` tag is **not pinned**. MinIO images are no longer published on Docker Hub, so MinIO is not used. |
| Ghost (`ghost:5-alpine`) | 5.130.6 | MIT (`license` field of Ghost's own package.json inside the image) | local test instance for the Ghost connector |
| **Lemmy (`dessalines/lemmy:0.19.5`)** | 0.19.5 | **AGPL-3.0-or-later** (image label) | local test instance for the Lemmy connector (required by the brief) |
| **pict-rs (`asonix/pictrs:0.5`)** | 0.5 | the image label is empty; upstream pict-rs is believed to be **AGPL-3.0**, **not verified here** | Lemmy's image backend |
| Mock platforms (`app/mocks/platforms.py`) | n/a | this project's own code | simulated Discourse, Lemmy, Dev.to, Ghost, partner site |

## 3. Single-container demo image (`deploy/render/Dockerfile.render`)
Base: `python:3.11-slim` (Debian 13.7, Python 3.11.18; PSF licence for Python, Debian free-software licences for the packages).

| Debian package | Version | Licence (from the package's copyright file) | Role |
|---|---|---|---|
| **redis-server** | **5:8.0.2-3+deb13u3 (Redis 8.0.2)** | **a choice of (a) RSALv2, (b) SSPLv1 or (c) AGPLv3**, plus BSD-3, BSD-2 and MIT parts. This replaces the earlier statement "7.0, BSD-3-Clause", which was wrong. | queue inside the demo container |
| supervisor | 4.2.5-3 | BSD-derived; `supervisor/http_client.py` is under the RDFLIB licence; Debian packaging files GPL-2+ | process manager |

SQLite (Python's built-in `sqlite3`) is in the public domain.

## 4. Platform API references (documentation only, no code copied)
Discourse API docs, Lemmy API v3, Forem/Dev.to API v1, Ghost Admin API (JWT authentication).

## 5. Unresolved licensing and distribution questions: FOR THE OWNER'S REVIEW
Nothing below has been changed in the code or the compose file; these are open questions, not conclusions.
1. **Redis server in `docker-compose.yml` (7.4.12) is RSALv2/SSPLv1.** Using it as an internal job queue is allowed by RSALv2 (what it
   restricts is offering Redis itself as a managed service), but it is **not OSI open source**, and it conflicts with the brief's rule that
   everything in the default path be licensed for commercial use if that rule is read strictly. Options: pin Redis 7.2 or older (BSD-3), or
   use a BSD-3 fork such as Valkey (checked on 2026-11-23: image `valkey/valkey:8-alpine` runs Valkey 8.1.12, and Debian's `valkey-server` 8.1.1 copyright file
   names BSD-3-Clause; **not yet tested with this project**, so the compose file still uses Redis).
2. **Redis 8.0.2 in the demo image** is offered under RSALv2, SSPLv1 **or AGPLv3**; one of them must be chosen. Choosing AGPLv3 would put AGPL
   code in a shipped image, which the brief says to avoid. The same two options as in item 1 apply.
3. **Lemmy (AGPL-3.0-or-later) and pict-rs (believed AGPL-3.0) start with the default `docker compose up`.** They run unmodified as separate
   network services, are not linked with or shipped inside the IncuBrix service, and the brief itself requires a local Lemmy. Whether a
   separate test container satisfies "avoid AGPL code in the shipped service" is for the reviewer and the owner to judge: running a
   container separately does not by itself make its licence irrelevant. A compose profile that starts them only for end-to-end tests would
   make the separation clearer.
4. **psycopg (LGPL-3.0)** is used unmodified as a dynamically imported library. `psycopg-binary` bundles native libraries (libpq and others);
   redistributing the Docker image carries LGPL notice obligations and the right to replace the library (`pip install` allows that).
5. **certifi (MPL-2.0)** is file-level copyleft and is used unmodified.
6. The pict-rs licence is unverified (empty image label), the SeaweedFS and PostgreSQL images are not pinned by digest, and the texts of
   transitive dependencies were not reviewed individually.
