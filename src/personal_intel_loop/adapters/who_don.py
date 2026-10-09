"""WHO Disease Outbreak News (`who_don`) —— 官方传染病暴发通报。

报纸「风险提示栏」四条官方源之一(见 `docs/paper_v2_contract.md` 第 9 节)。DON 是 WHO 唯一的
**暴发级**传染病官方通报(不是常规疫情统计),对「我要去哪个国家」这条 lane 是硬信号。

## 接口(2026-10-04 实测)

`GET https://www.who.int/api/news/diseaseoutbreaknews` —— 站点列表页
`https://www.who.int/emergencies/disease-outbreak-news` 背后的 OData JSON API,免费无鉴权。

实测行为:
- **默认返回 50 条, 按 `PublicationDate` 升序**(最老的 2006-03-20 在最前), 且带
  `@odata.nextLink`(`?$skip=50`)。**不排序直接用会拿到 2006 年的旧条目**。
- 加 `?$orderby=PublicationDate desc` 得到最新在前; 再加 `&$top=N` 限量。
  实测 `$orderby`+`$top=10` 返回 10 条且 `nextLink` 变 `None`(总量不足)。
- `$filter=PublicationDate ge 2026-09-01T00:00:00Z` 可用(实测返回 2 条,正是 9 月那两期)。
- `$select=Id,DonId,Title,PublicationDate,Summary,Assessment,Advice,ItemDefaultUrl`
  可用,体积从 308KB 掉到 33KB(`$top=5`)。

字段(实测一条 2026-DON618):`Id` GUID、`DonId` "2026-DON618"、`Title`、
`PublicationDate` "2026-09-25T15:30:18Z"、`ItemDefaultUrl` "/2026-DON618"、
`Summary`/`Assessment`/`Advice`/`Epidemiology`/`Overview`/`Response`/`FurtherInformation`
都是 **HTML 片段**(带大量 inline style / `paraid` 属性),不是纯文本。
- `ItemDefaultUrl` 拼根域名是 **404**(实测 `https://www.who.int/2026-DON618` → 404),
  真正的详情页是 `https://www.who.int/emergencies/disease-outbreak-news/item/<ItemDefaultUrl去斜杠>`
  (实测 200 / 108KB)。DON 列表页链接也是这个形式(见 `FurtherInformation` 里的自引用)。

## 坑
- 老条目(2021 年及以前)的 `DonId` **是空字符串**,`ItemDefaultUrl` 也不是 `2026-DONxxx` 形式
  (例 `/2006_03_20-en`)。所以去重与 url 拼装都必须能在 `DonId` 为空时退回 `Id`。
- 同一个 DON 会**反复更新同一条**(2026-DON616/617/618 都是同一场 Bundibugyo 疫情的更新),
  每期 `DonId` 不同 → 每次更新算一条新条目, 这是对的(官方每次更新都是独立一期)。
- `regions` 只能从标题/正文里抽。DON 标题没有结构化国家字段(实测 API 不返回 country),
  官方句式是 `<病名> - <国家/地区>` 或 `... in <国家>`。抽不出一律留空数组, 不编。
"""
from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from personal_intel_loop import DATA_DIR
from personal_intel_loop.schemas import Item, compute_item_id

logger = logging.getLogger(__name__)

ISSUER = "World Health Organization (WHO)"
API_BASE = "https://www.who.int/api/news/diseaseoutbreaknews"
ITEM_URL = "https://www.who.int/emergencies/disease-outbreak-news/item/{slug}"
DEFAULT_STATE_PATH = DATA_DIR / "cache" / "who_don_state.json"
#: `compute_item_id` 的 source 前缀, 与 `WhoDonAdapter.name` 一致(模块级常量, 供
#: `_dedup_key` 在类外复用, 保证去重键与入库 id 永远同源)。
NAME = "who_don"

REQUEST_TIMEOUT = 30
DEFAULT_DAYS = 14
TOP = 20
BODY_LIMIT = 20_000
SEEN_LIMIT = 4000
FETCH_FIELDS = "Id,DonId,Title,PublicationDate,ItemDefaultUrl,Summary,Assessment,Advice"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t　]+")

#: 标题里常见的"非地名"片段, 抽 regions 时先切掉。实测 DON 标题的 disease 名千变万化,
#: 只做"去掉疾病名"这一步, 剩下的短片段当地区 —— 抽不出的老实留空。
_NOT_A_REGION = re.compile(
    r"\b(update|updates|situation|outbreak|outbreaks|disease|infection|infections|"
    r"virus|cases|case|reported|concerning|response|summary|overview|assessment|"
    r"advice|weekly|daily|new|ongoing|end of|follow-up|surveillance|death|deaths)\b",
    re.I,
)

#: 10-05 复审 R08: **短语级**的非地名。`_NOT_A_REGION` 只做单词 `fullmatch`/搜索, 挡不住
#: `Multi-locations` / `Global` 这种整体就不是地名的说法(实测 DON611`…, Multi-locations`
#: 与 DON610 `Yellow fever - Global` 都被当成了地区)。命中就整条丢掉, 不进 regions。
_NOT_A_REGION_PHRASE = frozenset({
    "global", "worldwide", "international", "multi-locations", "multi-location",
    "multi-country", "multi-country outbreak", "multiple countries",
    "various locations", "unknown", "several countries",
})

#: 10-05 复审 R08: 并列国名的分隔符。WHO 2026-06 起的 DON 大量用 `,` / `&` / `and` 并列
#: 多个国家(`Ebola …, Democratic Republic of the Congo & Uganda`), 而原来只认
#: ` - ` / ` – ` / ` in ` → 实测最近 13 条里 6 条 regions 落空, 恰是近期真实疫情那批。
_LIST_SEP_RE = re.compile(r"\s*(?:,|&|\band\b)\s*", re.I)

#: 内部含 "and" 的官方复合国名。拆并列前先整体占位, 否则 `\band\b` 会把
#: `Trinidad and Tobago` 拆成 ["Trinidad","Tobago"] 两个假地区(DON 历史期次确有用到)。
_COMPOUND_COUNTRIES = (
    "Trinidad and Tobago",
    "Bosnia and Herzegovina",
    "Antigua and Barbuda",
    "Saint Kitts and Nevis",
    "Saint Vincent and the Grenadines",
    "São Tomé and Príncipe",
    "Sao Tome and Principe",
    "Saint Pierre and Miquelon",
    "South Georgia and the South Sandwich Islands",
    "Turks and Caicos Islands",
)


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


def _html_to_text(raw: str | None) -> str:
    """WHO 的正文字段是 HTML 片段(带 inline style / paraid) → 纯文本。"""
    if not raw:
        return ""
    text = re.sub(r"<(script|style).*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|li|div|h\d|tr)>", "\n", text, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    lines = [_WS_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _plausible_region(part: str) -> bool:
    """一个片段像不像地名/地区名。挡掉疾病名与非地名短语, 不硬猜。"""
    if not part or len(part) > 60:
        return False
    if _NOT_A_REGION.search(part):
        return False
    # 10-05 复审 R08: 短语级过滤(`Multi-locations` / `Global` 整体不是地名)
    if part.strip().lower() in _NOT_A_REGION_PHRASE:
        return False
    return True


def _pick_regions(tail: str) -> list[str]:
    """把一段尾巴按 `,` / `&` / ` and ` 拆开, 逐个判断像不像地名, 返回像的那些。"""
    if not tail:
        return []
    # 复合国名先整体占位再拆, 拆完还原, 避免 and 分隔符切进国名内部
    protected = tail
    restored: dict[str, str] = {}
    for idx, name in enumerate(_COMPOUND_COUNTRIES):
        token = f"\x00{idx}\x00"
        pattern = re.compile(re.escape(name), re.I)
        if pattern.search(protected):
            protected = pattern.sub(token, protected)
            restored[token] = name
    parts = (
        [p.strip() for p in _LIST_SEP_RE.split(protected)]
        if _LIST_SEP_RE.search(protected)
        else [protected]
    )
    out = []
    for p in parts:
        for token, name in restored.items():  # 验收补: 占位符不是整段时也要还原
            p = p.replace(token, name)
        if _plausible_region(p):
            out.append(p)
    return out


def regions_from_title(title: str) -> list[str]:
    """从 DON 标题里取国家/地区名。取不到就返回 [](设计规格: 取不到就 '综合' 的同类处理)。

    句式尾巴: ` - X` / ` – X` / ` in X`; 尾巴里再用 `,` / `&` / ` and ` 拆并列国名
    (10-05 复审 R08, 实测主力形态)。尾巴上挂着的官方措辞(`situation in X` / `in X`)剥掉,
    剥完不像地名就放弃。**并列项逐个过滤**, 全不像地名才返回 [](而不是把疾病名当地区)。
    """
    cleaned = re.sub(r"\s*[-–—]\s*(update|updates|Update)\b.*$", "", title or "").strip()
    for sep in (" - ", " – ", " — ", " in ", " in the "):
        if sep not in cleaned:
            continue
        tail = cleaned.rsplit(sep, 1)[1].strip()
        # 「situation in Egypt」→ 剥成 Egypt; 剥完是空的/还是疾病名 → 放弃
        tail = re.sub(r"^(?:the\s+)?situation\s+", "", tail, flags=re.I).strip()
        if tail.lower().startswith("in "):
            tail = tail[3:].strip()
        picked = _pick_regions(tail)
        if picked:
            return picked
    # 10-05 复审 R08: 真实主力形态**根本没有** ` - ` / ` in ` 尾巴 ——
    # `Ebola disease caused by Bundibugyo virus, Democratic Republic of the Congo & Uganda`
    # 整句就一个逗号加一个 `&`。上面那圈 sep 全不命中时, 若整句确实有并列符, 就拿整句拆,
    # 前半截(疾病名)会被 `_NOT_A_REGION` 挡掉。没有并列符时**不能**兜底返回整句 ——
    # 那会把 `Yellow fever - Global` 这种非地名整句收进去。
    if _LIST_SEP_RE.search(cleaned):
        return _pick_regions(cleaned)
    return []


def _slug(raw: dict[str, Any]) -> str:
    """详情页 slug: 优先 `ItemDefaultUrl` 去掉斜杠, 退回 `DonId`, 再退回 `Id`。"""
    url_part = str(raw.get("ItemDefaultUrl") or "").strip("/")
    if url_part:
        return url_part
    for key in ("DonId", "Id"):
        value = str(raw.get(key) or "").strip()
        if value:
            return value
    return ""


def _guid(raw: dict[str, Any]) -> str:
    """官方 id: `DonId` 为空(老条目)时退回 `Id`。"""
    return str(raw.get("DonId") or "").strip() or str(raw.get("Id") or "").strip()


def parse_api(payload_text: str) -> list[dict[str, Any]]:
    """API 返回 → 目标条目 dict。结构变了/不是 JSON 返回 []。"""
    try:
        data = json.loads(payload_text)
    except (ValueError, TypeError) as exc:
        logger.warning("who_don JSON 解析失败: %s", exc)
        return []
    if not isinstance(data, dict) or not isinstance(data.get("value"), list):
        logger.warning("who_don 返回结构不是 {value:[...]}")
        return []
    out: list[dict[str, Any]] = []
    for raw in data["value"]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("Title") or "").strip()
        ts = _iso(raw.get("PublicationDate"))
        slug = _slug(raw)
        if not (title and ts and slug):
            continue
        body = "\n\n".join(x for x in (
            _html_to_text(raw.get("Summary")),
            _html_to_text(raw.get("Assessment")),
            _html_to_text(raw.get("Advice")),
        ) if x)
        out.append({
            "guid": _guid(raw),
            "don_id": str(raw.get("DonId") or "").strip(),
            "title": title,
            "ts": ts,
            "url": ITEM_URL.format(slug=slug),
            "body": body,
            "regions": regions_from_title(title),
        })
    return out


# ---- 网络(可注入)----------------------------------------------------------

def _build_query(since: datetime) -> dict[str, str]:
    return {
        "$filter": f"PublicationDate ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "$orderby": "PublicationDate desc",
        "$top": str(TOP),
        "$select": FETCH_FIELDS,
    }


def _fetch_api(since: datetime) -> str:
    try:
        r = requests.get(API_BASE, params=_build_query(since), timeout=REQUEST_TIMEOUT,
                         headers={"User-Agent": "Mozilla/5.0"})
    except requests.RequestException as exc:
        logger.warning("who_don fetch failed: %s", exc)
        return ""
    if r.status_code != 200:
        logger.warning("who_don non-200: %s", r.status_code)
        return ""
    return r.text


# ---- state -----------------------------------------------------------------

def _load_state(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    seen = data.get("seen") if isinstance(data, dict) else None
    return {str(x) for x in seen} if isinstance(seen, list) else set()


def _save_state(path: Path, seen: set[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"seen": sorted(seen)[-SEEN_LIMIT:]}, ensure_ascii=False), "utf-8")
    except OSError as exc:
        logger.warning("who_don state 写不进去 %s: %s", path, exc)


def _dedup_key(entry: dict[str, Any]) -> str:
    """去重键 —— 与入库 `Item.id` **用同一套键**(10-05 复审 R10)。

    修复前: `seen` 用 `_guid`(`DonId`, 老条目退回 `Id` GUID), 而入库 id 用
    `compute_item_id(name, url=url)`(URL 的 sha1) —— 两套键互不相干, 方向相反的错都可能出现:
    - 老条目同一期在列表页与详情页给出不同 `ItemDefaultUrl` → 两个不同 URL id, 但只用一个
      GUID 去重(入库一条, 另一条被静默跳过);
    - 两条不同 DON 撞同一个 URL → `upsert_item` 会合并, 而 `seen` 不认为重复, 每轮重抓。
    统一成 URL 派生的 item id 后, 「已见过」与「已入库」严格一致。
    """
    return compute_item_id(NAME, url=entry["url"])


class WhoDonAdapter:
    name = NAME

    def __init__(
        self,
        *,
        fetch_api: Callable[[datetime], str] | None = None,
        state_path: Path | None = None,
        days: int = DEFAULT_DAYS,
    ) -> None:
        self._fetch_api = fetch_api or _fetch_api
        self.state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        self.days = days

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable[ItemRecord]:
        # 10-05 复审 R09: naive `since` 一律按 **UTC** 解释, 不看宿主 TZ。
        # `cli.py` 的 `--since 2026-09-01` 走 `datetime.fromisoformat` 得到的就是 naive 值,
        # 原来的 `naive.astimezone(utc)` 按宿主本地时区算 → 同一命令在 `TZ=UTC` 与
        # `TZ=Asia/Tokyo` 下 cutoff 差 9 小时。WHO 的 `PublicationDate` 本身就是 UTC(带 `Z`),
        # 所以把 naive 理解成 UTC 是与数据口径一致、也与宿主无关的选择。
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        cutoff = since.astimezone(timezone.utc) if since else (
            datetime.now(timezone.utc) - timedelta(days=self.days))
        seen = _load_state(self.state_path)

        records: list[ItemRecord] = []
        produced_keys: list[str] = []   # 与 records 同序, 供截断后决定谁进 seen(R07)
        produced: set[str] = set()      # 本轮已产出的键, 挡同轮内的同 URL 重复(R10)
        for entry in parse_api(self._fetch_api(cutoff)):
            key = _dedup_key(entry)
            if key in seen or key in produced:
                continue
            ts = entry["ts"]
            if ts < cutoff:
                continue
            records.append(ItemRecord(
                item=Item(
                    id=key,                # R10: 去重键 == 入库 id
                    source=self.name,
                    url=entry["url"],
                    title=entry["title"][:512],
                    body=entry["body"][:BODY_LIMIT],
                    author=ISSUER,
                    ts=ts,
                    lang="en",
                    tags=["风险提示", "传染病"],
                ),
                adapter_name=self.name,
                source_payload_json=json.dumps(
                    {"kind": "risk", "issuer": ISSUER,
                     "published": ts.isoformat().replace("+00:00", "Z"),
                     "regions": entry["regions"],
                     "level": None,          # DON 没有官方等级字段(实测 API 不返回)
                     "don_id": entry["don_id"] or None},
                    ensure_ascii=False),
                media_urls=[],
            ))
            # 10-05 复审 R07: 只把**实际返回**的条目记进 seen。原来对每条产出都 `seen.add(guid)`
            # 且 state 在 `records[:limit]` 截断**之前**落盘 → 被 limit 截掉的 guid 已进 state,
            # 下一轮 `if guid in seen: continue` 直接跳过 → 永久丢失, 无任何日志。
            # 真实入口是 `cli.py` 的 `--limit` / `--limit-per-adapter`。
            produced_keys.append(key)
            produced.add(key)

        records.sort(key=lambda r: r.item.ts, reverse=True)
        selected = records[:limit] if limit is not None else records
        if limit is not None and len(records) > len(selected):
            logger.info("who_don 本轮产出 %d 条, limit=%d 只返回 %d 条; "
                        "落选的 %d 条不进 seen, 下一轮继续抓",
                        len(records), limit, len(selected), len(records) - len(selected))
        keep = {id(r) for r in selected}
        seen.update(k for k, r in zip(produced_keys, records) if id(r) in keep)
        _save_state(self.state_path, seen)
        return selected
