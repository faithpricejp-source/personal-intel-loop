"""Edition 的 sections.inbox / flash、notifications 的 since 边界、build_edition 开头 drain_spool。

flash = 当天(东京本地日)urgent 且未读; inbox = 当天收到的 normal/low + urgent 全部。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from personal_intel_loop import LOCAL_TZ
from personal_intel_loop.paper_api import get_editions, notifications
from personal_intel_loop.paper_inbox import accept, mark_read
from personal_intel_loop.store import upsert_item
from tests.conftest import make_item

NOW = "2026-10-04T01:00:00Z"
EDITION_DAY = datetime.fromisoformat(NOW.replace("Z", "+00:00")).astimezone(LOCAL_TZ).date()
YESTERDAY_STAMP = "2026-10-03T01:00:00Z"  # 比 NOW 早 24h, 本地日必然不同

AI_JSON = json.dumps(
    {"lede": "导语。", "one_liner": "一句话。", "backstory": None, "so_what": None, "claim": None,
     "byline": None, "style_tags": [], "topic": "科学", "profile_hit": None, "lane": "1"},
    ensure_ascii=False,
)


def _seed_edition(conn, edition_date: str, built_at: str):
    upsert_item(
        conn,
        make_item(item_id="item:lead", title="头条标题"),
        adapter_name="rss_briefing",
        source_payload_json="{}",
    )
    conn.execute(
        "INSERT INTO editions (edition_date, item_id, section, rank, blind_reason, built_at) VALUES (?, 'item:lead', 'lead', 0, NULL, ?)",
        (edition_date, built_at),
    )
    conn.commit()


def _edition(conn, profile_path):
    return get_editions(conn, before=(EDITION_DAY + timedelta(days=1)).isoformat(), limit=1, profile_path=profile_path)["editions"][0]


def _accept(conn, *, source="pil.alerts", title, priority, dedup_key=None, now_utc=NOW):
    return accept(conn, {"source": source, "title": title, "body": f"正文 {title}", "url": None,
                         "priority": priority, "dedup_key": dedup_key}, now_utc=now_utc)


def test_flash_only_unread_urgent_of_the_day(db_conn, tmp_path):
    _seed_edition(db_conn, EDITION_DAY.isoformat(), NOW)
    _accept(db_conn, title="urgent 未读", priority="urgent", dedup_key="k1")
    read_id = _accept(db_conn, title="urgent 已读", priority="urgent", dedup_key="k2")["inbox_id"]
    _accept(db_conn, title="normal", priority="normal", dedup_key="k3")
    _accept(db_conn, title="昨天的 urgent", priority="urgent", dedup_key="k4", now_utc=YESTERDAY_STAMP)
    mark_read(db_conn, read_id, now_utc=NOW)

    edition = _edition(db_conn, tmp_path / "profile.md")
    flash = edition["flash"]
    assert [entry["title"] for entry in flash] == ["urgent 未读"], "flash 只含当天且未读的 urgent"
    inbox = edition["sections"]["inbox"]
    assert [entry["title"] for entry in inbox] == ["urgent 未读", "urgent 已读", "normal"], "inbox 含当天 normal/low + urgent 全部, 不含他日"


def test_mark_read_removes_from_flash(db_conn, tmp_path):
    _seed_edition(db_conn, EDITION_DAY.isoformat(), NOW)
    inbox_id = _accept(db_conn, title="快讯", priority="urgent", dedup_key="k1")["inbox_id"]
    edition = _edition(db_conn, tmp_path / "profile.md")
    assert len(edition["flash"]) == 1 and edition["flash"][0]["read"] is False

    mark_read(db_conn, inbox_id, now_utc=NOW)
    edition = _edition(db_conn, tmp_path / "profile.md")
    assert edition["flash"] == [], "标已读后从 flash 消失"
    assert len(edition["sections"]["inbox"]) == 1 and edition["sections"]["inbox"][0]["read"] is True


def test_inbox_item_keys_and_source_label_mapping(db_conn, tmp_path):
    _seed_edition(db_conn, EDITION_DAY.isoformat(), NOW)
    _accept(db_conn, title="预警", priority="urgent", dedup_key="k1")
    _accept(db_conn, source="cook.today", title="晚饭", priority="low", dedup_key="k2")

    edition = _edition(db_conn, tmp_path / "profile.md")
    entry_alert, entry_cook = edition["sections"]["inbox"]
    contract_keys = {"inbox_id", "source", "source_label", "title", "body", "url", "priority", "created_at", "read"}
    assert set(entry_alert.keys()) == contract_keys and set(entry_cook.keys()) == contract_keys
    assert set(edition["flash"][0].keys()) == contract_keys
    assert entry_alert["source_label"] == "灾害预警" and entry_cook["source_label"] == "cook.today"
    assert entry_alert["created_at"].startswith(EDITION_DAY.isoformat()), "created_at 为本机时区 ISO"


def test_empty_inbox_edition_has_empty_flash(db_conn, tmp_path):
    _seed_edition(db_conn, EDITION_DAY.isoformat(), NOW)
    edition = _edition(db_conn, tmp_path / "profile.md")
    assert edition["sections"]["inbox"] == [] and edition["flash"] == []


def test_notifications_new_edition_once(db_conn, tmp_path):
    _seed_edition(db_conn, (EDITION_DAY - timedelta(days=1)).isoformat(), "2026-10-03T00:30:00Z")
    _seed_edition(db_conn, EDITION_DAY.isoformat(), "2026-10-04T00:30:00Z")
    # 同一期再添一条 briefs(PK 是 (edition_date, item_id), 用第二条 item), 仍只出一条通知
    conn = db_conn
    upsert_item(conn, make_item(item_id="item:br", title="简报标题"), adapter_name="rss_briefing", source_payload_json="{}")
    conn.execute(
        "INSERT INTO editions (edition_date, item_id, section, rank, blind_reason, built_at) VALUES (?, 'item:br', 'briefs', 0, NULL, '2026-10-04T00:30:00Z')",
        (EDITION_DAY.isoformat(),),
    )
    conn.commit()

    result = notifications(conn, since="2026-10-03T00:00:00Z", now_utc="2026-10-04T02:00:00Z")
    assert set(result.keys()) == {"now", "notifications"}
    assert result["now"] == "2026-10-04T02:00:00Z"
    edition_notifications = [n for n in result["notifications"] if n["id"].startswith("edition:")]
    assert len(edition_notifications) == 2, "每个 edition_date 只出一条"
    latest = edition_notifications[-1]
    assert set(latest.keys()) == {"id", "title", "body", "open_path"}
    assert latest == {"id": f"edition:{EDITION_DAY.isoformat()}", "title": "今日 · 第 2 期",
                      "body": "头条标题", "open_path": "/paper/#/"}
    assert edition_notifications[0]["title"] == "今日 · 第 1 期"

    # since 边界: built_at > since 才出
    assert not [n for n in notifications(conn, since="2026-10-04T00:30:00Z", now_utc="2026-10-04T02:00:00Z")["notifications"]
                if n["id"].startswith("edition:")]
    assert [n for n in notifications(conn, since="2026-10-04T00:29:59Z", now_utc="2026-10-04T02:00:00Z")["notifications"]
            if n["id"] == f"edition:{EDITION_DAY.isoformat()}"]


def test_notifications_urgent_since_boundary(db_conn, tmp_path):
    _seed_edition(db_conn, EDITION_DAY.isoformat(), NOW)
    inbox_id = _accept(db_conn, title="快讯", priority="urgent", dedup_key="k1")["inbox_id"]
    _accept(db_conn, title="简报", priority="normal", dedup_key="k2")

    result = notifications(db_conn, since="2026-10-04T00:00:00Z", now_utc="2026-10-04T02:00:00Z")
    urgent = [n for n in result["notifications"] if n["id"] == f"inbox:{inbox_id}"]
    assert urgent == [{"id": f"inbox:{inbox_id}", "title": "快讯", "body": "正文 快讯",
                       "open_path": f"/paper/#/inbox/{inbox_id}"}], "urgent 投递出通知, normal 不出"

    # created_at > since 严格成立才出; since 恰等于 created_at 时不重复打扰
    assert notifications(db_conn, since=NOW, now_utc="2026-10-04T02:00:00Z")["notifications"] == []
    # since 缺省 = 全量
    all_notifications = notifications(db_conn, now_utc="2026-10-04T02:00:00Z")["notifications"]
    assert any(n["id"] == f"inbox:{inbox_id}" for n in all_notifications)


def test_notifications_bad_since_raises(db_conn):
    import pytest
    with pytest.raises(ValueError):
        notifications(db_conn, since="not-a-time", now_utc=NOW)


def test_build_edition_drains_spool_first(db_conn, tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "alert.json").write_text(json.dumps({"source": "pil.alerts", "title": "spool 里的预警",
                                                  "priority": "urgent", "dedup_key": "k9"}, ensure_ascii=False), "utf-8")
    monkeypatch.setenv("TODAY_PAPER_SPOOL", str(spool))
    upsert_item(db_conn, make_item(item_id="item:lead", title="头条标题"), adapter_name="rss_briefing", source_payload_json="{}")
    db_conn.commit()

    from personal_intel_loop.paper import build_edition

    def select(conn, *, date_local, top_k, now_utc):
        return [{"item_id": "item:lead", "source": "rss_briefing:reuters_top_news", "source_payload": {},
                 "title": "头条标题", "body": "正文", "ts_utc": NOW}]

    result = build_edition(
        db_conn, date_local=EDITION_DAY, n=1, now_utc=NOW, select_fn=select,
        fetcher=lambda url: (None, "skipped"), llm_call=lambda prompt: AI_JSON,
        profile_dir=tmp_path, learn_first=False,
    )
    # TASK4 起 build_edition 结果带 warmth/risk/leisure/digests 计数
    assert set(result.keys()) == {"date", "picked", "blind", "fulltext_ok", "ai_ok", "warmth", "risk", "leisure", "digests"}
    row = db_conn.execute("SELECT title, priority FROM inbox WHERE dedup_key='k9'").fetchone()
    assert row is not None and row["priority"] == "urgent", "出版开头先补收 spool"
    assert (spool / "done" / "alert.json").is_file()
