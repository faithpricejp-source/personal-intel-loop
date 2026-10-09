"""代码审查复核(N 路): 每条发现先写钉住现行行为的测试复现, 再最小修复(N-4 另见 test_fulltext_pinned_dns.py)。

测试直接调用真文件里的真函数; 网络与 DNS 打桩, 不出网。
"""
from __future__ import annotations

import socket

import pytest
import requests

from personal_intel_loop.schemas import Item
from personal_intel_loop.store import upsert_item

NOW = "2026-10-06T12:00:00Z"


# ---- N-2: 相对地址的 301/302 Location ----------------------------------------

class _FakeResp:
    def __init__(self, url, status_code, location=None, text="<html>ok</html>"):
        self.url = url
        self.status_code = status_code
        self.text = text
        self.headers = {"Location": location} if location else {}
        self.is_redirect = status_code in (301, 302, 303, 307, 308)
        self.is_permanent_redirect = status_code in (301, 308)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"http {self.status_code}")


def _fake_public_dns(monkeypatch):
    """所有主机名解析到公网 IP: 真 SSRF 校验原样跑, 只是 DNS 打桩(不出网)。"""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def test_n2_relative_redirect_location_is_urljoined(monkeypatch):
    """短链 302 回相对 Location(/news/123)时应补全后跟随, 而不是当非法 URL 拒掉。"""
    _fake_public_dns(monkeypatch)
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if url == "https://news.example.com/s/abc":
            return _FakeResp(url, 302, location="/news/article/123")
        return _FakeResp(url, 200, text="<html>正文</html>")

    monkeypatch.setattr("personal_intel_loop.fulltext._pinned_get", fake_get)  # 单跳出口收口到 _pinned_get
    from personal_intel_loop.fulltext import _default_http_get

    html = _default_http_get("https://news.example.com/s/abc")
    assert html == "<html>正文</html>"
    assert calls == ["https://news.example.com/s/abc", "https://news.example.com/news/article/123"]


def test_n2_protocol_relative_redirect_location_is_urljoined(monkeypatch):
    """协议相对 //host/path 的跳转同样要能跟随。"""
    _fake_public_dns(monkeypatch)
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if url == "https://news.example.com/s/abc":
            return _FakeResp(url, 301, location="//cdn.example.com/news/1")
        return _FakeResp(url, 200, text="<html>正文</html>")

    monkeypatch.setattr("personal_intel_loop.fulltext._pinned_get", fake_get)  # 单跳出口收口到 _pinned_get
    from personal_intel_loop.fulltext import _default_http_get

    assert _default_http_get("https://news.example.com/s/abc") == "<html>正文</html>"
    assert calls[-1] == "https://cdn.example.com/news/1"


# ---- N-3: 预警 lookback 窗口两种时间字符串格式混用 ------------------------------

def _alert(conn, item_id, ts):
    upsert_item(
        conn,
        Item(id=item_id, source="disaster_alerts:cn:广州市",
             url=f"https://www.nmc.cn/publish/alarm/{item_id}.html",
             title="广东省广州市天河区气象台发布雷雨大风橙色预警信号",
             body="同上", ts=ts, lang="zh"),
        adapter_name="disaster_alerts", source_payload_json="{}")


def test_n3_alerts_lookback_excludes_same_day_out_of_window(db_conn):
    """cutoff 当天 0 点到 cutoff 之间的条目已超 6h lookback, 不得因 'T' > ' ' 的
    字符串比较被误判 ts >= cutoff。"""
    from personal_intel_loop import alerts_notify as an

    _alert(db_conn, "a-old", "2026-10-06T00:30:00Z")   # 11.5h 前, 已超窗, 但与 cutoff 同日
    _alert(db_conn, "a-new", "2026-10-06T08:00:00Z")   # 4h 前, 窗内
    rows = an._pending(db_conn, now_utc=NOW, lookback_hours=6)
    assert [r["item_id"] for r in rows] == ["a-new"]


# ---- N-6: mark_read 的 int() 转换接受 bool/float -------------------------------

def test_n6_mark_read_rejects_bool_and_float(db_conn):
    """{"inbox_id": true} / 1.9 是畸形输入, 应 400(ValueError), 而不是静默改标 1 号条目。"""
    from personal_intel_loop.paper_inbox import accept, mark_read

    first = accept(db_conn, {"source": "pil.alerts", "title": "t1", "body": "b"}, now_utc=NOW)
    assert first["inbox_id"] == 1
    for bad in (True, 1.9):
        with pytest.raises(ValueError):
            mark_read(db_conn, bad, now_utc=NOW)
    assert db_conn.execute("SELECT read_at FROM inbox WHERE inbox_id=1").fetchone()["read_at"] is None


def test_n6_mark_read_still_accepts_int_and_digit_str(db_conn):
    """防修过头: 正常 int 与数字字符串路径不变。"""
    from personal_intel_loop.paper_inbox import accept, mark_read

    inbox_id = accept(db_conn, {"source": "pil.alerts", "title": "t1", "body": "b"}, now_utc=NOW)["inbox_id"]
    assert mark_read(db_conn, inbox_id, now_utc=NOW) == {"ok": True}
    second = accept(db_conn, {"source": "pil.alerts", "title": "t2", "body": "b"}, now_utc=NOW)["inbox_id"]
    assert mark_read(db_conn, str(second), now_utc=NOW) == {"ok": True}
    with pytest.raises(ValueError):
        mark_read(db_conn, "abc", now_utc=NOW)


# ---- N-8: append_proposal 按子串切分提案段落 -----------------------------------

def test_n8_append_proposal_goes_inside_pending_section(tmp_path):
    """待接受段落后还有别的章节时, 新提案必须插进待接受段落末尾;
    子串 split 会把它追加到全文末尾, 后续章节的 - 行混进待接受清单。"""
    from personal_intel_loop.profile import append_proposal, pending_proposals

    p = tmp_path / "reading_profile.md"
    p.write_text(
        "# 阅读偏好\n\n## 待接受的修订\n-\n\n## 域饱和表\n| AI 产业经济 | 饱和 | x |\n- 便签行\n",
        "utf-8",
    )
    append_proposal("新提案", p)
    text = p.read_text("utf-8")
    assert pending_proposals(p) == ["新提案"]
    assert text.index("## 待接受的修订") < text.index("- 新提案") < text.index("## 域饱和表")


def test_n8_append_proposal_keeps_annotated_header_intact(tmp_path):
    """标题带尾注(与 decide_proposal 同口径的前缀匹配)时, 尾注保持原位,
    提案仍插进该段落末尾而不是把尾注行降级成正文。"""
    from personal_intel_loop.profile import append_proposal, pending_proposals

    p = tmp_path / "reading_profile.md"
    header = "## 待接受的修订(后台提案区,每条带日期与依据;我接受后上移合并)"
    p.write_text(f"# p\n\n正文\n\n{header}\n-\n", "utf-8")
    append_proposal("带日期的新提案", p)
    text = p.read_text("utf-8")
    assert f"{header}\n- 带日期的新提案\n" in text
    assert pending_proposals(p) == ["带日期的新提案"]
