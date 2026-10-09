from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

from personal_intel_loop import cli
from personal_intel_loop.store import connect_db, ensure_schema, fetch_item, upsert_item
from tests.conftest import make_item


class _FakeAdapter:
    def __init__(self, records=None, error: Exception | None = None):
        self.records = list(records or [])
        self.error = error

    def collect(self, *, since=None, limit=None):
        if self.error is not None:
            raise self.error
        if limit is None:
            return self.records
        return self.records[:limit]


def _record(item_id: str, title: str = "Example title"):
    return SimpleNamespace(
        item=make_item(item_id=item_id, title=title),
        adapter_name="rss_briefing",
        source_payload_json="{}",
    )


def test_ingest_first_ring_records_new_and_updated_counts(tmp_path, monkeypatch):
    db_path = tmp_path / "intel_loop.sqlite"
    runs_dir = tmp_path / "runs"
    existing = make_item(item_id="rss:existing", title="Old title")
    with connect_db(db_path) as conn:
        ensure_schema(conn)
        upsert_item(conn, existing, adapter_name="rss_briefing", source_payload_json="{}")

    monkeypatch.setattr(cli, "DB_PATH", db_path)
    monkeypatch.setattr(cli, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(
        cli,
        "_load_adapter",
        lambda name: _FakeAdapter([_record("rss:existing", "New title"), _record("rss:new")]),
    )

    args = argparse.Namespace(
        adapters=["rss_briefing"],
        since=None,
        limit_per_adapter=None,
        strict=False,
    )

    assert cli._cmd_ingest_first_ring(args) == 0
    run_path = sorted(runs_dir.glob("ingest_first_ring_*.jsonl"))[-1]
    payload = json.loads(run_path.read_text("utf-8"))
    assert payload["total_collected"] == 2
    assert payload["total_new_items"] == 1
    assert payload["total_updated_items"] == 1
    assert payload["error_count"] == 0
    assert payload["future_item_count"] == 0
    assert payload["data_quality_status"] == "ok"

    with connect_db(db_path) as conn:
        ensure_schema(conn)
        assert fetch_item(conn, "rss:existing")["title"] == "New title"
        assert fetch_item(conn, "rss:new") is not None


def test_ingest_first_ring_records_adapter_error_without_strict_failure(tmp_path, monkeypatch):
    db_path = tmp_path / "intel_loop.sqlite"
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(cli, "DB_PATH", db_path)
    monkeypatch.setattr(cli, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(cli, "_load_adapter", lambda name: _FakeAdapter(error=RuntimeError("source offline")))

    args = argparse.Namespace(
        adapters=["rss_briefing"],
        since=None,
        limit_per_adapter=None,
        strict=False,
    )

    assert cli._cmd_ingest_first_ring(args) == 0
    run_path = sorted(runs_dir.glob("ingest_first_ring_*.jsonl"))[-1]
    payload = json.loads(run_path.read_text("utf-8"))
    assert payload["error_count"] == 1
    assert payload["adapters"][0]["status"] == "error"
    assert "source offline" in payload["adapters"][0]["error"]


def test_ingest_first_ring_strict_returns_failure_on_adapter_error(tmp_path, monkeypatch):
    db_path = tmp_path / "intel_loop.sqlite"
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(cli, "DB_PATH", db_path)
    monkeypatch.setattr(cli, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(cli, "_load_adapter", lambda name: _FakeAdapter(error=RuntimeError("source offline")))

    args = argparse.Namespace(
        adapters=["rss_briefing"],
        since=None,
        limit_per_adapter=None,
        strict=True,
    )

    assert cli._cmd_ingest_first_ring(args) == 1


def test_ingest_first_ring_fails_when_runtime_db_contains_future_items(tmp_path, monkeypatch):
    db_path = tmp_path / "intel_loop.sqlite"
    runs_dir = tmp_path / "runs"
    future = make_item(
        item_id="nikkei:future",
        source="nikkei_cn:politicsaeconomy",
        url="https://cn.nikkei.com/politicsaeconomy/politicsasociety/60860-2099-12-29-05-00-05.html",
        ts="2099-12-28T20:00:05Z",
    )
    with connect_db(db_path) as conn:
        ensure_schema(conn)
        upsert_item(conn, future, adapter_name="nikkei_cn", source_payload_json="{}")

    monkeypatch.setattr(cli, "DB_PATH", db_path)
    monkeypatch.setattr(cli, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(cli, "_load_adapter", lambda name: _FakeAdapter([]))

    args = argparse.Namespace(
        adapters=["nikkei_cn"],
        since=None,
        limit_per_adapter=None,
        strict=False,
    )

    assert cli._cmd_ingest_first_ring(args) == 1
    run_path = sorted(runs_dir.glob("ingest_first_ring_*.jsonl"))[-1]
    payload = json.loads(run_path.read_text("utf-8"))
    assert payload["error_count"] == 0
    assert payload["future_item_count"] == 1
    assert payload["data_quality_status"] == "error"
