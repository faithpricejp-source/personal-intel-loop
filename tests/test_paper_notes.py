"""契约第 11 节：批注存取，以及次日复盘提示词里带上批注。"""
from datetime import date

import pytest

from personal_intel_loop import paper_feedback, paper_learn
from personal_intel_loop.store import connect_db, ensure_schema

NOW = "2026-10-04T03:00:00Z"


@pytest.fixture
def conn(tmp_path):
    c = connect_db(tmp_path / "t.sqlite")
    ensure_schema(c)
    c.execute(
        "INSERT INTO items(item_id,source,url,title,body,author,ts,lang,tags_json,source_payload_json,content_hash,adapter_name,first_ingested_at,last_seen_at) "
        "VALUES('i1','rss_briefing:a','https://e.com/a','港口那篇','b',NULL,?,'zh','[]','{}','h','rss_briefing',?,?)",
        (NOW, NOW, NOW),
    )
    c.commit()
    return c


def test_save_update_delete_note(conn):
    r = paper_feedback.save_note(conn, "i1", "  论证跳了一步，传导链缺中间环节  ", now_utc=NOW)
    assert r["note"] == "论证跳了一步，传导链缺中间环节" and r["effect"]["kind"] == "queued"
    paper_feedback.save_note(conn, "i1", "改过的批注", now_utc="2026-10-04T04:00:00Z")
    assert conn.execute("SELECT text FROM item_notes").fetchone()[0] == "改过的批注"
    paper_feedback.save_note(conn, "i1", "", now_utc=NOW)
    assert conn.execute("SELECT COUNT(*) FROM item_notes").fetchone()[0] == 0
    with pytest.raises(KeyError):
        paper_feedback.save_note(conn, "nope", "x", now_utc=NOW)


def test_notes_reach_learn_prompt_without_impressions(conn):
    paper_feedback.save_note(conn, "i1", "这个作者值得多看", now_utc=NOW)
    seen = {}

    def fake_llm(prompt):
        seen["prompt"] = prompt
        return '{"adjustments": [], "proposals": [], "reading_note": ""}'

    out = paper_learn.learn(conn, date_local=date(2026, 10, 4), now_utc="2026-10-05T00:00:00Z", llm_call=fake_llm)
    assert "skipped" not in out
    assert "这个作者值得多看" in seen["prompt"] and "港口那篇" in seen["prompt"]


def test_fts_sync_by_rowid_updates_only_own_row(tmp_path):
    # 10-05：_sync_fts 改按 rowid 删改；同一条重复写入只留一行，别的条目的索引不被误删
    from personal_intel_loop.store import connect_db, ensure_schema, upsert_item, reindex_fts
    from personal_intel_loop.schemas import Item

    conn = connect_db(tmp_path / "f.sqlite")
    ensure_schema(conn)

    def mk(i, body):
        return Item(id=f"x:{i}", source="rss_briefing:s", url=f"https://e.com/{i}", title=f"标题{i}", body=body,
                    author=None, ts="2026-10-05T00:00:00Z", lang="zh", transcript=None, summary=None, tags=[])

    with conn:
        upsert_item(conn, mk(1, "苹果 alpha"), adapter_name="rss_briefing", source_payload_json="{}")
        upsert_item(conn, mk(2, "香蕉 beta"), adapter_name="rss_briefing", source_payload_json="{}")
        upsert_item(conn, mk(1, "橙子 gamma"), adapter_name="rss_briefing", source_payload_json="{}")
    rows = conn.execute("SELECT item_id FROM items_fts ORDER BY item_id").fetchall()
    assert [r[0] for r in rows] == ["x:1", "x:2"]
    assert conn.execute("SELECT item_id FROM items_fts WHERE items_fts MATCH 'beta'").fetchall()[0][0] == "x:2"
    assert conn.execute("SELECT count(*) FROM items_fts WHERE items_fts MATCH 'alpha'").fetchone()[0] == 0
    assert reindex_fts(conn) == 2
    assert conn.execute("SELECT f.item_id FROM items_fts f JOIN items i ON i.rowid=f.rowid").fetchall() and all(
        r[0] == r[1] for r in conn.execute("SELECT f.item_id, i.item_id FROM items_fts f JOIN items i ON i.rowid=f.rowid"))
