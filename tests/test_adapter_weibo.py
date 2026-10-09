from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_intel_loop.adapters.weibo_timeline import WeiboTimelineAdapter


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", "utf-8")


def _make_fixture(tmp_path: Path) -> tuple[Path, Path]:
    timelines_dir = tmp_path / "weibo_timelines"
    timelines_dir.mkdir(parents=True)
    watchlist = timelines_dir / "watchlist.json"
    watchlist.write_text(
        json.dumps(
            {
                "accounts": [
                    {"uid": "111", "screen_name": "alpha"},
                    {"uid": "222", "screen_name": "beta"},
                ],
            },
            ensure_ascii=False,
        ),
        "utf-8",
    )
    _write_jsonl(
        timelines_dir / "111" / "analysis" / "scored_posts.jsonl",
        [
            {
                "uid": "111",
                "post_id": "p1",
                "screen_name": "alpha",
                "text_plain": "一条普通内容 " * 3,
                "authored_at": "2026-04-18T10:00:00+08:00",
                "status_url": "https://m.weibo.cn/status/p1",
                "pic_urls": ["https://example.com/a.jpg"],
                "is_long_text": False,
                "text_source": "raw_text",
                "priority_score": 5.0,
                "matched_useful_groups": ["ai_systems"],
            },
            {
                "uid": "111",
                "post_id": "p2-truncated",
                "text_plain": "这是一条截断的长微博预览…",
                "authored_at": "2026-04-17T10:00:00+08:00",
                "status_url": "https://m.weibo.cn/status/p2-truncated",
                "is_long_text": True,
                "text_source": "text_html",
            },
            {
                "uid": "111",
                "post_id": "p3-hydrated",
                "text_plain": "这是已经 hydrated 完整版的长文内容。",
                "authored_at": "2026-04-19T10:00:00+08:00",
                "status_url": "https://m.weibo.cn/status/p3-hydrated",
                "is_long_text": True,
                "text_source": "hydrated_long_text",
            },
        ],
    )
    _write_jsonl(
        timelines_dir / "222" / "analysis" / "scored_posts.jsonl",
        [
            {
                "uid": "222",
                "post_id": "q1",
                "screen_name": "beta",
                "text_plain": "beta 的第一条",
                "authored_at": "2026-04-19T09:00:00+08:00",
                "status_url": "https://m.weibo.cn/status/q1",
                "is_long_text": False,
                "text_source": "raw_text",
            },
        ],
    )
    return timelines_dir, watchlist


def test_weibo_adapter_skips_truncated_long_text(tmp_path: Path) -> None:
    timelines_dir, watchlist = _make_fixture(tmp_path)
    adapter = WeiboTimelineAdapter(watchlist_path=watchlist, timelines_dir=timelines_dir)
    records = list(adapter.collect())
    item_ids = {record.item.id for record in records}
    assert "weibo:111:p1" in item_ids
    assert "weibo:111:p3-hydrated" in item_ids
    assert "weibo:111:p2-truncated" not in item_ids
    assert adapter.last_skip_stat.truncated_long_text == 1


def test_weibo_adapter_requires_hydrated_flag_can_be_disabled(tmp_path: Path) -> None:
    timelines_dir, watchlist = _make_fixture(tmp_path)
    adapter = WeiboTimelineAdapter(
        watchlist_path=watchlist,
        timelines_dir=timelines_dir,
        require_hydrated=False,
    )
    records = list(adapter.collect())
    item_ids = {record.item.id for record in records}
    assert "weibo:111:p2-truncated" in item_ids


def test_weibo_adapter_honors_watchlist_and_sorts_desc(tmp_path: Path) -> None:
    timelines_dir, watchlist = _make_fixture(tmp_path)
    adapter = WeiboTimelineAdapter(watchlist_path=watchlist, timelines_dir=timelines_dir)
    records = list(adapter.collect())
    sources = {record.item.source for record in records}
    assert sources == {"weibo_timeline:111", "weibo_timeline:222"}
    timestamps = [record.item.ts for record in records]
    assert timestamps == sorted(timestamps, reverse=True)


def test_weibo_adapter_payload_preserves_scores_and_groups(tmp_path: Path) -> None:
    timelines_dir, watchlist = _make_fixture(tmp_path)
    adapter = WeiboTimelineAdapter(watchlist_path=watchlist, timelines_dir=timelines_dir)
    records = {record.item.id: record for record in adapter.collect()}
    payload = json.loads(records["weibo:111:p1"].source_payload_json)
    assert payload["priority_score"] == 5.0
    assert payload["matched_useful_groups"] == ["ai_systems"]
    assert payload["is_long_text"] is False
    assert records["weibo:111:p1"].item.tags[0] == "ai_systems"


def test_weibo_adapter_respects_since_filter(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    timelines_dir, watchlist = _make_fixture(tmp_path)
    adapter = WeiboTimelineAdapter(watchlist_path=watchlist, timelines_dir=timelines_dir)
    since = datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)
    records = list(adapter.collect(since=since))
    kept = {record.item.id for record in records}
    assert "weibo:111:p3-hydrated" in kept
    assert "weibo:111:p1" not in kept
