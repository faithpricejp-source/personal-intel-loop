"""drain_spool: 好文件→done/、坏文件→bad/、都不删除、目录缺失零副作用、同名冲突都保留。"""
from __future__ import annotations

import json

from personal_intel_loop.paper_inbox import drain_spool

NOW = "2026-10-04T01:00:00Z"


def _write(spool, name, payload):
    spool.mkdir(parents=True, exist_ok=True)
    path = spool / name
    path.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    return path


def _payload(**overrides):
    payload = {"source": "proj.notice", "title": "通知", "body": "b", "priority": "normal"}
    payload.update(overrides)
    return payload


def test_good_file_moved_to_done_and_stored(db_conn, tmp_path):
    spool = tmp_path / "spool"
    _write(spool, "a.json", _payload(title="第一条"))
    result = drain_spool(db_conn, spool, now_utc=NOW)
    assert result["accepted"] == 1 and result["bad"] == 0
    assert db_conn.execute("SELECT title FROM inbox WHERE inbox_id=1").fetchone()["title"] == "第一条"
    assert (spool / "done" / "a.json").is_file()
    assert not (spool / "a.json").exists(), "根目录文件必须被移走"


def test_bad_json_moved_to_bad(db_conn, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "broken.json").write_text("{not json", "utf-8")
    result = drain_spool(db_conn, spool, now_utc=NOW)
    assert result["accepted"] == 0 and result["bad"] == 1
    assert (spool / "bad" / "broken.json").read_text("utf-8") == "{not json"
    assert db_conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0


def test_invalid_payload_moved_to_bad(db_conn, tmp_path):
    spool = tmp_path / "spool"
    _write(spool, "missing_title.json", _payload(title=None))
    result = drain_spool(db_conn, spool, now_utc=NOW)
    assert result["accepted"] == 0 and result["bad"] == 1
    assert (spool / "bad" / "missing_title.json").is_file()


def test_files_never_deleted(db_conn, tmp_path):
    spool = tmp_path / "spool"
    _write(spool, "good1.json", _payload(title="一"))
    _write(spool, "good2.json", _payload(title="二"))
    _write(spool, "bad.json", _payload(title=None))
    drain_spool(db_conn, spool, now_utc=NOW)
    moved = [p.name for p in (spool / "done").iterdir()] + [p.name for p in (spool / "bad").iterdir()]
    assert sorted(moved) == ["bad.json", "good1.json", "good2.json"], "三个文件都必须还在(只是换了目录)"
    assert db_conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 2


def test_missing_spool_dir_is_noop_and_creates_nothing(db_conn, tmp_path):
    missing = tmp_path / "nope"
    result = drain_spool(db_conn, missing, now_utc=NOW)
    assert result == {"ok": True, "accepted": 0, "deduped": 0, "bad": 0}
    assert not missing.exists(), "目录缺失时不得创建任何目录"


def test_deduped_file_still_moved_to_done(db_conn, tmp_path):
    spool = tmp_path / "spool"
    _write(spool, "a.json", _payload(dedup_key="k1"))
    drain_spool(db_conn, spool, now_utc=NOW)
    _write(spool, "a.json", _payload(dedup_key="k1"))  # 同 key 重投(模拟投递方重试)
    result = drain_spool(db_conn, spool, now_utc="2026-10-04T05:00:00Z")
    assert result["accepted"] == 1 and result["deduped"] == 1
    assert db_conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 1


def test_name_collision_in_done_keeps_both(db_conn, tmp_path):
    spool = tmp_path / "spool"
    done = spool / "done"
    done.mkdir(parents=True)
    (done / "a.json").write_text("先前已收的存档", "utf-8")
    _write(spool, "a.json", _payload(title="新投递"))
    drain_spool(db_conn, spool, now_utc=NOW)
    assert (done / "a.json").read_text("utf-8") == "先前已收的存档", "原存档不得被覆盖"
    assert (done / "a.1.json").is_file()
