from __future__ import annotations

from pathlib import Path

from personal_intel_loop import RUNS_DIR
from personal_intel_loop.feedback import apply_feedback_scan
from personal_intel_loop.store import record_feedback_event, upsert_item
from tests.conftest import make_item


def test_scanning_same_digest_twice_creates_no_new_promotion_events(db_conn, tmp_path, monkeypatch):
    monkeypatch.setattr("personal_intel_loop.feedback.RUNS_DIR", tmp_path / "runs")
    item = make_item()
    with db_conn:
        upsert_item(db_conn, item, adapter_name="rss_briefing", source_payload_json="{}")
    digest_path = tmp_path / "intel_loop_digest_2026-04-19.md"
    digest_path.write_text(
        """<!-- PIL_DIGEST date=2026-04-19 ranking_version=v1_trust_recency -->
# Intel Loop Digest 2026-04-19
<!-- PIL_ITEM_START item_id=rss:test-item source=rss_briefing:reuters_top_news -->
- [x] ✅ promote-to-SRC <!-- pil_action=promote_to_src -->
<!-- PIL_PROCESSED event_ids= scanned_at_utc= -->
<!-- PIL_ITEM_END -->
""",
        "utf-8",
    )
    with db_conn:
        apply_feedback_scan(db_conn, paths=[digest_path], dry_run=False)
    first_count = db_conn.execute("SELECT COUNT(*) FROM promotion_events").fetchone()[0]
    with db_conn:
        apply_feedback_scan(db_conn, paths=[digest_path], dry_run=False)
    second_count = db_conn.execute("SELECT COUNT(*) FROM promotion_events").fetchone()[0]
    assert first_count == 1
    assert second_count == 1


def test_feedback_scan_marks_reviewed_and_rejected_statuses(db_conn, tmp_path, monkeypatch):
    monkeypatch.setattr("personal_intel_loop.feedback.RUNS_DIR", tmp_path / "runs")
    item_useful = make_item(item_id="rss:item-useful", url="https://example.com/useful")
    item_reject = make_item(item_id="rss:item-reject", url="https://example.com/reject")
    with db_conn:
        upsert_item(db_conn, item_useful, adapter_name="rss_briefing", source_payload_json="{}")
        upsert_item(db_conn, item_reject, adapter_name="rss_briefing", source_payload_json="{}")
    digest_path = tmp_path / "intel_loop_digest_2026-04-20.md"
    digest_path.write_text(
        """<!-- PIL_DIGEST date=2026-04-20 ranking_version=v2_vault_aligned -->
# Intel Loop Digest 2026-04-20
<!-- PIL_ITEM_START item_id=rss:item-useful source=rss_briefing:reuters_top_news -->
- [x] 🔥 这样的给我来一打 <!-- pil_action=useful -->
<!-- PIL_PROCESSED event_ids= scanned_at_utc= -->
<!-- PIL_ITEM_END -->
<!-- PIL_ITEM_START item_id=rss:item-reject source=rss_briefing:reuters_top_news -->
- [x] 🗑️ 浪费时间 <!-- pil_action=less_like_this -->
<!-- PIL_PROCESSED event_ids= scanned_at_utc= -->
<!-- PIL_ITEM_END -->
""",
        "utf-8",
    )
    with db_conn:
        apply_feedback_scan(db_conn, paths=[digest_path], dry_run=False)
    rows = {
        row["item_id"]: row["item_status"]
        for row in db_conn.execute(
            "SELECT item_id, item_status FROM items WHERE item_id IN ('rss:item-useful', 'rss:item-reject')"
        ).fetchall()
    }
    assert rows["rss:item-useful"] == "reviewed"
    assert rows["rss:item-reject"] == "rejected"
