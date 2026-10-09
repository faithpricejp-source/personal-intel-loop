"""报纸链路的共享小工具: 来源显示名 / 署名归一 / 作者信任分。

被 paper_feedback / paper / paper_api / paper_distill 共用, 放独立模块避免互相 import 成环。
"""
from __future__ import annotations

import json
import math
import sqlite3

# 各 adapter 往 source_payload_json 里塞的「来源显示名」键, 按命中率排序
SOURCE_LABEL_PAYLOAD_KEYS = (
    "feed_name",       # rss_briefing
    "channel_name",    # youtube_followed
    "builder_name",    # follow_builders (x)
    "show_name",       # follow_builders (podcast)
    "source_name",     # aihot
    "account_name",
    "user_name",
    "blog_name",
    "name",
)

# item_ai payload 里可能顶替来源名的键不算署名, byline 只认模型抽出的作者名

# 社交条目(6.4): 不逐帖上版, 聚类写综述; 单条照普通 Item 但显式标 kind="article"
SOCIAL_ADAPTERS = frozenset({"weibo_home", "weibo_timeline", "zhihu_moments"})
# 媒体条目(6.4): kind="media" + media 字段
MEDIA_ADAPTERS = frozenset({"podcast_new", "youtube_followed", "local_transcripts"})
MEDIA_TYPES = {"youtube_followed": "video", "podcast_new": "audio", "local_transcripts": "audio"}

# Edition.sections 的输出顺序(契约 6-10 节增补后); inbox 由投递箱表计算, 殿后
SECTION_ORDER = ("lead", "top", "counter", "briefs", "blind", "warmth", "risk", "opportunity", "settle", "leisure")
NOVELTY_KINDS = ("new_fact", "new_mechanism", "counter", "known", "confirming")
NOVELTY_NEW_KINDS = frozenset({"new_fact", "new_mechanism", "counter"})
NOVELTY_OLD_KINDS = frozenset({"known", "confirming"})
# 合成条目(digest/risk/leisure)的 item_id 前缀: 存 digests 表, 不在 items 表
SYNTHETIC_PREFIXES = ("digest:", "risk:", "leisure:")


def adapter_of(source: str) -> str:
    return str(source or "").split(":", 1)[0]


def item_frame() -> dict:
    """合成条目(digests 表里的 payload)的全键骨架——与 paper_api._item_dict 的键集合逐键一致。"""
    return {
        "item_id": None,
        "title": None,
        "url": None,
        "source": None,
        "source_label": None,
        "author_key": None,
        "author_label": None,
        "author_is_byline": False,
        "published_at": None,
        "lang": "zh",
        "image_url": None,
        "lede": None,
        "one_liner": None,
        "backstory": None,
        "so_what": None,
        "claim": None,
        "topic": None,
        "style_tags": [],
        "same_day_url": None,
        "kind": "article",
        "media": None,
        "novelty": None,
        "verification": None,
        "opportunity": None,
        "settle": None,
        "why_here": {"profile_hit": None, "lane": None, "blind_reason": None},
        "my": {"overall": 0, "quality": 0, "author": 0, "style": 0, "topic": 0, "reason_code": None, "note": None},  # 10-05 审计 F1: 契约 §11 要求 my.note
        "trust": {"source": None, "author": None},
    }


def cosine(a, b) -> float:
    if len(a) != len(b):
        # 10-05 审计 F2: zip 静默截断会让相似度系统性偏小且无痕(如混用不同模型维度), 显式报错
        raise ValueError(f"cosine: vectors must have the same length ({len(a)} != {len(b)})")
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def source_label_of(source: str, payload: dict | None) -> str:
    payload = payload or {}
    for key in SOURCE_LABEL_PAYLOAD_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return source.split(":", 1)[1] if ":" in source else source


def author_key_from_payload(payload: dict | None, source_label: str) -> str:
    """署名归一键: item_ai 抽到署名用署名, 抽不到用来源显示名(契约 Item.author_key 注释)。"""
    byline = (payload or {}).get("byline")
    if isinstance(byline, str) and byline.strip():
        return byline.strip()
    return source_label


def author_score(n_up: int, n_down: int) -> float:
    """作者信任分: (2 + n_up) / (4 + n_up + n_down), 无记录时 n_up=n_down=0 → 0.5。"""
    denominator = 4 + int(n_up) + int(n_down)
    return (2 + int(n_up)) / denominator if denominator else 0.5


def item_ai_payload(conn: sqlite3.Connection, item_id: str) -> dict | None:
    """item_ai.payload_json 反序列化; 无记录/坏 JSON 返回 None(不抛)。"""
    row = conn.execute("SELECT payload_json FROM item_ai WHERE item_id=?", (item_id,)).fetchone()
    if row is None or not row["payload_json"]:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def first_media_url(payload: dict | None) -> str | None:
    urls = (payload or {}).get("media_urls")
    if isinstance(urls, list):
        for url in urls:
            if isinstance(url, str) and url.strip():
                return url.strip()
    return None
