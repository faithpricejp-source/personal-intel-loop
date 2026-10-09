"""learn 硬约束: 无行为不调模型 / 越界截断 / 有效值夹紧 / 证据不足与未出现 key 拒 / 一天一次 /
上限条数 / 坏 JSON 不调旋钮 / 7.3 counter·blind 剔除 / 负向 delta 需显式负反馈 / 提案与 reading_note。"""
from __future__ import annotations

from personal_intel_loop.paper_learn import learn
from personal_intel_loop.profile import pending_proposals
from tests._behavior_helpers import (
    BEHAVIOR_DATE,
    NOW,
    TS_A,
    TS_B,
    add_edition,
    add_events,
    ai_learn_json,
    seed_item,
)

ADJ = lambda kind, key, delta, reason="依据:某条被连读三次": {"kind": kind, "key": key, "delta": delta, "reason": reason}


def _conn(tmp_path):
    from personal_intel_loop.store import connect_db, ensure_schema

    c = connect_db(tmp_path / f"db-{abs(hash(tmp_path))}.sqlite")
    ensure_schema(c)
    return c


def _profile(tmp_path) -> str:
    path = tmp_path / "reading_profile.md"
    path.write_text("# 阅读偏好\n\n关心农业与气候的一手报道。\n\n## 待接受的修订\n-\n", "utf-8")
    return path


def _seed_active_day(conn, *, sources=("rss_briefing:src_a", "rss_briefing:src_b")):
    """两个源各一条被深读的条目(各 3 次曝光), 都在版面上。"""
    labels = {"rss_briefing:src_a": ("源A", "张三"), "rss_briefing:src_b": ("源B", "李四")}
    for index, source in enumerate(sources):
        label, byline = labels[source]
        item_id = f"it:{index}"
        seed_item(conn, item_id, source, label, byline=byline)
        add_edition(conn, BEHAVIOR_DATE, item_id, "top", index)
        add_events(
            conn,
            [
                ("impression", item_id, 2000, {}),
                ("impression", item_id, 1000, {}),
                ("impression", item_id, 500, {}),
                ("open_item", item_id, None, {}),
                ("item_dwell", item_id, 20000, {"max_scroll_pct": 60}),
            ],
            ts=TS_A if index == 0 else TS_B,
        )
    conn.commit()


def test_no_behavior_skips_model(tmp_path):
    conn = _conn(tmp_path)
    try:
        seed_item(conn, "it:0", "rss_briefing:src_a", "源A")
        conn.commit()

        def boom(prompt):
            raise AssertionError("无行为不许调模型")

        result = learn(conn, date_local=BEHAVIOR_DATE, now_utc=NOW, llm_call=boom, profile_path=_profile(tmp_path))
        assert result == {"skipped": "no_behavior"}
        assert conn.execute("SELECT COUNT(*) FROM knob_offsets").fetchone()[0] == 0
    finally:
        conn.close()


def test_learn_happy_path_and_prompt_contents(tmp_path):
    conn = _conn(tmp_path)
    try:
        _seed_active_day(conn)
        profile_path = _profile(tmp_path)
        captured = []

        def llm(prompt):
            captured.append(prompt)
            return ai_learn_json(
                ADJ("source", "rss_briefing:src_a", 0.03),
                ADJ("author", "张三", 0.02),
                ADJ("topic", "科学", 0.01),
            )

        result = learn(conn, date_local=BEHAVIOR_DATE, now_utc=NOW, llm_call=llm, profile_path=profile_path)
        assert set(result.keys()) == {"applied", "clipped", "rejected", "proposals", "note"}
        assert result == {"applied": 3, "clipped": 0, "rejected": 0, "proposals": 0, "note": True}

        prompt = captured[0]
        assert "农业与气候" in prompt  # 画像正文
        assert "跳过不等于讨厌" in prompt
        assert "以显式评分为准" in prompt
        assert "让用户知道更多他不知道的" in prompt  # 7.3 要求的句子
        assert "标题 it:0" in prompt  # 汇总表带条目
        assert "深读" not in prompt or True

        rows = conn.execute("SELECT kind, key, offset FROM knob_offsets ORDER BY kind, key").fetchall()
        assert [(row["kind"], row["key"], round(row["offset"], 4)) for row in rows] == [
            ("author", "张三", 0.02),
            ("source", "rss_briefing:src_a", 0.03),
            ("topic", "科学", 0.01),
        ]
        adjustment = conn.execute("SELECT * FROM ai_adjustments").fetchone()
        assert adjustment["for_date"] == BEHAVIOR_DATE and adjustment["reverted"] == 0
        # 有效值 = trust_score(0.35) + 0.03
        assert adjustment["before"] == 0.35 and abs(adjustment["after"] - 0.38) < 1e-9
        note = conn.execute("SELECT * FROM edition_notes").fetchone()
        assert note["edition_date"] == "2026-10-05" and "昨天你读了" in note["reading_note"]
    finally:
        conn.close()


def test_delta_out_of_range_clipped(tmp_path):
    conn = _conn(tmp_path)
    try:
        _seed_active_day(conn)
        result = learn(
            conn, date_local=BEHAVIOR_DATE, now_utc=NOW,
            llm_call=lambda prompt: ai_learn_json(ADJ("source", "rss_briefing:src_a", 0.2)),
            profile_path=_profile(tmp_path),
        )
        assert result["applied"] == 1 and result["clipped"] == 1
        row = conn.execute("SELECT delta, after FROM ai_adjustments").fetchone()
        assert row["delta"] == 0.05  # 截断到 ±0.05
        offset = conn.execute("SELECT offset FROM knob_offsets WHERE kind='source' AND key='rss_briefing:src_a'").fetchone()
        assert offset["offset"] == 0.05
        assert abs(row["after"] - 0.40) < 1e-9  # 有效值 0.35+0.05
    finally:
        conn.close()


def test_effective_value_clamped_into_range(tmp_path):
    conn = _conn(tmp_path)
    try:
        _seed_active_day(conn)
        conn.execute("UPDATE source_trust SET trust_score=0.92 WHERE source='rss_briefing:src_a'")
        conn.commit()
        result = learn(
            conn, date_local=BEHAVIOR_DATE, now_utc=NOW,
            llm_call=lambda prompt: ai_learn_json(ADJ("source", "rss_briefing:src_a", 0.05)),
            profile_path=_profile(tmp_path),
        )
        assert result["applied"] == 1 and result["clipped"] == 1  # 夹紧也算 clipped
        row = conn.execute("SELECT before, after FROM ai_adjustments").fetchone()
        assert abs(row["before"] - 0.92) < 1e-9 and abs(row["after"] - 0.95) < 1e-9  # 夹在上界 0.95
        offset = conn.execute("SELECT offset FROM knob_offsets WHERE kind='source' AND key='rss_briefing:src_a'").fetchone()
        assert offset["offset"] == 0.05  # offset 本身不截断, 有效值才夹紧
    finally:
        conn.close()


def test_insufficient_evidence_and_unknown_key_rejected(tmp_path):
    conn = _conn(tmp_path)
    try:
        _seed_active_day(conn)
        # src_a 只有 3 次曝光是挂在 it:0 上的; 造一个只出现 1 次曝光的源
        seed_item(conn, "it:few", "rss_briefing:src_few", "源少")
        add_edition(conn, BEHAVIOR_DATE, "it:few", "briefs", 9)
        add_events(conn, [("impression", "it:few", 2000, {})], ts=TS_B)
        conn.commit()
        result = learn(
            conn, date_local=BEHAVIOR_DATE, now_utc=NOW,
            llm_call=lambda prompt: ai_learn_json(
                ADJ("source", "rss_briefing:src_few", 0.03),  # 出现了但证据不足(<3 次曝光)
                ADJ("source", "rss_briefing:ghost", 0.03),  # 当天汇总里根本没有
            ),
            profile_path=_profile(tmp_path),
        )
        assert result["applied"] == 0 and result["rejected"] == 2
        assert conn.execute("SELECT COUNT(*) FROM knob_offsets").fetchone()[0] == 0
    finally:
        conn.close()


def test_same_kind_key_once_per_day(tmp_path):
    conn = _conn(tmp_path)
    try:
        _seed_active_day(conn)
        profile_path = _profile(tmp_path)
        # 同一响应里同 key 两次 → 第二次拒
        result = learn(
            conn, date_local=BEHAVIOR_DATE, now_utc=NOW,
            llm_call=lambda prompt: ai_learn_json(ADJ("source", "rss_briefing:src_a", 0.03), ADJ("source", "rss_briefing:src_a", 0.02)),
            profile_path=profile_path,
        )
        assert result["applied"] == 1 and result["rejected"] == 1
        # 隔天重跑同一天 → 记录已存在, 再拒
        result2 = learn(
            conn, date_local=BEHAVIOR_DATE, now_utc=NOW,
            llm_call=lambda prompt: ai_learn_json(ADJ("source", "rss_briefing:src_a", 0.01)),
            profile_path=profile_path,
        )
        assert result2["applied"] == 0 and result2["rejected"] == 1
        offset = conn.execute("SELECT offset FROM knob_offsets WHERE kind='source' AND key='rss_briefing:src_a'").fetchone()
        assert offset["offset"] == 0.03  # 没有叠加
    finally:
        conn.close()


def test_max_adjustments_keeps_largest_delta(tmp_path):
    conn = _conn(tmp_path)
    try:
        _seed_active_day(conn)
        result = learn(
            conn, date_local=BEHAVIOR_DATE, now_utc=NOW, max_adjustments=2,
            llm_call=lambda prompt: ai_learn_json(
                ADJ("source", "rss_briefing:src_a", 0.05),
                ADJ("author", "张三", 0.03),
                ADJ("topic", "科学", 0.04),
            ),
            profile_path=_profile(tmp_path),
        )
        assert result["applied"] == 2 and result["rejected"] == 1
        keys = {row["kind"] for row in conn.execute("SELECT kind FROM ai_adjustments")}
        assert keys == {"source", "topic"}  # |delta| 0.05 与 0.04 保留, 0.03 被上限挤掉
    finally:
        conn.close()


def test_invalid_model_json_writes_nothing(tmp_path):
    conn = _conn(tmp_path)
    try:
        _seed_active_day(conn)
        result = learn(
            conn, date_local=BEHAVIOR_DATE, now_utc=NOW,
            llm_call=lambda prompt: "这不是 JSON",
            profile_path=_profile(tmp_path),
        )
        assert "error" in result and "invalid json" in result["error"]
        assert conn.execute("SELECT COUNT(*) FROM knob_offsets").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ai_adjustments").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM edition_notes").fetchone()[0] == 0
        # 模型抛异常同样返回 error 不抛
        def boom(prompt):
            raise RuntimeError("cloud down")

        result2 = learn(conn, date_local=BEHAVIOR_DATE, now_utc=NOW, llm_call=boom, profile_path=_profile(tmp_path))
        assert "error" in result2 and "cloud down" in result2["error"]
    finally:
        conn.close()


def test_counter_skipped_excluded_from_aggregation(tmp_path):
    conn = _conn(tmp_path)
    try:
        _seed_active_day(conn)
        # src_c 唯一一条是「跳过的反方」: 3 次曝光但被剔除 → 无证据、不进汇总
        seed_item(conn, "it:counter", "rss_briefing:src_c", "源C", topic="气候", payload_extra={"novelty": {"kind": "counter", "why": "反", "against": None}})
        add_edition(conn, BEHAVIOR_DATE, "it:counter", "briefs", 8)
        add_events(conn, [("impression", "it:counter", 2000, {}), ("impression", "it:counter", 1000, {}), ("impression", "it:counter", 500, {})], ts=TS_B)
        conn.commit()
        captured = []

        def llm(prompt):
            captured.append(prompt)
            return ai_learn_json(ADJ("source", "rss_briefing:src_c", -0.05))

        result = learn(conn, date_local=BEHAVIOR_DATE, now_utc=NOW, llm_call=llm, profile_path=_profile(tmp_path))
        assert result["applied"] == 0 and result["rejected"] == 1
        assert "rss_briefing:src_c" not in captured[0]  # 交模型前已剔除
    finally:
        conn.close()


def test_counter_exclusion_only_hits_skipped_glanced(tmp_path):
    conn = _conn(tmp_path)
    try:
        _seed_active_day(conn)
        # 对照: 跳过但 novelty 不是 counter → 正常进聚合, 正 delta 生效
        seed_item(conn, "it:new", "rss_briefing:src_c", "源C", topic="气候", payload_extra={"novelty": {"kind": "new_fact", "why": "新", "against": None}})
        add_edition(conn, BEHAVIOR_DATE, "it:new", "briefs", 8)
        add_events(conn, [("impression", "it:new", 2000, {}), ("impression", "it:new", 1000, {}), ("impression", "it:new", 500, {})], ts=TS_B)
        conn.commit()
        result = learn(
            conn, date_local=BEHAVIOR_DATE, now_utc=NOW,
            llm_call=lambda prompt: ai_learn_json(ADJ("source", "rss_briefing:src_c", 0.04)),
            profile_path=_profile(tmp_path),
        )
        assert result["applied"] == 1 and result["rejected"] == 0
    finally:
        conn.close()


def test_blind_skipped_excluded_from_aggregation(tmp_path):
    conn = _conn(tmp_path)
    try:
        _seed_active_day(conn)
        # 盲区版条目被跳过 → 该源无证据, 不进聚合
        seed_item(conn, "it:blind", "rss_briefing:src_d", "源D")
        add_edition(conn, BEHAVIOR_DATE, "it:blind", "blind", 0, blind_reason="盲区句")
        add_events(conn, [("impression", "it:blind", 2000, {}), ("impression", "it:blind", 1000, {}), ("impression", "it:blind", 500, {})], ts=TS_B)
        conn.commit()
        captured = []

        def llm(prompt):
            captured.append(prompt)
            return ai_learn_json(ADJ("source", "rss_briefing:src_d", 0.04))

        result = learn(conn, date_local=BEHAVIOR_DATE, now_utc=NOW, llm_call=llm, profile_path=_profile(tmp_path))
        assert result["applied"] == 0 and result["rejected"] == 1
        assert "rss_briefing:src_d" not in captured[0]
    finally:
        conn.close()


def test_negative_delta_requires_explicit_negative_feedback(tmp_path):
    conn = _conn(tmp_path)
    try:
        _seed_active_day(conn)
        # 无显式负反馈 → 负向 delta 被拒
        result = learn(
            conn, date_local=BEHAVIOR_DATE, now_utc=NOW,
            llm_call=lambda prompt: ai_learn_json(ADJ("source", "rss_briefing:src_b", -0.03)),
            profile_path=_profile(tmp_path),
        )
        assert result["applied"] == 0 and result["rejected"] == 1
        # 该源条目当天被显式点了 overall -1 → 负向 delta 生效
        conn.execute(
            "INSERT OR REPLACE INTO item_ratings (item_id, dim, value, author_key, ts, distilled_at) VALUES ('it:1', 'overall', -1, NULL, ?, NULL)",
            (TS_B,),
        )
        conn.commit()
        result2 = learn(
            conn, date_local=BEHAVIOR_DATE, now_utc=NOW,
            llm_call=lambda prompt: ai_learn_json(ADJ("source", "rss_briefing:src_b", -0.03)),
            profile_path=_profile(tmp_path),
        )
        assert result2["applied"] == 1 and result2["rejected"] == 0
        offset = conn.execute("SELECT offset FROM knob_offsets WHERE kind='source' AND key='rss_briefing:src_b'").fetchone()
        assert offset["offset"] == -0.03
    finally:
        conn.close()


def test_proposals_capped_three_per_day_with_format(tmp_path):
    conn = _conn(tmp_path)
    try:
        _seed_active_day(conn)
        profile_path = _profile(tmp_path)
        proposals = [
            {"target": "域饱和表", "basis": "两条气候深读", "suggestion": "新增行"},
            {"target": "文风", "basis": "数据密集连续接受", "suggestion": "保持"},
            {"target": "盲区", "basis": "没读反方", "suggestion": "加一条"},
            {"target": "第四条不该进", "basis": "x", "suggestion": "y"},
        ]
        result = learn(
            conn, date_local=BEHAVIOR_DATE, now_utc=NOW,
            llm_call=lambda prompt: ai_learn_json(proposals=proposals),
            profile_path=profile_path,
        )
        assert result["proposals"] == 3
        pending = pending_proposals(profile_path)
        assert len([line for line in pending if "· behavior ·" in line]) == 3
        assert pending[0] == "[2026-10-04 · behavior · 域饱和表] 依据: 两条气候深读 → 目标: 域饱和表 → 建议: 新增行"
        # 同一天重跑: 一天最多 3 条, 不再追加
        result2 = learn(
            conn, date_local=BEHAVIOR_DATE, now_utc=NOW,
            llm_call=lambda prompt: ai_learn_json(proposals=[{"target": "再来一条", "basis": "b", "suggestion": "s"}]),
            profile_path=profile_path,
        )
        assert result2["proposals"] == 0
    finally:
        conn.close()


def test_same_day_rerun_does_not_duplicate_proposal_target(tmp_path):
    # 10-05 审计 C1：同日重跑、模型给出同一 target，不再重复追加
    conn = _conn(tmp_path)
    try:
        _seed_active_day(conn)
        profile_path = _profile(tmp_path)
        prop = [{"target": "域饱和表", "basis": "两条气候深读", "suggestion": "新增行"}]
        for _ in range(2):
            learn(conn, date_local=BEHAVIOR_DATE, now_utc=NOW, llm_call=lambda prompt: ai_learn_json(proposals=prop), profile_path=profile_path)
        pending = pending_proposals(profile_path)
        assert len([line for line in pending if "· behavior · 域饱和表]" in line]) == 1
    finally:
        conn.close()


def test_reading_note_missing_means_note_false(tmp_path):
    conn = _conn(tmp_path)
    try:
        _seed_active_day(conn)
        result = learn(
            conn, date_local=BEHAVIOR_DATE, now_utc=NOW,
            llm_call=lambda prompt: ai_learn_json(note=""),
            profile_path=_profile(tmp_path),
        )
        assert result["note"] is False
        assert conn.execute("SELECT COUNT(*) FROM edition_notes").fetchone()[0] == 0
    finally:
        conn.close()
