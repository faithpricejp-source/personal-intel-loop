"""管道页「采集健康」后端测试。

临时 RUNS_DIR 全部由合成运行记录构造（字段形状照 cli 的真实运行记录），覆盖六种情况：
降级、部分失败、连续 0 条、旧记录无 status 键、超过 48 小时未跑、全部正常。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from personal_intel_loop.paper_api import get_pipeline

# 旧版 cli 写的单适配器运行记录：没有 status / errors 键
LEGACY_RECORD = {"adapter": "weibo_home", "count": 72, "new_items": 72, "updated_items": 0,
                 "max_item_ts": "2026-10-05T08:45:39Z", "since": None,
                 "run_at_utc": "2026-10-05T08:48:14.471498Z"}
# cli 的 first_ring 运行记录（嵌套 adapters）
FIRST_RING_RECORD = {"run_kind": "first_ring_ingest", "adapter_order": ["rss_briefing"], "adapter_count": 1,
                     "total_collected": 12, "total_new_items": 5, "total_updated_items": 7, "error_count": 0,
                     "future_item_count": 0, "data_quality_status": "ok", "since": None,
                     "limit_per_adapter": None, "strict": False, "run_at_utc": "2026-10-05T02:15:45.039838Z",
                     "adapters": [{"adapter": "rss_briefing", "status": "ok", "collected": 12, "new_items": 5,
                                   "updated_items": 7, "max_item_ts": "2026-10-05T02:15:45.040065Z",
                                   "sources": ["rss_briefing:example_feed_a", "rss_briefing:example_feed_b"]}]}
NOW = datetime(2026, 10, 5, 11, 0, tzinfo=timezone.utc)
HEALTH_KEYS = {"adapter", "status", "count", "errors", "run_at_utc", "zero_streak", "stale"}


def _utc(seconds_ago: int) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _write_run(runs_dir: Path, *, adapter: str, seconds_ago: int, status="ok", count=5, errors=None,
               extra_line: bool = False) -> Path:
    """按 cli._write_run_record 的命名与字段写一条单适配器运行记录。"""
    run_at_utc = _utc(seconds_ago)
    record = {
        "adapter": adapter,
        "status": status,
        "count": count,
        "new_items": count,
        "updated_items": 0,
        "max_item_ts": run_at_utc,
        "errors": errors if errors is not None else [],
        "since": None,
        "run_at_utc": run_at_utc,
    }
    stamp = run_at_utc.replace("-", "").replace(":", "")
    path = runs_dir / f"ingest_{stamp}.jsonl"
    text = json.dumps(record, ensure_ascii=False) + "\n"
    if extra_line:  # 同一文件多条记录（cli 追加写过的形态）
        older = dict(record, run_at_utc=_utc(seconds_ago + 7200))
        text = json.dumps(older, ensure_ascii=False) + "\n" + text
    path.write_text(text, "utf-8")
    return path


def _write_zero_streaks(runs_dir: Path, mapping: dict) -> None:
    payload = {name: {"zero_streak": streak, "ever_nonzero": True, "updated_at_utc": _utc(60)}
               for name, streak in mapping.items()}
    (runs_dir / "adapter_zero_streaks.json").write_text(json.dumps(payload, ensure_ascii=False) + "\n", "utf-8")


def _health(conn, runs_dir, **kwargs):
    result = get_pipeline(conn, profile_dir="/tmp/any", runs_dir=runs_dir, now=NOW, **kwargs)
    return result["health"]


def _by_adapter(health):
    return {row["adapter"]: row for row in health}


@pytest.fixture
def runs(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    return runs_dir


def test_health_field_always_present(db_conn, runs):
    """10-05 验收 C1：Pipeline 返回值新增 health 键（正常时为空列表）。"""
    body = get_pipeline(db_conn, profile_dir="/tmp/any", runs_dir=runs, now=NOW)
    assert set(body.keys()) == {"sources", "authors", "recent", "profile_dir", "health"}


def test_health_lists_degraded(db_conn, runs):
    """降级：status=degraded 的采集器要列出，带失败原因。"""
    _write_run(runs, adapter="aihot", seconds_ago=600, status="degraded", count=0,
               errors=["aihot non-200: 404"])
    health = _by_adapter(_health(db_conn, runs))
    assert set(health) == {"aihot"}
    row = health["aihot"]
    assert row["status"] == "degraded"
    assert row["count"] == 0
    assert row["errors"] == ["aihot non-200: 404"]
    assert row["zero_streak"] == 0
    assert row["stale"] is False
    assert set(row.keys()) == HEALTH_KEYS


def test_health_lists_partial_errors_with_ok_status(db_conn, runs):
    """部分失败：status 仍是 ok 但 errors 非空 → 要列出。"""
    _write_run(runs, adapter="rss_briefing", seconds_ago=900, count=140,
               errors=["feed failed: Reuters Top News (403)", "feed failed: BoE News (timeout)"])
    row = _by_adapter(_health(db_conn, runs))["rss_briefing"]
    assert row["status"] == "ok"
    assert len(row["errors"]) == 2


def test_health_lists_zero_streak_ge_3(db_conn, runs):
    """连续 0 条：zero_streak >= 3 即使 status=ok、无 errors 也要列出。"""
    _write_run(runs, adapter="weibo_home", seconds_ago=300, count=0)
    _write_zero_streaks(runs, {"weibo_home": 3, "mofa_anzen": 2})
    health = _by_adapter(_health(db_conn, runs))
    assert set(health) == {"weibo_home"}
    assert health["weibo_home"]["zero_streak"] == 3


def test_health_ignores_zero_streak_below_3(db_conn, runs):
    _write_run(runs, adapter="disaster_alerts", seconds_ago=300, count=0)
    _write_zero_streaks(runs, {"disaster_alerts": 2})
    assert _health(db_conn, runs) == []


def test_health_old_record_without_status_key(db_conn, runs):
    """旧记录没有 status/errors 键：视为 ok，但仍是该采集器的最近一次运行。"""
    stamp = _utc(1200).replace("-", "").replace(":", "")
    legacy = runs / f"ingest_{stamp}.jsonl"
    legacy.write_text(json.dumps(LEGACY_RECORD, ensure_ascii=False) + "\n", "utf-8")  # 旧记录：无 status 键
    assert "status" not in legacy.read_text("utf-8")
    _write_zero_streaks(runs, {"weibo_home": 4})
    health = _by_adapter(_health(db_conn, runs))
    assert set(health) == {"weibo_home"}
    assert health["weibo_home"]["status"] == "ok"
    assert health["weibo_home"]["errors"] == []
    assert health["weibo_home"]["zero_streak"] == 4


def test_health_lists_stale_over_48h(db_conn, runs):
    """超过 48 小时没跑：stale=true。"""
    _write_run(runs, adapter="who_don", seconds_ago=49 * 3600)
    row = _by_adapter(_health(db_conn, runs))["who_don"]
    assert row["stale"] is True
    assert row["status"] == "ok"


def test_health_fresh_at_47h_not_stale(db_conn, runs):
    _write_run(runs, adapter="who_don", seconds_ago=47 * 3600, status="degraded")
    assert _by_adapter(_health(db_conn, runs))["who_don"]["stale"] is False


def test_health_empty_when_all_normal(db_conn, runs):
    """全部正常：health 为空列表。"""
    _write_run(runs, adapter="weibo_home", seconds_ago=300, count=68)
    _write_run(runs, adapter="rss_briefing", seconds_ago=400, count=168)
    _write_zero_streaks(runs, {"weibo_home": 0, "rss_briefing": 1})
    assert _health(db_conn, runs) == []


def test_health_uses_latest_run_per_adapter(db_conn, runs):
    """同一采集器有多条记录时按 run_at_utc 取最新一条。"""
    _write_run(runs, adapter="aihot", seconds_ago=3600, status="degraded", count=0,
               errors=["aihot non-200: 404"], extra_line=True)
    _write_run(runs, adapter="aihot", seconds_ago=600, status="ok", count=120)
    assert _health(db_conn, runs) == []


def test_health_reads_first_ring_run_records(db_conn, runs):
    """cli 的 first_ring 运行记录（嵌套 adapters）同样是各采集器的运行记录。"""
    src = json.loads(json.dumps(FIRST_RING_RECORD))
    src["run_at_utc"] = _utc(1800)
    src["adapters"] = [
        {"adapter": "aihot", "status": "degraded", "collected": 0, "new_items": 0, "updated_items": 0,
         "error": "RuntimeError: profile dir missing"},
        {"adapter": "rss_briefing", "status": "ok", "collected": 168, "new_items": 51, "updated_items": 117},
    ]
    stamp = src["run_at_utc"].replace("-", "").replace(":", "")
    (runs / f"ingest_first_ring_{stamp}.jsonl").write_text(json.dumps(src, ensure_ascii=False) + "\n", "utf-8")
    row = _by_adapter(_health(db_conn, runs))["aihot"]
    assert row["status"] == "degraded"
    assert row["count"] == 0
    assert row["errors"] == ["RuntimeError: profile dir missing"]


def test_health_missing_runs_dir_is_empty(db_conn, tmp_path):
    assert _health(db_conn, tmp_path / "no_such_runs") == []


def test_health_broken_json_line_is_skipped(db_conn, runs):
    (runs / "ingest_20261005T100000.000000Z.jsonl").write_text("{not json\n" + "\n", "utf-8")
    _write_run(runs, adapter="aihot", seconds_ago=600, status="degraded", count=0)
    assert set(_by_adapter(_health(db_conn, runs))) == {"aihot"}


def test_health_scans_only_latest_500_files(db_conn, runs):
    """只扫最近 500 个文件（按文件名时间排序）：更早的降级记录不再列出。"""
    for i in range(501):  # i 越大越早；最早那条（src_500）是 degraded
        _write_run(runs, adapter=f"src_{i:03d}", seconds_ago=10_000 + i * 60,
                   status="degraded" if i == 500 else "ok", count=0)
    assert len({f.name for f in runs.glob("ingest_*.jsonl")}) == 501
    assert _health(db_conn, runs) == []
    _write_run(runs, adapter="bbc_zh", seconds_ago=60, status="degraded", count=0)
    assert set(_by_adapter(_health(db_conn, runs))) == {"bbc_zh"}


def test_health_500_file_window_is_ordered_by_time(db_conn, runs):
    """500 个文件的窗口按文件名里的时间排，不按文件名字典序：
    first_ring 文件名多一截前缀，字典序会永远排在 ingest_<时间戳> 之后，把最新的单适配器记录挤出去。"""
    _write_run(runs, adapter="aihot", seconds_ago=60, status="degraded", count=0,
               errors=["aihot non-200: 404"])
    for i in range(500):  # 500 个更早的 first_ring 记录，每轮各适配器都正常
        run_at = _utc(2000 + i * 10)
        payload = {"run_kind": "first_ring_ingest", "run_at_utc": run_at,
                   "adapters": [{"adapter": f"ring_src_{i:03d}", "status": "ok", "collected": 3, "errors": []}]}
        stamp = run_at.replace("-", "").replace(":", "")
        (runs / f"ingest_first_ring_{stamp}.jsonl").write_text(json.dumps(payload, ensure_ascii=False) + "\n", "utf-8")
    assert len(list(runs.glob("ingest_*.jsonl"))) == 501
    assert set(_by_adapter(_health(db_conn, runs))) == {"aihot"}


def test_health_500_file_window_is_ordered_by_time(db_conn, runs):
    """500 个文件的窗口按文件名里的时间排，不按文件名字典序：
    first_ring 文件名多一截前缀，字典序会永远排在 ingest_<时间戳> 之后，把最新的单适配器记录挤出去。"""
    _write_run(runs, adapter="aihot", seconds_ago=60, status="degraded", count=0,
               errors=["aihot non-200: 404"])
    for i in range(500):  # 500 个更早的 first_ring 记录，每轮各适配器都正常
        run_at = _utc(2000 + i * 10)
        payload = {"run_kind": "first_ring_ingest", "run_at_utc": run_at,
                   "adapters": [{"adapter": f"ring_src_{i:03d}", "status": "ok", "collected": 3, "errors": []}]}
        stamp = run_at.replace("-", "").replace(":", "")
        (runs / f"ingest_first_ring_{stamp}.jsonl").write_text(json.dumps(payload, ensure_ascii=False) + "\n", "utf-8")
    assert len(list(runs.glob("ingest_*.jsonl"))) == 501
    assert set(_by_adapter(_health(db_conn, runs))) == {"aihot"}


def test_health_mixed_record_shapes_all_normal(db_conn, tmp_path):
    """单适配器新旧两种记录 + first_ring 记录 + streak 文件混在一起、全部正常 → 空列表。"""
    runs_dir = tmp_path / "runs_mixed"
    runs_dir.mkdir()
    _write_run(runs_dir, adapter="weibo_home", seconds_ago=600, count=68)
    _write_run(runs_dir, adapter="aihot", seconds_ago=3600, count=40, extra_line=True)
    legacy = dict(LEGACY_RECORD, adapter="zhihu_moments", run_at_utc=_utc(7200))
    (runs_dir / f"ingest_{_utc(7200).replace('-', '').replace(':', '')}.jsonl").write_text(
        json.dumps(legacy, ensure_ascii=False) + "\n", "utf-8")
    ring = dict(FIRST_RING_RECORD, run_at_utc=_utc(1800))
    (runs_dir / f"ingest_first_ring_{_utc(1800).replace('-', '').replace(':', '')}.jsonl").write_text(
        json.dumps(ring, ensure_ascii=False) + "\n", "utf-8")
    _write_zero_streaks(runs_dir, {"weibo_home": 0, "aihot": 0, "rss_briefing": 1})
    result = get_pipeline(db_conn, profile_dir="/tmp/any", runs_dir=runs_dir, now=NOW)
    assert result["health"] == []


def test_health_ignores_retired_and_zero_normal_adapters(tmp_path):
    # 回归：灾害预警「连续 0 条」（常态）与已退役的采集器曾被误报
    import json
    from datetime import datetime, timezone
    from personal_intel_loop.paper_api import collect_pipeline_health

    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    (tmp_path / "ingest_20260706T233005.881736Z.jsonl").write_text(json.dumps(
        {"adapter": "xiaohongshu", "status": "ok", "count": 5, "run_at_utc": "2026-07-06T23:30:05Z"}) + "\n", "utf-8")
    (tmp_path / "ingest_20260826T142839.706152Z.jsonl").write_text(json.dumps(
        {"adapter": "disaster_alerts", "status": "ok", "count": 3, "run_at_utc": "2026-08-26T14:28:39Z"}) + "\n", "utf-8")
    (tmp_path / "adapter_zero_streaks.json").write_text(json.dumps(
        {"disaster_alerts": {"zero_streak": 5, "ever_nonzero": True, "updated_at_utc": "2026-10-05T11:00:00Z"}}), "utf-8")
    assert collect_pipeline_health(tmp_path, now=now) == []
