"""10-05 验收 SQL 性能四条(P03/P06/P09/P11)的修复测试。

每条两个层面(对应 AUDIT_SQLPERF 的修复要求):
- 机制: 被测真实函数发出的 SQL 必须已把过滤下推/改写(用 set_trace_callback 捕获真实执行的
  语句, 修复前是全表扫形态, 断言失败);
- 语义: 与改前行为逐条一致 —— 期望集用真实判定原语独立推导(P03), 对 item_status 全取值
  逐一判定(P06), 真实 item_id 格式上的匹配结果(P09), 新旧建档路径双库对照(P11)。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date

import pytest

from personal_intel_loop.digest import _select_rows, _select_transcript_rows
from personal_intel_loop.paper_behavior import day_event_rows, local_date_of_ts
from personal_intel_loop.schemas import FEEDBACK_EVENT_WEIGHTS, FeedbackEvent, compute_feedback_event_id
from personal_intel_loop.store import (
    _bootstrap_source_trust,
    connect_db,
    ensure_schema,
    record_feedback_event,
    recompute_source_trust,
    replace_digest_inclusions,
    upsert_item,
)
from personal_intel_loop.vault_scanner import _resolve_url_to_pil_item
from tests.conftest import make_item

NOW_P11 = "2026-10-05T00:00:00Z"


def _insert_item(conn, item_id, *, source="rss_briefing:p06", status="new", adapter="probe"):
    conn.execute(
        "INSERT INTO items (item_id, source, url, title, ts, content_hash, adapter_name, first_ingested_at, last_seen_at, item_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, source, "https://example.com/" + item_id, "t", "2026-10-01T00:00:00Z", "h-" + item_id, adapter, "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z", status),
    )


def test_p03_day_event_rows_pushes_utc_window_and_matches_per_row_filter(db_conn):
    # 同一批假事件: 覆盖当日整天与 JST 跨午夜边界(含边界整秒与带小数秒的事件)。
    # ui_events.ts 经 paper_events 归一为 UTC「Z」原文(F18), 字符串序=时间序。
    rows = [
        # (ts, item_id) —— 插入顺序与 ts 序不同, 顺带验证 ORDER BY id 保留
        ("2026-10-05T03:30:00.123Z", "rss:p03-midday"),
        ("2026-10-02T00:00:00Z", "rss:p03-other-day"),
        ("2026-10-04T14:59:59Z", "rss:p03-jst-235959"),
        ("2026-10-04T15:00:00Z", "rss:p03-jst-midnight-exact"),
        ("2026-10-04T15:00:00.500Z", "rss:p03-jst-midnight-frac"),
        ("2026-10-05T14:59:59.999Z", "rss:p03-jst-eve-frac"),
        ("2026-10-05T15:00:00Z", "rss:p03-next-midnight-exact"),
        ("2026-10-05T15:00:00.500Z", "rss:p03-next-midnight-frac"),
    ]
    with db_conn:
        for ts, item_id in rows:
            db_conn.execute(
                "INSERT INTO ui_events (session_id, ts, kind, item_id, edition_date, ms, meta_json, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("sess-p03", ts, "impression", item_id, None, None, "{}", "2026-10-06T00:00:00Z"),
            )
    captured: list[str] = []
    db_conn.set_trace_callback(captured.append)
    try:
        result = day_event_rows(db_conn, "2026-10-05")
    finally:
        db_conn.set_trace_callback(None)
    # 机制: 按日过滤必须下推成 UTC 区间条件(改前是全表 SELECT, 全部行进 Python)。
    # sqlite3 的 trace 回调会把绑定参数展开成字面值, 所以按语句形态断言而非占位符。
    sql_text = "\n".join(captured)
    assert "WHERE ts >=" in sql_text and " ts <" in sql_text and "ORDER BY id" in sql_text
    # 语义: 期望集用真实判定原语 local_date_of_ts(改前逐行过滤的判据)独立推导, 必须逐条一致
    all_ids = db_conn.execute("SELECT id, ts FROM ui_events ORDER BY id").fetchall()
    expected_ids = [r["id"] for r in all_ids if local_date_of_ts(r["ts"]) == date(2026, 10, 5)]
    assert [r["id"] for r in result] == expected_ids
    # JST(=LOCAL_TZ)下当日应恰为 4 条: 零点整、零点带小数秒、正午、23:59:59.999
    assert {r["item_id"] for r in result} == {
        "rss:p03-midday",
        "rss:p03-jst-midnight-exact",
        "rss:p03-jst-midnight-frac",
        "rss:p03-jst-eve-frac",
    }
    for r in result:
        assert r["local_date"] == "2026-10-05"
        assert r["session_id"] == "sess-p03"


def test_p06_digest_status_filter_positive_in_over_full_domain(db_conn):
    # ① 从活 schema 列出 item_status 的全部合法取值(001/002 迁移的 CHECK 约束) —— 等价性前提
    schema_sql = db_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='items'"
    ).fetchone()["sql"]
    m = re.search(r"CHECK\s*\(\s*item_status\s+IN\s*\(([^)]*)\)", schema_sql)
    assert m, "items.item_status 的 CHECK 约束缺失, NOT IN→IN 改写失去等价性前提"
    domain = {v.strip().strip("'") for v in m.group(1).split(",")}
    assert domain == {"new", "digested", "reviewed", "promoted", "rejected", "deferred"}
    # ② CHECK 封死域: 域外值写不进去(否则 NOT IN 4 个 ≢ IN 2 个)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_item(db_conn, "rss:p06-bad", status="weird")
    # ③ 六种状态各造一条, 真函数选出的候选必须恰好是 new/digested 两条
    with db_conn:
        for status in ["new", "digested", "reviewed", "promoted", "rejected", "deferred"]:
            _insert_item(db_conn, "rss:p06-" + status, status=status, adapter="local_transcripts")
    captured: list[str] = []
    db_conn.set_trace_callback(captured.append)
    try:
        daily = _select_rows(db_conn, pool_limit=50)
        transcript = _select_transcript_rows(db_conn, cooldown_since_utc=None)
    finally:
        db_conn.set_trace_callback(None)
    # 机制: 阻断 idx_items_status_ts 的 NOT IN 改成正向 IN(digest.py 两处同构 SQL 都验)
    sql_text = "\n".join(captured)
    assert "item_status IN ('new', 'digested')" in sql_text
    assert "NOT IN (" not in sql_text
    # 语义: 新旧条件对每个取值判定相同 → 候选集恰好是 new/digested 两条
    assert {r["item_id"] for r in daily} == {"rss:p06-new", "rss:p06-digested"}
    assert {r["item_id"] for r in transcript} == {"rss:p06-new", "rss:p06-digested"}


def test_p09_weibo_url_resolution_uses_glob_and_matches_like_on_real_ids(db_conn):
    rows = [
        # compute_item_id 的实际格式 weibo:<uid>:<post_id>(前缀固定小写, LIKE/GLOB 在其上语义一致)
        ("weibo:1000000001:5012345678901", "weibo_timeline:1000000001", "https://weibo.com/status/5012345678901"),
        ("weibo:1000000001:9999999999999", "weibo_timeline:1000000001", "https://weibo.com/status/9999999999999"),
        # post_id 只出现在 uid 位: 不得因子串误配(模式必须锚定行尾 :<post_id>)
        ("weibo:5012345678901:1111111111", "weibo_timeline:5012345678901", "https://weibo.com/status/1111111111"),
        ("xhs:abcdef1234567890abcdef12", "xhs:probe", "https://www.xiaohongshu.com/explore/abcdef1234567890abcdef12"),
        ("rss:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "rss_briefing:p09", "https://example.com/p09"),
    ]
    with db_conn:
        for item_id, source, url in rows:
            db_conn.execute(
                "INSERT INTO items (item_id, source, url, title, ts, content_hash, adapter_name, first_ingested_at, last_seen_at, item_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item_id, source, url, "t", "2026-10-01T00:00:00Z", "h-" + item_id, "probe", "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z", "new"),
            )
    captured: list[str] = []
    db_conn.set_trace_callback(captured.append)
    try:
        hit = _resolve_url_to_pil_item(db_conn, "https://weibo.com/1000000001/5012345678901")
        miss = _resolve_url_to_pil_item(db_conn, "https://weibo.com/1000000001/7777777777777")
    finally:
        db_conn.set_trace_callback(None)
    # 机制: weibo 分支必须用 GLOB 走主键范围搜(改前 LIKE 大小写不敏感, 吃不掉 BINARY 索引)
    sql_text = "\n".join(captured)
    assert "item_id GLOB 'weibo:*:5012345678901'" in sql_text
    assert "LIKE" not in sql_text
    # 语义: 与改前 LIKE 'weibo:%:<post_id>' 在真实 item_id 上结果一致
    assert hit == "weibo:1000000001:5012345678901"
    assert miss is None


def _seed_trust_fixture(conn) -> None:
    """同一批数据写两库: 3 个来源的 items(走真实 upsert_item → _bootstrap_source_trust)
    + 一条 digest inclusion + 一条 promote 反馈事件。"""
    items = [
        ("rss:p11-a", "rss_briefing:p11-a", "rss"),
        ("weibo:p11-b", "weibo_timeline:p11-b", "weibo"),
        ("rss:p11-c", "rss_briefing:p11-c", "rss"),
    ]
    with conn:
        for item_id, source, adapter in items:
            upsert_item(
                conn,
                make_item(item_id=item_id, source=source, url="https://example.com/" + item_id),
                adapter_name=adapter,
                source_payload_json="{}",
            )
        replace_digest_inclusions(
            conn,
            digest_kind="daily",
            digest_date="2026-09-30",
            digest_path="daily/2026-09-30.md",
            item_rows=[("rss:p11-a", "rss_briefing:p11-a")],
            included_at_utc="2026-09-30T12:00:00Z",
        )
        event = FeedbackEvent(
            event_id=compute_feedback_event_id(
                origin="cli",
                item_id="weibo:p11-b",
                action="promote_to_src",
                event_ts_utc_iso="2026-09-20T00:00:00Z",
            ),
            item_id="weibo:p11-b",
            action="promote_to_src",
            origin="cli",
            event_ts="2026-09-20T00:00:00Z",
        )
        assert record_feedback_event(conn, event) is True


def test_p11_recompute_source_trust_drops_items_distinct_scan(db_conn, tmp_path):
    _seed_trust_fixture(db_conn)
    captured: list[str] = []
    db_conn.set_trace_callback(captured.append)
    try:
        recompute_source_trust(db_conn, now_utc=NOW_P11)
    finally:
        db_conn.set_trace_callback(None)
    # 机制: 改前每次调用都对 items 做 DISTINCT 全索引扫(C04, 14.5 万行 ~190ms × 每日 50+ 次)
    sql_text = "\n".join(captured)
    assert "SELECT DISTINCT source, adapter_name FROM items" not in sql_text
    # 语义①: 结果与改前一致 —— 生产不变式是「source_trust 已覆盖 items 来源」(upsert_item 唯一
    # 写入口总是先 _bootstrap_source_trust)。在另一套同数据上用改前的建档来源(DISTINCT items →
    # 真 _bootstrap_source_trust)复现旧路径, 两库 source_trust 必须逐行一致。
    conn_old = connect_db(tmp_path / "old_path.sqlite")
    try:
        ensure_schema(conn_old)
        _seed_trust_fixture(conn_old)
        for row in conn_old.execute("SELECT DISTINCT source, adapter_name FROM items").fetchall():
            _bootstrap_source_trust(conn_old, source=row["source"], adapter_name=row["adapter_name"], now_utc=NOW_P11)
        recompute_source_trust(conn_old, now_utc=NOW_P11)
        new_rows = db_conn.execute("SELECT * FROM source_trust ORDER BY source").fetchall()
        old_rows = conn_old.execute("SELECT * FROM source_trust ORDER BY source").fetchall()
        assert [dict(r) for r in new_rows] == [dict(r) for r in old_rows]
    finally:
        conn_old.close()
    # 语义②: 每个 items 来源都有 trust 行, 数值与独立按公式算的一致
    sources = {r["source"] for r in db_conn.execute("SELECT DISTINCT source FROM items")}
    trusted = {r["source"] for r in db_conn.execute("SELECT source FROM source_trust")}
    assert sources <= trusted
    weight = FEEDBACK_EVENT_WEIGHTS["promote_to_src"]
    row_a = db_conn.execute("SELECT * FROM source_trust WHERE source='rss_briefing:p11-a'").fetchone()
    assert row_a["ingested_items_seen_90d"] == 1
    assert row_a["digested_items_seen_90d"] == 1
    # trust = (prior_weight*0.35 + weighted_feedback) / (prior_weight + digested_seen), 夹在 [0,1]
    assert row_a["trust_score"] == pytest.approx(min(1.0, max(0.0, (12.0 * 0.35 + 0.0) / 13.0)))
    row_b = db_conn.execute("SELECT * FROM source_trust WHERE source='weibo_timeline:p11-b'").fetchone()
    assert row_b["trust_score"] == pytest.approx(min(1.0, max(0.0, (12.0 * 0.35 + weight) / 12.0)))
