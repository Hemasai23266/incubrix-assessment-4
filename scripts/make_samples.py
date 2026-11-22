"""Generate the batch-runner sample inputs into samples/ (run once; outputs are committed)."""
import json
import pathlib

d = pathlib.Path(__file__).resolve().parents[1] / "samples"
d.mkdir(exist_ok=True)


def dest(name, conn, surface, **kw):
    base = {"name": name, "url": f"https://{name.lower().replace(' ', '-')}.example", "platform_key": conn,
            "surface": surface, "connector_key": conn, "self_promo_allowed": True, "options": kw.pop("options", {})}
    base.update(kw)
    return base


R = {"title": "Shipping our new video workflow today", "body": "We built a faster way to publish video to many communities."}
cases = {
    "01_discourse_publish": {"destination": dest("Sample Discourse", "discourse", "topic"), "rendition": R},
    "02_lemmy_publish": {"destination": dest("Sample Lemmy", "lemmy", "post", options={"community": "incubrix_test"}), "rendition": R},
    "03_devto_publish": {"destination": dest("Sample Devto", "devto", "article"),
                         "rendition": {**R, "tags": ["ai", "video"], "link": "https://incubrix.example/post"}},
    "04_ghost_publish": {"destination": dest("Sample Ghost", "ghost", "post"), "rendition": R},
    "05_webhook_publish": {"destination": dest("Sample Partner", "webhook", "partner_post"), "rendition": R},
    "06_assisted_pack": {"destination": dest("Sample Manual Community", "assisted", "manual_post", automation_allowed=False,
                                             rules_url="https://example.org/rules", options={"submit_url": "https://example.org/submit"}),
                         "rendition": R},
    "07_certify_discourse": {"action": "certify", "destination": dest("Cert Discourse", "discourse", "topic")},
    "08_title_too_long": {"destination": dest("Short Title Lemmy", "lemmy", "post", options={"community": "x"}),
                          "rendition": {**R, "title": "x" * 300}, "expect": "failed:DEST_SPEC_VIOLATION"},
    "09_promo_blocked": {"destination": dest("No Promo Lemmy", "lemmy", "post", self_promo_allowed=False, options={"community": "x"}),
                         "rendition": {**R, "is_promotional": True}, "expect": "failed:DEST_RULE_VIOLATION"},
    "10_ai_disclosure_missing": {"destination": dest("Disclosure Forum", "discourse", "topic", disclosure_required=True),
                                 "rendition": {**R, "ai_generated": True}, "expect": "failed:DEST_RULE_VIOLATION"},
    "11_content_rejected": {"destination": dest("Rejecting Devto", "devto", "article"),
                            "rendition": {**R, "title": R["title"] + " [[reject]]"}, "expect": "failed:PLATFORM_CONTENT_REJECTED"},
    "12_non_english": {"destination": dest("Hindi Forum", "discourse", "topic", language="hi"),
                       "rendition": {"title": "नमस्ते दुनिया — यह एक लंबा शीर्षक है", "body": "यह परीक्षण पाठ है", "language": "hi"}},
}
for k, v in cases.items():
    v["name"] = k
    (d / f"{k}.json").write_text(json.dumps(v, indent=2, ensure_ascii=False), encoding="utf-8")
print(len(cases), "samples written")
