"""build_edition / plan_edition: 屏蔽、去重、分区、lead 带图、盲区上限、同日重跑、dry-run。"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from personal_intel_loop.paper import build_edition, plan_edition
from personal_intel_loop.store import connect_db, ensure_schema, upsert_item
from tests.conftest import make_item

TODAY = date(2026, 10, 4)
NOW = "2026-10-04T02:00:00Z"
AI_JSON = json.dumps(
    {
        "lede": "导语。",
        "one_liner": "一句话。",
        "backstory": None,
        "so_what": None,
        "claim": None,
        "byline": None,
        "style_tags": ["数据密集"],
        "topic": "科学",
        "profile_hit": None,
        "lane": "material",
    },
    ensure_ascii=False,
)


def _source_of(index: int) -> str:
    if 17 <= index <= 23:
        return "rss_briefing:feed_a"
    if index >= 24:
        return "rss_briefing:feed_b"
    return f"rss_briefing:feed_{index % 3}"


def _candidate(index: int, *, image: bool = False) -> dict:
    payload = {"feed_name": f"源 {index % 3}"}
    if image:
        payload["media_urls"] = [f"https://img.example.com/{index}.jpg"]
    return {
        "item_id": f"item:{index:02d}",
        "source": _source_of(index),
        "source_payload": payload,
        "title": f"标题 {index}",
        "body": "正文",
        "ts_utc": NOW,
    }


def _seed(conn, count: int = 30):
    for index in range(count):
        payload = {"feed_name": f"源 {index % 3}"}
        upsert_item(
            conn,
            make_item(
                item_id=f"item:{index:02d}",
                source=_source_of(index),
                url=f"https://example.com/{index}",
                title=f"标题 {index}",
            ),
            adapter_name="rss_briefing",
            source_payload_json=json.dumps(payload, ensure_ascii=False),
        )
    conn.commit()


def _select_fn(candidates):
    def select(conn, *, date_local, top_k, now_utc):
        return [dict(candidate) for candidate in candidates]

    return select


def _embed_flat(texts):
    """全部同向单位向量: 相似度并列 1.0, 顺序由稳定排序保持, reason = 第一条盲区句。"""
    return [[0.0, 0.0, 1.0] for _ in texts]


def _fetch_ok(url):
    return "这是抓回来的正文", "ok"


def _llm_ok(prompt):
    return AI_JSON


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
    """build_edition 走 ensure_fulltext 默认 sleep_s=1.0; 测试里关掉等待。"""
    from personal_intel_loop import fulltext as fulltext_mod

    monkeypatch.setattr(fulltext_mod.time, "sleep", lambda seconds: None)


def _picked(conn, edition_date=TODAY.isoformat()):
    return {row[0] for row in conn.execute("SELECT item_id FROM editions WHERE edition_date=?", (edition_date,))}


def _section_counts(conn, edition_date=TODAY.isoformat()):
    rows = conn.execute(
        "SELECT section, COUNT(*) FROM editions WHERE edition_date=? GROUP BY section",
        (edition_date,),
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def test_sections_counts_n20_blind3(conn, tmp_path):
    _seed(conn, 30)
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / "blindspots.md").write_text("- 示例盲区:与自己立场相反的一手叙述\n", "utf-8")
    candidates = [_candidate(i) for i in range(30)]
    result = build_edition(
        conn,
        date_local=TODAY,
        n=20,
        blind_share=0.15,
        now_utc=NOW,
        select_fn=_select_fn(candidates),
        embed_fn=_embed_flat,
        fetcher=_fetch_ok,
        llm_call=_llm_ok,
        profile_dir=str(profile_dir),
    )
    assert result["picked"] == 17
    assert result["blind"] == 3
    assert result["fulltext_ok"] == 20
    assert result["ai_ok"] == 20
    assert _section_counts(conn) == {"lead": 1, "top": 5, "briefs": 11, "blind": 3}


def test_muted_source_and_author_excluded(conn):
    _seed(conn, 30)
    conn.execute(
        "INSERT OR REPLACE INTO source_overrides (source, muted, boosted, updated_at) VALUES ('rss_briefing:feed_0', 1, 0, ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO item_ai (item_id, payload_json, model, error, created_at) VALUES ('item:01', ?, 'm', NULL, ?)",
        (json.dumps({"byline": "坏作者"}, ensure_ascii=False), NOW),
    )
    conn.execute(
        "INSERT OR REPLACE INTO author_trust (author_key, label, n_up, n_down, muted, updated_at) VALUES ('坏作者', '坏作者', 0, 0, 1, ?)",
        (NOW,),
    )
    conn.commit()
    candidates = [_candidate(i) for i in range(30)]
    build_edition(
        conn,
        date_local=TODAY,
        n=20,
        blind_share=0.15,
        now_utc=NOW,
        select_fn=_select_fn(candidates),
        embed_fn=_embed_flat,
        fetcher=_fetch_ok,
        llm_call=_llm_ok,
    )
    picked = _picked(conn)
    assert "item:00" not in picked  # 源 feed_0 静音
    assert "item:03" not in picked  # 同上
    assert "item:01" not in picked  # 署名作者静音


def test_items_from_previous_three_days_excluded(conn):
    _seed(conn, 30)
    conn.execute(
        "INSERT INTO editions (edition_date, item_id, section, rank, blind_reason, built_at) VALUES (?, 'item:05', 'briefs', 0, NULL, ?)",
        ((TODAY - timedelta(days=1)).isoformat(), NOW),
    )
    conn.commit()
    candidates = [_candidate(i) for i in range(30)]
    build_edition(
        conn,
        date_local=TODAY,
        n=20,
        blind_share=0.15,
        now_utc=NOW,
        select_fn=_select_fn(candidates),
        embed_fn=_embed_flat,
        fetcher=_fetch_ok,
        llm_call=_llm_ok,
    )
    assert "item:05" not in _picked(conn)


def test_lead_prefers_first_image_in_top3(conn):
    _seed(conn, 30)
    candidates = [_candidate(i) for i in range(30)]
    candidates[2] = _candidate(2, image=True)  # 前 3 条里的第 3 条带图
    build_edition(
        conn,
        date_local=TODAY,
        n=20,
        blind_share=0.15,
        now_utc=NOW,
        select_fn=_select_fn(candidates),
        embed_fn=_embed_flat,
        fetcher=_fetch_ok,
        llm_call=_llm_ok,
    )
    rows = conn.execute(
        "SELECT item_id, rank FROM editions WHERE edition_date=? AND section='lead'",
        (TODAY.isoformat(),),
    ).fetchall()
    assert [row["item_id"] for row in rows] == ["item:02"]
    top_ids = [
        row["item_id"]
        for row in conn.execute(
            "SELECT item_id FROM editions WHERE edition_date=? AND section='top' ORDER BY rank",
            (TODAY.isoformat(),),
        )
    ]
    assert top_ids == ["item:00", "item:01", "item:03", "item:04", "item:05"]


def test_blind_per_source_cap_and_reason(conn, tmp_path):
    _seed(conn, 30)
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / "blindspots.md").write_text(
        "- 示例盲区:与自己立场相反的一手叙述\n- 另一条盲区\n", "utf-8"
    )
    candidates = [_candidate(i) for i in range(30)]
    result = build_edition(
        conn,
        date_local=TODAY,
        n=20,
        blind_share=0.15,
        now_utc=NOW,
        select_fn=_select_fn(candidates),
        embed_fn=_embed_flat,
        fetcher=_fetch_ok,
        llm_call=_llm_ok,
        profile_dir=str(profile_dir),
    )
    # 主线吃掉 0..16; 盲区候选 17..29(全在 36h 内): 17..23 同源 feed_a(上限 2), 24..29 同源 feed_b
    blind_rows = conn.execute(
        "SELECT item_id, blind_reason FROM editions WHERE edition_date=? AND section='blind' ORDER BY rank",
        (TODAY.isoformat(),),
    ).fetchall()
    blind_ids = [row["item_id"] for row in blind_rows]
    assert blind_ids == ["item:17", "item:18", "item:24"]  # feed_a 第 3 条被上限挡下, 轮到 feed_b
    assert all(row["blind_reason"] == "示例盲区:与自己立场相反的一手叙述" for row in blind_rows)
    assert result["blind"] == 3


def test_blindspots_missing_file_means_empty_blind(conn, tmp_path):
    _seed(conn, 30)
    candidates = [_candidate(i) for i in range(30)]
    result = build_edition(
        conn,
        date_local=TODAY,
        n=20,
        blind_share=0.15,
        now_utc=NOW,
        select_fn=_select_fn(candidates),
        embed_fn=_embed_flat,
        fetcher=_fetch_ok,
        llm_call=_llm_ok,
        profile_dir=str(tmp_path / "no_such_dir"),
    )
    assert result["blind"] == 0
    assert _section_counts(conn) == {"lead": 1, "top": 5, "briefs": 11}


def test_same_day_rerun_no_duplicates(conn, tmp_path):
    _seed(conn, 30)
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / "blindspots.md").write_text("- 示例盲区:与自己立场相反的一手叙述\n", "utf-8")
    candidates = [_candidate(i) for i in range(30)]
    fetch_calls = []
    llm_calls = []

    def fetcher(url):
        fetch_calls.append(url)
        return "正文", "ok"

    def llm(prompt):
        llm_calls.append(prompt)
        return AI_JSON

    first = build_edition(
        conn,
        date_local=TODAY,
        n=20,
        blind_share=0.15,
        now_utc=NOW,
        select_fn=_select_fn(candidates),
        embed_fn=_embed_flat,
        fetcher=fetcher,
        llm_call=llm,
        profile_dir=str(profile_dir),
    )
    rows_after_first = conn.execute(
        "SELECT item_id, section, rank FROM editions WHERE edition_date=? ORDER BY item_id",
        (TODAY.isoformat(),),
    ).fetchall()
    fetch_after_first = len(fetch_calls)
    llm_after_first = len(llm_calls)

    second = build_edition(
        conn,
        date_local=TODAY,
        n=20,
        blind_share=0.15,
        now_utc=NOW,
        select_fn=_select_fn(candidates),
        embed_fn=_embed_flat,
        fetcher=fetcher,
        llm_call=llm,
        profile_dir=str(profile_dir),
    )
    rows_after_second = conn.execute(
        "SELECT item_id, section, rank FROM editions WHERE edition_date=? ORDER BY item_id",
        (TODAY.isoformat(),),
    ).fetchall()
    assert first == second
    assert [tuple(r) for r in rows_after_first] == [tuple(r) for r in rows_after_second]
    assert len(fetch_calls) == fetch_after_first  # 全文已抓, 不重复
    assert len(llm_calls) == llm_after_first  # AI 已做, 不重复


def test_plan_edition_has_no_side_effects(conn):
    _seed(conn, 30)
    candidates = [_candidate(i) for i in range(30)]

    def boom(*args, **kwargs):
        raise AssertionError("plan_edition 不许抓网/调模型/写库")

    plan = plan_edition(
        conn,
        date_local=TODAY,
        n=20,
        blind_share=0.15,
        now_utc=NOW,
        select_fn=_select_fn(candidates),
        embed_fn=boom,
    )
    assert plan["picked"] == 17 and plan["blind"] == 0
    assert plan["date"] == TODAY.isoformat()
    assert len(plan["sections"]["lead"]) == 1
    assert len(plan["sections"]["top"]) == 5
    assert len(plan["sections"]["briefs"]) == 11
    assert conn.execute("SELECT COUNT(*) FROM editions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM item_fulltext").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM item_ai").fetchone()[0] == 0


def test_cli_paper_dry_run_short_circuits_before_side_effects(monkeypatch, tmp_path, capsys):
    """dry-run 短路点在第一个副作用之前: 不写库、不抓网、不调模型。"""
    from personal_intel_loop import cli
    from personal_intel_loop import paper as paper_mod

    db = tmp_path / "cli.sqlite"
    conn = connect_db(db)
    ensure_schema(conn)
    _seed(conn, 24)
    conn.close()

    monkeypatch.setattr(cli, "DB_PATH", db)
    monkeypatch.setattr(cli, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(paper_mod, "_default_select", lambda conn, **kwargs: [_candidate(i) for i in range(24)])
    monkeypatch.setattr(
        paper_mod, "_default_embed", lambda texts: (_ for _ in ()).throw(AssertionError("dry-run 不许嵌入"))
    )
    monkeypatch.setattr(
        "personal_intel_loop.fulltext.ensure_fulltext",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("dry-run 不许抓全文")),
    )
    monkeypatch.setattr(
        "personal_intel_loop.paper_ai.preprocess",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("dry-run 不许调模型")),
    )

    rc = cli.main(["paper", "--date", "2026-10-04", "--n", "20", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[lead] 1" in out and "[top] 5" in out and "[briefs] 11" in out
    check = connect_db(db)
    try:
        assert check.execute("SELECT COUNT(*) FROM editions").fetchone()[0] == 0
        assert check.execute("SELECT COUNT(*) FROM item_fulltext").fetchone()[0] == 0
        assert check.execute("SELECT COUNT(*) FROM item_ai").fetchone()[0] == 0
    finally:
        check.close()


def test_cli_paper_writes_edition(monkeypatch, tmp_path):
    from personal_intel_loop import cli
    from personal_intel_loop import paper as paper_mod

    db = tmp_path / "cli2.sqlite"
    conn = connect_db(db)
    ensure_schema(conn)
    _seed(conn, 24)
    conn.close()

    monkeypatch.setattr(cli, "DB_PATH", db)
    monkeypatch.setattr(cli, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(paper_mod, "_default_select", lambda conn, **kwargs: [_candidate(i) for i in range(24)])
    # 非 dry-run 会补全文与 AI: 注入替身, 不许真抓网/真调模型
    monkeypatch.setattr("personal_intel_loop.fulltext.ensure_fulltext", lambda conn, ids, **kw: len(list(ids)))
    monkeypatch.setattr("personal_intel_loop.paper_ai.preprocess", lambda conn, ids, **kw: len(list(ids)))

    rc = cli.main(["paper", "--date", "2026-10-04", "--n", "20", "--blind-share", "0.15"])
    assert rc == 0
    check = connect_db(db)
    try:
        counts = {
            row[0]: row[1]
            for row in check.execute(
                "SELECT section, COUNT(*) FROM editions WHERE edition_date='2026-10-04' GROUP BY section"
            )
        }
        assert counts["lead"] == 1 and counts["briefs"] == 11
    finally:
        check.close()


def test_cli_paper_distill(monkeypatch, tmp_path):
    from personal_intel_loop import cli

    db = tmp_path / "cli3.sqlite"
    conn = connect_db(db)
    ensure_schema(conn)
    profile = tmp_path / "reading_profile.md"
    profile.write_text("# p\n\n## 待接受的修订\n-\n", "utf-8")
    for index in range(3):
        upsert_item(
            conn,
            make_item(item_id=f"d:{index}", title=f"标题 {index}"),
            adapter_name="rss_briefing",
            source_payload_json="{}",
        )
        conn.execute(
            "INSERT OR REPLACE INTO item_ratings (item_id, dim, value, author_key, ts, distilled_at) VALUES (?, 'style', -1, NULL, ?, NULL)",
            (f"d:{index}", NOW),
        )
    conn.commit()
    conn.close()

    monkeypatch.setattr(cli, "DB_PATH", db)
    monkeypatch.setattr(cli, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setenv("PIL_PROFILE_DIR", str(tmp_path))
    # 缺省 llm_call 走云端路径 → 注入 summarizer._try_cloud 的替身
    class _Resp:
        text = json.dumps(
            [
                {
                    "item_id": "d:0",
                    "dim": "style",
                    "basis": "三条 style -1",
                    "target": "文风",
                    "suggestion": "论战体降权",
                }
            ],
            ensure_ascii=False,
        )
        model = "test"

    import personal_intel_loop.paper_distill as paper_distill_mod

    monkeypatch.setattr(paper_distill_mod, "_call_cloud", lambda prompt: _Resp.text)

    rc = cli.main(["paper-distill", "--min-new", "3"])
    assert rc == 0
    assert "论战体降权" in profile.read_text("utf-8")
