# SOURCES

Every third-party component the project uses, with the **version and licence actually found** on 2026-11-23.

**How this was verified** (nothing below is copied from memory or from an earlier version of this file):
* Python packages: `importlib.metadata` of the installed environment (the `License` / `License-Expression` / classifier fields). The Docker
  images install the same pinned `requirements.txt`; the Linux-only extras were read inside the demo image.
* Container images: image labels (`docker image inspect`), the version command inside each image, and, for the Debian packages in the demo
  image, each package's `/usr/share/doc/<package>/copyright` file.
* Redis/Valkey licensing: redis.io's licence pages (for the old Redis) and the Debian copyright files shipped with the packages (see sections 2, 3 and 5).
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
| redis-py | 5.2.1 | MIT | Redis client (a client library; it also talks to Valkey; the server is separate, see sections 2 and 3) |
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
| **Valkey (`valkey/valkey:8.1-alpine`)** | 8.1.x | **BSD-3-Clause** (Valkey is the Linux Foundation fork of Redis 7.2; the image has no licence label, so this is taken from the upstream project and from Debian's copyright file for the same software, see section 3) | job queue (replaced Redis 7.4.12, which is RSALv2/SSPLv1, on 2026-11-23) |
| SeaweedFS (`chrislusf/seaweedfs:latest`) | 4.54 | **Apache-2.0** (image label `org.opencontainers.image.licenses`) | S3-compatible storage. The `latest` tag is **not pinned**. MinIO images are no longer published on Docker Hub, so MinIO is not used. |
| Ghost (`ghost:5-alpine`) | 5.130.6 | MIT (`license` field of Ghost's own package.json inside the image) | local test instance for the Ghost connector |
| **Lemmy (`dessalines/lemmy:0.19.5`)** | 0.19.5 | **AGPL-3.0-or-later** (image label) | local test instance for the Lemmy connector (required by the brief) |
| **pict-rs (`asonix/pictrs:0.5`)** | 0.5 | the image label is empty; upstream pict-rs is believed to be **AGPL-3.0**, **not verified here** | Lemmy's image backend |
| Mock platforms (`app/mocks/platforms.py`) | n/a | this project's own code | simulated Discourse, Lemmy, Dev.to, Ghost, partner site |

## 3. Single-container demo image (`deploy/render/Dockerfile.render`)
Base: `python:3.11-slim` (Debian 13.7, Python 3.11.18; PSF licence for Python, Debian free-software licences for the packages).

| Debian package | Version | Licence (from the package's copyright file) | Role |
|---|---|---|---|
| **valkey-server / valkey-tools** | 8.1.1 | **BSD-3-Clause** (28 BSD-3 entries, plus BSD-2 and MIT parts; two GPL-3 entries are in the copyright file for non-core files and were not investigated further) | queue inside the demo container (replaced Debian's redis-server 8.0.2, RSALv2/SSPLv1/AGPLv3) |
| supervisor | 4.2.5-3 | BSD-derived; `supervisor/http_client.py` is under the RDFLIB licence; Debian packaging files GPL-2+ | process manager |

SQLite (Python's built-in `sqlite3`) is in the public domain.

## 4. Platform API references (documentation only, no code copied)
Discourse API docs, Lemmy API v3, Forem/Dev.to API v1, Ghost Admin API (JWT authentication).

## 5. Unresolved licensing and distribution questions: FOR THE OWNER'S REVIEW
Items 1 and 2 were resolved by replacing Redis with Valkey. The rest are open questions, not conclusions.
1. **Resolved by replacement (2026-11-23): Redis was swapped for Valkey (BSD-3-Clause)** in `docker-compose.yml` and in the demo image. Valkey speaks the Redis protocol; the
   Python client (redis-py, MIT) and RQ are unchanged. Verified: host and Docker test suites and the 49-check demo-image run pass against Valkey.
   Remaining caveat: two GPL-3 entries in Debian's valkey copyright file were not investigated, and Valkey in the compose image has no licence label.
2. (Redis 8.0.2 in the demo image: replaced as above, so the AGPLv3 option no longer applies.)
3. **Lemmy (AGPL-3.0-or-later) and pict-rs (believed AGPL-3.0) start with the default `docker compose up`.** They run unmodified as separate
   network services, are not linked with or shipped inside the IncuBrix service, and the brief itself requires a local Lemmy. Whether a
   separate test container satisfies "avoid AGPL code in the shipped service" is for the reviewer and the owner to judge: running a
   container separately does not by itself make its licence irrelevant. **Decision (2026-11-23): Lemmy and pict-rs stay in the default `docker compose up`.** Moving them
   behind a compose profile was considered and not done, because the brief asks `docker compose up` to start the local test servers and a real Lemmy is one of them.
   If the reviewer reads the AGPL rule as covering test containers, the fix is to add `profiles: [e2e]` to `lemmy`, `lemmy-db` and `pictrs` (the seed service's dependency on `lemmy`
   would then also need the profile).
4. **psycopg (LGPL-3.0)** is used unmodified as a dynamically imported library. `psycopg-binary` bundles native libraries (libpq and others);
   redistributing the Docker image carries LGPL notice obligations and the right to replace the library (`pip install` allows that).
5. **certifi (MPL-2.0)** is file-level copyleft and is used unmodified.
6. The pict-rs licence is unverified (empty image label), the SeaweedFS and PostgreSQL images are not pinned by digest, and the texts of
   transitive dependencies were not reviewed individually.
