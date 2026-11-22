"""Background jobs (Integration Standard point 3). RQ in production, inline execution for tests."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from urllib.parse import urlsplit

from redis.exceptions import RedisError

from app import netguard, runtime
from app.config import get_settings
from app.db import Job, session_scope, utcnow
from app.errors import AppError
from app.logging_setup import correlation_id_var, job_id_var, log

logger = logging.getLogger("incubrix.jobs")


def sign_callback(body: bytes) -> str:
    secret = get_settings().callback_signing_secret.encode()
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def _send_callback(url: str, payload: dict[str, Any]) -> None:
    """Deliver the signed callback through the SSRF guard. A blocked or failing callback never fails the job."""
    body = json.dumps(payload, default=str, sort_keys=True).encode()
    headers = {"Content-Type": "application/json", "X-IncuBrix-Signature": sign_callback(body)}
    try:
        status = netguard.safe_post(url, body, headers)
        log(logger, logging.INFO, "callback delivered", host=urlsplit(url).hostname, http_status=status)
    except netguard.CallbackRejected as e:
        log(logger, logging.WARNING, "callback blocked by the outbound-request guard", host=urlsplit(url).hostname, reason=e.reason)
    except Exception as e:  # a failing callback must never fail the job
        log(logger, logging.WARNING, "callback delivery failed", host=urlsplit(url).hostname, error=type(e).__name__)


class QueueUnavailable(Exception):
    """The job queue (Redis) could not be reached when enqueueing."""


def job_view(j: Job) -> dict[str, Any]:
    links = {"self": f"/api/v1/jobs/{j.id}"}
    res = j.result or {}
    if res.get("download_url"):
        links["download"] = res["download_url"]
    if res.get("evidence_url"):
        links["evidence"] = res["evidence_url"]
    return {"job_id": j.id, "kind": j.kind, "status": j.status, "progress": j.progress, "result": j.result,
            "error": j.error, "links": links, "created_at": j.created_at.isoformat(),
            "finished_at": j.finished_at.isoformat() if j.finished_at else None}


def run_job(job_id: str) -> None:
    db = session_scope()
    try:
        job = db.get(Job, job_id)
        if job is None or job.status in ("succeeded", "failed"):
            return
        job_id_var.set(job_id)
        correlation_id_var.set(job.correlation_id)
        job.status, job.progress = "running", 10
        db.commit()
        p = job.payload
        try:
            if job.kind == "publish":
                result = runtime.publish_flow(db, p["destination_id"], p["rendition"], p["idempotency_key"])
            elif job.kind == "certify":
                result = runtime.certify(db, p["destination_id"])
            elif job.kind == "cleanup":
                result = {"deleted": runtime.cleanup_stale_test_posts(db)}
            else:
                raise AppError("INTERNAL_ERROR", f"unknown job kind {job.kind}", {}, 500)
            job.status, job.progress, job.result = "succeeded", 100, result
            if job.kind == "certify" and result.get("status") == "failed":
                job.status = "failed"
                job.error = {"code": result["failure"]["code"], "message": result["failure"]["message"],
                             "retryable": result["failure"]["retryable"], "details": result["failure"].get("details", {})}
        except AppError as e:
            db.rollback()
            job = db.get(Job, job_id)
            job.status, job.progress, job.error = "failed", 100, e.to_dict()["error"]
            if e.details.get("reschedule_at"):
                job.result = {"status": "rescheduled", "reschedule_at": e.details["reschedule_at"], "reason": e.message}
            elif e.code in ("DEST_RULE_VIOLATION", "DEST_NOT_CERTIFIED", "DEST_PERMISSION_REVOKED"):
                job.result = {"status": "blocked", "reason": e.message}
            log(logger, logging.WARNING, "job failed", code=e.code, detail=e.message)
        except Exception as e:  # never leave a job stuck in 'running'
            db.rollback()
            job = db.get(Job, job_id)
            job.status, job.progress = "failed", 100
            job.error = {"code": "INTERNAL_ERROR", "message": f"{type(e).__name__}: {e}", "retryable": False, "details": {}}
            logger.exception("job crashed")
        job.finished_at = utcnow()
        db.commit()
        if job.callback_url:
            _send_callback(job.callback_url, job_view(job))
    finally:
        db.close()


def enqueue(job_id: str) -> None:
    s = get_settings()
    if s.job_backend == "rq" and s.redis_url:
        from redis import Redis
        from rq import Queue
        try:
            Queue("incubrix", connection=Redis.from_url(s.redis_url, socket_connect_timeout=2, socket_timeout=5)).enqueue(
                "app.jobs.run_job", job_id, job_timeout=600)
        except (RedisError, OSError) as e:
            raise QueueUnavailable(type(e).__name__) from e
    else:
        run_job(job_id)
