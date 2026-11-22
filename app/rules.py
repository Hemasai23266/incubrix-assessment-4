"""Posting-rules engine. Runs before EVERY post. Returns allow / block / reschedule with reasons."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Destination, Post, RuleEvent, utcnow

URL_RE = re.compile(r"https?://[^\s)\]>\"']+", re.I)


@dataclass
class Verdict:
    action: str = "allow"  # allow | block | reschedule
    violations: list[dict[str, Any]] = field(default_factory=list)
    reschedule_at: datetime | None = None

    def add(self, rule: str, action: str, reason: str, **extra: Any) -> None:
        self.violations.append({"rule": rule, "action": action, "reason": reason, **extra})
        # a hard block beats a reschedule
        if action == "block" or (action == "reschedule" and self.action != "block"):
            self.action = action

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "violations": self.violations,
                "reschedule_at": self.reschedule_at.isoformat() if self.reschedule_at else None}


def count_links(rendition: dict) -> int:
    text = f"{rendition.get('title', '')} {rendition.get('body', '')}"
    urls = set(URL_RE.findall(text))
    if rendition.get("link"):
        urls.add(rendition["link"])
    return len(urls)


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def evaluate(db: Session, dest: Destination, rendition: dict, now: datetime | None = None,
             idempotency_key: str = "", record: bool = True) -> Verdict:
    now = _aware(now or utcnow())
    v = Verdict()

    # 1. frequency cap (counts real, non-test posts inside the window; all times in UTC)
    cap = dest.frequency_cap or {}
    if cap.get("max_posts") and cap.get("window_hours"):
        window = timedelta(hours=float(cap["window_hours"]))
        since = now - window
        posts = db.scalars(select(Post).where(Post.destination_id == dest.id, Post.is_test.is_(False),
                                              Post.state.in_(["published", "pending", "unknown"]))).all()
        recent = sorted(_aware(p.created_at) for p in posts if _aware(p.created_at) >= since)
        if len(recent) >= int(cap["max_posts"]):
            oldest_blocking = recent[len(recent) - int(cap["max_posts"])]
            v.reschedule_at = oldest_blocking + window
            v.add("frequency_cap", "reschedule",
                  f"Frequency cap reached: {len(recent)} post(s) in the last {cap['window_hours']}h "
                  f"(max {cap['max_posts']}). Rescheduled to {v.reschedule_at.isoformat()}.",
                  code="DEST_RULE_FREQUENCY_CAP")

    # 2. self-promotion policy
    if rendition.get("is_promotional") and not dest.self_promo_allowed:
        v.add("self_promotion", "block", f"'{dest.name}' does not allow self-promotion; this post is marked promotional.")

    # 3. link policy
    n_links = count_links(rendition)
    policy = (dest.link_policy or "allowed").lower()
    if policy == "forbidden" and n_links > 0:
        v.add("link_policy", "block", f"'{dest.name}' forbids links but the post contains {n_links}.", links=n_links)
    elif policy.startswith("max:"):
        try:
            limit = int(policy.split(":", 1)[1])
        except ValueError:
            limit = 0
        if n_links > limit:
            v.add("link_policy", "block", f"Link policy allows at most {limit} link(s); post has {n_links}.", links=n_links)

    # 4. AI-content disclosure
    if dest.disclosure_required and rendition.get("ai_generated"):
        text = f"{rendition.get('title', '')} {rendition.get('body', '')} {rendition.get('disclosure', '')}".lower()
        needle = (dest.disclosure_text or "ai").lower()
        if not (rendition.get("disclosure") or needle in text):
            v.add("ai_disclosure", "block",
                  f"'{dest.name}' requires AI-content disclosure; add: \"{dest.disclosure_text}\" (rendition.disclosure).")

    # 5. destination state
    if dest.status == "revoked":
        v.add("destination_status", "block", "Partner revoked permission for this destination.",
              code="DEST_PERMISSION_REVOKED")
    elif dest.status == "demoted":
        v.add("destination_status", "block", "Destination is demoted (failed recertification); not eligible for publishing.",
              code="DEST_NOT_CERTIFIED")

    if record:
        db.add(RuleEvent(destination_id=dest.id, idempotency_key=idempotency_key, verdict=v.action,
                         violations=v.violations))
        db.commit()
    return v
