from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from personal_intel_loop import MIGRATIONS_DIR
from personal_intel_loop.schemas import (
    FeedbackEvent,
    Item,
    compute_content_hash,
    compute_digest_inclusion_id,
    normalize_dt_to_utc_z,
    sha1_hex,
    weight_for,
)


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    # 5s 不够: 定时 digest 要跑摘要器(每条数十秒), 与高频的 scan-feedback 撞车时
    # 后者拿不到写锁就抛 OperationalError, 静默吞掉一次 source_trust 更新
    # ——不报错但数据少一笔, 是最难发现的坏法。
    conn.execute("PRAGMA busy_timeout=120000")
    # WAL 下读不阻塞写, 写才互斥; NORMAL 把每次事务的 fsync 省掉, 缩短持锁窗口。
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get_user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def apply_migrations(conn: sqlite3.Connection) -> None:
    current = get_user_version(conn)
    for migration in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        version = int(migration.name.split("_", 1)[0])
        if version <= current:
            continue
        conn.executescript(migration.read_text("utf-8"))
        conn.execute(f"PRAGMA user_version={version}")
        current = version


def ensure_schema(conn: sqlite3.Connection) -> None:
    apply_migrations(conn)


def _bootstrap_source_trust(conn: sqlite3.Connection, *, source: str, adapter_name: str, now_utc: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO source_trust (
          source,
          adapter_name,
          trust_score,
          prior_score,
          prior_weight,
          updated_at
        )
        VALUES (?, ?, 0.35, 0.35, 12.0, ?)
        """,
        (source, adapter_name, now_utc),
    )


def ts_is_fallback(source_payload_json: str | None) -> bool:
    """payload 是否标了「ts 是兜底来的」。

    10-05 验收 G203：文章缺发布时间时 adapter 用 feed 生成时间兜底，这种 ts 不能覆盖库里已有的
    真实发布时间（否则 2026-05-02 入库的旧文会被刷成当天时间，反复以「新鲜」身份进日报）。
    """
    if not source_payload_json:
        return False
    try:
        payload = json.loads(source_payload_json)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("ts_is_fallback") is True


def upsert_item(conn: sqlite3.Connection, item: Item, *, adapter_name: str, source_payload_json: str) -> bool:
    now_utc = normalize_dt_to_utc_z(datetime.now(timezone.utc))
    created = conn.execute("SELECT 1 FROM items WHERE item_id=?", (item.id,)).fetchone() is None
    _bootstrap_source_trust(conn, source=item.source, adapter_name=adapter_name, now_utc=now_utc)
    conn.execute(
        """
        INSERT INTO items (
          item_id,
          source,
          url,
          title,
          body,
          author,
          ts,
          lang,
          embedding,
          transcript,
          summary,
          tags_json,
          source_payload_json,
          media_manifest_relpath,
          content_hash,
          adapter_name,
          first_ingested_at,
          last_seen_at,
          item_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, 'new')
        ON CONFLICT(item_id) DO UPDATE SET
          source = excluded.source,
          url = excluded.url,
          title = excluded.title,
          body = excluded.body,
          author = excluded.author,
          -- 10-05 验收附带发现 A：正文（body/transcript/title）哈希没变就保留已算好的向量。
          -- 原先恒写 excluded.embedding（绑定 None），每次重采都清空向量：local_transcripts 44,500 条
          -- 只剩 25 条有向量、nikkei 280/1,799、bbc 215/894。
          -- 不用 COALESCE(excluded.embedding, items.embedding)：那样正文被换掉后旧向量会与新正文对不上。
          embedding = CASE WHEN excluded.content_hash = items.content_hash THEN items.embedding ELSE NULL END,
          -- 10-05 验收 G203：兜底时间只在首次入库时用，已有条目的 ts 不覆盖。
          ts = CASE WHEN ? = 1 THEN items.ts ELSE excluded.ts END,
          lang = excluded.lang,
          transcript = excluded.transcript,
          summary = excluded.summary,
          tags_json = excluded.tags_json,
          source_payload_json = excluded.source_payload_json,
          content_hash = excluded.content_hash,
          adapter_name = excluded.adapter_name,
          last_seen_at = excluded.last_seen_at
        """,
        (
            item.id,
            item.source,
            item.url,
            item.title,
            item.body,
            item.author,
            normalize_dt_to_utc_z(item.ts),
            item.lang,
            None,
            item.transcript,
            item.summary,
            json.dumps(item.tags, ensure_ascii=False),
            source_payload_json,
            compute_content_hash(item),
            adapter_name,
            now_utc,
            now_utc,
            1 if ts_is_fallback(source_payload_json) else 0,
        ),
    )
    _sync_fts(conn, item)
    return created


def _sync_fts(conn: sqlite3.Connection, item: Item) -> None:
    """ingest 写入时同步全文检索索引(契约 6.2)。索引失败不影响条目写入:
    FTS 表缺失/损坏只丢检索能力, reindex 可重建; 在 ingest 的事务里吞掉异常,
    否则会把整个 upsert 事务一起回滚。"""
    # 10-05 修：items_fts 的 rowid 与 items.rowid 对齐，按 rowid 删改（O(log n)）。原先按 item_id 删——
    # item_id 是 UNINDEXED 列，每次 upsert 都全表扫正文，库大了以后一轮抓取能跑几小时、整段持写锁，
    # 出版与学习回路全部 database is locked。改了这里之后必须先 `pil archive-reindex` 一次（旧索引 rowid 未对齐）。
    try:
        row = conn.execute("SELECT rowid FROM items WHERE item_id=?", (item.id,)).fetchone()
        if row is None:
            return
        rid = row[0]
        conn.execute("DELETE FROM items_fts WHERE rowid=?", (rid,))
        conn.execute(
            "INSERT INTO items_fts (rowid, item_id, title, body) VALUES (?, ?, ?, ?)",
            (rid, item.id, segment_for_fts(item.title), segment_for_fts(item.body)),
        )
    except sqlite3.Error:
        pass


def reindex_fts(conn: sqlite3.Connection) -> int:
    """pil archive-reindex: 从 items 全量重建 items_fts。返回重建的条数。"""
    with conn:
        conn.execute("DELETE FROM items_fts")
        for row in conn.execute("SELECT rowid, item_id, title, body FROM items"):
            conn.execute(
                "INSERT INTO items_fts (rowid, item_id, title, body) VALUES (?, ?, ?, ?)",
                (row["rowid"], row["item_id"], segment_for_fts(row["title"]), segment_for_fts(row["body"])),
            )
    return int(conn.execute("SELECT COUNT(*) FROM items_fts").fetchone()[0])


_CJK_RANGES = (
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 基本区
    (0x3040, 0x30FF),  # 平假名/片假名
    (0x3405, 0x3405),
)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def _bigrams(chars: list[str]) -> list[str]:
    if len(chars) == 1:
        return chars  # 单字单独成 token, 查询侧用前缀匹配兜住
    return ["".join(chars[index : index + 2]) for index in range(len(chars) - 1)]


def segment_for_fts(text: str | None) -> str:
    """CJK 二元分词(FTS5 默认 unicode61 不切中文): 「央行加息」→「央行 行加 加息」,
    非 CJK 的连续字母数字按词保留(「Example title」→「Example title」)。
    写入与查询两侧用同一套切法, 否则拉丁词永远匹配不上。"""
    tokens: list[str] = []
    run: list[str] = []
    word: list[str] = []

    def _flush_run() -> None:
        if run:
            tokens.extend(_bigrams(run))
            run.clear()

    def _flush_word() -> None:
        if word:
            tokens.append("".join(word))
            word.clear()

    for char in str(text or ""):
        if _is_cjk(char):
            _flush_word()
            run.append(char)
        elif char.isalnum():
            _flush_run()
            word.append(char)
        else:
            _flush_run()
            _flush_word()
    _flush_run()
    _flush_word()
    return " ".join(tokens)


def fts_match_query(query: str) -> str:
    """把用户检索词转成 FTS5 MATCH 表达式(与 segment_for_fts 同一崖口):
    CJK 串 → 逐 bigram 短语 AND; 单个 CJK 字 → 前缀匹配; 非 CJK 字词 → 短语。"""
    parts: list[str] = []
    run: list[str] = []
    word: list[str] = []

    def _flush_run() -> None:
        if run:
            if len(run) == 1:
                parts.append(f'"{run[0]}"*')
            else:
                parts.extend(f'"{''.join(run[index: index + 2])}"' for index in range(len(run) - 1))
            run.clear()

    def _flush_word() -> None:
        if word:
            parts.append(f'"{''.join(word)}"')
            word.clear()

    for char in str(query or ""):
        if _is_cjk(char):
            _flush_word()
            run.append(char)
        elif char.isalnum():
            _flush_run()
            word.append(char)
        else:
            _flush_run()
            _flush_word()
    _flush_run()
    _flush_word()
    return " ".join(parts)


def count_future_items(
    conn: sqlite3.Connection,
    *,
    now_utc: str,
    tolerance_hours: int = 24,
) -> int:
    now_dt = datetime.fromisoformat(now_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
    cutoff = normalize_dt_to_utc_z(now_dt + timedelta(hours=tolerance_hours))
    row = conn.execute("SELECT COUNT(*) FROM items WHERE ts > ?", (cutoff,)).fetchone()
    return int(row[0])


EMBEDDING_DTYPE = "float32"


def set_item_embedding(conn: sqlite3.Connection, item_id: str, vector) -> None:
    import numpy as np

    arr = np.asarray(vector, dtype=EMBEDDING_DTYPE).ravel()
    conn.execute(
        "UPDATE items SET embedding=? WHERE item_id=?",
        (arr.tobytes(), item_id),
    )


def get_item_embedding(conn: sqlite3.Connection, item_id: str):
    import numpy as np

    row = conn.execute("SELECT embedding FROM items WHERE item_id=?", (item_id,)).fetchone()
    if row is None or row["embedding"] is None:
        return None
    return np.frombuffer(row["embedding"], dtype=EMBEDDING_DTYPE)


def set_media_manifest_relpath(conn: sqlite3.Connection, item_id: str, manifest_relpath: str | None) -> None:
    conn.execute(
        "UPDATE items SET media_manifest_relpath=? WHERE item_id=?",
        (manifest_relpath, item_id),
    )


def record_feedback_event(conn: sqlite3.Connection, event: FeedbackEvent) -> bool:
    row = conn.execute(
        "SELECT source FROM items WHERE item_id=?",
        (event.item_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown item_id: {event.item_id}")
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO promotion_events (
          event_id,
          item_id,
          source,
          event_type,
          event_weight,
          origin,
          digest_path,
          vault_object_type,
          vault_object_id,
          note,
          event_ts,
          created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
        """,
        (
            event.event_id,
            event.item_id,
            row["source"],
            event.action,
            weight_for(event.action),
            event.origin,
            event.digest_path,
            event.vault_target,
            event.note,
            normalize_dt_to_utc_z(event.event_ts),
            normalize_dt_to_utc_z(datetime.now(timezone.utc)),
        ),
    )
    return cursor.rowcount > 0


def mark_item_status(conn: sqlite3.Connection, item_id: str, status: str) -> None:
    conn.execute("UPDATE items SET item_status=? WHERE item_id=?", (status, item_id))


def mark_items_digested(conn: sqlite3.Connection, item_ids: list[str]) -> None:
    if not item_ids:
        return
    conn.executemany(
        "UPDATE items SET item_status='digested' WHERE item_id=?",
        [(item_id,) for item_id in item_ids],
    )


def replace_digest_inclusions(
    conn: sqlite3.Connection,
    *,
    digest_kind: str,
    digest_date: str,
    digest_path: str,
    item_rows: list[tuple[str, str]],
    included_at_utc: str,
) -> None:
    conn.execute(
        "DELETE FROM digest_inclusions WHERE digest_kind=? AND digest_date=?",
        (digest_kind, digest_date),
    )
    unique_rows = list(dict.fromkeys(item_rows))
    conn.executemany(
        """
        INSERT INTO digest_inclusions (
          inclusion_id,
          item_id,
          source,
          digest_kind,
          digest_date,
          digest_path,
          included_at_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                compute_digest_inclusion_id(
                    digest_kind=digest_kind,
                    digest_date=digest_date,
                    item_id=item_id,
                ),
                item_id,
                source,
                digest_kind,
                digest_date,
                digest_path,
                included_at_utc,
            )
            for item_id, source in unique_rows
        ],
    )


def recompute_source_trust(conn: sqlite3.Connection, *, now_utc: str) -> None:
    cutoff_dt = datetime.fromisoformat(now_utc.replace("Z", "+00:00")) - timedelta(days=90)
    cutoff_utc = normalize_dt_to_utc_z(cutoff_dt)
    now_z = normalize_dt_to_utc_z(now_utc)

    # 10-05 验收 P11: 改前每次调用都对 items 做 SELECT DISTINCT source, adapter_name 的
    # 全索引扫(14.5 万行 ~190ms, 每日 50+ 次调用)。但 items 的唯一写入口 upsert_item 总是
    # 先 _bootstrap_source_trust, 所以 items 的来源清单恒被 source_trust 覆盖, 这条建档
    # 扫描是纯冗余: 缺行的情况 upsert_item 当时就补上了。直接用 source_trust 自己的来源
    # 清单(下面的 source_rows), 本函数从「建档 + 重算」两步变成只重算。

    ingested_counts = {
        row["source"]: int(row["count_seen"])
        for row in conn.execute(
            """
            SELECT source, COUNT(*) AS count_seen
            FROM items
            WHERE first_ingested_at >= ?
            GROUP BY source
            """,
            (cutoff_utc,),
        )
    }

    digested_counts = {
        row["source"]: int(row["count_seen"])
        for row in conn.execute(
            """
            SELECT source, COUNT(*) AS count_seen
            FROM digest_inclusions
            WHERE included_at_utc >= ?
            GROUP BY source
            """,
            (cutoff_utc,),
        )
    }

    event_rows = conn.execute(
        """
        SELECT
          source,
          SUM(event_weight) AS weighted_feedback,
          SUM(CASE WHEN event_type='promote_to_src' THEN 1 ELSE 0 END) AS promote_src_90d,
          SUM(CASE WHEN event_type='promote_to_evd' THEN 1 ELSE 0 END) AS promote_evd_90d,
          SUM(CASE WHEN event_type='detected_jdg_reference' THEN 1 ELSE 0 END) AS cited_jdg_90d,
          SUM(CASE WHEN event_type='detected_cas_link' THEN 1 ELSE 0 END) AS linked_cas_90d,
          SUM(CASE WHEN event_type='detected_dec_link' THEN 1 ELSE 0 END) AS linked_dec_90d,
          SUM(CASE WHEN event_type='detected_mon_link' THEN 1 ELSE 0 END) AS linked_mon_90d,
          MAX(event_ts) AS last_event_ts
        FROM promotion_events
        WHERE event_ts >= ?
        GROUP BY source
        """,
        (cutoff_utc,),
    ).fetchall()
    event_map = {row["source"]: row for row in event_rows}

    source_rows = conn.execute(
        """
        SELECT source, adapter_name, prior_score, prior_weight
        FROM source_trust
        ORDER BY source
        """
    ).fetchall()

    for row in source_rows:
        source = row["source"]
        event_row = event_map.get(source)
        weighted_feedback = float(event_row["weighted_feedback"] or 0.0) if event_row else 0.0
        digested_seen = int(digested_counts.get(source, 0))
        prior_score = float(row["prior_score"])
        prior_weight = float(row["prior_weight"])
        denominator = prior_weight + digested_seen
        trust_score = (prior_weight * prior_score + weighted_feedback) / denominator if denominator else prior_score
        trust_score = max(0.0, min(1.0, trust_score))

        conn.execute(
            """
            UPDATE source_trust
            SET
              adapter_name=?,
              trust_score=?,
              ingested_items_seen_90d=?,
              digested_items_seen_90d=?,
              promote_src_90d=?,
              promote_evd_90d=?,
              cited_jdg_90d=?,
              linked_cas_90d=?,
              linked_dec_90d=?,
              linked_mon_90d=?,
              last_event_ts=?,
              updated_at=?
            WHERE source=?
            """,
            (
                row["adapter_name"],
                trust_score,
                int(ingested_counts.get(source, 0)),
                digested_seen,
                int(event_row["promote_src_90d"] or 0) if event_row else 0,
                int(event_row["promote_evd_90d"] or 0) if event_row else 0,
                int(event_row["cited_jdg_90d"] or 0) if event_row else 0,
                int(event_row["linked_cas_90d"] or 0) if event_row else 0,
                int(event_row["linked_dec_90d"] or 0) if event_row else 0,
                int(event_row["linked_mon_90d"] or 0) if event_row else 0,
                event_row["last_event_ts"] if event_row else None,
                now_z,
                source,
            ),
        )


def fetch_item(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()


def get_status_snapshot(conn: sqlite3.Connection, *, source: str | None = None) -> dict:
    params: tuple[str, ...] = ()
    sql = """
        SELECT source, trust_score, ingested_items_seen_90d, digested_items_seen_90d, updated_at
        FROM source_trust
    """
    if source:
        sql += " WHERE source=?"
        params = (source,)
    sql += " ORDER BY trust_score DESC, source ASC LIMIT 20"
    rows = conn.execute(sql, params).fetchall()
    return {
        "user_version": get_user_version(conn),
        "sources": [dict(row) for row in rows],
    }



# --- claims: 可检验断言 (RSS lane 的价值单位) ---


def upsert_claim(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    source: str,
    claim: str,
    check_after: str | None = None,
    extracted_at: str | None = None,
) -> str:
    """幂等写入一条可检验断言, 返回 claim_id = sha1(item_id|claim)。重复抽取不产生新行。"""
    claim_id = sha1_hex(f"{item_id}|{claim}")
    conn.execute(
        """
        INSERT OR IGNORE INTO claims (claim_id, item_id, source, claim, check_after, extracted_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            claim_id,
            item_id,
            source,
            claim,
            check_after,
            extracted_at or normalize_dt_to_utc_z(datetime.now(timezone.utc)),
        ),
    )
    return claim_id


def list_open_claims(conn: sqlite3.Connection, *, due_before: str | None = None) -> list[sqlite3.Row]:
    """未结算的断言; due_before 给 YYYY-MM-DD 时只列 check_after 已到期(或未定)的。"""
    if due_before is None:
        return conn.execute(
            "SELECT * FROM claims WHERE outcome IS NULL ORDER BY check_after IS NULL, check_after, extracted_at"
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM claims
        WHERE outcome IS NULL AND (check_after IS NULL OR check_after <= ?)
        ORDER BY check_after IS NULL, check_after, extracted_at
        """,
        (due_before,),
    ).fetchall()


def resolve_claim(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    outcome: str,
    note: str | None = None,
    resolved_at: str | None = None,
) -> bool:
    if outcome not in {"true", "false", "unresolvable"}:
        raise ValueError(f"unsupported claim outcome: {outcome}")
    cur = conn.execute(
        "UPDATE claims SET outcome=?, outcome_note=?, resolved_at=? WHERE claim_id=? AND outcome IS NULL",
        (outcome, note, resolved_at or normalize_dt_to_utc_z(datetime.now(timezone.utc)), claim_id),
    )
    return cur.rowcount == 1
