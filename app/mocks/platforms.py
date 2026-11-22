"""Mock platforms (Discourse, Lemmy, Dev.to, Ghost, partner webhook) with fault injection.

Used in-process in simulation mode and as a standalone server (`uvicorn app.mocks.platforms:mock_app`) in compose.
Fault markers in a title: [[429]] [[quota]] [[reject]] [[500]] [[drop]] (post is saved but the response is lost).
Per-instance switches in FAULTS[name]: auth_expired, rename, down, revoked.
"""
from __future__ import annotations

import hashlib
import hmac
import itertools
import time
from collections.abc import MutableMapping
from typing import Any

from app.config import get_settings

from fastapi import APIRouter, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response

INSTANCES = ["discourse", "discourse-b", "lemmy", "devto", "ghost", "partner-site", "partner-site-b"]
FAULT_DEFAULTS = {"auth_expired": False, "rename": False, "down": False, "revoked": False}


class _FaultView(MutableMapping):
    """Dict-like view of one instance's failure switches (reads and writes go through the shared store)."""

    def __init__(self, store: "SharedFaults", name: str) -> None:
        self._s, self._n = store, name

    def __getitem__(self, k: str) -> bool:
        return self._s.get(self._n)[k]

    def __setitem__(self, k: str, v: bool) -> None:
        cur = self._s.get(self._n)
        cur[k] = bool(v)
        self._s.put(self._n, cur)

    def __delitem__(self, k: str) -> None:
        raise TypeError("fault switches cannot be deleted")

    def __iter__(self):
        return iter(FAULT_DEFAULTS)

    def __len__(self) -> int:
        return len(FAULT_DEFAULTS)

    def __repr__(self) -> str:
        return repr(self._s.get(self._n))


class SharedFaults:
    """Failure switches for the mock platforms.

    In-process mode (tests, inline jobs) they live in a plain dict. With RQ the API process flips the switches but the
    WORKER process runs the mock platform, so they are kept in Redis (hash per instance) and both processes agree.
    If Redis is unreachable it falls back to the local dict, so simulation never breaks because of it."""

    def __init__(self) -> None:
        self._local: dict[str, dict[str, bool]] = {}
        self._client: Any = None

    def _redis(self):
        s = get_settings()
        if s.job_backend != "rq" or not s.redis_url:
            return None
        if self._client is None:
            from redis import Redis
            self._client = Redis.from_url(s.redis_url, socket_connect_timeout=1, socket_timeout=1, decode_responses=True)
        return self._client

    def get(self, name: str) -> dict[str, bool]:
        r = self._redis()
        if r is not None:
            try:
                raw = r.hgetall(f"incubrix:mock-faults:{name}")
                return {k: raw.get(k) == "1" for k in FAULT_DEFAULTS}
            except Exception:
                pass
        return dict(self._local.get(name, FAULT_DEFAULTS))

    def put(self, name: str, value: dict[str, bool]) -> None:
        clean = {k: bool(value.get(k, False)) for k in FAULT_DEFAULTS}
        self._local[name] = clean
        r = self._redis()
        if r is not None:
            try:
                r.hset(f"incubrix:mock-faults:{name}", mapping={k: "1" if v else "0" for k, v in clean.items()})
            except Exception:
                pass

    def __getitem__(self, name: str) -> _FaultView:
        if name not in INSTANCES:
            raise KeyError(name)
        return _FaultView(self, name)

    def __setitem__(self, name: str, value: dict[str, bool]) -> None:
        self.put(name, value)

    def __contains__(self, name: object) -> bool:
        return name in INSTANCES

    def __iter__(self):
        return iter(INSTANCES)

    def clear(self) -> None:
        self._local.clear()
        r = self._redis()
        if r is not None:
            try:
                r.delete(*[f"incubrix:mock-faults:{n}" for n in INSTANCES])
            except Exception:
                pass


FAULTS = SharedFaults()
STORES: dict[str, dict[str, Any]] = {}
WEBHOOK_SECRETS = {"partner-site": "sim-webhook-secret", "partner-site-b": "sim-webhook-secret"}
_ids = itertools.count(100)


def reset_mocks(shared: bool = True) -> None:
    if shared:  # an explicit reset also clears the switches kept in Redis; plain start-up must not undo another process's switches
        FAULTS.clear()
        for n in INSTANCES:
            FAULTS[n] = dict(FAULT_DEFAULTS)
    STORES.clear()
    for n in INSTANCES:
        STORES[n] = {"posts": {}, "idem": {}, "seen_drop": set(), "calls": 0}


reset_mocks(shared=False)


def _err(status: int, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": message, "message": message}, status_code=status, headers=headers or {})


def _gate(name: str, title: str, token: str | None, idem: str | None) -> JSONResponse | None:
    """Faults common to every platform. Returns an error response, or None to continue."""
    f = FAULTS[name]
    STORES[name]["calls"] += 1
    if f["down"]:
        return _err(503, "service unavailable")
    if f["auth_expired"] or not token or token.startswith("expired"):
        return _err(401, "invalid or expired credentials")
    seen = STORES[name].setdefault("seen_429", set())
    if "[[429]]" in title and (idem or title) not in seen:  # first attempt only: the retry after backoff succeeds
        seen.add(idem or title)
        return _err(429, "rate limited", {"Retry-After": "1"})
    if "[[429long]]" in title:
        return _err(429, "rate limited", {"Retry-After": "120"})
    if "[[quota]]" in title:
        return _err(429, "daily quota used", {"Retry-After": "3600", "X-RateLimit-Scope": "daily"})
    if "[[reject]]" in title:
        return _err(422, "Title contains content that is not allowed")
    if "[[500]]" in title:
        return _err(500, "internal error")
    return None


def _maybe_dedupe(name: str, idem: str | None):
    s = STORES[name]
    if idem and idem in s["idem"]:
        return s["posts"].get(s["idem"][idem])
    return None


def _store(name: str, idem: str | None, rec: dict) -> dict:
    s = STORES[name]
    s["posts"][rec["id"]] = rec
    if idem:
        s["idem"][idem] = rec["id"]
    return rec


def _drop_response(name: str, title: str, idem: str | None):
    """[[drop]]: first attempt is saved on the platform but the response never reaches the caller."""
    s = STORES[name]
    if "[[drop]]" in title and (idem or title) not in s["seen_drop"]:
        s["seen_drop"].add(idem or title)
        return Response(status_code=504, content=b"gateway timeout")
    return None


def build_discourse(name: str) -> APIRouter:
    r = APIRouter()

    @r.post("/posts.json")
    async def create(req: Request, api_key: str | None = Header(None, alias="Api-Key"),
                     idem: str | None = Header(None, alias="Idempotency-Key")):
        body = await req.json()
        title = body.get("title", "")
        if (e := _gate(name, title, api_key, idem)):
            return e
        if len(title) < 15 or not body.get("raw"):
            return _err(422, "Title is too short (min 15) or body is empty")
        existing = _maybe_dedupe(name, idem)
        if existing is None:
            tid = next(_ids)
            existing = _store(name, idem, {"id": tid, "slug": "t-" + str(tid), "title": title, "raw": body["raw"],
                                           "deleted_at": None, "views": 7, "like_count": 2, "posts_count": 3})
        if (d := _drop_response(name, title, idem)):
            return d
        out = {"id": existing["id"], "topic_id": existing["id"], "topic_slug": existing["slug"], "post_number": 1}
        if FAULTS[name]["rename"]:
            out["topic_ident"] = out.pop("topic_id")  # platform renamed a field
        return out

    @r.get("/t/{tid}.json")
    async def get(tid: int, api_key: str | None = Header(None, alias="Api-Key")):
        if (e := _gate(name, "", api_key, None)):
            return e
        p = STORES[name]["posts"].get(tid)
        if not p:
            return _err(404, "not found")
        return {"id": p["id"], "slug": p["slug"], "title": p["title"], "visible": p["deleted_at"] is None,
                "deleted_at": p["deleted_at"], "views": p["views"], "like_count": p["like_count"],
                "posts_count": p["posts_count"]}

    @r.delete("/t/{tid}.json")
    async def delete(tid: int, api_key: str | None = Header(None, alias="Api-Key")):
        if (e := _gate(name, "", api_key, None)):
            return e
        p = STORES[name]["posts"].get(tid)
        if not p:
            return _err(404, "not found")
        p["deleted_at"] = "now"
        return {"success": "OK"}

    return r


def build_devto(name: str) -> APIRouter:
    r = APIRouter()

    @r.post("/api/articles")
    async def create(req: Request, api_key: str | None = Header(None, alias="api-key"),
                     idem: str | None = Header(None, alias="Idempotency-Key")):
        art = (await req.json()).get("article", {})
        title = art.get("title", "")
        if (e := _gate(name, title, api_key, idem)):
            return e
        if len(art.get("tags", [])) > 4 or not art.get("body_markdown"):
            return _err(422, "Validation failed: tags max 4 / body required")
        existing = _maybe_dedupe(name, idem)
        if existing is None:
            i = next(_ids)
            existing = _store(name, idem, {"id": i, "title": title, "url": f"https://dev.example/sim/{i}",
                                           "published": True, "page_views_count": 11,
                                           "public_reactions_count": 4, "comments_count": 1})
        if (d := _drop_response(name, title, idem)):
            return d
        out = {"id": existing["id"], "url": existing["url"], "published": existing["published"]}
        if FAULTS[name]["rename"]:
            out["article_url"] = out.pop("url")
        return out

    @r.get("/api/articles/{aid}")
    async def get(aid: int, api_key: str | None = Header(None, alias="api-key")):
        if (e := _gate(name, "", api_key, None)):
            return e
        p = STORES[name]["posts"].get(aid)
        return p or _err(404, "not found")

    @r.put("/api/articles/{aid}")
    async def update(aid: int, req: Request, api_key: str | None = Header(None, alias="api-key")):
        if (e := _gate(name, "", api_key, None)):
            return e
        p = STORES[name]["posts"].get(aid)
        if not p:
            return _err(404, "not found")
        p["published"] = (await req.json()).get("article", {}).get("published", True)
        return p

    return r


def build_ghost(name: str) -> APIRouter:
    r = APIRouter()

    def _tok(auth: str | None) -> str | None:
        return auth.split(" ", 1)[1] if auth and auth.startswith("Ghost ") and auth.count(".") == 2 else None

    @r.post("/ghost/api/admin/posts/")
    async def create(req: Request, authorization: str | None = Header(None),
                     idem: str | None = Header(None, alias="Idempotency-Key")):
        posts = (await req.json()).get("posts", [{}])
        title = posts[0].get("title", "")
        if (e := _gate(name, title, _tok(authorization), idem)):
            return e
        existing = _maybe_dedupe(name, idem)
        if existing is None:
            i = f"ghost{next(_ids)}"
            existing = _store(name, idem, {"id": i, "title": title, "url": f"http://ghost.sim/{i}/",
                                           "status": posts[0].get("status", "published")})
        if (d := _drop_response(name, title, idem)):
            return d
        p = dict(existing)
        if FAULTS[name]["rename"]:
            p["permalink"] = p.pop("url")
        return JSONResponse({"posts": [p]}, status_code=201)

    @r.get("/ghost/api/admin/posts/{pid}/")
    async def get(pid: str, authorization: str | None = Header(None)):
        if (e := _gate(name, "", _tok(authorization), None)):
            return e
        p = STORES[name]["posts"].get(pid)
        return {"posts": [p]} if p else _err(404, "not found")

    @r.delete("/ghost/api/admin/posts/{pid}/")
    async def delete(pid: str, authorization: str | None = Header(None)):
        if (e := _gate(name, "", _tok(authorization), None)):
            return e
        if STORES[name]["posts"].pop(pid, None) is None:
            return _err(404, "not found")
        return Response(status_code=204)

    return r


def build_lemmy(name: str) -> APIRouter:
    r = APIRouter()
    lem = lambda a: a.split(" ", 1)[1] if a and a.startswith("Bearer ") else None

    @r.post("/api/v3/user/login")
    async def login(req: Request):
        b = await req.json()
        if FAULTS[name]["auth_expired"] or not b.get("password") or str(b["password"]).startswith("expired"):
            return _err(401, "incorrect_login")
        return {"jwt": "sim-jwt"}

    @r.get("/api/v3/community")
    async def community(req: Request):
        return {"community_view": {"community": {"id": 1, "name": req.query_params.get("name", "")}}}

    @r.post("/api/v3/post")
    async def create(req: Request, authorization: str | None = Header(None),
                     idem: str | None = Header(None, alias="Idempotency-Key")):
        b = await req.json()
        title = b.get("name", "")
        if (e := _gate(name, title, lem(authorization), idem)):
            return e
        if len(title) > 200:
            return _err(400, "post_title_too_long")
        existing = _maybe_dedupe(name, idem)
        if existing is None:
            i = next(_ids)
            existing = _store(name, idem, {"id": i, "name": title, "deleted": False, "removed": False,
                                           "counts": {"upvotes": 3, "comments": 2}})
        if (d := _drop_response(name, title, idem)):
            return d
        post = {"id": existing["id"], "name": existing["name"], "deleted": False}
        if FAULTS[name]["rename"]:
            post["post_id"] = post.pop("id")
        return {"post_view": {"post": post, "counts": existing["counts"]}}

    @r.get("/api/v3/post")
    async def get(id: int, authorization: str | None = Header(None)):
        if (e := _gate(name, "", lem(authorization), None)):
            return e
        p = STORES[name]["posts"].get(id)
        if not p:
            return _err(400, "couldnt_find_post")
        return {"post_view": {"post": {"id": p["id"], "name": p["name"], "deleted": p["deleted"], "removed": p["removed"]},
                              "counts": p["counts"]}}

    @r.post("/api/v3/post/delete")
    async def delete(req: Request, authorization: str | None = Header(None)):
        if (e := _gate(name, "", lem(authorization), None)):
            return e
        b = await req.json()
        p = STORES[name]["posts"].get(b.get("post_id"))
        if not p:
            return _err(400, "couldnt_find_post")
        p["deleted"] = bool(b.get("deleted", True))
        return {"post_view": {"post": {"id": p["id"], "deleted": p["deleted"]}}}

    return r


def build_partner(name: str) -> APIRouter:
    """Partner site that verifies IncuBrix's HMAC signature, like a real partner would."""
    r = APIRouter()

    def _verify(raw: bytes, ts: str | None, sig: str | None) -> JSONResponse | None:
        if FAULTS[name]["revoked"]:
            return _err(403, "partner revoked IncuBrix's permission")
        if not ts or not sig or abs(time.time() - int(ts)) > 300:
            return _err(401, "missing or stale timestamp")
        want = "sha256=" + hmac.new(WEBHOOK_SECRETS[name].encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
        return None if hmac.compare_digest(want, sig) else _err(401, "bad signature")

    @r.post("/publish")
    async def publish(req: Request, x_incubrix_timestamp: str | None = Header(None),
                      x_incubrix_signature: str | None = Header(None),
                      idem: str | None = Header(None, alias="Idempotency-Key")):
        raw = await req.body()
        if (e := _verify(raw, x_incubrix_timestamp, x_incubrix_signature)):
            return e
        import json as _j
        content = _j.loads(raw).get("content", {})
        title = content.get("title", "")
        if (e := _gate(name, title, "ok", idem)):
            return e
        existing = _maybe_dedupe(name, idem)
        if existing is None:
            i = next(_ids)
            existing = _store(name, idem, {"id": i, "url": f"https://partner.example/{name}/{i}", "status": "published"})
        if (d := _drop_response(name, title, idem)):
            return d
        out = dict(existing)
        if FAULTS[name]["rename"]:
            out["link"] = out.pop("url")
        return JSONResponse(out, status_code=201)

    @r.get("/posts/{pid}")
    async def get(pid: int, x_incubrix_timestamp: str | None = Header(None), x_incubrix_signature: str | None = Header(None)):
        import json as _j
        raw = _j.dumps({"id": str(pid)}, sort_keys=True, separators=(",", ":")).encode()
        if (e := _verify(raw, x_incubrix_timestamp, x_incubrix_signature)):
            return e
        return STORES[name]["posts"].get(pid) or _err(404, "not found")

    @r.delete("/posts/{pid}")
    async def delete(pid: int, x_incubrix_timestamp: str | None = Header(None), x_incubrix_signature: str | None = Header(None)):
        import json as _j
        raw = _j.dumps({"id": str(pid)}, sort_keys=True, separators=(",", ":")).encode()
        if (e := _verify(raw, x_incubrix_timestamp, x_incubrix_signature)):
            return e
        return Response(status_code=204) if STORES[name]["posts"].pop(pid, None) else _err(404, "not found")

    return r


mock_app = FastAPI(title="IncuBrix mock platforms")
_BUILDERS = {"discourse": build_discourse, "discourse-b": build_discourse, "lemmy": build_lemmy,
             "devto": build_devto, "ghost": build_ghost, "partner-site": build_partner, "partner-site-b": build_partner}
for _n, _b in _BUILDERS.items():
    mock_app.include_router(_b(_n), prefix=f"/{_n}")


@mock_app.get("/__health")
def _health():
    return {"ok": True, "instances": INSTANCES}


@mock_app.post("/__control/{instance}")
async def _control(instance: str, req: Request):
    """Flip a fault switch at runtime, e.g. {"rename": true}."""
    FAULTS[instance].update(await req.json())
    return dict(FAULTS[instance])


@mock_app.post("/__reset")
def _reset():
    reset_mocks()
    return {"ok": True}
