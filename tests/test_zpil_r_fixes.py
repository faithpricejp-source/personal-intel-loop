"""z-pil-R 审查修复：直接调用真函数复现，再钉住修后行为。"""
from __future__ import annotations

import json

import pytest

from personal_intel_loop import paper_inbox
from personal_intel_loop.profile import pending_proposals
from personal_intel_loop.profile_proposals import propose_from_feedback
from personal_intel_loop.schemas import FeedbackEvent, compute_feedback_event_id
from personal_intel_loop.store import record_feedback_event, upsert_item
from tests.conftest import make_item


def _seed_reason(db_conn, action: str, item_id: str, ts: str) -> None:
    upsert_item(
        db_conn,
        make_item(item_id=item_id, title=f"title {item_id}"),
        adapter_name="rss_briefing",
        source_payload_json="{}",
    )
    event_id = compute_feedback_event_id(origin="digest_checkbox", item_id=item_id, action=action, digest_stem="d")
    record_feedback_event(
        db_conn,
        FeedbackEvent(event_id=event_id, item_id=item_id, action=action, origin="digest_checkbox", event_ts=ts),
    )


def _proposal_json(*, change: str) -> str:
    return json.dumps({"no_change": False, "target": "新增于: 域饱和表", "change": change, "rationale": "具体特征"})


def test_n1_llm_failure_does_not_advance_watermark(db_conn, tmp_path):
    """第一条模型失败即停：水位线只停在最后一条成功事件，失败事件下轮仍能被捞回。"""
    profile = tmp_path / "reading_profile.md"
    profile.write_text("# p\n\n## 待接受的修订\n-\n", "utf-8")
    state = tmp_path / "state.json"
    _seed_reason(db_conn, "keep", "rss:n1a", "2026-10-07T01:00:00Z")
    _seed_reason(db_conn, "keep", "rss:n1b", "2026-10-07T02:00:00Z")
    _seed_reason(db_conn, "keep", "rss:n1c", "2026-10-07T03:00:00Z")

    def flaky(prompt: str) -> str | None:
        if "title rss:n1a" in prompt:
            return _proposal_json(change="记下 a")
        if "title rss:n1b" in prompt:
            return None
        if "title rss:n1c" in prompt:
            return _proposal_json(change="记下 c")
        return None

    propose_from_feedback(db_conn, llm=flaky, profile_path=profile, state_path=state)
    saved = json.loads(state.read_text("utf-8")) if state.exists() else {}
    assert saved.get("last_event_ts") == "2026-10-07T01:00:00Z"

    def ok_llm(prompt: str) -> str:
        return _proposal_json(change="重试成功")

    again = propose_from_feedback(db_conn, llm=ok_llm, profile_path=profile, state_path=state)
    lines = pending_proposals(profile)
    assert any("rss:n1b" in line for line in lines), again
    assert json.loads(state.read_text("utf-8"))["last_event_ts"] == "2026-10-07T03:00:00Z"


def test_n1_failure_sharing_ts_with_success_is_not_skipped(db_conn, tmp_path):
    """验收方补: 同 ts 两条事件一成一败时, 水位线不得停在该 ts(抓取用 >, 会越过失败那条)。"""
    profile = tmp_path / "reading_profile.md"
    profile.write_text("# p\n\n## 待接受的修订\n-\n", "utf-8")
    state = tmp_path / "state.json"
    _seed_reason(db_conn, "keep", "rss:t0", "2026-10-07T00:30:00Z")
    _seed_reason(db_conn, "keep", "rss:same-ok", "2026-10-07T01:00:00Z")
    _seed_reason(db_conn, "unclear", "rss:same-bad", "2026-10-07T01:00:00Z")

    def flaky(prompt: str) -> str | None:
        return None if "title rss:same-bad" in prompt else _proposal_json(change="ok")

    first = propose_from_feedback(db_conn, llm=flaky, profile_path=profile, state_path=state)
    assert any(s["reason"] == "llm_failed_or_invalid_json" for s in first["skipped"])
    saved = json.loads(state.read_text("utf-8"))
    assert saved["last_event_ts"] == "2026-10-07T00:30:00Z"

    again = propose_from_feedback(db_conn, llm=lambda p: _proposal_json(change="retry"), profile_path=profile, state_path=state)
    assert any("rss:same-bad" in line for line in pending_proposals(profile)), again
    # 已追加过的同 ts 成功事件走去重, 不重复提案
    assert sum(1 for line in pending_proposals(profile) if "rss:same-ok]" in line) == 1
    assert json.loads(state.read_text("utf-8"))["last_event_ts"] == "2026-10-07T01:00:00Z"


def test_n5_spool_without_dedup_key_is_not_reingested_after_done_rename_fails(db_conn, tmp_path, monkeypatch):
    """accept 已提交但移入 done/ 失败时，无 dedup_key 的同一文件重扫不得再插一行。"""
    spool = tmp_path / "spool"
    spool.mkdir()
    payload = {"source": "proj.notice", "title": "无键投递", "body": "b", "priority": "normal"}
    (spool / "a.json").write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    real_move = paper_inbox._move_unique
    calls = {"n": 0}

    def flaky(path, dst_dir):
        if dst_dir.name == "done":
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("done rename failed")
        return real_move(path, dst_dir)

    monkeypatch.setattr(paper_inbox, "_move_unique", flaky)
    with pytest.raises(OSError):
        paper_inbox.drain_spool(db_conn, spool, now_utc="2026-10-07T01:00:00Z")
    assert (spool / "a.json").is_file()

    paper_inbox.drain_spool(db_conn, spool, now_utc="2026-10-07T01:05:00Z")
    rows = db_conn.execute("SELECT dedup_key FROM inbox ORDER BY inbox_id").fetchall()
    assert len(rows) == 1
    assert rows[0]["dedup_key"] == "spool:a.json"


def test_n7_default_profile_path_follows_env_set_after_import(db_conn, tmp_path, monkeypatch):
    """import 时绑死的 PROFILE_PATH 不得盖过之后才设置的 PIL_PROFILE_DIR。"""
    from personal_intel_loop.digest import _profile_header_line
    from personal_intel_loop.profile import (
        PROFILE_PATH,
        append_proposal,
        decide_proposal,
        load_profile,
        pending_proposals,
        profile_body_for_prompt,
        profile_mtime_local,
    )
    from personal_intel_loop.summarizer import _profile_for_prompt
    from personal_intel_loop.web import handle_profile_save, handle_proposal

    new_dir = tmp_path / "later_profile"
    new_dir.mkdir()
    profile = new_dir / "reading_profile.md"
    pending_line = "[2026-10-07 · keep · rss:n7] 依据: 旧 → 目标: t → 建议: s"
    profile.write_text(
        f"# 阅读偏好\n\nN7_MARKER_BODY\n\n## 待接受的修订\n- {pending_line}\n\n## 已接受的修订\n-\n",
        "utf-8",
    )
    monkeypatch.setenv("PIL_PROFILE_DIR", str(new_dir))
    assert PROFILE_PATH.resolve() != profile.resolve()

    assert "N7_MARKER_BODY" in load_profile()
    assert "N7_MARKER_BODY" in profile_body_for_prompt()
    assert pending_proposals() == [pending_line]
    assert profile_mtime_local() is not None
    assert "N7_MARKER_BODY" in _profile_for_prompt()
    assert "待接受提案 1 条" in _profile_header_line()

    append_proposal("N7_NEW_LINE")
    assert "N7_NEW_LINE" in profile.read_text("utf-8")
    assert decide_proposal("N7_NEW_LINE", "accept") == 1
    assert "N7_NEW_LINE" not in pending_proposals()
    assert "N7_NEW_LINE" in profile.read_text("utf-8")
    append_proposal("N7_WEB_LINE")
    handle_proposal({"line": "N7_WEB_LINE", "decision": "accept"})
    assert "N7_WEB_LINE" not in pending_proposals()
    assert "N7_WEB_LINE" in profile.read_text("utf-8")
    saved = handle_profile_save(
        {"text": "# saved-via-default\n\n## 待接受的修订\n-\n"},
        runs_dir=tmp_path / "backups",
    )
    assert saved["ok"] is True
    assert profile.read_text("utf-8").startswith("# saved-via-default")
    assert "saved-via-default" in load_profile()

    _seed_reason(db_conn, "keep", "rss:n7evt", "2026-10-07T04:00:00Z")
    # save 清掉了待接受段；缺省 profile_path 仍应写回环境变量指向的文件
    propose_from_feedback(
        db_conn,
        llm=lambda prompt: _proposal_json(change="n7"),
        state_path=tmp_path / "n7-state.json",
    )
    assert "rss:n7evt" in profile.read_text("utf-8")
    assert "rss:n7evt" in load_profile()


def test_n9_distill_rerun_does_not_duplicate_proposal_after_mid_append_crash(db_conn, tmp_path, monkeypatch):
    """追加到一半崩溃后重跑，已写入的 dim+item_id 不得再追加一遍。"""
    from personal_intel_loop.paper_distill import distill
    from personal_intel_loop.profile import append_proposal, pending_proposals
    from tests.test_paper_distill import _add_rating

    profile = tmp_path / "reading_profile.md"
    profile.write_text("# 阅读偏好\n\n正文\n\n## 待接受的修订\n-\n", "utf-8")
    for index in range(3):
        _add_rating(db_conn, f"n9-{index}", "style", -1)
    raw = json.dumps(
        [
            {"item_id": "n9-0", "dim": "style", "basis": "第一条", "target": "文风", "suggestion": "少论战"},
            {"item_id": "n9-1", "dim": "style", "basis": "第二条", "target": "文风", "suggestion": "少数据堆砌"},
        ],
        ensure_ascii=False,
    )
    real_append = append_proposal
    calls = {"n": 0}

    def flaky(line, path=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_append(line, path)

    monkeypatch.setattr("personal_intel_loop.profile.append_proposal", flaky)
    with pytest.raises(OSError):
        distill(db_conn, llm_call=lambda prompt: raw, profile_path=profile, min_new=3)
    assert sum(1 for line in pending_proposals(profile) if "· style · n9-0]" in line) == 1
    assert all(row[0] is None for row in db_conn.execute("SELECT distilled_at FROM item_ratings"))

    monkeypatch.setattr("personal_intel_loop.profile.append_proposal", real_append)
    distill(db_conn, llm_call=lambda prompt: raw, profile_path=profile, min_new=3)
    pending = pending_proposals(profile)
    assert sum(1 for line in pending if "· style · n9-0]" in line) == 1
    assert sum(1 for line in pending if "· style · n9-1]" in line) == 1
    assert all(row[0] is not None for row in db_conn.execute("SELECT distilled_at FROM item_ratings"))


def test_m1_numeric_zhihu_id_not_excluded_by_weibo_follow(db_conn):
    """已关注微博的纯数字 uid 不得误杀同号的知乎候选；微博侧仍排除。"""
    from personal_intel_loop.paper_follow import collect_candidates

    shared = "1234567890"
    upsert_item(
        db_conn,
        make_item(item_id="item:wb-home", source=f"weibo_home:{shared}", url="https://weibo.example/u", title="微博关注流"),
        adapter_name="weibo_home",
        source_payload_json="{}",
    )
    upsert_item(
        db_conn,
        make_item(item_id="item:wb-mention", source="rss_briefing:x", url="https://example.com/m", title="提到微博"),
        adapter_name="rss_briefing",
        source_payload_json=json.dumps({"mentioned_accounts": [{"id": shared, "name": "微博甲", "platform": "weibo"}]}),
    )
    upsert_item(
        db_conn,
        make_item(
            item_id="item:zh",
            source=f"zhihu_moments:{shared}",
            url=f"https://www.zhihu.com/people/{shared}",
            title="知乎候选",
        ),
        adapter_name="zhihu_moments",
        source_payload_json=json.dumps(
            {"author_key": shared, "author_name": "纯数字知乎", "endorsed_by": ["别人赞同了回答"]},
            ensure_ascii=False,
        ),
    )
    db_conn.commit()
    candidates = collect_candidates(db_conn, now_utc="2026-10-07T02:00:00Z")
    zhihu = [c for c in candidates if c["platform"] == "zhihu" and c["account_id"] == shared]
    weibo = [c for c in candidates if c["platform"] == "weibo" and c["account_id"] == shared]
    assert len(zhihu) == 1
    assert weibo == []


def test_n5_non_string_dedup_key_still_goes_to_bad(db_conn, tmp_path):
    """验收方补: 兜底 key 只补缺省/空串; dedup_key 类型错的 spool 文件仍按契约进 bad/。"""
    spool = tmp_path / "spool"
    spool.mkdir()
    payload = {"source": "proj.notice", "title": "坏键", "body": "b", "dedup_key": 123}
    (spool / "bad.json").write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    result = paper_inbox.drain_spool(db_conn, spool, now_utc="2026-10-07T01:00:00Z")
    assert result["bad"] == 1 and result["accepted"] == 0
    assert (spool / "bad" / "bad.json").is_file()
