"""预警绕开排序器: 前置在候选最前、不受限额约束、进过日报的不再重复、超窗的不再冒出。

profile「我的人所在地的灾害预警」那行说这条 lane 的价值是能转发能行动、不是情报,
所以它不跟别的内容抢分数,也不受 A 段 ≤3 限额。这几条测试钉住那个"绕开"。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from personal_intel_loop import digest as digest_mod
from personal_intel_loop.digest import (
    RANKING_V2,
    TIER_ALERT,
    render_digest,
    select_alert_candidates,
    select_candidates_with_fallback,
)
from personal_intel_loop.schemas import Item
from personal_intel_loop.store import upsert_item

NOW = "2026-08-26T12:00:00Z"


def _alert(conn, *, item_id="disaster_alerts:x1", ts="2026-08-26T10:35:00Z",
           title="广东省广州市天河区气象台发布雷雨大风黄色预警信号"):
    upsert_item(
        conn,
        Item(id=item_id, source="disaster_alerts:cn:广州市",
             url=f"https://www.nmc.cn/publish/alarm/{item_id}.html",
             title=title, body=title, ts=ts, lang="zh"),
        adapter_name="disaster_alerts",
        source_payload_json="{}",
    )


def test_recent_alert_is_selected(db_conn):
    _alert(db_conn)
    got = select_alert_candidates(db_conn, now_utc=NOW)
    assert len(got) == 1
    assert got[0]["tier"] == TIER_ALERT
    assert got[0]["source"] == "disaster_alerts:cn:广州市"


def test_alert_outside_lookback_window_dropped(db_conn):
    _alert(db_conn, ts="2026-08-24T10:00:00Z")   # 48h 前
    assert select_alert_candidates(db_conn, now_utc=NOW) == []


def test_already_digested_alert_not_repeated(db_conn):
    _alert(db_conn)
    db_conn.execute(
        "INSERT INTO digest_inclusions (inclusion_id, item_id, source, digest_kind, digest_date,"
        " digest_path, included_at_utc) VALUES (?,?,?,?,?,?,?)",
        ("inc1", "disaster_alerts:x1", "disaster_alerts:cn:广州市", "daily",
         "2026-08-25", "/tmp/d.md", "2026-08-25T00:00:00Z"),
    )
    assert select_alert_candidates(db_conn, now_utc=NOW) == []


def test_alerts_prepended_ahead_of_ranked_candidates(db_conn, monkeypatch):
    """包装把预警放在排序结果**前面**, 且不消耗 top_k 名额。"""
    _alert(db_conn)
    ranked = [{"item_id": "rss:a", "source": "rss_briefing:x", "tier": "longform"}]
    monkeypatch.setattr(digest_mod, "_select_ranked_with_fallback",
                        lambda conn, **kw: (list(ranked), RANKING_V2))
    cands, version = select_candidates_with_fallback(
        db_conn, date_local=date(2026, 8, 26), top_k=3, now_utc=NOW, ranking="auto")
    assert version == RANKING_V2
    assert [c["item_id"] for c in cands] == ["disaster_alerts:x1", "rss:a"]


def test_no_alerts_leaves_ranked_untouched(db_conn, monkeypatch):
    """阴性对照: 库里没预警时包装必须是恒等的 —— 否则上面那条证明不了是"前置"生效。"""
    ranked = [{"item_id": "rss:a", "source": "rss_briefing:x", "tier": "longform"}]
    monkeypatch.setattr(digest_mod, "_select_ranked_with_fallback",
                        lambda conn, **kw: (list(ranked), RANKING_V2))
    cands, _ = select_candidates_with_fallback(
        db_conn, date_local=date(2026, 8, 26), top_k=3, now_utc=NOW, ranking="auto")
    assert [c["item_id"] for c in cands] == ["rss:a"]


def test_alert_section_rendered_with_own_heading():
    cand = {
        "item_id": "disaster_alerts:x1", "source": "disaster_alerts:cn:广州市",
        "title": "广东省广州市天河区气象台发布雷雨大风黄色预警信号",
        "trust_score": 0.35, "ts_utc": "2026-08-26T10:35:00Z", "tags": [],
        "url": "https://www.nmc.cn/publish/alarm/x1.html", "summary": "",
        "media_manifest_relpath": None, "tier": TIER_ALERT, "score_v2": 0.0,
    }
    md = render_digest(date_local=date(2026, 8, 26), candidates=[cand], ranking_version=RANKING_V2)
    assert "## ⚠️ 预警 (你的人所在地 · 可直接转发)" in md
    assert "广州市天河区" in md
    from personal_intel_loop.web_digest import parse_digest
    assert parse_digest(md).items[0]["tier"] == "alert"
