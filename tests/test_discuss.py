from __future__ import annotations

from pathlib import Path

import pytest

from personal_intel_loop import discuss as discuss_mod
from personal_intel_loop.discuss import (
    _safe_item_id,
    emit_discuss_packet,
)
from personal_intel_loop.schemas import Item
from personal_intel_loop.store import replace_digest_inclusions, set_item_embedding, upsert_item

_DRAFT_COLS = [
    "id", "claim", "anchor", "provenance", "source_stance", "probe", "host",
    "derivability", "disposition", "edges", "conflict", "triplet", "flip", "cost",
]


def test_safe_item_id_sanitizes_colons_and_slashes():
    assert _safe_item_id("weibo:1000000001:5000000000000001") == "weibo_1000000001_5000000000000001"
    assert _safe_item_id("rss:abc/def") == "rss_abc_def"


def _seed_item(conn, *, item_id: str = "weibo:111:p1") -> None:
    item = Item(
        id=item_id,
        source="weibo_timeline:111",
        url="https://m.weibo.cn/status/p1",
        title="alpha post",
        body="第一段正文,讲 DeepSeek V4 的算力栈。\n\n第二段,提到华为昇腾。",
        author="alpha",
        ts="2026-04-18T10:00:00+00:00",
        lang="zh",
        summary="摘要",
        tags=["ai_systems"],
    )
    with conn:
        upsert_item(
            conn,
            item,
            adapter_name="weibo_timeline",
            source_payload_json='{"priority_score": 5.0, "matched_useful_groups": ["ai_systems"]}',
        )


def test_emit_packet_happy_path_no_embedding(db_conn, tmp_path, monkeypatch):
    _seed_item(db_conn)
    # stub active_corpus to avoid loading sentence-transformers
    import personal_intel_loop.active_corpus as ac

    class _FakeRef:
        object_id = "JDG_sample"
        object_type = "judgment"
        source_file = "/tmp/x.md"
        text = "某条 active JDG 的摘要"
        vector = None

    monkeypatch.setattr(ac, "build_active_references", lambda *a, **kw: [_FakeRef()])

    staging = tmp_path / "staging"
    result = emit_discuss_packet(db_conn, item_id="weibo:111:p1", staging_dir=staging)
    assert result.created is True
    assert result.error is None
    text = result.path.read_text("utf-8")
    # key blocks present
    assert "讨论上下文:alpha post" in text
    assert "item_id: `weibo:111:p1`" in text
    assert "第一段正文,讲 DeepSeek V4 的算力栈" in text
    assert "JDG_sample" in text
    assert "priority_score" in text  # source_payload block
    assert "## 启动 prompt" in text
    # no embedding → fallback message
    assert "item 还未嵌入" in text


def test_emit_packet_queries_vault_when_embedding_cached(db_conn, tmp_path, monkeypatch):
    import numpy as np

    _seed_item(db_conn)
    with db_conn:
        set_item_embedding(db_conn, "weibo:111:p1", np.zeros(4, dtype=np.float32))

    # stub vault_corpus.query_top_k and active_corpus
    import personal_intel_loop.vault_corpus as vc
    import personal_intel_loop.active_corpus as ac

    class _FakeHit:
        def __init__(self, score, title, object_type, text):
            self.score = score
            self.title = title
            self.object_type = object_type
            self.text = text
            self.source = f"05 知识本体/{title}.md"
            self.layer = "05 知识本体"
            self.note_id = title
            self.domain = ""
            self.status = ""

    monkeypatch.setattr(
        vc, "query_top_k",
        lambda vec, **kw: [
            _FakeHit(0.74, "MEC_算力栈迁移机制", "mechanism", "华为昇腾的 HCCS 带宽"),
            _FakeHit(0.71, "JDG_2026-04-18_DeepSeek_V4", "judgment", "对 DeepSeek V4 的判断"),
        ],
    )
    monkeypatch.setattr(ac, "build_active_references", lambda *a, **kw: [])

    staging = tmp_path / "staging"
    result = emit_discuss_packet(db_conn, item_id="weibo:111:p1", staging_dir=staging)
    assert result.created is True
    text = result.path.read_text("utf-8")
    assert "MEC_算力栈迁移机制" in text
    assert "0.740" in text
    assert "Qdrant top-5" in text


def test_emit_packet_unknown_item(db_conn, tmp_path):
    result = emit_discuss_packet(db_conn, item_id="does:not:exist", staging_dir=tmp_path)
    assert result.created is False
    assert result.error == "item not found"


def test_emit_packet_gracefully_handles_vault_error(db_conn, tmp_path, monkeypatch):
    import numpy as np

    _seed_item(db_conn)
    with db_conn:
        set_item_embedding(db_conn, "weibo:111:p1", np.zeros(4, dtype=np.float32))

    import personal_intel_loop.vault_corpus as vc
    import personal_intel_loop.active_corpus as ac

    def _raise(*a, **kw):
        raise RuntimeError("chroma offline")

    monkeypatch.setattr(vc, "query_top_k", _raise)
    monkeypatch.setattr(ac, "build_active_references", lambda *a, **kw: [])

    result = emit_discuss_packet(db_conn, item_id="weibo:111:p1", staging_dir=tmp_path)
    assert result.created is True  # packet still written
    text = result.path.read_text("utf-8")
    assert "查询失败" in text


def test_feedback_scan_emits_packet_on_deep_discuss_check(db_conn, tmp_path, monkeypatch):
    """End-to-end: a checked deep_discuss box in a digest triggers packet emission via apply_feedback_scan."""
    from personal_intel_loop import feedback as fb

    _seed_item(db_conn)

    # stub heavy corpus paths
    import personal_intel_loop.active_corpus as ac

    monkeypatch.setattr(ac, "build_active_references", lambda *a, **kw: [])

    digest_path = tmp_path / "intel_loop_digest_2026-04-20.md"
    digest_path.write_text(
        """<!-- PIL_DIGEST date=2026-04-20 ranking_version=v2_vault_aligned -->
# Intel Loop Digest 2026-04-20

<!-- PIL_ITEM_START item_id=weibo:111:p1 source=weibo_timeline:111 -->
## [weibo:111:p1] alpha post

### Feedback
- [ ] useful <!-- pil_action=useful -->
- [x] 💬 deep_discuss <!-- pil_action=deep_discuss -->
<!-- PIL_PROCESSED event_ids= scanned_at_utc= -->
<!-- PIL_ITEM_END -->
""",
        "utf-8",
    )
    monkeypatch.setattr(fb, "STAGING_DIR", tmp_path)
    monkeypatch.setattr(fb, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr("personal_intel_loop.discuss.STAGING_DIR", tmp_path)

    summary = fb.apply_feedback_scan(db_conn, paths=[digest_path], dry_run=False)
    assert summary["inserted_events"] == 1
    assert len(summary["discuss_packets"]) == 1
    packet = summary["discuss_packets"][0]
    assert packet["created"] is True
    assert Path(packet["path"]).exists()
    assert "weibo_111_p1" in packet["path"]


class _Hit:
    def __init__(self, score, title, note_id=None):
        self.score = score
        self.title = title
        self.object_type = "mechanism"
        self.text = "preview"
        self.source = f"05 知识本体/{title}.md"
        self.layer = "05 知识本体"
        self.note_id = title if note_id is None else note_id
        self.domain = ""
        self.status = ""


def _draft_cells(text: str) -> dict:
    for line in text.splitlines():
        if not line.startswith("| pil:"):
            continue
        tmp = line.strip()[1:]
        if tmp.endswith("|"):
            tmp = tmp[:-1]
        parts = [c.replace("\x00", "|").strip() for c in tmp.replace("\\|", "\x00").split("|")]
        return dict(zip(_DRAFT_COLS, parts))
    raise AssertionError("draft row missing")


def _stub_active(monkeypatch) -> None:
    import personal_intel_loop.active_corpus as ac

    monkeypatch.setattr(ac, "build_active_references", lambda *a, **kw: [])


def _put_digest(conn, tmp_path, item_id: str, body: str, date: str = "2026-04-20"):
    path = tmp_path / f"intel_loop_digest_{date}.md"
    path.write_text(
        f"<!-- PIL_ITEM_START item_id={item_id} source=weibo_timeline:111 -->\n"
        f"## [{item_id}] alpha post\n\n{body}\n<!-- PIL_ITEM_END -->\n",
        "utf-8",
    )
    with conn:
        replace_digest_inclusions(
            conn,
            digest_kind="daily",
            digest_date=date,
            digest_path=str(path),
            item_rows=[(item_id, "weibo_timeline:111")],
            included_at_utc=f"{date}T00:00:00Z",
        )
    return path


def test_chunk_why_for_you_sets_host_and_disposition_threshold(db_conn, tmp_path, monkeypatch):
    _seed_item(db_conn)
    _stub_active(monkeypatch)
    item_id = "weibo:111:p1"
    _put_digest(
        db_conn,
        tmp_path,
        item_id,
        "- novelty: 0.40\n- active_relevance: 0.40\n- why_for_you: [active_layer] JDG_target @ 0.40\n",
    )
    result = emit_discuss_packet(db_conn, item_id=item_id, staging_dir=tmp_path / "staging")
    text = result.path.read_text("utf-8")
    cells = _draft_cells(text)
    assert cells["host"] == "JDG_target"
    assert cells["disposition"] == "instance-fire"
    assert text.index("## 原文正文") < text.index("## 候选表征块草稿") < text.index("## vault 最近邻")
    assert "3) 补齐上表 f2 / derivability / disposition / conflict / flip" in text
    _put_digest(
        db_conn,
        tmp_path,
        item_id,
        "- novelty: 0.50\n- active_relevance: 0.40\n- why_for_you: [active_layer] JDG_target @ 0.40\n",
    )
    flipped = _draft_cells(emit_discuss_packet(db_conn, item_id=item_id, staging_dir=tmp_path / "staging").path.read_text("utf-8"))
    assert flipped["host"] == "JDG_target"
    assert flipped["disposition"] == "undetermined"


def test_chunk_host_chroma_top1_when_no_digest(db_conn, tmp_path, monkeypatch):
    import numpy as np
    import personal_intel_loop.vault_corpus as vc

    _seed_item(db_conn)
    _stub_active(monkeypatch)
    with db_conn:
        set_item_embedding(db_conn, "weibo:111:p1", np.zeros(4, dtype=np.float32))
    monkeypatch.setattr(vc, "query_top_k", lambda vec, **kw: [_Hit(0.74, "MEC_chroma_top")])
    cells = _draft_cells(
        emit_discuss_packet(db_conn, item_id="weibo:111:p1", staging_dir=tmp_path).path.read_text("utf-8")
    )
    assert cells["host"] == "MEC_chroma_top"
    assert "UNMOUNTABLE: 0；已挂 MEC_chroma_top" in emit_discuss_packet(
        db_conn, item_id="weibo:111:p1", staging_dir=tmp_path
    ).path.read_text("utf-8")
    monkeypatch.setattr(vc, "query_top_k", lambda vec, **kw: [])
    empty = _draft_cells(
        emit_discuss_packet(db_conn, item_id="weibo:111:p1", staging_dir=tmp_path).path.read_text("utf-8")
    )
    assert empty["host"] == "UNMOUNTABLE"


def test_chunk_unmountable_without_embedding_or_digest(db_conn, tmp_path, monkeypatch):
    import numpy as np
    import personal_intel_loop.vault_corpus as vc

    _seed_item(db_conn)
    _stub_active(monkeypatch)
    text = emit_discuss_packet(db_conn, item_id="weibo:111:p1", staging_dir=tmp_path).path.read_text("utf-8")
    cells = _draft_cells(text)
    assert cells["host"] == "UNMOUNTABLE"
    assert "UNMOUNTABLE: 1；top-3: pil:weibo:111:p1:1" in text
    with db_conn:
        set_item_embedding(db_conn, "weibo:111:p1", np.zeros(4, dtype=np.float32))
    monkeypatch.setattr(vc, "query_top_k", lambda vec, **kw: [_Hit(0.61, "MEC_after_embed")])
    flipped = emit_discuss_packet(db_conn, item_id="weibo:111:p1", staging_dir=tmp_path).path.read_text("utf-8")
    assert _draft_cells(flipped)["host"] == "MEC_after_embed"
    assert "UNMOUNTABLE: 0；已挂 MEC_after_embed" in flipped


def test_chunk_missing_novelty_stays_undetermined(db_conn, tmp_path, monkeypatch):
    _seed_item(db_conn)
    _stub_active(monkeypatch)
    item_id = "weibo:111:p1"
    _put_digest(
        db_conn,
        tmp_path,
        item_id,
        "- active_relevance: 0.40\n- why_for_you: [active_layer] JDG_target @ 0.40\n",
    )
    cells = _draft_cells(
        emit_discuss_packet(db_conn, item_id=item_id, staging_dir=tmp_path / "staging").path.read_text("utf-8")
    )
    assert cells["host"] == "JDG_target"
    assert cells["disposition"] == "undetermined"
    _put_digest(
        db_conn,
        tmp_path,
        item_id,
        "- novelty: 0.40\n- active_relevance: 0.40\n- why_for_you: [active_layer] JDG_target @ 0.40\n",
    )
    flipped = _draft_cells(
        emit_discuss_packet(db_conn, item_id=item_id, staging_dir=tmp_path / "staging").path.read_text("utf-8")
    )
    assert flipped["disposition"] == "instance-fire"
