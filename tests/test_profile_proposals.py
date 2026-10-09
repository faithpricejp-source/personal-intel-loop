"""反馈 → profile 提案。LLM 用假函数替代。"""
from __future__ import annotations

import json

from personal_intel_loop.profile import pending_proposals
from personal_intel_loop.profile_proposals import fetch_reason_events, propose_from_feedback
from personal_intel_loop.schemas import FeedbackEvent, compute_feedback_event_id
from personal_intel_loop.store import record_feedback_event, upsert_item
from tests.conftest import make_item


def _seed(db_conn, action: str, item_id: str, ts: str, origin: str = "digest_checkbox"):
    upsert_item(db_conn, make_item(item_id=item_id, title=f"title {item_id}"), adapter_name="rss_briefing", source_payload_json="{}")
    if origin == "cli":
        event_id = compute_feedback_event_id(origin="cli", item_id=item_id, action=action, event_ts_utc_iso=ts)
    else:
        event_id = compute_feedback_event_id(origin="digest_checkbox", item_id=item_id, action=action, digest_stem="d")
    ev = FeedbackEvent(event_id=event_id, item_id=item_id, action=action, origin=origin, event_ts=ts)
    record_feedback_event(db_conn, ev)


def test_cli_origin_reason_codes_are_included(db_conn):
    _seed(db_conn, "already_known", "rss:cli1", "2026-08-26T05:00:00Z", origin="cli")
    _seed(db_conn, "keep", "rss:vd", "2026-08-26T06:00:00Z", origin="vault_detection")  # 非人工来源不算
    assert [e["item_id"] for e in fetch_reason_events(db_conn, since_ts=None, limit=10)] == ["rss:cli1"]


def test_fetch_only_reason_codes_after_watermark(db_conn):
    _seed(db_conn, "keep", "rss:a", "2026-08-26T01:00:00Z")
    _seed(db_conn, "less_like_this", "rss:b", "2026-08-26T02:00:00Z")  # legacy, 不算
    _seed(db_conn, "unclear", "rss:c", "2026-08-26T03:00:00Z")
    got = fetch_reason_events(db_conn, since_ts=None, limit=10)
    assert [e["item_id"] for e in got] == ["rss:a", "rss:c"]
    got2 = fetch_reason_events(db_conn, since_ts="2026-08-26T01:00:00Z", limit=10)
    assert [e["item_id"] for e in got2] == ["rss:c"]


def test_proposals_appended_and_watermark_advances(db_conn, tmp_path):
    profile = tmp_path / "reading_profile.md"
    profile.write_text("# profile\n\n## 域饱和表\n| AI | 饱和 |\n\n## 待接受的修订\n-\n", "utf-8")
    state = tmp_path / "state.json"
    _seed(db_conn, "keep", "rss:a", "2026-08-26T01:00:00Z")
    _seed(db_conn, "already_known", "rss:b", "2026-08-26T02:00:00Z")

    def fake_llm(prompt: str) -> str:
        assert "## 当前 profile" in prompt and "| AI | 饱和 |" in prompt
        if "title rss:a" in prompt:
            return json.dumps({"no_change": False, "target": "| AI | 饱和 |", "change": "加例外: 含成本数字的机制推演", "rationale": "含 $25M 蒸馏成本"})
        return json.dumps({"no_change": True, "rationale": "饱和表已覆盖"})

    r = propose_from_feedback(db_conn, llm=fake_llm, profile_path=profile, state_path=state)
    assert r["events"] == 2 and len(r["proposals"]) == 1 and len(r["skipped"]) == 1
    lines = pending_proposals(profile)
    assert len(lines) == 1 and "keep · rss:a" in lines[0] and "加例外" in lines[0]
    assert json.loads(state.read_text())["last_event_ts"] == "2026-08-26T02:00:00Z"
    # 第二次运行: 水位之后没有新事件
    r2 = propose_from_feedback(db_conn, llm=fake_llm, profile_path=profile, state_path=state)
    assert r2["events"] == 0 and pending_proposals(profile) == lines


def test_dry_run_writes_nothing(db_conn, tmp_path):
    profile = tmp_path / "reading_profile.md"; profile.write_text("# p\n", "utf-8")
    state = tmp_path / "state.json"
    _seed(db_conn, "deep_discuss", "rss:z", "2026-08-26T01:00:00Z")
    r = propose_from_feedback(db_conn, dry_run=True, profile_path=profile, state_path=state,
                              llm=lambda p: json.dumps({"target": "新增于: 域饱和表", "change": "x", "rationale": "y"}))
    assert len(r["proposals"]) == 1
    assert pending_proposals(profile) == [] and not state.exists()


def test_llm_failure_is_skipped_not_fatal(db_conn, tmp_path):
    profile = tmp_path / "p.md"; profile.write_text("# p\n", "utf-8")
    _seed(db_conn, "keep", "rss:q", "2026-08-26T01:00:00Z")
    r = propose_from_feedback(db_conn, profile_path=profile, state_path=tmp_path / "s.json", llm=lambda p: None)
    assert r["proposals"] == [] and r["skipped"][0]["reason"] == "llm_failed_or_invalid_json"


def test_already_proposed_event_is_skipped(db_conn, tmp_path):
    profile = tmp_path / "reading_profile.md"
    profile.write_text("# p\n\n## 待接受的修订\n- [2026-08-26 · keep · rss:a] 依据: x → 目标: y → 建议: z\n", "utf-8")
    state = tmp_path / "state.json"
    _seed(db_conn, "keep", "rss:a", "2026-08-26T01:00:00Z")
    calls = []
    r = propose_from_feedback(db_conn, profile_path=profile, state_path=state,
                              llm=lambda p: (calls.append(1), json.dumps({"target": "t", "change": "c", "rationale": "r"}))[1])
    assert calls == []  # 没调模型
    assert r["skipped"][0]["reason"] == "already_proposed" and r["proposals"] == []
    assert len(pending_proposals(profile)) == 1
    assert json.loads(state.read_text())["last_event_ts"] == "2026-08-26T01:00:00Z"  # 水位仍推进
