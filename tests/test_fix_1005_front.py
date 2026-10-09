"""10-05 验收修复（后端）· H204：「取消加权」发 unboost，后端白名单不收返回 400。

修法（VERDICT_H H204）：set_pipeline 白名单补 unboost，语义是只撤销加权、不动屏蔽。
"""
from __future__ import annotations

import json

import pytest

from personal_intel_loop.paper_api import set_pipeline
from personal_intel_loop.store import connect_db, ensure_schema, upsert_item
from tests.conftest import make_item

NOW = "2026-10-04T00:00:00Z"


@pytest.fixture
def conn(tmp_path):
    conn = connect_db(tmp_path / "pil.sqlite3")
    ensure_schema(conn)
    yield conn
    conn.close()


def _seed_source(conn, source, key="item:s1"):
    upsert_item(
        conn,
        make_item(item_id=key, source=source, url=f"https://example.com/{key}", title=f"标题 {key}"),
        adapter_name="rss_briefing",
        source_payload_json=json.dumps({"feed_name": "源一"}, ensure_ascii=False),
    )


def test_set_pipeline_unboost_only_clears_boost(conn):
    # 10-05 验收 H204：unboost 必须被接受，且只撤销加权、保留屏蔽
    _seed_source(conn, "rss_briefing:src1")
    set_pipeline(conn, {"kind": "source", "key": "rss_briefing:src1", "action": "mute"}, now_utc=NOW)
    set_pipeline(conn, {"kind": "source", "key": "rss_briefing:src1", "action": "boost"}, now_utc=NOW)
    row = conn.execute("SELECT muted, boosted FROM source_overrides WHERE source='rss_briefing:src1'").fetchone()
    assert (row["muted"], row["boosted"]) == (1, 1)

    result = set_pipeline(conn, {"kind": "source", "key": "rss_briefing:src1", "action": "unboost"}, now_utc=NOW)
    assert result["ok"] is True
    assert result["action"] == "unboost"
    row = conn.execute("SELECT muted, boosted FROM source_overrides WHERE source='rss_briefing:src1'").fetchone()
    assert (row["muted"], row["boosted"]) == (1, 0)  # 屏蔽不动


def test_set_pipeline_unboost_is_idempotent_on_unboosted(conn):
    _seed_source(conn, "rss_briefing:src2", key="item:s2")
    set_pipeline(conn, {"kind": "source", "key": "rss_briefing:src2", "action": "boost"}, now_utc=NOW)
    set_pipeline(conn, {"kind": "source", "key": "rss_briefing:src2", "action": "unboost"}, now_utc=NOW)
    set_pipeline(conn, {"kind": "source", "key": "rss_briefing:src2", "action": "unboost"}, now_utc=NOW)
    row = conn.execute("SELECT muted, boosted FROM source_overrides WHERE source='rss_briefing:src2'").fetchone()
    assert (row["muted"], row["boosted"]) == (0, 0)


def test_set_pipeline_author_unboost_rejected(conn):
    # author 本来就不支持加权，unboost 同样拒绝（与 boost 一致）
    with pytest.raises(ValueError):
        set_pipeline(conn, {"kind": "author", "key": "作者甲", "action": "unboost"}, now_utc=NOW)
