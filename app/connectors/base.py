"""Connector contract (Appendix A). Connectors return results only; they never touch the database."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from app.config import get_settings
from app.errors import AppError

MANIFEST_DIR = Path(__file__).parent / "manifests"
REQUIRED_MANIFEST_KEYS = ["key", "version", "tier", "auth", "surfaces", "rate_limits", "capabilities", "error_map"]
TIERS = {"native", "protocol", "bridge", "feed", "assisted"}
AUTHS = {"oauth2", "api_key", "bot_token", "none"}


def load_manifest(key: str) -> dict[str, Any]:
    data = yaml.safe_load((MANIFEST_DIR / f"{key}.yaml").read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED_MANIFEST_KEYS if k not in data]
    if missing:
        raise ValueError(f"manifest {key} missing keys {missing}")
    if data["tier"] not in TIERS or data["auth"] not in AUTHS:
        raise ValueError(f"manifest {key} has invalid tier/auth")
    return data


def make_http_client(connection: dict[str, Any], connector_key: str) -> httpx.Client:
    """Simulation mode talks to in-process mock platforms through the SAME http code path
    the real platform would use; real mode talks to destination.options.base_url."""
    s = get_settings()
    base_url = connection.get("base_url", "")
    timeout = s.http_timeout_seconds
    if s.simulation_mode:
        from fastapi.testclient import TestClient
        from app.mocks.platforms import mock_app
        base = base_url or f"http://mock/{connector_key}"
        # TestClient is an httpx.Client subclass wired to the in-process ASGI mock
        return TestClient(mock_app, base_url=base, raise_server_exceptions=False)
    if not base_url:
        raise AppError("DEST_SPEC_VIOLATION", "connection.base_url is required",
                       {"field": "base_url"}, 422)
    return httpx.Client(base_url=base_url, timeout=timeout)


class BaseConnector:
    key = ""
    default_error_map = {"429": "PLATFORM_RATE_LIMIT", "401": "CONNECTION_AUTH_EXPIRED",
                         "403": "CONNECTION_AUTH_EXPIRED", "422": "PLATFORM_CONTENT_REJECTED",
                         "5xx": "PLATFORM_UNAVAILABLE"}

    def __init__(self) -> None:
        self.manifest = load_manifest(self.key)

    # ---- helpers -------------------------------------------------------
    def surface_spec(self, destination: dict[str, Any]) -> dict[str, Any]:
        wanted = destination.get("surface")
        for sf in self.manifest["surfaces"]:
            if sf["key"] == wanted:
                return sf["spec"]
        return self.manifest["surfaces"][0]["spec"]

    def client(self, connection: dict[str, Any]) -> httpx.Client:
        return make_http_client(connection, self.key)

    def request(self, client: httpx.Client, method: str, url: str, **kw: Any) -> httpx.Response:
        """One place where HTTP failures become shared error codes."""
        try:
            resp = client.request(method, url, **kw)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise AppError("PLATFORM_UNAVAILABLE", f"{self.key}: network failure ({type(e).__name__})",
                           {"cause": type(e).__name__, "possible_post_created": method.upper() == "POST"},
                           503) from e
        if resp.status_code >= 400:
            err = self.map_error(resp)
            if resp.status_code >= 500 and method.upper() == "POST":
                err.details["possible_post_created"] = True  # a gateway error may hide a created post
            raise err
        return resp

    def map_error(self, resp: httpx.Response) -> AppError:
        emap = {**self.default_error_map, **{str(k): v for k, v in self.manifest["error_map"].items()}}
        status = resp.status_code
        code = emap.get(str(status)) or (emap.get("5xx") if status >= 500 else None) or "PLATFORM_CONTENT_REJECTED"
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:300]}
        message = ""
        if isinstance(body, dict):
            message = str(body.get("error") or body.get("message") or body.get("errors") or "")[:300]
        details: dict[str, Any] = {"platform_status": status, "platform_message": message}
        if status == 429:
            retry_after = _parse_reset(resp)
            details["retry_after_seconds"] = retry_after
            if resp.headers.get("X-RateLimit-Scope") == "daily":
                code = "PLATFORM_QUOTA_EXHAUSTED"
        return AppError(code, message or f"{self.key} returned HTTP {status}", details,
                        429 if status == 429 else (503 if status >= 500 else 422))

    def require_fields(self, data: Any, fields: list[str], where: str) -> None:
        """If a platform renames/removes a field, fail loudly instead of returning junk (hard case 5)."""
        missing = [f for f in fields if not isinstance(data, dict) or data.get(f) in (None, "")]
        if missing:
            hint = next((str(data[k]) for k in ("id", "topic_id", "post_id") if isinstance(data, dict) and data.get(k)), None)
            raise AppError("CONTRACT_VIOLATION",
                           f"{self.key}: response from {where} is missing expected field(s) {missing}",
                           {"missing_fields": missing, "where": where, "keys_seen": sorted(data) if isinstance(data, dict) else [],
                            "platform_post_id_hint": hint}, 502)

    # ---- contract methods ---------------------------------------------
    def validate(self, rendition: dict, destination: dict, connection: dict | None = None) -> dict:
        spec = self.surface_spec(destination)
        violations: list[dict[str, Any]] = []
        title = rendition.get("title") or ""
        body = rendition.get("body") or ""
        if len(title) > spec.get("title_max_chars", 10**9):
            violations.append({"field": "title", "reason": f"title is {len(title)} chars, limit {spec['title_max_chars']}"})
        if len(title) < spec.get("title_min_chars", 0):
            violations.append({"field": "title", "reason": f"title is {len(title)} chars, minimum {spec['title_min_chars']}"})
        if len(body) > spec.get("body_max_chars", 10**9):
            violations.append({"field": "body", "reason": f"body is {len(body)} chars, limit {spec['body_max_chars']}"})
        if not title.strip():
            violations.append({"field": "title", "reason": "title is required"})
        media = rendition.get("media") or []
        allowed = spec.get("media_types")
        if media and not allowed:
            violations.append({"field": "media", "reason": "destination accepts no media"})
        for i, m in enumerate(media):
            ct = (m or {}).get("content_type", "")
            if allowed and ct not in allowed:
                violations.append({"field": f"media[{i}]", "reason": f"media type '{ct}' not accepted; allowed {allowed}"})
        if spec.get("max_media") is not None and len(media) > spec["max_media"]:
            violations.append({"field": "media", "reason": f"{len(media)} media items, limit {spec['max_media']}"})
        if rendition.get("link") and not spec.get("links_allowed", True):
            violations.append({"field": "link", "reason": "links not allowed on this surface"})
        tags_max = spec.get("tags_max")
        if tags_max is not None and len(rendition.get("tags") or []) > tags_max:
            violations.append({"field": "tags", "reason": f"{len(rendition['tags'])} tags, limit {tags_max}"})
        return {"ok": not violations, "violations": violations}

    def publish(self, rendition: dict, destination: dict, connection: dict, idempotency_key: str) -> dict:
        raise NotImplementedError

    def get_status(self, platform_post_id: str, connection: dict) -> dict:
        raise NotImplementedError

    def delete(self, platform_post_id: str, connection: dict) -> dict:
        raise NotImplementedError

    def fetch_metrics(self, platform_post_id: str, connection: dict, since: str | None = None) -> dict:
        raise AppError("PLATFORM_CONTENT_REJECTED", f"{self.key} does not support fetch_metrics", {}, 501)

    def refresh_auth(self, connection: dict) -> dict:
        raise AppError("PLATFORM_CONTENT_REJECTED", f"{self.key} does not support refresh_auth", {}, 501)

    def build_assist_pack(self, rendition: dict, destination: dict) -> dict:
        raise AppError("PLATFORM_CONTENT_REJECTED", f"{self.key} is not an assisted connector", {}, 501)


def _parse_reset(resp: httpx.Response) -> float:
    for h in ("Retry-After", "X-RateLimit-Reset"):
        v = resp.headers.get(h)
        if v:
            try:
                val = float(v)
                # an epoch timestamp (X-RateLimit-Reset) vs. a seconds delta (Retry-After)
                return max(0.0, val - time.time()) if val > 10**9 else val
            except ValueError:
                continue
    return 1.0


def normalise_metrics(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}
    keys = ["views", "likes", "comments", "shares", "saves", "watch_time_s", "clicks"]
    return {k: raw.get(k) for k in keys}
