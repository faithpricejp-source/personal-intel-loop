"""Per-platform admission cap + compact push render + summarizer topic field.

Covers the 2026-06-05 digest changes: stop followed-YouTube channels (high
novelty, zero active/enrich) from flooding the longform tier, group the push
render by topic, and parse the new summarizer `topic` field.
"""
from __future__ import annotations

from datetime import date

from personal_intel_loop.digest import (
    DEFAULT_PER_PLATFORM_CAP,
    render_compact_digest,
    select_digest_candidates_v2,
)
from personal_intel_loop.ranking import TIER_LONGFORM
from personal_intel_loop.summarizer import _coerce_summary


class _FakeHit:
    def __init__(self, score=0.3, obj="mechanism"):
        self.score = score
        self.note_id = ""
        self.title = "x"
        self.source = "05 知识本体/foo.md"
        self.object_type = obj
        self.layer = "05 知识本体"


def _install_corpus_stubs(monkeypatch):
    import personal_intel_loop.vault_corpus as vc
    import personal_intel_loop.active_corpus as ac

    monkeypatch.setattr(vc, "assert_fresh", lambda min_count=400: 4225)
    monkeypatch.setattr(vc, "max_similarity", lambda *a, **kw: 0.3)
    monkeypatch.setattr(vc, "query_top_k", lambda *a, **kw: [_FakeHit()])
    monkeypatch.setattr(ac, "build_active_references", lambda *a, **kw: [])
    monkeypatch.setattr(ac, "max_similarity_to_active", lambda vec, refs: (0.0, None))


def _stub_embeddings(monkeypatch):
    import numpy as np
    import personal_intel_loop.embeddings as emb

    def _q(texts):
        return np.zeros((len(list(texts)), 4), dtype=np.float32)

    monkeypatch.setattr(emb, "embed_queries", _q)
    monkeypatch.setattr(emb, "embed_documents", _q)


def _seed(conn, source: str, item_id: str, body: str, ts: str):
    from personal_intel_loop.schemas import Item
    from personal_intel_loop.store import upsert_item

    item = Item(
        id=item_id,
        source=source,
        url=f"https://example.com/{item_id}",
        title=body[:40] or "empty",
        body=body,
        author="a",
        ts=ts,
        lang="zh",
        tags=[],
    )
    with conn:
        upsert_item(conn, item, adapter_name=source.split(":", 1)[0], source_payload_json="{}")


def test_youtube_platform_capped_in_longform(db_conn, monkeypatch):
    _install_corpus_stubs(monkeypatch)
    _stub_embeddings(monkeypatch)

    long_body = "长内容 " * 300  # >> TIER_LENGTH_THRESHOLD → longform
    # 10 followed-YouTube longform videos (the flooding case)
    for i in range(10):
        _seed(db_conn, f"youtube_followed:UC{i}", f"yt{i}", long_body, ts=f"2026-04-20T{i:02d}:00:00Z")
    # plus other-platform longform that should survive the cap
    for i in range(5):
        _seed(db_conn, "rss_briefing:feedX", f"rss{i}", long_body, ts=f"2026-04-20T{(i+10):02d}:00:00Z")

    result = select_digest_candidates_v2(
        db_conn,
        date_local=date(2026, 4, 20),
        top_k=100,
        now_utc="2026-04-20T23:00:00Z",
        candidate_pool=50,
        summarize=False,
    )
    yt = [c for c in result if c["source"].startswith("youtube_followed")]
    rss = [c for c in result if c["source"].startswith("rss_briefing")]
    assert len(yt) <= DEFAULT_PER_PLATFORM_CAP["youtube_followed"]
    # rss is uncapped → all 5 admitted, not crowded out by the 10 youtube items
    assert len(rss) == 5
    assert all(c["tier"] == TIER_LONGFORM for c in result)


def test_per_platform_cap_override(db_conn, monkeypatch):
    _install_corpus_stubs(monkeypatch)
    _stub_embeddings(monkeypatch)

    long_body = "长内容 " * 300
    for i in range(6):
        _seed(db_conn, f"youtube_followed:UC{i}", f"yt{i}", long_body, ts=f"2026-04-20T{i:02d}:00:00Z")

    result = select_digest_candidates_v2(
        db_conn,
        date_local=date(2026, 4, 20),
        top_k=100,
        now_utc="2026-04-20T23:00:00Z",
        candidate_pool=50,
        summarize=False,
        per_platform_cap={"youtube_followed": 2},
    )
    yt = [c for c in result if c["source"].startswith("youtube_followed")]
    assert len(yt) == 2


def test_platform_cap_blocks_before_llm_call(db_conn, monkeypatch):
    """Capped youtube items must be skipped BEFORE the (expensive) summarizer runs."""
    _install_corpus_stubs(monkeypatch)
    _stub_embeddings(monkeypatch)

    long_body = "长内容 " * 300
    for i in range(8):
        _seed(db_conn, f"youtube_followed:UC{i}", f"yt{i}", long_body, ts=f"2026-04-20T{i:02d}:00:00Z")

    summarized: list[str] = []

    import personal_intel_loop.summarizer as summ

    def _fake_summarize(*, candidate):
        summarized.append(candidate["item_id"])
        return summ.ItemSummary(
            one_liner="x", why_for_you="y", is_noise=False, topic="AI与模型"
        )

    monkeypatch.setattr(summ, "summarize_candidate", _fake_summarize)

    result = select_digest_candidates_v2(
        db_conn,
        date_local=date(2026, 4, 20),
        top_k=100,
        now_utc="2026-04-20T23:00:00Z",
        candidate_pool=50,
        summarize=True,
        per_platform_cap={"youtube_followed": 3},
    )
    yt = [c for c in result if c["source"].startswith("youtube_followed")]
    # cap admits 3; the other 5 must never reach the summarizer
    assert len(yt) == 3
    assert len(summarized) == 3


def test_render_compact_groups_by_topic_in_order():
    def _cand(item_id, source, title, one_liner, topic):
        return {
            "item_id": item_id,
            "source": source,
            "title": title,
            "llm_summary": {"one_liner": one_liner, "why_for_you": "", "topic": topic},
        }

    candidates = [
        _cand("1", "nikkei_cn:x", "日元跌向170", "日元干预失效。", "宏观与市场"),
        _cand("2", "follow_builders:blog:anthropic", "Claude containment", "爆炸半径控制。", "AI与模型"),
        _cand("3", "bbc_zh:simp", "南海造礁", "越南固桩。", "地缘政治"),
    ]
    out = render_compact_digest(date_local=date(2026, 6, 5), candidates=candidates)

    assert "你的日报 2026-06-05 · 3 条" in out
    # AI bucket renders before 宏观 before 地缘 (COMPACT_TOPIC_ORDER)
    assert out.index("【AI与模型】") < out.index("【宏观与市场】") < out.index("【地缘政治】")
    # source label mapping + one_liner present
    assert "[日经] 日元跌向170" in out
    assert "日元干预失效。" in out
    assert "[BBC中文] 南海造礁" in out


def test_render_compact_unknown_topic_falls_into_other():
    candidates = [
        {
            "item_id": "1",
            "source": "rss_briefing:feed",
            "title": "怪话题",
            "llm_summary": {"one_liner": "正文", "topic": "不存在的门类"},
        }
    ]
    out = render_compact_digest(date_local=date(2026, 6, 5), candidates=candidates)
    assert "【其他】" in out


def test_summarizer_coerce_topic_valid_and_invalid():
    valid = _coerce_summary(
        {"one_liner": "x", "why_for_you": "y", "is_noise": False, "topic": "AI与模型"}
    )
    assert valid.topic == "AI与模型"

    invalid = _coerce_summary(
        {"one_liner": "x", "why_for_you": "y", "is_noise": False, "topic": "瞎填的"}
    )
    assert invalid.topic == "其他"

    missing = _coerce_summary({"one_liner": "x", "why_for_you": "y", "is_noise": False})
    assert missing.topic == "其他"


def test_caixin_structural_pages_are_skipped(tmp_path):
    """财新的导播/回声/答疑/读周刊看视频不是文章, 但标题贴当期热点、打分不低。

    实测: 候选池加保底后这类结构页会挤进 top-15。
    """
    from personal_intel_loop.adapters.local_transcripts import LocalTranscriptsAdapter

    root = tmp_path / "caixin" / "cw1220"
    root.mkdir(parents=True)
    (root / "00_最新封面报道_AI基建得东南亚者得天下.md").write_text(
        "# 最新封面报道｜AI基建：得东南亚者得天下\n\n正文内容足够长以通过空文过滤。" * 3, "utf-8")
    (root / "02_最新周刊导播_AI基建争夺东南亚.md").write_text(
        "# 最新周刊导播｜AI基建争夺东南亚、聪明钱AI大调仓\n\n本期看点导航。" * 3, "utf-8")
    (root / "14_回声（《财新周刊》2026年第32期）.md").write_text(
        "# 回声（《财新周刊》2026年第32期）\n\n读者来信若干。" * 3, "utf-8")
    (root / "15_读周刊 看视频（《财新周刊》2026年第33期）.md").write_text(
        "# 读周刊 看视频（《财新周刊》2026年第33期）\n\n视频索引。" * 3, "utf-8")

    recs = list(LocalTranscriptsAdapter(roots={"caixin": tmp_path / "caixin"}).collect())
    titles = [r.item.title for r in recs]
    assert len(titles) == 1 and "封面报道" in titles[0]


def test_structural_filter_only_applies_to_caixin(tmp_path):
    from personal_intel_loop.adapters.local_transcripts import LocalTranscriptsAdapter

    root = tmp_path / "podcast" / "某节目"
    root.mkdir(parents=True)
    (root / "01_回声（某期）.md").write_text("# 回声（某期）\n\n播客转录正文内容。" * 3, "utf-8")
    recs = list(LocalTranscriptsAdapter(roots={"podcast": tmp_path / "podcast"}).collect())
    assert len(recs) == 1          # 非 caixin 根不受影响
