"""Render demo worker entrypoint: wait for Redis, re-enqueue orphaned jobs, then run an RQ SimpleWorker."""
import sys
import time

sys.path.insert(0, "/srv")

from redis import Redis  # noqa: E402
from rq import Queue, SimpleWorker  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.recovery import recover_orphans  # noqa: E402

if __name__ == "__main__":
    setup_logging()
    init_db()
    s = get_settings()
    conn = Redis.from_url(s.redis_url)
    for _ in range(60):  # supervisord starts redis first, but give it a moment
        try:
            conn.ping()
            break
        except Exception:
            time.sleep(1)
    recover_orphans()
    # SimpleWorker runs jobs in this one long-lived process instead of forking per job. The simulation-mode mock platforms keep
    # their state (created posts, "already seen" markers) in memory, so a forked child would forget it after every job and
    # a retry would not find the post its timed-out first attempt created.
    SimpleWorker([Queue("incubrix", connection=conn)], connection=conn).work()
