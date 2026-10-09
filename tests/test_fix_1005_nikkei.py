"""10-05 验收 · 日经中文正文抽取（G115 容器选择器）+ 付费墙截断标记（G122 一部分）。

fixtures/nikkei/article_{1,2,3}.html 为 10-05 未登录实抓页面。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from personal_intel_loop.adapters.nikkei_cn import (
    NikkeiCnAdapter,
    _ArticleRef,
    _extract_body,
    _extract_body_parts,
)

FIXTURES = Path(__file__).parent / "fixtures" / "nikkei"

SAMPLE_HTML = {
    name: (FIXTURES / name).read_text(encoding="utf-8")
    for name in ("article_1.html", "article_2.html", "article_3.html")
}

# 首段正文 / 推荐或相关新闻列表里的标题（只出现在正文容器之外）
EXPECTED_FIRST_PARA = {
    "article_1.html": "沙特阿拉伯和阿拉伯联合酋长国（UAE）将参与亚洲石油储备的合作机制",
    "article_2.html": "中国半导体相关企业的业绩正在不断改善",
    "article_3.html": "日本首相高市早苗4日在个人X账号上谈及该起抢劫杀人案件",
}
EXPECTED_RECOMMENDED_TITLE = {
    "article_1.html": "中国人形机器人崛起背后的“华为军团”",
    "article_2.html": "中国8月对美出口增34%，贸易顺差重新扩大",
    "article_3.html": "名古屋亚运会问题频出，日本缺的是什么？",
}

COPYRIGHT_TAIL = "Nikkei Inc. All rights reserved./ 日本经济新闻社知识产权所有 未经许可不得转载"
PAYWALL_LINES = (
    "敬请登录以便观看全部文章",
    "如果您还不是日经中文网会员",
    "如果您已经是日经中文网会员",
)

# ---------------------------------------------------------------- 编号 1 · G115


def test_g115_real_page_container_is_matched():
    """三份实抓页面都命中正文容器，不再走全页 <p> 兜底。"""
    for name, html in SAMPLE_HTML.items():
        _body, fallback, _paywalled = _extract_body_parts(html)
        assert fallback is False, name


def test_g115_body_excludes_copyright_tail():
    for name, html in SAMPLE_HTML.items():
        body = _extract_body(html)
        assert COPYRIGHT_TAIL not in body, name


def test_g115_body_excludes_recommended_titles():
    for name, html in SAMPLE_HTML.items():
        body = _extract_body(html)
        assert EXPECTED_RECOMMENDED_TITLE[name] not in body, name


def test_g115_body_keeps_first_paragraph():
    for name, html in SAMPLE_HTML.items():
        body = _extract_body(html)
        assert EXPECTED_FIRST_PARA[name] in body, name


def test_g115_fallback_flagged_in_payload_and_logs(monkeypatch, caplog):
    """容器缺失时走兜底：payload 记 body_fallback=true 并 warning。"""
    html = """<html><head><title>t</title></head><body>
    <p>这是一段足够长的兜底正文，用来验证容器选择器完全不命中的页面上，采集器仍然会把段落拼成正文，但必须把这种退化路径标记出来，便于排查容器选择器失效。</p>
    </body></html>"""

    class _FakeSession:
        headers: dict = {}
        cookies = None

        def get(self, url, timeout):
            class _R:
                status_code = 200
                text = html

            return _R()

    adapter = NikkeiCnAdapter(session=_FakeSession(), fetch_delay=0.0)
    ref = _ArticleRef(
        url="https://cn.nikkei.com/china/ceconomy/64148-2026-10-05-05-00-40.html",
        category="china",
        subcategory="ceconomy",
        article_id="64148",
        published=datetime(2026, 10, 4, 20, 0, 0, tzinfo=timezone.utc),
    )
    with caplog.at_level(logging.WARNING, logger="personal_intel_loop.adapters.nikkei_cn"):
        record = adapter._build_record(ref)
    payload = json.loads(record.source_payload_json)
    assert payload["body_fallback"] is True
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_g115_normal_article_not_flagged_fallback():
    adapter = NikkeiCnAdapter(fetch_delay=0.0)
    ref = _ArticleRef(
        url="https://cn.nikkei.com/industry/ienvironment/64218-2026-10-05-09-03-36.html",
        category="industry",
        subcategory="ienvironment",
        article_id="64218",
        published=datetime(2026, 10, 5, 0, 3, 36, tzinfo=timezone.utc),
    )
    monkeypatch_html = SAMPLE_HTML["article_1.html"]
    adapter._get = lambda url: monkeypatch_html  # type: ignore[method-assign]
    record = adapter._build_record(ref)
    assert json.loads(record.source_payload_json)["body_fallback"] is False


# ---------------------------------------------------------------- 编号 2 · G122


def test_g122_paywalled_sample_detected_and_strip_registration():
    body, _fallback, paywalled = _extract_body_parts(SAMPLE_HTML["article_3.html"])
    assert paywalled is True
    for line in PAYWALL_LINES:
        assert line not in body
    assert EXPECTED_FIRST_PARA["article_3.html"] in body


def test_g122_free_samples_not_marked_paywalled():
    for name in ("article_1.html", "article_2.html"):
        _body, _fallback, paywalled = _extract_body_parts(SAMPLE_HTML[name])
        assert paywalled is False, name


def test_g122_payload_paywalled_and_logs(caplog):
    adapter = NikkeiCnAdapter(fetch_delay=0.0)
    ref = _ArticleRef(
        url="https://cn.nikkei.com/politicsaeconomy/politicsasociety/64217-2026-10-04-14-52-35.html",
        category="politicsaeconomy",
        subcategory="politicsasociety",
        article_id="64217",
        published=datetime(2026, 10, 4, 5, 52, 35, tzinfo=timezone.utc),
    )
    html = SAMPLE_HTML["article_3.html"]
    adapter._get = lambda url: html  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger="personal_intel_loop.adapters.nikkei_cn"):
        record = adapter._build_record(ref)
    payload = json.loads(record.source_payload_json)
    assert payload["paywalled"] is True
    assert "如果您还不是日经中文网会员" not in record.item.body
    assert any("paywall" in r.getMessage().lower() for r in caplog.records)


def test_g122_free_article_payload_paywalled_false():
    adapter = NikkeiCnAdapter(fetch_delay=0.0)
    ref = _ArticleRef(
        url="https://cn.nikkei.com/china/ceconomy/64148-2026-10-05-05-00-40.html",
        category="china",
        subcategory="ceconomy",
        article_id="64148",
        published=datetime(2026, 10, 4, 20, 0, 0, tzinfo=timezone.utc),
    )
    html = SAMPLE_HTML["article_2.html"]
    adapter._get = lambda url: html  # type: ignore[method-assign]
    record = adapter._build_record(ref)
    assert json.loads(record.source_payload_json)["paywalled"] is False


def test_container_block_handles_nested_divs():
    # 10-05 复审 R01：容器内有嵌套 div 时不能在嵌套块结束处截断，也不能越过容器
    from personal_intel_loop.adapters.nikkei_cn import _container_block, _find_article_container

    html = ('<div class="newsText"><p>第一段正文足够长足够长足够长足够长足够长。</p>'
            '<div class="pic"><img src="x"></div>'
            '<p>第二段正文也足够长足够长足够长足够长足够长。</p></div>'
            '<div class="footer"><p>© Nikkei Inc. All rights reserved.</p></div>')
    block = _container_block(html, _find_article_container(html))
    assert "第二段" in block
    assert "Nikkei Inc." not in block
