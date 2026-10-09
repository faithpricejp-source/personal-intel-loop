"""本机投递箱(契约第 5 节 POST /api/paper/inbox)。

本机各项目把原来发邮件的通知投进报纸: 服务可达走 HTTP(web.py → accept),
不可达写本机 spool 文件, 服务启动后与每次出版前 drain_spool 补收。
去重: 同 dedup_key 24 小时内只收一次。title/body 超长截断(不拒绝 —— 预警类投递
丢条目比截断更糟, 与 fulltext 20000 字先例一致)。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from personal_intel_loop.schemas import normalize_dt_to_utc_z

PRIORITIES = ("urgent", "normal", "low")
TITLE_MAX = 300
BODY_MAX = 20000
DEDUP_WINDOW_HOURS = 24

# 投递方在服务不可达时写 *.json 到这里; TODAY_PAPER_SPOOL 可覆盖(测试用 tmp_path)。
# 本模块只读不写这个目录: done/、bad/ 子目录仅在 drain 移动文件时创建。
SPOOL_ENV_VAR = "TODAY_PAPER_SPOOL"
DEFAULT_SPOOL_DIR = Path.home() / "Library" / "Application Support" / "TodayPaper" / "spool"

# 版面显示名映射(契约 5 节 InboxItem.source_label); 未登记的 source 缺省 = source 原文
SOURCE_LABELS = {"pil.alerts": "灾害预警"}


def spool_dir() -> Path:
    """调用时解析: TODAY_PAPER_SPOOL 覆盖生效, 否则缺省投递箱路径。"""
    env = os.environ.get(SPOOL_ENV_VAR, "").strip()
    return Path(env) if env else DEFAULT_SPOOL_DIR


def source_label_for_inbox(source: str) -> str:
    return SOURCE_LABELS.get(source, source)


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def accept(conn: sqlite3.Connection, payload, *, now_utc: str) -> dict:
    """校验并收一条投递, 返回契约的 {"ok", "inbox_id", "deduped"}。

    24 小时内同 dedup_key 幂等: 返回已收那条的 inbox_id, deduped=True。
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    source = payload.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source is required")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title is required")
    body = payload.get("body")
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise ValueError("body must be a string")
    url = payload.get("url")
    if url is not None:
        if not isinstance(url, str):
            raise ValueError("url must be a string or null")
        url = url.strip() or None
    priority = payload.get("priority") or "normal"
    if priority not in PRIORITIES:
        raise ValueError(f"unsupported priority: {priority!r}")
    dedup_key = payload.get("dedup_key")
    if dedup_key is not None:
        if not isinstance(dedup_key, str):
            raise ValueError("dedup_key must be a string or null")
        dedup_key = dedup_key.strip() or None

    created_at = normalize_dt_to_utc_z(now_utc)
    if dedup_key:
        cutoff = normalize_dt_to_utc_z(_parse_utc(now_utc) - timedelta(hours=DEDUP_WINDOW_HOURS))
        row = conn.execute(
            "SELECT inbox_id FROM inbox WHERE dedup_key=? AND created_at > ? ORDER BY inbox_id DESC LIMIT 1",
            (dedup_key, cutoff),
        ).fetchone()
        if row is not None:
            return {"ok": True, "inbox_id": int(row["inbox_id"]), "deduped": True}

        # 10-05 审计 F17: 判重与落库合进单条原子语句——并发/快速重试下两个连接都查空时,
        # NOT EXISTS 在写入锁内再判一次, 保证同 key 24h 内只落一行
        cursor = conn.execute(
            "INSERT INTO inbox (source, title, body, url, priority, dedup_key, created_at, read_at)"
            " SELECT ?, ?, ?, ?, ?, ?, ?, NULL WHERE NOT EXISTS ("
            "SELECT 1 FROM inbox WHERE dedup_key=? AND created_at > ?)",
            (source.strip(), title[:TITLE_MAX], body[:BODY_MAX], url, priority, dedup_key, created_at, dedup_key, cutoff),
        )
        if cursor.rowcount == 0:
            row = conn.execute(
                "SELECT inbox_id FROM inbox WHERE dedup_key=? AND created_at > ? ORDER BY inbox_id DESC LIMIT 1",
                (dedup_key, cutoff),
            ).fetchone()
            conn.commit()
            return {"ok": True, "inbox_id": int(row["inbox_id"]), "deduped": True}
    else:
        cursor = conn.execute(
            "INSERT INTO inbox (source, title, body, url, priority, dedup_key, created_at, read_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (source.strip(), title[:TITLE_MAX], body[:BODY_MAX], url, priority, dedup_key, created_at),
        )
    conn.commit()
    return {"ok": True, "inbox_id": int(cursor.lastrowid), "deduped": False}


def mark_read(conn: sqlite3.Connection, inbox_id, *, now_utc: str) -> dict:
    """点开即标已读; 重复标读无害(覆盖 read_at)。未知 id → KeyError(web 层转 404)。"""
    # int() 对 bool/float 宽松(True→1, 1.9→1), {"inbox_id": true} 会误标 1 号已读;
    # 只放行整型与数字字符串, 其余一律 400
    if isinstance(inbox_id, bool) or not isinstance(inbox_id, (int, str)):
        raise ValueError(f"unsupported inbox_id: {inbox_id!r}")
    if isinstance(inbox_id, str) and not inbox_id.strip().isdigit():
        raise ValueError(f"unsupported inbox_id: {inbox_id!r}")
    try:
        inbox_id = int(inbox_id)
    except (TypeError, ValueError):
        raise ValueError(f"unsupported inbox_id: {inbox_id!r}") from None
    cursor = conn.execute(
        "UPDATE inbox SET read_at=? WHERE inbox_id=?",
        (normalize_dt_to_utc_z(now_utc), inbox_id),
    )
    conn.commit()
    if cursor.rowcount == 0:
        raise KeyError(f"unknown inbox_id: {inbox_id}")
    return {"ok": True}


def _move_unique(path: Path, dst_dir: Path) -> None:
    """移入 dst_dir; 同名冲突加序号后缀, 两份都保留(硬规则: 不删除)。"""
    dst_dir.mkdir(parents=True, exist_ok=True)
    target = dst_dir / path.name
    serial = 1
    while target.exists():
        target = dst_dir / f"{path.stem}.{serial}{path.suffix}"
        serial += 1
    path.replace(target)


def drain_spool(conn: sqlite3.Connection, spool_dir, *, now_utc: str) -> dict:
    """补收 spool 目录下每个 *.json: 入库成功移到 done/, 坏文件移到 bad/, 都不删除。

    目录不存在 = 没有积压, 原样返回(也不创建目录)。返回 {"ok", "accepted", "deduped", "bad"}。
    """
    root = Path(spool_dir)
    if not root.is_dir():
        return {"ok": True, "accepted": 0, "deduped": 0, "bad": 0}
    accepted = deduped = bad = 0
    for path in sorted(root.glob("*.json")):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text("utf-8"))
            # fix-1007-N-5: 无 dedup_key 的 spool 文件用文件名兜底。accept 已提交但
            # 移入 done/ 失败时，重扫命中同一 key，不再把同一文件插成第二行。
            if isinstance(payload, dict):
                raw_key = payload.get("dedup_key")
                # 只补缺省/空串; 非字符串的 dedup_key 仍交给 accept 判坏文件, 不改契约
                if raw_key is None or (isinstance(raw_key, str) and not raw_key.strip()):
                    payload = dict(payload)
                    payload["dedup_key"] = f"spool:{path.name}"  # fix-1007-N-5
            result = accept(conn, payload, now_utc=now_utc)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            _move_unique(path, root / "bad")
            bad += 1
            continue
        accepted += 1
        if result.get("deduped"):
            deduped += 1
        _move_unique(path, root / "done")
    return {"ok": True, "accepted": accepted, "deduped": deduped, "bad": bad}
