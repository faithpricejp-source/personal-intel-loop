"""web 路由: ThreadingHTTPServer 起在随机端口, 每个新端点打一遍; 既有路由不受影响。"""
from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from personal_intel_loop import web
from personal_intel_loop.store import connect_db, ensure_schema, upsert_item
from tests.conftest import make_item

NOW = "2026-10-04T00:00:00Z"


@pytest.fixture
def client(monkeypatch, tmp_path):
    db = tmp_path / "web.sqlite"
    conn = connect_db(db)
    ensure_schema(conn)
    upsert_item(
        conn,
        make_item(item_id="rss:web1", source="rss_briefing:feed"),
        adapter_name="rss_briefing",
        source_payload_json=json.dumps({"feed_name": "Feed"}, ensure_ascii=False),
    )
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


def _get(client, path):
    client.request("GET", path)
    response = client.getresponse()
    return response.status, response.read()


def _post(client, path, payload):
    client.request(
        "POST",
        path,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    response = client.getresponse()
    return response.status, json.loads(response.read().decode("utf-8"))


def test_paper_static_page_served(client):
    for path in ("/paper", "/paper/", "/paper/index.html"):
        status, body = _get(client, path)
        assert status == 200, path
        assert "<script src=\"app.js\"></script>" in body.decode("utf-8")  # 10-04 合入正式前端，不再是占位页


def test_paper_static_rejects_traversal_and_missing(client):
    status, body = _get(client, "/paper/../x")
    assert status == 404 and b"error" in body
    status, _ = _get(client, "/paper/missing.js")
    assert status == 404
    status, _ = _get(client, "/paper/sub/dir.js")
    assert status == 404
    status, _ = _get(client, "/api/nothing")
    assert status == 404


def test_paper_editions_endpoint(client):
    status, body = _get(client, "/api/paper/editions?limit=1")
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert set(payload.keys()) == {"editions", "next_before"}
    status, body = _get(client, "/api/paper/editions")
    assert status == 200
    status, body = _get(client, "/api/paper/editions?limit=0")
    assert status == 200  # limit 钳制到 [1, 3]
    assert len(json.loads(body.decode("utf-8"))["editions"]) <= 1
    status, body = _get(client, "/api/paper/editions?before=garbage")
    assert status == 400


def test_paper_item_endpoint(client):
    status, body = _get(client, "/api/paper/item/rss:web1")
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert set(payload.keys()) >= {"item_id", "fulltext", "edition_date", "my", "trust", "why_here"}
    status, body = _get(client, "/api/paper/item/rss:ghost")
    assert status == 404 and b"error" in body


def test_paper_rate_endpoint(client):
    status, payload = _post(client, "/api/paper/rate", {"item_id": "rss:web1", "dim": "style", "value": 1})
    assert status == 200 and payload["ok"] is True
    assert set(payload.keys()) == {"ok", "item_id", "dim", "value", "effect"}
    assert payload["effect"]["kind"] == "queued"
    status, payload = _post(client, "/api/paper/rate", {"item_id": "rss:web1", "dim": "overall", "value": 1})
    assert status == 200 and payload["effect"]["kind"] == "source_trust"
    status, payload = _post(client, "/api/paper/rate", {"item_id": "rss:ghost", "dim": "style", "value": 1})
    assert status == 404
    status, payload = _post(client, "/api/paper/rate", {"item_id": "rss:web1", "dim": "bogus", "value": 1})
    assert status == 400
    status, payload = _post(client, "/api/paper/rate", {"item_id": "rss:web1", "dim": "style", "value": 9})
    assert status == 400


def test_paper_pipeline_endpoints(client):
    status, body = _get(client, "/api/paper/pipeline")
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert set(payload.keys()) == {"sources", "authors", "recent", "profile_dir", "health"}  # 10-05 验收 C1: +health
    status, payload = _post(client, "/api/paper/pipeline", {"kind": "source", "key": "rss_briefing:feed", "action": "mute"})
    assert status == 200 and payload["ok"] is True and payload["muted"] is True
    status, payload = _post(client, "/api/paper/pipeline", {"kind": "author", "key": "nobody", "action": "boost"})
    assert status == 400
    status, payload = _post(client, "/api/paper/pipeline", {"kind": "author", "key": "ghost", "action": "mute"})
    assert status == 404


def test_paper_read_endpoint(client):
    status, payload = _post(client, "/api/paper/read", {"item_id": "rss:web1", "dwell_ms": 4321})
    assert status == 200 and payload == {"ok": True}
    status, payload = _post(client, "/api/paper/read", {"item_id": "rss:ghost", "dwell_ms": 1})
    assert status == 404


def test_auth_routes(client):
    status, body = _get(client, "/api/paper/auth")
    assert status == 200
    channels = json.loads(body.decode("utf-8"))["channels"]
    assert {"key", "label", "ok", "checked_at", "detail", "since_failing", "relogin_hint"} <= set(channels[0].keys())
    # relogin 只认注册表: 前端传的 command 字段被忽略; 未登记 key → 404
    status, payload = _post(client, "/api/paper/auth/relogin", {"key": "weread", "command": "rm -rf /"})
    assert status == 200 and payload == {"ok": True, "open_url": "https://weread.qq.com/#login"}
    status, payload = _post(client, "/api/paper/auth/relogin", {"key": "not-registered"})
    assert status == 404
    status, payload = _post(client, "/api/paper/auth/relogin", {"key": ""})
    assert status == 400


def test_archive_routes(client):
    status, body = _get(client, "/api/paper/archive?q=Example")
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert set(payload.keys()) == {"items", "next_before"}
    assert payload["items"] and set(payload["items"][0].keys()) == {
        "item_id", "title", "url", "source", "source_label", "author_label", "published_at", "one_liner", "on_paper",
    }
    status, body = _get(client, "/api/paper/archive/sources")
    assert status == 200
    assert json.loads(body.decode("utf-8"))["sources"][0]["source"] == "rss_briefing:feed"
    status, _ = _get(client, "/api/paper/archive?date=not-a-date")
    assert status == 400


def test_follow_suggestion_routes(client):
    status, body = _get(client, "/api/paper/follow_suggestions")
    assert status == 200 and json.loads(body.decode("utf-8")) == {"suggestions": []}
    status, payload = _post(client, "/api/paper/follow_suggestions/decide", {"id": "weibo:x", "decision": "dismiss"})
    assert status == 404
    status, payload = _post(client, "/api/paper/follow_suggestions/decide", {"id": "x", "decision": "bogus"})
    assert status == 400


def test_trip_routes(client):
    status, body = _get(client, "/api/paper/trips")
    assert status == 200 and json.loads(body.decode("utf-8")) == {"trips": []}
    status, payload = _post(client, "/api/paper/trips", {"place": "大阪", "start_date": "2026-10-20", "end_date": "2026-10-25"})
    assert status == 200 and payload["ok"] is True
    trip_id = payload["trip"]["trip_id"]
    status, body = _get(client, "/api/paper/trips")
    assert [trip["trip_id"] for trip in json.loads(body.decode("utf-8"))["trips"]] == [trip_id]
    status, payload = _post(client, "/api/paper/trips/delete", {"trip_id": trip_id})
    assert status == 200 and payload == {"ok": True}
    status, payload = _post(client, "/api/paper/trips/delete", {"trip_id": trip_id})
    assert status == 404
    status, payload = _post(client, "/api/paper/trips", {"place": "大阪", "start_date": "bad-date"})
    assert status == 400


def test_existing_feedback_route_still_works(client):
    status, payload = _post(
        client,
        "/api/feedback",
        {"item_id": "rss:web1", "action": "keep", "digest_date": "2026-10-04"},
    )
    assert status == 200 and payload["recorded"] is True
    assert "trust_before" in payload and "trust_after" in payload


def test_existing_pages_still_served(client):
    status, _ = _get(client, "/")
    assert status in (200, 400)  # 无日报文件时按原逻辑 400(FileNotFoundError→400), 有则 200; 均非 404/500
    status, body = _get(client, "/claims")
    assert status == 200
    status, body = _get(client, "/profile")
    assert status == 200
