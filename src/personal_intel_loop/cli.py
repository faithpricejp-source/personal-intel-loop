from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from personal_intel_loop import DB_PATH, LOCAL_TZ, RUNS_DIR, STAGING_DIR, ensure_runtime_dirs
from personal_intel_loop.adapters import (
    FollowBuildersAdapter,
    LocalTranscriptsAdapter,
    NikkeiCnAdapter,
    RSSBriefingAdapter,
    WeiboTimelineAdapter,
)
from personal_intel_loop.digest import (
    render_compact_digest,
    render_digest,
    select_candidates_with_fallback,
    select_digest_candidates,
    select_transcript_candidates_v2,
    write_digest,
)
from personal_intel_loop.feedback import apply_feedback_scan, scan_feedback_files
from personal_intel_loop.legacy_import import import_weibo_feedback_log
from personal_intel_loop.media import ensure_staging_media_symlink, localize_item_media
from personal_intel_loop.schemas import FeedbackEvent, compute_feedback_event_id, normalize_dt_to_utc_z
from personal_intel_loop.store import (
    connect_db,
    count_future_items,
    ensure_schema,
    fetch_item,
    get_status_snapshot,
    mark_item_status,
    mark_items_digested,
    recompute_source_trust,
    record_feedback_event,
    replace_digest_inclusions,
    set_media_manifest_relpath,
    upsert_item,
)

# 10-05 验收 logging: 项目此前无任何 logging 配置, logger.info/warning 全部不输出
logger = logging.getLogger(__name__)


SUPPORTED_ADAPTER_NAMES = (
    "follow_builders",
    "rss_briefing",
    "weibo_timeline",
    "nikkei_cn",
    "local_transcripts",
    "youtube_followed",
    "bbc_zh",
    "aihot",
    "disaster_alerts",
    "weibo_home",
    "zhihu_moments",
    "weread_mp",
    "podcast_new",
    # 2026-10-05 报纸风险提示栏(paper_v2_contract 第 9 节)四个官方公开源
    "mofa_anzen",
    "who_don",
    "cn_consular",
    "enso_status",
    "thepaper_warm",
    "html_columns",
    "tokyo_events",
    "home_alerts",
)

FIRST_RING_ADAPTER_NAMES = (
    "rss_briefing",
    "follow_builders",
    "youtube_followed",
    "nikkei_cn",
    "bbc_zh",
    "weibo_timeline",
    "aihot",
    "local_transcripts",
    "disaster_alerts",
    # 需要登录态/本机产物的源（微博/知乎各 1 页、公众号读导出产物、播客读本机转录）
    "weibo_home",
    "zhihu_moments",
    "weread_mp",
    "podcast_new",
)


def _now_utc() -> str:
    return normalize_dt_to_utc_z(datetime.now(timezone.utc))


def _write_run_record(kind: str, payload: dict) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _now_utc().replace("-", "").replace(":", "")
    path = RUNS_DIR / f"{kind}_{stamp}.jsonl"
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", "utf-8")
    return path


def _load_adapter(name: str, *, pages: int | None = None, min_score: int | None = None):
    if name == "follow_builders":
        return FollowBuildersAdapter()
    if name == "rss_briefing":
        return RSSBriefingAdapter()
    if name == "weibo_timeline":
        return WeiboTimelineAdapter()
    if name == "nikkei_cn":
        return NikkeiCnAdapter()
    if name == "local_transcripts":
        return LocalTranscriptsAdapter()
    if name == "youtube_followed":
        from personal_intel_loop.adapters.youtube_followed import YouTubeFollowedAdapter
        return YouTubeFollowedAdapter()
    if name == "bbc_zh":
        from personal_intel_loop.adapters.bbc_zh import BBCZhAdapter
        return BBCZhAdapter()
    if name == "disaster_alerts":
        from personal_intel_loop.adapters.disaster_alerts import DisasterAlertsAdapter
        return DisasterAlertsAdapter()
    if name == "mofa_anzen":
        from personal_intel_loop.adapters.mofa_anzen import MofaAnzenAdapter
        return MofaAnzenAdapter()
    if name == "who_don":
        from personal_intel_loop.adapters.who_don import WhoDonAdapter
        return WhoDonAdapter()
    if name == "cn_consular":
        from personal_intel_loop.adapters.cn_consular import CnConsularAdapter
        return CnConsularAdapter()
    if name == "enso_status":
        from personal_intel_loop.adapters.enso_status import EnsoStatusAdapter
        return EnsoStatusAdapter()
    if name == "aihot":
        from personal_intel_loop.adapters.aihot import AihotAdapter
        kwargs = {}
        if pages is not None:
            kwargs["pages"] = pages
        if min_score is not None:
            kwargs["min_score"] = min_score
        return AihotAdapter(**kwargs)
    if name == "weibo_home":
        from personal_intel_loop.adapters.weibo_home import (
            DEFAULT_MAX_PAGES as WEIBO_MAX_PAGES,
            DEFAULT_SLEEP_S as WEIBO_SLEEP_S,
            DEFAULT_STATE_PATH as WEIBO_STATE_PATH,
            DEFAULT_STORAGE_STATE,
            WeiboHomeAdapter,
        )

        return WeiboHomeAdapter(
            state_path=WEIBO_STATE_PATH,
            storage_state=DEFAULT_STORAGE_STATE,
            max_pages=WEIBO_MAX_PAGES,
            sleep_s=WEIBO_SLEEP_S,
        )
    if name == "zhihu_moments":
        from personal_intel_loop.adapters.zhihu_moments import (
            DEFAULT_COOKIES_FILE,
            DEFAULT_MAX_PAGES as ZHIHU_MAX_PAGES,
            DEFAULT_SLEEP_S as ZHIHU_SLEEP_S,
            DEFAULT_STATE_PATH as ZHIHU_STATE_PATH,
            ZhihuMomentsAdapter,
        )

        return ZhihuMomentsAdapter(
            state_path=ZHIHU_STATE_PATH,
            cookies_file=DEFAULT_COOKIES_FILE,
            max_pages=ZHIHU_MAX_PAGES,
            sleep_s=ZHIHU_SLEEP_S,
        )
    if name == "weread_mp":
        from personal_intel_loop.adapters.weread_mp import (
            DEFAULT_MAX_ARTICLES,
            DEFAULT_PROFILE_DIR,
            DEFAULT_SLEEP_S as WEREAD_SLEEP_S,
            DEFAULT_STATE_PATH as WEREAD_STATE_PATH,
            WereadMpAdapter,
        )

        return WereadMpAdapter(
            state_path=WEREAD_STATE_PATH,
            profile_dir=DEFAULT_PROFILE_DIR,
            max_articles=DEFAULT_MAX_ARTICLES,
            sleep_s=WEREAD_SLEEP_S,
        )
    if name == "podcast_new":
        from personal_intel_loop.adapters.podcast_new import (
            DEFAULT_DAYS,
            DEFAULT_ROOTS,
            DEFAULT_STATE_PATH as PODCAST_STATE_PATH,
            PodcastNewAdapter,
        )

        return PodcastNewAdapter(
            DEFAULT_ROOTS,
            state_path=PODCAST_STATE_PATH,
            days=DEFAULT_DAYS,
        )
    if name == "html_columns":
        from personal_intel_loop.adapters.html_columns import HtmlColumnsAdapter

        return HtmlColumnsAdapter()
    if name == "tokyo_events":
        from personal_intel_loop.adapters.tokyo_events import TokyoEventsAdapter

        return TokyoEventsAdapter()
    if name == "home_alerts":
        from personal_intel_loop.adapters.home_alerts import HomeAlertsAdapter

        return HomeAlertsAdapter()
    if name == "thepaper_warm":
        from personal_intel_loop.adapters.thepaper_warm import ThepaperWarmAdapter

        return ThepaperWarmAdapter()
    raise SystemExit(f"unsupported adapter: {name}")


def _parse_optional_datetime(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _status_for_action(action: str) -> str | None:
    if action in {"promote_to_src", "promote_to_evd"}:
        return "promoted"
    if action in {"useful", "light", "deep_discuss", "more_like_this"}:
        return "reviewed"
    if action in {"spam_or_false", "less_like_this"}:
        return "rejected"
    if action == "later":
        return "deferred"
    return None


def _cmd_ingest(args: argparse.Namespace) -> int:
    conn = connect_db(DB_PATH)
    ensure_schema(conn)
    since = _parse_optional_datetime(args.since)
    now_utc = _now_utc()
    summary = _ingest_adapter(conn, adapter_name=args.adapter, since=since, limit=args.limit,
                               pages=getattr(args, "pages", None), min_score=getattr(args, "min_score", None))
    with conn:
        recompute_source_trust(conn, now_utc=now_utc)
    run_path = _write_run_record(
        "ingest",
        {
            "adapter": args.adapter,
            # 10-05 验收 C1: 单适配器运行记录也带 status/失败原因, degraded 不再看起来一切正常
            "status": summary["status"],
            "count": summary["collected"],
            "new_items": summary["new_items"],
            "updated_items": summary["updated_items"],
            "max_item_ts": summary["max_item_ts"],
            "errors": summary.get("errors", []),
            "since": args.since,
            "run_at_utc": now_utc,
        },
    )
    print(
        f"ingested {summary['collected']} items via {args.adapter} "
        f"(new={summary['new_items']}, updated={summary['updated_items']})"
    )
    print(run_path)
    return 0


ZERO_STREAK_PATH = RUNS_DIR / "adapter_zero_streaks.json"
# 没事时本来就 0 条的采集器（预警/风险/展演）：连续 0 条不告警
ZERO_IS_NORMAL = frozenset({
    "disaster_alerts", "home_alerts", "mofa_anzen", "who_don", "cn_consular", "enso_status", "tokyo_events",
})  # 测试经 conftest 改指临时目录


def _update_zero_streak(adapter_name: str, collected: int, *, state_path: Path | None = None) -> None:
    # 10-05 验收 C1: 连续 3 轮 0 条而此前非零 → warning。状态沿用 adapter 各自 state 文件的
    # 同款机制: 一个小 json 落在 RUNS_DIR, 每轮采集后更新。
    path = state_path or ZERO_STREAK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    import fcntl

    # 多个 launchd 进程并发更新这张表(见下方 R03 注释), 原子替换只防半截文件、
    # 不防读-改-写交错丢更新; 用 flock 把整个读-改-写串行化。
    lock_path = path.parent / (path.stem + ".lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            try:
                state = json.loads(path.read_text("utf-8"))
                if not isinstance(state, dict):
                    state = {}
            except (FileNotFoundError, json.JSONDecodeError):
                state = {}
            info = state.get(adapter_name) if isinstance(state.get(adapter_name), dict) else {}
            streak = int(info.get("zero_streak", 0))
            ever_nonzero = bool(info.get("ever_nonzero", False))
            if collected > 0:
                streak = 0
                ever_nonzero = True
            else:
                streak += 1
                if streak >= 3 and ever_nonzero and adapter_name not in ZERO_IS_NORMAL:
                    logger.warning(
                        "adapter %s collected 0 items for %d consecutive rounds (previously non-zero)",
                        adapter_name,
                        streak,
                    )
            state[adapter_name] = {
                "zero_streak": streak,
                "ever_nonzero": ever_nonzero,
                "updated_at_utc": _now_utc(),
            }
            # 10-05 复审 R03：多个 launchd 任务会同时更新这张表，先写临时文件再原子替换，避免半截文件读成空表
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False) + "\n", "utf-8")
            os.replace(tmp, path)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def _ingest_adapter(
    conn,
    *,
    adapter_name: str,
    since: datetime | None,
    limit: int | None,
    pages: int | None = None,
    min_score: int | None = None,
) -> dict:
    # first-ring 等路径不传这两个 aihot 专用参数; 保持 _load_adapter(name) 的单参调用形态,
    # 否则会打断既有测试对它的 monkeypatch。
    extra = {k: v for k, v in (("pages", pages), ("min_score", min_score)) if v is not None}
    adapter = _load_adapter(adapter_name, **extra)
    records = list(adapter.collect(since=since, limit=limit))
    summary = {
        "adapter": adapter_name,
        "status": "ok",
        "collected": 0,
        "new_items": 0,
        "updated_items": 0,
        "max_item_ts": None,
        "sources": [],
    }
    sources: set[str] = set()
    with conn:
        for record in records:
            created = upsert_item(
                conn,
                record.item,
                adapter_name=record.adapter_name,
                source_payload_json=record.source_payload_json,
            )
            summary["collected"] += 1
            if created:
                summary["new_items"] += 1
            else:
                summary["updated_items"] += 1
            sources.add(record.item.source)
            ts_utc = normalize_dt_to_utc_z(record.item.ts)
            if summary["max_item_ts"] is None or ts_utc > summary["max_item_ts"]:
                summary["max_item_ts"] = ts_utc
    summary["sources"] = sorted(sources)
    # 10-05 验收 C1: adapter 通过实例属性 last_errors: list[str] 报告本轮失败原因(逐轮在
    # collect 开头清空)。0 条且有失败原因 → degraded; 部分失败 → 状态仍 ok 但带 errors 进运行记录。
    last_errors = list(getattr(adapter, "last_errors", []) or [])
    if last_errors:
        summary["errors"] = last_errors
        if not records:
            summary["status"] = "degraded"
            logger.warning(
                "adapter %s collected 0 items with failure reasons: %s",
                adapter_name,
                "; ".join(last_errors),
            )
    _update_zero_streak(adapter_name, summary["collected"])
    return summary


def _cmd_ingest_first_ring(args: argparse.Namespace) -> int:
    adapter_names = tuple(args.adapters or FIRST_RING_ADAPTER_NAMES)
    conn = connect_db(DB_PATH)
    ensure_schema(conn)
    since = _parse_optional_datetime(args.since)
    now_utc = _now_utc()

    summaries: list[dict] = []
    for adapter_name in adapter_names:
        try:
            summary = _ingest_adapter(
                conn,
                adapter_name=adapter_name,
                since=since,
                limit=args.limit_per_adapter,
            )
        except Exception as exc:
            summary = {
                "adapter": adapter_name,
                "status": "error",
                "collected": 0,
                "new_items": 0,
                "updated_items": 0,
                "max_item_ts": None,
                "sources": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
        summaries.append(summary)

    with conn:
        recompute_source_trust(conn, now_utc=now_utc)
    future_item_count = count_future_items(conn, now_utc=now_utc)

    error_count = sum(1 for summary in summaries if summary["status"] != "ok")
    payload = {
        "run_kind": "first_ring_ingest",
        "adapter_order": list(adapter_names),
        "adapter_count": len(adapter_names),
        "total_collected": sum(int(summary["collected"]) for summary in summaries),
        "total_new_items": sum(int(summary["new_items"]) for summary in summaries),
        "total_updated_items": sum(int(summary["updated_items"]) for summary in summaries),
        "error_count": error_count,
        "future_item_count": future_item_count,
        "data_quality_status": "error" if future_item_count else "ok",
        "since": args.since,
        "limit_per_adapter": args.limit_per_adapter,
        "strict": args.strict,
        "run_at_utc": now_utc,
        "adapters": summaries,
    }
    run_path = _write_run_record("ingest_first_ring", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(run_path)
    return 1 if future_item_count or (args.strict and error_count) else 0


def _cmd_digest(args: argparse.Namespace) -> int:
    ensure_runtime_dirs()
    conn = connect_db(DB_PATH)
    ensure_schema(conn)
    digest_date = date.fromisoformat(args.date) if args.date else datetime.now(LOCAL_TZ).date()
    now_utc = _now_utc()
    tier_top_k: tuple[int, int] | None
    if args.pulse_top_k is not None and args.longform_top_k is not None:
        tier_top_k = (args.pulse_top_k, args.longform_top_k)
    elif args.pulse_top_k is not None or args.longform_top_k is not None:
        raise SystemExit("must set both --pulse-top-k and --longform-top-k together (or neither)")
    else:
        tier_top_k = None

    candidates, ranking_version = select_candidates_with_fallback(
        conn,
        date_local=digest_date,
        top_k=args.top_k,
        now_utc=now_utc,
        ranking=args.ranking,
        candidate_pool=args.candidate_pool,
        summarize=args.summarize,
        translate=args.translate,
        tier_top_k=tier_top_k,
        live_cooldown_days=args.live_cooldown_days,
    )
    if args.transcript_top_k > 0:
        if ranking_version != "v2_vault_aligned":
            print("[warn] transcript backlog requires v2; skipped because digest fell back to v1")
        else:
            transcript_candidates = select_transcript_candidates_v2(
                conn,
                top_k=args.transcript_top_k,
                now_utc=now_utc,
                shortlist_k=args.transcript_shortlist,
                summarize=args.summarize,
                translate=args.translate,
                series_cap=args.transcript_series_cap,
                platform_cap=args.transcript_platform_cap,
                cooldown_days=args.transcript_cooldown_days,
            )
            candidates.extend(transcript_candidates)

    ensure_staging_media_symlink(STAGING_DIR, DB_PATH.parent / "media")
    with conn:
        for candidate in candidates:
            row = fetch_item(conn, candidate["item_id"])
            if row is None:
                continue
            from personal_intel_loop.schemas import Item  # local import to avoid a circular import at module load time

            item = Item(
                id=row["item_id"],
                source=row["source"],
                url=row["url"],
                title=row["title"],
                body=row["body"],
                author=row["author"],
                ts=row["ts"],
                lang=row["lang"],
                transcript=row["transcript"],
                summary=row["summary"],
                tags=json.loads(row["tags_json"] or "[]"),
            )
            media_urls = list(candidate["source_payload"].get("media_urls", []))
            manifest_relpath = localize_item_media(item, media_urls, media_root=DB_PATH.parent / "media")
            if manifest_relpath:
                set_media_manifest_relpath(conn, item.id, manifest_relpath)
                candidate["media_manifest_relpath"] = manifest_relpath

    content = render_digest(date_local=digest_date, candidates=candidates, ranking_version=ranking_version)
    try:
        digest_path = write_digest(date_local=digest_date, content=content, force=args.force)
    except FileExistsError:
        print(f"digest already exists for {digest_date.isoformat()}")
        return 2

    included_at_utc = _now_utc()
    with conn:
        replace_digest_inclusions(
            conn,
            digest_kind="daily",
            digest_date=digest_date.isoformat(),
            digest_path=str(digest_path),
            item_rows=[(candidate["item_id"], candidate["source"]) for candidate in candidates],
            included_at_utc=included_at_utc,
        )
        mark_items_digested(conn, [candidate["item_id"] for candidate in candidates])

    print(digest_path)
    if args.epub:
        _write_epub(Path(digest_path), digest_date)
    if candidates:
        # Always render + print the compact view so the result is inspectable
        # (esp. the LLM-populated topic grouping) — the launchd .out captures it
        # for the scheduled run instead of it being fire-and-forget.
        compact = render_compact_digest(date_local=digest_date, candidates=candidates)
        print("\n----- compact push preview -----")
        print(compact)
        print("----- end compact -----")
        if args.push != "none":
            ok = _send_push(compact, channel=args.push)
            print(f"[push] {args.push}: {'sent' if ok else 'FAILED'} ({len(candidates)} items)")
    elif args.push != "none":
        print(f"[push] skipped: 0 candidates for {digest_date.isoformat()}")
    return 0


def _write_epub(digest_path: Path, digest_date: date) -> bool:
    """把当日完整 digest markdown 用 pandoc 转成 EPUB, 写到环境变量 PIL_EPUB_DIR 指定的目录
    (例如一个会同步到电子书阅读器的文件夹)。

    digest 里图片是 ./pil_media/... 相对路径 (相对 STAGING_DIR), 用 --resource-path=STAGING_DIR
    让其在 EPUB 内解析嵌入。失败不抛 — 不阻塞已成功的 digest 写入。
    """
    import os
    import shutil
    import subprocess

    out_dir_raw = os.environ.get("PIL_EPUB_DIR", "").strip()
    if not out_dir_raw:
        print("[epub] PIL_EPUB_DIR not set; skipped")
        return False
    pandoc = shutil.which("pandoc")
    if not pandoc:
        print("[epub] pandoc not found; skipped")
        return False
    try:
        out_dir = Path(out_dir_raw).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        epub_out = out_dir / f"pil-{digest_date.isoformat()}.epub"
        subprocess.run(
            [
                pandoc, str(digest_path), "-o", str(epub_out),
                "--metadata", f"title=个人情报日报 {digest_date.strftime('%m/%d')}",
                f"--resource-path={STAGING_DIR}",
                "--metadata", "lang=zh-CN",
                "--toc-depth=2",
            ],
            check=True, capture_output=True, timeout=300,
        )
        print(f"[epub] wrote {epub_out}")
        return True
    except Exception as exc:  # pragma: no cover - pandoc/runtime dependent
        print(f"[epub] FAILED (digest 已写, 不阻塞): {exc}")
        return False


def _send_push(text: str, *, channel: str) -> bool:
    """Send the compact digest via the configurable notify module (see notify.py).

    Returns False (and prints the error) on failure rather than raising, so a
    push problem never blocks the digest write that already succeeded.
    """
    from personal_intel_loop import notify

    try:
        ok = notify.send(text, channel, subject="personal-intel-loop digest")
    except Exception as exc:  # pragma: no cover - channel/runtime dependent
        print(f"[push] send failed: {exc}")
        return False
    if not ok:
        print("[push] send failed (see log)")
    return ok


def _cmd_promote_to_src(args: argparse.Namespace) -> int:
    conn = connect_db(DB_PATH)
    ensure_schema(conn)
    item = fetch_item(conn, args.item_id)
    if item is None:
        raise SystemExit(f"unknown item_id: {args.item_id}")
    event_ts = _now_utc()
    event = FeedbackEvent(
        event_id=compute_feedback_event_id(
            origin="cli",
            item_id=args.item_id,
            action="promote_to_src",
            event_ts_utc_iso=event_ts,
        ),
        item_id=args.item_id,
        action="promote_to_src",
        origin="cli",
        note=args.note,
        event_ts=event_ts,
    )
    with conn:
        record_feedback_event(conn, event)
        mark_item_status(conn, args.item_id, "promoted")
        recompute_source_trust(conn, now_utc=event_ts)
    snippet = "\n".join(
        [
            f"# SRC Draft: {item['title']}",
            "",
            f"- source: {item['source']}",
            f"- url: {item['url']}",
            f"- ts_utc: {item['ts']}",
            "",
            item["summary"] or item["body"][:800],
        ]
    )
    print(snippet)
    return 0


def _cmd_feedback(args: argparse.Namespace) -> int:
    conn = connect_db(DB_PATH)
    ensure_schema(conn)
    event_ts = _now_utc()
    event = FeedbackEvent(
        event_id=compute_feedback_event_id(
            origin="cli",
            item_id=args.item_id,
            action=args.action,
            event_ts_utc_iso=event_ts,
        ),
        item_id=args.item_id,
        action=args.action,
        origin="cli",
        note=args.note,
        event_ts=event_ts,
    )
    with conn:
        inserted = record_feedback_event(conn, event)
        status = _status_for_action(args.action)
        if inserted and status:
            mark_item_status(conn, args.item_id, status)
        recompute_source_trust(conn, now_utc=event_ts)
    print(f"feedback {'recorded' if inserted else 'already existed'}: {args.item_id} {args.action}")
    return 0


def _cmd_propose_profile_edits(args: argparse.Namespace) -> int:
    from personal_intel_loop.profile_proposals import propose_from_feedback

    conn = connect_db(DB_PATH)
    ensure_schema(conn)
    result = propose_from_feedback(conn, since_ts=args.since, limit=args.limit, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_claims(args: argparse.Namespace) -> int:
    from datetime import date as _date

    from personal_intel_loop.store import list_open_claims, resolve_claim

    conn = connect_db(DB_PATH)
    ensure_schema(conn)
    if args.resolve:
        if not args.outcome:
            print("--resolve 需要同时给 --outcome {true,false,unresolvable}")
            return 2
        with conn:
            ok = resolve_claim(conn, claim_id=args.resolve, outcome=args.outcome, note=args.note, resolved_at=_now_utc())
        print(f"claim {args.resolve[:12]} {'resolved=' + args.outcome if ok else 'not found or already resolved'}")
        return 0 if ok else 1
    rows = list_open_claims(conn, due_before=_date.today().isoformat() if args.due else None)
    for row in rows:
        print(f"{row['claim_id'][:12]}  [{row['source']}]  {row['claim']}  (核验起点: {row['check_after'] or '未定'})")
    print(f"{len(rows)} open claims")
    return 0


def _cmd_settle_backlog(args: argparse.Namespace) -> int:
    """结算积压批量初判。缺省 dry-run: 只读连库, 列将处理的断言与检索到的证据数, 不调模型不写库。"""
    import sqlite3
    from collections import Counter
    from datetime import datetime as _dt

    from personal_intel_loop.paper_settle import settle_backlog

    dry_run = args.dry_run or not args.execute
    if dry_run:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    else:
        conn = connect_db(DB_PATH)
        ensure_schema(conn)
    date_local = _dt.now(LOCAL_TZ).date()
    result = settle_backlog(
        conn, date_local=date_local, now_utc=_now_utc(), limit=args.limit, dry_run=dry_run,
        rejudge_before=args.rejudge_before,
    )
    for row in result["rows"]:
        tail = f"证据 {row['evidence_n']} 条" if dry_run else f"{row['verdict']}: {row['basis'][:60]}"
        print(f"{row['claim_id'][:12]}  到期 {row['check_after'] or '未定'}  {tail}  {row['claim'][:50]}")
    summary = {
        "dry_run": dry_run,
        "mode": result["mode"],
        "backlog_total": result["backlog_total"],
        "listed": len(result["rows"]),
        "processed": result["processed"],
        "by_due_year": dict(sorted(Counter((r["check_after"] or "未定")[:4] for r in result["rows"]).items())),
        "by_due_month": dict(sorted(Counter((r["check_after"] or "未定")[:7] for r in result["rows"]).items())),
    }
    if dry_run:
        summary["evidence_n_hist"] = dict(sorted(Counter(r["evidence_n"] for r in result["rows"]).items()))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _cmd_scan_feedback(args: argparse.Namespace) -> int:
    conn = connect_db(DB_PATH)
    ensure_schema(conn)
    digest_path = Path(args.digest) if args.digest else None
    paths = scan_feedback_files(staging_dir=STAGING_DIR, digest=digest_path)
    with conn:
        summary = apply_feedback_scan(conn, paths=paths, dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.propose and not args.dry_run:
        # 回路的写侧: 新原因码反馈 → profile 修改提案。水位保证幂等, 无新事件时不调模型。
        from personal_intel_loop.profile_proposals import propose_from_feedback

        result = propose_from_feedback(conn, limit=args.propose_limit)
        print(json.dumps({"profile_proposals": result}, ensure_ascii=False, indent=2))
    return 0


def _cmd_alerts_notify(args: argparse.Namespace) -> int:
    """抓一轮预警并投递: channel=paper(缺省)进报纸投递箱, email 发邮件。给 launchd 每 30 分钟跑。"""
    from personal_intel_loop.alerts_notify import run as notify_run

    conn = connect_db(DB_PATH)
    ensure_schema(conn)
    try:
        if not args.no_ingest:
            _ingest_adapter(conn, adapter_name="disaster_alerts", since=None, limit=None)
        result = notify_run(conn, lookback_hours=args.lookback_hours, dry_run=args.dry_run,
                            channel=args.channel)
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from personal_intel_loop.web import serve

    serve(host=args.host, port=args.port, date_iso=args.date)
    return 0


def _latest_run_path(prefix: str) -> str | None:
    stamp_prefix = f"{prefix}_"
    paths = sorted(
        path
        for path in RUNS_DIR.glob(f"{prefix}_*.jsonl")
        if path.name.startswith(stamp_prefix) and path.name[len(stamp_prefix):len(stamp_prefix) + 1].isdigit()
    )
    return str(paths[-1]) if paths else None


def _cmd_status(args: argparse.Namespace) -> int:
    conn = connect_db(DB_PATH)
    ensure_schema(conn)
    with conn:
        recompute_source_trust(conn, now_utc=_now_utc())
    snapshot = get_status_snapshot(conn, source=args.source)
    payload = {
        "user_version": snapshot["user_version"],
        "latest_ingest_run": _latest_run_path("ingest"),
        "latest_first_ring_ingest_run": _latest_run_path("ingest_first_ring"),
        "latest_feedback_scan_run": _latest_run_path("feedback_scan"),
        "sources": snapshot["sources"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_import_legacy_feedback(args: argparse.Namespace) -> int:
    if args.source != "weibo_log":
        raise SystemExit(f"unsupported legacy source: {args.source}")
    log_path = Path(args.path)
    if not log_path.exists():
        raise SystemExit(f"feedback log not found: {log_path}")
    conn = connect_db(DB_PATH)
    ensure_schema(conn)
    now_utc = _now_utc()
    with conn:
        result = import_weibo_feedback_log(conn, feedback_log_path=log_path)
        recompute_source_trust(conn, now_utc=now_utc)
    payload = {
        "source": args.source,
        "path": str(log_path),
        "scanned": result.scanned_rows,
        "inserted": result.inserted_rows,
        "skipped_missing_item": result.skipped_missing_item,
        "skipped_unsupported_action": result.skipped_unsupported_action,
        "skipped_bad_rows": result.skipped_bad_rows,
    }
    run_path = _write_run_record("import_legacy_feedback", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(run_path)
    return 0


def _cmd_detect_promotions(args: argparse.Namespace) -> int:
    """Scan vault for SRC/EVD/JDG/MEC/DIA/HEU/MON/DEC/CAS objects that cite pil items.

    Matches vault URLs (and inline item_id strings) against pil items.url / items.item_id,
    inserts `promotion_events` rows of type `detected_*_reference`. Idempotent.
    """
    from datetime import timedelta

    from personal_intel_loop import vault_scanner

    conn = connect_db(DB_PATH)
    ensure_schema(conn)

    since_utc: datetime | None
    if args.all:
        since_utc = None
    elif args.since:
        since_utc = datetime.fromisoformat(args.since.replace("Z", "+00:00")).astimezone(timezone.utc)
    else:
        # incremental: use persisted last-scan timestamp minus a 1h buffer for clock drift
        last = vault_scanner.load_last_scan_ts()
        since_utc = (last - timedelta(hours=1)) if last else None

    with conn:
        result = vault_scanner.scan_vault(
            conn,
            since_utc=since_utc,
            persist_last_scan=not args.dry_run,
        )
        if not args.dry_run:
            recompute_source_trust(conn, now_utc=_now_utc())

    payload = {
        "files_scanned": result.files_scanned,
        "citations_found": result.citations_found,
        "events_inserted": result.events_inserted,
        "events_skipped_dup": result.events_skipped_dup,
        "scan_started_at_utc": result.scan_started_at_utc,
        "scan_finished_at_utc": result.scan_finished_at_utc,
        "last_scan_persisted_to": result.last_scan_persisted_to,
        "since_utc": since_utc.isoformat() if since_utc else None,
        "dry_run": args.dry_run,
    }
    run_path = _write_run_record("detect_promotions", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(run_path)
    return 0


def _cmd_review_weekly(args: argparse.Namespace) -> int:
    conn = connect_db(DB_PATH)
    ensure_schema(conn)
    now_local = datetime.now(LOCAL_TZ)
    if args.week:
        year, week = args.week.split("-W")
        week_start = datetime.fromisocalendar(int(year), int(week), 1).replace(tzinfo=LOCAL_TZ)
    else:
        week_start = now_local - timedelta(days=7)
    week_end = week_start + timedelta(days=7)
    rows = conn.execute(
        """
        SELECT source, event_type, COUNT(*) AS count_seen
        FROM promotion_events
        WHERE event_ts >= ? AND event_ts < ?
        GROUP BY source, event_type
        ORDER BY count_seen DESC, source ASC
        """,
        (
            normalize_dt_to_utc_z(week_start.astimezone(timezone.utc)),
            normalize_dt_to_utc_z(week_end.astimezone(timezone.utc)),
        ),
    ).fetchall()
    label = args.week or now_local.strftime("%G-W%V")
    path = STAGING_DIR / f"intel_loop_review_{label}.md"
    lines = [f"# Intel Loop Review {label}", ""]
    if not rows:
        lines.append("No feedback events in this window.")
    else:
        current_source = None
        for row in rows:
            if row["source"] != current_source:
                current_source = row["source"]
                lines.extend(["", f"## {current_source}"])
            lines.append(f"- {row['event_type']}: {row['count_seen']}")
    path.write_text("\n".join(lines).rstrip() + "\n", "utf-8")
    print(path)
    return 0


def _cmd_paper(args: argparse.Namespace) -> int:
    """出版一天一期报纸。--dry-run 只打印入选清单与分区, 不写库、不抓网、不调模型。"""
    from personal_intel_loop import paper as paper_mod
    from personal_intel_loop.paper_ai import default_vault_lookup
    from personal_intel_loop.paper_leisure import default_authors_path, default_home_cinema_db

    conn = connect_db(DB_PATH)
    ensure_schema(conn)
    try:
        date_local = date.fromisoformat(args.date) if args.date else datetime.now(LOCAL_TZ).date()
        now_utc = _now_utc()
        if args.dry_run:
            plan = paper_mod.plan_edition(conn, date_local=date_local, n=args.n, blind_share=args.blind_share, now_utc=now_utc)
            print(f"paper dry-run {plan['date']}: picked={plan['picked']} blind={plan['blind']}")
            for section in ("lead", "top", "briefs", "blind"):
                entries = plan["sections"][section]
                print(f"[{section}] {len(entries)}")
                for entry in entries:
                    reason = f"  ← {entry['blind_reason']}" if entry.get("blind_reason") else ""
                    print(f"  {entry['item_id']}  {entry['title']}{reason}")
            return 0
        result = paper_mod.build_edition(
            conn, date_local=date_local, n=args.n, blind_share=args.blind_share, now_utc=now_utc,
            learn_first=not args.no_learn,
            vault_lookup=default_vault_lookup, home_cinema_db=default_home_cinema_db(),
            leisure_authors_path=default_authors_path(),
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        conn.close()


def _cmd_auth_check(args: argparse.Namespace) -> int:
    """跑一轮登录态探针并打印表格(契约 6.1, 随 scan-feedback 同款的定时任务调用)。"""
    from personal_intel_loop.paper_auth import auth_overview, default_registry, run_probes

    conn = connect_db(DB_PATH)
    ensure_schema(conn)
    try:
        run_probes(conn, [c for c in default_registry() if callable(c.get("probe"))], now_utc=_now_utc())
        header = f"{'key':<10} {'label':<10} {'ok':<6} {'checked_at':<25} {'since_failing':<25} detail"
        print(header)
        for channel in auth_overview(conn):
            print(
                f"{channel['key']:<10} {channel['label']:<10} {str(channel['ok']):<6} "
                f"{str(channel['checked_at'] or '-'):.<25} {str(channel['since_failing'] or '-'):.<25} {channel['detail'] or ''}"
            )
        return 0
    finally:
        conn.close()


def _cmd_archive_reindex(args: argparse.Namespace) -> int:
    """从 items 全量重建全文检索索引(契约 6.2)。"""
    from personal_intel_loop.store import reindex_fts

    conn = connect_db(DB_PATH)
    ensure_schema(conn)
    try:
        count = reindex_fts(conn)
        print(f"items_fts reindexed: {count} items")
        return 0
    finally:
        conn.close()


def _cmd_paper_distill(args: argparse.Namespace) -> int:
    """把攒下的 style/topic 评分蒸馏成 profile 修订提案(编辑部来信)。"""
    from personal_intel_loop import paper_distill

    conn = connect_db(DB_PATH)
    ensure_schema(conn)
    try:
        count = paper_distill.distill(conn, min_new=args.min_new)
        print(count)
        return 0
    finally:
        conn.close()


def _cmd_paper_learn(args: argparse.Namespace) -> int:
    """对指定日期(缺省昨天)的行为汇总跑 AI 学习: 调旋钮/写提案/写阅读注记。"""
    from personal_intel_loop import paper_learn

    conn = connect_db(DB_PATH)
    ensure_schema(conn)
    try:
        date_local = date.fromisoformat(args.date) if args.date else datetime.now(LOCAL_TZ).date() - timedelta(days=1)
        if args.dry_run:
            preview = paper_learn.learn_preview(conn, date_local=date_local, now_utc=_now_utc())
            if preview.get("skipped"):
                print(f"skipped: {preview['skipped']}")
                print(json.dumps(preview.get("summary", {}), ensure_ascii=False, indent=2))
                return 0
            print(json.dumps(preview.get("summary", {}), ensure_ascii=False, indent=2))
            print("----- 模型原始输出 -----")
            print(preview.get("raw") or preview.get("error") or "")
            return 0
        result = paper_learn.learn(conn, date_local=date_local, now_utc=_now_utc())
        print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pil")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest")
    ingest.add_argument("--adapter", default="rss_briefing", choices=SUPPORTED_ADAPTER_NAMES)
    ingest.add_argument("--pages", type=int, help="仅 aihot: 抓几页(每页 40 条, 上限 8 页覆盖 7 天全窗)")
    ingest.add_argument("--min-score", type=int, help="仅 aihot: finalScore 下限(默认 0 全收, 过滤交给 digest)")
    ingest.add_argument("--since")
    ingest.add_argument("--limit", type=int)
    ingest.set_defaults(func=_cmd_ingest)

    ingest_first_ring = subparsers.add_parser(
        "ingest-first-ring",
        help="run the first-ring source intake set and write per-adapter health counters",
    )
    ingest_first_ring.add_argument(
        "--adapter",
        dest="adapters",
        action="append",
        choices=SUPPORTED_ADAPTER_NAMES,
        help="adapter to include; may be repeated. Default is the full first-ring set.",
    )
    ingest_first_ring.add_argument("--since")
    ingest_first_ring.add_argument("--limit-per-adapter", type=int)
    ingest_first_ring.add_argument(
        "--strict",
        action="store_true",
        help="return non-zero if any adapter fails; default records errors and continues",
    )
    ingest_first_ring.set_defaults(func=_cmd_ingest_first_ring)

    digest = subparsers.add_parser("digest")
    digest.add_argument("--date")
    digest.add_argument("--top-k", type=int, default=15)
    digest.add_argument("--force", action="store_true")
    digest.add_argument(
        "--push",
        choices=("command", "email", "none"),
        default="none",
        help="after writing the staging digest, send a compact topic-grouped version via this channel (see notify.py)",
    )
    digest.add_argument(
        "--epub",
        action="store_true",
        help="把当日完整 digest 用 pandoc 转 EPUB 写到 PIL_EPUB_DIR",
    )
    digest.add_argument(
        "--ranking",
        choices=("auto", "v1", "v2"),
        default="auto",
        help="auto (default) prefers v2, falls back to v1 if vault vectordb stale",
    )
    digest.add_argument(
        "--candidate-pool",
        type=int,
        default=100,
        help="candidate pool size before v2 reranking",
    )
    digest.add_argument(
        "--summarize",
        action="store_true",
        help="call LLM summarizer for one_liner + why_for_you + noise filtering (v2 only)",
    )
    digest.add_argument(
        "--translate",
        action="store_true",
        help="pre-translate non-Chinese items to Chinese using the configured LLM (llm_backend)",
    )
    digest.add_argument(
        "--pulse-top-k",
        type=int,
        default=None,
        help="cap for short-content tier (< 500 chars). Default split: longform 70 + pulse 30 of --top-k.",
    )
    digest.add_argument(
        "--longform-top-k",
        type=int,
        default=None,
        help="cap for long-content tier (>= 500 chars). Default split: longform 70 + pulse 30 of --top-k.",
    )
    digest.add_argument(
        "--live-cooldown-days",
        type=int,
        default=1,
        help="skip live-feed items already included in the previous N digest days; same-day force rewrites are exempt",
    )
    digest.add_argument(
        "--transcript-top-k",
        type=int,
        default=0,
        help="append local transcript backlog picks (全库 rerank, default off for backwards compatibility)",
    )
    digest.add_argument(
        "--transcript-shortlist",
        type=int,
        default=600,
        help="pre-rerank shortlist size for transcript backlog after active-layer coarse scoring",
    )
    digest.add_argument(
        "--transcript-series-cap",
        type=int,
        default=2,
        help="max transcript picks per series in one digest",
    )
    digest.add_argument(
        "--transcript-platform-cap",
        type=int,
        default=8,
        help="max transcript picks per platform in one digest",
    )
    digest.add_argument(
        "--transcript-cooldown-days",
        type=int,
        default=45,
        help="skip transcript items already included within this many days",
    )
    digest.set_defaults(func=_cmd_digest)

    promote = subparsers.add_parser("promote-to-src")
    promote.add_argument("item_id")
    promote.add_argument("--note")
    promote.set_defaults(func=_cmd_promote_to_src)

    feedback = subparsers.add_parser("feedback")
    feedback.add_argument("item_id")
    feedback.add_argument("action")
    feedback.add_argument("--note")
    feedback.set_defaults(func=_cmd_feedback)

    propose = subparsers.add_parser("propose-profile-edits", help="把新原因码反馈翻成 profile 修改提案(追加到「待接受的修订」)")
    propose.add_argument("--dry-run", action="store_true")
    propose.add_argument("--limit", type=int, default=20)
    propose.add_argument("--since", help="只处理此 UTC 时间戳之后的反馈; 缺省用上次运行的水位")
    propose.set_defaults(func=_cmd_propose_profile_edits)

    claims = subparsers.add_parser("claims", help="可检验断言: 列未结算的 / 回填 outcome")
    claims.add_argument("--due", action="store_true", help="只列 check_after 已到期或未定的")
    claims.add_argument("--resolve", metavar="CLAIM_ID")
    claims.add_argument("--outcome", choices=("true", "false", "unresolvable"))
    claims.add_argument("--note")
    claims.set_defaults(func=_cmd_claims)

    backlog = subparsers.add_parser(
        "settle-backlog",
        help="结算积压(已到期但不在近期窗口/无到期日、无有效初判)批量初判; 缺省 dry-run 不调模型不写库",
    )
    backlog.add_argument("--limit", type=int, default=20)
    backlog.add_argument("--dry-run", action="store_true", help="缺省即 dry-run, 此旗标只为显式")
    backlog.add_argument("--execute", action="store_true", help="真跑: 每条调一次付费模型(deepseek-flash)写初判; 高峰时段先睡到空闲")
    backlog.add_argument(
        "--rejudge-before", metavar="DATE",
        help="改为重判: 初判 created_at 早于该日期(YYYY-MM-DD)的已到期未结断言(10-07 前是旧窗口初判)",
    )
    backlog.set_defaults(func=_cmd_settle_backlog)

    scan = subparsers.add_parser("scan-feedback")
    scan.add_argument("--dry-run", action="store_true")
    scan.add_argument("--digest")
    scan.add_argument("--propose", action="store_true", help="扫描后把新原因码反馈翻成 profile 修改提案")
    scan.add_argument("--propose-limit", type=int, default=20)
    scan.set_defaults(func=_cmd_scan_feedback)

    alerts = subparsers.add_parser("alerts-notify", help="抓一轮灾害预警并投递(paper 缺省进报纸投递箱, email 发邮件)")
    alerts.add_argument("--lookback-hours", type=int, default=6)
    alerts.add_argument(
        "--channel",
        choices=("paper", "email"),
        default="paper",
        help="投递通道: paper=报纸投递箱(urgent, 弹系统通知), email=邮件; 缺省 paper",
    )
    alerts.add_argument("--dry-run", action="store_true", help="只算不投不写台账")
    alerts.add_argument("--no-ingest", action="store_true", help="跳过采集, 只投库里已有的")
    alerts.set_defaults(func=_cmd_alerts_notify)

    serve = subparsers.add_parser("serve", help="在局域网浏览器提供日报反馈页面")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8766)
    serve.add_argument("--date")
    serve.set_defaults(func=_cmd_serve)

    status = subparsers.add_parser("status")
    status.add_argument("--source")
    status.set_defaults(func=_cmd_status)

    review = subparsers.add_parser("review-weekly")
    review.add_argument("--week")
    review.set_defaults(func=_cmd_review_weekly)

    detect = subparsers.add_parser(
        "detect-promotions",
        help="scan vault for SRC/EVD/JDG/... that cite pil items; insert detected_* events",
    )
    detect.add_argument("--since", help="ISO timestamp (UTC); default = last scan persisted")
    detect.add_argument("--all", action="store_true", help="ignore last-scan; walk everything")
    detect.add_argument("--dry-run", action="store_true", help="don't write events or persist last_scan")
    detect.set_defaults(func=_cmd_detect_promotions)

    legacy = subparsers.add_parser("import-legacy-feedback")
    legacy.add_argument("--source", default="weibo_log")
    legacy.add_argument(
        "--path",
        required=True,
        help="旧版微博编辑器导出的 feedback_log.jsonl 路径",
    )
    legacy.set_defaults(func=_cmd_import_legacy_feedback)

    paper_cmd = subparsers.add_parser("paper", help="出版一天一期的报纸(选条目/分区/全文/AI 预处理)")
    paper_cmd.add_argument("--date", help="东京本地日期 YYYY-MM-DD, 缺省今天")
    paper_cmd.add_argument("--n", type=int, default=60, help="一期总条数(含盲区版), 缺省 60")
    paper_cmd.add_argument("--blind-share", type=float, default=0.15, help="盲区版占比, 缺省 0.15")
    paper_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印入选清单与分区, 不写库、不抓网、不调模型(短路点在第一个副作用之前)",
    )
    paper_cmd.add_argument(
        "--no-learn",
        action="store_true",
        help="出版前不对前一天跑 AI 学习(缺省会先 learn 再出版)",
    )
    paper_cmd.set_defaults(func=_cmd_paper)

    paper_learn_cmd = subparsers.add_parser(
        "paper-learn",
        help="对前一天的行为汇总跑 AI 学习: 调旋钮/写编辑部提案/写「昨天你是怎么读的」",
    )
    paper_learn_cmd.add_argument("--date", help="行为日期 YYYY-MM-DD, 缺省昨天")
    paper_learn_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="打印行为汇总与模型原始输出, 不写任何表",
    )
    paper_learn_cmd.set_defaults(func=_cmd_paper_learn)

    paper_distill_cmd = subparsers.add_parser("paper-distill", help="把攒下的 style/topic 评分蒸馏成 profile 修订提案(编辑部来信)")
    paper_distill_cmd.add_argument("--min-new", type=int, default=3, help="少于这个未蒸馏评分数就不调模型, 缺省 3")
    paper_distill_cmd.set_defaults(func=_cmd_paper_distill)

    auth_check_cmd = subparsers.add_parser(
        "auth-check",
        help="跑一轮登录态探针并打印表格(真实探针由各平台接入方注册)",
    )
    auth_check_cmd.set_defaults(func=_cmd_auth_check)

    archive_reindex_cmd = subparsers.add_parser(
        "archive-reindex",
        help="从 items 全量重建全部来源检索索引(items_fts 标题+正文)",
    )
    archive_reindex_cmd.set_defaults(func=_cmd_archive_reindex)
    return parser


def _setup_logging() -> None:
    # 10-05 验收 logging: CLI 入口配置 logging(INFO), 否则各模块 logger.info/warning 全不输出;
    # 已有 handler(如被嵌入别的进程)不重复加。
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    ensure_runtime_dirs()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
