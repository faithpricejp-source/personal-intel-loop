"""预警推送 —— 抓到就发, 不等次日日报。

为什么单独一条投递路径:
傍晚发布的台风预警如果跟着次日早上的日报走, 读者看到时台风已经过去了。
这条 lane 的价值是**能提前行动**, 日报的投递周期会把这个价值吃掉。

去重用 digest_inclusions 表, digest_kind='alert_email'(表上 UNIQUE(item_id, kind, date)
天然幂等, 不新增 schema)。**不用 replace_digest_inclusions** —— 那个函数按 (kind,date)
先 DELETE 再插, 同一天内第二轮会把第一轮的记录抹掉, 于是已发过的又发一遍。这里必须只增不删。

与日报的关系: 邮件是投递, 日报是反馈面(五个原因码在那儿), 同一条预警两边都出现,
所以 select_alert_candidates 只看 digest_kind='daily', 不受本模块影响。

投递通道 channel='paper'(缺省)投报纸本机投递箱(paper_inbox.accept, priority=urgent);
channel='email' 走 notify.send_email(SMTP, 见 notify.py)。
两个通道共用 digest_inclusions(kind='alert_email')台账做"只发新的"水位。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from personal_intel_loop import LOCAL_TZ
from personal_intel_loop.schemas import compute_digest_inclusion_id

logger = logging.getLogger(__name__)

LOOKBACK_HOURS = 6          # 比日报的 24h 短: 这是"刚发生"的推送, 不是当天回顾
EMAIL_KIND = "alert_email"
MAX_PER_MAIL = 40

# 邮件是**打断**通道。采集门槛已经是橙(黄/蓝在 adapter 里就丢了),
# 所以对有等级词的条目这道门已经是冗余的; 留着是因为它还管**没写等级词**的条目
# (cn_level 返回 0): 那些进日报不进邮件。
# 日本侧不用再过滤: adapter 已经只收 警報/特別警報(丢了 注意報), 量级对应橙/红。
EMAIL_MIN_CN_LEVEL = 3      # 橙色


def email_worthy(source: str, title: str) -> bool:
    if ":jp:" in source:
        return True
    from personal_intel_loop.adapters.disaster_alerts import cn_level
    return cn_level(title) >= EMAIL_MIN_CN_LEVEL


def _pending(conn, *, now_utc: str, lookback_hours: int):
    return conn.execute(
        """
        SELECT i.item_id, i.source, i.title, i.body, i.url, i.ts
        FROM items i
        WHERE i.adapter_name = 'disaster_alerts'
          -- items.ts 是 'YYYY-MM-DDTHH:MM:SSZ' 格式, cutoff 必须归成同格式再比;
          -- datetime() 的空格分隔输出会让同日 0 点起、已超窗的条目被 'T'>' ' 误判进窗
          AND i.ts >= strftime('%Y-%m-%dT%H:%M:%SZ', ?, ?)
          AND NOT EXISTS (
                SELECT 1 FROM digest_inclusions d
                WHERE d.item_id = i.item_id AND d.digest_kind = ?
              )
        ORDER BY i.ts DESC, i.rowid  -- 同 ts 按入库顺序，别依赖扫描顺序（加索引后会变）
        """,
        (now_utc, f"-{int(lookback_hours)} hours", EMAIL_KIND),
    ).fetchall()


def _place(source: str) -> str:
    """disaster_alerts:cn:广州市 → 广州市"""
    return source.rsplit(":", 1)[-1]


def compose(rows) -> tuple[str, str]:
    """(subject, body)。主题要在锁屏通知里就能看懂是哪儿、多严重。"""
    places = sorted({_place(r["source"]) for r in rows})
    subject = f"⚠️ {'/'.join(places)} 预警 {len(rows)} 条"
    lines = []
    for place in places:
        lines.append(f"【{place}】")
        for r in rows:
            if _place(r["source"]) != place:
                continue
            local = datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).astimezone(LOCAL_TZ)
            lines.append(f"  {local:%m-%d %H:%M}  {r['title']}")
            lines.append(f"            {r['url']}")
        lines.append("")
    lines.append("—— 可直接转发。来源: 中央气象台 / 気象庁")
    return subject, "\n".join(lines)


def _mark_sent(conn, rows, *, now_utc: str) -> None:
    """只增不删(INSERT OR IGNORE): 同一天多轮推送, 后一轮不能抹掉前一轮的已发记录。"""
    date_local = datetime.now(LOCAL_TZ).date().isoformat()
    conn.executemany(
        """
        INSERT OR IGNORE INTO digest_inclusions
          (inclusion_id, item_id, source, digest_kind, digest_date, digest_path, included_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                compute_digest_inclusion_id(digest_kind=EMAIL_KIND, digest_date=date_local,
                                            item_id=r["item_id"]),
                r["item_id"], r["source"], EMAIL_KIND, date_local, "(email)", now_utc,
            )
            for r in rows
        ],
    )
    conn.commit()


def run(conn, *, now_utc: str | None = None, lookback_hours: int = LOOKBACK_HOURS,
        dry_run: bool = False, send=None, channel: str = "paper", accept=None) -> dict:
    if channel not in ("paper", "email"):
        raise ValueError(f"unsupported channel: {channel!r}")
    now_utc = now_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = _pending(conn, now_utc=now_utc, lookback_hours=lookback_hours)
    seen = len(rows)
    rows = [r for r in rows if email_worthy(r["source"], r["title"])]
    if not rows:
        # 心跳: 就算没得发也要留痕 —— 否则"一个季度不响"和"哨兵早就死了"长得一模一样,
        # 而这条 lane 的门槛高到常态就是不响。
        _heartbeat(now_utc=now_utc, seen=seen, sent=0)
        return {"pending": 0, "seen": seen, "sent": False, "reason": "no_new_alerts", "channel": channel}
    if channel == "email":
        rows = rows[:MAX_PER_MAIL]
    subject, body = compose(rows)
    if dry_run:
        # dry-run 必须在**第一个副作用之前**短路: 既不投递也不写台账。
        return {"pending": len(rows), "sent": False, "reason": "dry_run", "channel": channel,
                "subject": subject, "body": body}
    if channel == "email":
        if send is None:
            from personal_intel_loop.notify import send_email as send
        ok = send(subject, body)
        if not ok:
            # 发失败**不**标记已发, 下一轮(30 分钟后)重试。宁可重发一次, 不能漏发。
            logger.warning("alert email failed, will retry next run")
            return {"pending": len(rows), "sent": False, "reason": "send_failed", "channel": channel}
    else:
        # 缺省投报纸: 每条预警一条 inbox, priority=urgent,
        # dedup_key 沿用原来的去重键(item_id), 24 小时内重投由 inbox 幂等吸收。
        if accept is None:
            from personal_intel_loop.paper_inbox import accept as accept
        try:
            results = [
                accept(conn, {"source": "pil.alerts", "title": r["title"], "body": r["body"] or r["title"],
                              "url": r["url"], "priority": "urgent", "dedup_key": r["item_id"]},
                       now_utc=now_utc)
                for r in rows
            ]
        except Exception:
            # 投递失败**不**标记已发, 下一轮(30 分钟后)重试 —— 与邮件通道同纪律。
            logger.warning("alert inbox accept failed, will retry next run")
            return {"pending": len(rows), "sent": False, "reason": "accept_failed", "channel": channel}
    _mark_sent(conn, rows, now_utc=now_utc)
    _heartbeat(now_utc=now_utc, seen=seen, sent=len(rows))
    out = {"pending": len(rows), "seen": seen, "sent": True, "channel": channel,
           "places": sorted({_place(r["source"]) for r in rows})}
    if channel == "email":
        out["subject"] = subject
    else:
        out["accepted"] = sum(1 for r in results if not r.get("deduped"))
        out["deduped"] = sum(1 for r in results if r.get("deduped"))
    return out


HEARTBEAT_PATH = None       # 由 __init__ 的 RUNS_DIR 决定, 延迟解析便于测试覆写


def _heartbeat(*, now_utc: str, seen: int, sent: int) -> None:
    """每轮都写, 不管有没有发信。判据是"哨兵跑过没有", 不是"有没有信"。"""
    import json as _json
    from personal_intel_loop import RUNS_DIR
    path = HEARTBEAT_PATH or (RUNS_DIR / "alerts_heartbeat.json")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(
            {"last_run_utc": now_utc, "candidates_seen": seen, "emails_sent": sent},
            ensure_ascii=False), encoding="utf-8")
    except OSError as exc:                       # 心跳写不了不该让推送失败
        logger.warning("alerts heartbeat write failed: %s", exc)
