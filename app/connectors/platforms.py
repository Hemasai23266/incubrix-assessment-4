"""The six Core connectors. One class per platform TYPE; each destination (forum, community, site) is a
catalogue row whose connection.base_url points at its instance."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from app.connectors.base import BaseConnector, normalise_metrics
from app.errors import AppError


def _tok(connection: dict) -> str:
    return connection.get("token") or ""


class DiscourseConnector(BaseConnector):
    key = "discourse"

    def _h(self, c: dict) -> dict:
        return {"Api-Key": _tok(c), "Api-Username": c.get("username", "system")}

    def publish(self, rendition, destination, connection, idempotency_key):
        body = rendition.get("body", "")
        if rendition.get("link"):
            body += f"\n\n{rendition['link']}"
        payload = {"title": rendition["title"], "raw": body}
        if (destination.get("options") or {}).get("category_id"):
            payload["category"] = destination["options"]["category_id"]
        with self.client(connection) as cl:
            r = self.request(cl, "POST", "/posts.json", json=payload,
                             headers={**self._h(connection), "Idempotency-Key": idempotency_key})
            data = r.json()
        self.require_fields(data, ["topic_id", "topic_slug"], "POST /posts.json")
        base = connection["base_url"].rstrip("/")
        return {"platform_post_id": str(data["topic_id"]),
                "live_url": f"{base}/t/{data['topic_slug']}/{data['topic_id']}", "status": "published"}

    def get_status(self, platform_post_id, connection):
        with self.client(connection) as cl:
            r = self.request(cl, "GET", f"/t/{platform_post_id}.json", headers=self._h(connection))
            data = r.json()
        self.require_fields(data, ["id", "slug"], f"GET /t/{platform_post_id}.json")
        base = connection["base_url"].rstrip("/")
        status = "deleted" if data.get("deleted_at") else ("published" if data.get("visible", True) else "hidden")
        return {"status": status, "live_url": f"{base}/t/{data['slug']}/{data['id']}"}

    def delete(self, platform_post_id, connection):
        with self.client(connection) as cl:
            self.request(cl, "DELETE", f"/t/{platform_post_id}.json", headers=self._h(connection))
        return {"deleted": True}

    def fetch_metrics(self, platform_post_id, connection, since=None):
        with self.client(connection) as cl:
            d = self.request(cl, "GET", f"/t/{platform_post_id}.json", headers=self._h(connection)).json()
        return normalise_metrics({"views": d.get("views"), "likes": d.get("like_count"),
                                  "comments": max(0, (d.get("posts_count") or 1) - 1)})


_LEMMY_JWT: dict[tuple, tuple[str, float]] = {}


class LemmyConnector(BaseConnector):
    key = "lemmy"

    def _login(self, cl, c: dict) -> dict:
        if c.get("token"):  # a pre-issued JWT
            return {"Authorization": f"Bearer {c['token']}"}
        # Lemmy rate-limits logins, so reuse the JWT for a few minutes (cache key never contains the password)
        ck = (c.get("base_url", ""), c.get("username", ""), hashlib.sha256((c.get("password") or "").encode()).hexdigest()[:12])
        hit = _LEMMY_JWT.get(ck)
        if hit and hit[1] > time.time():
            return {"Authorization": f"Bearer {hit[0]}"}
        r = self.request(cl, "POST", "/api/v3/user/login",
                         json={"username_or_email": c.get("username", ""), "password": c.get("password", "")})
        jwt = r.json().get("jwt")
        if not jwt:
            raise AppError("CONNECTION_AUTH_EXPIRED", "Lemmy login returned no jwt", {}, 401)
        _LEMMY_JWT[ck] = (jwt, time.time() + 600)
        return {"Authorization": f"Bearer {jwt}"}

    def publish(self, rendition, destination, connection, idempotency_key):
        community = (destination.get("options") or {}).get("community", "")
        with self.client(connection) as cl:
            h = self._login(cl, connection)
            cr = self.request(cl, "GET", "/api/v3/community", params={"name": community}, headers=h).json()
            self.require_fields(cr, ["community_view"], "GET /api/v3/community")
            cid = cr["community_view"]["community"]["id"]
            payload: dict[str, Any] = {"name": rendition["title"], "community_id": cid,
                                       "body": rendition.get("body", "")}
            if rendition.get("link"):
                payload["url"] = rendition["link"]
            data = self.request(cl, "POST", "/api/v3/post", json=payload,
                                headers={**h, "Idempotency-Key": idempotency_key}).json()
        self.require_fields(data, ["post_view"], "POST /api/v3/post")
        post = data["post_view"]["post"]
        self.require_fields(post, ["id"], "POST /api/v3/post -> post_view.post")
        base = connection["base_url"].rstrip("/")
        return {"platform_post_id": str(post["id"]), "live_url": f"{base}/post/{post['id']}", "status": "published"}

    def get_status(self, platform_post_id, connection):
        with self.client(connection) as cl:
            h = self._login(cl, connection)
            data = self.request(cl, "GET", "/api/v3/post", params={"id": platform_post_id}, headers=h).json()
        self.require_fields(data, ["post_view"], "GET /api/v3/post")
        post = data["post_view"]["post"]
        base = connection["base_url"].rstrip("/")
        status = "deleted" if post.get("deleted") or post.get("removed") else "published"
        return {"status": status, "live_url": f"{base}/post/{post['id']}"}

    def delete(self, platform_post_id, connection):
        with self.client(connection) as cl:
            h = self._login(cl, connection)
            self.request(cl, "POST", "/api/v3/post/delete",
                         json={"post_id": int(platform_post_id), "deleted": True}, headers=h)
        return {"deleted": True}

    def fetch_metrics(self, platform_post_id, connection, since=None):
        with self.client(connection) as cl:
            h = self._login(cl, connection)
            d = self.request(cl, "GET", "/api/v3/post", params={"id": platform_post_id}, headers=h).json()
        counts = d.get("post_view", {}).get("counts", {})
        return normalise_metrics({"likes": counts.get("upvotes"), "comments": counts.get("comments")})


class DevtoConnector(BaseConnector):
    key = "devto"

    def _h(self, c: dict) -> dict:
        return {"api-key": _tok(c), "Accept": "application/vnd.forem.api-v1+json"}

    def publish(self, rendition, destination, connection, idempotency_key):
        article = {"title": rendition["title"], "body_markdown": rendition.get("body", ""),
                   "published": True, "tags": (rendition.get("tags") or [])[:4]}
        if rendition.get("link"):
            article["canonical_url"] = rendition["link"]
        with self.client(connection) as cl:
            data = self.request(cl, "POST", "/api/articles", json={"article": article},
                                headers={**self._h(connection), "Idempotency-Key": idempotency_key}).json()
        self.require_fields(data, ["id", "url"], "POST /api/articles")
        return {"platform_post_id": str(data["id"]), "live_url": data["url"], "status": "published"}

    def get_status(self, platform_post_id, connection):
        with self.client(connection) as cl:
            d = self.request(cl, "GET", f"/api/articles/{platform_post_id}", headers=self._h(connection)).json()
        self.require_fields(d, ["id", "url"], "GET /api/articles/{id}")
        return {"status": "published" if d.get("published", True) else "unpublished", "live_url": d["url"]}

    def delete(self, platform_post_id, connection):
        # Dev.to exposes no DELETE for articles: unpublishing is the closest clean-up the API allows.
        with self.client(connection) as cl:
            self.request(cl, "PUT", f"/api/articles/{platform_post_id}", json={"article": {"published": False}},
                         headers=self._h(connection))
        return {"deleted": True, "method": "unpublish"}

    def fetch_metrics(self, platform_post_id, connection, since=None):
        with self.client(connection) as cl:
            d = self.request(cl, "GET", f"/api/articles/{platform_post_id}", headers=self._h(connection)).json()
        return normalise_metrics({"views": d.get("page_views_count"), "likes": d.get("public_reactions_count"),
                                  "comments": d.get("comments_count")})


def ghost_jwt(admin_key: str) -> str:
    """Ghost Admin API auth: HS256 JWT signed with the hex secret half of 'id:secret'."""
    kid, secret = admin_key.split(":", 1)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    now = int(time.time())
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": kid}).encode())
    payload = b64(json.dumps({"iat": now, "exp": now + 300, "aud": "/admin/"}).encode())
    sig = hmac.new(bytes.fromhex(secret), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{b64(sig)}"


class GhostConnector(BaseConnector):
    key = "ghost"

    def _h(self, c: dict) -> dict:
        t = _tok(c)
        if ":" not in t:
            raise AppError("CONNECTION_AUTH_EXPIRED", "Ghost admin key must look like '<id>:<secret>'", {}, 401)
        return {"Authorization": f"Ghost {ghost_jwt(t)}"}

    def publish(self, rendition, destination, connection, idempotency_key):
        html = rendition.get("html") or f"<p>{rendition.get('body', '')}</p>"
        if rendition.get("link"):
            html += f'<p><a href="{rendition["link"]}">{rendition["link"]}</a></p>'
        status = (destination.get("options") or {}).get("post_status", "published")
        post = {"title": rendition["title"], "html": html, "status": status}
        if rendition.get("link"):
            post["canonical_url"] = rendition["link"]
        with self.client(connection) as cl:
            data = self.request(cl, "POST", "/ghost/api/admin/posts/?source=html", json={"posts": [post]},
                                headers={**self._h(connection), "Idempotency-Key": idempotency_key}).json()
        self.require_fields(data, ["posts"], "POST /ghost/api/admin/posts/")
        p = data["posts"][0]
        self.require_fields(p, ["id", "url"], "POST /ghost/api/admin/posts/ -> posts[0]")
        return {"platform_post_id": p["id"], "live_url": p["url"], "status": p.get("status", "published")}

    def get_status(self, platform_post_id, connection):
        with self.client(connection) as cl:
            d = self.request(cl, "GET", f"/ghost/api/admin/posts/{platform_post_id}/", headers=self._h(connection)).json()
        self.require_fields(d, ["posts"], "GET /ghost/api/admin/posts/{id}/")
        p = d["posts"][0]
        return {"status": p.get("status", "published"), "live_url": p.get("url", "")}

    def delete(self, platform_post_id, connection):
        with self.client(connection) as cl:
            self.request(cl, "DELETE", f"/ghost/api/admin/posts/{platform_post_id}/", headers=self._h(connection))
        return {"deleted": True}


def sign_webhook(secret: str, timestamp: str, raw_body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


class WebhookConnector(BaseConnector):
    """Generic partner-site connector: signed JSON POST to the partner's endpoint."""
    key = "webhook"

    def _signed(self, connection: dict, method: str, body: dict | None) -> tuple[bytes, dict]:
        secret = connection.get("secret") or ""
        if not secret:
            raise AppError("CONNECTION_AUTH_EXPIRED", "webhook secret is not configured", {}, 401)
        raw = json.dumps(body or {}, sort_keys=True, separators=(",", ":")).encode()
        ts = str(int(time.time()))
        return raw, {"Content-Type": "application/json", "X-IncuBrix-Timestamp": ts,
                     "X-IncuBrix-Signature": sign_webhook(secret, ts, raw)}

    def publish(self, rendition, destination, connection, idempotency_key):
        raw, headers = self._signed(connection, "POST", {"event": "post.publish", "idempotency_key": idempotency_key,
                                                          "destination": {"id": destination.get("id"), "name": destination.get("name")},
                                                          "content": rendition})
        headers["Idempotency-Key"] = idempotency_key
        with self.client(connection) as cl:
            data = self.request(cl, "POST", "/publish", content=raw, headers=headers).json()
        self.require_fields(data, ["id", "url"], "POST /publish")
        return {"platform_post_id": str(data["id"]), "live_url": data["url"], "status": data.get("status", "published")}

    def get_status(self, platform_post_id, connection):
        raw, headers = self._signed(connection, "GET", {"id": platform_post_id})
        with self.client(connection) as cl:
            d = self.request(cl, "GET", f"/posts/{platform_post_id}", headers=headers).json()
        self.require_fields(d, ["id", "url"], "GET /posts/{id}")
        return {"status": d.get("status", "published"), "live_url": d["url"]}

    def delete(self, platform_post_id, connection):
        raw, headers = self._signed(connection, "DELETE", {"id": platform_post_id})
        with self.client(connection) as cl:
            self.request(cl, "DELETE", f"/posts/{platform_post_id}", headers=headers)
        return {"deleted": True}


class AssistedConnector(BaseConnector):
    """For communities where automated posting is NOT allowed: we prepare a pack, a human posts."""
    key = "assisted"

    def publish(self, rendition, destination, connection, idempotency_key):
        raise AppError("DEST_RULE_VIOLATION",
                       "Automated posting is not permitted for assisted destinations; use build_assist_pack.",
                       {"destination": destination.get("name")}, 422)

    def build_assist_pack(self, rendition: dict, destination: dict) -> dict:
        opts = destination.get("options") or {}
        caption = rendition.get("title", "")
        text = rendition.get("body", "")
        link = rendition.get("link", "")
        files = [{"name": "post.txt", "content_type": "text/plain",
                  "content": f"{caption}\n\n{text}\n\n{link}".strip()}]
        for i, m in enumerate(rendition.get("media") or []):
            files.append({"name": m.get("filename") or f"media_{i}", "content_type": m.get("content_type", ""),
                          "source_url": m.get("url", "")})
        checklist = [
            f"Open the community rules: {destination.get('rules_url') or 'n/a'}",
            "Confirm automated posting is not required: a person must post this manually.",
            "Self-promotion allowed here: " + ("yes" if destination.get("self_promo_allowed") else "NO - do not post promotional content"),
            f"Link policy: {destination.get('link_policy', 'allowed')}",
            "AI disclosure required: " + ("YES - include the disclosure line" if destination.get("disclosure_required") else "no"),
            f"Frequency cap: {destination.get('frequency_cap') or 'none recorded'}",
            "Post using your own account; never use shared or fake accounts.",
            "After posting, paste the live URL back into IncuBrix.",
        ]
        deep_link = opts.get("submit_url") or destination.get("url", "")
        return {"files": files, "caption": caption, "deep_link": deep_link, "checklist": checklist,
                "instructions": "Open deep_link, paste caption and post.txt, attach media, tick every checklist item, then post manually."}

    def get_status(self, platform_post_id, connection):
        return {"status": "awaiting_manual_post", "live_url": ""}

    def delete(self, platform_post_id, connection):
        return {"deleted": True}
