# SOURCES

Everything in the default path is permissively licensed and usable commercially. No AGPL code is shipped in the service.

## Libraries (Python, installed from PyPI; see `requirements.txt`)
| Library | Licence | Use |
|---|---|---|
| FastAPI 0.115 | MIT | HTTP API, OpenAPI 3.1 generation |
| Starlette (via FastAPI) | BSD-3 | ASGI, `TestClient` used to run mock platforms in-process |
| Uvicorn | BSD-3 | server |
| SQLAlchemy 2.0 | MIT | database models |
| psycopg 3 | LGPL-3.0 (dynamic use, not modified) | PostgreSQL driver |
| Pydantic / pydantic-settings | MIT | validation, env settings |
| httpx | BSD-3 | HTTP client |
| PyYAML | MIT | connector manifests |
| boto3 | Apache-2.0 | S3-compatible storage (MinIO) |
| RQ / redis-py | BSD-2 / MIT | background jobs |
| python-multipart | Apache-2.0 | file uploads |
| pytest | MIT | tests |

## Services / containers (run locally, throw-away credentials)
| Component | Licence | Use |
|---|---|---|
| PostgreSQL 16 (`postgres:16-alpine`) | PostgreSQL licence | database |
| Redis 7 (`redis:7-alpine`) | BSD-3 (7.x line) | job queue |
| SeaweedFS (`chrislusf/seaweedfs`) | Apache-2.0 | S3-compatible object storage in compose (MinIO images are no longer published on Docker Hub; MinIO or AWS S3 work by changing `S3_ENDPOINT_URL`) |
| Ghost 5 (`ghost:5-alpine`) | MIT | local test instance for the Ghost connector |
| Lemmy 0.19 (`dessalines/lemmy`) | **AGPL-3.0** | local test instance only (separate container, never linked or shipped) |
| pict-rs (`asonix/pictrs`) | AGPL-3.0 | Lemmy's image backend, test instance only |

The AGPL items are separate containers used to exercise the connectors over HTTP; the IncuBrix service contains none of their code.

## Models / AI APIs
None. No LLM, embedding model or paid API is called by the shipped code. Cost per 1,000 publications: **0.00 USD** (no paid services).

## Datasets and media
None. All test content is synthetic text written for this project. No third-party media, voices or faces are used.

## Platform API references (documentation only, no code copied)
Discourse API docs, Lemmy API (v3), Forem/Dev.to API v1, Ghost Admin API (JWT auth).
