from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from personal_intel_loop import DB_PATH, LOCAL_TZ, STAGING_DIR, resolve_data_relpath
from personal_intel_loop.ranking import (
    ACTIVE_OBJECT_TYPES,
    ENRICH_OBJECT_TYPES,
    ItemSignalsV2,
    TIER_LONGFORM,
    TIER_PULSE,
    classify_tier,
    score_backlog_item_v2,
    score_item_v1,
    score_item_v2,
    sort_key_for_backlog_item_v2,
    sort_key_for_item,
    sort_key_for_item_v2,
)
from personal_intel_loop.store import recompute_source_trust

RANKING_V1 = "v1_trust_recency"
RANKING_V2 = "v2_vault_aligned"
TIER_ALERT = "alert"
DIGEST_BUCKET_LIVE = "live_feed"
DIGEST_BUCKET_TRANSCRIPT = "transcript_backlog"

# Per-platform admission cap for the v2 live digest. Platform = source prefix
# before the first ':' (e.g. "youtube_followed", "rss_briefing"). Without this,
# high-volume low-signal platforms (followed YouTube channels — history/edu docs
# that score high on novelty but zero on active/enrich) flood the longform tier
# and crowd out vault-relevant items. Only platforms listed here are capped;
# rss_briefing is intentionally uncapped because its single prefix fans out to
# ~50 distinct feeds (capping the prefix would gut RSS coverage).
# 单一源霸榜上限。2026-08-26 加候选池保底后, 财新(周刊, 池内 25 席)一举拿下 top-15 的 7 席,
# 从"一条进不来"摆到另一端。保底解决的是"进得了池子", 这里解决的是"不许占满输出"。
DEFAULT_PER_PLATFORM_CAP: dict[str, int] = {
    "youtube_followed": 4,
    "local_transcripts": 3,   # 本机转录/文章合计
    "aihot": 3,
    # 微博不设上限: 它在 profile 里走独立 lane(意外/跨域, 按人配额), 不该在这里被排序器二次限流;
    # 且既有 tier 测试用 weibo 造数据, 加上限会误伤。
}


def _platform_of(source: str) -> str:
    return (source or "").split(":", 1)[0]


def _to_local_iso(ts_utc: str) -> str:
    return (
        datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
        .astimezone(LOCAL_TZ)
        .isoformat(timespec="seconds")
    )


def _manifest_links(manifest_relpath: str | None) -> tuple[str | None, str | None]:
    if not manifest_relpath:
        return None, None
    manifest_path = resolve_data_relpath(manifest_relpath)
    if not manifest_path.exists():
        return None, None
    manifest = json.loads(manifest_path.read_text("utf-8"))
    assets = manifest.get("assets", [])
    if not assets:
        return None, None
    first_relpath = str(assets[0]["local_relpath"])
    suffix = first_relpath.removeprefix("data/media/")
    dirname = str(Path(suffix).parent)
    return f"./pil_media/{suffix}", f"./pil_media/{dirname}/"


def _build_embed_text(candidate: dict) -> str:
    parts = [candidate.get("title") or ""]
    body = candidate.get("body") or candidate.get("summary") or ""
    if body:
        parts.append(body[:1500])
    return "\n".join(p for p in parts if p).strip()


def _ensure_candidate_embeddings(conn, candidates: list[dict]) -> None:
    import numpy as np

    from personal_intel_loop import embeddings
    from personal_intel_loop.store import set_item_embedding

    needing: list[int] = []
    for idx, cand in enumerate(candidates):
        blob = cand.pop("embedding_blob", None)
        if blob is None:
            needing.append(idx)
        else:
            cand["_embedding"] = np.frombuffer(blob, dtype="float32")
    if not needing:
        return

    texts = [_build_embed_text(candidates[i]) for i in needing]
    vectors = embeddings.embed_queries(texts)
    with conn:
        for i, vec in zip(needing, vectors):
            candidates[i]["_embedding"] = vec.astype("float32")
            set_item_embedding(conn, candidates[i]["item_id"], vec)


def _mmr_dedup(candidates: list[dict], *, sim_threshold: float = 0.88) -> list[dict]:
    """Greedy event dedup: keep highest-scored item, skip near-duplicates.

    Assumes candidates are already sorted by score descending.
    Items without embeddings are always kept.
    """
    import numpy as np

    kept: list[dict] = []
    kept_vecs: list = []
    for cand in candidates:
        vec = cand.get("_embedding")
        if vec is None or len(kept_vecs) == 0:
            kept.append(cand)
            if vec is not None:
                kept_vecs.append(vec)
            continue
        stacked = np.stack(kept_vecs)  # (n, dim)
        norm = np.linalg.norm(vec) + 1e-9
        norms = np.linalg.norm(stacked, axis=1, keepdims=True) + 1e-9
        sims = (stacked / norms) @ (vec / norm)
        if sims.max() >= sim_threshold:
            continue
        kept.append(cand)
        kept_vecs.append(vec)
    return kept


def _candidate_tier(candidate: dict) -> str:
    payload = candidate.get("source_payload") or {}
    content_len = payload.get("content_char_len")
    if isinstance(content_len, int) and content_len > 0:
        return classify_tier("x" * min(content_len, 501))
    return classify_tier(candidate.get("body"))


def _cooldown_since_digest_date(
    *,
    date_local: date | None,
    now_utc: str,
    cooldown_days: int | None,
) -> str | None:
    if cooldown_days is None or cooldown_days <= 0:
        return None
    digest_date = date_local or datetime.fromisoformat(now_utc.replace("Z", "+00:00")).date()
    return (digest_date - timedelta(days=cooldown_days)).isoformat()


def _lookback_since_utc(*, now_utc: str, days: int) -> str:
    now_dt = datetime.fromisoformat(now_utc.replace("Z", "+00:00"))
    return (now_dt - timedelta(days=days)).isoformat().replace("+00:00", "Z")


# 每个 adapter 在候选池里的保底名额。
# 2026-08-26 实测: 原实现是 `ORDER BY ts DESC LIMIT 300` 硬截断, 10 天窗内有 1906 条候选,
# 池子只装最新 300 条 —— 第 300 条的时间戳是 14 小时前。于是日更快讯(界面新闻一天 19 条)
# 霸占整个池子, 而周刊/深度/转录(财新 208 篇, 最新 8-24)在**打分之前**就被砍掉, 一条都进不了。
# 这不是权重问题(age 权重只有 0.05), 是池子选取问题, 所以给每个 adapter 留保底名额。
PER_ADAPTER_FLOOR = 25


def _select_rows(
    conn,
    *,
    pool_limit: int,
    lookback_since_utc: str | None = None,
    cooldown_since_digest_date: str | None = None,
    current_digest_date: str | None = None,
    per_adapter_floor: int = PER_ADAPTER_FLOOR,
):
    return conn.execute(
        """
        WITH pool AS (
        SELECT
          i.item_id,
          i.source,
          i.url,
          i.title,
          i.body,
          i.author,
          i.ts,
          i.lang,
          i.transcript,
          i.summary,
          i.tags_json,
          i.source_payload_json,
          i.media_manifest_relpath,
          i.item_status,
          i.embedding AS embedding_blob,
          COALESCE(st.trust_score, 0.35) AS trust_score,
          ROW_NUMBER() OVER (
            PARTITION BY substr(i.source, 1, instr(i.source || ':', ':') - 1)
            ORDER BY i.ts DESC
          ) AS rn_adapter,
          ROW_NUMBER() OVER (ORDER BY i.ts DESC) AS rn_global
        FROM items AS i
        LEFT JOIN source_trust AS st
          ON st.source = i.source
        -- 10-05 验收 P06: NOT IN 阻断 idx_items_status_ts(迁移 001/002 的 CHECK 取值域恰为
        -- new/digested/reviewed/promoted/rejected/deferred), 改成正向 IN ('new','digested')
        -- 走索引; 语义等价 = 6 个取值上判定逐一相同。
        WHERE i.item_status IN ('new', 'digested')
          AND NOT EXISTS (
            SELECT 1
            FROM promotion_events AS pe
            WHERE pe.item_id = i.item_id
          )
          AND COALESCE(json_extract(i.source_payload_json, '$.truncated'), 0) = 0
          AND (? IS NULL OR i.ts >= ?)
          AND (
            ? IS NULL OR NOT EXISTS (
              SELECT 1
              FROM digest_inclusions AS di
              WHERE di.item_id = i.item_id
                AND di.digest_kind = 'daily'
                AND di.digest_date >= ?
                AND (? IS NULL OR di.digest_date <> ?)
            )
          )
        )
        SELECT * FROM pool
        WHERE rn_adapter <= ? OR rn_global <= ?
        ORDER BY (rn_adapter <= ?) DESC, ts DESC
        LIMIT ?
        """,
        (
            lookback_since_utc,
            lookback_since_utc,
            cooldown_since_digest_date,
            cooldown_since_digest_date,
            current_digest_date,
            current_digest_date,
            per_adapter_floor,
            pool_limit,
            per_adapter_floor,
            pool_limit,
        ),
    ).fetchall()


def _select_transcript_rows(conn, *, cooldown_since_utc: str | None):
    return conn.execute(
        """
        SELECT
          i.item_id,
          i.source,
          i.url,
          i.title,
          i.body,
          i.author,
          i.ts,
          i.lang,
          i.summary,
          i.tags_json,
          i.source_payload_json,
          i.item_status,
          i.embedding AS embedding_blob,
          COALESCE(st.trust_score, 0.35) AS trust_score
        FROM items AS i
        LEFT JOIN source_trust AS st
          ON st.source = i.source
        WHERE i.adapter_name = 'local_transcripts'
          -- 10-05 验收 P06: 同 _select_rows, NOT IN → 正向 IN 走 idx_items_status_ts
          AND i.item_status IN ('new', 'digested')
          AND NOT EXISTS (
            SELECT 1
            FROM promotion_events AS pe
            WHERE pe.item_id = i.item_id
          )
          AND COALESCE(json_extract(i.source_payload_json, '$.truncated'), 0) = 0
          AND (
            ? IS NULL OR NOT EXISTS (
              SELECT 1
              FROM digest_inclusions AS di
              WHERE di.item_id = i.item_id
                AND di.included_at_utc >= ?
            )
          )
        ORDER BY i.ts DESC
        """,
        (cooldown_since_utc, cooldown_since_utc),
    ).fetchall()


def _row_to_transcript_candidate(row) -> dict:
    trust_score = float(row["trust_score"] or 0.35)
    payload = json.loads(row["source_payload_json"] or "{}")
    return {
        "item_id": row["item_id"],
        "source": row["source"],
        "url": row["url"],
        "title": row["title"],
        "body": row["body"],
        "author": row["author"],
        "ts_utc": row["ts"],
        "transcript": None,
        "summary": row["summary"],
        "tags": json.loads(row["tags_json"] or "[]"),
        "source_payload": payload,
        "media_manifest_relpath": None,
        "item_status": row["item_status"],
        "trust_score": trust_score,
        "embedding_blob": row["embedding_blob"],
        "digest_bucket": DIGEST_BUCKET_TRANSCRIPT,
    }


def _row_to_candidate(row) -> dict:
    trust_score = float(row["trust_score"] or 0.35)
    payload = json.loads(row["source_payload_json"] or "{}")
    return {
        "item_id": row["item_id"],
        "source": row["source"],
        "url": row["url"],
        "title": row["title"],
        "body": row["body"],
        "author": row["author"],
        "ts_utc": row["ts"],
        "transcript": row["transcript"],
        "summary": row["summary"],
        "tags": json.loads(row["tags_json"] or "[]"),
        "source_payload": payload,
        "media_manifest_relpath": row["media_manifest_relpath"],
        "item_status": row["item_status"],
        "trust_score": trust_score,
        "embedding_blob": row["embedding_blob"],
        "digest_bucket": DIGEST_BUCKET_LIVE,
    }


ALERT_ADAPTER = "disaster_alerts"
ALERT_LOOKBACK_HOURS = 24


def select_alert_candidates(conn, *, now_utc: str, lookback_hours: int = ALERT_LOOKBACK_HOURS) -> list[dict]:
    """「我的人所在地的灾害预警」——不进排序器, 按时间直取。

    profile 说这条 lane 的价值是能转发能行动, 不是情报: 它不跟别的内容抢分数,
    也不受 A 段 ≤3 的限额约束(限额是为了控阅读量, 而预警是 actionable 不是读物)。
    只排除已经进过**日报**的(digest_kind='daily'), 免得同一条预警天天重复;
    邮件推送另记 digest_kind='alert_email', **不**影响这里——邮件是投递,
    日报是反馈面(五个原因码在那儿), 同一条预警两边都该出现。
    """
    rows = conn.execute(
        """
        SELECT i.*, i.embedding AS embedding_blob,
               COALESCE(st.trust_score, 0.35) AS trust_score
        FROM items i
        LEFT JOIN source_trust st ON st.source = i.source
        WHERE i.adapter_name = ?
          AND i.ts >= datetime(?, ?)
          AND NOT EXISTS (
                SELECT 1 FROM digest_inclusions d
                WHERE d.item_id = i.item_id AND d.digest_kind = 'daily'
              )
        ORDER BY i.ts DESC
        """,
        (ALERT_ADAPTER, now_utc, f"-{int(lookback_hours)} hours"),
    ).fetchall()
    out = []
    for row in rows:
        cand = _row_to_candidate(row)
        cand["tier"] = TIER_ALERT
        out.append(cand)
    return out


def select_digest_candidates(
    conn,
    *,
    date_local: date | None,
    top_k: int,
    now_utc: str,
    live_cooldown_days: int = 1,
) -> list[dict]:
    """Legacy v1 ranker. Kept for `--ranking v1` and as deterministic fallback."""
    recompute_source_trust(conn, now_utc=now_utc)
    rows = _select_rows(
        conn,
        pool_limit=500,
        lookback_since_utc=_lookback_since_utc(now_utc=now_utc, days=10),
        cooldown_since_digest_date=_cooldown_since_digest_date(
            date_local=date_local,
            now_utc=now_utc,
            cooldown_days=live_cooldown_days,
        ),
        current_digest_date=date_local.isoformat() if date_local else None,
    )
    candidates: list[dict] = []
    for row in rows:
        cand = _row_to_candidate(row)
        cand["score_v1"] = score_item_v1(
            source_trust=cand["trust_score"], ts_utc=cand["ts_utc"], now_utc=now_utc
        )
        candidates.append(cand)
    candidates.sort(
        key=lambda c: sort_key_for_item(
            item_id=c["item_id"],
            source_trust=c["trust_score"],
            ts_utc=c["ts_utc"],
            now_utc=now_utc,
        )
    )
    for cand in candidates:
        cand.pop("embedding_blob", None)
    return candidates[:top_k]


def select_digest_candidates_v2(
    conn,
    *,
    date_local: date | None,
    top_k: int,
    now_utc: str,
    candidate_pool: int | None = None,
    summarize: bool = False,
    translate: bool = False,
    tier_top_k: tuple[int, int] | None = None,
    live_cooldown_days: int = 1,
    per_platform_cap: dict[str, int] | None = None,
) -> list[dict]:
    """Vault-aligned v2 ranker. Requires a fresh vault Qdrant collection (see embeddings.py)."""
    from personal_intel_loop import active_corpus, vault_corpus

    if candidate_pool is None:
        candidate_pool = max(top_k * 10, 2000)

    vault_corpus.assert_fresh()
    recompute_source_trust(conn, now_utc=now_utc)
    rows = _select_rows(
        conn,
        pool_limit=candidate_pool,
        lookback_since_utc=_lookback_since_utc(now_utc=now_utc, days=10),
        cooldown_since_digest_date=_cooldown_since_digest_date(
            date_local=date_local,
            now_utc=now_utc,
            cooldown_days=live_cooldown_days,
        ),
        current_digest_date=date_local.isoformat() if date_local else None,
    )
    candidates = [_row_to_candidate(row) for row in rows]
    if not candidates:
        return []

    _ensure_candidate_embeddings(conn, candidates)

    # 2) build active reference vectors once
    active_refs = active_corpus.build_active_references()

    # 3) score each candidate + classify tier by actual body length
    for cand in candidates:
        vec = cand["_embedding"]
        novelty = _clamp(1.0 - vault_corpus.max_similarity(vec))
        enrich_score = vault_corpus.max_similarity(vec, object_types=ENRICH_OBJECT_TYPES)
        active_score, active_ref = active_corpus.max_similarity_to_active(vec, active_refs)
        signals = ItemSignalsV2(
            novelty=novelty,
            active_relevance=active_score,
            enrich_score=enrich_score,
            source_trust=cand["trust_score"],
            ts_utc=cand["ts_utc"],
        )
        cand["signals_v2"] = signals
        cand["score_v2"] = score_item_v2(signals=signals, now_utc=now_utc)
        cand["novelty"] = novelty
        cand["active_relevance"] = active_score
        cand["enrich_score"] = enrich_score
        cand["tier"] = _candidate_tier(cand)
        cand["why_for_you"] = _pick_why_for_you(
            vec, active_ref, active_score, enrich_score
        )

    candidates.sort(
        key=lambda c: sort_key_for_item_v2(
            item_id=c["item_id"],
            signals=c["signals_v2"],
            now_utc=now_utc,
        )
    )

    # Tier-aware selection. candidates is already globally sorted by score_v2.
    # Dedup first (global, cross-tier) so same-event items from different sources
    # don't fill both longform and pulse slots.
    candidates = _mmr_dedup(candidates)

    # Split into pulse vs longform buckets, cap each by its own top-k,
    # then optionally run summarizer per bucket (order: longform first so LLM
    # attention lands on the deep-read items; pulse is cheap fill-in).
    if tier_top_k is None:
        # Default: longform 70 + pulse 30 (total 100),
        # reflecting that shorter content has lower signal density on average.
        pulse_cap = max(1, top_k * 30 // 100) if top_k else 30
        longform_cap = max(1, top_k - pulse_cap) if top_k else 70
    else:
        pulse_cap, longform_cap = tier_top_k

    platform_caps = DEFAULT_PER_PLATFORM_CAP if per_platform_cap is None else per_platform_cap
    # Shared across longform + pulse so a platform's cap is global to the digest,
    # not per-tier (otherwise youtube could take its full cap in each tier).
    platform_counts: dict[str, int] = {}

    def _platform_blocked(cand: dict) -> bool:
        platform = _platform_of(cand.get("source", ""))
        cap = platform_caps.get(platform)
        if cap is None:
            return False
        return platform_counts.get(platform, 0) >= cap

    def _platform_admit(cand: dict) -> None:
        platform = _platform_of(cand.get("source", ""))
        if platform in platform_caps:
            platform_counts[platform] = platform_counts.get(platform, 0) + 1

    pulse_bucket = [c for c in candidates if c.get("tier") == TIER_PULSE]
    longform_bucket = [c for c in candidates if c.get("tier") == TIER_LONGFORM]

    if translate:
        from personal_intel_loop.translator import needs_translation, translate_body

        def _add_translation(cand: dict) -> None:
            body = cand.get("body") or ""
            if needs_translation(body):
                result = translate_body(body, title=cand.get("title") or "")
                if result:
                    cand["translation"] = result

    if summarize:
        from personal_intel_loop.summarizer import summarize_candidate

        def _filter_bucket(bucket: list[dict], cap: int) -> list[dict]:
            out: list[dict] = []
            for cand in bucket:
                if len(out) >= cap:
                    break
                if _platform_blocked(cand):
                    continue
                summary = summarize_candidate(candidate=cand)
                if summary.error:
                    cand["llm_summary"] = None
                    cand["llm_error"] = summary.error
                    if translate:
                        _add_translation(cand)
                    _platform_admit(cand)
                    out.append(cand)
                    continue
                if summary.is_noise:
                    cand["_dropped_as_noise"] = summary.noise_category or "noise"
                    continue
                cand["llm_summary"] = {
                    "one_liner": summary.one_liner,
                    "why_for_you": summary.why_for_you,
                    "topic": summary.topic,
                    "profile_hit": summary.profile_hit,
                    "backstory": summary.backstory,
                    "so_what": summary.so_what,
                    "lane": summary.lane,
                    "saturated": summary.saturated,
                    "model": summary.model,
                    "backend": summary.backend,
                }
                _attach_claim(conn, cand, summary)
                if translate:
                    _add_translation(cand)
                _platform_admit(cand)
                out.append(cand)
            return out

        # Longform first — it's the higher-density tier and we want LLM budget
        # (and the user's attention) to land there first.
        longform_result = _filter_bucket(longform_bucket, longform_cap)
        pulse_result = _filter_bucket(pulse_bucket, pulse_cap)
    else:
        def _slice_bucket(bucket: list[dict], cap: int) -> list[dict]:
            out: list[dict] = []
            for cand in bucket:
                if len(out) >= cap:
                    break
                if _platform_blocked(cand):
                    continue
                _platform_admit(cand)
                out.append(cand)
            return out

        longform_result = _slice_bucket(longform_bucket, longform_cap)
        pulse_result = _slice_bucket(pulse_bucket, pulse_cap)
        if translate:
            for cand in longform_result + pulse_result:
                _add_translation(cand)

    # Concatenate longform before pulse so render_digest can emit them in
    # deep-read-first order. render_digest uses the `tier` field to place
    # section headers.
    result = longform_result + pulse_result

    for cand in result:
        cand.pop("_embedding", None)
        cand.pop("signals_v2", None)
    return result


def select_transcript_candidates_v2(
    conn,
    *,
    top_k: int,
    now_utc: str,
    shortlist_k: int = 600,
    summarize: bool = False,
    translate: bool = False,
    series_cap: int = 2,
    platform_cap: int = 8,
    cooldown_days: int = 45,
) -> list[dict]:
    """Rerank the full local transcript corpus as an evergreen backlog.

    Unlike live feeds, transcript backlog ranking removes age penalty and instead
    uses a digest-inclusion cooldown plus per-series/platform caps.
    """
    import numpy as np

    from personal_intel_loop import active_corpus, vault_corpus

    vault_corpus.assert_fresh()
    recompute_source_trust(conn, now_utc=now_utc)
    cooldown_since_utc = None
    if cooldown_days > 0:
        cooldown_since_utc = (
            datetime.fromisoformat(now_utc.replace("Z", "+00:00")) - timedelta(days=cooldown_days)
        ).isoformat().replace("+00:00", "Z")
    rows = _select_transcript_rows(conn, cooldown_since_utc=cooldown_since_utc)
    candidates = [_row_to_transcript_candidate(row) for row in rows]
    if not candidates:
        return []

    _ensure_candidate_embeddings(conn, candidates)

    active_refs = active_corpus.build_active_references()
    if active_refs:
        ref_matrix = np.stack([ref.vector for ref in active_refs], axis=0).astype("float32")
        cand_matrix = np.stack([cand["_embedding"] for cand in candidates], axis=0).astype("float32")
        scores = cand_matrix @ ref_matrix.T
        best_idx = scores.argmax(axis=1)
        best_scores = scores.max(axis=1)
        for cand, idx, score in zip(candidates, best_idx.tolist(), best_scores.tolist()):
            cand["_coarse_active_score"] = float(score)
            cand["_coarse_active_ref"] = active_refs[idx]
    else:
        for cand in candidates:
            cand["_coarse_active_score"] = 0.0
            cand["_coarse_active_ref"] = None

    candidates.sort(
        key=lambda c: (
            -float(c.get("_coarse_active_score", 0.0)),
            -float(c.get("trust_score", 0.0)),
            -datetime.fromisoformat(c["ts_utc"].replace("Z", "+00:00")).timestamp(),
            c["item_id"],
        )
    )
    shortlist = candidates[:shortlist_k] if shortlist_k > 0 else candidates

    for cand in shortlist:
        vec = cand["_embedding"]
        novelty = _clamp(1.0 - vault_corpus.max_similarity(vec))
        enrich_score = vault_corpus.max_similarity(vec, object_types=ENRICH_OBJECT_TYPES)
        active_score = float(cand.get("_coarse_active_score", 0.0))
        active_ref = cand.get("_coarse_active_ref")
        signals = ItemSignalsV2(
            novelty=novelty,
            active_relevance=active_score,
            enrich_score=enrich_score,
            source_trust=cand["trust_score"],
            ts_utc=cand["ts_utc"],
        )
        cand["signals_v2"] = signals
        cand["score_v2"] = score_backlog_item_v2(signals=signals)
        cand["novelty"] = novelty
        cand["active_relevance"] = active_score
        cand["enrich_score"] = enrich_score
        cand["tier"] = _candidate_tier(cand)
        cand["why_for_you"] = _pick_why_for_you(vec, active_ref, active_score, enrich_score)

    shortlist.sort(
        key=lambda c: sort_key_for_backlog_item_v2(
            item_id=c["item_id"],
            signals=c["signals_v2"],
        )
    )

    if translate:
        from personal_intel_loop.translator import needs_translation, translate_body as _translate_body

        def _add_trans(cand: dict) -> None:
            body = cand.get("body") or ""
            if needs_translation(body):
                result = _translate_body(body, title=cand.get("title") or "")
                if result:
                    cand["translation"] = result

    if summarize:
        from personal_intel_loop.summarizer import summarize_candidate

        filtered: list[dict] = []
        for cand in shortlist:
            summary = summarize_candidate(candidate=cand)
            if summary.error:
                cand["llm_summary"] = None
                cand["llm_error"] = summary.error
                if translate:
                    _add_trans(cand)
                filtered.append(cand)
                continue
            if summary.is_noise:
                cand["_dropped_as_noise"] = summary.noise_category or "noise"
                continue
            cand["llm_summary"] = {
                "one_liner": summary.one_liner,
                "why_for_you": summary.why_for_you,
                "topic": summary.topic,
                "profile_hit": summary.profile_hit,
                "backstory": summary.backstory,
                "so_what": summary.so_what,
                "lane": summary.lane,
                "saturated": summary.saturated,
                "model": summary.model,
                "backend": summary.backend,
            }
            _attach_claim(conn, cand, summary)
            if translate:
                _add_trans(cand)
            filtered.append(cand)
        shortlist = filtered
    elif translate:
        for cand in shortlist:
            _add_trans(cand)

    series_seen: dict[str, int] = {}
    platform_seen: dict[str, int] = {}
    selected: list[dict] = []
    for cand in shortlist:
        payload = cand.get("source_payload") or {}
        series_name = str(payload.get("series_name") or cand.get("author") or "").strip() or "(unknown)"
        platform = str(payload.get("platform") or cand["source"].split(":", 1)[-1]).strip() or "(unknown)"
        if series_cap > 0 and series_seen.get(series_name, 0) >= series_cap:
            continue
        if platform_cap > 0 and platform_seen.get(platform, 0) >= platform_cap:
            continue
        selected.append(cand)
        series_seen[series_name] = series_seen.get(series_name, 0) + 1
        platform_seen[platform] = platform_seen.get(platform, 0) + 1
        if len(selected) >= top_k:
            break

    for cand in selected:
        cand.pop("_embedding", None)
        cand.pop("signals_v2", None)
        cand.pop("_coarse_active_score", None)
        cand.pop("_coarse_active_ref", None)
    return selected


def _pick_why_for_you(item_vec, active_ref, active_score: float, enrich_score: float) -> dict | None:
    from personal_intel_loop import vault_corpus

    if active_ref is not None and active_score >= enrich_score and active_score > 0.35:
        return {
            "target_id": active_ref.object_id,
            "target_type": active_ref.object_type,
            "source_file": active_ref.source_file,
            "score": round(float(active_score), 3),
            "route": "active_layer",
        }
    if enrich_score > 0.45:
        hits = vault_corpus.query_top_k(
            item_vec, k=1, object_types=ENRICH_OBJECT_TYPES
        )
        if hits:
            h = hits[0]
            return {
                "target_id": h.note_id or h.title,
                "target_type": h.object_type,
                "source_file": h.source,
                "score": round(float(h.score), 3),
                "route": "enrich_mec_dia_heu",
            }
    return None


def _clamp(value: float) -> float:
    if value != value:
        return 0.0
    return max(0.0, min(1.0, float(value)))


def _local_doc_link(candidate: dict) -> tuple[str | None, str | None]:
    payload = candidate.get("source_payload") or {}
    local_path = str(payload.get("local_path") or "").strip()
    if not local_path:
        return None, None
    path = Path(local_path)
    try:
        return path.as_uri(), local_path
    except ValueError:
        return None, local_path


# Topic ordering for the compact push render. Derived from the single source of
# truth in summarizer (its tuple is already in display order, 其他 last) so the
# two can't drift. summarizer's module-level imports are light (no llm load).
from personal_intel_loop.summarizer import TOPIC_CATEGORIES as COMPACT_TOPIC_ORDER
_SOURCE_LABELS = {
    "rss_briefing": "RSS",
    "youtube_followed": "YouTube",
    "nikkei_cn": "日经",
    "bbc_zh": "BBC中文",
    "follow_builders": "Builder",
    "weibo_timeline": "微博",
    "xhs": "小红书",
    "local_transcripts": "转录",
}


def _source_label(source: str) -> str:
    platform = _platform_of(source)
    return _SOURCE_LABELS.get(platform, platform)


LANE_ZH = {"material": "外脑原料", "surprise": "意外/跨域", "track": "track record", "warmth": "人间温暖", "none": "—"}


def _context_lines(llm: dict) -> list[str]:
    """来龙去脉两行。设计约定: 只报"发生了 A"、不给前情与后果的孤立新闻不要看。
    两者皆缺时显式标出来, 让"这条没有脉络"本身可见, 而不是静默省略成一条干条目。"""
    back = (llm.get("backstory") or "").strip()
    fwd = (llm.get("so_what") or "").strip()
    if not back and not fwd:
        return ["- 来龙去脉: 缺(只报了事件本体)"]
    lines = []
    if back:
        lines.append(f"- 来龙: {back}")
    if fwd:
        lines.append(f"- 去脉: {fwd}")
    return lines


def _profile_line(llm: dict) -> str:
    """每条带一行: 它命中了 profile 的哪句、判进哪条 lane、是否饱和域——让判定可追溯到那页文本。"""
    lane = LANE_ZH.get(llm.get("lane") or "none", "—")
    hit = llm.get("profile_hit") or "无"
    tail = " · 饱和域" if llm.get("saturated") else ""
    return f"- profile: [{lane}] 命中「{hit}」{tail}"


def _alerts_header_line() -> str:
    """预警哨的活体信号。邮件门槛是橙/红(全国常态占比 16% / 0%), 所以**常态就是不响**——
    没有这行的话, "这季度确实没橙色预警" 和 "哨兵早就死了" 长得一模一样。"""
    import json as _json
    from personal_intel_loop import RUNS_DIR

    path = RUNS_DIR / "alerts_heartbeat.json"
    try:
        d = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "- alerts: ⚠️ 预警哨无心跳(从未跑过或写盘失败)"
    last = d.get("last_run_utc", "?")
    stale = ""
    try:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(last.replace("Z", "+00:00"))
        if delta > timedelta(hours=3):           # 30 分钟一轮, 3 小时没动静就是坏了
            stale = f" ⚠️ 已 {int(delta.total_seconds() // 3600)}h 未跑"
    except ValueError:
        stale = " ⚠️ 心跳时间戳无法解析"
    return (f"- alerts: 预警哨最后一轮 {last}{stale} · "
            f"该轮候选 {d.get('candidates_seen', '?')} 条 / 发信 {d.get('emails_sent', '?')} 条")


def _profile_header_line() -> str:
    from personal_intel_loop.profile import pending_proposals, profile_mtime_local, resolve_profile_path

    path = resolve_profile_path()  # fix-1007-N-7
    mtime = profile_mtime_local(path)
    if mtime is None:
        return "- profile: (缺失, 判定未带 profile)"
    return f"- profile: {path.name} (修改于 {mtime}) · 待接受提案 {len(pending_proposals(path))} 条"


def _attach_claim(conn, cand: dict, summary) -> None:
    """把摘要器抽出的可检验断言落到 claims 表, 并挂到候选上供渲染。无断言则什么都不做。"""
    claim = (getattr(summary, "checkable_claim", None) or "").strip()
    if not claim:
        return
    from personal_intel_loop.store import upsert_claim

    cand["claim"] = claim
    cand["claim_check_after"] = getattr(summary, "claim_check_after", None)
    cand["claim_id"] = upsert_claim(
        conn,
        item_id=cand["item_id"],
        source=cand["source"],
        claim=claim,
        check_after=cand["claim_check_after"],
    )


def render_compact_digest(*, date_local: date, candidates: list[dict]) -> str:
    """Human-facing, scannable digest for a push channel (see notify.py).

    Groups by the summarizer `topic` field and emits source-labelled items with
    their substantive one_liner. Decoupled from `render_digest`, which stays
    verbose because the Obsidian checkbox feedback scan parses its PIL_ITEM markers.
    """
    by_topic: dict[str, list[dict]] = {}
    for cand in candidates:
        llm = cand.get("llm_summary") or {}
        topic = llm.get("topic") or "其他"
        if topic not in COMPACT_TOPIC_ORDER:
            topic = "其他"
        by_topic.setdefault(topic, []).append(cand)

    lines = [f"📡 你的日报 {date_local.isoformat()} · {len(candidates)} 条"]
    for topic in COMPACT_TOPIC_ORDER:
        items = by_topic.get(topic)
        if not items:
            continue
        lines.append("")
        lines.append(f"【{topic}】")
        for cand in items:
            llm = cand.get("llm_summary") or {}
            label = _source_label(cand.get("source", ""))
            title = cand.get("title") or "(无标题)"
            lines.append(f"• [{label}] {title}")
            one_liner = (llm.get("one_liner") or "").strip()
            if one_liner and one_liner != title:
                lines.append(f"  {one_liner}")
    return "\n".join(lines)


def render_digest(
    *,
    date_local: date,
    candidates: list[dict],
    ranking_version: str = RANKING_V1,
) -> str:
    generated_at_local = datetime.now(LOCAL_TZ).isoformat(timespec="seconds")
    pulse_count = sum(1 for c in candidates if c.get("tier") == TIER_PULSE)
    longform_count = sum(1 for c in candidates if c.get("tier") == TIER_LONGFORM)
    transcript_count = sum(1 for c in candidates if c.get("digest_bucket") == DIGEST_BUCKET_TRANSCRIPT)
    lines = [
        f"<!-- PIL_DIGEST date={date_local.isoformat()} ranking_version={ranking_version} -->",
        f"# Intel Loop Digest {date_local.isoformat()}",
        "",
        f"- generated_at_local: {generated_at_local}",
        f"- top_k: {len(candidates)} (longform {longform_count} + pulse {pulse_count})",
        f"- transcript_backlog_items: {transcript_count}",
        f"- ranking_version: {ranking_version}",
        f"- store: {DB_PATH}",
        "- feedback_mode: obsidian_checkbox",
        _profile_header_line(),
        _alerts_header_line(),
        "- store_timestamps: UTC_Z",
        "",
    ]

    # B 段: 今天所有候选里抽出的可检验断言, 放在最前——这是时间会替用户判对错的那部分。
    claim_lines = [
        f"- [{_source_label(c.get('source', ''))}] {c['claim']} "
        f"(核验起点: {c.get('claim_check_after') or '未定'}) <!-- pil_claim={c.get('claim_id', '')} -->"
        for c in candidates
        if c.get("claim")
    ]
    if claim_lines:
        lines.extend(["## 可检验断言", "", *claim_lines, ""])

    last_tier: str | None = None
    last_bucket: str | None = None
    for candidate in candidates:
        bucket = candidate.get("digest_bucket", DIGEST_BUCKET_LIVE)
        if bucket != last_bucket:
            if bucket == DIGEST_BUCKET_TRANSCRIPT:
                lines.extend(["", "## 🎧 Transcript Backlog (本地转录库 / 全库 rerank)", ""])
            last_bucket = bucket
            last_tier = None
        tier = candidate.get("tier")
        if tier == TIER_ALERT and tier != last_tier:
            lines.extend(["", "## ⚠️ 预警 (你的人所在地 · 可直接转发)", ""])
            last_tier = tier
        if ranking_version == RANKING_V2 and tier in (TIER_LONGFORM, TIER_PULSE) and tier != last_tier:
            if tier == TIER_LONGFORM:
                lines.extend(["", "## 📖 Longform (坐下来读 / 深度对齐 vault)", ""])
            else:
                lines.extend(["", "## 🔁 Pulse (短内容 / 每日脉搏)", ""])
            last_tier = tier


        media_link, media_dir_link = _manifest_links(candidate.get("media_manifest_relpath"))
        local_doc_uri, local_doc_path = _local_doc_link(candidate)
        tags = ", ".join(candidate["tags"]) if candidate["tags"] else ""
        lines.extend(
            [
                f"<!-- PIL_ITEM_START item_id={candidate['item_id']} source={candidate['source']} -->",
                f"## [{candidate['item_id']}] {candidate['title']}",
                "",
                f"- source: {candidate['source']}",
                f"- trust: {candidate['trust_score']:.2f}",
                f"- ts_local: {_to_local_iso(candidate['ts_utc'])}",
            ]
        )
        if ranking_version == RANKING_V2 and "score_v2" in candidate:
            lines.extend(
                [
                    f"- score_v2: {candidate['score_v2']:.4f}",
                    f"- novelty: {candidate.get('novelty', 0):.3f}",
                    f"- active_relevance: {candidate.get('active_relevance', 0):.3f}",
                    f"- enrich_score: {candidate.get('enrich_score', 0):.3f}",
                ]
            )
            why = candidate.get("why_for_you")
            if why:
                lines.append(
                    f"- why_for_you: [{why['route']}] {why['target_id']} @ {why['score']}"
                )
            else:
                lines.append("- why_for_you: [novelty] (new-to-vault)")
            llm = candidate.get("llm_summary")
            if llm and llm.get("why_for_you"):
                lines.append(f"- llm_why: {llm['why_for_you']}")
            if llm:
                lines.extend(_context_lines(llm))
            if llm and llm.get("lane"):
                lines.append(_profile_line(llm))
        else:
            lines.append(f"- score_v1: {candidate.get('score_v1', 0):.4f}")
        lines.append(f"- tags: {tags}")
        lines.append("")

        if media_link:
            lines.extend([f"![]({media_link})", ""])

        llm = candidate.get("llm_summary") if ranking_version == RANKING_V2 else None
        if llm and llm.get("one_liner"):
            summary_text = llm["one_liner"]
        else:
            summary_text = candidate.get("summary") or candidate["title"]
        lines.extend(
            [
                summary_text[:2000],
                "",
                f"- [原链]({candidate['url']})",
            ]
        )
        translation = candidate.get("translation")
        if translation:
            lines.extend(
                [
                    "",
                    "**中文译文**",
                    "",
                    translation[:8000],
                    "",
                ]
            )
        if local_doc_uri:
            lines.append(f"- [本地文稿]({local_doc_uri})")
        if local_doc_path:
            lines.append(f"- local_path: `{local_doc_path}`")
        if media_dir_link:
            lines.append(f"- [本地媒体目录]({media_dir_link})")
        if candidate.get("claim"):
            lines.append(f"- 可检验断言: {candidate['claim']}")
        lines.extend(
            [
                "",
                "### Feedback",
                "- [ ] 🧠 早知道 <!-- pil_action=already_known -->",
                "- [ ] 🌫️ 没看懂 <!-- pil_action=unclear -->",
                "- [ ] 🚫 不感兴趣 <!-- pil_action=not_interested -->",
                "- [ ] 📌 留 <!-- pil_action=keep -->",
                "- [ ] 💬 深挖 (生成 staging/discuss_*.md 上下文包) <!-- pil_action=deep_discuss -->",
                "<!-- PIL_PROCESSED event_ids= scanned_at_utc= -->",
                "<!-- PIL_ITEM_END -->",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def write_digest(*, date_local: date, content: str, force: bool = False, staging_dir: Path = STAGING_DIR) -> Path:
    staging_dir.mkdir(parents=True, exist_ok=True)
    digest_path = staging_dir / f"intel_loop_digest_{date_local.isoformat()}.md"
    if digest_path.exists() and not force:
        raise FileExistsError(str(digest_path))
    digest_path.write_text(content, "utf-8")
    return digest_path


def _select_ranked_with_fallback(
    conn,
    *,
    date_local: date,
    top_k: int,
    now_utc: str,
    ranking: str = "auto",
    candidate_pool: int | None = None,
    summarize: bool = False,
    translate: bool = False,
    tier_top_k: tuple[int, int] | None = None,
    live_cooldown_days: int = 1,
) -> tuple[list[dict], str]:
    """Router: ranking='v2' | 'v1' | 'auto'. Returns (candidates, ranking_version_used).

    `auto` only falls back to v1 when the vault vectordb is explicitly stale
    (`VaultCorpusStale`). Any other exception propagates so real v2 regressions
    are not silently masked.
    """
    if ranking == "v1":
        return (
            select_digest_candidates(
                conn,
                date_local=date_local,
                top_k=top_k,
                now_utc=now_utc,
                live_cooldown_days=live_cooldown_days,
            ),
            RANKING_V1,
        )

    from personal_intel_loop.vault_corpus import VaultCorpusStale

    try:
        candidates = select_digest_candidates_v2(
            conn,
            date_local=date_local,
            top_k=top_k,
            now_utc=now_utc,
            candidate_pool=candidate_pool,
            summarize=summarize,
            translate=translate,
            tier_top_k=tier_top_k,
            live_cooldown_days=live_cooldown_days,
        )
        return candidates, RANKING_V2
    except VaultCorpusStale as exc:
        if ranking == "v2":
            raise
        print(f"[warn] vault corpus stale ({exc}); falling back to v1", file=sys.stderr)
        return (
            select_digest_candidates(
                conn,
                date_local=date_local,
                top_k=top_k,
                now_utc=now_utc,
                live_cooldown_days=live_cooldown_days,
            ),
            RANKING_V1,
        )


def select_candidates_with_fallback(conn, **kwargs):
    """排序结果之上前置预警段。

    预警绕开排序器(profile: 不参与打分、不受 A 段 ≤3 限额), 所以在收口处拼,
    而不是塞进 v1/v2 任一条排序路径里——那样要改两处且会被限额截掉。
    """
    candidates, ranking_version = _select_ranked_with_fallback(conn, **kwargs)
    alerts = select_alert_candidates(conn, now_utc=kwargs["now_utc"])
    return alerts + candidates, ranking_version
