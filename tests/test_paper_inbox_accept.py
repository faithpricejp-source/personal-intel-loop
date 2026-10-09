"""paper_inbox.accept / mark_read: 字段校验、24h 去重、超长截断(选截断不选拒绝)。"""
from __future__ import annotations

import pytest

from personal_intel_loop.paper_inbox import accept, mark_read

NOW = "2026-10-04T01:00:00Z"


def _payload(**overrides):
    payload = {
        "source": "pil.alerts",
        "title": "東京都 大雨警報",
        "body": "正文",
        "url": "https://example.com/a",
        "priority": "urgent",
        "dedup_key": "da:j1",
    }
    payload.update(overrides)
    return payload


def test_accept_inserts_contract_fields(db_conn):
    result = accept(db_conn, _payload(), now_utc=NOW)
    assert set(result.keys()) == {"ok", "inbox_id", "deduped"}
    assert result == {"ok": True, "inbox_id": 1, "deduped": False}
    row = db_conn.execute("SELECT * FROM inbox WHERE inbox_id=1").fetchone()
    assert row["source"] == "pil.alerts"
    assert row["title"] == "東京都 大雨警報"
    assert row["body"] == "正文"
    assert row["url"] == "https://example.com/a"
    assert row["priority"] == "urgent"
    assert row["dedup_key"] == "da:j1"
    assert row["created_at"] == NOW
    assert row["read_at"] is None


def test_missing_source_or_title_rejected(db_conn):
    with pytest.raises(ValueError):
        accept(db_conn, {"title": "t"}, now_utc=NOW)
    with pytest.raises(ValueError):
        accept(db_conn, {"source": "s"}, now_utc=NOW)
    with pytest.raises(ValueError):
        accept(db_conn, {"source": "s", "title": "   "}, now_utc=NOW)


def test_priority_validated_and_defaults_normal(db_conn):
    with pytest.raises(ValueError):
        accept(db_conn, _payload(priority="high"), now_utc=NOW)
    result = accept(db_conn, _payload(priority=None), now_utc=NOW)
    assert result["deduped"] is False
    assert db_conn.execute("SELECT priority FROM inbox WHERE inbox_id=1").fetchone()["priority"] == "normal"


def test_body_and_url_and_dedup_key_defaults(db_conn):
    accept(db_conn, {"source": "x.proj", "title": "t"}, now_utc=NOW)
    row = db_conn.execute("SELECT body, url, dedup_key FROM inbox WHERE inbox_id=1").fetchone()
    assert row["body"] == "" and row["url"] is None and row["dedup_key"] is None
    with pytest.raises(ValueError):
        accept(db_conn, _payload(body=123), now_utc=NOW)
    with pytest.raises(ValueError):
        accept(db_conn, _payload(url=123), now_utc=NOW)
    # 空 dedup_key 与缺省等价 = 不去重
    accept(db_conn, _payload(dedup_key=""), now_utc=NOW)
    assert db_conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 2


def test_overlong_title_and_body_truncated(db_conn):
    """选截断: title ≤300 字、body ≤20000 字, 多余部分丢弃不报错。"""
    accept(db_conn, _payload(title="题" * 400, body="文" * 25000, dedup_key=None), now_utc=NOW)
    row = db_conn.execute("SELECT title, body FROM inbox WHERE inbox_id=1").fetchone()
    assert len(row["title"]) == 300 and row["title"] == "题" * 300
    assert len(row["body"]) == 20000


def test_same_dedup_key_within_24h_deduped(db_conn):
    first = accept(db_conn, _payload(), now_utc=NOW)
    second = accept(db_conn, _payload(), now_utc="2026-10-04T12:00:00Z")
    assert second == {"ok": True, "inbox_id": first["inbox_id"], "deduped": True}
    assert db_conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 1


def test_same_dedup_key_after_24h_accepted_again(db_conn):
    accept(db_conn, _payload(), now_utc=NOW)
    second = accept(db_conn, _payload(), now_utc="2026-10-05T01:00:01Z")
    assert second["deduped"] is False and second["inbox_id"] != 1
    assert db_conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 2


def test_dedup_only_within_same_key(db_conn):
    accept(db_conn, _payload(), now_utc=NOW)
    other = accept(db_conn, _payload(dedup_key="da:j2"), now_utc=NOW)
    assert other["deduped"] is False


def test_no_dedup_key_never_deduped(db_conn):
    first = accept(db_conn, _payload(dedup_key=None), now_utc=NOW)
    second = accept(db_conn, _payload(dedup_key=None, title="另一条"), now_utc=NOW)
    assert first["deduped"] is False and second["deduped"] is False
    assert second["inbox_id"] != first["inbox_id"]


def test_mark_read_sets_read_at(db_conn):
    inbox_id = accept(db_conn, _payload(), now_utc=NOW)["inbox_id"]
    result = mark_read(db_conn, inbox_id, now_utc="2026-10-04T02:00:00Z")
    assert result == {"ok": True}
    row = db_conn.execute("SELECT read_at FROM inbox WHERE inbox_id=1").fetchone()
    assert row["read_at"] == "2026-10-04T02:00:00Z"
    # 重复标读无害
    assert mark_read(db_conn, inbox_id, now_utc="2026-10-04T03:00:00Z") == {"ok": True}


def test_mark_read_unknown_id_keyerror(db_conn):
    with pytest.raises(KeyError):
        mark_read(db_conn, 999, now_utc=NOW)
    with pytest.raises(ValueError):
        mark_read(db_conn, "abc", now_utc=NOW)
