"""Single-container demo support: shared fault switches, orphaned-job recovery, SQLite hardening."""
import os
import tempfile
import threading

from sqlalchemy import text

from app.mocks.platforms import FAULTS, SharedFaults


class FakeRedis:
    """Tiny in-memory stand-in for the two Redis calls the fault store uses (two 'processes' share one instance)."""
    def __init__(self):
        self.h = {}

    def hgetall(self, key):
        return dict(self.h.get(key, {}))

    def hset(self, key, mapping):
        self.h.setdefault(key, {}).update(mapping)

    def delete(self, *keys):
        for k in keys:
            self.h.pop(k, None)


def test_fault_switches_are_shared_between_processes(monkeypatch):
    shared = FakeRedis()
    api_proc, worker_proc = SharedFaults(), SharedFaults()  # two separate objects = two separate processes
    for obj in (api_proc, worker_proc):
        monkeypatch.setattr(obj, "_redis", lambda shared=shared: shared)
    assert worker_proc["discourse"]["rename"] is False
    api_proc["discourse"].update({"rename": True})  # what POST /api/v1/simulation/faults does in the API process
    assert worker_proc["discourse"]["rename"] is True  # the worker, which runs the mock platform, sees it
    api_proc["discourse"]["rename"] = False
    assert worker_proc["discourse"]["rename"] is False
    assert dict(worker_proc["ghost"]) == {"auth_expired": False, "rename": False, "down": False, "revoked": False}


def test_faults_fall_back_to_local_when_redis_is_down(monkeypatch):
    f = SharedFaults()

    class Broken:
        def hgetall(self, *a, **k):
            raise ConnectionError("down")

        hset = delete = hgetall

    monkeypatch.setattr(f, "_redis", lambda: Broken())
    f["lemmy"]["down"] = True
    assert f["lemmy"]["down"] is True  # still works in-process


def test_in_process_default_behaviour_unchanged():
    FAULTS["devto"]["rename"] = True
    assert FAULTS["devto"]["rename"] is True and "devto" in FAULTS and "nope" not in FAULTS


def test_orphaned_jobs_are_reenqueued(monkeypatch):
    from app import recovery
    from app.config import get_settings
    from app.db import Job, session_scope

    s = get_settings()
    monkeypatch.setattr(s, "job_backend", "rq")
    monkeypatch.setattr(s, "redis_url", "redis://fake")
    enq = []

    class FakeJob:
        def __init__(self, jid):
            self.args = (jid,)

    class FakeQueue:
        jobs = [FakeJob("still-queued")]

        def __init__(self, *a, **k):
            pass

        def enqueue(self, fn, job_id, **k):
            enq.append(job_id)

    monkeypatch.setattr(recovery, "Queue", FakeQueue)
    monkeypatch.setattr(recovery, "Redis", type("R", (), {"from_url": staticmethod(lambda u: object())}))
    db = session_scope()
    for jid, st in [("still-queued", "queued"), ("orphan-queued", "queued"), ("orphan-running", "running"), ("done", "succeeded"), ("bad", "failed")]:
        db.add(Job(id=jid, kind="publish", status=st, payload={}))
    db.commit()
    db.close()
    assert recovery.recover_orphans() == 2
    assert sorted(enq) == ["orphan-queued", "orphan-running"]


def test_sqlite_file_db_is_hardened_and_survives_concurrent_writers(monkeypatch):
    from app.config import reset_settings
    from app.db import Job, init_db, reset_db_engine, session_scope, get_engine
    path = os.path.join(tempfile.mkdtemp(), "c.db")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    reset_settings()
    reset_db_engine()
    init_db()
    with get_engine().connect() as c:
        assert c.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert c.execute(text("PRAGMA busy_timeout")).scalar() == 30000
    errors = []

    def writer(n):
        try:
            for i in range(25):
                db = session_scope()
                db.add(Job(id=f"w{n}-{i}", kind="publish", status="queued", payload={}))
                db.commit()
                j = db.get(Job, f"w{n}-{i}")
                j.status = "running"
                db.commit()
                db.close()
        except Exception as e:  # 'database is locked' would land here
            errors.append(repr(e))

    ts = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == []
    db = session_scope()
    assert db.query(Job).count() == 8 * 25
    db.close()
    reset_db_engine()
