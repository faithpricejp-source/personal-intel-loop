"""paper_distill.distill: 不足不调模型、足量追加可读可接受、distilled_at 回填、失败不回填。"""
from __future__ import annotations

import json

import pytest

from personal_intel_loop.paper_distill import distill
from personal_intel_loop.profile import pending_proposals, decide_proposal
from personal_intel_loop.store import connect_db, ensure_schema, upsert_item
from tests.conftest import make_item

NOW = "2026-10-04T00:00:00Z"


@pytest.fixture
def conn(tmp_path):
    c = connect_db(tmp_path / "db.sqlite")
    ensure_schema(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def profile_path(tmp_path):
    path = tmp_path / "reading_profile.md"
    path.write_text("# 阅读偏好\n\n正文若干\n\n## 待接受的修订\n-\n", "utf-8")
    return path


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


def test_below_min_new_returns_zero_without_llm(conn, profile_path):
    _add_rating(conn, "s1", "style", -1)
    _add_rating(conn, "t1", "topic", 1)
    calls = []

    def llm(prompt):
        calls.append(prompt)
        return "[]"

    assert distill(conn, llm_call=llm, profile_path=profile_path, min_new=3) == 0
    assert calls == []
    assert pending_proposals(profile_path) == []


def test_distill_appends_proposals_readable_and_decidable(conn, profile_path):
    _add_rating(conn, "s1", "style", -1, payload={"style_tags": ["论战体"], "topic": "AI 产业"})
    _add_rating(conn, "s2", "style", -1, payload={"style_tags": ["论战体"], "topic": "地缘"})
    _add_rating(conn, "t1", "topic", 1, payload={"style_tags": [], "topic": "港口物流"})

    def llm(prompt):
        assert "style -1" in prompt or "style 不喜欢" in prompt
        assert "港口物流" in prompt  # style_tags/topic 来自 item_ai
        return json.dumps(
            [
                {
                    "item_id": "s1",
                    "dim": "style",
                    "basis": "两条 style -1 都是论战体",
                    "target": "文风",
                    "suggestion": "论战体且无数据支撑的降权",
                },
                {
                    "item_id": "t1",
                    "dim": "topic",
                    "basis": "topic +1 命中港口物流",
                    "target": "新增于: 优先级",
                    "suggestion": "新增行「港口/航运物流 | 不饱和 | 优先」",
                },
                {"item_id": "ghost", "dim": "style", "basis": "x", "target": "y", "suggestion": "z"},  # 未知 item 应被丢弃
            ],
            ensure_ascii=False,
        )

    appended = distill(conn, llm_call=llm, profile_path=profile_path, min_new=3)
    assert appended == 2
    pending = pending_proposals(profile_path)
    assert len(pending) == 2
    assert pending[0].startswith("[2026-") and "· style · s1]" in pending[0]
    assert "依据: 两条 style -1 都是论战体" in pending[0]
    assert "→ 目标: 文风" in pending[0]
    assert "→ 建议: " in pending[0]
    # 追加的行能被 decide_proposal 接受(与现有提案回路兼容)
    remaining = decide_proposal(pending[0], "accept", profile_path)
    assert remaining == 1
    assert "## 已接受的修订" in profile_path.read_text("utf-8")


def test_distill_marks_distilled_and_reruns_zero(conn, profile_path):
    for index in range(3):
        _add_rating(conn, f"s{index}", "style", 1 if index % 2 else -1)
    calls = []

    def llm(prompt):
        calls.append(prompt)
        return "[]"

    assert distill(conn, llm_call=llm, profile_path=profile_path, min_new=3) == 0  # 模型说无可提案
    assert len(calls) == 1
    # 有可解析输出(哪怕 0 条)也算蒸馏完成
    assert all(row[0] is not None for row in conn.execute("SELECT distilled_at FROM item_ratings"))
    assert distill(conn, llm_call=llm, profile_path=profile_path, min_new=3) == 0
    assert len(calls) == 1  # 没有新的未蒸馏评分, 不再调模型


def test_distill_model_failure_returns_zero_and_keeps_pending(conn, profile_path):
    for index in range(3):
        _add_rating(conn, f"s{index}", "style", -1)
    assert distill(conn, llm_call=lambda p: "完全不是 JSON", profile_path=profile_path, min_new=3) == 0
    assert pending_proposals(profile_path) == []
    assert all(row[0] is None for row in conn.execute("SELECT distilled_at FROM item_ratings"))  # 未回填, 下次重试


def test_distill_llm_exception_returns_zero(conn, profile_path):
    for index in range(3):
        _add_rating(conn, f"s{index}", "style", -1)

    def llm(prompt):
        raise RuntimeError("down")

    assert distill(conn, llm_call=llm, profile_path=profile_path, min_new=3) == 0
    assert pending_proposals(profile_path) == []
