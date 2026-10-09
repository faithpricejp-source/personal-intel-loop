from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np

from personal_intel_loop.active_corpus import ActiveReference
from personal_intel_loop.digest import DIGEST_BUCKET_TRANSCRIPT, render_digest, select_transcript_candidates_v2
from personal_intel_loop.schemas import Item
from personal_intel_loop.store import replace_digest_inclusions, set_item_embedding, upsert_item


def _seed_transcript(
    conn,
    *,
    item_id: str,
    platform: str,
    series_name: str,
    title: str,
    ts: str,
    local_path: str,
    body: str = "长文 " * 300,
) -> None:
    item = Item(
        id=item_id,
        source=f"local_transcripts:{platform}",
        url=f"https://local.intel-loop/transcripts/{platform}/{item_id}",
        title=title,
        body=body,
        author=series_name,
        ts=ts,
        lang="zh",
        summary=body[:400],
        tags=["local_transcript", platform, series_name],
    )
    payload = {
        "platform": platform,
        "series_name": series_name,
        "local_path": local_path,
        "content_char_len": len(body),
        "truncated": False,
        "media_urls": [],
    }
    with conn:
        upsert_item(
            conn,
            item,
            adapter_name="local_transcripts",
            source_payload_json=json.dumps(payload, ensure_ascii=False),
        )


def test_transcript_digest_applies_series_cap_and_cooldown(db_conn, monkeypatch, tmp_path):
    import personal_intel_loop.active_corpus as ac
    import personal_intel_loop.vault_corpus as vc

    _seed_transcript(
        db_conn,
        item_id="local:1",
        platform="dedao",
        series_name="AI前沿",
        title="旧但更相关",
        ts="2024-01-01T00:00:00Z",
        local_path=str(tmp_path / "a.md"),
    )
    _seed_transcript(
        db_conn,
        item_id="local:2",
        platform="dedao",
        series_name="AI前沿",
        title="同系列第二篇",
        ts="2026-04-18T00:00:00Z",
        local_path=str(tmp_path / "b.md"),
    )
    _seed_transcript(
        db_conn,
        item_id="local:3",
        platform="kanlixiang",
        series_name="商业社会",
        title="另一系列",
        ts="2026-04-19T00:00:00Z",
        local_path=str(tmp_path / "c.md"),
    )
    _seed_transcript(
        db_conn,
        item_id="local:4",
        platform="podcast",
        series_name="硅谷101",
        title="最近刚看过",
        ts="2026-04-19T12:00:00Z",
        local_path=str(tmp_path / "d.md"),
    )
    with db_conn:
        set_item_embedding(db_conn, "local:1", np.asarray([0.90, 0.0], dtype=np.float32))
        set_item_embedding(db_conn, "local:2", np.asarray([0.80, 0.0], dtype=np.float32))
        set_item_embedding(db_conn, "local:3", np.asarray([0.70, 0.0], dtype=np.float32))
        set_item_embedding(db_conn, "local:4", np.asarray([0.95, 0.0], dtype=np.float32))
        replace_digest_inclusions(
            db_conn,
            digest_kind="daily",
            digest_date="2026-04-19",
            digest_path="/tmp/intel_loop_digest_2026-04-19.md",
            item_rows=[("local:4", "local_transcripts:podcast")],
            included_at_utc="2026-04-19T13:00:00Z",
        )

    monkeypatch.setattr(vc, "assert_fresh", lambda min_count=400: 500)
    monkeypatch.setattr(vc, "max_similarity", lambda *args, **kwargs: 0.20 if not kwargs.get("object_types") else 0.10)
    monkeypatch.setattr(
        ac,
        "build_active_references",
        lambda *args, **kwargs: [
            ActiveReference(
                object_id="JDG_ai",
                source_file="/tmp/JDG_ai.md",
                object_type="judgment",
                text="AI",
                vector=np.asarray([1.0, 0.0], dtype=np.float32),
            )
        ],
    )

    result = select_transcript_candidates_v2(
        db_conn,
        top_k=3,
        now_utc="2026-04-20T00:00:00Z",
        shortlist_k=10,
        summarize=False,
        series_cap=1,
        platform_cap=10,
        cooldown_days=45,
    )
    ids = [item["item_id"] for item in result]
    assert "local:4" not in ids
    assert "local:1" in ids
    assert len([item for item in result if item["source_payload"]["series_name"] == "AI前沿"]) == 1
    assert all(item["digest_bucket"] == DIGEST_BUCKET_TRANSCRIPT for item in result)


def test_render_digest_shows_transcript_backlog_and_local_path(tmp_path):
    candidate = {
        "item_id": "local:1",
        "source": "local_transcripts:dedao",
        "url": "https://local.intel-loop/transcripts/dedao/local1",
        "title": "测试课程",
        "body": "长文 " * 200,
        "author": "AI前沿",
        "ts_utc": "2026-04-20T00:00:00Z",
        "summary": "摘要",
        "tags": ["local_transcript", "dedao", "AI前沿"],
        "source_payload": {
            "platform": "dedao",
            "series_name": "AI前沿",
            "local_path": str(tmp_path / "doc.md"),
            "content_char_len": 800,
        },
        "media_manifest_relpath": None,
        "item_status": "new",
        "trust_score": 0.35,
        "tier": "longform",
        "score_v2": 0.51,
        "novelty": 0.70,
        "active_relevance": 0.62,
        "enrich_score": 0.18,
        "why_for_you": None,
        "digest_bucket": DIGEST_BUCKET_TRANSCRIPT,
    }
    content = render_digest(
        date_local=date(2026, 4, 20),
        candidates=[candidate],
        ranking_version="v2_vault_aligned",
    )
    assert "Transcript Backlog" in content
    assert "本地文稿" in content
    assert "local_path" in content
