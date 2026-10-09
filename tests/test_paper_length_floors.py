"""10-04 首期 dry-run 发现：盲区版被微博短帖占满、单条微博回复进主线。两个长度下限的回归测试。"""
from datetime import date

import personal_intel_loop.paper as paper
from personal_intel_loop.store import connect_db, ensure_schema

NOW = "2026-10-04T00:00:00Z"


def _db(tmp_path):
    conn = connect_db(tmp_path / "t.sqlite")
    ensure_schema(conn)
    rows = [
        ("w1", "weibo_home:1", "微博长帖" * 200),
        ("r_short", "rss_briefing:a", "短"),
        ("r_long", "rss_briefing:b", "长文" * 300),
    ]
    for item_id, source, body in rows:
        conn.execute(
            "INSERT INTO items(item_id,source,url,title,body,author,ts,lang,tags_json,source_payload_json,content_hash,adapter_name,first_ingested_at,last_seen_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, source, f"https://e.com/{item_id}", item_id, body, None, NOW, "zh", "[]", "{}", item_id, source.split(":")[0], NOW, NOW),
        )
    conn.commit()
    return conn


def test_blind_skips_social_and_short(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "BLIND_MIN_BODY_CHARS", 300)
    conn = _db(tmp_path)
    picked = paper._select_blind(
        conn, now_utc=NOW, main_ids=set(), blindspots=["东南亚"], blind_n=5,
        embed_fn=lambda texts: [[1.0, 0.0] for _ in texts], date_local=date(2026, 10, 4),
    )
    assert [c["item_id"] for c, _reason in picked] == ["r_long"]


def test_blind_judge_requires_stakes(monkeypatch):
    # 10-05 用户：盲区只选「补上能少犯错、多得利」的——模型说不出可能改变的判断就不选
    import json
    import personal_intel_loop.paper_ai as paper_ai
    from personal_intel_loop.paper import default_blind_judge

    spots = ["东南亚产业"]
    cands = [{"item_id": "a", "title": "t1"}, {"item_id": "b", "title": "t2"}, {"item_id": "c", "title": "t3"}]
    raw = json.dumps({"0": {"spot": "东南亚产业", "stakes": "越南产能转移对日本商社的判断"},
                      "1": {"spot": "东南亚产业", "stakes": ""}, "2": "东南亚产业"}, ensure_ascii=False)
    monkeypatch.setattr(paper_ai, "_call_judge", lambda prompt: (raw, "m"))
    out = default_blind_judge(cands, spots)
    assert out["a"] == "东南亚产业｜可能影响：越南产能转移对日本商社的判断"
    assert "b" not in out and out["c"] == "东南亚产业"


def test_warm_candidates_come_from_db_not_interest_pool(tmp_path):
    # 10-05：温暖栏不走兴趣排序——候选直接从库里取近 48h 的温暖兜底源与人情味采集器条目
    from personal_intel_loop.paper import _warm_candidates
    from personal_intel_loop.store import connect_db, ensure_schema

    conn = connect_db(tmp_path / "w.sqlite")
    ensure_schema(conn)
    rows = [("w1", "rss_briefing:good_news_network", "rss_briefing", "2026-10-05T01:00:00Z"),
            ("w2", "html_columns:asahi_hito", "html_columns", "2026-10-05T02:00:00Z"),
            ("x1", "rss_briefing:ft", "rss_briefing", "2026-10-05T03:00:00Z"),
            ("old", "rss_briefing:good_news_network", "rss_briefing", "2026-10-01T00:00:00Z")]
    for item_id, source, adapter, ingested in rows:
        conn.execute(
            "INSERT INTO items(item_id,source,url,title,body,author,ts,lang,tags_json,source_payload_json,content_hash,adapter_name,first_ingested_at,last_seen_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, source, f"https://e.com/{item_id}", item_id, "b", None, ingested, "zh", "[]", "{}", item_id, adapter, ingested, ingested))
    conn.commit()
    got = _warm_candidates(conn, now_utc="2026-10-05T08:00:00Z", exclude={"w2"}, warmth_sources=["rss_briefing:good_news_network"], limit=10)
    assert [e["item_id"] for e, _ in got] == ["w1"]
    got = _warm_candidates(conn, now_utc="2026-10-05T08:00:00Z", exclude=set(), warmth_sources=["rss_briefing:good_news_network"], limit=10)
    assert [e["item_id"] for e, _ in got] == ["w2", "w1"]  # 人情味采集器先送预处理
