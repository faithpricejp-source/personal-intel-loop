"""风险提示栏(契约第 9 节): 按行程个性化的「行前风险简报」。

对每个「今天到出发日 ≤30 天且未结束」的行程(start_date ≤ 今天+30 且 end_date ≥ 今天,
含进行中; 无 end_date 的只从开始日起监测 14 天, 见 `upcoming_trips`), 每期生成一条 kind="risk" 的 Item: 输入 = 行程 + 近 30 天库里与目的地相关的
条目(注入 search_fn, 缺省按地名 LIKE) + 模型。提示词要求每条结论附来源与日期, 来源不足
明说「没查到官方说法」, 不得编。输出 JSON {"title","lede","sections":[{"heading","points":
[{"text","source_title","source_url","date"}]}]}。Item 存 digests 表(item_id=risk:<trip_id>:<date>)。

行程 ≤3 天且当天出现新的 urgent 相关条目(标题/正文含地名)时投递一条 urgent inbox,
dedup 按 trip+date(复用 inbox.accept 的 dedup_key)。

地名匹配走 `place_aliases.expand`(中/日/英全部别名, 城市名能反查国家); 提示词另带一段
`{reference}` 官方参考资料块, 由 `risk_reference.load_reference` 从 `DATA_DIR / "risk_reference"`
离线读(可注入 `reference_fn` 替换)。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from personal_intel_loop import LOCAL_TZ
from personal_intel_loop import place_aliases
from personal_intel_loop.paper_common import item_frame
from personal_intel_loop.schemas import normalize_dt_to_utc_z

RISK_WINDOW_DAYS = 30
RISK_PUSH_DAYS = 3
OPEN_TRIP_MONITOR_DAYS = 14  # 无 end_date 行程自开始日起监测天数(H-6)
RISK_SEARCH_DAYS = 30
RISK_SOURCE = "pil.risk"
#: ENSO 是赤道太平洋的大尺度气候状态, 与任何目的地都相关: 近 N 天最新两条总纳入。
RISK_ENSO_DAYS = 45
RISK_ENSO_LIMIT = 2
ENSO_SOURCE_PREFIX = "enso_status:"
#: risk 条目(source_payload.kind=="risk")命中任一别名即纳入, 不受标题/正文 LIKE 限制。
RISK_PAYLOAD_DAYS = 30
#: 返回条数上限(对三个来源合并后的结果生效)。
RISK_RESULT_LIMIT = 10

#: 常驻地。别名由 `place_aliases.expand(*HOME_PLACE)` 展开。环境变量 PIL_HOME_PLACE 覆盖,
#: 写成「城市,国家」(任何语言写法, 需能在 place_aliases.json 里查到), 缺省 東京,日本。
HOME_PLACE = tuple(
    part.strip() for part in (os.environ.get("PIL_HOME_PLACE") or "東京,日本").split(",") if part.strip()
)
HOME_ALERTS_SOURCE_PREFIX = "home_alerts:"
#: 官方真实风险条目的入库时间窗(小时): 近 48 小时新入库的才算「本期新出现」。
RISK_REAL_WINDOW_HOURS = 48
#: 风险栏里官方真实条目的条数上限(缺省值, 可在 paper_layout.toml 的 [layout] risk_real 配)。
RISK_REAL_CAP = 5
#: 48h 入库窗 × 日刊节奏, 同一条目会连续两期落进窗口。官方风险条目
#: 按「低召回、出条即事件」定位, 不做重复曝光 —— 上过风险栏的 item_id 在之后 3 期
#: (上一期及前 2 期)内不再上栏。
RISK_EXCLUDE_PREV_EDITIONS = 3

_RISK_DAY_BOUNDS_PLACE = re.compile(r"[\s　]+")

logger = logging.getLogger(__name__)

RISK_PROMPT = """你在为一位常驻{home}的用户写一条「行前风险简报」。输出严格的 JSON(不要 markdown 代码块, 不要解释)。

## 行程
- 目的地: {place}({country})
- 日期: {start_date} 到 {end_date}
- 备注: {note}
- 距出发: {days_until} 天

## 库里近 30 天与目的地相关的条目(可用证据)
{related}

## 目的地官方参考资料(原文摘录)
以下是官方原文摘录, 引用时注明机构与日期; 摘录日期较旧的要说明。
{reference}

## 要求
- 综合气候与季节性灾害(含 ENSO 等大尺度气候状态的当期说法)、传染病、治安(具体到区域与时段)、交通与诈骗常见手法、当地法规雷区。
- 每条结论必须附来源(source_title)与日期(date); 来源不足时明说「没查到官方说法」, **不得编造**。
- 引用「官方参考资料」写进 points 时, source_title 写摘录里标注的机构名(如「日本外務省」「英国 FCDO」,
  统计类写发布机构如「NYPD / NYC Open Data」), source_url 写摘录里标注的来源 URL, date 写摘录标注的日期(不是今天)。
- 摘录日期较旧(与今天相差超过一年)的结论必须在 text 里说明「官方摘录日期为 YYYY-MM-DD, 属较旧信息」, 或改用库里更新的条目佐证。
- 参考资料为空或「(无)」时, 不得凭印象写治安/法规结论。
- sections: 2-4 个小节, 每节 {{"heading": "...", "points": [{{"text": "...", "source_title": "...", "source_url": "...", "date": "YYYY-MM-DD"}}]}}; 没有来源支撑的点不要写。
- title: ≤30 字; lede: 两三句中文总述(含证据是否充分)。

## 输出
只返回一个 JSON 对象:
{{"title": "...", "lede": "...", "sections": []}}
"""


def _extract_json(text):
    from personal_intel_loop.summarizer import _extract_json as summarizer_extract_json

    parsed = summarizer_extract_json(text or "")
    return parsed if isinstance(parsed, dict) else None


def _row_to_entry(row) -> dict:
    return {
        "item_id": row["item_id"],
        "title": row["title"],
        "url": row["url"],
        "source": row["source"],
        "date": str(row["ts"])[:10],
    }


def _cutoff(now_utc, days: int) -> str:
    base = datetime.fromisoformat(str(now_utc).replace("Z", "+00:00"))
    return normalize_dt_to_utc_z(base - timedelta(days=days))


def _cutoff_hours(now_utc, hours: int) -> str:
    base = datetime.fromisoformat(str(now_utc).replace("Z", "+00:00"))
    return normalize_dt_to_utc_z(base - timedelta(hours=hours))


def _payload_regions(source_payload_json) -> list[str]:
    """source_payload_json → regions 列表。解析不了返回 []。"""
    try:
        payload = json.loads(source_payload_json or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(payload, dict) or payload.get("kind") != "risk":
        return []
    regions = payload.get("regions")
    if not isinstance(regions, list):
        return []
    return [str(r).strip() for r in regions if isinstance(r, str) and r.strip()]


def _like_any(terms: list[str], sql: str = "title LIKE ? OR body LIKE ?") -> tuple[str, list[str]]:
    """`(title LIKE ? OR body LIKE ?) OR (...)` 片段 + 扁平参数。"""
    parts = [f"({sql})" for _ in terms]
    return "(" + " OR ".join(parts) + ")", [arg for term in terms for arg in (f"%{term}%", f"%{term}%")]


def default_search_fn(
    conn: sqlite3.Connection,
    place: str,
    *,
    now_utc=None,
    days: int = RISK_SEARCH_DAYS,
    country: str | None = None,
) -> list[dict]:
    """库内与目的地相关的近 N 天条目(缺省实现)。

    三个来源合并去重, 都按 `ts DESC` 排, 最多 RISK_RESULT_LIMIT 条:
    1. 标题/正文 LIKE: 用 `place_aliases.expand` 的**全部别名**(中/日/英), 不只用户填的那种写法;
    2. `source_payload.kind == "risk"` 且 `regions` 命中任一别名的条目(近 RISK_PAYLOAD_DAYS 天),
       不受 LIKE 限制 — 官方源的地区名常只出现在 payload 里, 标题正文一个字都不提;
    3. ENSO 条目(`source` 以 `enso_status:` 开头)近 RISK_ENSO_DAYS 天内最新 RISK_ENSO_LIMIT 条,
       总是纳入: 气候状态对任何目的地都相关。

    `country` 只影响别名解析(行程表里有国家列时传进来, 目的地只写了城市名也能定国家)。
    """
    place = str(place or "").strip()
    if not place and not country:
        return []
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)  # 10-05 审计 F3：naive 会让 normalize 抛错
    aliases = place_aliases.expand(place, country)
    # 别名按长度降序: 「United States」要在「United」之前匹配, 且长别名先 LIKE 更划算。
    terms = sorted({t for t in aliases["terms"] if t}, key=lambda t: (-len(t), t))
    if not terms:
        return []

    found: dict[str, dict] = {}
    ts_of: dict[str, str] = {}
    tier_of: dict[str, int] = {}

    def _absorb(rows, tier: int) -> None:
        for row in rows:
            entry = _row_to_entry(row)
            item_id = entry["item_id"]
            if item_id not in found:
                found[item_id] = entry
                ts_of[item_id] = str(row["ts"])
                tier_of[item_id] = tier
            else:
                tier_of[item_id] = min(tier_of[item_id], tier)

    # 10-05 实测：假名别名按子串匹配全是噪音（「タイ」命中「阪神タイガース」「タイトル」，「インド」命中「インドネシア」），
    # 社交帖顺带提到地名也多是噪音——LIKE 粗筛后按词边界复核（假名前后不能紧挨片假名、拉丁字母按整词），只扫非社交源。
    like_sql, like_args = _like_any(terms)
    rows = conn.execute(
        f"SELECT item_id, title, url, source, ts, body FROM items WHERE first_ingested_at >= ? AND {like_sql}"
        " AND source NOT LIKE 'weibo%' AND source NOT LIKE 'zhihu%'"
        " ORDER BY ts DESC LIMIT ?",
        [_cutoff(now_utc, days), *like_args, RISK_RESULT_LIMIT * 5],
    ).fetchall()
    term_res = [_term_pattern(t) for t in terms]
    _absorb([r for r in rows if any(rx.search(f"{r['title'] or ''}\n{r['body'] or ''}") for rx in term_res)][:RISK_RESULT_LIMIT], 2)

    # 官方源的地区名常只落在 source_payload.regions 里, 标题正文一个字都不提
    # (外务省条目标题只有「.gobp.go.jp issues new warning」), 所以单独扫一遍 payload。
    wanted = {place_aliases.normalize_term(t) for t in terms}
    for row in conn.execute(
        "SELECT item_id, title, url, source, ts, source_payload_json FROM items"
        " WHERE first_ingested_at >= ? ORDER BY ts DESC",
        (_cutoff(now_utc, RISK_PAYLOAD_DAYS),),
    ).fetchall():
        if _regions_hit(row["source_payload_json"], wanted):
            _absorb([row], 0)

    _absorb(
        conn.execute(
            "SELECT item_id, title, url, source, ts FROM items"
            " WHERE first_ingested_at >= ? AND source LIKE ?"
            " ORDER BY ts DESC LIMIT ?",
            (_cutoff(now_utc, RISK_ENSO_DAYS), f"{ENSO_SOURCE_PREFIX}%", RISK_ENSO_LIMIT),
        ).fetchall(),
        1,
    )

    # 官方风险源（payload 命中）> ENSO > 正文提及；同档按时间新到旧
    ordered = sorted(found.values(), key=lambda e: (ts_of[e["item_id"]], e["item_id"]), reverse=True)
    ordered.sort(key=lambda e: tier_of[e["item_id"]])
    return ordered[:RISK_RESULT_LIMIT]


def _term_pattern(term: str) -> "re.Pattern[str]":
    """地名别名的词边界：含汉字的按子串；假名的前后不能紧挨片假名；拉丁字母按整词（不分大小写）。"""
    esc = re.escape(term)
    if re.search(r"[\u4e00-\u9fff]", term):
        return re.compile(esc)
    if re.search(r"[\u3040-\u30ff]", term):
        return re.compile(rf"(?<![\u30a0-\u30ff]){esc}(?![\u30a0-\u30ff])")
    return re.compile(rf"(?<![A-Za-z]){esc}(?![A-Za-z])", re.I)


def _regions_hit(source_payload_json, wanted: set[str]) -> bool:
    """risk payload 的 regions 是否命中任一别名(归一化后比较)。"""
    for region in _payload_regions(source_payload_json):
        if place_aliases.normalize_term(region) in wanted:
            return True
    return False


def _is_risk_payload(source_payload_json) -> bool:
    """source_payload_json 是否 `kind == "risk"`(采集器入库的官方风险条目)。"""
    try:
        payload = json.loads(source_payload_json or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("kind") == "risk"


def default_home_terms(*, path=None) -> set[str]:
    """常驻地别名(归一化)。"""
    expanded = place_aliases.expand(*HOME_PLACE, path=path)
    return {norm for norm in (place_aliases.normalize_term(t) for t in expanded["terms"]) if norm}


def default_trip_terms(conn: sqlite3.Connection, *, date_local, path=None) -> set[str]:
    """近30天内所有行程的别名并集(归一化); 没有行程返回空集。"""
    out: set[str] = set()
    for trip in upcoming_trips(conn, date_local=date_local):
        place = str(trip["place"] or "").strip()
        if not place:
            continue
        for term in place_aliases.expand(place, trip["country"], path=path)["terms"]:
            norm = place_aliases.normalize_term(term)
            if norm:
                out.add(norm)
    return out


def _recent_risk_edition_ids(conn: sqlite3.Connection, *, date_local,
                             editions: int = RISK_EXCLUDE_PREV_EDITIONS) -> set[str]:
    """最近 `editions` 期(严格早于 date_local, 不含当期)风险栏已上过的 item_id。

    当期重跑不受影响: build_edition 同日重跑先删当期行, 这里的过滤又只看
    `edition_date < 当期`, 所以同日重跑风险栏照常重现。
    """
    try:
        rows = conn.execute(
            "SELECT item_id FROM editions WHERE section='risk' AND edition_date IN ("
            "  SELECT DISTINCT edition_date FROM editions WHERE edition_date < ?"
            "  ORDER BY edition_date DESC LIMIT ?)",
            (date_local.isoformat(), int(editions)),
        ).fetchall()
    except sqlite3.Error:
        return set()          # editions 表异常时退回无跨期去重, 别挡出版
    return {str(row[0]) for row in rows}


def select_real_risk_items(
    conn: sqlite3.Connection,
    *,
    date_local,
    now_utc: str,
    home_terms: set[str] | None = None,
    trip_terms: set[str] | None = None,
    cap: int | None = RISK_REAL_CAP,
    exclude_ids=None,
) -> list[str]:
    """近 48 小时入库的官方风险条目(`source_payload.kind == "risk"`)里, 该上风险栏的 item_id。

    契约第 9 节: 风险栏 = 当期所有 `kind:"risk"` 条目 + 与常驻地/行程地相关的 urgent 投递。
    这里只管前者 —— 采集器入库的官方条目(home_alerts / mofa_anzen / who_don / cn_consular /
    enso_status), 它们带 `regions` 列表, 地区名常只出现在 payload 里, 标题正文一个字都不提。

    入选规则:
    1. `home_alerts:*` 一律入选(常驻地, 本来就是低频高价值);
    2. 其他官方源: `regions` 命中常驻地别名或任一临近行程的别名;
    3. `enso_status:*`: ENSO 是赤道太平洋的大尺度状态, 与任何目的地都相关, 近 48h 新入库即入选
       (`first_ingested_at` 窗口本身就是「发布后首次出现」—— 入库只在首次见到时写, 重跑不刷新)。

    排序: home_alerts > 行程地/常驻地命中 > ENSO; 同档按入库时间新到旧, 再按 item_id 稳定排序。
    `exclude_ids` 里已在本期别的分区上版的条目剔除(在截断之前剔, 免得白占名额);
    上一期及前 2 期风险栏已上过的 item_id 一并剔除(10-05 审计 E06, 查 editions 表)。
    `cap` 为 None 表示不限。`home_terms` / `trip_terms` 缺省按 `HOME_PLACE` 与 trips 表现场算。
    """
    home_set = home_terms if home_terms is not None else default_home_terms()
    trip_set = trip_terms if trip_terms is not None else default_trip_terms(conn, date_local=date_local)
    wanted = {t for t in (set(home_set) | set(trip_set)) if t}
    excluded = {str(i) for i in (exclude_ids or ())}
    excluded |= _recent_risk_edition_ids(conn, date_local=date_local)

    cutoff = _cutoff_hours(now_utc, RISK_REAL_WINDOW_HOURS)
    rows = conn.execute(
        "SELECT item_id, source, first_ingested_at, source_payload_json FROM items"
        " WHERE first_ingested_at >= ? ORDER BY first_ingested_at DESC, item_id ASC",
        (cutoff,),
    ).fetchall()

    picked: list[tuple[int, str, str]] = []  # (档, first_ingested_at, item_id)
    for row in rows:
        item_id = str(row["item_id"])
        if item_id in excluded or not _is_risk_payload(row["source_payload_json"]):
            continue
        source = str(row["source"] or "")
        if source.startswith(HOME_ALERTS_SOURCE_PREFIX):
            tier = 0
        elif source.startswith(ENSO_SOURCE_PREFIX):
            tier = 2
        elif wanted and _regions_hit(row["source_payload_json"], wanted):
            tier = 1
        else:
            continue
        picked.append((tier, str(row["first_ingested_at"] or ""), item_id))

    picked.sort(key=lambda entry: entry[1], reverse=True)  # 同档新到旧
    picked.sort(key=lambda entry: entry[0])  # 稳定排序, 档位优先
    ids = [item_id for _tier, _at, item_id in picked]
    return ids if cap is None else ids[: max(int(cap), 0)]


def upcoming_trips(conn: sqlite3.Connection, *, date_local) -> list[sqlite3.Row]:
    """今天到出发日 ≤30 天且未结束的行程(含进行中: start ≤ 今天+30 且 end ≥ 今天)。

    没写 end_date 的开放式行程只持续监测 OPEN_TRIP_MONITOR_DAYS=14 天,
    开始日算第 1 天: today - start ≤ 13 天仍生成, today - start ≥ 14 天(开始日早于今天满 14 天)
    不再生成简报、也不再算风险栏的行程地命中。写了 end_date 的照旧按 end ≥ 今天判断。
    """
    today = date_local
    horizon = (today + timedelta(days=RISK_WINDOW_DAYS)).isoformat()
    open_cutoff = (today - timedelta(days=OPEN_TRIP_MONITOR_DAYS)).isoformat()
    return conn.execute(
        "SELECT * FROM trips WHERE start_date <= ? AND ((end_date IS NULL AND start_date > ?) OR end_date >= ?) "
        "ORDER BY start_date ASC, trip_id ASC",
        (horizon, open_cutoff, today.isoformat()),
    ).fetchall()


def _today_urgent_related(conn: sqlite3.Connection, place: str, *, date_local) -> list[sqlite3.Row]:
    """当天(东京本地日)urgent 且标题/正文含地名的投递。"""
    day_start = datetime.combine(date_local, datetime.min.time()).replace(tzinfo=LOCAL_TZ)
    start_z = normalize_dt_to_utc_z(day_start)
    end_z = normalize_dt_to_utc_z(day_start + timedelta(days=1))
    # place 是用户自由输入, LIKE 通配符(% _)与转义符本身先转义(SQLite 无默认转义符,
    # 须显式 ESCAPE '\'), 否则 place="100%"/"_" 会让当天任意 urgent 全部误判相关。
    escaped = place.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    return conn.execute(
        "SELECT * FROM inbox WHERE priority='urgent' AND created_at >= ? AND created_at < ? AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\') ORDER BY inbox_id ASC",
        (start_z, end_z, like, like),
    ).fetchall()


def _normalize_sections(raw) -> list[dict]:
    sections = []
    if not isinstance(raw, list):
        return sections
    for section in raw:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or "").strip()
        points = []
        for point in section.get("points") or []:
            if not isinstance(point, dict):
                continue
            text = str(point.get("text") or "").strip()
            if not text:
                continue
            points.append(
                {
                    "text": text[:500],
                    "source_title": (str(point.get("source_title") or "").strip()[:200] or None),
                    "source_url": (str(point.get("source_url") or "").strip()[:500] or None),
                    "date": (str(point.get("date") or "").strip()[:10] or None),
                }
            )
        if heading and points:
            sections.append({"heading": heading, "points": points})
    return sections


def default_reference_fn(place: str, country: str | None = None, *, ref_dir=None) -> str:
    """目的地(country_key/city_key)的官方参考块; 目录/文件不存在返回 ""。"""
    from personal_intel_loop import risk_reference

    aliases = place_aliases.expand(place, country)
    if not aliases["country_key"]:
        return ""
    return risk_reference.load_reference(
        aliases["country_key"], aliases["city_key"], ref_dir=ref_dir
    )


def generate(
    conn: sqlite3.Connection,
    *,
    date_local,
    now_utc: str,
    llm_call=None,
    search_fn=None,
    reference_fn=None,
) -> tuple[list[dict], dict[str, list[str]]]:
    """为每个临近行程生成 risk Item payload(不落库, paper.py 统一写 digests)。

    返回 (payloads, {risk_item_id: [相关条目 item_id...]}); 模型/解析失败跳过该行程不抛。

    `reference_fn(place, country) -> str` 注入官方参考资料块(缺省 `default_reference_fn`,
    从 `DATA_DIR / "risk_reference"` 离线读); 参考块取不到就填 "(无)", 简报照旧生成。
    """
    payloads: list[dict] = []
    member_map: dict[str, list[str]] = {}
    date_iso = date_local.isoformat()
    ref_fn = default_reference_fn if reference_fn is None else reference_fn
    for trip in upcoming_trips(conn, date_local=date_local):
        place = str(trip["place"] or "").strip()
        if not place:
            continue
        related = (
            list(search_fn(place))
            if search_fn is not None
            else default_search_fn(conn, place, now_utc=now_utc, country=trip["country"])
        )
        related_ids = [str(entry["item_id"]) for entry in related if entry.get("item_id")]
        related_lines = [
            f"- {entry.get('title') or '(无标题)'} ({entry.get('source') or '?'}, {entry.get('date') or '?'}): {entry.get('url') or '无链接'}"
            for entry in related
        ]
        start_date = trip["start_date"]
        try:
            days_until = (datetime.fromisoformat(start_date).date() - date_local).days
        except ValueError:
            days_until = None
        # 参考块取不到不让整期简报失败: 参考资料本来就是增强, 不是硬依赖。
        try:
            reference = str(ref_fn(place, trip["country"]) or "").strip()
        except Exception as exc:  # noqa: BLE001 — 参考资料失败不挡出版
            logger.warning("risk: reference failed for trip %s: %s", trip["trip_id"], exc)
            reference = ""
        prompt = RISK_PROMPT.format(
            home=HOME_PLACE[0] if HOME_PLACE else "本地",
            place=place,
            country=trip["country"] or "-",
            start_date=start_date,
            end_date=trip["end_date"] or "未定",
            note=trip["note"] or "-",
            days_until=days_until if days_until is not None else "?",
            related="\n".join(related_lines) if related_lines else "(无)",
            reference=reference if reference else "(无)",
        )
        try:
            if llm_call is not None:
                raw = llm_call(prompt)
            else:
                from personal_intel_loop.paper_ai import _call_cloud

                raw, _model = _call_cloud(prompt)
        except Exception as exc:  # noqa: BLE001 — 风险简报失败不挡出版
            logger.warning("risk: llm failed for trip %s: %s", trip["trip_id"], exc)
            raw = None
        parsed = _extract_json(raw)
        if parsed is None:
            # 10-05 审计 F4: 简报失败只跳过 payload 构造, 不再 continue——
            # 下面的 urgent 投递推送与 LLM 无关, 被同一 continue 吞掉会漏推
            if raw is not None:
                logger.warning("risk: bad json for trip %s", trip["trip_id"])
        else:
            title = str(parsed.get("title") or "").strip()
            lede = str(parsed.get("lede") or "").strip()
            sections = _normalize_sections(parsed.get("sections"))
            if title and lede and sections:
                risk_item_id = f"risk:{trip['trip_id']}:{date_iso}"
                payload = item_frame()
                payload.update(
                    {
                        "item_id": risk_item_id,
                        "title": title,
                        "url": None,
                        "source": RISK_SOURCE,
                        "source_label": "行前风险",
                        "author_key": RISK_SOURCE,
                        "author_label": "行前风险",
                        "published_at": now_utc,
                        "lede": lede,
                        "one_liner": title,
                        "topic": "风险",
                        "kind": "risk",
                        "sections": sections,
                        "trip": {
                            "trip_id": trip["trip_id"],
                            "place": place,
                            "country": trip["country"],
                            "start_date": start_date,
                            "end_date": trip["end_date"],
                            "days_until": days_until,
                        },
                        "why_here": {"profile_hit": None, "lane": "risk", "blind_reason": None},
                    }
                )
                payloads.append(payload)
                member_map[risk_item_id] = related_ids

        # 行程 ≤3 天 + 当天有新的 urgent 相关条目 → 投一条 urgent inbox(dedup trip+date)
        if days_until is not None and 0 <= days_until <= RISK_PUSH_DAYS:
            # 10-05 审计 E08: 这段与 LLM 无关, 却没有独立兜底 —— 一次 sqlite 错误/磁盘满
            # 会让异常冒出 generate(), 被 paper.py 的 "risk generation failed" 整体吞掉,
            # 前面行程已构造好的 payloads 全部丢弃, 与「某行程 LLM 失败」也无法区分。
            # 推送失败只跳过本行程的投递, 不丢当期简报。
            try:
                urgent_related = _today_urgent_related(conn, place, date_local=date_local)
                if urgent_related:
                    from personal_intel_loop.paper_inbox import accept as inbox_accept

                    inbox_accept(
                        conn,
                        {
                            "source": RISK_SOURCE,
                            "title": f"行程临近 {place}: {len(urgent_related)} 条新的紧急相关提示",
                            "body": "\n".join(f"- {row['title']}" for row in urgent_related[:10]),
                            "url": None,
                            "priority": "urgent",
                            "dedup_key": f"risk:{trip['trip_id']}:{date_iso}",
                        },
                        now_utc=now_utc,
                    )
            except Exception as exc:  # noqa: BLE001 — 推送失败不挡出版
                logger.warning("risk: urgent push failed for trip %s: %s",
                               trip["trip_id"], exc)
    return payloads, member_map
