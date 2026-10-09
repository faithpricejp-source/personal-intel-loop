"""契约第 8、9 节: 温暖栏(数量/核实字段/不参与行为学习)、行程 CRUD、30 天窗口的行前风险
简报结构、≤3 天且当天有 urgent 时的推送去重。"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from personal_intel_loop.paper import build_edition
from personal_intel_loop.paper_api import create_trip, delete_trip, get_editions, get_item, get_trips
from personal_intel_loop.paper_learn import guardrail_filter
from personal_intel_loop.paper_risk import generate, upcoming_trips
from tests._behavior_helpers import add_edition
from tests._platform_helpers import (
    NOW,
    RISK_JSON,
    ai_json,
    edition_section_ids,
    make_dispatch_llm,
    seed_candidate,
)
from tests.conftest import make_item

TODAY = date(2026, 10, 4)

VERIFICATION = {"level": "primary", "named": ["佐藤美咲", "2026-10-01", "5000 日元"], "note": "当事人受访记录"}


def _candidate(index: int):
    return {
        "item_id": f"item:{index:02d}",
        "source": f"rss_briefing:feed_{index % 3}",
        "source_payload": {"feed_name": f"源 {index % 3}"},
        "title": f"标题 {index}",
        "body": "正文",
        "ts_utc": NOW,
    }


def _seed_with_fulltext(conn, count: int):
    for index in range(count):
        seed_candidate(conn, index)
        conn.execute(
            "INSERT OR REPLACE INTO item_fulltext (item_id, text, status, fetched_at) VALUES (?, '正文全文', 'ok', ?)",
            (f"item:{index:02d}", NOW),
        )
    conn.commit()


def _build(conn, tmp_path, *, llm, count=12, n=12, spool_name="spool", layout=None):
    _seed_with_fulltext(conn, count)
    return build_edition(
        conn,
        date_local=TODAY,
        n=n,
        now_utc=NOW,
        select_fn=lambda c, *, date_local, top_k, now_utc: [dict(x) for x in (_candidate(i) for i in range(count))][:top_k],
        embed_fn=lambda texts: [[0.0, 0.0, 1.0] for _ in texts],
        fetcher=lambda url: ("正文全文", "ok"),
        llm_call=llm,
        learn_first=False,
        spool_dir=tmp_path / spool_name,
        layout_path=layout or (tmp_path / "none.toml"),
    )


def test_warmth_section_size_and_verification(db_conn, tmp_path):
    layout = tmp_path / "layout.toml"
    layout.write_text("[layout]\nwarmth = 3\n\n[warmth]\nsources = []\n", encoding="utf-8")
    llm = make_dispatch_llm(
        ai=lambda prompt: ai_json(
            novelty_kind="new_fact",
            lane_tags=["warmth"],
            verification=VERIFICATION if "标题 17" in prompt else None,
        )
    )
    # 主线 17 条 + 备选 4 条 → 温暖栏候选从备选池里挑, 按配置上限 3 条截断
    _build(db_conn, tmp_path, llm=llm, layout=layout, count=30, n=20)
    warmth_ids = edition_section_ids(db_conn, TODAY.isoformat(), "warmth")
    assert len(warmth_ids) == 3, "温暖栏按配置上限截断"
    edition = get_editions(db_conn, before="2026-10-05", limit=1, profile_path=tmp_path / "profile.md")["editions"][0]
    warm_items = edition["sections"]["warmth"]
    verified = [item for item in warm_items if item["verification"] is not None]
    assert len(verified) == 1 and verified[0]["verification"] == VERIFICATION, "温暖条目输出 verification(level/named/note)"
    # 没给核实信息的温暖条目该字段为 null, 不伪造
    assert all(item["verification"] is None for item in warm_items if item["item_id"] != verified[0]["item_id"])


def test_warmth_items_excluded_from_behavior_learning(db_conn, tmp_path):
    # skipped 的 warmth 条目不计入聚合(同盲区规则)
    from personal_intel_loop.store import upsert_item

    upsert_item(db_conn, make_item(item_id="item:warm", source="rss_briefing:f", url="https://e.com/w", title="温暖故事"), adapter_name="rss_briefing", source_payload_json="{}")
    db_conn.execute(
        "INSERT OR REPLACE INTO item_ai (item_id, payload_json, model, error, created_at) VALUES ('item:warm', ?, 'm', NULL, ?)",
        (json.dumps({"novelty": {"kind": "known"}}), NOW),
    )
    add_edition(db_conn, TODAY.isoformat(), "item:warm", "warmth", 0)
    db_conn.commit()
    items = [{"item_id": "item:warm", "section": "warmth", "label": "skipped"}]
    assert guardrail_filter(db_conn, items) == [], "温暖栏跳过不算负反馈"
    items[0]["label"] = "read"
    assert guardrail_filter(db_conn, items) == items, "读了仍计入"


def test_trips_crud(db_conn):
    created = create_trip(db_conn, {"place": "大阪", "country": "日本", "start_date": "2026-10-20", "end_date": "2026-10-25", "note": "看展"}, now_utc=NOW)
    assert created["ok"] is True
    trip_id = created["trip"]["trip_id"]
    assert get_trips(db_conn)["trips"] == [created["trip"]]
    assert set(created["trip"].keys()) == {"trip_id", "place", "country", "start_date", "end_date", "note", "created_at"}
    assert delete_trip(db_conn, {"trip_id": trip_id}, now_utc=NOW) == {"ok": True}
    assert get_trips(db_conn)["trips"] == []
    with pytest.raises(KeyError):
        delete_trip(db_conn, {"trip_id": trip_id}, now_utc=NOW)
    with pytest.raises(ValueError):
        create_trip(db_conn, {"place": "", "start_date": "2026-10-20"}, now_utc=NOW)
    with pytest.raises(ValueError):
        create_trip(db_conn, {"place": "大阪", "start_date": "2026-10-25", "end_date": "2026-10-20"}, now_utc=NOW)


def test_upcoming_trips_window_is_30_days(db_conn):
    inside = create_trip(db_conn, {"place": "大阪", "start_date": "2026-10-20", "end_date": "2026-10-25"}, now_utc=NOW)["trip"]
    ongoing = create_trip(db_conn, {"place": "京都", "start_date": "2026-10-01", "end_date": "2026-10-06"}, now_utc=NOW)["trip"]
    create_trip(db_conn, {"place": "纽约", "start_date": "2026-12-01"}, now_utc=NOW)
    create_trip(db_conn, {"place": "伦敦", "start_date": "2026-09-01", "end_date": "2026-09-05"}, now_utc=NOW)
    ids = [row["trip_id"] for row in upcoming_trips(db_conn, date_local=TODAY)]
    assert ids == [ongoing["trip_id"], inside["trip_id"]], "≤30 天且未结束(含进行中)"


def test_open_ended_trip_stops_after_14_days(db_conn):
    """H-6: 无 end_date 的行程从开始日起持续监测 14 天(开始日算第 1 天)。
    today - start ≤ 13 天仍生成; today - start ≥ 14 天(开始日早于今天满 14 天)不再生成。"""
    day14 = create_trip(db_conn, {"place": "首尔", "start_date": (TODAY - timedelta(days=13)).isoformat()}, now_utc=NOW)["trip"]
    create_trip(db_conn, {"place": "釜山", "start_date": (TODAY - timedelta(days=14)).isoformat()}, now_utc=NOW)
    create_trip(db_conn, {"place": "台北", "start_date": (TODAY - timedelta(days=40)).isoformat()}, now_utc=NOW)
    # 写了 end_date 的不受 14 天限制: 开始早于 14 天但尚未结束, 照旧算进行中
    long_trip = create_trip(
        db_conn,
        {"place": "巴黎", "start_date": (TODAY - timedelta(days=20)).isoformat(), "end_date": (TODAY + timedelta(days=1)).isoformat()},
        now_utc=NOW,
    )["trip"]
    ids = [row["trip_id"] for row in upcoming_trips(db_conn, date_local=TODAY)]
    assert ids == [long_trip["trip_id"], day14["trip_id"]]


def test_open_ended_expired_trip_generates_no_risk_item(db_conn):
    create_trip(db_conn, {"place": "釜山", "start_date": (TODAY - timedelta(days=14)).isoformat()}, now_utc=NOW)
    payloads, members = generate(
        db_conn,
        date_local=TODAY,
        now_utc=NOW,
        llm_call=lambda prompt: RISK_JSON,
        search_fn=lambda place: [],
    )
    assert payloads == [] and members == {}
    from personal_intel_loop.paper_risk import default_trip_terms

    assert default_trip_terms(db_conn, date_local=TODAY) == set(), "过期开放式行程的地名不再算风险栏命中"


def test_risk_item_structure(db_conn, tmp_path):
    create_trip(db_conn, {"place": "大阪", "country": "日本", "start_date": "2026-10-20", "end_date": "2026-10-25"}, now_utc=NOW)
    seed_candidate(db_conn, 1, title="大阪暴雨", body="大阪今日大雨")
    db_conn.commit()
    payloads, members = generate(
        db_conn,
        date_local=TODAY,
        now_utc=NOW,
        llm_call=lambda prompt: RISK_JSON,
        search_fn=lambda place: [{"item_id": "item:01", "title": "大阪暴雨", "url": "https://e.com/1", "source": "rss_briefing:f", "date": "2026-10-03"}],
    )
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["item_id"].startswith("risk:trip:") and payload["item_id"].endswith(":2026-10-04")
    assert payload["kind"] == "risk"
    assert payload["topic"] == "风险"
    headings = [section["heading"] for section in payload["sections"]]
    assert headings == ["气候", "治安"]
    point = payload["sections"][0]["points"][0]
    assert set(point.keys()) == {"text", "source_title", "source_url", "date"}, "每条结论附来源与日期"
    # 来源不足时明说, 不编
    honest = payload["sections"][1]["points"][0]
    assert honest["text"] == "没查到官方说法" and honest["source_title"] is None
    assert members[payload["item_id"]] == ["item:01"]


def test_risk_appears_in_edition_section(db_conn, tmp_path):
    create_trip(db_conn, {"place": "大阪", "start_date": "2026-10-20"}, now_utc=NOW)
    _build(
        db_conn,
        tmp_path,
        llm=make_dispatch_llm(ai=ai_json(novelty_kind="new_fact"), risk=RISK_JSON),
        count=6,
        n=6,
    )
    risk_ids = edition_section_ids(db_conn, TODAY.isoformat(), "risk")
    assert len(risk_ids) == 1 and risk_ids[0].startswith("risk:")
    detail = get_item(db_conn, risk_ids[0])
    assert detail["kind"] == "risk"
    assert detail["edition_date"] == TODAY.isoformat()


def test_urgent_push_dedup_for_near_trip(db_conn, tmp_path):
    from personal_intel_loop.paper_inbox import accept

    # 行程 2 天后出发, 当天有 urgent 且含地名的投递
    create_trip(db_conn, {"place": "大阪", "start_date": (TODAY + timedelta(days=2)).isoformat()}, now_utc=NOW)
    accept(
        db_conn,
        {"source": "pil.alerts", "title": "大阪暴雨警报", "body": "大阪大雨", "url": None, "priority": "urgent", "dedup_key": "k1"},
        now_utc=NOW,
    )
    db_conn.commit()
    payloads, _members = generate(db_conn, date_local=TODAY, now_utc=NOW, llm_call=lambda p: RISK_JSON)
    assert payloads
    pushed = db_conn.execute("SELECT COUNT(*) FROM inbox WHERE source='pil.risk'").fetchone()[0]
    assert pushed == 1
    # 同日重跑不再投(dedup trip+date)
    generate(db_conn, date_local=TODAY, now_utc=NOW, llm_call=lambda p: RISK_JSON)
    assert db_conn.execute("SELECT COUNT(*) FROM inbox WHERE source='pil.risk'").fetchone()[0] == 1

    # 行程还早(>3 天)不推
    create_trip(db_conn, {"place": "京都", "start_date": (TODAY + timedelta(days=20)).isoformat()}, now_utc=NOW)
    accept(
        db_conn,
        {"source": "pil.alerts", "title": "京都地震", "body": "京都震感", "url": None, "priority": "urgent", "dedup_key": "k2"},
        now_utc=NOW,
    )
    db_conn.commit()
    generate(db_conn, date_local=TODAY, now_utc=NOW, llm_call=lambda p: RISK_JSON)
    titles = [row["title"] for row in db_conn.execute("SELECT title FROM inbox WHERE source='pil.risk'")]
    assert len(titles) == 1 and "大阪" in titles[0], "只有 ≤3 天的行程推 urgent"