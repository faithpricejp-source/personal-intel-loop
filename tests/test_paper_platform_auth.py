"""6.1 登录态健康: 探针状态迁移、只推一次、探针异常、relogin 只认注册表、Edition.auth_alerts。"""
from __future__ import annotations

import pytest

from personal_intel_loop.paper_api import get_editions, notifications
from personal_intel_loop.paper_auth import auth_alerts, auth_overview, relogin, run_probes
from personal_intel_loop.store import upsert_item
from tests.conftest import make_item
from tests.test_paper_inbox_edition import _seed_edition  # 复用 TASK3 的 seed helper

NOW = "2026-10-04T01:00:00Z"
T0 = "2026-10-04T00:00:00Z"
T1 = "2026-10-04T01:00:00Z"
T2 = "2026-10-04T02:00:00Z"


def _failing(detail="cookie expired"):
    return {"ok": False, "detail": detail}


def _ok():
    return {"ok": True, "detail": None}


def test_since_failing_transitions(db_conn):
    channels = [{"key": "weibo", "label": "微博", "relogin_hint": "重新登录", "probe": _failing, "relogin": {"kind": "command", "value": "my-weibo-login"}}]
    first = run_probes(db_conn, channels, now_utc=T0)
    assert first[0]["ok"] is False and first[0]["since_failing"] == T0
    # 持续失效: since_failing 保留第一次失效时间
    second = run_probes(db_conn, channels, now_utc=T1)
    assert second[0]["since_failing"] == T0
    # 恢复: 清空
    channels[0]["probe"] = _ok
    third = run_probes(db_conn, channels, now_utc=T2)
    assert third[0]["ok"] is True and third[0]["since_failing"] is None
    row = db_conn.execute("SELECT ok, since_failing FROM auth_status WHERE key='weibo'").fetchone()
    assert row["ok"] == 1 and row["since_failing"] is None
    # 恢复后再失效: 记新时间
    channels[0]["probe"] = _failing
    fourth = run_probes(db_conn, channels, now_utc="2026-10-04T03:00:00Z")
    assert fourth[0]["since_failing"] == "2026-10-04T03:00:00Z"


def test_probe_exception_counts_as_failing_with_class_name(db_conn):
    def boom():
        raise RuntimeError("network down")

    channels = [{"key": "zhihu", "label": "知乎", "relogin_hint": "", "probe": boom, "relogin": {"kind": "url", "value": "https://example.com"}}]
    result = run_probes(db_conn, channels, now_utc=T0)
    assert result[0]["ok"] is False and result[0]["detail"] == "RuntimeError"


def test_failure_notification_pushed_once_per_episode(db_conn):
    channels = [{"key": "weibo", "label": "微博", "relogin_hint": "重新登录", "probe": _failing, "relogin": {"kind": "command", "value": "my-weibo-login"}}]

    def pull(since, now):
        return notifications(db_conn, since=since, now_utc=now)["notifications"]

    run_probes(db_conn, channels, now_utc=T0)
    alerts = [n for n in pull("2026-10-03T00:00:00Z", T1) if n["id"].startswith("auth:")]
    assert len(alerts) == 1
    assert alerts[0] == {
        "id": "auth:weibo:2026-10-04T00:00:00Z",
        "title": "微博 登录已失效",
        "body": "cookie expired",
        "open_path": "/paper/#/auth",
    }
    # 同一次失效再拉: 只推一次(since 前移后不再出)
    assert [n for n in pull(T0, T1) if n["id"].startswith("auth:")] == []
    # 恢复后再失效 → 新 since_failing → 再推一次
    channels[0]["probe"] = _ok
    run_probes(db_conn, channels, now_utc=T1)
    channels[0]["probe"] = _failing
    run_probes(db_conn, channels, now_utc=T2)
    again = [n for n in pull(T1, "2026-10-04T03:00:00Z") if n["id"].startswith("auth:")]
    assert [n["id"] for n in again] == ["auth:weibo:2026-10-04T02:00:00Z"]


def test_relogin_only_registry_commands(db_conn):
    started = []
    payload = {"key": "zhihu", "command": "rm -rf /", "value": "evil string"}
    result = relogin(payload, starter=started.append)
    assert result == {"ok": True, "started": True}
    assert started == ["zhihu-cli login"], "只执行注册表里登记的命令, 前端传入字符串被忽略"

    result_url = relogin({"key": "weread"}, starter=started.append)
    assert result_url == {"ok": True, "open_url": "https://weread.qq.com/#login"}
    assert len(started) == 1, "url 通道不启动命令"

    with pytest.raises(KeyError):
        relogin({"key": "not-registered"}, starter=started.append)


def test_auth_overview_and_alerts(db_conn):
    channels = [{"key": "weibo", "label": "微博", "relogin_hint": "重新登录", "probe": _failing, "relogin": {"kind": "command", "value": "my-weibo-login"}}]
    # 未探测过的通道也在列表里, ok=None
    overview = auth_overview(db_conn)
    assert {channel["key"] for channel in overview} >= {"weibo", "zhihu", "weread", "caixin", "youtube"}
    weibo = next(channel for channel in overview if channel["key"] == "weibo")
    assert weibo["ok"] is None
    run_probes(db_conn, channels, now_utc=T0)
    assert [c["key"] for c in auth_alerts(db_conn)] == ["weibo"]
    alerts = auth_alerts(db_conn)
    assert set(alerts[0].keys()) == {"key", "label", "ok", "checked_at", "detail", "since_failing", "relogin_hint", "relogin_kind"}


def test_edition_carries_auth_alerts(db_conn):
    upsert_item(db_conn, make_item(item_id="item:lead", title="头条标题"), adapter_name="rss_briefing", source_payload_json="{}")
    db_conn.commit()
    _seed_edition(db_conn, "2026-10-04", NOW)
    channels = [{"key": "weibo", "label": "微博", "relogin_hint": "重新登录", "probe": _failing, "relogin": {"kind": "command", "value": "my-weibo-login"}}]
    run_probes(db_conn, channels, now_utc=T0)
    edition = get_editions(db_conn, before="2026-10-05", limit=1)["editions"][0]
    assert [entry["key"] for entry in edition["auth_alerts"]] == ["weibo"]
    # 读侧(Edition)的 label/hint 取自模块注册表
    from personal_intel_loop.paper_auth import default_registry

    expected_hint = next(c["relogin_hint"] for c in default_registry() if c["key"] == "weibo")
    assert edition["auth_alerts"][0]["relogin_hint"] == expected_hint
