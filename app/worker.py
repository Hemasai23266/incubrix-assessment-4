"""RQ worker entrypoint: `python -m app.worker`."""
from redis import Redis
from rq import Queue, Worker

from app.config import get_settings
from app.db import init_db
from app.logging_setup import setup_logging

if __name__ == "__main__":
    setup_logging()
    init_db()
    s = get_settings()
    conn = Redis.from_url(s.redis_url)
    Worker([Queue("incubrix", connection=conn)], connection=conn).work()
