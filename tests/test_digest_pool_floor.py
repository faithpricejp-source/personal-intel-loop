"""候选池的 per-adapter 保底名额。

2026-08-26 实测缺陷: `ORDER BY ts DESC LIMIT 300` 硬截断——10 天窗内 1906 条候选只装最新 300 条,
第 300 条是 14 小时前, 于是日更快讯霸占全池, 财新(周刊, 208 篇)在**打分之前**就被砍光。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from personal_intel_loop.digest import _select_rows
from personal_intel_loop.store import upsert_item
from tests.conftest import make_item


def _seed(conn, source: str, n: int, *, hours_ago_start: int):
    base = datetime.now(timezone.utc)
    for i in range(n):
        ts = (base - timedelta(hours=hours_ago_start + i)).isoformat().replace("+00:00", "Z")
        upsert_item(conn, make_item(item_id=f"{source}-{i}", source=source, url=f"https://x/{source}/{i}", ts=ts),
                    adapter_name=source.split(":")[0], source_payload_json="{}")


def test_slow_cadence_adapter_survives_recency_truncation(db_conn):
    # 日更快讯: 60 条, 全是最近 60 小时
    _seed(db_conn, "fast_news:daily", 60, hours_ago_start=0)
    # 周刊: 10 条, 都比快讯旧
    _seed(db_conn, "weekly_mag:caixin", 10, hours_ago_start=100)

    # 旧行为(floor=0)= 纯 ts 降序截断: 周刊全被挤掉
    rows = _select_rows(db_conn, pool_limit=30, per_adapter_floor=0)
    assert {r["source"].split(":")[0] for r in rows} == {"fast_news"}

    # 新行为: 周刊拿到保底名额
    rows = _select_rows(db_conn, pool_limit=30, per_adapter_floor=5)
    kinds = [r["source"].split(":")[0] for r in rows]
    assert kinds.count("weekly_mag") == 5
    assert kinds.count("fast_news") == 25
    assert len(rows) == 30


def test_floor_larger_than_available_does_not_pad(db_conn):
    _seed(db_conn, "a:one", 3, hours_ago_start=0)
    _seed(db_conn, "b:two", 2, hours_ago_start=10)
    rows = _select_rows(db_conn, pool_limit=100, per_adapter_floor=25)
    assert len(rows) == 5  # 只有 5 条就返 5 条, 保底不是配额下限


def test_pool_limit_still_caps_total(db_conn):
    for name in ("a:x", "b:y", "c:z"):
        _seed(db_conn, name, 20, hours_ago_start=0)
    rows = _select_rows(db_conn, pool_limit=12, per_adapter_floor=10)
    assert len(rows) == 12
