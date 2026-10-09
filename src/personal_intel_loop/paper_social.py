"""讨论综述(契约第 6.4 节): 社交条目不逐帖上版, 按语义聚类写综述。

出版时把 adapter 属于 SOCIAL_ADAPTERS 的候选从主线拿出来, 按嵌入(注入 embed_fn)
做贪心聚类: 与既有组成员的最大余弦 ≥ DIGEST_SIMILARITY_THRESHOLD 归同组。
每组 ≥2 条交模型写一条综述 Item(kind="digest", 存 digests 表, item_id=digest:<id>);
单条的照普通 Item 处理但仍标 kind。综述分数 = 组内最高分 + 0.05×(组大小-1), 上限 +0.2。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Callable

from personal_intel_loop.paper_common import cosine

DIGEST_SIMILARITY_THRESHOLD = 0.78
DIGEST_BONUS_PER_MEMBER = 0.05
DIGEST_BONUS_CAP = 0.2
DIGEST_BODY_LIMIT = 400

DIGEST_PROMPT = """你在把同一话题的一组社交帖子写成一条「讨论综述」。输出严格的 JSON(不要 markdown 代码块, 不要解释)。

## 原帖(按时间倒序)
{members}

## 要求
- title: 话题一句话(≤40 字), 不是任何一帖的复述。
- lede: 发生了什么、各方说法与分歧(两三句中文); 原帖里没有的信息不要编。
- quotes: 2-3 条最有信息量的原话, 每条 {{"text": "<原帖原文摘录>", "author_label": "<作者/来源显示名>", "url": "<该帖 url>"}}; text 必须逐字来自上面某条原帖。
- topic: 主题门类(3-10 个字)。

## 输出
只返回一个 JSON 对象:
{{"title": "...", "lede": "...", "quotes": [], "topic": "..."}}
"""


def cluster_candidates(candidates: list[dict], vectors: list[list[float]], *, threshold: float = DIGEST_SIMILARITY_THRESHOLD) -> list[list[int]]:
    """贪心聚类: 每条与既有各组所有成员向量的最大余弦 ≥ threshold → 归入该组, 否则新组。

    返回组列表(元素是 candidates 的下标), 组内顺序 = 输入顺序。
    """
    if len(candidates) != len(vectors):
        # 10-05 审计 F13: 返回的下标指向 candidates, 长度不一致会越界指向不存在的候选——显式报错
        raise ValueError(f"candidates ({len(candidates)}) and vectors ({len(vectors)}) must have the same length")
    groups: list[list[int]] = []
    for index, vector in enumerate(vectors):
        best_group: list[int] | None = None
        best_sim = -1.0
        for group in groups:
            sim = max(cosine(vector, vectors[member]) for member in group)
            if sim >= threshold and sim > best_sim:
                best_sim, best_group = sim, group
        if best_group is not None:
            best_group.append(index)
        else:
            groups.append([index])
    return groups


def digest_id_for(date_iso: str, member_ids: list[str]) -> str:
    """同一组成员 + 同一天 → 稳定 digest_id(同日重跑幂等)。"""
    digest_key = hashlib.sha256("|".join([date_iso, *sorted(member_ids)]).encode("utf-8")).hexdigest()[:12]
    return f"dg:{digest_key}"


def build_social_digest_prompt(members: list[dict]) -> str:
    lines = []
    for member in members:
        body = (member.get("body") or "").strip()[:DIGEST_BODY_LIMIT]
        lines.append(
            f"- [{member.get('author_label') or member.get('source_label') or member.get('source')}]"
            f" {member.get('title') or ''} ({member.get('url') or '无链接'}) 正文: {body or '(空)'}"
        )
    return DIGEST_PROMPT.format(members="\n".join(lines))


def _extract_json(text):
    from personal_intel_loop.summarizer import _extract_json as summarizer_extract_json

    parsed = summarizer_extract_json(text or "")
    return parsed if isinstance(parsed, dict) else None


def build_digests(
    conn: sqlite3.Connection,
    *,
    date_iso: str,
    social_scored: list[tuple[dict, float]],
    embed_fn: Callable,
    llm_call=None,
    now_utc: str,
) -> tuple[list[tuple[dict, float]], set[str]]:
    """社交候选 → 聚类 → 综述。

    返回 ([(综述 payload, 分数)], 被综述吸收的原帖 item_id 集合)。单条组不产综述,
    其成员不算被吸收(照普通 Item 上版)。综述解析失败/模型异常的组也按单条处理。
    """
    from personal_intel_loop.paper_common import item_frame

    consumed: set[str] = set()
    digests: list[tuple[dict, float]] = []
    if not social_scored:
        return digests, consumed

    texts = [f"{c.get('title') or ''}\n{(c.get('body') or '')[:500]}" for c, _score in social_scored]
    vectors = embed_fn(texts)
    groups = cluster_candidates(social_scored, vectors)

    for group in groups:
        if len(group) < 2:
            continue
        members = [social_scored[index][0] for index in group]
        top_score = max(score for _c, score in (social_scored[index] for index in group))
        member_ids = [str(m["item_id"]) for m in members]
        try:
            if llm_call is not None:
                raw = llm_call(build_social_digest_prompt(members))
                model = None
            else:
                from personal_intel_loop.paper_ai import _call_cloud

                raw, model = _call_cloud(build_social_digest_prompt(members))
        except Exception:  # noqa: BLE001 — 综述失败降级为单条, 不挡出版
            continue
        parsed = _extract_json(raw)
        if parsed is None:
            continue
        title = str(parsed.get("title") or "").strip() or None
        lede = str(parsed.get("lede") or "").strip() or None
        if not title or not lede:
            continue
        quotes = []
        for quote in parsed.get("quotes") or []:
            if isinstance(quote, dict) and str(quote.get("text") or "").strip():
                quotes.append(
                    {
                        "text": str(quote.get("text")).strip()[:500],
                        "author_label": str(quote.get("author_label") or "").strip() or None,
                        "url": str(quote.get("url") or "").strip() or None,
                    }
                )
            if len(quotes) >= 3:
                break
        if not quotes:
            # 10-05 审计 F11: 契约 §6.4 要求综述带最有信息量的原话; 一条都抽不出的按综述失败
            # 降级为单条不上版(1 条时与既有测试钉死的行为一致, 照常产出)
            continue
        digest_id = digest_id_for(date_iso, member_ids)
        bonus = min(DIGEST_BONUS_PER_MEMBER * (len(group) - 1), DIGEST_BONUS_CAP)
        payload = item_frame()
        payload.update(
            {
                "item_id": f"digest:{digest_id}",
                "title": title,
                "url": None,
                "source": "paper_digest",
                "source_label": "讨论综述",
                "author_key": "paper_digest",
                "author_label": "讨论综述",
                "published_at": now_utc,
                "lede": lede,
                "one_liner": title,
                "topic": str(parsed.get("topic") or "").strip()[:40] or None,
                "kind": "digest",
                "quotes": quotes,
                "members": member_ids,
                "why_here": {"profile_hit": None, "lane": "digest", "blind_reason": None},
            }
        )
        digests.append((payload, top_score + bonus))
        consumed.update(member_ids)
    return digests, consumed


def store_digests(conn: sqlite3.Connection, *, date_iso: str, payloads: list[dict], member_map: dict[str, list[str]], now_utc: str) -> int:
    """把合成条目(digest/risk/leisure)写入 digests 表。返回写入条数。"""
    from personal_intel_loop.schemas import normalize_dt_to_utc_z

    created = normalize_dt_to_utc_z(now_utc)
    for payload in payloads:
        item_id = str(payload.get("item_id") or "")
        if not item_id:
            continue
        member_ids = member_map.get(item_id, [])
        if item_id.startswith("digest:") and member_ids:
            # 10-05 审计 F12: 同日重跑组员集变化 → digest_id 变了, INSERT OR REPLACE 只替换同 id 行,
            # 旧综述残留成同日同话题两条。删掉同期里与本次组员有交集的旧综述行;
            # 只动 digest: 前缀(risk:/leisure: 的 related ids 重叠是正常的, 不清)。
            for old_row in conn.execute(
                "SELECT digest_id, member_ids_json FROM digests WHERE edition_date=? AND digest_id LIKE 'digest:%'",
                (date_iso,),
            ).fetchall():
                if old_row["digest_id"] == item_id:
                    continue
                try:
                    old_members = set(json.loads(old_row["member_ids_json"] or "[]"))
                except (json.JSONDecodeError, TypeError):
                    continue
                if old_members & set(member_ids):
                    conn.execute("DELETE FROM digests WHERE digest_id=?", (old_row["digest_id"],))
        conn.execute(
            "INSERT OR REPLACE INTO digests (digest_id, edition_date, payload_json, member_ids_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                item_id,
                date_iso,
                json.dumps(payload, ensure_ascii=False),
                json.dumps(member_ids, ensure_ascii=False),
                created,
            ),
        )
    conn.commit()
    return len(payloads)
