"""LAN-only digest reader and feedback console.

This deliberately has no authentication: bind to 127.0.0.1 unless the user
explicitly opts into 0.0.0.0 on a trusted LAN.
"""
from __future__ import annotations

import html
import json
import socket
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from personal_intel_loop import DB_PATH, LOCAL_TZ, RUNS_DIR, STAGING_DIR
from personal_intel_loop.feedback import _status_for_action
from personal_intel_loop.paper_api import (
    create_trip,
    decide_follow_suggestion,
    delete_trip,
    get_archive,
    get_archive_sources,
    get_editions,
    get_follow_suggestions,
    get_item,
    get_pipeline,
    get_trips,
    list_adjustments,
    notifications,
    revert_adjustment,
    set_pipeline,
)
from personal_intel_loop.paper_auth import relogin as auth_relogin
from personal_intel_loop.paper_events import store_events
from personal_intel_loop.paper_feedback import rate as paper_rate
from personal_intel_loop.paper_feedback import record_read
from personal_intel_loop.paper_inbox import accept as inbox_accept
from personal_intel_loop.paper_inbox import drain_spool, mark_read as inbox_mark_read
from personal_intel_loop.paper_inbox import spool_dir as inbox_spool_dir
from personal_intel_loop.profile import decide_proposal, load_profile, pending_proposals, resolve_profile_path
from personal_intel_loop.schemas import FeedbackEvent, compute_feedback_event_id, normalize_dt_to_utc_z
from personal_intel_loop.store import (
    connect_db,
    ensure_schema,
    fetch_item,
    list_open_claims,
    mark_item_status,
    recompute_source_trust,
    record_feedback_event,
    resolve_claim,
)
from personal_intel_loop.web_digest import DigestView, parse_digest
from personal_intel_loop.web_style import CSS


PAPER_WEB_DIR = Path(__file__).resolve().parent / "paper_web"
PAPER_STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


ALLOWED_ACTIONS = ("already_known", "unclear", "not_interested", "keep", "deep_discuss")
ACTION_LABELS = {
    "already_known": "🧠 早知道",
    "unclear": "🌫️ 没看懂",
    "not_interested": "🚫 不感兴趣",
    "keep": "📌 留",
    "deep_discuss": "💬 深挖",
}
CLAIM_OUTCOMES = ("true", "false", "unresolvable")


def _now_utc() -> str:
    return normalize_dt_to_utc_z(datetime.now(timezone.utc))


def _digest_path(date_iso: str | None = None) -> Path:
    if date_iso:
        path = STAGING_DIR / f"intel_loop_digest_{date_iso}.md"
        if not path.exists():
            raise FileNotFoundError(str(path))
        return path
    paths = sorted(STAGING_DIR.glob("intel_loop_digest_*.md"))
    if not paths:
        raise FileNotFoundError("no digest found")
    return paths[-1]


def _trust(conn, source: str) -> float:
    row = conn.execute("SELECT trust_score FROM source_trust WHERE source=?", (source,)).fetchone()
    return float(row["trust_score"]) if row else 0.35


def _recorded_actions(conn, item_id: str, digest_path: str) -> set[str]:
    rows = conn.execute(
        "SELECT event_type FROM promotion_events WHERE item_id=? AND (digest_path=? OR digest_path LIKE ?)",
        (item_id, digest_path, f"%{Path(digest_path).stem}%"),
    ).fetchall()
    return {str(row["event_type"]) for row in rows}


def handle_feedback(conn, payload: dict) -> dict:
    action = str(payload.get("action") or "")
    item_id = str(payload.get("item_id") or "")
    date_iso = str(payload.get("digest_date") or "")
    if action not in ALLOWED_ACTIONS:
        raise ValueError("unsupported action")
    if not item_id or not date_iso:
        raise ValueError("item_id and digest_date are required")
    item = fetch_item(conn, item_id)
    if item is None:
        raise KeyError(f"unknown item_id: {item_id}")
    before = _trust(conn, item["source"])
    event_ts = _now_utc()
    digest_path = str(STAGING_DIR / f"intel_loop_digest_{date_iso}.md")
    # 一条内容同时只能有一个原因码: 改主意 = 替换。
    # 实测: 用户先点"不感兴趣"(-0.5)又改点"深挖"(+1.5), 两条都留在库里同时生效。
    replaced = [
        row["event_type"]
        for row in conn.execute(
            "SELECT event_type FROM promotion_events WHERE item_id=? AND event_type IN ({}) AND event_type<>?".format(
                ",".join("?" for _ in ALLOWED_ACTIONS)
            ),
            (item_id, *ALLOWED_ACTIONS, action),
        )
    ]
    event = FeedbackEvent(
        event_id=compute_feedback_event_id(origin="web", digest_stem=Path(digest_path).stem, item_id=item_id, action=action),
        item_id=item_id, action=action, origin="web", digest_path=digest_path, event_ts=event_ts,
    )
    with conn:
        if replaced:
            conn.execute(
                "DELETE FROM promotion_events WHERE item_id=? AND event_type IN ({}) AND event_type<>?".format(
                    ",".join("?" for _ in ALLOWED_ACTIONS)
                ),
                (item_id, *ALLOWED_ACTIONS, action),
            )
        recorded = record_feedback_event(conn, event)
        if recorded:
            status = _status_for_action(action)
            if status:
                mark_item_status(conn, item_id, status)
            if action == "deep_discuss":
                from personal_intel_loop.discuss import emit_discuss_packet
                emit_discuss_packet(conn, item_id=item_id, staging_dir=STAGING_DIR)  # 运行期取模块变量，测试 monkeypatch 才生效（10-04 测试曾写进真 vault staging）
        recompute_source_trust(conn, now_utc=event_ts)
    return {
        "recorded": recorded,
        "replaced": replaced,
        "item_id": item_id,
        "action": action,
        "source": item["source"],
        "trust_before": before,
        "trust_after": _trust(conn, item["source"]),
    }


def handle_claim_resolve(conn, payload: dict) -> dict:
    outcome = str(payload.get("outcome") or "")
    if outcome not in CLAIM_OUTCOMES:
        raise ValueError("unsupported claim outcome")
    with conn:
        ok = resolve_claim(conn, claim_id=str(payload.get("claim_id") or ""), outcome=outcome, note=payload.get("note"), resolved_at=_now_utc())
    return {"ok": ok}


def handle_proposal(payload: dict, *, profile_path: Path | None = None) -> dict:
    if profile_path is None:
        profile_path = resolve_profile_path()  # fix-1007-N-7
    line, decision = str(payload.get("line") or ""), str(payload.get("decision") or "")
    from personal_intel_loop import source_proposals

    if source_proposals.is_source_line(line):
        # 信源提案：接受时落到抓取配置，不进画像正文（画像正文会进模型提示词）；两种决定都只从待接受区移除
        source_proposals.parse_source_line(line)  # 格式不对先报错，别把行删了
        remaining = decide_proposal(line, "reject", profile_path)
        applied = source_proposals.apply_decision(line, decision)
        return {"ok": True, "pending_count": remaining, **applied}
    remaining = decide_proposal(line, decision, profile_path)
    return {"ok": True, "pending_count": remaining}


def handle_profile_save(payload: dict, *, profile_path: Path | None = None, runs_dir: Path = RUNS_DIR) -> dict:
    if profile_path is None:
        profile_path = resolve_profile_path()  # fix-1007-N-7
    text = payload.get("text")
    if not isinstance(text, str):
        raise ValueError("text is required")
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    if profile_path.exists():
        (runs_dir / f"profile_backup_{stamp}.md").write_text(profile_path.read_text("utf-8"), "utf-8")
    profile_path.write_text(text, "utf-8")
    return {"ok": True}


def _e(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _proposal_card(line: str) -> str:
    # 原文形如: [头部] 依据: … → 目标: … → 建议: …, 拆成三段显示
    header, _, rest = line.partition("]")
    header = header.lstrip("- [")
    segs = []
    for part in rest.split(" → "):
        part = part.strip()
        for tag in ("依据:", "目标:", "建议:"):
            if part.startswith(tag):
                segs.append((tag.rstrip(":"), part[len(tag):].strip()))
                break
        else:
            if part:
                segs.append(("", part))
    rows = "".join(
        "<div class='pline'>" + (f"<span class='plabel'>{label}</span>" if label else "") + _e(text) + "</div>"
        for label, text in segs
    )
    # json.dumps 的双引号必须转义成 &quot;, 否则会提前截断 onclick="..." 属性
    payload = _e(json.dumps(line, ensure_ascii=False))
    return (
        f"<div class='proposal'><div class='meta'>{_e(header)}</div>{rows}"
        f"<div class='pactions'>"
        f"<button onclick=\"send('/api/proposal',{{line:{payload},decision:'accept'}},this)\">接受</button>"
        f"<button onclick=\"send('/api/proposal',{{line:{payload},decision:'reject'}},this)\">拒绝</button>"
        f"</div></div>"
    )


def _page(title: str, body: str) -> str:
    # JS 是独立常量, 避免 f-string 里大括号转义; send() 的网络逻辑与互斥逻辑不可动。
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_e(title)}</title><style>{CSS}</style></head>"
        f"<body><main>{body}</main><script>{_JS}</script></body></html>"
    )


_JS = """
async function send(url,payload,button){
  if(button){ if(button.dataset.busy) return; button.dataset.busy='1'; button.disabled=true;
    if(!button.dataset.label) button.dataset.label=button.textContent.replace(/ · 已记.*$/,'');
    button.textContent=button.dataset.label+' · 记录中…'; }
  let d=null,ok=false;
  try{ const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
        d=await r.json(); ok=r.ok&&!d.error; }catch(e){ d={error:String(e)}; }
  if(button){ delete button.dataset.busy; }
  if(!ok){ if(button){ button.disabled=false; button.textContent=button.dataset.label; }
            alert((d&&d.error)||'操作失败'); return; }
  if(button){
    button.classList.add('pressed'); button.textContent=button.dataset.label+' · 已记';
    // 原因码互斥: 后端已替换旧的, 前端把同卡片其他按钮恢复可点
    const card=button.closest('.card');
    if(card) card.querySelectorAll('.actions button').forEach(b=>{
      if(b!==button){ b.classList.remove('pressed'); b.disabled=false;
        if(b.dataset.label) b.textContent=b.dataset.label; }
    });
    const box=card&&card.querySelector('.delta');
    if(box&&d.trust_before!==undefined){
      const rep=(d.replaced&&d.replaced.length)?'(替换了「'+d.replaced.join('、')+'」) ':'';
      box.textContent='已记 '+rep+'· 此源信任 '+Number(d.trust_before).toFixed(2)+'→'+Number(d.trust_after).toFixed(2);
    }
  }
}
"""


def _today_local() -> str:
    return datetime.now(LOCAL_TZ).date().isoformat()


def _serve_paper_static(handler: "_Handler", name: str) -> None:
    """GET /paper/<file>: 只允许 paper_web 目录里的单层文件, 拒绝 `..`/子目录/隐藏文件; 不存在 404。"""
    if not name or "/" in name or "\\" in name or ".." in name or name.startswith("."):
        handler._json(404, {"error": "not found"})
        return
    path = PAPER_WEB_DIR / name
    if path.parent != PAPER_WEB_DIR or not path.is_file():
        handler._json(404, {"error": "not found"})
        return
    raw = path.read_bytes()
    content_type = PAPER_STATIC_TYPES.get(path.suffix.lower(), "application/octet-stream")
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _render_claims(view: DigestView, conn, *, open_only: bool = False, show_all: bool = False) -> str:
    if open_only:
        rows = list_open_claims(conn) if show_all else list_open_claims(conn, due_before=_today_local())
        claims = [{"claim_id": r["claim_id"], "source_label": r["source"], "text": r["claim"],
                   "check_after": r["check_after"] or "未定"} for r in rows]
        total = len(list_open_claims(conn))
        head = (f"<h2>断言台账 · 全部 {total} 条</h2><p class='muted'><a href='/claims'>只看到期</a></p>" if show_all
                else f"<h2>到期待核 {len(claims)} 条</h2><p class='muted'>未到期的不打扰你 · <a href='/claims?all=1'>看全部 {total} 条</a></p>")
    else:
        claims = view.claims
        head = "<h2>可检验断言</h2>"
    out = [head]
    for claim in claims:
        cid = _e(claim["claim_id"])
        out.append(
            f"<div class='claim'><div class='ctext'><b>[{_e(claim['source_label'])}]</b> {_e(claim['text'])}</div>"
            f"<div class='cdue'>核验起点: {_e(claim.get('check_after') or '未定')}</div><div class='actions'>"
        )
        for outcome, label in (("true", "真"), ("false", "假"), ("unresolvable", "无法判定")):
            out.append(f"<button onclick=\"send('/api/claims/resolve',{{claim_id:'{cid}',outcome:'{outcome}'}},this)\">{label}</button>")
        out.append("</div></div>")
    return "".join(out) if claims else head + "<p class='notice'>没有到期的断言, 不用管。</p>"


_TIER_HEADINGS = (
    ("alert", "⚠️ 预警"),
    ("longform", "📖 Longform"),
    ("pulse", "🔁 Pulse"),
    ("transcript_backlog", "🎧 Transcript Backlog"),
    (None, "其他"),
)


def _render_card(item: dict, recorded: set[str], view: DigestView) -> str:
    buttons = []
    for action, label in ACTION_LABELS.items():
        disabled = " disabled class='pressed'" if action in recorded else ""
        suffix = " · 已记" if action in recorded else ""
        buttons.append(f"<button{disabled} onclick=\"send('/api/feedback',{{item_id:'{_e(item['item_id'])}',action:'{action}',digest_date:'{view.date}'}},this)\">{label}{suffix}</button>")

    if item["url"]:
        title = f"<a href='{_e(item['url'])}' target='_blank' rel='noopener'>{_e(item['title'])}</a>"
    else:
        title = _e(item["title"])

    # 第二层: 来龙 / 去脉。正文字号, 不灰, 去脉略重; context_missing 用警示色, 不静默省略。
    ctx = []
    if item.get("backstory"):
        ctx.append(f"<p class='ctx back'><span class='tag'>来龙</span>{_e(item['backstory'])}</p>")
    if item.get("so_what"):
        ctx.append(f"<p class='ctx so'><span class='tag'>去脉</span>{_e(item['so_what'])}</p>")
    if item.get("context_missing"):
        ctx.append("<p class='ctx missing'>⚠ 来龙去脉: 缺(只报了事件本体)</p>")

    summary = f"<div class='summary'>{_e(item['summary'])}</div>" if item["summary"] else ""
    translation = (
        f"<details><summary>展开译文</summary><div class='summary'>{_e(item['translation'])}</div></details>"
        if item["translation"] else ""
    )

    # 第四层: profile 命中行 / 断言 / 来源, 最小最灰
    meta = [f"<span class='src'>{_e(item['source'])}</span>"]
    if item.get("profile_line"):
        meta.append(_e(item["profile_line"]))
    if item.get("claim"):
        meta.append(f"可检验断言: {_e(item['claim'])}")

    return (
        f"<article class='card'><h3>{title}</h3>{''.join(ctx)}{summary}{translation}"
        f"<div class='meta'>{' · '.join(meta)}</div>"
        f"<div class='actions'>{''.join(buttons)}</div><div class='delta'></div></article>"
    )


def _render_digest(view: DigestView, conn, digest_path: Path) -> str:
    # 断言不在日报页铺开: 它的价值在到期那天, 不在抽出来那天。
    # 可检验断言当天无法判定真假 —— 所以
    # 31 条里只有 3 条到期、17 条未到期、11 条压根没有核验期, 全塞给他判就是噪音。
    due = list_open_claims(conn, due_before=_today_local())
    pending = len(list_open_claims(conn))
    if due:
        claim_line = f"<p class='notice'><a href='/claims'><b>{len(due)} 条断言到期待核</b></a> · 另有 {pending - len(due)} 条未到期</p>"
    else:
        claim_line = f"<p class='notice'><a href='/claims'>断言台账</a> · {pending} 条在等, 今天无到期</p>"
    body = [
        "<p class='topnav'><a href='/profile'>Profile</a><a href='/claims'>断言台账</a></p>"
        f"<h1>日报 {_e(view.date)}</h1>"
        f"<p class='pageline'>{len(view.items)} 条 · ranking {_e(view.ranking_version)} · {_e(view.profile_status)}</p>",
        claim_line,
    ]
    for tier, heading in _TIER_HEADINGS:
        items = [item for item in view.items if item["tier"] == tier]
        if not items:
            continue
        body.append(f"<h2>{heading}</h2>")
        for item in items:
            recorded = _recorded_actions(conn, item["item_id"], str(digest_path))
            body.append(_render_card(item, recorded, view))
    return _page(f"日报 {view.date}", "".join(body))


class _Handler(BaseHTTPRequestHandler):
    server_version = "PILWeb/0.1"
    def _json(self, status: int, value: object) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8"); self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def _html(self, value: str, status: int = 200) -> None:
        raw = value.encode("utf-8"); self.send_response(status); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def _payload(self) -> dict:
        size = int(self.headers.get("Content-Length", "0")); return json.loads(self.rfile.read(size) or b"{}")
    def do_GET(self) -> None:
        conn = connect_db(DB_PATH); ensure_schema(conn)
        try:
            parsed = urlparse(self.path); route = parsed.path
            if route == "/":
                path = _digest_path((parse_qs(parsed.query).get("date") or [None])[0]); self._html(_render_digest(parse_digest(path.read_text("utf-8")), conn, path))
            elif route == "/profile":
                proposals = pending_proposals()
                cards = []
                for line in proposals:
                    cards.append(_proposal_card(line))
                entries = "".join(cards) or "<p class='notice'>暂无待接受修订</p>"
                self._html(_page("Profile", "<p class='topnav'><a href='/'>返回日报</a></p><h1>Reading profile</h1><h2>待接受修订</h2>" + entries + "<h2>编辑 profile</h2><textarea id='profile'>" + _e(load_profile()) + "</textarea><p><button class='savebtn' onclick=\"send('/api/profile/save',{text:document.getElementById('profile').value},this)\">保存</button></p>"))
            elif route == "/claims":
                show_all = parse_qs(parsed.query).get("all", ["0"])[0] == "1"
                self._html(_page("Claims", "<p><a href='/'>返回日报</a></p>" + _render_claims(DigestView("", ""), conn, open_only=True, show_all=show_all)))
            elif route in ("/paper", "/paper/"):
                _serve_paper_static(self, "index.html")
            elif route.startswith("/paper/"):
                _serve_paper_static(self, unquote(route[len("/paper/"):]))
            elif route == "/api/paper/editions":
                query = parse_qs(parsed.query)
                try:
                    self._json(200, get_editions(conn, before=(query.get("before") or [None])[0], limit=(query.get("limit") or [1])[0]))
                except KeyError as exc:
                    self._json(404, {"error": str(exc)})
            elif route.startswith("/api/paper/item/"):
                try:
                    item_id = unquote(route.split("/api/paper/item/", 1)[1])
                    self._json(200, get_item(conn, item_id))
                except KeyError as exc:
                    self._json(404, {"error": str(exc)})
            elif route == "/api/paper/pipeline":
                self._json(200, get_pipeline(conn))
            elif route == "/api/paper/adjustments":
                limit = (parse_qs(parsed.query).get("limit") or [50])[0]
                self._json(200, list_adjustments(conn, limit=limit))
            elif route == "/api/paper/notifications":
                since = (parse_qs(parsed.query).get("since") or [None])[0]
                self._json(200, notifications(conn, since=since))
            elif route == "/api/paper/auth":
                from personal_intel_loop.paper_auth import auth_overview

                self._json(200, {"channels": auth_overview(conn)})
            elif route == "/api/paper/archive":
                query = parse_qs(parsed.query)
                self._json(
                    200,
                    get_archive(
                        conn,
                        source=(query.get("source") or [None])[0],
                        date_local=(query.get("date") or [None])[0],
                        q=(query.get("q") or [None])[0],
                        before=(query.get("before") or [None])[0],
                        limit=(query.get("limit") or [50])[0],
                    ),
                )
            elif route == "/api/paper/archive/sources":
                self._json(200, get_archive_sources(conn))
            elif route == "/api/paper/follow_suggestions":
                self._json(200, get_follow_suggestions(conn))
            elif route == "/api/paper/trips":
                self._json(200, get_trips(conn))
            else: self._json(404, {"error": "not found"})
        except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError) as exc: self._json(400, {"error": str(exc)})
        finally: conn.close()
    def do_POST(self) -> None:
        conn = connect_db(DB_PATH); ensure_schema(conn)
        try:
            payload = self._payload()
            if self.path == "/api/feedback": result = handle_feedback(conn, payload)
            elif self.path == "/api/claims/resolve": result = handle_claim_resolve(conn, payload)
            elif self.path == "/api/proposal": result = handle_proposal(payload)
            elif self.path == "/api/profile/save": result = handle_profile_save(payload)
            elif self.path == "/api/paper/note":
                from personal_intel_loop.paper_feedback import save_note
                try:
                    result = save_note(conn, str(payload.get("item_id") or ""), payload.get("text"), now_utc=_now_utc())
                except KeyError as exc:
                    self._json(404, {"error": str(exc)}); return
            elif self.path == "/api/paper/rate":
                try:
                    result = paper_rate(conn, str(payload.get("item_id") or ""), str(payload.get("dim") or ""), payload.get("value"), now_utc=_now_utc())
                except KeyError as exc:
                    self._json(404, {"error": str(exc)}); return
            elif self.path == "/api/paper/pipeline":
                try:
                    result = set_pipeline(conn, payload, now_utc=_now_utc())
                except KeyError as exc:
                    self._json(404, {"error": str(exc)}); return
            elif self.path == "/api/paper/read":
                try:
                    result = record_read(conn, str(payload.get("item_id") or ""), payload.get("dwell_ms"), now_utc=_now_utc())
                except KeyError as exc:
                    self._json(404, {"error": str(exc)}); return
            elif self.path == "/api/paper/events":
                # sendBeacon 用 text/plain: _payload 不看 Content-Type, 一律按 JSON 解析
                result = store_events(conn, payload, now_utc=_now_utc())
            elif self.path == "/api/paper/adjustments/revert":
                try:
                    result = revert_adjustment(conn, payload.get("id"), now_utc=_now_utc())
                except KeyError as exc:
                    self._json(404, {"error": str(exc)}); return
            elif self.path == "/api/paper/inbox":
                result = inbox_accept(conn, payload, now_utc=_now_utc())
            elif self.path == "/api/paper/auth/relogin":
                # 只从注册表取动作: payload 里的任何命令字符串都不会被执行
                try:
                    result = auth_relogin(payload)
                except KeyError as exc:
                    self._json(404, {"error": str(exc)}); return
            elif self.path == "/api/paper/follow_suggestions/decide":
                try:
                    result = decide_follow_suggestion(conn, payload, now_utc=_now_utc())
                except KeyError as exc:
                    self._json(404, {"error": str(exc)}); return
            elif self.path == "/api/paper/trips":
                result = create_trip(conn, payload, now_utc=_now_utc())
            elif self.path == "/api/paper/trips/delete":
                try:
                    result = delete_trip(conn, payload, now_utc=_now_utc())
                except KeyError as exc:
                    self._json(404, {"error": str(exc)}); return
            elif self.path == "/api/paper/inbox/read":
                try:
                    result = inbox_mark_read(conn, payload.get("inbox_id"), now_utc=_now_utc())
                except KeyError as exc:
                    self._json(404, {"error": str(exc)}); return
                # 契约第 5 节: 点开即标已读, 也算一次行为事件 open_inbox(ms=null)。
                # 投递方(Mac App)没有前端 session, 用固定 "inbox" 代 session_id。
                store_events(
                    conn,
                    {"session_id": "inbox", "events": [{
                        "ts": _now_utc(), "kind": "open_inbox", "item_id": None,
                        "edition_date": None, "ms": None, "meta": {"inbox_id": payload.get("inbox_id")},
                    }]},
                    now_utc=_now_utc(),
                )
            else: self._json(404, {"error": "not found"}); return
            self._json(200, result)
        except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError) as exc: self._json(400, {"error": str(exc)})
        finally: conn.close()


def startup_drain() -> None:
    """serve() 启动时补收一次投递箱 spool(契约第 5 节)。失败只打警告, 不挡服务启动。"""
    try:
        conn = connect_db(DB_PATH)
        try:
            ensure_schema(conn)
            drain_spool(conn, inbox_spool_dir(), now_utc=_now_utc())
        finally:
            conn.close()
    except Exception as exc:
        print(f"[inbox] spool drain failed: {exc}", flush=True)


def serve(*, host: str = "127.0.0.1", port: int = 8766, date_iso: str | None = None) -> None:
    _digest_path(date_iso)
    startup_drain()
    server = ThreadingHTTPServer((host, port), _Handler)
    print(f"PIL web: http://{host}:{port}/", flush=True)
    if host == "0.0.0.0":
        try: print(f"PIL LAN: http://{socket.gethostbyname(socket.gethostname())}:{port}/", flush=True)
        except OSError: pass
    server.serve_forever()
