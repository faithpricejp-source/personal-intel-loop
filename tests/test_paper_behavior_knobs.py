"""旋钮生效: source/author/topic offset 各自改变名次(对照)、盲区版不受 offset 影响、
build_edition learn_first、CLI pil paper --no-learn 与 pil paper-learn --dry-run。"""
from __future__ import annotations

import json

import pytest

from personal_intel_loop.paper import build_edition
from personal_intel_loop.store import connect_db, ensure_schema, upsert_item
from tests._behavior_helpers import BEHAVIOR_DATE, NOW, TS_A, add_events, seed_item
from tests.conftest import make_item

TODAY = BEHAVIOR_DATE  # 出版日期
AI_JSON = json.dumps(
    {"lede": "导语。", "one_liner": "一句话。", "backstory": None, "so_what": None, "claim": None,
     "byline": None, "style_tags": ["数据密集"], "topic": "科学", "profile_hit": None, "lane": "material"},
    ensure_ascii=False,
)


@pytest.fixture
def conn(tmp_path):
    c = connect_db(tmp_path / "db.sqlite")
    ensure_schema(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture(autouse=True)
def _no_fulltext_sleep(monkeypatch):
    from personal_intel_loop import fulltext as fulltext_mod

    monkeypatch.setattr(fulltext_mod.time, "sleep", lambda seconds: None)


def _seed_two(conn, *, prefix="rss_briefing:src"):
    for index, source in enumerate((f"{prefix}_a", f"{prefix}_b")):
        upsert_item(
            conn,
            make_item(item_id=f"it:{index}", source=source, url=f"https://example.com/{index}", title=f"标题 {index}"),
            adapter_name="rss_briefing",
            source_payload_json=json.dumps({"feed_name": f"源{index}"}, ensure_ascii=False),
        )
    conn.commit()


def _select_fn(candidates):
    def select(conn, *, date_local, top_k, now_utc):
        return [dict(candidate) for candidate in candidates]

    return select


def _candidate(item_id, source, label):
    return {"item_id": item_id, "source": source, "source_payload": {"feed_name": label}, "title": f"标题 {item_id}", "body": "正文", "ts_utc": NOW}


def _section_of(conn, item_id, edition_date=TODAY):
    row = conn.execute("SELECT section FROM editions WHERE edition_date=? AND item_id=?", (edition_date, item_id)).fetchone()
    return row["section"] if row else None


def _set_offset(conn, kind, key, offset):
    conn.execute(
        "INSERT OR REPLACE INTO knob_offsets (kind, key, offset, updated_at) VALUES (?, ?, ?, ?)",
        (kind, key, offset, NOW),
    )
    conn.commit()


def _build(conn, candidates, *, n=2, blind_share=0.0, llm=None, **kwargs):
    return build_edition(
        conn,
        date_local=__import__("datetime").date.fromisoformat(TODAY),
        n=n,
        blind_share=blind_share,
        now_utc=NOW,
        select_fn=_select_fn(candidates),
        fetcher=lambda url: ("正文", "ok"),
        llm_call=llm or (lambda prompt: AI_JSON),
        learn_first=False,
        **kwargs,
    )


def test_source_offset_changes_rank(conn):
    _seed_two(conn)
    candidates = [_candidate("it:0", "rss_briefing:src_a", "源0"), _candidate("it:1", "rss_briefing:src_b", "源1")]
    _build(conn, candidates)
    assert _section_of(conn, "it:0") == "lead"  # 对照: 无 offset 时按 base 序

    conn.execute("DELETE FROM editions")
    _set_offset(conn, "source", "rss_briefing:src_b", 0.6)
    _build(conn, candidates)
    assert _section_of(conn, "it:1") == "lead"  # source_offset +0.6 反超


def test_author_offset_changes_rank(conn):
    # 4 条候选; item:00 署名 作者甲 且被 author_offset -0.9 压到第二
    candidates = [
        _candidate("it:0", "rss_briefing:src_a", "源0"),
        _candidate("it:1", "rss_briefing:src_a", "源0"),
        _candidate("it:2", "rss_briefing:src_a", "源0"),
        _candidate("it:3", "rss_briefing:src_a", "源0"),
    ]
    for index in range(4):
        upsert_item(
            conn,
            make_item(item_id=f"it:{index}", source="rss_briefing:src_a", url=f"https://example.com/{index}", title=f"标题 {index}"),
            adapter_name="rss_briefing",
            source_payload_json="{}",
        )
    for index, byline in enumerate(("作者甲", "作者乙", "作者丙", "作者丁")):
        conn.execute(
            "INSERT OR REPLACE INTO item_ai (item_id, payload_json, model, error, created_at) VALUES (?, ?, 'm', NULL, ?)",
            (f"it:{index}", json.dumps({"byline": byline, "topic": "科学"}, ensure_ascii=False), NOW),
        )
    conn.commit()
    _build(conn, candidates, n=4)
    assert _section_of(conn, "it:0") == "lead"  # 对照

    conn.execute("DELETE FROM editions")
    # 10-05 审计 C3：author_eff 夹在 [0.05,0.95]，-0.9 只生效到 -0.45 → 0.3*(-0.45)=-0.135 分，小于 4 条候选的分差 0.25
    _set_offset(conn, "author", "作者甲", -0.9)
    _build(conn, candidates, n=4)
    assert _section_of(conn, "it:0") == "lead"
    # 作者乙同时调到顶（+0.135），两者相对差 0.27 > 0.25，第二名反超
    conn.execute("DELETE FROM editions")
    _set_offset(conn, "author", "作者乙", 0.9)
    _build(conn, candidates, n=4)
    assert _section_of(conn, "it:0") != "lead"
    assert _section_of(conn, "it:1") == "lead"


def test_topic_offset_reranks_after_preprocess(conn):
    _seed_two(conn)
    # 预置 item_ai: it:0 topic 科学, it:1 topic 气候(preprocess 跳过已有记录)
    for item_id, topic in (("it:0", "科学"), ("it:1", "气候")):
        conn.execute(
            "INSERT OR REPLACE INTO item_ai (item_id, payload_json, model, error, created_at) VALUES (?, ?, 'm', NULL, ?)",
            (item_id, json.dumps({"byline": None, "topic": topic}, ensure_ascii=False), NOW),
        )
    conn.commit()
    candidates = [_candidate("it:0", "rss_briefing:src_a", "源0"), _candidate("it:1", "rss_briefing:src_b", "源1")]

    _build(conn, candidates)
    assert _section_of(conn, "it:0") == "lead"  # 对照: 无 topic offset

    conn.execute("DELETE FROM editions")
    # 单个话题旋钮有效偏移上限 0.45（夹在 [0.05,0.95]），两条候选分差 0.5，需两个话题反向各调到底
    _set_offset(conn, "topic", "气候", 0.6)
    _set_offset(conn, "topic", "科学", -0.6)
    _build(conn, candidates)
    assert _section_of(conn, "it:1") == "lead"  # 预处理完成后按 topic_offset 重排, it:1 反超

    conn.execute("DELETE FROM editions")
    _set_offset(conn, "topic", "科学", 0.0)
    _set_offset(conn, "topic", "气候", 5.0)  # 10-05 审计 C3：超出夹紧带的累计值不再无限放大
    _build(conn, candidates)
    assert _section_of(conn, "it:0") == "lead"


def test_blind_section_ignores_offsets(conn, tmp_path):
    from personal_intel_loop import paper as paper_mod

    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / "blindspots.md").write_text("- 盲区:与自己立场相反的一手叙述\n", "utf-8")

    def embed_fn(texts):
        # 盲区句与 B1 同向(强匹配), 与 B2 正交(弱匹配)
        return [[1.0, 0.0] if "B1" in text or "盲区" in text else [0.0, 1.0] for text in texts]

    for item_id, source, label in (
        ("m:1", "rss_briefing:src_m1", "源M1"),
        ("m:2", "rss_briefing:src_m1", "源M1"),
        ("b:B1", "rss_briefing:src_b1", "源B1"),
        ("b:B2", "rss_briefing:src_b2", "源B2"),
    ):
        upsert_item(
            conn,
            make_item(item_id=item_id, source=source, url=f"https://example.com/{item_id}", title=f"标题 {item_id}"),
            adapter_name="rss_briefing",
            source_payload_json=json.dumps({"feed_name": label}, ensure_ascii=False),
        )
    conn.commit()

    # 单元级: 盲区打分只看余弦, source_offset 再大也不改盲区排序
    _set_offset(conn, "source", "rss_briefing:src_b2", 2.0)
    picked = paper_mod._select_blind(
        conn,
        now_utc=NOW,
        main_ids={"m:1", "m:2"},
        blindspots=["盲区:与自己立场相反的一手叙述"],
        blind_n=2,
        embed_fn=embed_fn,
    )
    assert [candidate["item_id"] for candidate, _reason in picked] == ["b:B1", "b:B2"]

    # build 级: offset 全落在主线源/统一 topic 上(主线成员不变), 盲区行原样不动
    conn.execute("DELETE FROM knob_offsets")  # 清掉上面给 src_b2 的 offset, 避免它把 B2 拉进主线
    conn.commit()
    candidates = [
        _candidate("m:1", "rss_briefing:src_m1", "源M1"),
        _candidate("m:2", "rss_briefing:src_m1", "源M1"),
        _candidate("b:B1", "rss_briefing:src_b1", "源B1"),
        _candidate("b:B2", "rss_briefing:src_b2", "源B2"),
    ]

    def _blind_rows():
        return [
            (row["item_id"], row["rank"], row["blind_reason"])
            for row in conn.execute("SELECT item_id, rank, blind_reason FROM editions WHERE edition_date=? AND section='blind' ORDER BY rank", (TODAY,))
        ]

    _build(conn, candidates, n=4, blind_share=0.5, llm=None, embed_fn=embed_fn, profile_dir=str(profile_dir))
    blind_before = _blind_rows()
    assert [row[0] for row in blind_before] == ["b:B1", "b:B2"]

    conn.execute("DELETE FROM editions")
    conn.execute("INSERT OR REPLACE INTO knob_offsets (kind, key, offset, updated_at) VALUES ('source', 'rss_briefing:src_m1', 2.0, ?)", (NOW,))
    conn.execute("INSERT OR REPLACE INTO knob_offsets (kind, key, offset, updated_at) VALUES ('topic', '科学', 2.0, ?)", (NOW,))
    conn.commit()
    _build(conn, candidates, n=4, blind_share=0.5, llm=None, embed_fn=embed_fn, profile_dir=str(profile_dir))
    assert _blind_rows() == blind_before  # 盲区版不受任何 offset 影响


def test_build_edition_learn_first_runs_and_survives_failure(conn, tmp_path):
    _seed_two(conn)
    # 2026-10-04(=出版日的前一天)有行为: 深读 src_a 条目 3 次曝光
    seed_item(conn, "it:9", "rss_briefing:src_a", "源0")
    add_events(
        conn,
        [("impression", "it:9", 2000, {}), ("impression", "it:9", 1000, {}), ("impression", "it:9", 500, {}), ("open_item", "it:9", None, {}), ("item_dwell", "it:9", 20000, {})],
        ts=TS_A,
        edition_date=BEHAVIOR_DATE,
    )
    conn.commit()
    from datetime import date as _date

    build_date = _date(2026, 10, 5)  # 出版日: learn 看前一天 2026-10-04
    calls = []

    def llm(prompt):
        calls.append(prompt)
        if "复盘" in prompt:
            return json.dumps({"adjustments": [{"kind": "source", "key": "rss_briefing:src_a", "delta": 0.03, "reason": "深读"}], "proposals": [], "reading_note": ""}, ensure_ascii=False)
        return AI_JSON

    result = build_edition(
        conn, date_local=build_date, n=2, blind_share=0.0, now_utc=NOW,
        select_fn=_select_fn([_candidate("it:0", "rss_briefing:src_a", "源0"), _candidate("it:1", "rss_briefing:src_b", "源1")]),
        fetcher=lambda url: ("正文", "ok"), llm_call=llm,
    )
    assert result["picked"] == 2
    offset = conn.execute("SELECT offset FROM knob_offsets WHERE kind='source' AND key='rss_briefing:src_a'").fetchone()
    assert offset is not None and offset["offset"] == 0.03  # 出版前先 learn

    # learn 失败不影响出版
    conn.execute("DELETE FROM editions")
    conn.execute("DELETE FROM knob_offsets")
    conn.commit()

    def llm_boom(prompt):
        if "复盘" in prompt:
            raise RuntimeError("cloud down")
        return AI_JSON

    result2 = build_edition(
        conn, date_local=build_date, n=2, blind_share=0.0, now_utc=NOW,
        select_fn=_select_fn([_candidate("it:0", "rss_briefing:src_a", "源0"), _candidate("it:1", "rss_briefing:src_b", "源1")]),
        fetcher=lambda url: ("正文", "ok"), llm_call=llm_boom,
    )
    assert result2["picked"] == 2
    assert conn.execute("SELECT COUNT(*) FROM editions WHERE edition_date='2026-10-05'").fetchone()[0] == 2


def test_cli_paper_learn_dry_run_writes_nothing(monkeypatch, tmp_path, capsys):
    from personal_intel_loop import cli
    import personal_intel_loop.paper_learn as paper_learn_mod

    db = tmp_path / "cli.sqlite"
    c = connect_db(db)
    ensure_schema(c)
    seed_item(c, "it:9", "rss_briefing:src_a", "源A")
    add_events(c, [("impression", "it:9", 2000, {}), ("impression", "it:9", 1000, {}), ("impression", "it:9", 500, {})], ts=TS_A)
    c.commit()
    c.close()
    profile = tmp_path / "reading_profile.md"
    profile.write_text("# 阅读偏好\n\n正文\n\n## 待接受的修订\n-\n", "utf-8")

    raw = json.dumps({"adjustments": [{"kind": "source", "key": "rss_briefing:src_a", "delta": 0.03, "reason": "r"}], "proposals": [], "reading_note": "昨天你读了"}, ensure_ascii=False)
    monkeypatch.setattr(cli, "DB_PATH", db)
    monkeypatch.setattr(cli, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(paper_learn_mod, "_call_cloud", lambda prompt: (raw, "test-model"))

    rc = cli.main(["paper-learn", "--date", BEHAVIOR_DATE, "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "rss_briefing:src_a" in out and "昨天你读了" in out
    check = connect_db(db)
    try:
        assert check.execute("SELECT COUNT(*) FROM knob_offsets").fetchone()[0] == 0
        assert check.execute("SELECT COUNT(*) FROM ai_adjustments").fetchone()[0] == 0
        assert check.execute("SELECT COUNT(*) FROM edition_notes").fetchone()[0] == 0
    finally:
        check.close()
    assert "· behavior ·" not in profile.read_text("utf-8")  # 提案也没写


def test_cli_paper_no_learn_flag(monkeypatch, tmp_path):
    from personal_intel_loop import cli
    import personal_intel_loop.paper_learn as paper_learn_mod
    from personal_intel_loop import paper as paper_mod

    db = tmp_path / "cli2.sqlite"
    c = connect_db(db)
    ensure_schema(c)
    for index in range(4):
        upsert_item(
            c,
            make_item(item_id=f"it:{index}", source="rss_briefing:src_a", url=f"https://example.com/{index}", title=f"标题 {index}"),
            adapter_name="rss_briefing",
            source_payload_json="{}",
        )
    c.commit()
    c.close()
    monkeypatch.setattr(cli, "DB_PATH", db)
    monkeypatch.setattr(cli, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(paper_mod, "_default_select", lambda conn, **kwargs: [_candidate(f"it:{i}", "rss_briefing:src_a", "源A") for i in range(4)])
    monkeypatch.setattr("personal_intel_loop.fulltext.ensure_fulltext", lambda conn, ids, **kw: len(list(ids)))
    monkeypatch.setattr("personal_intel_loop.paper_ai.preprocess", lambda conn, ids, **kw: len(list(ids)))
    learn_calls = []
    monkeypatch.setattr(paper_learn_mod, "learn", lambda conn, **kwargs: learn_calls.append(kwargs) or {"applied": 0, "clipped": 0, "rejected": 0, "proposals": 0, "note": False})

    rc = cli.main(["paper", "--date", TODAY, "--n", "4", "--blind-share", "0", "--no-learn"])
    assert rc == 0
    assert learn_calls == []  # --no-learn 不调 learn

    rc = cli.main(["paper", "--date", TODAY, "--n", "4", "--blind-share", "0"])
    assert rc == 0
    assert len(learn_calls) == 1  # 缺省会先 learn
    assert learn_calls[0]["date_local"].isoformat() == "2026-10-03"  # 前一天
