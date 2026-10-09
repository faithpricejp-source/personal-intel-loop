"""新接口: GET /api/paper/adjustments、POST /api/paper/adjustments/revert、Edition 的 reading_note。"""
from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from personal_intel_loop import web
from personal_intel_loop.paper_api import get_editions, list_adjustments, revert_adjustment
from personal_intel_loop.store import connect_db, ensure_schema, upsert_item
from tests._behavior_helpers import NOW
from tests.conftest import make_item


@pytest.fixture
def conn(tmp_path):
    c = connect_db(tmp_path / "db.sqlite")
    ensure_schema(c)
    try:
        yield c
    finally:
        c.close()


def _seed_adjustment(conn, *, adjustment_id=1, key="rss_briefing:src", delta=0.04, reverted=0, offset=0.04):
    conn.execute("UPDATE source_trust SET trust_score=0.35 WHERE source=?", (key,))
    conn.execute(
        "INSERT OR REPLACE INTO ai_adjustments (id, ts, for_date, kind, key, label, delta, before, after, reason, reverted, model) VALUES (?, ?, '2026-10-04', 'source', ?, '此源信任 0.35→0.39', ?, 0.35, 0.39, '依据:深读三次', ?, 'm')",
        (adjustment_id, NOW, key, delta, reverted),
    )
    if not reverted:
        conn.execute(
            "INSERT OR REPLACE INTO knob_offsets (kind, key, offset, updated_at) VALUES ('source', ?, ?, ?)",
            (key, offset, NOW),
        )
    conn.commit()


def test_list_adjustments_keys_and_order(conn):
    _seed_adjustment(conn, adjustment_id=1)
    _seed_adjustment(conn, adjustment_id=2, delta=0.02, offset=0.02)
    result = list_adjustments(conn)
    assert set(result.keys()) == {"adjustments"}
    assert len(result["adjustments"]) == 2
    assert result["adjustments"][0]["id"] == 2  # 新的在前
    expected_keys = {"id", "ts", "kind", "key", "label", "before", "after", "reason", "reverted"}
    for adjustment in result["adjustments"]:
        assert set(adjustment.keys()) == expected_keys
        assert adjustment["reverted"] is False
    assert list_adjustments(conn, limit=1)["adjustments"][0]["id"] == 2
    with pytest.raises(ValueError):
        list_adjustments(conn, limit="很多")


def test_revert_adjustment_restores_offset(conn):
    _seed_adjustment(conn, adjustment_id=1)
    result = revert_adjustment(conn, 1, now_utc=NOW)
    # 契约键集合
    assert set(result.keys()) == {"ok", "key", "before", "after"}
    assert result["ok"] is True and result["key"] == "rss_briefing:src"
    assert abs(result["before"] - 0.39) < 1e-9 and abs(result["after"] - 0.35) < 1e-9
    offset = conn.execute("SELECT offset FROM knob_offsets WHERE kind='source' AND key='rss_briefing:src'").fetchone()
    assert offset["offset"] == 0.0  # delta 从 offset 里减回
    row = conn.execute("SELECT reverted FROM ai_adjustments WHERE id=1").fetchone()
    assert row["reverted"] == 1
    # 列表里显示已撤销
    assert list_adjustments(conn)["adjustments"][0]["reverted"] is True


def test_revert_adjustment_twice_and_unknown(conn):
    _seed_adjustment(conn, adjustment_id=1)
    revert_adjustment(conn, 1, now_utc=NOW)
    with pytest.raises(ValueError):  # 已撤销的再撤 → 400
        revert_adjustment(conn, 1, now_utc=NOW)
    with pytest.raises(KeyError):  # 未知 id → 404
        revert_adjustment(conn, 99, now_utc=NOW)


def test_edition_carries_reading_note(conn):
    upsert_item(
        conn,
        make_item(item_id="it:1", source="rss_briefing:src"),
        adapter_name="rss_briefing",
        source_payload_json="{}",
    )
    conn.execute(
        "INSERT INTO editions (edition_date, item_id, section, rank, blind_reason, built_at) VALUES ('2026-10-04', 'it:1', 'lead', 0, NULL, ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO edition_notes (edition_date, reading_note, model, created_at) VALUES ('2026-10-04', '昨天你深读了X, 跳过了Y, 和画像里「关心气候」一致。', 'm', ?)",
        (NOW,),
    )
    # reading_note 记在「行为日期+1」的期上: 行为 10-03 → 注记挂 10-04 这一期
    conn.execute(
        "INSERT OR REPLACE INTO edition_notes (edition_date, reading_note, model, created_at) VALUES ('2026-10-03', '旧一期不会带这条', 'm', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO editions (edition_date, item_id, section, rank, blind_reason, built_at) VALUES ('2026-10-03', 'it:1', 'lead', 0, NULL, ?)",
        (NOW,),
    )
    conn.commit()
    result = get_editions(conn, before="2026-10-05", limit=3)
    notes = {edition["date"]: edition["reading_note"] for edition in result["editions"]}
    assert notes["2026-10-04"].startswith("昨天你深读了X")
    assert notes["2026-10-03"] == "旧一期不会带这条"


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


def _post(client, path, payload):
    client.request("POST", path, body=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json"})
    response = client.getresponse()
    return response.status, json.loads(response.read().decode("utf-8"))


def test_adjustment_routes(client):
    http_client, db = client
    c = connect_db(db)
    try:
        ensure_schema(c)
        _seed_adjustment(c, adjustment_id=1, key="rss_briefing:src", delta=0.04, offset=0.04)
        _seed_adjustment(c, adjustment_id=2, key="rss_briefing:src2", delta=0.02, offset=0.02)
    finally:
        c.close()

    http_client.request("GET", "/api/paper/adjustments")
    response = http_client.getresponse()
    body = json.loads(response.read().decode("utf-8"))
    assert response.status == 200 and len(body["adjustments"]) == 2
    assert set(body["adjustments"][0].keys()) == {"id", "ts", "kind", "key", "label", "before", "after", "reason", "reverted"}

    status, payload = _post(http_client, "/api/paper/adjustments/revert", {"id": 1})
    assert status == 200 and set(payload.keys()) == {"ok", "key", "before", "after"}
    assert payload["key"] == "rss_briefing:src"
    status, payload = _post(http_client, "/api/paper/adjustments/revert", {"id": 1})
    assert status == 400 and "error" in payload  # 二次撤销
    status, payload = _post(http_client, "/api/paper/adjustments/revert", {"id": 99})
    assert status == 404 and "error" in payload

    check = connect_db(db)
    try:
        offsets = {row["key"]: row["offset"] for row in check.execute("SELECT key, offset FROM knob_offsets WHERE kind='source'")}
        assert offsets["rss_briefing:src"] == 0.0  # id=1 撤销: 0.04-0.04
        assert offsets["rss_briefing:src2"] == 0.02  # id=2 不受影响
    finally:
        check.close()
