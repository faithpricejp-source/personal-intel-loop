"""web 路由(契约第 5 节): POST /api/paper/inbox、POST /api/paper/inbox/read、GET /api/paper/notifications。"""
from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from personal_intel_loop import web
from personal_intel_loop.store import connect_db, ensure_schema


@pytest.fixture
def client(monkeypatch, tmp_path):
    db = tmp_path / "web.sqlite"
    conn = connect_db(db)
    ensure_schema(conn)
    conn.commit()
    conn.close()
    monkeypatch.setattr(web, "DB_PATH", db)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    http_client = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
    try:
        yield http_client
    finally:
        http_client.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _post(client, path, payload):
    client.request(
        "POST",
        path,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    response = client.getresponse()
    return response.status, json.loads(response.read().decode("utf-8"))


def _get(client, path):
    client.request("GET", path)
    response = client.getresponse()
    return response.status, json.loads(response.read().decode("utf-8"))


def test_inbox_accept_route(client):
    payload = {"source": "pil.alerts", "title": "東京都 大雨警報", "body": "正文", "priority": "urgent", "dedup_key": "k1"}
    status, result = _post(client, "/api/paper/inbox", payload)
    assert status == 200 and set(result.keys()) == {"ok", "inbox_id", "deduped"}
    assert result == {"ok": True, "inbox_id": 1, "deduped": False}
    # 同 dedup_key 再投 → deduped
    status, result = _post(client, "/api/paper/inbox", payload)
    assert status == 200 and result["deduped"] is True
    status, result = _post(client, "/api/paper/inbox", {"source": "x"})
    assert status == 400 and "error" in result


def test_inbox_read_route_records_open_inbox_event(client):
    _post(client, "/api/paper/inbox", {"source": "pil.alerts", "title": "快讯", "priority": "urgent"})
    status, result = _post(client, "/api/paper/inbox/read", {"inbox_id": 1})
    assert status == 200 and result == {"ok": True}
    status, result = _post(client, "/api/paper/inbox/read", {"inbox_id": 999})
    assert status == 404 and "error" in result
    # 契约第 5 节: 点开即标已读, 也算一次行为事件 open_inbox(ms=null)
    conn = connect_db(web.DB_PATH)
    try:
        event = conn.execute("SELECT kind, ms, item_id, meta_json FROM ui_events WHERE kind='open_inbox'").fetchone()
        assert event is not None
        assert event["ms"] is None and event["item_id"] is None
        assert json.loads(event["meta_json"]) == {"inbox_id": 1}
        assert conn.execute("SELECT read_at FROM inbox WHERE inbox_id=1").fetchone()["read_at"] is not None
    finally:
        conn.close()


def test_notifications_route(client):
    status, result = _post(client, "/api/paper/inbox", {"source": "pil.alerts", "title": "快讯", "priority": "urgent"})
    assert status == 200
    status, result = _get(client, "/api/paper/notifications")
    assert status == 200 and set(result.keys()) == {"now", "notifications"}
    assert [n["id"] for n in result["notifications"]] == ["inbox:1"]
    assert set(result["notifications"][0].keys()) == {"id", "title", "body", "open_path"}
    status, result = _get(client, "/api/paper/notifications?since=2099-01-01T00:00:00Z")
    assert status == 200 and result["notifications"] == []
    status, result = _get(client, "/api/paper/notifications?since=garbage")
    assert status == 400 and "error" in result


def test_startup_drain_uses_env_spool(monkeypatch, tmp_path):
    """serve() 启动补收: startup_drain() 读 TODAY_PAPER_SPOOL 并入库。"""
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "a.json").write_text(json.dumps({"source": "proj.x", "title": "积压的通知", "priority": "normal"}), "utf-8")
    monkeypatch.setenv("TODAY_PAPER_SPOOL", str(spool))
    monkeypatch.setattr(web, "DB_PATH", tmp_path / "db.sqlite")

    web.startup_drain()

    assert (spool / "done" / "a.json").is_file()
    conn = connect_db(web.DB_PATH)
    try:
        assert conn.execute("SELECT title FROM inbox").fetchone()["title"] == "积压的通知"
    finally:
        conn.close()
