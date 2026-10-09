from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from personal_intel_loop.adapters.local_transcripts import LocalTranscriptsAdapter


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")


def test_collect_extracts_platform_series_and_local_path(tmp_path):
    root = tmp_path / "播客"
    target = root / "高能量" / "001_测试节目_transcript.md"
    _write(
        target,
        """---
title: "001_测试节目"
---

# 001_测试节目

这是第一段。

这是第二段，带一个[链接](https://example.com)。
""",
    )
    adapter = LocalTranscriptsAdapter(roots={"podcast": root})
    records = list(adapter.collect())
    assert len(records) == 1
    record = records[0]
    assert record.adapter_name == "local_transcripts"
    assert record.item.source == "local_transcripts:podcast"
    assert record.item.author == "高能量"
    assert "这是第一段" in record.item.body
    assert "链接" in record.item.body
    payload = record.source_payload_json
    assert "高能量" in payload
    assert str(target) in payload


def test_collect_respects_since_and_limit(tmp_path):
    root = tmp_path / "得到"
    older = root / "课程A" / "001_旧文稿.md"
    newer = root / "课程A" / "002_新文稿.md"
    _write(older, "# 旧文稿\n\n旧内容")
    _write(newer, "# 新文稿\n\n新内容")
    older_ts = datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()
    newer_ts = datetime(2026, 4, 20, tzinfo=timezone.utc).timestamp()
    older.touch()
    newer.touch()
    import os

    os.utime(older, (older_ts, older_ts))
    os.utime(newer, (newer_ts, newer_ts))

    adapter = LocalTranscriptsAdapter(roots={"dedao": root})
    records = list(
        adapter.collect(
            since=datetime(2026, 1, 1, tzinfo=timezone.utc),
            limit=1,
        )
    )
    assert len(records) == 1
    assert records[0].item.title == "新文稿"
