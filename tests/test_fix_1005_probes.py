"""10-05 验收 G212 / G213：auth_probes 两处登录态误报的成对回归测试。

G212：微信读书探针只认 `shelfIndexes` 后紧跟 `[`（允许空白）且数组非空为登录；
      `shelfIndexes:null` 后面再出现任何非空数组不得误判正常。
G213：财新探针不得整页找 `userInfo` 子串就判正常；`userInfo` 值为 null / 空对象 /
      uid 为空时判失效。
"""
from __future__ import annotations

from personal_intel_loop.auth_probes import probe_caixin, probe_weread


def _fn(result=None, exc: Exception | None = None):
    calls: list[str] = []

    def fetch(url: str, **kwargs):
        calls.append(url)
        if exc is not None:
            raise exc
        return result

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


# ---- G212 微信读书 ----------------------------------------------------------


def test_g212_weread_logged_in_page_ok():
    """登入页：shelfIndexes 紧跟非空数组 → 正常。"""
    html = '<script>window.__INITIAL_STATE__={"shelf":{"shelfIndexes":[{"bookId":"b7"}]}}</script>'
    result = probe_weread(_fn(html))
    assert result["ok"] is True


def test_g212_weread_logged_in_whitespace_ok():
    """登入页：shelfIndexes 与 `[` 之间允许空白。"""
    html = '<script>window.__INITIAL_STATE__={ "shelfIndexes" :  [ {"bookId":"b8"} ] }</script>'
    result = probe_weread(_fn(html))
    assert result["ok"] is True


def test_g212_weread_null_then_array_is_not_logged_in():
    """登出页误报形状：shelfIndexes:null，其后别的键带非空数组 → 必须判失效。"""
    html = '<script>window.__INITIAL_STATE__={"shelf":{"shelfIndexes":null},"archive":[{"id":1}]}</script>'
    result = probe_weread(_fn(html))
    assert result["ok"] is False


def test_g212_weread_empty_array_is_not_logged_in():
    """登出页：shelfIndexes 紧跟空数组 → 判失效。"""
    html = '<script>window.__INITIAL_STATE__={"shelf":{"shelfIndexes":[ ]}}</script>'
    result = probe_weread(_fn(html))
    assert result["ok"] is False


# ---- G213 财新 ---------------------------------------------------------------


def test_g213_caixin_logged_in_uid_ok():
    """登入页：userInfo 对象带非空 uid → 正常。"""
    html = '<html><script>var userInfo = {"uid":"10086","name":"某用户"};</script></html>'
    result = probe_caixin(_fn(html))
    assert result["ok"] is True


def test_g213_caixin_null_marker_is_not_logged_in():
    """登出页误报形状：整页含 userInfo 字面量但值为 null → 必须判失效。"""
    html = '<html><script>{"userInfo":null,"menu":[{"id":1}]}</script></html>'
    result = probe_caixin(_fn(html))
    assert result["ok"] is False


def test_g213_caixin_empty_object_is_not_logged_in():
    """登出页：userInfo 为空对象 → 判失效。"""
    html = '<html><script>var userInfo = {};</script><body>请登录</body></html>'
    result = probe_caixin(_fn(html))
    assert result["ok"] is False


def test_g213_caixin_empty_uid_is_not_logged_in():
    """登出页：userInfo 存在但 uid 为空串 → 判失效。"""
    html = '<html><script>var userInfo = {"uid":"","name":""};</script></html>'
    result = probe_caixin(_fn(html))
    assert result["ok"] is False
