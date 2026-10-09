"""paper_ai.preprocess: 已处理跳过、失败写 error、全文优先、可续跑; 提示词构成。"""
from __future__ import annotations

import json

import pytest

from personal_intel_loop.paper_ai import PAPER_AI_BODY_LIMIT, build_paper_prompt, preprocess
from personal_intel_loop.store import connect_db, ensure_schema, upsert_item
from tests.conftest import make_item

NOW = "2026-10-04T00:00:00Z"

GOOD_JSON = json.dumps(
    {
        "lede": "导语两三句。",
        "one_liner": "一句话。",
        "backstory": None,
        "so_what": None,
        "claim": {"text": "断言", "check_after": "2027-01-01"},
        "byline": "李四",
        "style_tags": ["数据密集", "第一人称", "冷幽默", "第四个被截掉"],
        "topic": "AI 产业",
        "profile_hit": "命中句",
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


def _add_item(conn, item_id, body="条目自带正文"):
    upsert_item(
        conn,
        make_item(item_id=item_id, title=f"标题 {item_id}", body=body),
        adapter_name="rss_briefing",
        source_payload_json="{}",
    )
    conn.commit()


def _payload_of(conn, item_id):
    row = conn.execute("SELECT payload_json, model, error FROM item_ai WHERE item_id=?", (item_id,)).fetchone()
    return row


def test_preprocess_writes_normalized_payload(conn):
    _add_item(conn, "a1")
    calls = []

    def llm(prompt):
        calls.append(prompt)
        return GOOD_JSON

    processed = preprocess(conn, ["a1"], llm_call=llm)
    assert processed == 1 and len(calls) == 1
    row = _payload_of(conn, "a1")
    assert row["error"] is None and row["model"] is None
    payload = json.loads(row["payload_json"])
    assert payload["byline"] == "李四"
    assert payload["style_tags"] == ["数据密集", "第一人称", "冷幽默"]  # ≤3
    assert payload["claim"] == {"text": "断言", "check_after": "2027-01-01"}
    assert payload["lede"] == "导语两三句。"


def test_preprocess_rerun_skips_existing(conn):
    _add_item(conn, "a1")
    _add_item(conn, "a2")
    calls = []
    preprocess(conn, ["a1", "a2"], llm_call=lambda p: (calls.append(p) or GOOD_JSON))
    assert len(calls) == 2
    preprocess(conn, ["a1", "a2"], llm_call=lambda p: (calls.append(p) or GOOD_JSON))
    assert len(calls) == 2  # 重跑一个都不再调


def test_preprocess_resume_only_missing(conn):
    _add_item(conn, "a1")
    _add_item(conn, "a2")
    # a2 上次已处理
    conn.execute(
        "INSERT OR REPLACE INTO item_ai (item_id, payload_json, model, error, created_at) VALUES ('a2', '{}', NULL, NULL, ?)",
        (NOW,),
    )
    conn.commit()
    calls = []
    preprocess(conn, ["a1", "a2"], llm_call=lambda p: (calls.append(p) or GOOD_JSON))
    assert len(calls) == 1
    assert "标题 a1" in calls[0]


def test_preprocess_bad_json_writes_error(conn):
    _add_item(conn, "b1")
    processed = preprocess(conn, ["b1"], llm_call=lambda p: "这不是 JSON")
    assert processed == 0
    row = _payload_of(conn, "b1")
    assert row["payload_json"] == "{}"
    assert "invalid json" in row["error"]


def test_preprocess_llm_exception_writes_error_not_raise(conn):
    _add_item(conn, "b2")

    def llm(prompt):
        raise RuntimeError("cloud down")

    processed = preprocess(conn, ["b2"], llm_call=llm)
    assert processed == 0
    row = _payload_of(conn, "b2")
    assert "llm failed" in row["error"]


def test_preprocess_error_rows_retried_on_rerun(conn):
    # 10-05 审计 B1：LLM 故障写的 error 行不能固化，下次重跑要重试并以成功结果覆盖
    _add_item(conn, "b3")
    preprocess(conn, ["b3"], llm_call=lambda p: "垃圾")
    assert conn.execute("SELECT error FROM item_ai WHERE item_id='b3'").fetchone()[0] is not None
    calls = []
    preprocess(conn, ["b3"], llm_call=lambda p: (calls.append(p) or GOOD_JSON))
    assert len(calls) == 1
    assert conn.execute("SELECT error FROM item_ai WHERE item_id='b3'").fetchone()[0] is None
    calls2 = []
    preprocess(conn, ["b3"], llm_call=lambda p: (calls2.append(p) or GOOD_JSON))
    assert calls2 == []  # 成功后不再重跑


def test_preprocess_judgment_missing_marked_for_retry(conn, monkeypatch):
    # 10-05 审计 B2：两段式里判定段失败，机械字段照常入库但记 error，下次重试
    import personal_intel_loop.paper_ai as paper_ai

    _add_item(conn, "b4")
    monkeypatch.setattr(paper_ai, "_call_item_preprocess", lambda prompt: (GOOD_JSON, "deepseek-flash+-"))
    preprocess(conn, ["b4"])
    row = conn.execute("SELECT payload_json, error FROM item_ai WHERE item_id='b4'").fetchone()
    assert row[1] == "judgment missing" and json.loads(row[0]).get("lede")
    lede_before = json.loads(row[0])["lede"]
    # 10-06：重试只补判定段，不重跑机械段（机械字段可能是当晚到期的 DeepSeek 赠送额度做的）
    def _no_full(prompt):
        raise AssertionError("judgment-missing retry must not rerun the mechanical stage")
    monkeypatch.setattr(paper_ai, "_call_item_preprocess", _no_full)
    monkeypatch.setattr(paper_ai, "_call_judge", lambda prompt: (None, None))
    preprocess(conn, ["b4"])  # 判定仍失败：半份结果原样保留
    assert conn.execute("SELECT error FROM item_ai WHERE item_id='b4'").fetchone()[0] == "judgment missing"
    monkeypatch.setattr(paper_ai, "_call_judge", lambda prompt: (GOOD_JSON, "or-high"))
    preprocess(conn, ["b4"])
    row = conn.execute("SELECT payload_json, error, model FROM item_ai WHERE item_id='b4'").fetchone()
    assert row[1] is None and row[2] == "deepseek-flash+or-high"
    assert json.loads(row[0])["lede"] == lede_before


def test_preprocess_prefers_fulltext_over_body(conn):
    _add_item(conn, "c1", body="条目自带正文")
    conn.execute(
        "INSERT OR REPLACE INTO item_fulltext (item_id, text, status, fetched_at) VALUES ('c1', '抓回来的全文正文', 'ok', ?)",
        (NOW,),
    )
    conn.commit()
    calls = []
    preprocess(conn, ["c1"], llm_call=lambda p: (calls.append(p) or GOOD_JSON))
    assert "抓回来的全文正文" in calls[0]
    assert "条目自带正文" not in calls[0]


def test_preprocess_falls_back_to_body_when_fulltext_failed(conn):
    _add_item(conn, "c2", body="条目自带正文")
    conn.execute(
        "INSERT OR REPLACE INTO item_fulltext (item_id, text, status, fetched_at) VALUES ('c2', NULL, 'failed', ?)",
        (NOW,),
    )
    conn.commit()
    calls = []
    preprocess(conn, ["c2"], llm_call=lambda p: (calls.append(p) or GOOD_JSON))
    assert "条目自带正文" in calls[0]


def test_build_prompt_shape():
    body = "正" * (PAPER_AI_BODY_LIMIT + 100)
    prompt = build_paper_prompt(profile="画像正文", title="标题", source="rss_briefing:x", body=body)
    assert "画像正文" in prompt
    assert "标题" in prompt
    assert "rss_briefing:x" in prompt
    assert "正" * PAPER_AI_BODY_LIMIT in prompt
    assert "正" * (PAPER_AI_BODY_LIMIT + 100) not in prompt  # 截到 6000
