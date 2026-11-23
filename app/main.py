"""IncuBrix Long-Tail Distribution service: FastAPI app. All management routes live under /api/v1/."""
from __future__ import annotations

import hashlib
import hmac
import functools
import json
import logging
import re
import time
import uuid
from http import HTTPStatus
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Any, Callable

from fastapi import Depends, FastAPI, File, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import fetcher, jobs, netguard, rules, runtime, storage
from app.config import get_settings
from app.connectors.registry import get_connector, get_registry
from app.db import ConnectorState, Destination, IdempotencyRecord, IdempotentResponse, Job, get_session, init_db, new_id, session_scope, utcnow
from app.errors import ERROR_CODES, AppError
from app.logging_setup import correlation_id_var, log, setup_logging

logger = logging.getLogger("incubrix.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    init_db()
    yield


app = FastAPI(title="IncuBrix Long-Tail Distribution", version="1.0.0", lifespan=lifespan,
              description="Connector runtime, posting rules and certification for long-tail destinations.")

_hits: dict[str, deque] = defaultdict(deque)


@app.middleware("http")
async def correlation_and_limits(request: Request, call_next):
    cid = request.headers.get("X-Correlation-Id") or uuid.uuid4().hex
    correlation_id_var.set(cid)
    limit = get_settings().rate_limit_per_minute
    if limit and request.url.path not in ("/health", "/ready"):
        ip = request.client.host if request.client else "?"
        q = _hits[ip]
        now = time.time()
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= limit:
            err = AppError("RATE_LIMITED", "too many requests; slow down", {"limit_per_minute": limit}, 429)
            return JSONResponse(err.to_dict(), status_code=429, headers={"Retry-After": "30", "X-Correlation-Id": cid})
        q.append(now)
    start = time.time()
    response = await call_next(request)
    response.headers["X-Correlation-Id"] = cid
    log(logger, logging.INFO, "request", method=request.method, path=request.url.path,
        status=response.status_code, ms=round((time.time() - start) * 1000, 1))
    return response


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    headers = {}
    if exc.details.get("retry_after_seconds"):
        headers["Retry-After"] = str(int(exc.details["retry_after_seconds"]))
    return JSONResponse(exc.to_dict(), status_code=exc.status_code, headers=headers)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    errs = [{"field": ".".join(str(x) for x in e["loc"] if x != "body"), "reason": e["msg"]} for e in exc.errors()]
    return JSONResponse(AppError("VALIDATION_ERROR", "request body failed validation", {"errors": errs}, 422).to_dict(),
                        status_code=422)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    logger.exception("unhandled error")
    return JSONResponse(AppError("INTERNAL_ERROR", "unexpected server error", {}, 500).to_dict(), status_code=500)


_HTTP_CODES = {400: "VALIDATION_ERROR", 401: "UNAUTHORIZED", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 429: "RATE_LIMITED"}
_HTTP_MESSAGES = {404: "route or resource not found", 405: "method not allowed for this route"}


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Framework-generated errors (wrong method, unknown route, bad request line, ...) use the same envelope as application errors.
    Messages come from a fixed table / the HTTP status phrase, never from exception internals."""
    status = exc.status_code
    phrase = HTTPStatus(status).phrase.lower() if status in HTTPStatus._value2member_map_ else "http error"
    details: dict = {"path": request.url.path}
    headers: dict = {}
    allow = (exc.headers or {}).get("Allow") or (exc.headers or {}).get("allow")
    if status == 405 and allow:
        details["allowed_methods"] = [m.strip() for m in allow.split(",")]
        headers["Allow"] = allow
    err = AppError(_HTTP_CODES.get(status, "HTTP_ERROR"), _HTTP_MESSAGES.get(status, phrase), details, status, retryable=status >= 500)
    return JSONResponse(err.to_dict(), status_code=status, headers=headers)


def require_auth(authorization: str | None = Header(None)) -> None:
    expected = get_settings().api_token
    got = (authorization or "").removeprefix("Bearer ").strip()
    if not got or not hmac.compare_digest(got, expected):
        raise AppError("UNAUTHORIZED", "missing or invalid bearer token", {}, 401)


_KEY_RE = re.compile(r"^[\x21-\x7e]{1,200}$")  # printable ASCII, no spaces, 1-200 characters


def _validated_key(raw: str | None, required: bool) -> str | None:
    if raw is None or raw == "":
        if required:
            raise AppError("IDEMPOTENCY_KEY_REQUIRED", "send an Idempotency-Key header (1-200 printable characters) on this POST", {}, 400)
        return None
    if not _KEY_RE.match(raw):
        raise AppError("IDEMPOTENCY_KEY_INVALID", "Idempotency-Key must be 1-200 printable ASCII characters without spaces", {}, 400)
    return raw


def require_idem(idempotency_key: str | None = Header(None, alias="Idempotency-Key")) -> str:
    """EVERY POST route requires the header (Integration Standard point 4). A repeat with the same key replays the first answer."""
    return _validated_key(idempotency_key, True)  # type: ignore[return-value]



# ------------------------------------------------------------------ schemas
class Media(BaseModel):
    url: str = ""
    content_type: str = ""
    filename: str = ""


class Rendition(BaseModel):
    title: str
    body: str = ""
    html: str | None = None
    link: str | None = None
    media: list[Media] = []
    tags: list[str] = []
    language: str = "en"
    is_promotional: bool = False
    ai_generated: bool = False
    disclosure: str | None = None


class DestinationIn(BaseModel):
    id: str | None = None
    name: str
    url: str
    platform_key: str
    surface: str = "topic"
    category: str = ""
    audience_tags: list[str] = []
    language: str = "en"
    geo: str = "global"
    est_audience: int = 0
    rules_url: str = ""
    frequency_cap: dict[str, Any] = Field(default_factory=dict)
    self_promo_allowed: bool = False
    link_policy: str = "allowed"
    disclosure_required: bool = False
    disclosure_text: str = "This content was created with AI assistance."
    automation_allowed: bool = True
    connector_key: str
    relevance_score: int | None = Field(None, ge=1, le=5)
    relevance_reason: str = ""
    level: str = "L0"
    credentials_ref: str = ""
    options: dict[str, Any] = Field(default_factory=dict)


class PublishIn(BaseModel):
    destination_id: str
    rendition: Rendition
    callback_url: str | None = None


class CertifyIn(BaseModel):
    callback_url: str | None = None


class FetchIn(BaseModel):
    url: str
    filename: str | None = Field(None, max_length=120)
    callback_url: str | None = None


class FaultIn(BaseModel):
    auth_expired: bool | None = None
    rename: bool | None = None
    down: bool | None = None
    revoked: bool | None = None


# ------------------------------------------------------------------ helpers
def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def create_idempotent_job(db: Session, kind: str, scope: str, key: str, payload: dict, callback_url: str | None,
                          request: Request) -> tuple[Job, bool]:
    """Same key + same request => the SAME job, enqueued once. Same key + different request => 422."""
    h = _hash(payload)
    rec = db.scalars(select(IdempotencyRecord).where(IdempotencyRecord.scope == scope, IdempotencyRecord.key == key)).first()
    if rec is None:
        job = Job(kind=kind, payload=payload, callback_url=callback_url, correlation_id=correlation_id_var.get())
        db.add(job)
        db.flush()
        db.add(IdempotencyRecord(scope=scope, key=key, request_hash=h, job_id=job.id))
        try:
            db.commit()
            return job, True
        except IntegrityError:  # a concurrent request with the same key won the race
            db.rollback()
            rec = db.scalars(select(IdempotencyRecord).where(IdempotencyRecord.scope == scope, IdempotencyRecord.key == key)).one()
    if rec.request_hash != h:
        raise AppError("IDEMPOTENCY_KEY_REUSED", "this Idempotency-Key was already used with a different request",
                       {"key": key}, 422)
    job = db.get(Job, rec.job_id)
    # Same key after a RETRYABLE failure (e.g. timeout): run the SAME job again. The post row and the
    # platform-side key make this safe: a post that already exists is reconciled, never duplicated.
    if job.status == "failed" and (job.error or {}).get("retryable"):
        job.status, job.progress, job.error, job.result, job.finished_at = "queued", 0, None, None, None
        db.commit()
        return job, True
    return job, False




def _aware(d):
    from datetime import timezone
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def run_idempotent(scope: str, key: str | None, payload: Any, fn: Callable[[], Any]) -> tuple[Any, bool]:
    """Run fn() at most once per (scope, key). Returns (response, replayed).
    Same key + same request  -> the stored response of the first call (fn is NOT run again).
    Same key + other request -> 422 IDEMPOTENCY_KEY_REUSED.
    Concurrent duplicates    -> the database unique constraint picks one owner; the others wait (up to 5 s) and replay its response.
    If fn() fails the claim is released, so a retry with the same key runs it again."""
    if key is None:
        return fn(), False
    h = _hash(payload)
    db = session_scope()
    try:
        deadline = time.time() + get_settings().idempotency_wait_seconds
        while True:
            rec = IdempotentResponse(scope=scope, key=key, request_hash=h, state="in_progress")
            db.add(rec)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                db.expire_all()
                other = db.scalars(select(IdempotentResponse).where(IdempotentResponse.scope == scope, IdempotentResponse.key == key)).first()
                snap = None if other is None else (other.request_hash, other.state, other.response, other.created_at)
                db.rollback()  # end the read transaction NOW: on SQLite it would hold the write lock the owner needs to finish
                if snap is None:  # the owner failed and released the key a moment ago: try to claim it ourselves
                    continue
                other_hash, other_state, other_response, other_created = snap
                if other_hash != h:
                    raise AppError("IDEMPOTENCY_KEY_REUSED", "this Idempotency-Key was already used with a different request", {"key": key}, 422)
                if other_state == "done":
                    return other_response, True
                if (utcnow() - _aware(other_created)).total_seconds() > get_settings().idempotency_claim_ttl_seconds:  # owner died: release the stale claim
                    stale = db.scalars(select(IdempotentResponse).where(IdempotentResponse.scope == scope, IdempotentResponse.key == key,
                                                                        IdempotentResponse.state == "in_progress")).first()
                    if stale is not None:
                        db.delete(stale)
                    db.commit()
                    continue
                if time.time() > deadline:
                    raise AppError("IDEMPOTENCY_IN_PROGRESS", "a request with this Idempotency-Key is still running; retry shortly",
                                   {"retry_after_seconds": 2}, 409)
                time.sleep(0.05)
                continue
            try:
                result = fn()
            except BaseException:
                db.delete(rec)
                db.commit()
                raise
            rec.state, rec.response = "done", result
            db.commit()
            return result, False
    finally:
        db.close()


def reply(result: Any, replayed: bool):
    return JSONResponse(result, headers={"Idempotent-Replay": "true"}) if replayed else result


def accepted(job: Job, created: bool) -> JSONResponse:
    body = {"job_id": job.id, "status": job.status, "status_url": f"/api/v1/jobs/{job.id}", "idempotent_replay": not created}
    return JSONResponse(body, status_code=202, headers={"Location": f"/api/v1/jobs/{job.id}"})


def check_callback_url(url: str | None) -> None:
    """Reject unsafe callback URLs at submission time (they are checked again, authoritatively, when the callback is sent)."""
    if url:
        try:
            netguard.validate_callback_url(url)
        except netguard.CallbackRejected as e:
            raise AppError("CALLBACK_URL_REJECTED", f"callback_url rejected: {e.reason}", {"reason": e.reason}, 422)


def dispatch(db: Session, job: Job) -> None:
    """Hand a new job to the worker. If the queue is down, mark the job failed-but-retryable and answer 503, so a retry with
    the same Idempotency-Key re-runs this same job instead of leaving it queued forever."""
    try:
        jobs.enqueue(job.id)
    except jobs.QueueUnavailable:
        job.status, job.progress, job.finished_at = "failed", 100, utcnow()
        job.error = {"code": "QUEUE_UNAVAILABLE", "message": "the job queue is unavailable; retry with the same Idempotency-Key",
                     "retryable": True, "details": {}}
        db.commit()
        raise AppError("QUEUE_UNAVAILABLE", "the job queue is temporarily unavailable; retry with the same Idempotency-Key",
                       {"job_id": job.id, "retry_with_same_idempotency_key": True, "retry_after_seconds": 5}, 503)
    db.refresh(job)


def _get_dest(db: Session, dest_id: str) -> Destination:
    d = db.get(Destination, dest_id)
    if d is None:
        raise AppError("NOT_FOUND", f"destination '{dest_id}' not found", {}, 404)
    return d


# ------------------------------------------------------------------ public
@app.get("/health", tags=["ops"])
def health():
    return {"status": "ok"}


@app.get("/ready", tags=["ops"])
def ready():
    checks: dict[str, str] = {}
    try:
        db = session_scope()
        db.execute(text("select 1"))
        db.close()
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"fail: {type(e).__name__}"
    s = get_settings()
    if s.redis_url and s.job_backend == "rq":
        try:
            from redis import Redis
            Redis.from_url(s.redis_url, socket_connect_timeout=2).ping()
            checks["redis"] = "ok"
        except Exception as e:
            checks["redis"] = f"fail: {type(e).__name__}"
    checks["storage"] = "ok" if storage.storage_ready() else "fail"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"status": "ready" if ok else "not_ready", "mode": "simulation" if s.simulation_mode else "live",
                         "checks": checks}, status_code=200 if ok else 503)


# ------------------------------------------------------------------ /api/v1
@app.get("/api/v1/connectors", dependencies=[Depends(require_auth)], tags=["connectors"])
def list_connectors(db: Session = Depends(get_session)):
    out = []
    for k, c in get_registry().items():
        st = db.get(ConnectorState, k)
        out.append({"key": k, "version": c.manifest["version"], "tier": c.manifest["tier"], "auth": c.manifest["auth"],
                    "capabilities": c.manifest["capabilities"], "disabled": bool(st and st.disabled),
                    "consecutive_failures": st.consecutive_failures if st else 0})
    return {"connectors": out, "error_codes": {k: {"retryable": v[0], "meaning": v[1]} for k, v in ERROR_CODES.items()}}


@app.post("/api/v1/connectors/{key}/disable", dependencies=[Depends(require_auth)], tags=["connectors"])
def disable_connector(key: str, idem: str = Depends(require_idem), db: Session = Depends(get_session)):
    def work():
        get_connector(key)
        runtime._state(db, key).disabled = True
        db.commit()
        return {"key": key, "disabled": True}
    return reply(*run_idempotent(f"disable:{key}", idem, {"key": key}, work))


@app.post("/api/v1/connectors/{key}/enable", dependencies=[Depends(require_auth)], tags=["connectors"])
def enable_connector(key: str, idem: str = Depends(require_idem), db: Session = Depends(get_session)):
    def work():
        get_connector(key)
        st = runtime._state(db, key)
        st.disabled, st.consecutive_failures = False, 0
        db.commit()
        return {"key": key, "disabled": False}
    return reply(*run_idempotent(f"enable:{key}", idem, {"key": key}, work))


@app.post("/api/v1/destinations", status_code=201, dependencies=[Depends(require_auth)], tags=["destinations"])
def create_destination(body: DestinationIn, idem: str = Depends(require_idem), db: Session = Depends(get_session)):
    """A new forum/community/site of a supported type is ONE row: no new connector code."""
    get_connector(body.connector_key)
    scope = "create_destination"
    rec = db.scalars(select(IdempotencyRecord).where(IdempotencyRecord.scope == scope, IdempotencyRecord.key == idem)).first()
    if rec:
        if rec.request_hash != _hash(body.model_dump()):
            raise AppError("IDEMPOTENCY_KEY_REUSED", "key already used with a different request", {}, 422)
        return JSONResponse(runtime.dest_to_dict(_get_dest(db, rec.job_id)), status_code=200)
    d = Destination(**{**body.model_dump(), "id": body.id or new_id()})
    db.add(d)
    db.flush()
    db.add(IdempotencyRecord(scope=scope, key=idem, request_hash=_hash(body.model_dump()), job_id=d.id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError("VALIDATION_ERROR", "destination id already exists or key reused concurrently", {"id": body.id}, 409)
    return runtime.dest_to_dict(d)


@app.get("/api/v1/destinations", dependencies=[Depends(require_auth)], tags=["destinations"])
def list_destinations(db: Session = Depends(get_session), connector_key: str | None = None, status: str | None = None):
    q = select(Destination)
    if connector_key:
        q = q.where(Destination.connector_key == connector_key)
    if status:
        q = q.where(Destination.status == status)
    return {"destinations": [runtime.dest_to_dict(d) for d in db.scalars(q).all()]}


@app.get("/api/v1/destinations/{dest_id}", dependencies=[Depends(require_auth)], tags=["destinations"])
def get_destination(dest_id: str, db: Session = Depends(get_session)):
    return runtime.dest_to_dict(_get_dest(db, dest_id))


@app.post("/api/v1/destinations/{dest_id}/revoke", dependencies=[Depends(require_auth)], tags=["destinations"])
def revoke_destination(dest_id: str, idem: str = Depends(require_idem), db: Session = Depends(get_session)):
    """A partner revoked permission: block all further posting to it."""
    def work():
        d = _get_dest(db, dest_id)
        d.status, d.certified, d.level = "revoked", False, "L0"
        db.commit()
        return runtime.dest_to_dict(d)
    return reply(*run_idempotent(f"revoke:{dest_id}", idem, {"dest_id": dest_id}, work))


@app.post("/api/v1/destinations/{dest_id}/check", dependencies=[Depends(require_auth)], tags=["rules"])
def check_rules(dest_id: str, rendition: Rendition, idem: str = Depends(require_idem), db: Session = Depends(get_session)):
    """Dry run: which rules would fire for this content? Nothing is posted or recorded."""
    def work():
        d = _get_dest(db, dest_id)
        r = rendition.model_dump()
        v = rules.evaluate(db, d, r, record=False)
        spec = get_connector(d.connector_key).validate(r, runtime.dest_to_dict(d), None)
        return {"rules": v.as_dict(), "spec": spec}
    return reply(*run_idempotent(f"check:{dest_id}", idem, {"dest_id": dest_id, "rendition": rendition.model_dump()}, work))


@app.post("/api/v1/destinations/{dest_id}/certify", status_code=202, dependencies=[Depends(require_auth)], tags=["certification"])
def certify_destination(dest_id: str, request: Request, body: CertifyIn | None = None,
                        idem: str = Depends(require_idem), db: Session = Depends(get_session)):
    _get_dest(db, dest_id)
    check_callback_url(body.callback_url if body else None)
    job, created = create_idempotent_job(db, "certify", f"certify:{dest_id}", idem, {"destination_id": dest_id},
                                         body.callback_url if body else None, request)
    if created:
        dispatch(db, job)
    return accepted(job, created)


@app.get("/api/v1/destinations/{dest_id}/certifications", dependencies=[Depends(require_auth)], tags=["certification"])
def list_certifications(dest_id: str, db: Session = Depends(get_session)):
    from app.db import Certification
    rows = db.scalars(select(Certification).where(Certification.destination_id == dest_id)
                      .order_by(Certification.created_at.desc())).all()
    return {"certifications": [{"id": c.id, "status": c.status, "environment": c.environment, "duration_s": c.duration_s,
                                "evidence_url": c.evidence_url, "created_at": c.created_at.isoformat()} for c in rows]}


@app.post("/api/v1/publish", status_code=202, dependencies=[Depends(require_auth)], tags=["publish"])
def publish(body: PublishIn, request: Request, idem: str = Depends(require_idem), db: Session = Depends(get_session)):
    """Queue a publish. Same Idempotency-Key => same job, never a second post."""
    _get_dest(db, body.destination_id)
    check_callback_url(body.callback_url)
    payload = {"destination_id": body.destination_id, "rendition": body.rendition.model_dump(), "idempotency_key": idem}
    job, created = create_idempotent_job(db, "publish", "publish", idem, {k: v for k, v in payload.items()}, body.callback_url, request)
    if created:
        dispatch(db, job)
    return accepted(job, created)


@app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(require_auth)], tags=["jobs"])
def get_job(job_id: str, db: Session = Depends(get_session)):
    j = db.get(Job, job_id)
    if j is None:
        raise AppError("NOT_FOUND", f"job '{job_id}' not found", {}, 404)
    return jobs.job_view(j)


@app.post("/api/v1/maintenance/cleanup-test-posts", status_code=202, dependencies=[Depends(require_auth)], tags=["certification"])
def cleanup(request: Request, idem: str = Depends(require_idem), db: Session = Depends(get_session)):
    job, created = create_idempotent_job(db, "cleanup", "cleanup", idem, {}, None, request)
    if created:
        dispatch(db, job)
    return accepted(job, created)


@app.post("/api/v1/files", status_code=201, dependencies=[Depends(require_auth)], tags=["files"])
async def upload_file(file: UploadFile = File(...), idem: str = Depends(require_idem)):
    """Files in: upload; the stored copy gets a download link (S3-compatible storage)."""
    data = await file.read()
    limit = get_settings().upload_max_bytes
    if len(data) > limit:
        raise AppError("VALIDATION_ERROR", f"file larger than {limit} bytes", {"size": len(data), "limit": limit}, 413)
    safe = "".join(c for c in (file.filename or "upload") if c.isalnum() or c in "._-") or "upload"
    key = f"uploads/{hashlib.sha256(idem.encode()).hexdigest()[:16]}/{safe}"
    url = storage.put_bytes(key, data, file.content_type or "application/octet-stream")
    return {"key": key, "download_url": url, "size": len(data), "content_type": file.content_type}


@app.post("/api/v1/files/from-url", status_code=202, dependencies=[Depends(require_auth)], tags=["files"])
def fetch_file_from_url(body: FetchIn, request: Request, idem: str = Depends(require_idem), db: Session = Depends(get_session)):
    """Files in, by URL: a background job downloads the file SAFELY (public addresses only, DNS pinned, redirects re-validated, size and
    type limits) and stores it; the job result carries the download link and the standard manifest."""
    fetcher.check_url(body.url)  # 422 MEDIA_URL_REJECTED for anything unsafe; the worker re-validates every hop
    check_callback_url(body.callback_url)
    job, created = create_idempotent_job(db, "fetch", "fetch", idem, {"url": body.url, "filename": body.filename}, body.callback_url, request)
    if created:
        dispatch(db, job)
    return accepted(job, created)


@app.get("/api/v1/files/{key:path}", dependencies=[Depends(require_auth)], tags=["files"])
def download_file(key: str):
    try:
        data, ct = storage.get_bytes(key)
    except Exception:
        raise AppError("NOT_FOUND", "file not found", {"key": key}, 404)
    return Response(content=data, media_type=ct)


@app.post("/api/v1/simulation/faults/{instance}", dependencies=[Depends(require_auth)], tags=["simulation"])
def set_fault(instance: str, body: FaultIn, idem: str = Depends(require_idem)):
    """Flip a failure switch on a mock platform (simulation mode only)."""
    def work():
        if not get_settings().simulation_mode:
            raise AppError("VALIDATION_ERROR", "fault injection is only available in simulation mode", {}, 403)
        from app.mocks.platforms import FAULTS
        if instance not in FAULTS:
            raise AppError("NOT_FOUND", f"unknown mock instance '{instance}'", {"known": sorted(FAULTS)}, 404)
        FAULTS[instance].update({k: v for k, v in body.model_dump().items() if v is not None})
        return {"instance": instance, "faults": dict(FAULTS[instance])}
    return reply(*run_idempotent(f"faults:{instance}", idem, {"instance": instance, "body": body.model_dump()}, work))


# ------------------------------------------------------------------ connector contract (Appendix A)
CONTRACT_METHODS = {"validate", "publish", "get_status", "delete", "fetch_metrics", "refresh_auth", "build_assist_pack"}


def _contract_execute(connector_key: str, method: str, b: dict, key: str | None):
    c = get_connector(connector_key)
    st = session_scope()
    try:
        if runtime._state(st, connector_key).disabled:
            raise AppError("CONNECTOR_DISABLED", f"connector '{connector_key}' is disabled", {}, 503)
    finally:
        st.close()
    conn = b.get("connection") or {}
    if get_settings().simulation_mode:  # no real accounts needed: fill in simulation credentials
        conn = {**runtime.SIM_CREDENTIALS.get(connector_key, {}), **conn}
    if get_settings().simulation_mode and not conn.get("base_url"):
        conn["base_url"] = f"http://mock/{(b.get('destination') or {}).get('options', {}).get('mock_instance', runtime.SIM_INSTANCE.get(connector_key, connector_key))}"
    dest = b.get("destination") or {}
    try:
        if method == "validate":
            return c.validate(b.get("rendition") or {}, dest, conn)
        if method == "publish":
            if not key:
                raise AppError("VALIDATION_ERROR", "idempotency_key is required", {"field": "idempotency_key"}, 422)
            res = c.validate(b.get("rendition") or {}, dest, conn)
            if not res["ok"]:
                raise AppError("DEST_SPEC_VIOLATION", "content breaks the destination's format limits", {"violations": res["violations"]}, 422)
            return c.publish(b.get("rendition") or {}, dest, conn, key)
        if method == "get_status":
            return c.get_status(b["platform_post_id"], conn)
        if method == "delete":
            return c.delete(b["platform_post_id"], conn)
        if method == "fetch_metrics":
            return c.fetch_metrics(b["platform_post_id"], conn, b.get("since"))
        if method == "refresh_auth":
            return c.refresh_auth(conn)
        return c.build_assist_pack(b.get("rendition") or {}, dest)
    except KeyError as e:
        raise AppError("VALIDATION_ERROR", f"missing field {e}", {"field": str(e).strip("'")}, 422)


@app.post("/connector/v1/{connector_key}/{method}", dependencies=[Depends(require_auth)], tags=["connector-contract"])
async def connector_call(connector_key: str, method: str, request: Request, key: str = Depends(require_idem)):
    """Exact Appendix A contract. Methods return results only and never write to the database.
    The Idempotency-Key HEADER is required for every method including publish; a body field `idempotency_key` is NOT accepted in its place
    (the header value is the key that is sent on to the platform). A repeated identical request replays the first answer."""
    if method not in CONTRACT_METHODS:
        raise AppError("NOT_FOUND", f"unknown method '{method}'", {"methods": sorted(CONTRACT_METHODS)}, 404)
    try:
        b = await request.json()
    except Exception:
        raise AppError("VALIDATION_ERROR", "body must be JSON", {}, 422)
    if not isinstance(b, dict):
        raise AppError("VALIDATION_ERROR", "body must be a JSON object", {}, 422)
    work = functools.partial(_contract_execute, connector_key, method, b, key)
    result, replayed = await run_in_threadpool(run_idempotent, f"contract:{connector_key}:{method}", key,
                                               {"connector": connector_key, "method": method, "body": b}, work)
    return reply(result, replayed)


def _openapi_with_required_idempotency_key():
    """The header is enforced by `require_idem` (answers 400 IDEMPOTENCY_KEY_REQUIRED in the standard envelope), not by FastAPI's own validation,
    so FastAPI documents it as optional. The published spec must match the running service: mark it required on every POST operation."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = _original_openapi()
    for ops in schema["paths"].values():
        for param in (ops.get("post") or {}).get("parameters", []):
            if param.get("name", "").lower() == "idempotency-key":
                param["required"] = True
                param.setdefault("description", "Required. 1-200 printable ASCII characters, no spaces. A repeat with the same key replays the first answer.")
    return schema


_original_openapi = app.openapi
app.openapi = _openapi_with_required_idempotency_key  # type: ignore[method-assign]
