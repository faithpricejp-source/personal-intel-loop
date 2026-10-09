"""10-05 F1 批次验收修复的回归测试(C1 / logging / G145 / G134 / G137 / G201 / G227)。

每条至少一个测试, 修复前失败、修复后通过; 全部调用真函数, 不把被测函数复制改写。
对应 TASK.md: PIL 修采集失败静默成功与 RSS/aihot/信源提案。
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from personal_intel_loop import cli
from personal_intel_loop import source_proposals as sp
from personal_intel_loop.adapters.aihot import AihotAdapter
from personal_intel_loop.adapters.follow_builders import FollowBuildersAdapter
from personal_intel_loop.adapters.rss_briefing import RSSBriefingAdapter
from personal_intel_loop.store import connect_db, ensure_schema
from tests.conftest import make_item


# ---------- 桩与工具 ----------

class _StubAdapter:
    """collect 可控、可选 last_errors 的桩 adapter(形态同 test_ingest_first_ring._FakeAdapter)。"""

    def __init__(self, records=(), last_errors=None):
        self.records = list(records)
        if last_errors is not None:
            self.last_errors = list(last_errors)

    def collect(self, *, since=None, limit=None):
        if limit is None:
            return list(self.records)
        return list(self.records)[:limit]


def _stub_record(item_id: str = "rss:stub-1"):
    return SimpleNamespace(
        item=make_item(item_id=item_id),
        adapter_name="rss_briefing",
        source_payload_json="{}",
    )


@pytest.fixture
def ingest_env(tmp_path, monkeypatch):
    """_ingest_adapter 直连测试: DB 与 RUNS_DIR(零连空轮 state/运行记录落点)都隔离到 tmp。"""
    monkeypatch.setattr(cli, "RUNS_DIR", tmp_path / "runs")
    conn = connect_db(tmp_path / "ingest.sqlite")
    ensure_schema(conn)
    try:
        yield conn
    finally:
        conn.close()


def _rss_feeds_file(tmp_path, feeds):
    f = tmp_path / "rss_feeds.json"
    f.write_text(json.dumps(feeds, ensure_ascii=False), "utf-8")
    return f


def _ok_response(xml: bytes) -> MagicMock:
    resp = MagicMock()
    resp.content = xml
    resp.raise_for_status = MagicMock()
    return resp


GOOD_FEED_XML = (
    b'<?xml version="1.0"?>\n<rss version="2.0"><channel><title>G</title>'
    b"<item><title>Good entry</title><link>https://good.example/1</link>"
    b"<guid>g1</guid><description>d</description></item>"
    b"</channel></rss>"
)


def _feed_xml(items: list[tuple[str, str, datetime]]) -> bytes:
    parts = ['<?xml version="1.0"?>\n<rss version="2.0"><channel><title>T</title>']
    for title, link, pub in items:
        parts.append(
            f"<item><title>{title}</title><link>{link}</link><guid>{link}</guid>"
            f"<pubDate>{pub.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
        )
    parts.append("</channel></rss>")
    return "".join(parts).encode("utf-8")


# ---------- C1: 采集失败不再静默记 ok ----------

def test_c1_zero_items_with_errors_marks_degraded(ingest_env, monkeypatch, caplog):
    monkeypatch.setattr(cli, "_load_adapter", lambda name: _StubAdapter(last_errors=["Test Feed: HTTPError/404"]))
    with caplog.at_level(logging.WARNING, logger="personal_intel_loop.cli"):
        summary = cli._ingest_adapter(ingest_env, adapter_name="rss_briefing", since=None, limit=None)
    assert summary["collected"] == 0
    assert summary["status"] == "degraded"
    assert summary["errors"] == ["Test Feed: HTTPError/404"]
    assert any("rss_briefing" in r.getMessage() and "404" in r.getMessage() for r in caplog.records)


def test_c1_partial_errors_keep_ok_but_carry_errors(ingest_env, monkeypatch):
    monkeypatch.setattr(
        cli, "_load_adapter",
        lambda name: _StubAdapter(records=[_stub_record()], last_errors=["坏源: ConnectionError/timeout"]),
    )
    summary = cli._ingest_adapter(ingest_env, adapter_name="rss_briefing", since=None, limit=None)
    assert summary["status"] == "ok"
    assert summary["collected"] == 1
    assert summary["errors"] == ["坏源: ConnectionError/timeout"]


def test_c1_clean_adapter_has_no_errors_key(ingest_env, monkeypatch):
    monkeypatch.setattr(cli, "_load_adapter", lambda name: _StubAdapter(records=[_stub_record()]))
    summary = cli._ingest_adapter(ingest_env, adapter_name="rss_briefing", since=None, limit=None)
    assert summary["status"] == "ok"
    assert "errors" not in summary


def test_c1_single_ingest_run_record_carries_status_and_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "intel_loop.sqlite")
    monkeypatch.setattr(cli, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(cli, "_load_adapter", lambda name: _StubAdapter(last_errors=["Test Feed: HTTPError/404"]))
    args = argparse.Namespace(adapter="rss_briefing", since=None, limit=None, pages=None, min_score=None)
    assert cli._cmd_ingest(args) == 0
    payload = json.loads(sorted((tmp_path / "runs").glob("ingest_*.jsonl"))[-1].read_text("utf-8"))
    assert payload["status"] == "degraded"
    assert payload["count"] == 0
    assert payload["errors"] == ["Test Feed: HTTPError/404"]


def test_c1_three_consecutive_zero_rounds_warn_after_nonzero(ingest_env, monkeypatch, caplog):
    monkeypatch.setattr(cli, "_load_adapter", lambda name: _StubAdapter(records=[_stub_record()]))
    cli._ingest_adapter(ingest_env, adapter_name="rss_briefing", since=None, limit=None)  # 首轮非零

    monkeypatch.setattr(cli, "_load_adapter", lambda name: _StubAdapter())  # 之后每轮 0 条
    with caplog.at_level(logging.WARNING, logger="personal_intel_loop.cli"):
        for _ in range(3):
            cli._ingest_adapter(ingest_env, adapter_name="rss_briefing", since=None, limit=None)
    streak_warnings = [r for r in caplog.records if "consecutive" in r.getMessage()]
    assert len(streak_warnings) == 1  # 第 3 轮才告警, 前两轮不告警
    state = json.loads(cli.ZERO_STREAK_PATH.read_text("utf-8"))
    assert state["rss_briefing"]["zero_streak"] == 3
    assert state["rss_briefing"]["ever_nonzero"] is True


def test_c1_zero_streak_requires_prior_nonzero(ingest_env, monkeypatch, caplog):
    monkeypatch.setattr(cli, "_load_adapter", lambda name: _StubAdapter())  # 从未非零
    with caplog.at_level(logging.WARNING, logger="personal_intel_loop.cli"):
        for _ in range(3):
            cli._ingest_adapter(ingest_env, adapter_name="rss_briefing", since=None, limit=None)
    assert not [r for r in caplog.records if "consecutive" in r.getMessage()]


# ---------- logging: CLI 入口 basicConfig ----------

def test_setup_logging_installs_info_handler_once():
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        for handler in saved_handlers:
            root.removeHandler(handler)
        cli._setup_logging()
        assert root.level == logging.INFO
        assert len(root.handlers) == 1
        cli._setup_logging()  # 已有 handler 不重复加
        assert len(root.handlers) == 1
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


# ---------- G145: aihot 非 200 进失败原因 ----------

def test_aihot_non_200_records_failure_reason():
    resp = requests.Response()
    resp.status_code = 404
    adapter = AihotAdapter(pages=1)
    with patch("personal_intel_loop.adapters.aihot.requests.get", return_value=resp):
        records = list(adapter.collect())
    assert records == []
    assert adapter.last_errors == ["aihot http 404"]


# ---------- G134: rss_briefing 逐源异常进失败清单 ----------

def test_rss_feed_fetch_failure_recorded_and_good_feed_still_collected(tmp_path, caplog):
    feeds_file = _rss_feeds_file(tmp_path, [
        {"name": "Good Feed", "url": "https://good.example/feed"},
        {"name": "坏源", "url": "https://bad.example/feed"},
    ])
    bad_response = requests.Response()
    bad_response.status_code = 403

    def fake_get(url, timeout=20, headers=None):
        if "bad.example" in url:
            return bad_response
        return _ok_response(GOOD_FEED_XML)

    adapter = RSSBriefingAdapter(feeds_file=feeds_file)
    with patch("personal_intel_loop.adapters.rss_briefing.requests.get", side_effect=fake_get):
        with caplog.at_level(logging.WARNING, logger="personal_intel_loop.adapters.rss_briefing"):
            records = list(adapter.collect())
    assert [r.item.source for r in records] == ["rss_briefing:good_feed"]  # 好源照常采集
    assert len(adapter.last_errors) == 1
    assert "坏源" in adapter.last_errors[0]
    assert "HTTPError" in adapter.last_errors[0]
    assert "403" in adapter.last_errors[0]
    assert any("坏源" in r.getMessage() for r in caplog.records)


# ---------- G137: 未来时间条目跳过并计数 ----------

def test_rss_future_dated_entry_skipped_and_counted(tmp_path):
    now = datetime.now(timezone.utc)
    feeds_file = _rss_feeds_file(tmp_path, [{"name": "BoC", "url": "https://bank.example/feed"}])
    xml = _feed_xml([
        ("soon agenda", "https://bank.example/1", now + timedelta(minutes=30)),  # ≤1h: 保留
        ("far agenda", "https://bank.example/2", now + timedelta(hours=2)),      # >1h: 跳过
    ])
    adapter = RSSBriefingAdapter(feeds_file=feeds_file)
    with patch("personal_intel_loop.adapters.rss_briefing.requests.get", return_value=_ok_response(xml)):
        records = list(adapter.collect())
    assert [r.item.url for r in records] == ["https://bank.example/1"]
    assert adapter.skipped_future == 1
    payload = json.loads(records[0].source_payload_json)
    assert payload["ts_was_future"] is False  # 保留条目不再被打上未来标记


# ---------- G201: follow_builders 取数异常进失败原因 ----------

class _FailingSession:
    def __init__(self):
        self.headers = {}

    def get(self, url, timeout=None):
        raise requests.ConnectionError("network down")


def test_follow_builders_fetch_error_recorded(tmp_path):
    blogs = tmp_path / "blogs.json"
    blogs.write_text(json.dumps({"blogs": [{"title": "Post", "url": "https://blog.example/1"}]}), "utf-8")
    podcasts = tmp_path / "podcasts.json"
    podcasts.write_text(json.dumps({"podcasts": []}), "utf-8")
    adapter = FollowBuildersAdapter(
        feed_sources={"x": "https://x.example/feed-x.json", "blogs": blogs, "podcasts": podcasts},
        session=_FailingSession(),
    )
    records = list(adapter.collect())
    assert [r.item.url for r in records] == ["https://blog.example/1"]  # 其余源照常
    assert adapter.last_errors and adapter.last_errors[0].startswith("x:")
    assert "ConnectionError" in adapter.last_errors[0]


# ---------- G227: rss_feeds.json 损坏时报错 + .bak ----------

def _source_line(name="New Mandala", url="https://newmandala.org/feed/", target="盲区-东南亚", kind="rss"):
    return sp.format_source_line(date_iso="2026-10-05", name=name, basis="测试依据",
                                 target=target, kind=kind, url=url)


def _sp_kw(tmp_path):
    return dict(rss_path=tmp_path / "rss_feeds.json", podcasts_path=tmp_path / "podcasts.json",
                html_todo_path=tmp_path / "todo.json", log_path=tmp_path / "log.jsonl")


def test_source_proposals_corrupt_rss_json_raises_and_keeps_original(tmp_path):
    kw = _sp_kw(tmp_path)
    original = ('[\n  {"name": "Old A", "url": "https://a.example/feed"},\n'
                '  {"name": "Old B", "url": "https://b.example/feed"},\n]')  # 尾逗号: 非法 JSON
    kw["rss_path"].write_text(original, "utf-8")
    with pytest.raises(json.JSONDecodeError):
        sp.apply_decision(_source_line(), "accept", **kw)
    assert kw["rss_path"].read_text("utf-8") == original  # 接受提案不再把原有条目覆盖丢
    assert not kw["log_path"].exists()  # 失败不落决定日志


def test_source_proposals_accept_writes_bak_and_keeps_entries(tmp_path):
    kw = _sp_kw(tmp_path)
    kw["rss_path"].write_text(json.dumps([{"name": "Old A", "url": "https://a.example/feed"}]), "utf-8")
    sp.apply_decision(_source_line(), "accept", **kw)
    feeds = json.loads(kw["rss_path"].read_text("utf-8"))
    assert [f["url"] for f in feeds] == ["https://a.example/feed", "https://newmandala.org/feed/"]
    bak = tmp_path / "rss_feeds.json.bak"
    assert bak.exists()
    assert json.loads(bak.read_text("utf-8")) == [{"name": "Old A", "url": "https://a.example/feed"}]


def test_source_proposals_non_list_json_raises(tmp_path):
    kw = _sp_kw(tmp_path)
    kw["rss_path"].write_text('{"name": "not-a-list"}', "utf-8")
    with pytest.raises(ValueError):
        sp.apply_decision(_source_line(), "accept", **kw)


def test_source_proposals_missing_file_still_treated_empty(tmp_path):
    kw = _sp_kw(tmp_path)
    sp.apply_decision(_source_line(), "accept", **kw)
    assert json.loads(kw["rss_path"].read_text("utf-8")) == [
        {"name": "New Mandala", "url": "https://newmandala.org/feed/", "category": "盲区-东南亚"}
    ]
