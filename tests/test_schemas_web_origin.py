from personal_intel_loop.schemas import compute_feedback_event_id


def test_web_event_id_is_stable_and_distinct_from_checkbox():
    web = compute_feedback_event_id(origin="web", digest_stem="intel_loop_digest_2026-08-26", item_id="rss:a", action="keep")
    web2 = compute_feedback_event_id(origin="web", digest_stem="intel_loop_digest_2026-08-26", item_id="rss:a", action="keep")
    checkbox = compute_feedback_event_id(origin="digest_checkbox", digest_stem="intel_loop_digest_2026-08-26", item_id="rss:a", action="keep")
    assert web == web2 and web != checkbox
