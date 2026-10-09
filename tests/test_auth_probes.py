"""登录态探针：每个探针真/假两种情况 + CHANNELS 登记表 + probe_all。"""
from __future__ import annotations

import pytest

from personal_intel_loop.auth_probes import (
    CHANNELS,
    probe_all,
    probe_caixin,
    probe_weibo,
    probe_weread,
    probe_zhihu,
)


def _fn(result=None, exc: Exception | None = None):
    """造一个注入函数：记录调用，返回 result 或抛exc。"""
    calls: list[str] = []

    def fetch(url: str, **kwargs):
        calls.append(url)
        if exc is not None:
            raise exc
        return result

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


# ---- 微博 -------------------------------------------------------------------


def test_probe_weibo_ok():
    payload = {"ok": 1, "data": {"groups": [{"name": "好友"}, {"name": "分组二"}]}}
    fetch = _fn(payload)
    result = probe_weibo(fetch)
    assert result["ok"] is True
    assert "好友" in result["detail"]
    assert fetch.calls == ["/ajax/feed/allGroups"]


def test_probe_weibo_groups_at_top_level():
    payload = {"ok": 1, "groups": [{"title": "顶格分组"}]}
    assert probe_weibo(_fn(payload))["ok"] is True


def test_probe_weibo_ok_not_1():
    result = probe_weibo(_fn({"ok": 0, "data": {"groups": [{"name": "好友"}]}}))
    assert result["ok"] is False
    assert "ok=0" in result["detail"]


def test_probe_weibo_empty_groups():
    result = probe_weibo(_fn({"ok": 1, "data": {"groups": []}}))
    assert result["ok"] is False
    assert "分组为空" in result["detail"]


def test_probe_weibo_exception():
    result = probe_weibo(_fn(exc=RuntimeError("HTTP 403")))
    assert result["ok"] is False
    assert "HTTP 403" in result["detail"]


# ---- 知乎 -------------------------------------------------------------------


def test_probe_zhihu_ok():
    payload = {"id": "示例id330e85", "name": "示例name6e620e", "url_token": "示例url_token"}
    result = probe_zhihu(_fn(payload))
    assert result["ok"] is True
    assert "示例name6e620e" in result["detail"]
    assert result.get("id") is None  # 不泄漏多余字段


def test_probe_zhihu_401():
    assert probe_zhihu(_fn({"code": 401, "error": {"message": "未登录"}}))["ok"] is False


def test_probe_zhihu_403():
    assert probe_zhihu(_fn({"code": 403}))["ok"] is False


def test_probe_zhihu_40352_risk():
    result = probe_zhihu(_fn({"code": 40352, "detail": "系统监测到您的请求过于频繁"}))
    assert result["ok"] is False
    assert "40352" in result["detail"]


def test_probe_zhihu_no_id():
    assert probe_zhihu(_fn({"error": {"message": "缺少 id"}}))["ok"] is False


def test_probe_zhihu_exception():
    assert probe_zhihu(_fn(exc=RuntimeError("HTTP 401")))["ok"] is False


# ---- 微信读书 ---------------------------------------------------------------


def test_probe_weread_ok():
    html = '<script>window.__INITIAL_STATE__={"shelf":{"shelfIndexes":[{"bookId":"b1"}]}}</script>'
    result = probe_weread(_fn(html))
    assert result["ok"] is True
    assert "shelfIndexes 非空" in result["detail"]


def test_probe_weread_empty_indexes():
    html = '<script>window.__INITIAL_STATE__={"shelf":{"shelfIndexes":[]}}</script>'
    result = probe_weread(_fn(html))
    assert result["ok"] is False
    assert "为空" in result["detail"]


def test_probe_weread_no_initial_state():
    result = probe_weread(_fn("<html><body>请登录</body></html>"))
    assert result["ok"] is False
    assert "登录页" in result["detail"]


def test_probe_weread_no_shelf_indexes():
    result = probe_weread(_fn('<script>window.__INITIAL_STATE__={"user":{}}</script>'))
    assert result["ok"] is False
    assert "没有 shelfIndexes" in result["detail"]


def test_probe_weread_empty_page():
    assert probe_weread(_fn(""))["ok"] is False


def test_probe_weread_exception():
    assert probe_weread(_fn(exc=RuntimeError("连接超时")))["ok"] is False


# ---- 财新 -------------------------------------------------------------------


def test_probe_caixin_ok():
    result = probe_caixin(_fn("<html><script>var userInfo = {...}</script></html>"))
    assert result["ok"] is True
    assert "userInfo" in result["detail"]


def test_probe_caixin_marker_missing():
    result = probe_caixin(_fn("<html><body>请登录后阅读</body></html>"))
    assert result["ok"] is False
    assert "userInfo" in result["detail"]


def test_probe_caixin_custom_marker():
    result = probe_caixin(_fn("<html>caixinLogined</html>"), marker="caixinLogined")
    assert result["ok"] is True


def test_probe_caixin_exception():
    assert probe_caixin(_fn(exc=RuntimeError("HTTP 500")))["ok"] is False


# ---- 登记表与批量 -----------------------------------------------------------


def test_channels_registry_shape():
    assert CHANNELS, "登记表不能是空的"
    for entry in CHANNELS:
        assert set(entry) == {"key", "label", "relogin_hint"}
        assert entry["key"] and entry["label"]
        # 人话：不能是命令、路径或空串
        assert len(entry["relogin_hint"]) >= 10
        assert not entry["relogin_hint"].startswith(("/", "-", "~"))
    keys = [e["key"] for e in CHANNELS]
    assert len(keys) == len(set(keys))
    for expected in ("weibo_home", "zhihu_moments", "weread_mp", "caixin"):
        assert expected in keys


def test_probe_all_mixed():
    fetches = {
        "weibo": _fn({"ok": 1, "data": {"groups": [{"name": "好友"}]}}),
        "zhihu": _fn({"id": "abc", "name": "示例name"}),
        "weread": _fn('<script>window.__INITIAL_STATE__={"shelf":{"shelfIndexes":[{"bookId":"b"}]}}</script>'),
        "caixin": _fn("<html>userInfo</html>"),
    }
    out = probe_all(fetches)
    assert len(out) == 4
    assert all(e["ok"] for e in out)
    assert all("relogin_hint" in e for e in out)


def test_probe_all_reports_failures_without_raising():
    fetches = {
        "weibo": _fn({"ok": 0}),
        "zhihu": _fn(exc=RuntimeError("401")),
        # weread / caixin 完全没注册
    }
    out = {e["key"]: e for e in probe_all(fetches)}
    assert out["weibo_home"]["ok"] is False
    assert out["zhihu_moments"]["ok"] is False
    assert out["weread_mp"]["ok"] is False
    assert "没注册" in out["weread_mp"]["detail"]
    assert out["caixin"]["ok"] is False


@pytest.mark.parametrize(
    "probe,good",
    [
        (probe_weibo, {"ok": 1, "data": {"groups": [{"name": "g"}]}}),
        (probe_zhihu, {"id": "x", "name": "n"}),
        (probe_weread, '__INITIAL_STATE__={"shelf":{"shelfIndexes":[{"bookId":"b"}]}}'),
        (probe_caixin, "<html>userInfo</html>"),
    ],
)
def test_probe_good_and_bad_shape(probe, good):
    """四个探针的返回结构统一是 {"ok": bool, "detail": str}。"""
    ok_result = probe(_fn(good))
    assert set(ok_result) == {"ok", "detail"}
    assert ok_result["ok"] is True
    assert isinstance(ok_result["detail"], str) and ok_result["detail"]

    bad_result = probe(_fn(exc=RuntimeError("boom")))
    assert set(bad_result) == {"ok", "detail"}
    assert bad_result["ok"] is False
    assert isinstance(bad_result["detail"], str) and bad_result["detail"]