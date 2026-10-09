"""结算栏以到期日为锚 + 近期到期才占出版名额 + 批量结算积压入口（）。"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from personal_intel_loop.paper_settle import (
    default_search_fn,
    run_prechecks,
    settle_backlog,
)
from personal_intel_loop.store import upsert_claim, upsert_item
from tests.conftest import make_item

NOW = "2026-10-07T12:00:00Z"
TODAY = date(2026, 10, 7)
CLAIM = "日银将在3月会议上加息25个基点"
OK_JSON = '{"verdict":"likely_true","basis":"依据证据","links":[]}'


def _put(conn, *, item_id: str, title: str, ts: str, body: str = "正文") -> None:
    upsert_item(
        conn,
        make_item(item_id=item_id, title=title, body=body, ts=ts, url=f"https://example.com/{item_id}"),
        adapter_name="rss_briefing",
        source_payload_json="{}",
    )


def _claim(conn, *, item_id: str, claim: str, check_after: str | None) -> str:
    _put(conn, item_id=item_id, title=f"出处 {item_id}", ts="2023-01-01T00:00:00Z", body="出处正文")
    claim_id = upsert_claim(conn, item_id=item_id, source="rss_briefing:feed", claim=claim, check_after=check_after)
    conn.commit()
    return claim_id


def _seed_evidence(conn) -> None:
    _put(conn, item_id="ev:2024", title="日银3月会议决定加息25个基点", ts="2024-04-02T00:00:00Z")
    _put(conn, item_id="ev:recent", title="日银会议再度加息25个基点", ts="2026-10-05T00:00:00Z")
    _put(conn, item_id="ev:2021", title="日银3月会议维持利率不加息", ts="2021-03-20T00:00:00Z")


# 真跑路径有 DeepSeek 错峰闸门: 测试钉在周六(空闲), 否则工作日高峰跑测试会真睡到空闲。
_SATURDAY = lambda: datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)  # noqa: E731


def _no_sleep(seconds: float) -> None:
    raise AssertionError(f"空闲时段不应等待 {seconds}")


def _boom(prompt: str):
    raise AssertionError("dry-run 不得调模型")


def test_search_anchored_on_due_date_returns_items_near_2024(db_conn):
    _seed_evidence(db_conn)
    ids = [row["item_id"] for row in default_search_fn(db_conn, CLAIM, now_utc=NOW, anchor_date="2024-03-31")]
    assert ids == ["ev:2024"]


def test_search_without_anchor_keeps_recent_window(db_conn):
    _seed_evidence(db_conn)
    ids = [row["item_id"] for row in default_search_fn(db_conn, CLAIM, now_utc=NOW)]
    assert ids == ["ev:recent"]


def test_precheck_prompt_uses_due_date_window(db_conn):
    _seed_evidence(db_conn)
    _claim(db_conn, item_id="src:old", claim=CLAIM, check_after="2024-03-31")
    prompts: list[str] = []
    out = settle_backlog(
        db_conn, date_local=TODAY, now_utc=NOW, limit=5, dry_run=False, clock=_SATURDAY, sleep=_no_sleep,
        llm_call=lambda p: prompts.append(p) or OK_JSON,
    )
    assert out["processed"] == 1 and len(prompts) == 1
    assert "日银3月会议决定加息25个基点" in prompts[0]
    assert "日银会议再度加息" not in prompts[0]
    row = db_conn.execute("SELECT verdict FROM claim_prechecks").fetchone()
    assert row["verdict"] == "likely_true"


def test_stale_and_undated_claims_do_not_take_publish_slots(db_conn):
    _claim(db_conn, item_id="src:2024", claim="陈年断言甲", check_after="2024-01-01")
    _claim(db_conn, item_id="src:spring", claim="陈年断言乙", check_after="2026-02-01")
    _claim(db_conn, item_id="src:none", claim="未定日期断言", check_after=None)
    _claim(db_conn, item_id="src:edge-old", claim="窗口外一天", check_after="2026-09-29")
    _claim(db_conn, item_id="src:week", claim="一周内断言", check_after="2026-09-30")
    _claim(db_conn, item_id="src:today", claim="今天到期断言", check_after="2026-10-07")
    _claim(db_conn, item_id="src:future", claim="未来断言", check_after="2026-10-08")
    out = run_prechecks(db_conn, date_local=TODAY, now_utc=NOW, llm_call=lambda p: OK_JSON, search_fn=lambda c: [])
    ids = [row["claim_row"]["item_id"] for row in out]
    assert ids == ["src:today", "src:week"]


def test_backlog_dry_run_no_model_no_writes(db_conn):
    _seed_evidence(db_conn)
    _claim(db_conn, item_id="src:old", claim=CLAIM, check_after="2024-03-31")
    _claim(db_conn, item_id="src:none", claim="未定日期断言", check_after=None)
    _claim(db_conn, item_id="src:today", claim="今天到期断言", check_after="2026-10-07")
    _claim(db_conn, item_id="src:future", claim="未来断言", check_after="2026-12-01")
    db_conn.commit()
    before = db_conn.total_changes
    out = settle_backlog(db_conn, date_local=TODAY, now_utc=NOW, limit=10, llm_call=_boom)
    assert out["dry_run"] is True
    assert db_conn.total_changes == before
    assert db_conn.execute("SELECT COUNT(*) FROM claim_prechecks").fetchone()[0] == 0
    rows = {row["item_id"]: row for row in out["rows"]}
    assert set(rows) == {"src:old", "src:none"}, "近期到期的归出版, 未来的未到期"
    assert rows["src:old"]["evidence_n"] == 1
    assert out["backlog_total"] == 2


def test_backlog_skips_valid_prechecks_and_honours_limit(db_conn):
    for index in range(4):
        _claim(db_conn, item_id=f"src:{index}", claim=f"陈年断言{index}", check_after=f"2025-0{index + 1}-01")
    first = settle_backlog(db_conn, date_local=TODAY, now_utc=NOW, limit=3, dry_run=False, llm_call=lambda p: OK_JSON,
                           clock=_SATURDAY, sleep=_no_sleep)
    assert first["processed"] == 3
    second = settle_backlog(db_conn, date_local=TODAY, now_utc=NOW, limit=3, llm_call=_boom)
    assert second["backlog_total"] == 1 and len(second["rows"]) == 1


def test_cli_settle_backlog_default_dry_run_no_model_no_writes(db_conn, tmp_path, monkeypatch, capsys):
    from personal_intel_loop import cli, paper_settle

    _seed_evidence(db_conn)
    _claim(db_conn, item_id="src:old", claim=CLAIM, check_after="2024-03-31")
    db_conn.commit()
    db_path = tmp_path / "intel_loop.sqlite"
    monkeypatch.setattr(cli, "DB_PATH", db_path)

    def boom(prompt):
        raise AssertionError("dry-run 不得调模型")

    monkeypatch.setattr(paper_settle, "_call_cloud", boom)
    before = db_conn.total_changes
    assert cli.main(["settle-backlog", "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert '"backlog_total": 1' in out and "证据 1 条" in out
    assert db_conn.execute("SELECT COUNT(*) FROM claim_prechecks").fetchone()[0] == 0
    assert db_conn.total_changes == before
    # --execute 与 --dry-run 同给时 dry-run 优先
    assert cli.main(["settle-backlog", "--execute", "--dry-run"]) == 0
    assert db_conn.execute("SELECT COUNT(*) FROM claim_prechecks").fetchone()[0] == 0


def test_due_before_source_extends_window_to_source_and_excludes_self(db_conn):
    """到期日早于出处发布日(回顾性断言或抽取把年份写早): 上界延到出处发布日之后, 出处自己不算证据。"""
    claim = "尼康将在10月23日正式上市Z5ⅡC新款相机"
    _put(db_conn, item_id="src:nikon", title="尼康推出Z5ⅡC新款相机 10月23日上市", ts="2026-09-29T00:00:00Z")
    upsert_claim(db_conn, item_id="src:nikon", source="rss_briefing:feed", claim=claim, check_after="2025-10-23")
    _put(db_conn, item_id="ev:nikon-launch", title="尼康Z5ⅡC新款相机今日上市", ts="2026-10-05T00:00:00Z")
    _put(db_conn, item_id="ev:nikon-2023", title="尼康新款相机Z5上市", ts="2023-06-01T00:00:00Z")
    db_conn.commit()
    out = settle_backlog(db_conn, date_local=TODAY, now_utc=NOW, limit=5)
    assert out["rows"][0]["evidence_n"] == 1
    hits = default_search_fn(
        db_conn, claim, now_utc=NOW, anchor_date="2025-10-23", source_date="2026-09-29T00:00:00Z",
        exclude_item_id="src:nikon",
    )
    assert [h["item_id"] for h in hits] == ["ev:nikon-launch"]
