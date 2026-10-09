"""summarize_day: 五种标签各一例、dwell_ratio 计算、expected_read_ms 口径、session_ms、聚合与显式评分。"""
from __future__ import annotations

from personal_intel_loop.paper_behavior import summarize_day
from tests._behavior_helpers import BEHAVIOR_DATE, TS_A, TS_B, add_edition, add_events, seed_item
from tests.conftest import make_item
from personal_intel_loop.store import upsert_item


def _conn(tmp_path):
    from personal_intel_loop.store import connect_db, ensure_schema

    c = connect_db(tmp_path / "db.sqlite")
    ensure_schema(c)
    return c


def _label_of(summary, item_id):
    return next(item for item in summary["items"] if item["item_id"] == item_id)["label"]


def test_five_labels_each_one_case(tmp_path):
    conn = _conn(tmp_path)
    try:
        # deep_read: 开了原文
        seed_item(conn, "it:orig", "rss_briefing:src_a", "源A", byline="张三")
        add_edition(conn, BEHAVIOR_DATE, "it:orig", "top", 0)
        add_events(conn, [("impression", "it:orig", 2000, {}), ("open_original", "it:orig", None, {})])
        # deep_read: dwell_ratio ≥ 0.7(全文 400 字 → 预期 60000ms, 停留 45000ms = 0.75)
        seed_item(conn, "it:deep", "rss_briefing:src_a", "源A", body="x" * 50)
        conn.execute("INSERT INTO item_fulltext (item_id, text, status, fetched_at) VALUES (?, ?, 'ok', ?)", ("it:deep", "y" * 400, TS_A))
        add_edition(conn, BEHAVIOR_DATE, "it:deep", "top", 1)
        add_events(conn, [("impression", "it:deep", 2000, {}), ("open_item", "it:deep", None, {}), ("item_dwell", "it:deep", 45000, {"max_scroll_pct": 80})], ts=TS_B)
        # read: 0.2 ≤ ratio < 0.7(预期 60000ms, 停留 30000ms = 0.5)
        seed_item(conn, "it:read", "rss_briefing:src_b", "源B", byline="李四")
        conn.execute("INSERT INTO item_fulltext (item_id, text, status, fetched_at) VALUES (?, ?, 'ok', ?)", ("it:read", "y" * 400, TS_A))
        add_edition(conn, BEHAVIOR_DATE, "it:read", "briefs", 0)
        add_events(conn, [("impression", "it:read", 2000, {}), ("open_item", "it:read", None, {}), ("item_dwell", "it:read", 30000, {"max_scroll_pct": 45})], ts=TS_B)
        # glanced: 打开但 ratio < 0.2
        seed_item(conn, "it:glance", "rss_briefing:src_b", "源B")
        add_edition(conn, BEHAVIOR_DATE, "it:glance", "briefs", 1)
        add_events(conn, [("impression", "it:glance", 2000, {}), ("open_item", "it:glance", None, {}), ("item_dwell", "it:glance", 5000, {})], ts=TS_B)
        # skipped: 曝光 ≥1500 未打开、无显式反馈
        seed_item(conn, "it:skip", "rss_briefing:src_c", "源C")
        add_edition(conn, BEHAVIOR_DATE, "it:skip", "briefs", 2)
        add_events(conn, [("impression", "it:skip", 2000, {})], ts=TS_B)
        # unseen: 曝光 < 1500
        seed_item(conn, "it:unseen", "rss_briefing:src_c", "源C")
        add_edition(conn, BEHAVIOR_DATE, "it:unseen", "briefs", 3)
        add_events(conn, [("impression", "it:unseen", 500, {})], ts=TS_B)
        conn.commit()

        summary = summarize_day(conn, date_local=BEHAVIOR_DATE)
        assert summary["date"] == BEHAVIOR_DATE
        assert _label_of(summary, "it:orig") == "deep_read"
        assert _label_of(summary, "it:deep") == "deep_read"
        assert _label_of(summary, "it:read") == "read"
        assert _label_of(summary, "it:glance") == "glanced"
        assert _label_of(summary, "it:skip") == "skipped"
        assert _label_of(summary, "it:unseen") == "unseen"
    finally:
        conn.close()


def test_dwell_ratio_and_expected_read_ms(tmp_path):
    conn = _conn(tmp_path)
    try:
        # 全文 200 字 → 200*150 = 30000ms; 停留 12000ms → ratio 0.4
        seed_item(conn, "it:r1", "rss_briefing:src_a", "源A", body="x" * 999)
        conn.execute("INSERT INTO item_fulltext (item_id, text, status, fetched_at) VALUES (?, ?, 'ok', ?)", ("it:r1", "y" * 200, TS_A))
        add_events(conn, [("open_item", "it:r1", None, {}), ("item_dwell", "it:r1", 12000, {})])
        # 无全文按 body: body 300 字 → 45000ms; 停留 9000ms → ratio 0.2 → read
        seed_item(conn, "it:r2", "rss_briefing:src_a", "源A", body="z" * 300)
        add_events(conn, [("open_item", "it:r2", None, {}), ("item_dwell", "it:r2", 9000, {})], ts=TS_B)
        # 空 body 且无全文 → 最少 30 秒
        seed_item(conn, "it:r3", "rss_briefing:src_a", "源A", body="")
        add_events(conn, [("open_item", "it:r3", None, {}), ("item_dwell", "it:r3", 60000, {})], ts=TS_B)
        conn.commit()

        summary = summarize_day(conn, date_local=BEHAVIOR_DATE)
        items = {item["item_id"]: item for item in summary["items"]}
        assert items["it:r1"]["expected_read_ms"] == 30000
        assert items["it:r1"]["dwell_ratio"] == 0.4
        assert items["it:r2"]["expected_read_ms"] == 45000
        assert items["it:r2"]["dwell_ratio"] == 0.2
        assert items["it:r3"]["expected_read_ms"] == 30000
    finally:
        conn.close()


def test_max_scroll_and_session_ms_and_date_filter(tmp_path):
    conn = _conn(tmp_path)
    try:
        seed_item(conn, "it:m", "rss_briefing:src_a", "源A")
        add_events(conn, [("item_dwell", "it:m", 1000, {"max_scroll_pct": 30})], ts=TS_A)
        add_events(conn, [("item_dwell", "it:m", 2000, {"max_scroll_pct": 70})], ts=TS_B)
        add_events(conn, [("session_end", None, 120000, {"editions_seen": 1})], ts=TS_A)
        add_events(conn, [("session_end", None, 60000, {})], ts=TS_B)
        # 前一天的曝光不算今天
        add_events(conn, [("impression", "it:m", 9999, {})], ts="2026-10-03T03:00:00Z")
        conn.commit()

        summary = summarize_day(conn, date_local=BEHAVIOR_DATE)
        item = summary["items"][0]
        assert item["max_scroll_pct"] == 70
        assert item["dwell_ms"] == 3000
        assert item["impression_ms"] == 0  # 昨天的 impression 不计
        assert summary["session_ms"] == 180000
    finally:
        conn.close()


def test_by_source_aggregation_and_ratings_surfaced(tmp_path):
    conn = _conn(tmp_path)
    try:
        seed_item(conn, "it:1", "rss_briefing:src_a", "源A", byline="张三")
        seed_item(conn, "it:2", "rss_briefing:src_a", "源A", byline="张三")
        seed_item(conn, "it:3", "rss_briefing:src_b", "源B", topic="气候")
        add_events(conn, [("impression", "it:1", 2000, {}), ("open_item", "it:1", None, {}), ("item_dwell", "it:1", 12000, {})])
        add_events(conn, [("impression", "it:2", 2000, {})], ts=TS_B)
        add_events(conn, [("impression", "it:3", 2000, {})], ts=TS_B)
        # 显式评分 + 原因码
        conn.execute(
            "INSERT OR REPLACE INTO item_ratings (item_id, dim, value, author_key, ts, distilled_at) VALUES ('it:1', 'overall', -1, NULL, ?, NULL)",
            (TS_A,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO promotion_events (event_id, item_id, source, event_type, event_weight, origin, digest_path, vault_object_type, vault_object_id, note, event_ts, created_at) VALUES ('e1', 'it:3', 'rss_briefing:src_b', 'not_interested', -0.5, 'web', NULL, NULL, NULL, NULL, ?, ?)",
            (TS_B, TS_B),
        )
        conn.commit()

        summary = summarize_day(conn, date_local=BEHAVIOR_DATE)
        assert summary["by_source"] == {
            "rss_briefing:src_a": {"read": 1, "skipped": 1},
            "rss_briefing:src_b": {"skipped": 1},
        }
        assert summary["by_author"] == {"张三": {"read": 1, "skipped": 1}, "源B": {"skipped": 1}}
        assert summary["by_topic"] == {"科学": {"read": 1, "skipped": 1}, "气候": {"skipped": 1}}
        items = {item["item_id"]: item for item in summary["items"]}
        assert items["it:1"]["ratings"] == {"overall": -1}
        assert items["it:1"]["author_key"] == "张三"
        assert items["it:3"]["reason_code"] == "not_interested"
    finally:
        conn.close()


def test_section_and_rank_taken_from_editions(tmp_path):
    conn = _conn(tmp_path)
    try:
        seed_item(conn, "it:s", "rss_briefing:src_a", "源A")
        add_edition(conn, BEHAVIOR_DATE, "it:s", "blind", 4, blind_reason="盲区句")
        add_events(conn, [("impression", "it:s", 2000, {})])
        # 版面外的条目 section/rank 为 None
        upsert_item(
            conn,
            make_item(item_id="it:off", source="rss_briefing:src_a", title="版面外"),
            adapter_name="rss_briefing",
            source_payload_json="{}",
        )
        add_events(conn, [("impression", "it:off", 2000, {})], ts=TS_B)
        conn.commit()

        summary = summarize_day(conn, date_local=BEHAVIOR_DATE)
        items = {item["item_id"]: item for item in summary["items"]}
        assert (items["it:s"]["section"], items["it:s"]["rank"]) == ("blind", 4)
        assert items["it:off"]["section"] is None and items["it:off"]["rank"] is None
    finally:
        conn.close()
