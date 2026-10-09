"""alerts_notify paper 通道: 预警写 inbox 而不发邮件(注入会抛异常的发信函数证明没调)。"""
from __future__ import annotations

import pytest

from personal_intel_loop import alerts_notify as an
from personal_intel_loop.schemas import Item
from personal_intel_loop.store import upsert_item

NOW = "2026-08-26T12:00:00Z"


def _alert(conn, item_id, *, city="广州市", ts="2026-08-26T10:35:00Z", title="广东省广州市天河区气象台发布雷雨大风橙色预警信号"):
    upsert_item(
        conn,
        Item(id=item_id, source=f"disaster_alerts:cn:{city}",
             url=f"https://www.nmc.cn/publish/alarm/{item_id}.html",
             title=title, body=f"{title} 的正文", ts=ts, lang="zh"),
        adapter_name="disaster_alerts", source_payload_json="{}")


def _exploding_send(subject, body):
    raise AssertionError("paper 通道不得发邮件")


def _run(conn, **kwargs):
    kwargs.setdefault("now_utc", NOW)
    kwargs.setdefault("send", _exploding_send)
    return an.run(conn, **kwargs)


def test_default_channel_is_paper_writes_inbox_not_email(db_conn):
    """缺省通道 = paper: 注入会抛异常的发信函数, 没抛 = 没调; 预警进了 inbox。"""
    _alert(db_conn, "a1")
    _alert(db_conn, "a2", city="杭州市", title="浙江省杭州市淳安县气象台发布台风橙色预警信号")
    result = _run(db_conn)
    assert result["sent"] is True and result["channel"] == "paper" and result["accepted"] == 2
    rows = db_conn.execute("SELECT * FROM inbox ORDER BY inbox_id").fetchall()
    assert [row["title"] for row in rows] == [
        "广东省广州市天河区气象台发布雷雨大风橙色预警信号",
        "浙江省杭州市淳安县气象台发布台风橙色预警信号",
    ]
    assert all(row["source"] == "pil.alerts" and row["priority"] == "urgent" for row in rows)
    assert all(row["dedup_key"] == item_id for row, item_id in zip(rows, ("a1", "a2"))), "dedup_key 用原来的去重键(item_id)"
    assert all(row["url"] == f"https://www.nmc.cn/publish/alarm/{item_id}.html" for row, item_id in zip(rows, ("a1", "a2")))


def test_paper_channel_marks_ledger_so_next_run_is_quiet(db_conn):
    _alert(db_conn, "a1")
    assert _run(db_conn)["sent"] is True
    result = _run(db_conn)
    assert result["reason"] == "no_new_alerts" and result["sent"] is False
    assert db_conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 1


def test_paper_channel_dry_run_writes_nothing(db_conn):
    _alert(db_conn, "a1")
    result = _run(db_conn, dry_run=True)
    assert result["sent"] is False and result["reason"] == "dry_run" and result["channel"] == "paper"
    assert db_conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0
    assert db_conn.execute(
        "SELECT COUNT(*) FROM digest_inclusions WHERE digest_kind='alert_email'").fetchone()[0] == 0


def test_paper_channel_accept_failure_retries_next_round(db_conn):
    _alert(db_conn, "a1")

    def broken_accept(conn, payload, *, now_utc):
        raise RuntimeError("spool 落盘前的瞬时故障")

    result = _run(db_conn, accept=broken_accept)
    assert result["sent"] is False and result["reason"] == "accept_failed"
    assert db_conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0
    assert db_conn.execute(
        "SELECT COUNT(*) FROM digest_inclusions WHERE digest_kind='alert_email'").fetchone()[0] == 0, "失败不标已发"
    assert _run(db_conn)["sent"] is True, "下一轮重试"


def test_paper_channel_keeps_level_threshold(db_conn):
    """email_worthy 的橙/红门槛对 paper 通道同样生效(urgent 是打断通道, 门槛同源)。"""
    _alert(db_conn, "y1", title="广东省广州市天河区气象台发布暴雨黄色预警信号")
    result = _run(db_conn)
    assert result["reason"] == "no_new_alerts"
    assert db_conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0


def test_unsupported_channel_rejected(db_conn):
    with pytest.raises(ValueError):
        _run(db_conn, channel="sms")


def test_email_channel_still_sends(db_conn):
    """email 通道行为与之前完全一致(断言与既有 test_alerts_notify 同款)。"""
    calls = []

    def send(subject, body):
        calls.append((subject, body))
        return True

    _alert(db_conn, "a1")
    result = an.run(db_conn, now_utc=NOW, channel="email", send=send)
    assert result["sent"] is True and result["channel"] == "email"
    assert len(calls) == 1
    assert db_conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0, "email 通道不写 inbox"
