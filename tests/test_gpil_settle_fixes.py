"""结算栏审查修复：直接调用 paper_settle 的真函数。"""
from __future__ import annotations

from datetime import date

from personal_intel_loop.paper_settle import default_search_fn, run_prechecks
from personal_intel_loop.store import upsert_claim, upsert_item
from tests.conftest import make_item

NOW = "2026-10-07T12:00:00Z"
CLAIM = "2024年9月9日，日银将在10月会议上加息25个基点"


def _put(conn, *, item_id: str, title: str, body: str, ts: str) -> None:
    upsert_item(
        conn,
        make_item(item_id=item_id, title=title, body=body, ts=ts, url=f"https://example.com/{item_id}"),
        adapter_name="rss_briefing",
        source_payload_json="{}",
    )


def test_s1_chinese_evidence_ranks_content_over_shared_date(db_conn):
    """相关中文条目应排在最前；只共享日期的无关条目不应被选中。"""
    _put(
        db_conn,
        item_id="item:relevant",
        title="日银宣布加息25个基点",
        body="日本央行把政策利率上调，市场关注后续声明。",
        ts="2026-09-20T00:00:00Z",
    )
    _put(
        db_conn,
        item_id="item:meeting",
        title="下周会议日程",
        body="会议通知：下周例会改期。",
        ts="2026-10-06T00:00:00Z",
    )
    _put(
        db_conn,
        item_id="item:date-only",
        title="2024年9月9日烟花大会",
        body="2024年9月9日东京举办烟花大会，观众人数创纪录。",
        ts="2026-10-06T12:00:00Z",
    )

    hits = default_search_fn(db_conn, CLAIM, now_utc=NOW)
    ids = [row["item_id"] for row in hits]
    assert ids, "应命中相关中文条目"
    assert ids[0] == "item:relevant"
    assert "item:date-only" not in ids


def test_s2_filter_missing_items_before_settle_cap(db_conn):
    """孤儿断言先占满名额时，仍在库的到期断言应留下，且不超过 max_n。"""
    for index in range(5):
        upsert_claim(
            db_conn,
            item_id=f"missing:{index}",
            source="rss_briefing:feed",
            claim=f"孤儿断言{index}",
            check_after="2026-10-07",  # 10-07 起名额只给近期到期, 新到期在前: 孤儿排最前
        )
    for index in range(6):
        item_id = f"item:live-{index}"
        _put(
            db_conn,
            item_id=item_id,
            title=f"在库条目{index}",
            body="正文",
            ts="2026-09-01T00:00:00Z",
        )
        upsert_claim(
            db_conn,
            item_id=item_id,
            source="rss_briefing:feed",
            claim=f"在库断言{index}",
            check_after=f"2026-10-0{index + 1}",
        )

    out = run_prechecks(
        db_conn,
        date_local=date(2026, 10, 7),
        now_utc=NOW,
        llm_call=lambda prompt: '{"verdict":"unclear","basis":"没查到足够证据","links":[]}',
        search_fn=lambda claim: [],
        max_n=5,
    )
    ids = [row["claim_row"]["item_id"] for row in out]
    assert ids == [f"item:live-{index}" for index in range(5, 0, -1)]


def test_s1_long_transcript_does_not_outrank_focused_item(db_conn):
    """验收方补: 排序按 bm25(长度归一), 长转写稿零散命中更多二元组也不能压过切题短条目。"""
    _put(
        db_conn,
        item_id="item:focused",
        title="日银10月会议加息25个基点",
        body="日银在10月会议上决定加息。",
        ts="2026-09-20T00:00:00Z",
    )
    filler = "播客转写闲聊内容与主题无关。" * 400
    _put(
        db_conn,
        item_id="item:transcript",
        title="本周播客合集",
        body=filler + "日银" + filler + "银将" + filler + "会议" + filler + "议上" + filler + "加息" + filler + "基点" + filler + "个基" + filler + "月会" + filler + "上加",
        ts="2026-10-06T00:00:00Z",
    )
    ids = [row["item_id"] for row in default_search_fn(db_conn, CLAIM, now_utc=NOW)]
    assert ids[:2] == ["item:focused", "item:transcript"]
