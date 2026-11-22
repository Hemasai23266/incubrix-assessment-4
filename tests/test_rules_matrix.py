"""Rule-safety set: every case is labelled violation / legitimate in advance. Writes evidence/rule_safety.json."""
import json
import pathlib

import pytest

from tests.conftest import H, make_dest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TITLE = "A sufficiently long test title"

# (label, destination overrides, rendition overrides, should_be_blocked_or_rescheduled)
CASES = [
    ("promo to no-promo", {"self_promo_allowed": False}, {"is_promotional": True}, True),
    ("promo to promo-ok", {"self_promo_allowed": True}, {"is_promotional": True}, False),
    ("non-promo to no-promo", {"self_promo_allowed": False}, {"is_promotional": False}, False),
    ("link to links-forbidden (body)", {"link_policy": "forbidden"}, {"body": "see https://a.example/x"}, True),
    ("link to links-forbidden (link field)", {"link_policy": "forbidden"}, {"link": "https://a.example"}, True),
    ("no link to links-forbidden", {"link_policy": "forbidden"}, {"body": "plain text"}, False),
    ("2 links to max:1", {"link_policy": "max:1"}, {"body": "https://a.example https://b.example"}, True),
    ("1 link to max:1", {"link_policy": "max:1"}, {"body": "https://a.example"}, False),
    ("same link twice to max:1", {"link_policy": "max:1"}, {"body": "https://a.example and https://a.example"}, False),
    ("AI content, disclosure required, none given", {"disclosure_required": True}, {"ai_generated": True}, True),
    ("AI content, disclosure given", {"disclosure_required": True}, {"ai_generated": True, "disclosure": "AI-assisted"}, False),
    ("AI disclosure inside body text", {"disclosure_required": True, "disclosure_text": "made with ai"},
     {"ai_generated": True, "body": "Made with AI. text"}, False),
    ("human content, disclosure required", {"disclosure_required": True}, {"ai_generated": False}, False),
    ("AI content, disclosure not required", {"disclosure_required": False}, {"ai_generated": True}, False),
    ("automation forbidden on native connector", {"automation_allowed": False}, {}, True),
    ("promo + links-forbidden + ai (all three)", {"self_promo_allowed": False, "link_policy": "forbidden", "disclosure_required": True},
     {"is_promotional": True, "link": "https://a.example", "ai_generated": True}, True),
]


def _verdict(client, dest_over, rend_over):
    d = make_dest(client, "discourse", **dest_over)
    rend = {"title": TITLE, "body": "hello world"}
    rend.update(rend_over)
    r = client.post(f"/api/v1/destinations/{d['id']}/check", json=rend, headers=H).json()
    blocked = r["rules"]["action"] != "allow"
    if dest_over.get("automation_allowed") is False:  # enforced in the publish flow, not the dry run
        from tests.conftest import job, publish
        j = job(client, publish(client, d["id"], rend_over).json()["job_id"])
        blocked = j["status"] == "failed"
    return blocked


def test_rule_safety_set(client):
    caught = missed = false_positive = correct_allow = 0
    rows = []
    for label, dest_over, rend_over, should_block in CASES:
        blocked = _verdict(client, dest_over, rend_over)
        rows.append({"case": label, "expected": "block" if should_block else "allow", "actual": "block" if blocked else "allow"})
        if should_block and blocked:
            caught += 1
        elif should_block and not blocked:
            missed += 1
        elif not should_block and blocked:
            false_positive += 1
        else:
            correct_allow += 1
    summary = {"violations_caught": caught, "violations_missed": missed, "false_positives": false_positive,
               "legitimate_allowed": correct_allow, "total_cases": len(CASES), "cases": rows}
    out = ROOT / "evidence" / "rule_safety.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    assert missed == 0 and false_positive == 0, summary


def test_frequency_cap_window_expiry(client):
    """A post older than the window no longer counts; a post inside it does."""
    from datetime import timedelta

    from app.db import Post, session_scope, utcnow
    from app.db import Destination
    from app import rules
    d = make_dest(client, "discourse", frequency_cap={"max_posts": 2, "window_hours": 24})
    db = session_scope()
    dest = db.get(Destination, d["id"])
    now = utcnow()
    db.add(Post(idempotency_key="old-1", destination_id=d["id"], state="published", created_at=now - timedelta(hours=30)))
    db.add(Post(idempotency_key="new-1", destination_id=d["id"], state="published", created_at=now - timedelta(hours=2)))
    db.commit()
    assert rules.evaluate(db, dest, {"title": TITLE}, now=now, record=False).action == "allow"  # 1 of 2 inside window
    db.add(Post(idempotency_key="new-2", destination_id=d["id"], state="published", created_at=now - timedelta(hours=1)))
    db.commit()
    v = rules.evaluate(db, dest, {"title": TITLE}, now=now, record=False)
    assert v.action == "reschedule"
    assert abs((v.reschedule_at - (now - timedelta(hours=2) + timedelta(hours=24))).total_seconds()) < 1  # frees up when 'new-1' leaves the window
    # test posts never count toward the cap
    db.add(Post(idempotency_key="t-1", destination_id=d["id"], state="published", is_test=True, created_at=now))
    db.commit()
    assert rules.evaluate(db, dest, {"title": TITLE}, now=now + timedelta(hours=23), record=False).action == "allow"
