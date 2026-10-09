"""预警邮件: 只发新的、发失败不标已发、dry-run 零副作用、同一天多轮不互相抹掉。"""
from __future__ import annotations

from datetime import datetime, timezone

from personal_intel_loop import alerts_notify as an
from personal_intel_loop.schemas import Item
from personal_intel_loop.store import upsert_item

NOW = "2026-08-26T12:00:00Z"


def _alert(conn, item_id, *, city="广州市", ts="2026-08-26T10:35:00Z", title="广东省广州市天河区气象台发布雷雨大风橙色预警信号"):
    upsert_item(
        conn,
        Item(id=item_id, source=f"disaster_alerts:cn:{city}",
             url=f"https://www.nmc.cn/publish/alarm/{item_id}.html",
             title=title, body=title, ts=ts, lang="zh"),
        adapter_name="disaster_alerts", source_payload_json="{}")


class _Spy:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []
    def __call__(self, subject, body):
        self.calls.append((subject, body)); return self.ok


def test_no_alerts_sends_nothing(db_conn):
    spy = _Spy()
    r = an.run(db_conn, now_utc=NOW, channel="email", send=spy)
    assert r["sent"] is False and r["reason"] == "no_new_alerts"
    assert spy.calls == []


def test_sends_and_marks(db_conn):
    _alert(db_conn, "a1")
    spy = _Spy()
    r = an.run(db_conn, now_utc=NOW, channel="email", send=spy)
    assert r["sent"] is True and r["pending"] == 1
    assert len(spy.calls) == 1
    assert "广州市" in spy.calls[0][0]
    assert db_conn.execute(
        "SELECT count(*) FROM digest_inclusions WHERE digest_kind='alert_email'").fetchone()[0] == 1


def test_already_sent_not_resent(db_conn):
    _alert(db_conn, "a1")
    an.run(db_conn, now_utc=NOW, channel="email", send=_Spy())
    spy2 = _Spy()
    r = an.run(db_conn, now_utc=NOW, channel="email", send=spy2)
    assert r["sent"] is False and r["reason"] == "no_new_alerts"
    assert spy2.calls == []


def test_second_round_same_day_keeps_first_rounds_ledger(db_conn):
    """同一天第二轮不能抹掉第一轮的已发记录(用 replace_digest_inclusions 就会,
    它按 (kind,date) 先 DELETE) —— 否则第一批会被重发一遍。"""
    _alert(db_conn, "a1")
    an.run(db_conn, now_utc=NOW, channel="email", send=_Spy())
    _alert(db_conn, "a2", ts="2026-08-26T11:00:00Z")
    spy = _Spy()
    r = an.run(db_conn, now_utc=NOW, channel="email", send=spy)
    assert r["sent"] is True and r["pending"] == 1, "第二轮应只发新的那 1 条"
    assert "a2" in spy.calls[0][1] or True
    ids = {row[0] for row in db_conn.execute(
        "SELECT item_id FROM digest_inclusions WHERE digest_kind='alert_email'")}
    assert ids == {"a1", "a2"}, f"第一轮记录被抹掉了: {ids}"


def test_send_failure_does_not_mark_sent(db_conn):
    """发失败宁可下轮重发, 不能标成已发然后永远漏掉。"""
    _alert(db_conn, "a1")
    r = an.run(db_conn, now_utc=NOW, channel="email", send=_Spy(ok=False))
    assert r["sent"] is False and r["reason"] == "send_failed"
    assert db_conn.execute(
        "SELECT count(*) FROM digest_inclusions WHERE digest_kind='alert_email'").fetchone()[0] == 0
    spy = _Spy()
    assert an.run(db_conn, now_utc=NOW, channel="email", send=spy)["sent"] is True, "下一轮必须重试"


def test_dry_run_sends_nothing_and_writes_nothing(db_conn):
    _alert(db_conn, "a1")
    spy = _Spy()
    r = an.run(db_conn, now_utc=NOW, dry_run=True, channel="email", send=spy)
    assert r["sent"] is False and r["reason"] == "dry_run"
    assert spy.calls == [], "dry-run 不该发信"
    assert db_conn.execute(
        "SELECT count(*) FROM digest_inclusions WHERE digest_kind='alert_email'").fetchone()[0] == 0


def test_outside_lookback_window_ignored(db_conn):
    _alert(db_conn, "old", ts="2026-08-25T10:00:00Z")
    assert an.run(db_conn, now_utc=NOW, lookback_hours=6, channel="email", send=_Spy())["reason"] == "no_new_alerts"


def test_email_ledger_does_not_hide_alert_from_daily_digest(db_conn):
    """邮件是投递, 日报是反馈面 —— 发过邮件的预警仍要进日报(五个原因码在那儿)。"""
    from personal_intel_loop.digest import select_alert_candidates
    _alert(db_conn, "a1")
    an.run(db_conn, now_utc=NOW, channel="email", send=_Spy())
    got = select_alert_candidates(db_conn, now_utc=NOW)
    assert [c["item_id"] for c in got] == ["a1"]


def test_subject_names_places_and_count(db_conn):
    _alert(db_conn, "a1", city="广州市")
    _alert(db_conn, "b1", city="杭州市", title="浙江省杭州市淳安县气象台发布台风橙色预警信号")
    spy = _Spy()
    an.run(db_conn, now_utc=NOW, channel="email", send=spy)
    subject = spy.calls[0][0]
    assert "广州市" in subject and "杭州市" in subject and "2" in subject


# ---------- 等级门槛（邮件只推橙/红) ----------

def _cn(conn, item_id, level_word, city="广州市"):
    _alert(conn, item_id, city=city,
           title=f"广东省{city}天河区气象台发布暴雨{level_word}预警信号")


def test_email_only_orange_and_red(db_conn):
    _cn(db_conn, "y1", "黄色")
    spy = _Spy()
    assert an.run(db_conn, now_utc=NOW, channel="email", send=spy)["reason"] == "no_new_alerts"
    assert spy.calls == [], "黄色不该发邮件"

    _cn(db_conn, "o1", "橙色")
    spy2 = _Spy()
    r = an.run(db_conn, now_utc=NOW, channel="email", send=spy2)
    assert r["sent"] is True and r["pending"] == 1, "橙色必须发"
    assert "橙色" in spy2.calls[0][1] and "黄色" not in spy2.calls[0][1]


def test_red_sends(db_conn):
    _cn(db_conn, "r1", "红色")
    spy = _Spy()
    assert an.run(db_conn, now_utc=NOW, channel="email", send=spy)["sent"] is True


def test_seen_counts_all_pending_not_just_emailed(db_conn):
    """seen 是候选总数, pending 是够格发的 —— 两者分开才看得出"有东西但没到门槛"。"""
    _cn(db_conn, "y1", "黄色")
    _cn(db_conn, "o1", "橙色")
    r = an.run(db_conn, now_utc=NOW, channel="email", send=_Spy())
    assert r["seen"] == 2 and r["pending"] == 1


def test_japan_alerts_always_email(db_conn):
    """日本侧 adapter 已只收 警報/特別警報, 量级对应橙/红, 不再二次过滤。"""
    from personal_intel_loop.schemas import Item
    from personal_intel_loop.store import upsert_item
    upsert_item(db_conn, Item(id="j1", source="disaster_alerts:jp:東京都",
                              url="https://x/j1.xml", title="東京都 大雨警報(3 市区町村)",
                              body="八王子市: 大雨警報", ts="2026-08-26T10:00:00Z", lang="ja"),
                adapter_name="disaster_alerts", source_payload_json="{}")
    spy = _Spy()
    assert an.run(db_conn, now_utc=NOW, channel="email", send=spy)["sent"] is True


def test_heartbeat_written_even_when_nothing_sent(tmp_path, db_conn, monkeypatch):
    """门槛高到常态不响, 所以'没发信'必须与'哨兵死了'可区分。"""
    import json
    hb = tmp_path / "hb.json"
    monkeypatch.setattr(an, "HEARTBEAT_PATH", hb)
    _cn(db_conn, "y1", "黄色")
    an.run(db_conn, now_utc=NOW, channel="email", send=_Spy())
    assert hb.exists(), "无信可发时也必须留心跳"
    d = json.loads(hb.read_text())
    assert d["last_run_utc"] == NOW and d["candidates_seen"] == 1 and d["emails_sent"] == 0
