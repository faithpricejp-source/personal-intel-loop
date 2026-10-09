"""契约第 10 节: 机会栏字段、结算初判(存表 + 输出 + claim_id)、闲与美(无表/有表)、
各栏上限、est_read_min、连续 4 周点开率 <5% 的砍栏提案。"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from personal_intel_loop.paper import build_edition
from personal_intel_loop.paper_api import get_editions, get_item
from personal_intel_loop.paper_calibration import weekly_letters
from personal_intel_loop.paper_layout import cap_of, load_layout
from personal_intel_loop.paper_leisure import collect, home_cinema_new
from personal_intel_loop.paper_settle import run_prechecks
from personal_intel_loop.store import upsert_item
from tests._behavior_helpers import add_edition, add_events
from tests._platform_helpers import (
    NOW,
    SETTLE_JSON,
    ai_json,
    edition_section_ids,
    make_dispatch_llm,
    seed_candidate,
)
from tests.conftest import make_item

TODAY = date(2026, 10, 4)
MONDAY = date(2026, 10, 5)

OPPORTUNITY = {"if_true": "去看多晶硅产能数据", "kill_signal": "出现两份口径一致的下修", "horizon": "6-12 个月"}


def _candidate(index: int):
    return {
        "item_id": f"item:{index:02d}",
        "source": f"rss_briefing:feed_{index % 3}",
        "source_payload": {"feed_name": f"源 {index % 3}"},
        "title": f"标题 {index}",
        "body": "正文",
        "ts_utc": NOW,
    }


def _seed(conn, count: int):
    for index in range(count):
        seed_candidate(conn, index)
        conn.execute(
            "INSERT OR REPLACE INTO item_fulltext (item_id, text, status, fetched_at) VALUES (?, '正文全文', 'ok', ?)",
            (f"item:{index:02d}", NOW),
        )
    conn.commit()


def _build(conn, tmp_path, *, llm, count=12, n=12, layout=None, home_cinema_db=None, leisure_authors_path=None):
    _seed(conn, count)
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
        spool_dir=tmp_path / "spool",
        layout_path=layout or (tmp_path / "none.toml"),
        home_cinema_db=home_cinema_db,
        leisure_authors_path=leisure_authors_path,
    )


def test_layout_caps_and_uncapped_sections(tmp_path):
    layout = load_layout(tmp_path / "missing.toml")  # 缺省值；不读真实 config（那里的 warmth 源会变）
    assert layout["caps"] == {
        "lead": 1, "top": 5, "counter": 5, "briefs": 30, "blind": 9,
        "warmth": 5, "opportunity": 3, "settle": 5, "leisure": 4, "follow": 5,
        "risk_real": 5,  # 10-05 新增：风险栏真实条目上限
    }
    assert cap_of(layout["caps"], "risk") is None and cap_of(layout["caps"], "inbox") is None, "risk/inbox 不限"
    assert layout["warmth_sources"] == []


def test_section_caps_applied(db_conn, tmp_path):
    layout = tmp_path / "layout.toml"
    layout.write_text("[layout]\ntop = 2\nbriefs = 3\nopportunity = 1\n\n[warmth]\nsources = []\n", encoding="utf-8")
    # 两条机会条目(都在主线上), opportunity 上限 1 → 分数低的那条退回 briefs
    llm = make_dispatch_llm(
        ai=lambda prompt: ai_json(
            novelty_kind="new_fact",
            lane_tags=["opportunity"] if ("标题 5" in prompt or "标题 16" in prompt) else [],
            opportunity=OPPORTUNITY if ("标题 5" in prompt or "标题 16" in prompt) else None,
        )
    )
    _build(db_conn, tmp_path, llm=llm, layout=layout, count=20, n=20)
    assert len(edition_section_ids(db_conn, TODAY.isoformat(), "lead")) == 1
    assert len(edition_section_ids(db_conn, TODAY.isoformat(), "top")) == 2
    assert len(edition_section_ids(db_conn, TODAY.isoformat(), "briefs")) == 3
    assert edition_section_ids(db_conn, TODAY.isoformat(), "opportunity") == ["item:05"], "超上限的低分条目退回 briefs"
    # 超出的条目只是不上版, 没丢
    from personal_intel_loop.store import fetch_item

    assert fetch_item(db_conn, "item:19") is not None


def test_opportunity_field_on_items(db_conn, tmp_path):
    llm = make_dispatch_llm(
        ai=lambda prompt: ai_json(novelty_kind="new_fact", lane_tags=["opportunity"], opportunity=OPPORTUNITY)
    )
    _build(db_conn, tmp_path, llm=llm, count=12, n=12)
    opportunity_ids = edition_section_ids(db_conn, TODAY.isoformat(), "opportunity")
    assert opportunity_ids, "lane_tags 含 opportunity 的条目进机会栏"
    detail = get_item(db_conn, opportunity_ids[0])
    assert detail["opportunity"] == OPPORTUNITY
    assert set(detail["opportunity"].keys()) == {"if_true", "kill_signal", "horizon"}


def _seed_claim(conn, *, claim_id="c1", item_id="item:00", claim="断言: 明年光伏装机翻倍", check_after="2026-10-01"):
    conn.execute(
        "INSERT INTO claims (claim_id, item_id, source, claim, check_after, extracted_at) VALUES (?, ?, 'rss_briefing:feed_0', ?, ?, ?)",
        (claim_id, item_id, claim, check_after, NOW),
    )
    conn.commit()


def test_settle_failed_precheck_redone_next_time(db_conn):
    # 10-05 审计 ST-1：LLM 失败/坏 JSON/没给依据的初判不固化，下次出版重做
    _seed(db_conn, 3)
    _seed_claim(db_conn)
    run_prechecks(db_conn, date_local=TODAY, now_utc=NOW, llm_call=lambda p: "不是 JSON", search_fn=lambda claim: [])
    assert db_conn.execute("SELECT basis FROM claim_prechecks WHERE claim_id='c1'").fetchone()[0].startswith("invalid json:")
    calls = []
    again = run_prechecks(db_conn, date_local=TODAY, now_utc=NOW, llm_call=lambda p: calls.append(p) or SETTLE_JSON, search_fn=lambda claim: [])
    assert len(calls) == 1 and again[0]["precheck"]["verdict"] == "likely_true"
    db_conn.execute("UPDATE claim_prechecks SET verdict='unclear', basis='(模型未给依据)' WHERE claim_id='c1'")
    db_conn.commit()
    calls2 = []
    run_prechecks(db_conn, date_local=TODAY, now_utc=NOW, llm_call=lambda p: calls2.append(p) or SETTLE_JSON, search_fn=lambda claim: [])
    assert len(calls2) == 1


def test_settle_precheck_stored_and_rendered(db_conn, tmp_path):
    _seed(db_conn, 3)
    _seed_claim(db_conn)
    prepared = run_prechecks(
        db_conn,
        date_local=TODAY,
        now_utc=NOW,
        llm_call=lambda prompt: SETTLE_JSON,
        search_fn=lambda claim: [{"item_id": "item:01", "title": "后续报道", "url": "https://e.com/2", "date": NOW}],
    )
    assert len(prepared) == 1
    row = db_conn.execute("SELECT * FROM claim_prechecks WHERE claim_id='c1'").fetchone()
    assert row["verdict"] == "likely_true"
    assert row["basis"] == "库内新条目支持该断言"
    assert json.loads(row["links_json"]) == [{"title": "后续报道", "url": "https://example.com/followup"}]
    # 幂等: 已有初判不再调模型
    calls = []
    again = run_prechecks(db_conn, date_local=TODAY, now_utc=NOW, llm_call=lambda p: calls.append(p) or SETTLE_JSON)
    assert calls == [] and again[0]["precheck"]["verdict"] == "likely_true"

    llm = make_dispatch_llm(ai=ai_json(novelty_kind="new_fact"), settle=SETTLE_JSON)
    _build(db_conn, tmp_path, llm=llm, count=3, n=3)
    settle_ids = edition_section_ids(db_conn, TODAY.isoformat(), "settle")
    assert settle_ids == ["item:00"]
    edition = get_editions(db_conn, before="2026-10-05", limit=1, profile_path=tmp_path / "profile.md")["editions"][0]
    entry = edition["sections"]["settle"][0]
    settle = entry["settle"]
    assert settle["claim_id"] == "c1", "前端用它调 POST /api/claims/resolve"
    assert settle["claim"] == "断言: 明年光伏装机翻倍"
    assert settle["precheck"]["verdict"] == "likely_true"
    assert set(settle["author"].keys()) == {"n_true", "n_false", "n_unresolvable", "label"}


def test_settle_respects_cap(db_conn, tmp_path):
    _seed(db_conn, 8)
    for index in range(8):
        _seed_claim(db_conn, claim_id=f"c{index}", item_id=f"item:{index:02d}")
    layout = tmp_path / "layout.toml"
    layout.write_text("[layout]\nsettle = 2\n", encoding="utf-8")
    _build(
        db_conn,
        tmp_path,
        llm=make_dispatch_llm(ai=ai_json(novelty_kind="new_fact"), settle=SETTLE_JSON),
        layout=layout,
        count=8,
        n=8,
    )
    assert len(edition_section_ids(db_conn, TODAY.isoformat(), "settle")) == 2


def _make_home_cinema_db(path, *, with_table=True):
    conn = sqlite3.connect(path)
    if with_table:
        conn.execute("CREATE TABLE directors (title TEXT, director TEXT, added_at TEXT)")
        conn.executemany(
            "INSERT INTO directors VALUES (?, ?, ?)",
            [("引渡", "是枝裕和", "2026-10-03"), ("怪物", "是枝裕和", "2026-08-01")],
        )
    else:
        conn.execute("CREATE TABLE unrelated (x TEXT)")
        conn.execute("INSERT INTO unrelated VALUES ('y')")
    conn.commit()
    conn.close()


def test_leisure_home_cinema_with_and_without_table(db_conn, tmp_path):
    with_table = tmp_path / "hc_ok.db"
    _make_home_cinema_db(with_table, with_table=True)
    found = home_cinema_new(with_table, today=TODAY)
    assert [entry["title"] for entry in found] == ["是枝裕和 新作/更新: 引渡"], "只收近 7 天新增"

    without = tmp_path / "hc_bare.db"
    _make_home_cinema_db(without, with_table=False)
    assert home_cinema_new(without, today=TODAY) == [], "表结构不认识就返回空"
    assert home_cinema_new(tmp_path / "missing.db", today=TODAY) == [], "库不存在就跳过"


def test_leisure_authors_and_section(db_conn, tmp_path):
    _seed(db_conn, 6)
    upsert_item(
        db_conn,
        make_item(item_id="item:book", source="rss_briefing:feed_1", url="https://e.com/book", title="村上春树新书", body="新书上市"),
        adapter_name="rss_briefing",
        source_payload_json="{}",
    )
    db_conn.execute(
        "INSERT OR REPLACE INTO item_fulltext (item_id, text, status, fetched_at) VALUES ('item:book', '正文', 'ok', ?)", (NOW,)
    )
    db_conn.commit()
    authors = tmp_path / "authors.txt"
    authors.write_text("村上春树\n# 注释行\n", encoding="utf-8")
    result = collect(db_conn, date_local=TODAY, now_utc=NOW, home_cinema_db=None, authors_path=authors)
    assert [entry["item_id"] for entry in result["books"]] == ["item:book"]

    _build(
        db_conn,
        tmp_path,
        llm=make_dispatch_llm(ai=ai_json(novelty_kind="new_fact")),
        count=6,
        n=6,
        leisure_authors_path=authors,
    )
    leisure_ids = edition_section_ids(db_conn, TODAY.isoformat(), "leisure")
    assert "item:book" in leisure_ids


def test_est_read_min_and_page_footer_budget(db_conn, tmp_path):
    _build(db_conn, tmp_path, llm=make_dispatch_llm(ai=ai_json(novelty_kind="new_fact", lede="导语。" * 50)), count=10, n=10)
    edition = get_editions(db_conn, before="2026-10-05", limit=1, profile_path=tmp_path / "profile.md")["editions"][0]
    assert edition["stats"]["est_read_min"] >= 1
    assert isinstance(edition["stats"]["est_read_min"], int)
    # editions 接口 limit 缺省仍是 1 期(页尾按钮翻页, 不自动加载)
    assert len(get_editions(db_conn, before="2026-10-05", profile_path=tmp_path / "profile.md")["editions"]) == 1


def test_sunset_proposal_after_four_low_open_rate_weeks(db_conn, tmp_path):
    _seed(db_conn, 6)
    # 连续 4 周: leisure 栏曝光条目数达样本量但没人点开(曝光按不同条目计)
    for offset in range(1, 5):
        week_start = MONDAY - timedelta(days=7 * offset)
        edition_date = week_start.isoformat()
        for index in range(6):
            add_edition(db_conn, edition_date, f"item:{index:02d}", "leisure", index)
        ts = f"{edition_date}T02:00:00Z"
        add_events(
            db_conn,
            [("impression", f"item:{index:02d}", 1000, {"section": "leisure"}) for index in range(6)],
            ts=ts,
            edition_date=edition_date,
        )
    profile = tmp_path / "profile.md"
    profile.write_text("# 画像\n\n## 待接受的修订\n", encoding="utf-8")
    written = weekly_letters(db_conn, monday=MONDAY, now_utc=NOW, profile_path=profile)
    assert "leisure" in written["sunsets"]
    text = profile.read_text(encoding="utf-8")
    assert "建议砍掉/合并 leisure 栏" in text


def test_no_sunset_when_open_rate_healthy(db_conn, tmp_path):
    _seed(db_conn, 6)
    for offset in range(1, 5):
        week_start = MONDAY - timedelta(days=7 * offset)
        edition_date = week_start.isoformat()
        for index in range(6):
            add_edition(db_conn, edition_date, f"item:{index:02d}", "leisure", index)
        ts = f"{edition_date}T02:00:00Z"
        add_events(
            db_conn,
            [("impression", f"item:{index:02d}", 1000, {"section": "leisure"}) for index in range(6)],
            ts=ts,
            edition_date=edition_date,
        )
        add_events(db_conn, [("open_item", f"item:{index:02d}", None, {}) for index in range(3)], ts=ts, edition_date=edition_date)
    profile = tmp_path / "profile.md"
    profile.write_text("# 画像\n\n## 待接受的修订\n", encoding="utf-8")
    assert "leisure" not in weekly_letters(db_conn, monday=MONDAY, now_utc=NOW, profile_path=profile)["sunsets"]