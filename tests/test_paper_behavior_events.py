"""行为事件接收: store_events 校验与入库 + POST /api/paper/events 路由(text/plain / 未知 kind 丢弃 / 超 500 拒绝)。"""
from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from http.server import ThreadingHTTPServer

import pytest

from personal_intel_loop import web
from personal_intel_loop.paper_events import store_events
from personal_intel_loop.store import connect_db, ensure_schema

TS = "2026-10-04T08:01:02.345+09:00"
NOW = "2026-10-03T23:10:00Z"


@pytest.fixture
def conn(tmp_path):
    c = connect_db(tmp_path / "db.sqlite")
    ensure_schema(c)
    try:
        yield c
    finally:
        c.close()


def test_store_events_writes_valid_rows(conn):
    payload = {
        "session_id": "s-1",
        "events": [
            {"ts": TS, "kind": "impression", "item_id": "rss:a", "edition_date": "2026-10-04", "ms": 1800, "meta": {"section": "top", "rank": 1}},
            {"ts": TS, "kind": "open_item", "item_id": "rss:a", "edition_date": "2026-10-04", "ms": None, "meta": {"from": "paper"}},
            {"ts": TS, "kind": "session_start", "ms": None},
        ],
    }
    result = store_events(conn, payload, now_utc=NOW)
    assert result == {"ok": True, "stored": 3, "dropped": 0}
    rows = conn.execute("SELECT session_id, ts, kind, item_id, edition_date, ms, meta_json, received_at FROM ui_events ORDER BY id").fetchall()
    assert [row["kind"] for row in rows] == ["impression", "open_item", "session_start"]
    assert rows[0]["item_id"] == "rss:a" and rows[0]["ms"] == 1800
    assert json.loads(rows[0]["meta_json"]) == {"section": "top", "rank": 1}
    assert rows[2]["item_id"] is None and json.loads(rows[2]["meta_json"]) == {}
    assert rows[0]["received_at"] == NOW


def test_store_events_drops_unknown_kind_and_bad_ms_and_bad_ts(conn):
    result = store_events(
        conn,
        {
            "session_id": "s-1",
            "events": [
                {"ts": TS, "kind": "click_like", "item_id": "rss:a", "ms": 1},  # 未知 kind
                {"ts": TS, "kind": "impression", "item_id": "rss:a", "ms": -5},  # 负 ms
                {"ts": TS, "kind": "impression", "item_id": "rss:a", "ms": "很多"},  # 非整数
                {"ts": "不是时间", "kind": "impression", "item_id": "rss:a", "ms": 1},  # 坏 ts
                {"ts": TS, "kind": "impression", "item_id": "rss:a", "ms": True},  # bool 不算整数
                "not-a-dict",  # 非对象
                {"ts": TS, "kind": "focus", "item_id": "rss:a", "ms": None},  # 合法
            ],
        },
        now_utc=NOW,
    )
    assert result == {"ok": True, "stored": 1, "dropped": 6}
    assert conn.execute("SELECT COUNT(*) FROM ui_events").fetchone()[0] == 1


def test_store_events_validations_raise(conn):
    with pytest.raises(ValueError):
        store_events(conn, {"session_id": "", "events": []}, now_utc=NOW)
    with pytest.raises(ValueError):
        store_events(conn, {"events": []}, now_utc=NOW)
    with pytest.raises(ValueError):
        store_events(conn, {"session_id": "s", "events": "不是列表"}, now_utc=NOW)
    too_many = [{"ts": TS, "kind": "focus", "ms": None} for _ in range(501)]
    with pytest.raises(ValueError):
        store_events(conn, {"session_id": "s", "events": too_many}, now_utc=NOW)
    assert conn.execute("SELECT COUNT(*) FROM ui_events").fetchone()[0] == 0


def test_store_events_accepts_exactly_500(conn):
    events = [{"ts": TS, "kind": "focus", "item_id": f"rss:{i}", "ms": None, "meta": {}} for i in range(500)]
    result = store_events(conn, {"session_id": "s", "events": events}, now_utc=NOW)
    assert result == {"ok": True, "stored": 500, "dropped": 0}


@pytest.fixture
def client(monkeypatch, tmp_path):
    db = tmp_path / "web.sqlite"
    c = connect_db(db)
    ensure_schema(c)
    c.close()
    monkeypatch.setattr(web, "DB_PATH", db)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    http_client = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
    try:
        yield http_client, db
    finally:
        http_client.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _post_raw(client, path, body: bytes, content_type: str):
    client.request("POST", path, body=body, headers={"Content-Type": content_type})
    response = client.getresponse()
    return response.status, response.read()


def test_route_accepts_text_plain_sendbeacon(client):
    http_client, db = client
    body = json.dumps({"session_id": "beacon-1", "events": [{"ts": TS, "kind": "session_end", "ms": 120000, "meta": {}}]}, ensure_ascii=False).encode("utf-8")
    status, raw = _post_raw(http_client, "/api/paper/events", body, "text/plain")
    assert status == 200
    assert json.loads(raw.decode("utf-8")) == {"ok": True, "stored": 1, "dropped": 0}
    check = sqlite3.connect(db)
    try:
        assert check.execute("SELECT COUNT(*) FROM ui_events").fetchone()[0] == 1
    finally:
        check.close()


def test_route_rejects_bad_json_and_over_500(client):
    http_client, _db = client
    status, raw = _post_raw(http_client, "/api/paper/events", b"{not json", "text/plain")
    assert status == 400 and b"error" in raw
    too_many = json.dumps({"session_id": "s", "events": [{"ts": TS, "kind": "focus", "ms": None} for _ in range(501)]}).encode("utf-8")
    status, raw = _post_raw(http_client, "/api/paper/events", too_many, "application/json")
    assert status == 400 and b"error" in raw
