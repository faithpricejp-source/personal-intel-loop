"""反馈事件连续失败 3 次跳过（）：调真函数 propose_from_feedback。"""
from __future__ import annotations

import json
import logging

from personal_intel_loop.profile import pending_proposals
from personal_intel_loop.profile_proposals import propose_from_feedback
from tests.test_zpil_r_fixes import _proposal_json, _seed_reason


def _setup(db_conn, tmp_path):
    profile = tmp_path / "reading_profile.md"
    profile.write_text("# p\n\n## 待接受的修订\n-\n", "utf-8")
    state = tmp_path / "state.json"
    _seed_reason(db_conn, "keep", "rss:ok0", "2026-10-07T00:30:00Z")
    _seed_reason(db_conn, "keep", "rss:bad", "2026-10-07T01:00:00Z")
    _seed_reason(db_conn, "keep", "rss:after", "2026-10-07T02:00:00Z")
    return profile, state


def _bad_llm(calls):
    def llm(prompt: str):
        calls.append(prompt)
        return None if "title rss:bad" in prompt else _proposal_json(change="ok")
    return llm


def _state(path):
    return json.loads(path.read_text("utf-8")) if path.exists() else {}


def test_first_two_failures_stay_put_and_count_persists(db_conn, tmp_path):
    profile, state = _setup(db_conn, tmp_path)
    calls: list[str] = []
    propose_from_feedback(db_conn, llm=_bad_llm(calls), profile_path=profile, state_path=state)
    saved = _state(state)
    assert saved["last_event_ts"] == "2026-10-07T00:30:00Z"
    assert list(saved.get("fail_counts", {}).values()) == [1]

    propose_from_feedback(db_conn, llm=_bad_llm(calls), profile_path=profile, state_path=state)
    saved = _state(state)
    assert saved["last_event_ts"] == "2026-10-07T00:30:00Z"
    assert list(saved.get("fail_counts", {}).values()) == [2]
    assert not any("rss:after" in line for line in pending_proposals(profile))


def test_third_failure_skips_logs_and_advances(db_conn, tmp_path, caplog):
    profile, state = _setup(db_conn, tmp_path)
    calls: list[str] = []
    for _ in range(2):
        propose_from_feedback(db_conn, llm=_bad_llm(calls), profile_path=profile, state_path=state)
    with caplog.at_level(logging.WARNING, logger="personal_intel_loop.profile_proposals"):
        third = propose_from_feedback(db_conn, llm=_bad_llm(calls), profile_path=profile, state_path=state)
    assert any(s["reason"] == "skipped_after_3_failures" for s in third["skipped"])
    # 越过失败事件, 同一轮继续处理后面的
    assert any("rss:after" in line for line in pending_proposals(profile))
    saved = _state(state)
    assert saved["last_event_ts"] == "2026-10-07T02:00:00Z"
    assert saved.get("fail_counts", {}) == {}
    skipped = saved["skipped_events"]
    assert len(skipped) == 1 and skipped[0]["item_id"] == "rss:bad" and skipped[0]["failures"] == 3
    assert any("rss:bad" in rec.getMessage() and rec.levelno == logging.WARNING for rec in caplog.records)


def test_success_clears_count(db_conn, tmp_path):
    profile, state = _setup(db_conn, tmp_path)
    calls: list[str] = []
    propose_from_feedback(db_conn, llm=_bad_llm(calls), profile_path=profile, state_path=state)
    assert _state(state)["fail_counts"]
    propose_from_feedback(db_conn, llm=lambda p: _proposal_json(change="ok"), profile_path=profile, state_path=state)
    saved = _state(state)
    assert saved.get("fail_counts", {}) == {}
    assert saved["last_event_ts"] == "2026-10-07T02:00:00Z"


def test_skipped_event_not_retried_when_same_ts_sibling_rolls_back(db_conn, tmp_path):
    """被跳过的 X 与仍在失败的 Y 同 ts: Y 让水位线退回严格更早位置, X 不得被重新计数重试。"""
    profile = tmp_path / "reading_profile.md"
    profile.write_text("# p\n\n## 待接受的修订\n-\n", "utf-8")
    state = tmp_path / "state.json"
    _seed_reason(db_conn, "keep", "rss:t0", "2026-10-07T00:30:00Z")
    _seed_reason(db_conn, "keep", "rss:x", "2026-10-07T01:00:00Z")
    _seed_reason(db_conn, "unclear", "rss:y", "2026-10-07T01:00:00Z")
    x_calls: list[str] = []
    phase = {"y_bad": False}

    def llm(prompt: str):
        if "title rss:x" in prompt:
            x_calls.append(prompt)
            return None
        if "title rss:y" in prompt and phase["y_bad"]:
            return None
        return _proposal_json(change="ok")

    # 先把 x 打满 3 次失败(第 3 次被跳过), y 此时成功。
    for _ in range(3):
        propose_from_feedback(db_conn, llm=llm, profile_path=profile, state_path=state)
    assert len(x_calls) == 3
    # 回放场景: 清水位线让同 ts 两条再被捞回, y 变坏
    saved = _state(state)
    saved["last_event_ts"] = "2026-10-07T00:30:00Z"
    state.write_text(json.dumps(saved), "utf-8")
    profile.write_text("# p\n\n## 待接受的修订\n-\n", "utf-8")
    phase["y_bad"] = True
    propose_from_feedback(db_conn, llm=llm, profile_path=profile, state_path=state)
    propose_from_feedback(db_conn, llm=llm, profile_path=profile, state_path=state)
    assert len(x_calls) == 3, "已跳过的事件不应再调模型"


def test_dry_run_does_not_persist_counts(db_conn, tmp_path):
    profile, state = _setup(db_conn, tmp_path)
    for _ in range(3):
        propose_from_feedback(db_conn, llm=_bad_llm([]), profile_path=profile, state_path=state, dry_run=True)
    assert not state.exists()
