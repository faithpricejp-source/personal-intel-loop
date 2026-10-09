"""10-05 审计修复的回归测试。每条修复一个测试, 修复前失败、修复后通过。

对应审计: audits/AUDIT_A.md(A2/B4/B5)、AUDIT_B.md(API-2/API-3/FB-1/FB-2)、
AUDIT_C.md(F1/F2/F4/F10/F11/F12/F13/F14/F16/F17/F19/F21/F22)。
"""
from __future__ import annotations

import json
import socket
import sqlite3
from datetime import date

import pytest

from personal_intel_loop.paper import build_edition
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


@pytest.fixture
def conn(tmp_path):
    c = connect_db(tmp_path / "db.sqlite")
    ensure_schema(c)
    try:
        yield c
    finally:
        c.close()


def _source_of(index: int) -> str:
    if 17 <= index <= 23:
        return "rss_briefing:feed_a"
    if index >= 24:
        return "rss_briefing:feed_b"
    return f"rss_briefing:feed_{index % 3}"


def _candidate(index: int) -> dict:
    return {
        "item_id": f"item:{index:02d}",
        "source": _source_of(index),
        "source_payload": {"feed_name": f"源 {index % 3}"},
        "title": f"标题 {index}",
        "body": "正文",
        "ts_utc": NOW,
    }


def _seed(conn, count: int = 30):
    for index in range(count):
        upsert_item(
            conn,
            make_item(
                item_id=f"item:{index:02d}",
                source=_source_of(index),
                url=f"https://example.com/{index}",
                title=f"标题 {index}",
            ),
            adapter_name="rss_briefing",
            source_payload_json=json.dumps({"feed_name": f"源 {index % 3}"}, ensure_ascii=False),
        )
    conn.commit()


def _select_fn(candidates):
    def select(conn, *, date_local, top_k, now_utc):
        return [dict(candidate) for candidate in candidates]

    return select


def _embed_flat(texts):
    return [[0.0, 0.0, 1.0] for _ in texts]


def _fetch_ok(url):
    return "这是抓回来的正文", "ok"


def _llm_ok(prompt):
    return AI_JSON


# --- A2: spool 补收护栏只捕 OSError, sqlite 层出错会炸掉整个出版 ---


def test_spool_drain_sqlite_error_does_not_abort_build(conn, monkeypatch, tmp_path):
    import personal_intel_loop.paper_inbox as paper_inbox

    def boom(_conn, _spool_dir, *, now_utc):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(paper_inbox, "drain_spool", boom)
    result = build_edition(
        conn,
        date_local=TODAY,
        n=20,
        blind_share=0.15,
        now_utc=NOW,
        select_fn=_select_fn([_candidate(i) for i in range(30)]),
        embed_fn=_embed_flat,
        fetcher=_fetch_ok,
        llm_call=_llm_ok,
        profile_dir=str(tmp_path / "profile"),
    )
    assert result["picked"] == 17, "补收撞上 sqlite 错误不得让整期出版失败"


# --- B4: _asr_youtube 解析 stdout 的 except 漏 AttributeError, 非对象 JSON 穿出本函数 ---


def test_asr_youtube_non_object_json_last_line_returns_empty(monkeypatch):
    import subprocess

    from personal_intel_loop.paper_ai import _asr_youtube

    class _R:
        returncode = 0
        stdout = "123\n"  # 合法 JSON 但不是对象: 旧代码 .get 抛 AttributeError
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: _R())
    assert _asr_youtube("vid123", "中文标题") == ""


# --- B5: default_vault_lookup 吞掉一切异常返回 [], vault 长期不可用时无人知晓 ---


def test_default_vault_lookup_failure_logs_warning(monkeypatch, caplog):
    import logging

    import personal_intel_loop.vault_corpus as vault_corpus

    from personal_intel_loop.paper_ai import default_vault_lookup

    def boom():
        raise RuntimeError("embeddings model broken")

    monkeypatch.setattr(vault_corpus, "assert_fresh", boom)
    with caplog.at_level(logging.WARNING, logger="personal_intel_loop.paper_ai"):
        assert default_vault_lookup("某段正文") == [], "vault 失败仍要返回空列表不阻塞预处理"
    assert any("vault" in record.getMessage().lower() for record in caplog.records), "降级必须留日志"


# --- API-2: 合成条目的 digests 行缺失时, 整期 editions 接口报错而非跳过 ---


def _profile_path(tmp_path):
    path = tmp_path / "reading_profile.md"
    path.write_text("# 阅读偏好\n\n正文\n\n## 待接受的修订\n", "utf-8")
    return path


def _store_synthetic(conn, digest_id: str, title: str):
    from personal_intel_loop.paper_common import item_frame

    payload = item_frame()
    payload.update({"item_id": digest_id, "title": title, "lede": "导语", "one_liner": title})
    conn.execute(
        "INSERT INTO digests (digest_id, edition_date, payload_json, member_ids_json, created_at) VALUES (?, '2026-10-04', ?, '[]', ?)",
        (digest_id, json.dumps(payload, ensure_ascii=False), NOW),
    )


def test_edition_with_missing_synthetic_digest_still_served(conn, tmp_path):
    from personal_intel_loop.paper_api import get_editions

    _store_synthetic(conn, "digest:good", "综述在")
    # 一好一坏两条 editions 引用: 坏的 digest 行已被保留期裁剪
    conn.execute(
        "INSERT INTO editions (edition_date, item_id, section, rank, blind_reason, built_at) VALUES ('2026-10-04', 'digest:good', 'counter', 0, NULL, ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO editions (edition_date, item_id, section, rank, blind_reason, built_at) VALUES ('2026-10-04', 'digest:gone', 'counter', 1, NULL, ?)",
        (NOW,),
    )
    conn.commit()
    result = get_editions(conn, before="2026-10-05", limit=1, profile_path=_profile_path(tmp_path))
    assert len(result["editions"]) == 1, "一条脏引用不得放大成整期拿不到"
    counter_ids = [item["item_id"] for item in result["editions"][0]["sections"]["counter"]]
    assert counter_ids == ["digest:good"], "缺 digests 行的合成条目应被跳过而不是炸掉整期"


# --- API-3: payload["byline"] 为非空非字符串时 .strip() 抛 AttributeError ---


def _seed_item_with_dirty_byline(conn, item_id: str):
    upsert_item(
        conn,
        make_item(item_id=item_id, source="rss_briefing:feed_x", url=f"https://example.com/{item_id}", title="标题"),
        adapter_name="rss_briefing",
        source_payload_json="{}",
    )
    conn.execute(
        "INSERT OR REPLACE INTO item_ai (item_id, payload_json, model, error, created_at) VALUES (?, ?, 'm', NULL, ?)",
        (item_id, json.dumps({"lede": "导语", "one_liner": "一句话", "byline": 123}, ensure_ascii=False), NOW),
    )
    conn.commit()


def test_item_dict_dirty_byline_dropped_not_crash(conn):
    from personal_intel_loop.paper_api import get_item

    _seed_item_with_dirty_byline(conn, "rss:d1")
    item = get_item(conn, "rss:d1")
    assert item["author_is_byline"] is False, "脏 byline(数字)按缺失处理"


def test_archive_item_dirty_byline_dropped_not_crash(conn):
    from personal_intel_loop.paper_api import get_archive

    _seed_item_with_dirty_byline(conn, "rss:d2")
    result = get_archive(conn, limit=10)
    rows = [row for row in result["items"] if row["item_id"] == "rss:d2"]
    assert len(rows) == 1
    assert rows[0]["author_label"], "archive 列表不因脏 byline 报 500"


# --- FB-1: 撤销一条不存在的 author 评分也会插 author_trust 行(幽灵作者) ---


def _add_feedback_item(conn, item_id="rss:p1"):
    upsert_item(
        conn,
        make_item(item_id=item_id, source="rss_briefing:feed_one"),
        adapter_name="rss_briefing",
        source_payload_json="{}",
    )
    conn.commit()


def _insert_ai_payload(conn, item_id, payload):
    conn.execute(
        "INSERT OR REPLACE INTO item_ai (item_id, payload_json, model, error, created_at) VALUES (?, ?, 'test-model', NULL, ?)",
        (item_id, json.dumps(payload, ensure_ascii=False), NOW),
    )
    conn.commit()


def test_author_undo_without_prior_rating_inserts_no_ghost_row(conn):
    from personal_intel_loop.paper_feedback import rate

    _add_feedback_item(conn)
    result = rate(conn, "rss:p1", "author", 0, now_utc=NOW)
    assert result["ok"] is True
    rows = conn.execute("SELECT * FROM author_trust").fetchall()
    assert rows == [], "撤销一条从未存在的评分不得插 (0,0) 幽灵作者行"


# --- FB-2: author 评分「改主意覆盖」且署名归一键已变时, 旧作者的计数不回退 ---


def test_author_overwrite_after_key_change_rolls_back_old_author(conn):
    from personal_intel_loop.paper_feedback import rate

    _add_feedback_item(conn)
    _insert_ai_payload(conn, "rss:p1", {"byline": "作者A"})
    rate(conn, "rss:p1", "author", -1, now_utc=NOW)
    _insert_ai_payload(conn, "rss:p1", {"byline": "作者B"})  # AI 重跑后署名归一键变了
    rate(conn, "rss:p1", "author", 1, now_utc=NOW)
    counts = {row["author_key"]: (row["n_up"], row["n_down"]) for row in conn.execute("SELECT * FROM author_trust")}
    assert counts.get("作者A") == (0, 0), "旧作者键上的 -1 计数必须回退"
    assert counts.get("作者B") == (1, 0), "新作者键只记新评分"
    stored = conn.execute("SELECT author_key FROM item_ratings WHERE item_id='rss:p1' AND dim='author'").fetchone()
    assert stored["author_key"] == "作者B"


# --- F1: item_frame() 的 my 骨架缺 note 键, 合成条目不符合契约 §11 ---


def test_item_frame_my_has_note_key():
    from personal_intel_loop.paper_common import item_frame

    frame = item_frame()
    assert "note" in frame["my"], "契约 §11: Item.my.note 为当前批注或 null, 骨架必须带这个键"
    assert frame["my"]["note"] is None


# --- F2: cosine() 对不等长向量静默截断, 相似度值不可信 ---


def test_cosine_length_mismatch_raises_instead_of_silent_truncation():
    from personal_intel_loop.paper_common import cosine

    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0), "等长输入行为不变"
    with pytest.raises(ValueError):
        cosine([1.0, 0.0], [0.0, 1.0, 9.0, 9.0]), "不等长输入必须显式报错, 不得静默截断"


# --- F4: 单条行程 LLM 失败即静默跳过, ≤3 天的 urgent 投递推送被一并跳过 ---


def test_generate_llm_failure_still_pushes_urgent_inbox(conn):
    from personal_intel_loop.paper_risk import generate

    conn.execute(
        "INSERT INTO trips (trip_id, place, country, start_date, end_date, note, created_at) VALUES ('t1', '巴黎', '法国', '2026-10-06', NULL, NULL, ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO inbox (source, title, body, url, priority, dedup_key, created_at, read_at) VALUES ('x.alert', '巴黎地铁罢工', '详情', NULL, 'urgent', NULL, '2026-10-04T01:00:00Z', NULL)",
    )
    conn.commit()

    def boom(prompt):
        raise RuntimeError("llm down")

    payloads, member_map = generate(
        conn,
        date_local=TODAY,
        now_utc=NOW,
        llm_call=boom,
        search_fn=lambda place: [],
        reference_fn=lambda place, country: "",
    )
    assert payloads == [] and member_map == {}, "LLM 失败的行程本期没有风险简报"
    pushed = conn.execute("SELECT title FROM inbox WHERE source='pil.risk'").fetchall()
    assert len(pushed) == 1, "行程 ≤3 天的 urgent 投递推送与 LLM 无关, 不得被同一 continue 吞掉"


# --- F10: suggest() 对同一候选重复 pick 时 INSERT OR IGNORE 被忽略但 created 照加 ---


def test_follow_suggested_counts_only_actually_inserted(conn, tmp_path):
    from personal_intel_loop.paper_follow import suggest

    upsert_item(
        conn,
        make_item(item_id="rss:m1", source="weibo_timeline:user1", url="https://weibo.example/1", title="转发标题"),
        adapter_name="weibo_timeline",
        source_payload_json=json.dumps({"mentioned_accounts": [{"id": "acc1", "name": "账号一"}]}, ensure_ascii=False),
    )
    conn.commit()

    def llm(prompt):
        # 两条 pick: 一条带 platform, 一条只带 account_id——by_id 回退解析到同一候选
        return json.dumps(
            {
                "picks": [
                    {"platform": "weibo", "account_id": "acc1", "reason": "证据:「转发标题」值得关注"},
                    {"account_id": "acc1", "reason": "再次挑同一候选:「转发标题」"},
                ]
            },
            ensure_ascii=False,
        )

    result = suggest(conn, llm_call=llm, profile_path=_profile_path(tmp_path), now_utc=NOW)
    assert result["suggested"] == 1, "重复 pick 落到同一候选只能算一条, 不得虚报"
    assert conn.execute("SELECT COUNT(*) FROM follow_suggestions").fetchone()[0] == 1


# --- F11: 综述 quotes 无 2 条下限, 0 条引用的综述照常产出 ---


def test_digest_with_too_few_quotes_not_produced(conn):
    from personal_intel_loop.paper_social import build_digests

    social_scored = [
        (
            {
                "item_id": "weibo_timeline:u1:m1",
                "source": "weibo_timeline:u1",
                "source_payload": {"account_name": "账号一"},
                "title": "标题一",
                "body": "帖子正文一",
                "ts_utc": NOW,
            },
            1.0,
        ),
        (
            {
                "item_id": "weibo_timeline:u1:m2",
                "source": "weibo_timeline:u1",
                "source_payload": {"account_name": "账号二"},
                "title": "标题二",
                "body": "帖子正文二",
                "ts_utc": NOW,
            },
            0.9,
        ),
    ]

    def embed(texts):
        return [[1.0, 0.0], [0.99, 0.1]]  # 高相似 → 同一组

    def llm(prompt):
        # title/lede 齐备但 quotes 空 → 契约 §6.4 要求带原话, 不得照常上版
        return json.dumps({"title": "话题", "lede": "导语", "quotes": [], "topic": "AI"}, ensure_ascii=False)

    digests, consumed = build_digests(conn, date_iso="2026-10-04", social_scored=social_scored, embed_fn=embed, llm_call=llm, now_utc=NOW)
    assert digests == [], "0 条引用的综述不得产出"
    assert consumed == set()


def test_digest_with_one_quote_still_produced(conn):
    from personal_intel_loop.paper_social import build_digests

    social_scored = [
        (
            {
                "item_id": "weibo_timeline:u1:m1",
                "source": "weibo_timeline:u1",
                "source_payload": {"account_name": "账号一"},
                "title": "标题一",
                "body": "帖子正文一",
                "ts_utc": NOW,
            },
            1.0,
        ),
        (
            {
                "item_id": "weibo_timeline:u1:m2",
                "source": "weibo_timeline:u1",
                "source_payload": {"account_name": "账号二"},
                "title": "标题二",
                "body": "帖子正文二",
                "ts_utc": NOW,
            },
            0.9,
        ),
    ]

    def embed(texts):
        return [[1.0, 0.0], [0.99, 0.1]]

    def llm(prompt):
        # 1 条 quote: 既有测试(test_paper_platform_social.DIGEST_JSON)钉死的合法行为, 不得回归
        return json.dumps(
            {"title": "话题", "lede": "导语", "quotes": [{"text": "原话", "author_label": "甲", "url": "https://weibo.example/1"}], "topic": "AI"},
            ensure_ascii=False,
        )

    digests, consumed = build_digests(conn, date_iso="2026-10-04", social_scored=social_scored, embed_fn=embed, llm_call=llm, now_utc=NOW)
    assert len(digests) == 1
    assert consumed == {"weibo_timeline:u1:m1", "weibo_timeline:u1:m2"}


# --- F12: 同日重跑组员集变化 → 换 digest_id, 旧综述行不被替换, 同日残留两条综述 ---


def _digest_payload(digest_id: str, title: str):
    from personal_intel_loop.paper_common import item_frame

    payload = item_frame()
    payload.update({"item_id": digest_id, "title": title, "lede": "导语", "one_liner": title})
    return payload


def test_store_digests_member_change_replaces_same_day_old_row(conn):
    from personal_intel_loop.paper_social import store_digests

    store_digests(conn, date_iso="2026-10-04", payloads=[_digest_payload("digest:old", "话题")], member_map={"digest:old": ["i1", "i2"]}, now_utc=NOW)
    # 同日重跑, 聚类组员多了一条 → digest_id 变了
    store_digests(conn, date_iso="2026-10-04", payloads=[_digest_payload("digest:new", "话题(组员+1)")], member_map={"digest:new": ["i1", "i2", "i3"]}, now_utc=NOW)
    ids = {row["digest_id"] for row in conn.execute("SELECT digest_id FROM digests WHERE edition_date='2026-10-04'")}
    assert ids == {"digest:new"}, "组员集变化后旧综述行必须被替换, 同日同话题不得残留两条"

    # 组员无交集的别的综述不受影响; risk 条目(related ids 可能与综述组员重叠)也不得误删
    store_digests(conn, date_iso="2026-10-04", payloads=[_digest_payload("digest:other", "别的话题")], member_map={"digest:other": ["i9"]}, now_utc=NOW)
    store_digests(conn, date_iso="2026-10-04", payloads=[_digest_payload("risk:t1:2026-10-04", "风险")], member_map={"risk:t1:2026-10-04": ["i1"]}, now_utc=NOW)
    store_digests(conn, date_iso="2026-10-04", payloads=[_digest_payload("digest:newer", "话题(再跑)")], member_map={"digest:newer": ["i1", "i3"]}, now_utc=NOW)
    ids = {row["digest_id"] for row in conn.execute("SELECT digest_id FROM digests WHERE edition_date='2026-10-04'")}
    assert ids == {"digest:other", "digest:newer", "risk:t1:2026-10-04"}, "只清被取代的 digest:, 别的话题与 risk 条目原样保留"


# --- F13: cluster_candidates 的 candidates 参数从未使用, 长度不一致时下标越界 ---


def test_cluster_candidates_length_mismatch_raises():
    from personal_intel_loop.paper_social import cluster_candidates

    with pytest.raises(ValueError):
        cluster_candidates([{"a": 1}, {"b": 2}], [[]]), "candidates 与 vectors 长度不一致必须显式报错"


# --- F14: distill 打标不限本次送评的 item_id, 调用窗口内新增的评分被标「已蒸馏」但从未蒸馏 ---


def _add_rating(conn, item_id, dim, value, *, payload=None):
    upsert_item(
        conn,
        make_item(item_id=item_id, title=f"标题 {item_id}"),
        adapter_name="rss_briefing",
        source_payload_json="{}",
    )
    if payload is not None:
        conn.execute(
            "INSERT OR REPLACE INTO item_ai (item_id, payload_json, model, error, created_at) VALUES (?, ?, 'm', NULL, ?)",
            (item_id, json.dumps(payload, ensure_ascii=False), NOW),
        )
    conn.execute(
        "INSERT OR REPLACE INTO item_ratings (item_id, dim, value, author_key, ts, distilled_at) VALUES (?, ?, ?, NULL, ?, NULL)",
        (item_id, dim, value, NOW),
    )
    conn.commit()


def test_distill_marks_only_sent_ratings(conn, tmp_path):
    from personal_intel_loop.paper_distill import distill

    _add_rating(conn, "s1", "style", -1)
    _add_rating(conn, "s2", "style", -1)
    _add_rating(conn, "t1", "topic", 1)

    def llm(prompt):
        # 模拟 120s 模型调用窗口内用户新增的评分: 不在本次快照里, 不得被打标
        upsert_item(
            conn,
            make_item(item_id="late1", title="标题 late1"),
            adapter_name="rss_briefing",
            source_payload_json="{}",
        )
        conn.execute(
            "INSERT OR REPLACE INTO item_ratings (item_id, dim, value, author_key, ts, distilled_at) VALUES ('late1', 'style', -1, NULL, ?, NULL)",
            (NOW,),
        )
        conn.commit()
        return json.dumps([{"item_id": "s1", "dim": "style", "basis": "两条都是论战体", "target": "文风", "suggestion": "s"}], ensure_ascii=False)

    assert distill(conn, llm_call=llm, profile_path=_profile_path(tmp_path)) == 1
    marked = {row["item_id"] for row in conn.execute("SELECT item_id FROM item_ratings WHERE distilled_at IS NOT NULL")}
    assert marked == {"s1", "s2", "t1"}, "只给本次送评的评分打标"
    assert conn.execute("SELECT distilled_at FROM item_ratings WHERE item_id='late1'").fetchone()[0] is None, "窗口内新增评分要留给下次蒸馏"


# --- F16: distill 失败路径零日志, LLM 异常与解析失败均静默 return 0 ---


def test_distill_failure_paths_log_warning(conn, tmp_path, caplog):
    import logging

    from personal_intel_loop.paper_distill import distill

    _add_rating(conn, "s1", "style", -1)
    _add_rating(conn, "s2", "style", -1)
    _add_rating(conn, "t1", "topic", 1)

    def boom(prompt):
        raise RuntimeError("llm down")

    with caplog.at_level(logging.WARNING, logger="personal_intel_loop.paper_distill"):
        assert distill(conn, llm_call=boom, profile_path=_profile_path(tmp_path)) == 0
    assert any("llm down" in record.getMessage() for record in caplog.records), "模型失败必须留日志"

    caplog.clear()

    def bad_json(prompt):
        return "完全不是 JSON 的输出"

    with caplog.at_level(logging.WARNING, logger="personal_intel_loop.paper_distill"):
        assert distill(conn, llm_call=bad_json, profile_path=_profile_path(tmp_path)) == 0
    assert caplog.records, "解析失败必须留日志"


# --- F17: accept() 的 24h 去重是 SELECT 后 INSERT, 并发/快速重试可重复入库 ---


def test_accept_racing_same_dedup_key_inserts_once(conn, tmp_path):
    from personal_intel_loop.paper_inbox import accept

    # 复现竞态: 用 trace 钩子在 accept 的 INSERT 即将执行时, 让同一库文件上的
    # 「另一连接」抢先提交同 key 投递(即对手在判重 SELECT 之后、我方 INSERT 之前落库)。
    state = {"rival_inserted": False}
    rival = sqlite3.connect(tmp_path / "db.sqlite")

    def trace(sql):
        if not state["rival_inserted"] and sql.lstrip().startswith("INSERT INTO inbox"):
            state["rival_inserted"] = True
            rival.execute("INSERT INTO inbox (source, title, body, url, priority, dedup_key, created_at, read_at) VALUES (?, ?, ?, NULL, ?, ?, ?, NULL)", ("other.notice", "对手投递", "b", "normal", "k1", "2026-10-04T01:59:59Z"))
            rival.commit()

    conn.set_trace_callback(trace)
    try:
        result = accept(
            conn,
            {"source": "proj.notice", "title": "我的投递", "body": "b", "priority": "normal", "dedup_key": "k1"},
            now_utc=NOW,
        )
    finally:
        conn.set_trace_callback(None)
        rival.close()

    assert state["rival_inserted"], "竞态注入必须发生过"
    assert result["deduped"] is True, "竞态下慢的一方应判定为重复"
    assert result["inbox_id"] == 1, "返回的是已入库那条(对手)的 id"
    count = conn.execute("SELECT COUNT(*) FROM inbox WHERE dedup_key='k1'").fetchone()[0]
    assert count == 1, "同 key 24h 内只允许一行"


# --- F19: TOML 上限值是布尔时穿过 int 校验, caps 静默变成 True/False ---


def test_layout_bool_cap_rejected(tmp_path):
    from personal_intel_loop.paper_layout import DEFAULT_CAPS, load_layout

    path = tmp_path / "paper_layout.toml"
    path.write_text("[layout]\ntop = true\nlead = 2\n", "utf-8")
    layout = load_layout(path)
    assert layout["caps"]["top"] == DEFAULT_CAPS["top"], "布尔上限不是合法 int, 不得进 caps"
    assert layout["caps"]["lead"] == 2, "同文件里的正常 int 上限照常生效"


# --- F21: paper_layout.toml 语法错误被静默吞掉, 整套自定义上限无痕回落缺省 ---


def test_layout_broken_toml_logs_warning_and_falls_back(tmp_path, caplog):
    import logging

    from personal_intel_loop.paper_layout import DEFAULT_CAPS, load_layout

    path = tmp_path / "paper_layout.toml"
    path.write_text("layout = [bad", "utf-8")
    with caplog.at_level(logging.WARNING, logger="personal_intel_loop.paper_layout"):
        layout = load_layout(path)
    assert layout["caps"] == dict(DEFAULT_CAPS), "坏 TOML 仍回落缺省(既有约定)"
    assert caplog.records, "语法错误与文件缺失不同, 必须留痕"


# --- F22: 重定向只跟一跳, 第二跳仍是 3xx 时把跳转页 HTML 当正文 ---


class _FakeResp:
    def __init__(self, url, status, text, location=None):
        self.url = url
        self.status_code = status
        self.text = text
        self.headers = {"Location": location} if location else {}

    @property
    def is_redirect(self):
        return 300 <= self.status_code < 400 and "Location" in self.headers

    @property
    def is_permanent_redirect(self):
        return self.status_code in (301, 308)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http error {self.status_code}")


_PAGES = {
    "http://a.example.com/x": (302, "", "http://b.example.com/y"),
    "http://b.example.com/y": (301, "", "https://c.example.com/z"),
    "https://c.example.com/z": (302, "", "https://d.example.com/final"),
    "https://d.example.com/final": (200, "<html>正文</html>", None),
}


def _patch_http(monkeypatch, pages):
    import requests as requests_mod

    from personal_intel_loop import fulltext

    requested = []

    def fake_get(url, **kwargs):
        requested.append(url)
        assert kwargs.get("allow_redirects") is False
        status, text, location = pages[url]
        return _FakeResp(url, status, text, location)

    monkeypatch.setattr(fulltext, "_pinned_get", fake_get)  # 单跳出口收口到 _pinned_get
    monkeypatch.setattr(fulltext.socket, "getaddrinfo", lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))])
    return requested


def test_default_http_get_follows_multi_hop_redirects(monkeypatch):
    from personal_intel_loop import fulltext

    requested = _patch_http(monkeypatch, _PAGES)
    html = fulltext._default_http_get("http://a.example.com/x")
    assert html == "<html>正文</html>", "三跳之后必须拿到真正的正文而不是跳转页"
    assert requested[-1] == "https://d.example.com/final"


def test_default_http_get_redirect_loop_fails(monkeypatch):
    from personal_intel_loop import fulltext

    loop_pages = {"http://a.example.com/x": (302, "", "http://a.example.com/x")}
    _patch_http(monkeypatch, loop_pages)
    with pytest.raises(ValueError):
        fulltext._default_http_get("http://a.example.com/x"), "重定向超限要显式失败, 不得把跳转页当正文"




