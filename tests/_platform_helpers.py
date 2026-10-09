"""TASK4 平台测试共享的造库/假实现辅助(文件名不带 test_ 前缀, pytest 不收集)。

假 LLM 按提示词里的标记分派: 预处理 / 讨论综述 / 行前风险简报 / 结算栏 / 推荐关注,
并能按标题序号给不同 item 返回不同 novelty/lane_tags(配额与分区测试靠它做到确定性)。
"""
from __future__ import annotations

import json
import re

from personal_intel_loop.store import upsert_item
from tests.conftest import make_item

NOW = "2026-10-04T02:00:00Z"
TODAY = "2026-10-04"
MONDAY = "2026-10-05"  # 2026-10-05 是周一

_TITLE_RE = re.compile(r"- 标题: (.*)")


def ai_json(
    *,
    novelty_kind: str | None = "known",
    novelty_why: str | None = None,
    novelty_against: str | None = None,
    lane_tags: list[str] | None = None,
    verification: dict | None = None,
    opportunity: dict | None = None,
    byline: str | None = None,
    topic: str = "科学",
    lede: str = "导语。",
) -> str:
    return json.dumps(
        {
            "lede": lede,
            "one_liner": "一句话。",
            "backstory": None,
            "so_what": None,
            "claim": None,
            "byline": byline,
            "style_tags": [],
            "topic": topic,
            "profile_hit": None,
            "lane": "material",
            "novelty": None if novelty_kind is None else {"kind": novelty_kind, "why": novelty_why, "against": novelty_against},
            "lane_tags": lane_tags or [],
            "verification": verification,
            "opportunity": opportunity,
        },
        ensure_ascii=False,
    )


DIGEST_JSON = json.dumps(
    {
        "title": "加息话题讨论升温",
        "lede": "关于加息, 两方说法出现分歧。",
        "quotes": [{"text": "加息板上钉钉", "author_label": "甲", "url": "https://weibo.example/1"}],
        "topic": "宏观",
    },
    ensure_ascii=False,
)

RISK_JSON = json.dumps(
    {
        "title": "大阪行前风险简报",
        "lede": "总体平稳, 台风季需留意(证据一般)。",
        "sections": [
            {
                "heading": "气候",
                "points": [{"text": "9 月台风多发, 关注 JMA 警报", "source_title": "JMA", "source_url": "https://jma.example/1", "date": "2026-09-20"}],
            },
            {
                "heading": "治安",
                "points": [{"text": "没查到官方说法", "source_title": None, "source_url": None, "date": None}],
            },
        ],
    },
    ensure_ascii=False,
)

SETTLE_JSON = json.dumps(
    {
        "verdict": "likely_true",
        "basis": "库内新条目支持该断言",
        "links": [{"title": "后续报道", "url": "https://example.com/followup"}],
    },
    ensure_ascii=False,
)

FOLLOW_JSON = json.dumps(
    {"picks": [{"platform": "weibo", "account_id": "789", "reason": "证据「乙转了三条行业数据帖」值得跟"}]},
    ensure_ascii=False,
)


def extract_title(prompt: str) -> str:
    match = _TITLE_RE.search(prompt)
    return match.group(1).strip() if match else ""


def make_dispatch_llm(*, ai=None, digest=None, risk=None, settle=None, follow=None, calls: list | None = None):
    """按提示词标记分派的假 LLM。各分派值是 str 或 callable(prompt)->str。"""

    def _run(spec, prompt):
        if spec is None:
            raise AssertionError(f"unexpected prompt kind: {prompt[:80]}")
        return spec(prompt) if callable(spec) else spec

    def llm(prompt: str) -> str:
        if calls is not None:
            calls.append(prompt)
        if "讨论综述" in prompt:
            return _run(digest, prompt)
        if "行前风险简报" in prompt:
            return _run(risk, prompt)
        if "结算栏" in prompt:
            return _run(settle, prompt)
        if "推荐关注" in prompt:
            return _run(follow, prompt)
        return _run(ai, prompt)

    return llm


def seed_candidate(conn, index: int, *, source: str | None = None, title: str | None = None, body: str = "正文", payload: dict | None = None):
    """seed 一条候选 item(带 feed_name, 供 source_label/作者归一)。"""
    source = source or f"rss_briefing:feed_{index % 3}"
    payload = payload if payload is not None else {"feed_name": f"源 {index % 3}"}
    upsert_item(
        conn,
        make_item(
            item_id=f"item:{index:02d}",
            source=source,
            url=f"https://example.com/{index}",
            title=title or f"标题 {index}",
            body=body,
        ),
        adapter_name=source.split(":", 1)[0],
        source_payload_json=json.dumps(payload, ensure_ascii=False),
    )


def embed_by_keyword(keyword: str):
    """含关键词的文本 → [1,0], 否则 [0,1](两组各自聚在一起, 余弦 0/1 控制 0.78 阈值)。"""

    def embed(texts):
        return [[1.0, 0.0] if keyword in text else [0.0, 1.0] for text in texts]

    return embed


def embed_by_title_index(vectors: dict[int, list[float]]):
    """按候选标题里的序号给向量(测试 build_edition 的聚类时最直接)。"""

    def embed(texts):
        out = []
        for text in texts:
            match = re.search(r"(\d+)", text)
            index = int(match.group(1)) if match else -1
            out.append(list(vectors.get(index, [0.0, 1.0])))
        return out

    return embed


def edition_section_ids(conn, edition_date: str, section: str) -> list[str]:
    return [
        row["item_id"]
        for row in conn.execute(
            "SELECT item_id FROM editions WHERE edition_date=? AND section=? ORDER BY rank",
            (edition_date, section),
        )
    ]
