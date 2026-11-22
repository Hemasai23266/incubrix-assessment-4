"""IncuBrix Long-Tail Distribution service: FastAPI app. All management routes live under /api/v1/."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, File, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import jobs, netguard, rules, runtime, storage
from app.config import get_settings
from app.connectors.registry import get_connector, get_registry
from app.db import ConnectorState, Destination, IdempotencyRecord, Job, get_session, init_db, new_id, session_scope, utcnow
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


@app.exception_handler(404)
async def not_found(request: Request, exc):
    return JSONResponse(AppError("NOT_FOUND", "route or resource not found", {"path": request.url.path}, 404).to_dict(),
                        status_code=404)


def require_auth(authorization: str | None = Header(None)) -> None:
    expected = get_settings().api_token
    got = (authorization or "").removeprefix("Bearer ").strip()
    if not got or not hmac.compare_digest(got, expected):
        raise AppError("UNAUTHORIZED", "missing or invalid bearer token", {}, 401)


def require_idem(idempotency_key: str | None = Header(None, alias="Idempotency-Key")) -> str:
    if not idempotency_key or len(idempotency_key) > 200:
        raise AppError("IDEMPOTENCY_KEY_REQUIRED", "send an Idempotency-Key header (max 200 chars) on this POST", {}, 400)
    return idempotency_key


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
def disable_connector(key: str, db: Session = Depends(get_session)):
    get_connector(key)
    runtime._state(db, key).disabled = True
    db.commit()
    return {"key": key, "disabled": True}


@app.post("/api/v1/connectors/{key}/enable", dependencies=[Depends(require_auth)], tags=["connectors"])
def enable_connector(key: str, db: Session = Depends(get_session)):
    get_connector(key)
    st = runtime._state(db, key)
    st.disabled, st.consecutive_failures = False, 0
    db.commit()
    return {"key": key, "disabled": False}


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
def revoke_destination(dest_id: str, db: Session = Depends(get_session)):
    """A partner revoked permission: block all further posting to it."""
    d = _get_dest(db, dest_id)
    d.status, d.certified, d.level = "revoked", False, "L0"
    db.commit()
    return runtime.dest_to_dict(d)


@app.post("/api/v1/destinations/{dest_id}/check", dependencies=[Depends(require_auth)], tags=["rules"])
def check_rules(dest_id: str, rendition: Rendition, db: Session = Depends(get_session)):
    """Dry run: which rules would fire for this content? Nothing is posted or recorded."""
    d = _get_dest(db, dest_id)
    r = rendition.model_dump()
    v = rules.evaluate(db, d, r, record=False)
    spec = get_connector(d.connector_key).validate(r, runtime.dest_to_dict(d), None)
    return {"rules": v.as_dict(), "spec": spec}


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
    if len(data) > 25 * 1024 * 1024:
        raise AppError("VALIDATION_ERROR", "file larger than 25 MB", {"size": len(data)}, 413)
    safe = "".join(c for c in (file.filename or "upload") if c.isalnum() or c in "._-") or "upload"
    key = f"uploads/{hashlib.sha256(idem.encode()).hexdigest()[:16]}/{safe}"
    url = storage.put_bytes(key, data, file.content_type or "application/octet-stream")
    return {"key": key, "download_url": url, "size": len(data), "content_type": file.content_type}


@app.get("/api/v1/files/{key:path}", dependencies=[Depends(require_auth)], tags=["files"])
def download_file(key: str):
    try:
        data, ct = storage.get_bytes(key)
    except Exception:
        raise AppError("NOT_FOUND", "file not found", {"key": key}, 404)
    return Response(content=data, media_type=ct)


@app.post("/api/v1/simulation/faults/{instance}", dependencies=[Depends(require_auth)], tags=["simulation"])
def set_fault(instance: str, body: FaultIn):
    """Flip a failure switch on a mock platform (simulation mode only)."""
    if not get_settings().simulation_mode:
        raise AppError("VALIDATION_ERROR", "fault injection is only available in simulation mode", {}, 403)
    from app.mocks.platforms import FAULTS
    if instance not in FAULTS:
        raise AppError("NOT_FOUND", f"unknown mock instance '{instance}'", {"known": sorted(FAULTS)}, 404)
    FAULTS[instance].update({k: v for k, v in body.model_dump().items() if v is not None})
    return {"instance": instance, "faults": dict(FAULTS[instance])}


# ------------------------------------------------------------------ connector contract (Appendix A)
CONTRACT_METHODS = {"validate", "publish", "get_status", "delete", "fetch_metrics", "refresh_auth", "build_assist_pack"}


@app.post("/connector/v1/{connector_key}/{method}", dependencies=[Depends(require_auth)], tags=["connector-contract"])
async def connector_call(connector_key: str, method: str, request: Request):
    """Exact Appendix A contract. Methods return results only and never write to the database."""
    if method not in CONTRACT_METHODS:
        raise AppError("NOT_FOUND", f"unknown method '{method}'", {"methods": sorted(CONTRACT_METHODS)}, 404)
    c = get_connector(connector_key)
    st = session_scope()
    try:
        if runtime._state(st, connector_key).disabled:
            raise AppError("CONNECTOR_DISABLED", f"connector '{connector_key}' is disabled", {}, 503)
    finally:
        st.close()
    try:
        b = await request.json()
    except Exception:
        raise AppError("VALIDATION_ERROR", "body must be JSON", {}, 422)
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
            key = b.get("idempotency_key")
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
