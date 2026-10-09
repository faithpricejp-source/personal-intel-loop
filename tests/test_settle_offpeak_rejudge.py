"""结算积压续作（）: 无到期日以出处发布日为锚 / 重判入口 / DeepSeek 错峰闸门。"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pytest

from personal_intel_loop.paper_settle import (
    deepseek_peak_until,
    default_search_fn,
    settle_backlog,
)
from personal_intel_loop.store import upsert_claim, upsert_item
from tests.conftest import make_item

BJ = timezone(timedelta(hours=8))
NOW = "2026-10-07T12:00:00Z"
TODAY = date(2026, 10, 7)
OK_JSON = '{"verdict":"likely_true","basis":"依据证据","links":[]}'


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start
        self.sleeps: list[float] = []
        self.events: list[str] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.events.append(f"sleep:{seconds:.0f}")
        self.now = self.now + timedelta(seconds=seconds)



@pytest.fixture(autouse=True)
def _enable_offpeak_gate(monkeypatch):
    """错峰闸门缺省关闭(PIL_OFFPEAK_GATE), 本文件测的就是开启后的行为。"""
    monkeypatch.setenv("PIL_OFFPEAK_GATE", "deepseek")


def _put(conn, *, item_id: str, title: str, ts: str, body: str = "正文") -> None:
    upsert_item(
        conn,
        make_item(item_id=item_id, title=title, body=body, ts=ts, url=f"https://example.com/{item_id}"),
        adapter_name="rss_briefing",
        source_payload_json="{}",
    )


def _claim(conn, *, item_id: str, claim: str, check_after, src_ts="2023-01-01T00:00:00Z") -> str:
    _put(conn, item_id=item_id, title=f"出处 {item_id}", ts=src_ts, body="出处正文")
    claim_id = upsert_claim(conn, item_id=item_id, source="rss_briefing:feed", claim=claim, check_after=check_after)
    conn.commit()
    return claim_id


# ---- 错峰闸门 ----

@pytest.mark.parametrize(
    "start,expected_until",
    [
        (datetime(2026, 10, 7, 10, 0, tzinfo=BJ), datetime(2026, 10, 7, 12, 0, tzinfo=BJ)),  # 周三上午高峰
        (datetime(2026, 10, 7, 12, 30, tzinfo=BJ), None),  # 周三午间空闲
        (datetime(2026, 10, 10, 10, 0, tzinfo=BJ), None),  # 周六
        (datetime(2026, 10, 7, 17, 59, tzinfo=BJ), datetime(2026, 10, 7, 18, 0, tzinfo=BJ)),
        (datetime(2026, 10, 5, 9, 0, tzinfo=BJ), datetime(2026, 10, 5, 12, 0, tzinfo=BJ)),  # 周一 9:00 整
        (datetime(2026, 10, 7, 18, 0, tzinfo=BJ), None),
        (datetime(2026, 10, 7, 1, 0, tzinfo=timezone.utc), datetime(2026, 10, 7, 12, 0, tzinfo=BJ)),  # UTC 输入 = 北京 9:00
    ],
)
def test_peak_until(start, expected_until):
    assert deepseek_peak_until(start) == expected_until


def _execute_one(db_conn, clock, caplog=None):
    _claim(db_conn, item_id="src:old", claim="陈年断言", check_after="2025-01-01")
    calls: list[str] = []

    def llm(prompt):
        clock.events.append("llm")
        calls.append(prompt)
        return OK_JSON

    out = settle_backlog(
        db_conn, date_local=TODAY, now_utc=NOW, limit=5, dry_run=False,
        llm_call=llm, clock=clock, sleep=clock.sleep,
    )
    return out, calls


def test_execute_waits_until_offpeak_wednesday_10(db_conn, caplog):
    clock = FakeClock(datetime(2026, 10, 7, 10, 0, tzinfo=BJ))
    with caplog.at_level(logging.INFO, logger="personal_intel_loop.deepseek_peak"):
        out, calls = _execute_one(db_conn, clock)
    assert clock.events == ["sleep:7200", "llm"]
    assert out["processed"] == 1 and len(calls) == 1
    assert any("12:00" in rec.getMessage() for rec in caplog.records), "日志写明等待到何时"


def test_execute_immediate_wednesday_1230(db_conn):
    clock = FakeClock(datetime(2026, 10, 7, 12, 30, tzinfo=BJ))
    _execute_one(db_conn, clock)
    assert clock.events == ["llm"]


def test_execute_immediate_saturday_10(db_conn):
    clock = FakeClock(datetime(2026, 10, 10, 10, 0, tzinfo=BJ))
    _execute_one(db_conn, clock)
    assert clock.events == ["llm"]


def test_gate_checked_before_every_call(db_conn):
    """每条调用前都判: 13:59 起第一条立即, 模型耗时 2 分钟进入 14:01 高峰, 第二条须等到 18:00。"""
    for index in range(2):
        _claim(db_conn, item_id=f"src:{index}", claim=f"陈年断言{index}", check_after=f"2025-0{index + 1}-01")
    clock = FakeClock(datetime(2026, 10, 7, 13, 59, tzinfo=BJ))

    def llm(prompt):
        clock.events.append("llm")
        clock.now += timedelta(minutes=2)
        return OK_JSON

    settle_backlog(db_conn, date_local=TODAY, now_utc=NOW, limit=5, dry_run=False, llm_call=llm, clock=clock, sleep=clock.sleep)
    assert clock.events[0] == "llm" and clock.events[-1] == "llm"
    assert clock.events[1].startswith("sleep:") and sum(clock.sleeps) == pytest.approx((18 * 60 - (14 * 60 + 1)) * 60)


def test_dry_run_never_waits(db_conn):
    clock = FakeClock(datetime(2026, 10, 7, 10, 0, tzinfo=BJ))
    _claim(db_conn, item_id="src:old", claim="陈年断言", check_after="2025-01-01")
    settle_backlog(db_conn, date_local=TODAY, now_utc=NOW, limit=5, clock=clock, sleep=clock.sleep)
    assert clock.sleeps == []


def test_execute_resumable_after_interrupt(db_conn):
    for index in range(3):
        _claim(db_conn, item_id=f"src:{index}", claim=f"陈年断言{index}", check_after=f"2025-0{index + 1}-01")
    clock = FakeClock(datetime(2026, 10, 10, 10, 0, tzinfo=BJ))
    n = {"calls": 0}

    def flaky(prompt):
        n["calls"] += 1
        if n["calls"] == 2:
            raise KeyboardInterrupt
        return OK_JSON

    with pytest.raises(KeyboardInterrupt):
        settle_backlog(db_conn, date_local=TODAY, now_utc=NOW, limit=5, dry_run=False, llm_call=flaky, clock=clock, sleep=clock.sleep)
    assert db_conn.execute("SELECT COUNT(*) FROM claim_prechecks").fetchone()[0] == 1
    again = settle_backlog(db_conn, date_local=TODAY, now_utc=NOW, limit=5, dry_run=False,
                           llm_call=lambda p: OK_JSON, clock=clock, sleep=clock.sleep)
    assert again["processed"] == 2 and again["backlog_total"] == 2


# ---- 重判入口 ----

def _seed_old_precheck(conn, claim_id: str, created_at: str) -> None:
    conn.execute(
        "INSERT INTO claim_prechecks (claim_id, verdict, basis, links_json, model, created_at) VALUES (?, 'unclear', '没查到足够证据', '[]', 'm', ?)",
        (claim_id, created_at),
    )
    conn.commit()


def test_rejudge_dry_run_lists_old_prechecks_without_model_or_writes(db_conn):
    old = _claim(db_conn, item_id="src:old", claim="旧窗口判过的断言", check_after="2024-09-10")
    new = _claim(db_conn, item_id="src:new", claim="新窗口判过的断言", check_after="2024-05-20")
    _claim(db_conn, item_id="src:none", claim="没判过的断言", check_after="2025-01-01")
    _seed_old_precheck(db_conn, old, "2026-10-05T01:23:56Z")
    _seed_old_precheck(db_conn, new, "2026-10-07T03:00:00Z")
    before = db_conn.total_changes

    def boom(prompt):
        raise AssertionError("dry-run 不得调模型")

    out = settle_backlog(db_conn, date_local=TODAY, now_utc=NOW, limit=10, rejudge_before="2026-10-07", llm_call=boom)
    assert out["dry_run"] is True and out["mode"] == "rejudge"
    assert [row["claim_id"] for row in out["rows"]] == [old]
    assert db_conn.total_changes == before


def test_rejudge_execute_overwrites_and_is_resumable(db_conn):
    old = _claim(db_conn, item_id="src:old", claim="旧窗口判过的断言", check_after="2024-09-10")
    _seed_old_precheck(db_conn, old, "2026-10-05T01:23:56Z")
    clock = FakeClock(datetime(2026, 10, 7, 10, 0, tzinfo=BJ))
    out = settle_backlog(db_conn, date_local=TODAY, now_utc=NOW, limit=10, dry_run=False, rejudge_before="2026-10-07",
                         llm_call=lambda p: OK_JSON, clock=clock, sleep=clock.sleep)
    assert out["processed"] == 1 and clock.sleeps == [7200], "重判同样过错峰闸门"
    row = db_conn.execute("SELECT verdict, created_at FROM claim_prechecks WHERE claim_id=?", (old,)).fetchone()
    assert row["verdict"] == "likely_true" and row["created_at"] >= "2026-10-07"
    again = settle_backlog(db_conn, date_local=TODAY, now_utc=NOW, limit=10, rejudge_before="2026-10-07")
    assert again["backlog_total"] == 0


# ---- 无到期日: 以出处发布日为锚 ----

def test_undated_claim_anchors_on_source_date(db_conn):
    claim = "铜价将突破每吨一万美元关口"
    _claim(db_conn, item_id="src:copper", claim=claim, check_after=None, src_ts="2025-03-01T00:00:00Z")
    _put(db_conn, item_id="ev:copper-2025", title="铜价突破每吨一万美元", ts="2025-04-10T00:00:00Z")
    _put(db_conn, item_id="ev:copper-recent", title="铜价每吨一万美元关口再度失守", ts="2026-10-05T00:00:00Z")
    hits = default_search_fn(db_conn, claim, now_utc=NOW, anchor_date=None, source_date="2025-03-01T00:00:00Z",
                             exclude_item_id="src:copper")
    assert [h["item_id"] for h in hits] == ["ev:copper-2025"]
    out = settle_backlog(db_conn, date_local=TODAY, now_utc=NOW, limit=5)
    assert out["rows"][0]["evidence_n"] == 1


def test_cli_rejudge_dry_run(db_conn, tmp_path, monkeypatch, capsys):
    from personal_intel_loop import cli, paper_settle

    old = _claim(db_conn, item_id="src:old", claim="旧窗口判过的断言", check_after="2024-09-10")
    _seed_old_precheck(db_conn, old, "2026-10-05T01:23:56Z")
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "intel_loop.sqlite")
    monkeypatch.setattr(paper_settle, "_call_cloud", lambda p: (_ for _ in ()).throw(AssertionError("不得调模型")))
    before = db_conn.total_changes
    assert cli.main(["settle-backlog", "--rejudge-before", "2026-10-07"]) == 0
    out = capsys.readouterr().out
    assert '"mode": "rejudge"' in out and '"backlog_total": 1' in out
    assert db_conn.total_changes == before
