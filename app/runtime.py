"""Connector Runtime: the only layer that writes to the database. Orchestrates
validate -> rules -> idempotent publish -> persist, plus certification."""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import rules, storage
from app.config import get_settings
from app.connectors.registry import get_connector
from app.db import Certification, ConnectorState, Destination, Post, new_id, utcnow
from app.errors import AppError
from app.logging_setup import log

logger = logging.getLogger("incubrix.runtime")

SIM_CREDENTIALS: dict[str, dict[str, Any]] = {
    "discourse": {"token": "sim-api-key", "username": "incubrix"},
    "lemmy": {"username": "incubrix", "password": "sim-pass"},
    "devto": {"token": "sim-devto-key"},
    "ghost": {"token": "64f0aa11:" + "ab" * 32},
    "webhook": {"secret": "sim-webhook-secret"},
    "assisted": {},
}


SIM_INSTANCE = {"webhook": "partner-site"}


def dest_to_dict(d: Destination) -> dict[str, Any]:
    cols = ["id", "name", "url", "platform_key", "surface", "category", "audience_tags", "language", "geo",
            "est_audience", "rules_url", "frequency_cap", "self_promo_allowed", "link_policy",
            "disclosure_required", "disclosure_text", "automation_allowed", "connector_key", "relevance_score",
            "relevance_reason", "level", "status", "credentials_ref", "options", "certified"]
    out = {c: getattr(d, c) for c in cols}
    out["last_certified_at"] = d.last_certified_at.isoformat() if d.last_certified_at else None
    return out


_REF_RE = re.compile(r"^[A-Za-z0-9_]{1,100}$")


def read_secret(ref: str) -> str:
    """Resolve a credentials_ref NAME to its secret. A seeded file in CREDENTIALS_DIR (a read-only local volume,
    like Docker secrets) wins; otherwise the environment variable of that name. The value is never logged."""
    if not _REF_RE.match(ref or ""):
        return ""
    d = get_settings().credentials_dir
    if d:
        f = Path(d) / ref
        try:
            if f.is_file() and f.resolve().parent == Path(d).resolve():
                v = f.read_text(encoding="utf-8").strip()
                if v:
                    return v
        except OSError:
            pass
    return os.environ.get(ref, "")


def resolve_connection(dest: Destination) -> dict[str, Any]:
    """Credentials come from the environment (NAME stored in the row), never from the repository/DB."""
    s = get_settings()
    key = dest.connector_key
    opts = dest.options or {}
    secret = read_secret(dest.credentials_ref) if dest.credentials_ref else ""
    conn: dict[str, Any] = {"base_url": opts.get("base_url") or (f"http://mock/{opts.get('mock_instance', SIM_INSTANCE.get(key, key))}" if s.simulation_mode else ""),
                            "username": opts.get("username", "")}
    if s.simulation_mode:
        conn.update(SIM_CREDENTIALS.get(key, {}))
        conn["username"] = conn.get("username") or opts.get("username", "")
        if secret:  # tests may inject e.g. "expired-token" through the env var
            field = {"webhook": "secret", "lemmy": "password"}.get(key, "token")
            conn[field] = secret
        return conn
    if key != "assisted" and not secret:
        raise AppError("CONNECTION_AUTH_EXPIRED",
                       f"credential env var '{dest.credentials_ref or '(none set)'}' is empty; reconnect the destination",
                       {"credentials_ref": dest.credentials_ref}, 401)
    field = {"webhook": "secret", "lemmy": "password"}.get(key, "token")
    conn[field] = secret
    return conn


def _state(db: Session, key: str) -> ConnectorState:
    st = db.get(ConnectorState, key)
    if st is None:
        st = ConnectorState(key=key)
        db.add(st)
        db.commit()
    return st


def _check_connector_enabled(db: Session, key: str) -> ConnectorState:
    st = _state(db, key)
    if st.disabled:
        raise AppError("CONNECTOR_DISABLED", f"connector '{key}' is disabled by the kill switch", {"connector": key}, 503)
    if st.consecutive_failures >= get_settings().unavailable_stop_after:
        raise AppError("PLATFORM_UNAVAILABLE",
                       f"stopped calling '{key}' after {st.consecutive_failures} consecutive failures; reset it to resume",
                       {"connector": key, "consecutive_failures": st.consecutive_failures}, 503, retryable=False)
    return st


def _record_outcome(db: Session, st: ConnectorState, ok: bool, err: AppError | None = None) -> None:
    if ok:
        st.consecutive_failures = 0
    elif err is not None and err.code == "PLATFORM_UNAVAILABLE":
        st.consecutive_failures += 1
    db.commit()


def _call_with_backoff(fn, warnings: list[str]):
    """On 429 sleep for the platform's reset time if it is short, otherwise surface it for rescheduling."""
    s = get_settings()
    try:
        return fn()
    except AppError as e:
        if e.code == "PLATFORM_RATE_LIMIT":
            wait = float(e.details.get("retry_after_seconds", 1))
            if wait <= s.max_inline_backoff_seconds:
                warnings.append(f"429 from platform; backed off {wait:.1f}s (platform reset time) and retried once")
                time.sleep(wait)
                return fn()
            e.details["retry_at"] = (utcnow() + timedelta(seconds=wait)).isoformat()
        raise


SERVICE_VERSION = "1.0.0"
MOCK_VERSION = "in-process mock (app/mocks/platforms.py)"


def make_manifest(kind: str, started: float, warnings: list[str] | None = None, *, connector_key: str | None = None,
                  rendition: dict | None = None, rights: dict | None = None, providers: list[dict] | None = None,
                  extra_settings: dict | None = None, step_timings: dict | None = None, extra: dict | None = None) -> dict:
    """The JSON manifest attached to EVERY job result (publish, assist pack, certification, cleanup, media fetch, blocked/rescheduled).
    Unknown or inapplicable values are null/empty, never invented: no AI model is used (models: []), the cost is 0.0 because no paid
    service is called, and `seed` is null because nothing here is random."""
    s = get_settings()
    connector = None
    if connector_key:
        c = get_connector(connector_key)
        connector = {"key": c.key, "version": c.manifest["version"], "tier": c.manifest["tier"]}
        if providers is None:
            providers = [{"name": "mock-platform" if s.simulation_mode else c.key, "mode": "simulation" if s.simulation_mode else "live",
                          "version": MOCK_VERSION if s.simulation_mode else None}]
    timings: dict = {"total_s": round(time.time() - started, 4)}
    if step_timings:
        timings["steps"] = step_timings
    if rights is None:
        r = rendition or {}
        rights = {"licence": "content supplied by the submitter; IncuBrix adds no third-party media",
                  "ai_generated": bool(r.get("ai_generated")), "disclosure_included": bool(r.get("disclosure"))}
    m = {"schema_version": 1, "kind": kind, "service": {"name": "incubrix-longtail-distribution", "version": SERVICE_VERSION},
         "connector": connector, "providers": providers or [], "models": [],
         "settings": {"simulation_mode": s.simulation_mode, "job_backend": s.job_backend, **(extra_settings or {})},
         "seed": None, "timings": timings, "cost_estimate_usd": 0.0, "cost_basis": "no paid API or model is used",
         "warnings": list(warnings or []), "rights": rights}
    if extra:
        m.update(extra)
    return m


def build_manifest(connector_key: str, started: float, warnings: list[str], rendition: dict, extra: dict | None = None) -> dict:
    """Publish / assist-pack manifest (kept for existing callers)."""
    return make_manifest("publish", started, warnings, connector_key=connector_key, rendition=rendition, extra=extra)


def publish_flow(db: Session, destination_id: str, rendition: dict, idempotency_key: str,
                 is_test: bool = False) -> dict[str, Any]:
    started = time.time()
    warnings: list[str] = []
    dest = db.get(Destination, destination_id)
    if dest is None:
        raise AppError("NOT_FOUND", f"destination '{destination_id}' not found", {}, 404)
    connector = get_connector(dest.connector_key)

    # Idempotent replay: a finished post for this key is returned as-is, never re-published.
    existing = db.scalars(select(Post).where(Post.idempotency_key == idempotency_key)).first()
    if existing is not None and existing.state == "published":
        return {"status": "published", "platform_post_id": existing.platform_post_id, "live_url": existing.live_url,
                "replayed": True,
                "manifest": build_manifest(dest.connector_key, started, ["idempotent replay: returned the original post"], rendition)}

    st = _check_connector_enabled(db, dest.connector_key)
    ddict = dest_to_dict(dest)

    if not dest.automation_allowed and dest.connector_key != "assisted":
        raise AppError("DEST_RULE_VIOLATION", f"'{dest.name}' does not allow automated posting; use an assisted destination",
                       {"rule": "automation_allowed"}, 422)

    # 1. format limits
    res = connector.validate(rendition, ddict, None)
    if not res["ok"]:
        raise AppError("DEST_SPEC_VIOLATION", "content breaks the destination's format limits",
                       {"violations": res["violations"]}, 422)

    # 2. posting rules (before EVERY post). Test posts skip cap/promo but never skip destination status.
    verdict = rules.evaluate(db, dest, rendition if not is_test else {**rendition, "is_promotional": False},
                             idempotency_key=idempotency_key, record=True) if not is_test else None
    if verdict is not None and verdict.action != "allow":
        first = verdict.violations[0]
        code = first.get("code") or ("DEST_RULE_FREQUENCY_CAP" if verdict.action == "reschedule" else "DEST_RULE_VIOLATION")
        raise AppError(code, first["reason"], {"verdict": verdict.as_dict(), "reschedule_at": verdict.as_dict()["reschedule_at"]},
                       429 if verdict.action == "reschedule" else 422)

    if not dest.certified and not is_test:
        warnings.append("destination is not certified yet; run POST /api/v1/destinations/{id}/certify")

    # 3. exactly-once record per idempotency key (DB uniqueness guards concurrent duplicates)
    if existing is None:
        try:
            existing = Post(idempotency_key=idempotency_key, destination_id=dest.id, state="pending",
                            is_test=is_test, promotional=bool(rendition.get("is_promotional")))
            db.add(existing)
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.scalars(select(Post).where(Post.idempotency_key == idempotency_key)).one()
            if existing.state == "published":
                return {"status": "published", "platform_post_id": existing.platform_post_id,
                        "live_url": existing.live_url, "replayed": True,
                        "manifest": build_manifest(dest.connector_key, started, ["idempotent replay"], rendition)}

    # 4. assisted destinations produce a pack instead of posting
    if dest.connector_key == "assisted":
        pack = connector.build_assist_pack(rendition, ddict)
        link = storage.put_json(f"assist-packs/{idempotency_key}.json", pack)
        existing.state = "published"
        existing.platform_post_id = f"assist:{idempotency_key}"
        existing.live_url = ""
        db.commit()
        return {"status": "assist_pack_ready", "platform_post_id": existing.platform_post_id, "live_url": "",
                "assist_pack": pack, "download_url": link,
                "manifest": build_manifest("assisted", started, warnings + ["a person must post manually"], rendition)}

    # 5. publish
    conn = resolve_connection(dest)
    try:
        out = _call_with_backoff(lambda: connector.publish(rendition, ddict, conn, idempotency_key), warnings)
    except AppError as e:
        _record_outcome(db, st, False, e)
        # a timeout may have created the post on the platform -> keep the row 'unknown' so a retry with the
        # SAME key reconciles instead of creating a second post
        existing.state = "unknown" if (e.code == "PLATFORM_UNAVAILABLE" and e.details.get("possible_post_created")) else "failed"
        db.commit()
        if existing.state == "unknown":
            warnings.append("publish timed out; post state unknown - retry with the same Idempotency-Key")
            e.details["state"] = "unknown"
            e.details["retry_with_same_idempotency_key"] = True
        raise
    existing.state = "published"
    existing.platform_post_id = out["platform_post_id"]
    existing.live_url = out["live_url"]
    db.commit()
    _record_outcome(db, st, True)
    log(logger, logging.INFO, "published", destination=dest.id, connector=dest.connector_key, post=existing.platform_post_id)
    return {"status": out["status"], "platform_post_id": out["platform_post_id"], "live_url": out["live_url"],
            "replayed": False, "manifest": build_manifest(dest.connector_key, started, warnings, rendition)}


def _is_gone(e: AppError) -> bool:
    return e.details.get("platform_status") in (404, 410)


def certify(db: Session, destination_id: str) -> dict[str, Any]:
    """Publish a test post, confirm it is live, read its status, delete it, verify deletion, store evidence."""
    t0 = time.time()
    s = get_settings()
    dest = db.get(Destination, destination_id)
    if dest is None:
        raise AppError("NOT_FOUND", f"destination '{destination_id}' not found", {}, 404)
    connector = get_connector(dest.connector_key)
    ddict = dest_to_dict(dest)
    steps: list[dict[str, Any]] = []
    run_key = f"cert-{dest.id}-{new_id()}"  # unique per run: two certifications in one second must not collide
    evidence: dict[str, Any] = {"destination_id": dest.id, "destination": dest.name, "connector": dest.connector_key,
                                "environment": "simulation" if s.simulation_mode else "local-instance",
                                "started_at": utcnow().isoformat(), "steps": steps}
    status, failure = "passed", None
    post_id: str | None = None
    conn: dict[str, Any] = {}

    def step(name: str, fn):
        ts = time.time()
        try:
            r = fn()
            steps.append({"step": name, "ok": True, "seconds": round(time.time() - ts, 4), "response": r})
            return r
        except AppError as e:
            steps.append({"step": name, "ok": False, "seconds": round(time.time() - ts, 4), "error": e.to_dict()["error"]})
            raise

    try:
        _check_connector_enabled(db, dest.connector_key)
        if dest.status == "revoked":
            raise AppError("DEST_PERMISSION_REVOKED", "partner revoked permission", {}, 403)
        rend = {"title": f"IncuBrix certification test post {int(t0)}", "body": "Automated certification test post. It is deleted immediately.",
                "tags": ["test"], "media": [], "language": dest.language}
        if dest.disclosure_required:
            rend["ai_generated"] = False
        if dest.connector_key == "assisted":
            def mk():
                pack = connector.build_assist_pack(rend, ddict)
                bad = [k for k in ("files", "caption", "deep_link", "checklist") if not pack.get(k)]
                if bad:
                    raise AppError("CONTRACT_VIOLATION", f"assist pack missing {bad}", {"missing": bad}, 502)
                storage.put_json(f"assist-packs/{run_key}.json", pack)
                return {"files": len(pack["files"]), "checklist_items": len(pack["checklist"]), "deep_link": pack["deep_link"]}
            step("build_assist_pack", mk)
        else:
            conn = resolve_connection(dest)
            step("validate", lambda: connector.validate(rend, ddict, conn))
            pub = step("publish", lambda: connector.publish(rend, ddict, conn, run_key))
            post_id = pub["platform_post_id"]
            db.add(Post(idempotency_key=run_key, destination_id=dest.id, state="published",
                        is_test=True, platform_post_id=post_id, live_url=pub["live_url"]))
            db.commit()
            live = step("get_status_live", lambda: connector.get_status(post_id, conn))
            if live["status"] not in ("published", "draft") or not live.get("live_url"):
                raise AppError("CONTRACT_VIOLATION", f"post is not live after publish: {live}", {"status": live}, 502)
            step("delete", lambda: connector.delete(post_id, conn))

            def verify_deleted():
                try:
                    r = connector.get_status(post_id, conn)
                except AppError as e:
                    if _is_gone(e):
                        return {"status": "gone (404)"}
                    raise
                if r["status"] not in ("deleted", "unpublished"):
                    raise AppError("CONTRACT_VIOLATION", f"post still live after delete: {r}", {"status": r}, 502)
                return r
            step("verify_deleted", verify_deleted)
            p = db.scalars(select(Post).where(Post.idempotency_key == run_key)).first()
            if p:
                p.state, p.deleted_at = "deleted", utcnow()
                db.commit()
    except AppError as e:
        status, failure = "failed", e.to_dict()["error"]
        # clean up a half-finished test post right away (never leave junk on a destination)
        post_id = post_id or e.details.get("platform_post_id_hint")
        if post_id and not any(x["step"] == "delete" and x["ok"] for x in steps):
            try:
                connector.delete(post_id, conn)
                steps.append({"step": "cleanup_delete", "ok": True})
            except AppError as ce:
                steps.append({"step": "cleanup_delete", "ok": False, "error": ce.to_dict()["error"]})

    evidence.update({"status": status, "failure": failure, "duration_s": round(time.time() - t0, 4),
                     "finished_at": utcnow().isoformat(), "live_urls_seen": [x.get("response", {}).get("live_url") for x in steps
                                                                                if isinstance(x.get("response"), dict) and x["response"].get("live_url")]})
    cert_id = Certification(destination_id=dest.id, connector_key=dest.connector_key, status=status,
                            environment=evidence["environment"], duration_s=evidence["duration_s"], evidence=evidence)
    db.add(cert_id)
    db.flush()
    cert_id.evidence_url = storage.put_json(f"certifications/{dest.id}/{cert_id.id}.json", evidence)
    if status == "passed":
        dest.certified, dest.status = True, "active"
        dest.last_certified_at = utcnow()
        dest.level = "L1" if dest.connector_key == "assisted" else ("L4" if "metrics" in connector.manifest["capabilities"] and False else "L3")
    else:
        dest.certified = False
        dest.level = "L0"
        if dest.status != "revoked":
            dest.status = "demoted"  # failed certification => demoted and excluded from publishing
    db.commit()
    step_timings = {x["step"]: x.get("seconds") for x in steps}
    cert_rights = {"licence": "not applicable: synthetic test content generated by the harness, deleted after the run",
                   "ai_generated": False, "disclosure_included": False}
    manifest = make_manifest("certification", t0, [f"certification failed: {failure['code']}"] if failure else [],
                             connector_key=dest.connector_key, rights=cert_rights, step_timings=step_timings,
                             extra_settings={"environment": evidence["environment"]})
    return {"certification_id": cert_id.id, "status": status, "destination_id": dest.id, "evidence_url": cert_id.evidence_url,
            "duration_s": evidence["duration_s"], "failure": failure, "steps": steps, "manifest": manifest}


def cleanup_result(db: Session, started: float) -> dict:
    """Result of the cleanup job, with the standard manifest."""
    n = cleanup_stale_test_posts(db)
    rights = {"licence": "not applicable: only deletes expired test posts", "ai_generated": False, "disclosure_included": False}
    return {"deleted": n, "manifest": make_manifest("cleanup", started, [], rights=rights,
                                                    extra_settings={"max_test_post_lifetime_seconds": get_settings().max_test_post_lifetime_seconds})}


def cleanup_stale_test_posts(db: Session) -> int:
    """Safety net: any test post still published after MAX_TEST_POST_LIFETIME_SECONDS is deleted."""
    s = get_settings()
    cutoff = utcnow() - timedelta(seconds=s.max_test_post_lifetime_seconds)
    n = 0
    for p in db.scalars(select(Post).where(Post.is_test.is_(True), Post.state == "published")).all():
        created = p.created_at if p.created_at.tzinfo else p.created_at.replace(tzinfo=cutoff.tzinfo)
        if created <= cutoff and p.platform_post_id:
            dest = db.get(Destination, p.destination_id)
            try:
                get_connector(dest.connector_key).delete(p.platform_post_id, resolve_connection(dest))
                p.state, p.deleted_at = "deleted", utcnow()
                n += 1
            except AppError:
                pass
    db.commit()
    return n
