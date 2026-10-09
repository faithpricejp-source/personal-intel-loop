"""中国外交部 领事服务网 安全提醒 (`cn_consular`)。

报纸「风险提示栏」四条官方源之一(见 `docs/paper_v2_contract.md` 第 9 节)。这是**唯一一条
中国大陆官方口径**的风险源 —— 日本的 MOFA / 美国国务院 / WHO 都不说"你这一趟该不该走"。

## 接口(2026-10-04 实测)

列表页 `GET https://cs.mfa.gov.cn/aqtx/`(首页导航「安全提醒」指向它)。
**静态 HTML, 没有 XHR、没有 JSON API**, 直接解析:
```html
<ul class="news-list">
  <li><img .../><a href="./202609/t20260914_12021693.html">标题</a><span>2026-09-14</span></li>
  ...
</ul>
```
实测 15 条/页。翻页规律实测就是 `index_<N>.html`:
- 第 1 页 `https://cs.mfa.gov.cn/aqtx/` (200, 64601B)
- 第 2 页 `https://cs.mfa.gov.cn/aqtx/index_1.html` (200, 64814B, 15 条, 内容与第 1 页不重复)
按设计规格**只取第 1–2 页**。

详情页 `GET https://cs.mfa.gov.cn/aqtx/202609/t20260914_12021693.html`(200, 59183B):
- 标题 `<h1 class="article-title">`
- 时间 `<div class="article-meta"><span>发布时间：2026-09-14 14:55</span></div>`
- 正文 `<div class="article-content">` 里一个 `<div class="view_default TRS_UEDITOR…">`

## 坑
- **列表页与详情页给的是同一个日期口径**(`2026-09-14`), 详情页多给时分。列表页已经够用来做
  时间窗判断, 所以只在正文缺失时才去抓详情页。
- 相对链接是 `./YYYYMM/tYYYYMMDD_ID.html`, 要用 `urljoin` 补 `https://cs.mfa.gov.cn/aqtx/`。
- ⚠ **编码**: 页面是 UTF-8(`<meta charset="UTF-8">`) 但**响应头没有 charset**,
  requests 于是按 ISO-8859-1 解, 直接用 `r.text` 拿到的是 `ä¸­å\x8d£é¢\x84` 这种乱码
  (实测第一轮真网跑出来整条标题报废)。自己从 `r.content` 解。
- 第 2 页那几条的 **URL 日期(202607)与列表显示日期(2025-11/12)不一致** —— 官方如此
  (条目是 2025 年发布、2026-07 补录/迁移的)。用列表给的日期判时间窗, 不要从 URL 反解。
- ⚠ 详情页有**两个 `<h1>`**: 第一个是站点 logo(`中国领事服务网`), 第二个才是文章标题
  (`<h1 class="article-title">`)。取第一个会把整条标题变成"中国领事服务网"(实测踩过)。
- 标题里**没有结构化的国家字段**, `regions` 靠官方固定句式抽:
  「暂勿前往X」「近期谨慎前往X」「在X中国公民」「赴X中国公民」。抽不出 → `综合`(设计规格规定)。
  一条提醒可能涉及多个国家(实测「塔吉克斯坦和阿富汗边境地区」), 用 `和/及/、` 拆。
- 站点自称 `html.list-page`, 正文 class 是 `article-content`; 但**同名站 `/ljmdd/...` 的容器 class 不同**,
  本 adapter 只认 `aqtx` 栏目, 不去碰别的栏目。
"""
from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urljoin

import requests

from personal_intel_loop import DATA_DIR
from personal_intel_loop.schemas import Item, compute_item_id

logger = logging.getLogger(__name__)

ISSUER = "中华人民共和国外交部"
LIST_BASE = "https://cs.mfa.gov.cn/aqtx/"
PAGE_URLS = (LIST_BASE, urljoin(LIST_BASE, "index_1.html"))
DEFAULT_STATE_PATH = DATA_DIR / "cache" / "cn_consular_state.json"
FALLBACK_REGION = "综合"

REQUEST_TIMEOUT = 20
DEFAULT_DAYS = 14
BODY_LIMIT = 20_000
SEEN_LIMIT = 4000
CST = timezone(timedelta(hours=8))

_LIST_ITEM_RE = re.compile(
    r'<a href="(?P<href>[^"]*t(?P<date>\d{8})_\d+\.html)">(?P<title>.*?)</a>\s*'
    r"<span>(?P<span_date>\d{4}-\d{2}-\d{2})</span>",
    re.S,
)
_PUBLISHED_RE = re.compile(r"发布时间：\s*(\d{4})-(\d{2})-(\d{2})(?:\s+(\d{2}):(\d{2}))?")
_H1_RE = re.compile(r'<h1[^>]*>(.*?)</h1>', re.S)
#: 详情页有**两个** `<h1>`: 第一个是站点 logo(`<h1>中国领事服务网</h1>`), 第二个才是文章标题
#: (`<h1 class="article-title">…</h1>`)。取第一个会把标题变成"中国领事服务网"(实测踩过)。
_ARTICLE_TITLE_RE = re.compile(
    r'<h1[^>]*class="[^"]*article-title[^"]*"[^>]*>(.*?)</h1>', re.S)
_DIV_RE = re.compile(r"<div\b|</div>")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t　]+")

#: 标题里出现这些词, 说明后面跟的是地名(实测 30 条标题归纳出来的)。
_GEO_MARKERS = ("前往", "赴", "驻留", "在")
#: 地名后面的这些词不是地名的一部分, 要剥掉。
#: ⚠ 10-05 复审 R12: **必须按长度降序**。剥噪是「按词表顺序、`while` 反复剥」, 所以短词排在
#: 长词前面会先把长词的后半截剥掉: `地区` 曾排在 `周边地区` 之前, 于是
#: `伊朗及周边地区` 先被剥成 `伊朗及周边`, 此后再不匹配任何噪声词 → `周边` 被当成一个地名
#: (实测真实列表页 2026-03-11 那条抽出 `['伊朗', '周边']`)。降序后先剥 `周边地区` -> `伊朗及`
#: -> 拆成 `['伊朗']`。这里直接按长度排, 不靠人工维护顺序。
_GEO_TAIL_NOISE = tuple(sorted(
    ("边境地区", "东部地区", "地区", "周边地区", "全境", "旅游", "人员",
     "中国公民", "中国学生学者", "中国公民和机构", "和机构"),
    key=len, reverse=True))

#: 10-05 复审 R12: 剥完并列符后仍可能剩下**非地名**的碎片(`周边`/`附近`/`一带` 等
#: 以数量/方位词构指的泛指)。这些不是国家/地区实体, 收进 regions 会多出一个假桶。
_NOT_A_PLACE = frozenset({
    "周边", "附近", "一带", "周边国家", "周边地区", "全境", "地区", "边境地区",
    "东部地区", "当地", "本地", "该国", "各国", "多国",
})


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


def _text(raw: str) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", raw))).strip()


def _parse_date(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").replace(tzinfo=CST)
    except (ValueError, AttributeError):
        return None


def _split_regions(text: str) -> list[str]:
    """`塔吉克斯坦和阿富汗边境地区` → `["塔吉克斯坦", "阿富汗"]`。

    10-05 复审 R12: 拆完再滤掉非地名碎片(`周边` 之类, 见 `_NOT_A_PLACE`)。
    """
    parts = re.split(r"[和及、与]", text)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if not p or p in _NOT_A_PLACE:
            continue
        out.append(p)
    return out


def regions_from_title(title: str) -> list[str]:
    """从官方标题抽国家/地区名。抽不出返回 [](调用方落 `综合`)。"""
    for marker in _GEO_MARKERS:
        idx = title.find(marker)
        if idx < 0:
            continue
        tail = title[idx + len(marker):]
        # 「在委内瑞拉中国公民加强…」/「在尼泊尔中国公民和机构…」→ 到「中国公民」为止
        for stop in ("中国公民", "中国学生", "中国驻"):
            pos = tail.find(stop)
            if pos > 0:
                tail = tail[:pos]
        changed = True
        while changed:
            changed = False
            for noise in _GEO_TAIL_NOISE:
                if tail.endswith(noise) and len(tail) > len(noise):
                    tail = tail[: -len(noise)].strip()
                    changed = True
        tail = tail.strip("的")
        if not tail or len(tail) > 40:
            continue
        return _split_regions(tail)
    return []


def parse_list(page_html: str, base_url: str = LIST_BASE) -> list[dict[str, object]]:
    """列表页 HTML → 条目 dict(不含正文)。结构变了返回 []。"""
    out: list[dict[str, object]] = []
    for m in _LIST_ITEM_RE.finditer(page_html or ""):
        title = _text(m.group("title"))
        ts = _parse_date(m.group("span_date"))
        if not (title and ts):
            continue
        out.append({
            "title": title,
            "url": urljoin(base_url, m.group("href")),
            "ts": ts,
            "regions": regions_from_title(title),
        })
    return out


def _balanced_div(html_text: str, anchor: int) -> str:
    """`<div class="article-content">` 的内容。正文里**嵌套了一层 div**
    (`view_default TRS_UEDITOR…`, 实测 2026-09-14 那篇), 所以不能拿第一个 `</div>` 当结尾,
    得从开标签起数括号 —— 少了这一步 body 会是空串。
    """
    depth = 0
    for m in _DIV_RE.finditer(html_text, anchor):
        depth += 1 if m.group(0) == "<div" else -1
        if depth == 0:
            open_end = html_text.index(">", anchor) + 1
            return html_text[open_end:m.start()]
    return ""


def parse_detail(page_html: str) -> dict[str, object]:
    """详情页 HTML → `{title, ts, body}`。取不到正文时 `body` 为 ""。"""
    # 不做 _H1_RE 兜底 —— 站点改版丢掉 article-title class 时, 兜底命中的是
    # 站点 logo `<h1>中国领事服务网</h1>`(见文件头 35-36 行的实测坑), 返回后 collect 的
    # `title = detail["title"] or title` 会用非空 logo 覆盖列表页好标题。取不到就返回
    # 空串, 让 collect 回落到列表页标题。
    h1 = _ARTICLE_TITLE_RE.search(page_html or "")
    body = ""
    anchor = page_html.find('<div class="article-content"')
    if anchor >= 0:
        raw = _balanced_div(page_html, anchor)
        raw = re.sub(r"<(script|style).*?</\1>", " ", raw, flags=re.S | re.I)
        raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
        raw = re.sub(r"</(p|div|li|tr|h\d)>", "\n", raw, flags=re.I)
        body = _text(raw)
        body = "\n".join(ln.strip() for ln in body.splitlines() if ln.strip())
    ts = None
    pub = _PUBLISHED_RE.search(page_html or "")
    if pub:
        y, mo, d, hh, mm = pub.groups()
        ts = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), tzinfo=CST)
    return {"title": _text(h1.group(1)) if h1 else "", "ts": ts, "body": body}


# ---- 网络(可注入)----------------------------------------------------------

_META_CHARSET_RE = re.compile(rb'<meta[^>]+charset=["\']?\s*([\w-]+)', re.I)


def _decode(r: requests.Response) -> str:
    """按 `<meta charset>` 解。解不出来返回 ""。

    响应头是 `Content-Type: text/html`(**没有 charset**, 实测), requests 于是把
    `r.encoding` 设成 `ISO-8859-1`; 直接用 `r.text` 拿到的是 `ä¸­å\x8d£é¢\x84` 这种乱码
    (实测第一轮真网跑整条标题报废)。所以只能自己从 bytes 解。
    """
    declared = _META_CHARSET_RE.search(r.content[:1024])
    encodings = [declared.group(1).decode("ascii", "ignore")] if declared else []
    encodings.append("utf-8")
    for encoding in encodings:
        try:
            return r.content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return ""


def _get(url: str) -> str:
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    except requests.RequestException as exc:
        logger.warning("cn_consular fetch failed %s: %s", url, exc)
        return ""
    if r.status_code != 200:
        logger.warning("cn_consular non-200 %s: %s", url, r.status_code)
        return ""
    return _decode(r)


def _fetch_list(url: str) -> str:
    return _get(url)


def _fetch_detail(url: str) -> str:
    return _get(url)


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
        logger.warning("cn_consular state 写不进去 %s: %s", path, exc)


class CnConsularAdapter:
    name = "cn_consular"

    def __init__(
        self,
        *,
        fetch_list: Callable[[str], str] | None = None,
        fetch_detail: Callable[[str], str] | None = None,
        state_path: Path | None = None,
        days: int = DEFAULT_DAYS,
        pages: int = 2,
    ) -> None:
        self._fetch_list = fetch_list or _fetch_list
        self._fetch_detail = fetch_detail or _fetch_detail
        self.state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        self.days = days
        # 设计规格: 只取第 1-2 页
        self.page_urls = PAGE_URLS[:max(1, min(pages, len(PAGE_URLS)))]

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable[ItemRecord]:
        # 10-05 复审 R09 同型: naive `since` 按站点所在地时区(北京时间)解释, 不依赖宿主 TZ。
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=CST)
        cutoff = since.astimezone(timezone.utc) if since else (
            datetime.now(timezone.utc) - timedelta(days=self.days))
        seen = _load_state(self.state_path)

        entries: dict[str, dict[str, object]] = {}
        for page_url in self.page_urls:
            for entry in parse_list(self._fetch_list(page_url), page_url):
                entries.setdefault(str(entry["url"]), entry)   # 翻页重叠时以第 1 页为准

        # 10-05 复审 R13: 先用**列表页已有的日期**做窗口筛选, 再决定抓几个详情页。
        # 原来对每条通过初筛的条目都无条件抓详情页(实测两页 30 条 -> 每轮固定 30 次请求),
        # 与文件头「列表页已够用来做时间窗判断, 所以只在正文缺失时才去抓详情页」矛盾 ——
        # 列表页根本没给正文, 「正文缺失」等于「每条都抓」。请求量翻倍且无退避, 连续多轮容易
        # 被限流; 一旦详情页全失败, 这一轮所有条目全丢。
        # 现在: 先按列表日期过滤 + 排序 + (若有 limit)截断, 只对**真正要返回的**条目抓详情。
        candidates = [(url, entry) for url, entry in entries.items()
                      if url not in seen
                      and isinstance(entry["ts"], datetime)
                      and entry["ts"].astimezone(timezone.utc) >= cutoff]
        candidates.sort(key=lambda pair: pair[1]["ts"], reverse=True)  # type: ignore[operator]
        if limit is not None and len(candidates) > limit:
            logger.info("cn_consular 窗口内 %d 条, limit=%d 只抓 %d 个详情页; "
                        "落选的 %d 条不进 seen, 下一轮继续抓",
                        len(candidates), limit, limit, len(candidates) - limit)
            candidates = candidates[:limit]

        records: list[ItemRecord] = []
        produced: list[str] = []       # 与 records 同序, 供截断后决定谁进 seen(R11)
        for url, entry in candidates:
            ts = entry["ts"]
            assert isinstance(ts, datetime)

            body = ""
            title = str(entry["title"])
            detail = parse_detail(self._fetch_detail(url))
            if detail["body"]:
                body = str(detail["body"])
                title = str(detail["title"]) or title
                # 详情页的时间带时分, 比列表页准
                if isinstance(detail["ts"], datetime):
                    ts = detail["ts"]
                    if ts.astimezone(timezone.utc) < cutoff:
                        continue
            if not body:
                logger.info("cn_consular %s 拿不到正文,跳过", url)
                continue

            regions = list(entry["regions"]) or regions_from_title(title)
            label = regions[0] if regions else FALLBACK_REGION
            published = ts.astimezone(CST)
            records.append(ItemRecord(
                item=Item(
                    id=compute_item_id(f"{self.name}:{label}", url=url),
                    source=f"{self.name}:{label}",
                    url=url,
                    title=title[:512],
                    body=body[:BODY_LIMIT],
                    author=ISSUER,
                    ts=ts,
                    lang="zh",
                    tags=["风险提示", label],
                ),
                adapter_name=self.name,
                source_payload_json=json.dumps(
                    {"kind": "risk", "issuer": ISSUER,
                     "published": published.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                     "regions": regions or [FALLBACK_REGION],
                     "level": None,          # 安全提醒 没有官方等级(实测无等级字段/无等级措辞)
                     },
                    ensure_ascii=False),
                media_urls=[],
            ))
            produced.append(url)

        # 10-05 复审 R11: 只把**实际返回**的条目记进 seen。原来对每条产出都 `seen.add(url)`
        # 且 state 在 `records[:limit]` 截断**之前**落盘 -> 被 limit 截掉的 url 已进 state,
        # 下一轮 `if url in seen: continue` 直接跳过 -> 永久丢失, 无任何日志。
        # 真实入口是 `cli.py` 的 `--limit` / `--limit-per-adapter`。
        records.sort(key=lambda r: r.item.ts, reverse=True)
        selected = records[:limit] if limit is not None else records
        keep = {id(r) for r in selected}
        seen.update(u for u, r in zip(produced, records) if id(r) in keep)
        _save_state(self.state_path, seen)
        return selected
