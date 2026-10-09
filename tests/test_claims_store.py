"""claims 表: 可检验断言的幂等写入 / 列开放 / 结算。"""
from __future__ import annotations

from personal_intel_loop.store import list_open_claims, resolve_claim, upsert_claim


def test_claims_table_exists_after_ensure_schema(db_conn):
    row = db_conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='claims'").fetchone()
    assert row is not None


def test_upsert_claim_is_idempotent(db_conn):
    a = upsert_claim(db_conn, item_id="rss:x", source="rss_briefing:feed", claim="开源 6 个月内追平", check_after="2026-10-15")
    b = upsert_claim(db_conn, item_id="rss:x", source="rss_briefing:feed", claim="开源 6 个月内追平", check_after="2026-10-15")
    assert a == b
    assert db_conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1
    # 同一 item 不同断言 = 不同行
    c = upsert_claim(db_conn, item_id="rss:x", source="rss_briefing:feed", claim="另一条断言")
    assert c != a
    assert db_conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 2


def test_list_open_and_due_filter(db_conn):
    upsert_claim(db_conn, item_id="i1", source="s", claim="早到期", check_after="2026-01-01")
    upsert_claim(db_conn, item_id="i2", source="s", claim="晚到期", check_after="2099-01-01")
    upsert_claim(db_conn, item_id="i3", source="s", claim="未定")
    assert {r["claim"] for r in list_open_claims(db_conn)} == {"早到期", "晚到期", "未定"}
    assert {r["claim"] for r in list_open_claims(db_conn, due_before="2026-08-26")} == {"早到期", "未定"}


def test_resolve_claim_once(db_conn):
    cid = upsert_claim(db_conn, item_id="i1", source="s", claim="会追平")
    assert resolve_claim(db_conn, claim_id=cid, outcome="true", note="GLM/Kimi/DeepSeek 4 已追上") is True
    assert resolve_claim(db_conn, claim_id=cid, outcome="false") is False  # 已结算不可重结
    assert list_open_claims(db_conn) == []
    row = db_conn.execute("SELECT outcome, outcome_note, resolved_at FROM claims WHERE claim_id=?", (cid,)).fetchone()
    assert row["outcome"] == "true" and row["outcome_note"].startswith("GLM") and row["resolved_at"]


def test_resolve_rejects_unknown_outcome(db_conn):
    import pytest

    cid = upsert_claim(db_conn, item_id="i1", source="s", claim="x")
    with pytest.raises(ValueError):
        resolve_claim(db_conn, claim_id=cid, outcome="maybe")
