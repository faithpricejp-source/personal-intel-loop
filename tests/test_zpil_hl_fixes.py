"""Kimi 只读审查发现的复核与回归测试（每条直接调用真文件里的真函数）。"""
from __future__ import annotations

import os

import pytest

from personal_intel_loop.paper_api import get_archive, notifications
from personal_intel_loop.store import upsert_item
from tests.conftest import make_item


def _seed_archive(conn):
    for item_id, ts in [
        ("item:00", "2026-10-04T00:30:00Z"),
        ("item:01", "2026-10-04T01:30:00Z"),
    ]:
        upsert_item(
            conn,
            make_item(item_id=item_id, ts=ts),
            adapter_name="rss_briefing",
            source_payload_json='{"feed_name": "测试源"}',
        )
    conn.commit()


def test_archive_cursor_non_z_iso_normalized(db_conn):
    """"|" 游标的时间戳过 fromisoformat 校验后必须归一到 Z 格式再进字符串比较。

    客户端把游标重组成合法 ISO 但非 Z 写法（空格分隔 + 显式 +00:00）时，
    原实现拿原样字符串与库内 …T…Z 比较：空格(0x20) < 'T'(0x54)，
    当天所有条目都不满足 i.ts < ?，翻页静默漏条。
    """
    _seed_archive(db_conn)
    # 与 "2026-10-04T01:30:00Z|item:01" 同一时刻，fromisoformat 校验应通过
    cursor = "2026-10-04 01:30:00+00:00|item:01"
    result = get_archive(db_conn, before=cursor)
    ids = [entry["item_id"] for entry in result["items"]]
    assert ids == ["item:00"], f"非 Z 写法的合法游标应正确翻页，实际返回 {ids}"


def _insert_inbox_row(conn, inbox_id: int, created_at: str):
    conn.execute(
        "INSERT INTO inbox (inbox_id, source, title, body, url, priority, dedup_key, created_at, read_at)"
        " VALUES (?, 'risk', '紧急提醒', '正文', NULL, 'urgent', ?, ?, NULL)",
        (inbox_id, f"dedup-{inbox_id}", created_at),
    )


def test_notifications_naive_inbox_created_at_no_crash(db_conn):
    """inbox.created_at 出现无时区历史行时 notifications 不得在 sort 处 TypeError。

    同函数里 editions.built_at 与 auth_status.since_failing 的解析都有
    `if dt.tzinfo is None: replace(tzinfo=utc)` 兜底，唯独 inbox 这条没有；
    sort 混合比较 naive/aware datetime 会抛 TypeError，通知接口整体 500。
    """
    _insert_inbox_row(db_conn, 1, "2026-10-06T12:00:00")  # 模拟无时区后缀的脏历史行
    _insert_inbox_row(db_conn, 2, "2026-10-06T13:00:00Z")
    db_conn.commit()
    result = notifications(db_conn)
    ids = [n["id"] for n in result["notifications"]]
    assert ids == ["inbox:1", "inbox:2"], f"应按发生时间升序返回，实际 {ids}"


def _insert_urgent(conn, dedup: str, title: str, body: str, created_at: str):
    conn.execute(
        "INSERT INTO inbox (source, title, body, url, priority, dedup_key, created_at, read_at)"
        " VALUES ('risk', ?, ?, NULL, 'urgent', ?, ?, NULL)",
        (title, body, dedup, created_at),
    )


def test_today_urgent_related_escapes_like_wildcards(db_conn):
    """行程地名含 % / _ 时不得让 LIKE 通配符全匹配、误推紧急提醒。

    place 是 create_trip 里的自由用户输入；_today_urgent_related 用 f"%{place}%"
    直接进 LIKE（无正则复核、无别名表），place="100%" 会匹配一切含 100 的标题，
    place="_" 时 "%_%" 匹配所有非空串——当天任意 urgent 都被判「相关」。
    """
    from datetime import date

    from personal_intel_loop.paper_risk import _today_urgent_related

    # 东京本地日 2026-10-06 = UTC [2026-10-05T15:00:00Z, 2026-10-06T15:00:00Z)
    _insert_urgent(db_conn, "u1", "名古屋の警報", "普通正文", "2026-10-05T16:00:00Z")
    _insert_urgent(db_conn, "u2", "紧急通知 100 件", "正文", "2026-10-05T17:00:00Z")
    _insert_urgent(db_conn, "u3", "大阪の警報", "正文", "2026-10-05T18:00:00Z")
    db_conn.commit()
    day = date(2026, 10, 6)

    assert _today_urgent_related(db_conn, "100%", date_local=day) == [], "含 % 的地名不得按通配符解释"
    assert _today_urgent_related(db_conn, "_", date_local=day) == [], "含 _ 的地名不得匹配所有投递"
    hits = [row["title"] for row in _today_urgent_related(db_conn, "大阪", date_local=day)]
    assert hits == ["大阪の警報"], f"正常地名词面匹配必须保留，实际 {hits}"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root 无视文件权限位, chmod 造不出读/写失败")
def test_learn_proposal_write_failure_no_partial_adjustments(tmp_path):
    """画像提案写失败时不得留下已提交的旋钮调整（半成品）。

    修复前顺序是先 _apply_adjustments（SQLite 事务提交）再 _append_proposals（画像文件写）；
    画像文件只读时后者抛 PermissionError，被 learn 外层 except 吞成 {"error"}——
    调整已落库而提案丢失，同日重跑又被 ai_adjustments 判重挡住，当日提案永久丢失。
    """
    from personal_intel_loop.paper_learn import learn
    from personal_intel_loop.store import connect_db, ensure_schema
    from tests._behavior_helpers import (
        BEHAVIOR_DATE,
        NOW,
        add_edition,
        add_events,
        ai_learn_json,
        seed_item,
    )

    conn = connect_db(tmp_path / "db-h5.sqlite")
    ensure_schema(conn)
    try:
        seed_item(conn, "it:0", "rss_briefing:src_a", "源A", byline="张三")
        add_edition(conn, BEHAVIOR_DATE, "it:0", "top", 0)
        add_events(
            conn,
            [
                ("impression", "it:0", 2000, {}),
                ("impression", "it:0", 1000, {}),
                ("impression", "it:0", 500, {}),
            ],
        )
        conn.commit()

        profile_path = tmp_path / "reading_profile.md"
        profile_path.write_text("# 阅读偏好\n\n关心农业。\n\n## 待接受的修订\n-\n", "utf-8")
        profile_path.chmod(0o444)  # 可读（_prepare/pending_proposals 要读）不可写（append_proposal 抛）

        model_json = ai_learn_json(
            {"kind": "source", "key": "rss_briefing:src_a", "delta": 0.03, "reason": "依据:连读三次"},
            proposals=[{"target": "农业", "basis": "批注提及", "suggestion": "加权农业"}],
        )
        result = learn(conn, date_local=BEHAVIOR_DATE, now_utc=NOW, llm_call=lambda p: model_json, profile_path=profile_path)
        assert "error" in result, f"画像不可写应报 error，实际 {result}"
        assert conn.execute("SELECT COUNT(*) FROM ai_adjustments").fetchone()[0] == 0, "提案写失败时调整不得已提交"
        assert conn.execute("SELECT COUNT(*) FROM knob_offsets").fetchone()[0] == 0, "提案写失败时旋钮偏移不得已提交"

        # 画像恢复可写后同日重跑：同一次模型输出下调整与提案都应完整落盘（无永久丢失）
        profile_path.chmod(0o644)
        recovered = learn(conn, date_local=BEHAVIOR_DATE, now_utc=NOW, llm_call=lambda p: model_json, profile_path=profile_path)
        assert recovered.get("applied") == 1 and recovered.get("proposals") == 1, f"恢复后应完整重放，实际 {recovered}"
    finally:
        conn.close()


def test_preprocess_mechanical_missing_marked_for_retry(db_conn, monkeypatch):
    """机械段两连败+判定段成功时，半成品须记 error 供重试，不得当成功落库。

    修复前 _call_item_preprocess 返回 model="-+<judg_model>"，不以 "+-" 结尾 →
    error=None，该行以「成功」落库；preprocess 开头 WHERE error IS NULL 的跳过
    检查之后永远跳过它，lede/one_liner 等机械字段永久为空仍上版、无重试无告警。
    判定段失败的对称情况已有 "judgment missing" 处理（B2），机械段是被漏写的一侧。
    """
    import json as _json

    import personal_intel_loop.paper_ai as paper_ai
    from personal_intel_loop.paper_ai import preprocess
    from personal_intel_loop.store import upsert_item
    from tests.conftest import make_item

    upsert_item(
        db_conn,
        make_item(item_id="m1", title="标题 m1", body="条目自带正文"),
        adapter_name="rss_briefing",
        source_payload_json="{}",
    )
    db_conn.commit()

    judg_json = _json.dumps(
        {
            "profile_hit": "命中句",
            "lane": "material",
            "novelty": {"kind": "new_fact", "why": "新数字", "against": None},
            "lane_tags": [],
            "verification": None,
            "opportunity": None,
        },
        ensure_ascii=False,
    )

    def fake_primary(prompt, *, thinking=False):
        if thinking:  # 判定段（DeepSeek 开思考）成功
            return judg_json, "deepseek-flash-think"
        return None, None  # 机械段（关思考）失败

    def fake_fallback(prompt):
        return None, None  # 机械段回落 OpenRouter 也失败

    monkeypatch.setattr(paper_ai, "_call_primary", fake_primary)
    monkeypatch.setattr(paper_ai, "_call_fallback_high", fake_fallback)

    processed = preprocess(db_conn, ["m1"])
    assert processed == 1
    row = db_conn.execute("SELECT payload_json, error, model FROM item_ai WHERE item_id='m1'").fetchone()
    assert row["error"] == "mechanical missing", f"机械段缺失须记 error 供重试，实际 error={row['error']!r} model={row['model']!r}"

    # 重跑须重试该行（修复前 error IS NULL 的行被永久跳过）
    assert preprocess(db_conn, ["m1"]) == 1, "机械段缺失的半成品行下次必须重试"


def test_zero_streak_concurrent_update_no_lost_update(tmp_path, monkeypatch):
    """多进程并发更新零条连击状态文件不得丢更新。

    R03 注释自己写明「多个 launchd 任务会同时更新这张表」；原子替换只防半截文件，
    不防读-改-写交错。测试强制复现交错：线程 A 读到旧快照后挂起，线程 B 完成整轮
    读-改-写，然后 A 基于旧快照写回——修复前 B 的更新被整体覆盖丢失。
    """
    import pathlib
    import threading

    from personal_intel_loop import cli

    state_path = tmp_path / "streaks.json"
    state_path.write_text("{}\n", "utf-8")  # 播种合法状态, 保证两线程的 read_text 都成功返回
    orig_read_text = pathlib.Path.read_text
    a_read_started = threading.Event()
    b_done = threading.Event()

    def slow_read_text(self, *args, **kwargs):
        text = orig_read_text(self, *args, **kwargs)
        if self == state_path and not a_read_started.is_set():
            # 只挂起第一处（线程 A 的）读：等 B 完成整轮读-改-写后再返回旧快照
            a_read_started.set()
            b_done.wait(timeout=2)
        return text

    monkeypatch.setattr(pathlib.Path, "read_text", slow_read_text)

    def worker(adapter):
        if adapter == "ad_b":
            cli._update_zero_streak("ad_b", 0, state_path=state_path)
            b_done.set()
        else:
            cli._update_zero_streak("ad_a", 0, state_path=state_path)

    thread_a = threading.Thread(target=worker, args=("ad_a",))
    thread_b = threading.Thread(target=worker, args=("ad_b",))
    thread_a.start()
    a_read_started.wait(timeout=5)
    thread_b.start()
    thread_a.join(timeout=10)
    thread_b.join(timeout=10)

    state = __import__("json").loads(state_path.read_text("utf-8"))
    assert "ad_a" in state and "ad_b" in state, f"并发更新不得互相覆盖，实际 {state}"
