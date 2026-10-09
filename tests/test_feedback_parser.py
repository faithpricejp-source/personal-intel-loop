from __future__ import annotations

from personal_intel_loop.feedback import parse_digest_feedback

SAMPLE_DIGEST = """<!-- PIL_DIGEST date=2026-04-19 ranking_version=v1_trust_recency -->
# Intel Loop Digest 2026-04-19

<!-- PIL_ITEM_START item_id=rss:item-1 source=rss_briefing:feed -->
## [rss:item-1] Example

### Feedback
- [x] ✅ promote-to-SRC <!-- pil_action=promote_to_src -->
- [ ] 🚀 promote-to-EVD <!-- pil_action=promote_to_evd -->
- [x] 👍 more like this <!-- pil_action=more_like_this -->
- [ ] 👎 less like this <!-- pil_action=less_like_this -->
- [ ] 🗑️ spam / 事实错误 <!-- pil_action=spam_or_false -->
- [ ] 📌 稍后再看 <!-- pil_action=later -->
<!-- PIL_PROCESSED event_ids= scanned_at_utc= -->
<!-- PIL_ITEM_END -->
"""


def test_digest_round_trip_with_no_checked_boxes_yields_zero_events():
    digest = SAMPLE_DIGEST.replace("[x]", "[ ]")
    assert parse_digest_feedback(digest) == []


def test_checked_box_parse_matches_expected_event_set():
    events = parse_digest_feedback(SAMPLE_DIGEST)
    assert {(event.item_id, event.action) for event in events} == {
        ("rss:item-1", "promote_to_src"),
        ("rss:item-1", "more_like_this"),
    }


def test_duplicate_parse_skips_existing_processed_ids():
    first = parse_digest_feedback(SAMPLE_DIGEST)
    digest_with_processed = SAMPLE_DIGEST.replace(
        "<!-- PIL_PROCESSED event_ids= scanned_at_utc= -->",
        f"<!-- PIL_PROCESSED event_ids={first[0].event_id} scanned_at_utc=2026-04-19T00:00:00Z -->",
    )
    second = parse_digest_feedback(digest_with_processed)
    assert {(event.item_id, event.action) for event in second} == {
        ("rss:item-1", "more_like_this"),
    }


REASON_CODE_DIGEST = """<!-- PIL_DIGEST date=2026-08-26 ranking_version=v2_vault_aligned -->
# Intel Loop Digest 2026-08-26

<!-- PIL_ITEM_START item_id=rss:item-9 source=rss_briefing:dwarkesh_podcast -->
## [rss:item-9] Can distillation be stopped

### Feedback
- [ ] 🧠 早知道 <!-- pil_action=already_known -->
- [x] 🌫️ 没看懂 <!-- pil_action=unclear -->
- [x] 📌 留 <!-- pil_action=keep -->
- [ ] 💬 深挖 (生成 staging/discuss_*.md 上下文包) <!-- pil_action=deep_discuss -->
<!-- PIL_PROCESSED event_ids= scanned_at_utc= -->
<!-- PIL_ITEM_END -->
"""


def test_reason_code_checkboxes_parse_and_validate():
    events = parse_digest_feedback(REASON_CODE_DIGEST)
    assert {(event.item_id, event.action) for event in events} == {
        ("rss:item-9", "unclear"),
        ("rss:item-9", "keep"),
    }
    from personal_intel_loop.schemas import weight_for

    assert {event.action: weight_for(event.action) for event in events} == {"unclear": -0.5, "keep": 1.0}


def test_reason_code_status_mapping():
    from personal_intel_loop.feedback import _status_for_action

    assert _status_for_action("keep") == "reviewed"
    assert _status_for_action("deep_discuss") == "reviewed"
    assert _status_for_action("already_known") == "rejected"
    assert _status_for_action("unclear") == "rejected"
    # legacy 仍可用
    assert _status_for_action("less_like_this") == "rejected"


def test_not_interested_reason_code():
    from personal_intel_loop.feedback import _status_for_action
    from personal_intel_loop.schemas import weight_for

    assert weight_for("not_interested") == -0.5
    assert _status_for_action("not_interested") == "rejected"
    md = REASON_CODE_DIGEST.replace("- [ ] 🧠 早知道 <!-- pil_action=already_known -->",
                                    "- [x] 🚫 不感兴趣 <!-- pil_action=not_interested -->")
    assert ("rss:item-9", "not_interested") in {(e.item_id, e.action) for e in parse_digest_feedback(md)}
