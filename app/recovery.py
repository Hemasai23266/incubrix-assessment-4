"""Queue recovery for the single-container demo (and usable anywhere RQ + a database are used).

Redis may restart, lose its data, or the worker may die mid-job. Jobs are always recorded in the database first, so any job
that is still 'queued' or 'running' there but is NOT present in the RQ queue is an orphan and is enqueued again. Re-running
is safe: publishing is idempotent (unique post row per Idempotency-Key) and run_job() ignores jobs that already finished.
"""
from __future__ import annotations

import logging

from redis import Redis
from rq import Queue
from sqlalchemy import select

from app.config import get_settings
from app.db import Job, session_scope
from app.logging_setup import log

logger = logging.getLogger("incubrix.recovery")


def recover_orphans() -> int:
    s = get_settings()
    if s.job_backend != "rq" or not s.redis_url:
        return 0
    queue = Queue("incubrix", connection=Redis.from_url(s.redis_url))
    present = {str(j.args[0]) for j in queue.jobs if j.args}
    db = session_scope()
    try:
        orphans = [j.id for j in db.scalars(select(Job).where(Job.status.in_(["queued", "running"]))).all() if j.id not in present]
    finally:
        db.close()
    for job_id in orphans:
        queue.enqueue("app.jobs.run_job", job_id, job_timeout=600)
    if orphans:
        log(logger, logging.WARNING, "re-enqueued orphaned jobs", count=len(orphans))
    return len(orphans)
