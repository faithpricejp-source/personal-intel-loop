"""通用 HTML 列表采集器 (`html_columns`) —— 中日文「人情味」栏目 SSR 列表页。

合同依据 `docs/paper_v2_contract.md` 第 8 节: 温暖栏要**具名、可核实的真人真事**。
调研结论是中日文人情味栏目基本没有 RSS, 但少数几个列表页是**服务端直出
HTML(SSR)**, 直接正则就能解析。本 adapter 就是接这类列表页的通用盒子: 栏目定义全在
`config/html_columns.json`, 加栏目不改代码。

## 为什么不用 CSS 选择器 / bs4

只依赖标准库 `re` + `html.parser`(设计规格冻结范围), 不引新依赖。`item_regex` 是主路径,
`item_selector` 只是个受限语法(见 `_parse_by_selector`)的兜底, 用于结构规整到能按标签
链定位的列表页。

## 日期一律按配置取, 取不到就丢条目(2026-10-05 实测)

`date_source` 三种:

- `url_path`: 对**绝对 URL** 跑 `date_regex`。中工网条目日期写在路径里
  (`/c/2026-10-01/8907492.shtml`), 列表文本里另有发布时间, 两者一致, 路径更稳。
- `list_text`: 对**列表页里该条目的匹配片段**跑 `date_regex`(原始 HTML, 不剥标签 ——
  `<time datetime="...">` 这种属性值只有不剥标签才拿得到)。
- `detail_meta`: 抓详情页再跑 `date_regex`。最贵(每条多一次请求), 只在列表页确实没给
  日期时用。

**读売这条要特别说明**: URL 里的 `20261001` **不是**发布日期 —— 实测 2026-10-05 抓到的
`/serial/jidai/20261001-GYT8T00227/`, 列表页 `<time datetime="2026-10-02T10:00">`,
条目实际发布于 10-02。URL 路径比真实发布时间早一天。所以読売必须走 `list_text`。
**「日期在 URL 路径」这个结论对読売不成立**, 别照搬。

**取不到日期就丢这条, 不用抓取时刻冒充** —— 温暖栏按核实优先, 假时间比不产出更糟。

## `render: "browser"` —— 列表页要无头浏览器(2026-10-05 实测)

前三个栏目 SSR 直出, 但中新网「新闻浮世绘」与中日新聞「あの人に迫る」的**静态 HTML 里
列表是空的**: 条目由 JS 拿数据后渲染, 必须开浏览器。配置里加 `"render": "browser"`
(缺省 `"http"`, 即前三个栏目的行为完全不变), browser 模式用 playwright 打开 `list_url`
取渲染后的 HTML, **再走同一套 `item_regex`/`item_selector`** —— 解析层不因渲染方式分叉。

实测姿势(踩过的坑):

1. **`wait_until="networkidle"` 当 `goto` 条件会超时**(中日实测 12s 不达成), 必须拆开:
   `goto(domcontentloaded)` → `wait_for_load_state("networkidle", 12s)`(超时也继续) →
   滚轮 N 次触发懒加载 → `page.content()`。
2. **中新网要滚轮**(静态 HTML 空列表就是懒加载没触发); 中日滚不滚都能拿到条目,
   但滚一下更稳, 所以 `render_scroll` 做成可配置。
3. **详情页不需要浏览器**: 实测两站详情页普通 requests 都 200, trafilatura 能抽到
   700~1000 字正文。所以 `render_detail` 单独一个选项, 缺省 `"http"`; 真要开浏览器时
   走同一个浏览器实例。
4. 页面上可能同时存在 **JS 模板字符串区块**(中新网 offset~33912 是 `+ doc.url` 拼接的
   模板)。正则只要匹配真实 URL 字面量就不会误伤模板 —— 但自己写正则时要留意。

浏览器部分封成 `_Browser`(惰性 import, `browsers_path` 可注入), 测试注入假 `render_list`
不起浏览器。**playwright 没装时 browser 栏目记 warning 跳过, 不影响 http 栏目**
(`_render_unavailable` 标记 → 该栏目本轮0 条并提示, 其余栏目照跑)。

## 付费墙

日文大报正文受付费墙, 但列表页的标题/日期/URL 免费(实测 朝日 200/168KB, 正文只拿到
333 字导语 + `有料記事` 字样)。导语本身可读, 所以**照收**, 只在 `source_payload` 里
`paywalled: true` 标记, 让下游知道正文不完整。

## 结构改版监控

一条都匹配不到(而页面本身有正文, 不是空页)→ 记 warning 并在 state 里
`zero_match[key].zero_match_runs += 1`; 连续 3 轮为 0 → 日志升级 **error**, 方便维护者
在采集日志里直接发现页面改版。**抓取失败(网络超时/5xx/渲染失败, fetch 返回空串)不进
这个计数** —— 那是网络问题, 不是页面改版(10-05 审计 D10)。
"""
from __future__ import annotations

import html
import html.parser
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests

from personal_intel_loop import CONFIG_DIR, DATA_DIR
from personal_intel_loop.schemas import Item, compute_item_id

logger = logging.getLogger(__name__)

ADAPTER_NAME = "html_columns"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "html_columns.json"
DEFAULT_STATE_PATH = DATA_DIR / "cache" / "html_columns_state.json"

REQUEST_TIMEOUT = 30
DEFAULT_DAYS = 7
DEFAULT_MAX_PER_RUN = 10
BODY_LIMIT = 20_000
SEEN_LIMIT = 3000
ZERO_MATCH_ERROR_AT = 3
#: 纪律: 同一主机请求间隔 ≥3 秒(实测三站都是普通 requests, 不需要浏览器)
MIN_HOST_INTERVAL = 3.2
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ---- browser 模式参数(实测, 见模块 docstring) ----
RENDER_NAV_TIMEOUT_MS = 45_000
RENDER_IDLE_TIMEOUT_MS = 12_000     # 中日实测 networkidle 12s 不达成, 超时要继续
RENDER_SETTLE_MS = 1_200
RENDER_SCROLL_PAUSE_MS = 900
DEFAULT_RENDER_SCROLL = 6

#: 付费墙判据: 命中任一标记, 或正文短到只剩导语。
PAYWALL_MARKERS = ("有料記事", "この記事の続きが読める", "購読CNS")
PAYWALL_BODY_CHARS = 400

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


@dataclass
class Column:
    """一个栏目的采集定义(对应 `config/html_columns.json` 里的一项)。"""

    key: str
    name: str
    list_url: str
    lang: str
    kind: str = "warmth"
    item_regex: str | None = None
    item_selector: str | None = None
    title_group: str = "title"
    url_group: str = "url"
    date_source: str = "url_path"
    date_regex: str | None = None
    #: 日期无时刻时的本地时区。默认 Asia/Tokyo(读卖/朝日是 JST); 中工网配 Asia/Shanghai。
    tz: str = "Asia/Tokyo"
    max_per_run: int = DEFAULT_MAX_PER_RUN
    #: 列表页取法: `"http"`(默认, SSR 直出) 或 `"browser"`(JS 渲染, 需 playwright)。
    #: 缺省 http —— 前三个栏目行为不变。
    render: str = "http"
    #: 详情页取法, 语义同`render`, 缺省 http(实测两个browser 栏目的详情页普通请求都 200)。
    render_detail: str = "http"
    #: browser 模式滚轮次数(触发懒加载)。中新网必须滚, 中日不滚也能拿到。
    render_scroll: int = DEFAULT_RENDER_SCROLL
    _rx: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    @property
    def needs_browser(self) -> bool:
        return self.render == "browser" or self.render_detail == "browser"

    @property
    def source(self) -> str:
        return f"{ADAPTER_NAME}:{self.key}"

    def compiled_item_regex(self) -> re.Pattern[str] | None:
        """`item_regex` 编译结果。直接 `Column(...)` 构造时(不经 load_columns)也能用:
        懒编译一次并缓存, 语法错返回 None(记 warning), 不抛 AssertionError。"""
        if self.item_regex is None:
            return None
        if self._rx is None:
            try:
                self._rx = re.compile(self.item_regex, re.S)
            except (re.error, TypeError) as exc:
                # 10-05 审计 D12: item_regex 非 str 时是 TypeError, 不是 re.error
                logger.warning("html_columns: 栏目 %s item_regex 语法错: %s", self.key, exc)
                return None
        return self._rx


@dataclass
class RawItem:
    """列表页里抽出来、还没定时间戳的一条。"""

    title: str
    url: str
    block: str
    ts: datetime | None = None


def _clean_text(raw: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html.unescape(raw))).strip()


# ---------------------------------------------------------------- 配置加载


def load_columns(path: Path | str | None = None) -> list[Column]:
    """读栏目配置。**文件不存在返回空列表**(不是报错) —— 没配栏目就什么都不采。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    try:
        raw = json.loads(cfg_path.read_text("utf-8"))
    except FileNotFoundError:
        logger.info("html_columns: 配置不存在 %s, 无栏目可采", cfg_path)
        return []
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("html_columns: 配置读不进去 %s: %s", cfg_path, exc)
        return []
    if not isinstance(raw, list):
        logger.warning("html_columns: 配置根节点不是数组 %s", cfg_path)
        return []

    out: list[Column] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        col = _column_from_dict(entry)
        if col is None:
            continue
        # 正则当场编一次: 语法错当场跳过, 不留到跑的时候才炸
        if col.item_regex and col.compiled_item_regex() is None:
            continue
        out.append(col)
    return out


def _column_from_dict(entry: dict[str, Any]) -> Column | None:
    key = str(entry.get("key") or "").strip()
    list_url = str(entry.get("list_url") or "").strip()
    if not (key and list_url):
        logger.warning("html_columns: 缺 key 或 list_url, 跳过 %r", entry.get("key"))
        return None
    if not (entry.get("item_regex") or entry.get("item_selector")):
        logger.warning("html_columns: 栏目 %s 既没 item_regex 也没 item_selector, 跳过", key)
        return None
    # 10-05 审计 D12: 正则/选择器写成非字符串(数字/布尔)时, re.compile 抛 TypeError,
    # `except re.error` 接不住 —— 按语法错同款处理: 只跳过这条, 不炸整轮采集。
    for field_name in ("item_regex", "item_selector", "date_regex"):
        value = entry.get(field_name)
        if value is not None and not isinstance(value, str):
            logger.warning("html_columns: 栏目 %s 的 %s 不是字符串, 跳过该栏目", key, field_name)
            return None
    date_source = str(entry.get("date_source") or "url_path")
    if date_source not in ("url_path", "list_text", "detail_meta"):
        logger.warning("html_columns: 栏目 %s date_source 非法 %r, 按 url_path", key, date_source)
        date_source = "url_path"
    # render / render_detail 只认 http|browser, 其它值退回 http(而不是让栏目永远失败)
    render = _render_mode(entry.get("render"), key, "render")
    render_detail = _render_mode(entry.get("render_detail"), key, "render_detail")
    # 10-05 审计 D12: max_per_run / render_scroll 写成非数字时 int() 抛 ValueError,
    # 同样只回默认值, 不让单条坏配置连坐其他栏目。
    try:
        max_per_run = int(entry.get("max_per_run") or DEFAULT_MAX_PER_RUN)
    except (TypeError, ValueError):
        logger.warning("html_columns: 栏目 %s max_per_run 非数字 %r, 按默认 %d",
                       key, entry.get("max_per_run"), DEFAULT_MAX_PER_RUN)
        max_per_run = DEFAULT_MAX_PER_RUN
    try:
        render_scroll = int(entry.get("render_scroll") or DEFAULT_RENDER_SCROLL)
    except (TypeError, ValueError):
        logger.warning("html_columns: 栏目 %s render_scroll 非数字 %r, 按默认 %d",
                       key, entry.get("render_scroll"), DEFAULT_RENDER_SCROLL)
        render_scroll = DEFAULT_RENDER_SCROLL
    return Column(
        key=key,
        name=str(entry.get("name") or key),
        list_url=list_url,
        lang=str(entry.get("lang") or "ja"),
        kind=str(entry.get("kind") or "warmth"),
        item_regex=entry.get("item_regex") or None,
        item_selector=entry.get("item_selector") or None,
        title_group=str(entry.get("title_group") or "title"),
        url_group=str(entry.get("url_group") or "url"),
        date_source=date_source,
        date_regex=entry.get("date_regex") or None,
        tz=str(entry.get("tz") or "Asia/Tokyo"),
        max_per_run=max_per_run,
        render=render,
        render_detail=render_detail,
        render_scroll=render_scroll,
    )


def _render_mode(raw: Any, key: str, field_name: str) -> str:
    value = str(raw or "http").strip().lower()
    if value not in ("http", "browser"):
        logger.warning("html_columns: 栏目 %s 的 %s 非法 %r, 按 http", key, field_name, raw)
        return "http"
    return value


# ---------------------------------------------------------------- 日期解析


def parse_date(text: str, date_regex: str | None, tz: str) -> datetime | None:
    """按 `date_regex` 从 `text` 取时间戳。

    `date_regex` 用命名组 `y m d H M`(分/秒可选, 默认 0), 例如::

        (?P<y>\\d{4})年(?P<m>\\d{1,2})月(?P<d>\\d{1,2})日\\s*(?P<H>\\d{1,2})時(?P<M>\\d{1,2})分

    日期无时刻 → 当天 00:00 本地时间, 再转 UTC。解析不出来返回 None(调用方丢条目)。
    """
    if not text or not date_regex:
        return None
    try:
        rx = re.compile(date_regex)
    except re.error as exc:
        logger.warning("html_columns: date_regex 语法错: %s", exc)
        return None
    m = rx.search(text)
    if not m:
        return None
    groups = m.groupdict()
    try:
        naive = datetime(
            int(groups["y"]), int(groups["m"]), int(groups["d"]),
            int(groups.get("H") or 0), int(groups.get("M") or 0),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("html_columns: 日期字段缺失或越界 %r", m.group(0))
        return None
    try:
        loc = ZoneInfo(tz)
    except Exception:                       # 配错时区不该让整轮采集挂掉
        logger.warning("html_columns: 未知时区 %r, 退回 UTC", tz)
        loc = timezone.utc
    return naive.replace(tzinfo=loc).astimezone(timezone.utc)


def _resolve_ts(
    col: Column,
    raw: RawItem,
    *,
    detail_html: Callable[[str], str] | None = None,
) -> datetime | None:
    if col.date_source == "url_path":
        return parse_date(raw.url, col.date_regex, col.tz)
    if col.date_source == "list_text":
        return parse_date(raw.block, col.date_regex, col.tz)
    if col.date_source == "detail_meta" and detail_html is not None:
        return parse_date(detail_html(raw.url), col.date_regex, col.tz)
    return None


# ---------------------------------------------------------------- 列表解析


def _group(match: re.Match[str], name: str) -> str:
    """按名字取组; 不是合法组名时当序号用(允许配置写 `1` / `2`)。

    组不存在时 `match.group()` 抛 IndexError, 组名非法时抛 error —— 两种都当"没抽到"。
    """
    try:
        value = match.group(name)
    except (IndexError, re.error):
        logger.warning("html_columns: item_regex 里没有名为 %r 的组", name)
        return ""
    return (value or "").strip()


def parse_list(html_text: str, col: Column) -> list[RawItem]:
    """列表页 HTML → 原始条目(未去重、未定 ts)。结构不匹配返回 [](调用方记 zero_match)。"""
    if not html_text:
        return []
    blocks = (
        _blocks_by_regex(html_text, col) if col.item_regex
        else _blocks_by_selector(html_text, col)
    )
    out: list[RawItem] = []
    for title, href, block in blocks:
        if not (title and href):
            continue
        out.append(RawItem(
            title=_clean_text(title),
            url=urljoin(col.list_url, html.unescape(href)),
            block=block,
        ))
    return out


def _blocks_by_regex(html_text: str, col: Column) -> list[tuple[str, str, str]]:
    rx = col.compiled_item_regex()
    if rx is None:
        return []
    out: list[tuple[str, str, str]] = []
    for m in rx.finditer(html_text):
        title, href = _group(m, col.title_group), _group(m, col.url_group)
        if not (title and href):
            continue
        out.append((title, href, m.group(0)))
    return out


class _Node:
    __slots__ = ("tag", "classes", "attrs", "parent", "children", "text_parts")

    def __init__(self, tag: str, attrs: dict[str, str], parent: "_Node | None") -> None:
        self.tag = tag
        self.attrs = attrs
        self.classes = set((attrs.get("class") or "").split())
        self.parent = parent
        self.children: list[_Node] = []
        self.text_parts: list[str] = []


#: 10-05 审计 D11: HTML 允许省略闭合标签的元素。键 = 会隐式闭合前一个元素的 starttag,
#: 值 = (要闭合的标签集合, 搜索边界集合 —— 向上找时碰到边界就停, 免得把外层列表的项也闭掉)。
_IMPLIED_CLOSE = {
    "li": ({"li"}, {"ul", "ol", "menu"}),
    "dt": ({"dt", "dd"}, {"dl"}),
    "dd": ({"dt", "dd"}, {"dl"}),
    "tr": ({"tr"}, {"table", "thead", "tbody", "tfoot"}),
    "td": ({"td", "th"}, {"tr", "table", "thead", "tbody", "tfoot"}),
    "th": ({"td", "th"}, {"tr", "table", "thead", "tbody", "tfoot"}),
    "option": ({"option"}, {"select", "optgroup", "datalist"}),
    "p": ({"p"}, set()),
}

#: 10-05 审计 E13: HTML5 里这些块级 starttag 会关闭 button scope 内开放的 <p>
#: (div/ul/h1 等不再嵌进未闭合的 p)。boundary 集合照 button scope 的 scope 定义截断。
_P_CLOSING_STARTTAGS = frozenset({
    "address", "article", "aside", "blockquote", "center", "details", "dialog",
    "dir", "div", "dl", "dt", "dd", "fieldset", "figcaption", "figure", "footer",
    "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "li",
    "listing", "main", "menu", "nav", "ol", "p", "pre", "section", "summary",
    "table", "ul", "xmp",
})
_P_SCOPE_BOUNDARIES = frozenset({
    "applet", "caption", "html", "marquee", "object", "select", "table", "td",
    "th", "template", "button",
})


class _TreeBuilder(html.parser.HTMLParser):
    """只建标签树(不取 script/style 内容), 给 `item_selector` 用。"""

    _SKIP = {"script", "style", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("", {}, None)
        self._cur = self.root
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        self._imply_end(tag)
        node = _Node(tag, {k: (v or "") for k, v in attrs}, self._cur)
        self._cur.children.append(node)
        if tag not in ("meta", "link", "br", "img", "hr", "input"):
            self._cur = node

    def _imply_end(self, tag: str) -> None:
        """未写闭合标签的元素(`<li>`/`<p>` 等)由同层下一个 starttag 隐式闭合。

        不补这一步的话 `<ul><li>A<li>B</ul>` 的第二条 li 会嵌进第一条里,
        `ul > li` 子代链只匹配得到第一条, 静默漏抓(parsed 仍 >0, 改版监控不报警)。
        10-05 审计 E13: 块级 starttag(div/ul/h1 等)还要先关闭 button scope 内开放的
        `<p>`, 否则 `<div class="item"><a>…</a><p>摘要<div class="item">…` 的第二个
        item 会整个嵌进第一个 `<p>` 里, `div.list > div.item` 只匹配得到第一条。
        """
        if tag in _P_CLOSING_STARTTAGS:
            self._close_open_p()
        rule = _IMPLIED_CLOSE.get(tag)
        if rule is None:
            return
        closers, stops = rule
        node = self._cur
        while node is not self.root and node.tag not in stops:
            if node.tag in closers:
                self._cur = node.parent or self.root
                return
            node = node.parent

    def _close_open_p(self) -> None:
        node = self._cur
        while node is not self.root:
            if node.tag in _P_SCOPE_BOUNDARIES:
                return
            if node.tag == "p":
                self._cur = node.parent or self.root
                return
            node = node.parent

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth or tag in ("meta", "link", "br", "img", "hr", "input"):
            return
        node = self._cur
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self._cur = node.parent or self.root

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._cur.text_parts.append(data)


def _selector_match(node: _Node, chain: list[tuple[str, str]]) -> bool:
    """`chain` 是 [(tag, class), ...], 从根到叶, 逐级向上比对祖先链。"""
    cursor: _Node | None = node
    for tag, cls in reversed(chain):
        if cursor is None or cursor.tag != tag:
            return False
        if cls and cls not in cursor.classes:
            return False
        cursor = cursor.parent
    return True


def _parse_selector_part(part: str) -> tuple[str, str]:
    part = part.strip()
    tag, _, cls = part.partition(".")
    return tag, cls


def _render_node(node: _Node) -> str:
    """粗略重建该节点的 HTML —— 只用于 `date_source: list_text` 找日期, 不求保真。

    属性全部保留: `<time datetime="2026-10-02T10:00">` 的日期只在属性值里,
    丢属性就永远取不到时间戳。
    """
    attr_text = "".join(f' {k}="{v}"' for k, v in node.attrs.items())
    inner = "".join(node.text_parts) + "".join(_render_node(c) for c in node.children)
    if node.tag in ("meta", "link", "br", "img", "hr", "input"):
        return f"<{node.tag}{attr_text}>"
    return f"<{node.tag}{attr_text}>{inner}</{node.tag}>"


def _blocks_by_selector(html_text: str, col: Column) -> list[tuple[str, str, str]]:
    """受限选择器: `tag.class > tag.class`(子代链, 只支持 `>` 与单类名)。

    匹配到的节点里取第一个带 href 的 `<a>` 当条目, 标题优先取 `<h3>`/`<h2>` 的文字,
    否则用 `<a>` 自身文字。
    """
    chain = [_parse_selector_part(p) for p in str(col.item_selector or "").split(">") if p.strip()]
    if not chain:
        return []
    builder = _TreeBuilder()
    try:
        builder.feed(html_text)
        builder.close()
    except Exception as exc:                        # 畸形 HTML 不该让整轮挂掉
        logger.warning("html_columns: %s 列表页解析失败: %s", col.key, exc)
        return []

    out: list[tuple[str, str, str]] = []
    for node in _walk(builder.root):
        if not _selector_match(node, chain):
            continue
        anchor = node if node.tag == "a" and node.attrs.get("href") else _first_anchor(node)
        if anchor is None or not anchor.attrs.get("href"):
            continue
        title_node = _first_tag(node, ("h3", "h2")) or anchor
        title = _WS_RE.sub(" ", "".join(title_node.text_parts)).strip()
        out.append((title, anchor.attrs["href"], _render_node(node)))
    return out


def _walk(node: _Node) -> Iterable[_Node]:
    yield node
    for child in node.children:
        yield from _walk(child)


def _first_anchor(node: _Node) -> _Node | None:
    for cand in _walk(node):
        if cand.tag == "a" and cand.attrs.get("href"):
            return cand
    return None


def _first_tag(node: _Node, tags: tuple[str, ...]) -> _Node | None:
    for cand in _walk(node):
        if cand.tag in tags:
            return cand
    return None


# ---------------------------------------------------------------- 网络(可注入)


class _Throttle:
    """按主机限速, 保证同站请求间隔 ≥ `MIN_HOST_INTERVAL`。"""

    def __init__(self, interval: float = MIN_HOST_INTERVAL) -> None:
        self.interval = interval
        self._last: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc
        prev = self._last.get(host)
        now = time.monotonic()
        if prev is not None:
            gap = now - prev
            if gap < self.interval:
                time.sleep(self.interval - gap)
        self._last[host] = time.monotonic()


def _http_get(url: str, throttle: _Throttle) -> str:
    throttle.wait(url)
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
    except requests.RequestException as exc:
        logger.warning("html_columns: fetch 失败 %s: %s", url, exc)
        return ""
    if r.status_code != 200:
        logger.warning("html_columns: non-200 %s -> %s", url, r.status_code)
        return ""
    # 实测 workercn 只声明 ISO-8859-1, 正文实为 utf-8; apparent_encoding 两种都判得出 utf-8
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def _default_fetch_list(url: str) -> str:
    return _http_get(url, _THROTTLE)


def _default_fetch_detail(url: str) -> str:
    return _http_get(url, _THROTTLE)


# ---------------------------------------------------------------- 浏览器(可注入)


class BrowserUnavailable(RuntimeError):
    """playwright 没装 / 浏览器没下载 —— 调用方记warning 并跳过该栏目。"""


class _Browser:
    """playwright 封装。用法 `with _Browser(...) as b:`, 退出时必定关干净。

    `browsers_path` 对应环境变量 `PLAYWRIGHT_BROWSERS_PATH`(缺省 None = playwright 默认)。
    **必须在 launch() 之前显式写 `os.environ`**: playwright 的 python 绑定 import 时就读过一次
    路径, 事后设环境变量对它无效(同thepaper_warm 的实测结论)。
    """

    def __init__(
        self,
        browsers_path: str | None = None,
        *,
        headless: bool = True,
        throttle: _Throttle | None = None,
    ) -> None:
        self.browsers_path = browsers_path
        self.headless = headless
        self.throttle = throttle or _THROTTLE
        self._pw: Any = None
        self._browser: Any = None
        self._ctx: Any = None
        self._prev_browser_path: str | None = None

    def __enter__(self) -> "_Browser":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:                # 没装 playwright 是部署常态, 不是 bug
            raise BrowserUnavailable(f"playwright 未安装: {exc}") from exc
        # 10-05 审计 E16: 写全局环境变量前记下旧值, 退出时恢复 —— 否则整个进程此后
        # 的 playwright 都被指到这个路径(跨轮、跨模块泄漏)。
        self._prev_browser_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        if self.browsers_path:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = self.browsers_path
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=self.headless,
                # 去掉 automation 标记, 降低被 WAF 认出来的概率
                args=["--disable-blink-features=AutomationControlled"],
            )
            self._ctx = self._browser.new_context(
                viewport={"width": 1440, "height": 900},
                locale="ja-JP", timezone_id="Asia/Tokyo",
                user_agent=USER_AGENT,
            )
        except Exception as exc:                  # 浏览器没下载等
            self.__exit__()
            raise BrowserUnavailable(f"chromium 起不来: {type(exc).__name__}: {exc}") from exc
        return self

    def __exit__(self, *exc: Any) -> None:
        for obj, meth in ((self._ctx, "close"), (self._browser, "close"), (self._pw, "stop")):
            if obj is None:
                continue
            try:
                getattr(obj, meth)()
            except Exception as exc:              # 关浏览器失败不该盖掉真正的异常
                logger.info("html_columns: 关浏览器失败(%s)", exc)
        self._restore_browsers_path()

    def _restore_browsers_path(self) -> None:
        """10-05 审计 E16: 恢复进入前旧值。只在本对象真的写过环境变量时动手(幂等,
        失败路径会多调一次); browsers_path 为空时从没写过, 不碰全局。"""
        if not self.browsers_path:
            return
        if self._prev_browser_path is None:
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        else:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = self._prev_browser_path

    def render_html(self, url: str, *, scroll: int = DEFAULT_RENDER_SCROLL) -> str:
        """打开 url 返回**渲染后**的 HTML。失败返回空串(调用方按零匹配处理)。

        顺序是实测定的, 别改: `goto` 用 `domcontentloaded` —— 中日实测 `networkidle`
        当 goto 条件会超时; 然后单独等 networkidle(不达成也继续), 再滚轮触发懒加载,
        最后 `content()` 取序列化后的 DOM。
        """
        if self._ctx is None:
            raise BrowserUnavailable("_Browser 未进入 with 上下文")
        self.throttle.wait(url)
        page = self._ctx.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=RENDER_NAV_TIMEOUT_MS)
            status = resp.status if resp else None
            try:
                page.wait_for_load_state("networkidle", timeout=RENDER_IDLE_TIMEOUT_MS)
            except Exception as exc:# 不达成是常态(中日实测), 继续往下走
                logger.info("html_columns: %s networkidle 未达成(%s), 继续", url,
                            type(exc).__name__)
            for i in range(max(0, scroll)):
                page.mouse.wheel(0, 2000)
                page.wait_for_timeout(RENDER_SCROLL_PAUSE_MS)
            page.wait_for_timeout(RENDER_SETTLE_MS)
            if status is not None and status != 200:
                # 10-05 审计 E14: non-200(403 WAF 拦截页/5xx 错误页)必须与 http 路径同款
                # 返回空串 —— 返回错误页 HTML 会让 D10 的 fetch_failed 不置位, 零匹配被
                # 记成「页面可能改版」, 连续 3 轮误报 error 级报警。
                logger.warning("html_columns: render non-200 %s -> %s", url, status)
                return ""
            return page.content()
        except BrowserUnavailable:
            raise
        except Exception as exc:
            logger.warning("html_columns: render 失败 %s: %s", url,
                           f"{type(exc).__name__}: {exc}"[:160])
            return ""
        finally:
            try:
                page.close()
            except Exception:
                pass


#: 成对的块级标签: 有闭合标签就能整块剥掉
_SCRIPT_BLOCK_RE = re.compile(r"<(script|style|noscript)\b.*?</\1\s*>", re.S | re.I)
#: 10-05 审计 D15: 未闭合的 `<script>`(畸形页)没有闭合标签可配对, 上面的正则切不掉,
#: JS 源码原样留在正文里。浏览器会把余下文档整个当脚本内容吞掉 —— 剥离照此语义切到文档尾。
_UNCLOSED_SCRIPT_RE = re.compile(r"<(?:script|style|noscript)\b[^>]*>.*\Z", re.S | re.I)


def _strip_script_blocks(html_text: str) -> str:
    """剥 `<script>/<style>/<noscript>` 块; 未闭合的按浏览器语义切到文档尾(10-05 审计 D15)。"""
    return _UNCLOSED_SCRIPT_RE.sub(" ", _SCRIPT_BLOCK_RE.sub(" ", html_text))


def _extract_body(detail_html: str) -> str:
    """详情页 HTML → 正文。trafilatura 抽不到就退到剥标签的全文, 再不行返回空串。

    注意 trafilatura **不在 pyproject 的 dependencies 里**(部署环境可能另行手装),
    退化路径不是罕见分支 —— 所以它的剥离质量也要保住, 见 `_strip_script_blocks`。
    """
    if not detail_html:
        return ""
    try:
        import trafilatura
        text = trafilatura.extract(detail_html, include_comments=False) or ""
    except Exception as exc:                       # trafilatura 抽失败不该丢条目
        logger.warning("html_columns: 正文抽取异常: %s", exc)
        text = ""
    if len(text) >= 80:
        return text
    stripped = _clean_text(_strip_script_blocks(detail_html))
    return stripped if len(stripped) > len(text) else text


def is_paywalled(body: str) -> bool:
    return len(body) < PAYWALL_BODY_CHARS or any(m in body for m in PAYWALL_MARKERS)


# ---------------------------------------------------------------- state


def _load_state(path: Path) -> tuple[list[str], dict[str, int]]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], {}
    if not isinstance(data, dict):
        return [], {}
    seen = data.get("seen")
    zero = data.get("zero_match")
    zero_map: dict[str, int] = {}
    if isinstance(zero, dict):
        for key, val in zero.items():
            # 手工编辑/旧版本可能写出合法 JSON 但值不是数字, int() 抛
            # ValueError/TypeError 不在 except (OSError, JSONDecodeError) 内, 而
            # _load_state 在 collect() 的 try 之外 —— 坏 state 会让每轮都崩在同一处。
            # 坏值按 0 计, 走降级而不是整轮崩溃。
            try:
                if isinstance(val, dict):
                    zero_map[str(key)] = int(val.get("zero_match_runs") or 0)
                else:
                    zero_map[str(key)] = int(val or 0)
            except (TypeError, ValueError):
                zero_map[str(key)] = 0
    # 10-05 审计 D14: 返回**存储序**(≈采集先后)的 list, 截断要按它做, 不能再 sorted。
    return ([str(x) for x in seen] if isinstance(seen, list) else []), zero_map


class _OrderedSeen:
    """插入有序的去重集合(借 dict 保序), `collect_column` 里用法与 set 一致。

    10-05 审计 D14: seen 的截断要按「最晚采集」保留 —— set 没有顺序, 只能 sorted 后
    字典序截断, 换域名/协议后最新采集的整批反而排最前被先丢。
    """

    def __init__(self, keys: Iterable[str] = ()) -> None:
        self._items: dict[str, None] = dict.fromkeys(keys)

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def add(self, key: str) -> None:
        self._items.setdefault(key, None)

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)


def _save_state(path: Path, seen: Iterable[str], zero_map: dict[str, int]) -> None:
    payload = {
        # 10-05 审计 D14: 按**插入序(≈采集先后)**保留最近 SEEN_LIMIT 条, 不再按字典序
        "seen": list(seen)[-SEEN_LIMIT:],
        "zero_match": {k: {"zero_match_runs": n} for k, n in zero_map.items()},
    }
    # 10-05 审计 D13: 先写临时文件再原子替换 —— 直接 write_text("w") 先截断后写,
    # 中途被杀留半截 state, 下轮 _load_state 静默清零, 7 天窗内全部重采。
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("html_columns: state 写不进去 %s: %s", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------- adapter


class HtmlColumnsAdapter:
    name = ADAPTER_NAME

    def __init__(
        self,
        *,
        config_path: Path | str | None = None,
        state_path: Path | None = None,
        fetch_list: Callable[[str], str] | None = None,
        fetch_detail: Callable[[str], str] | None = None,
        extract_body: Callable[[str], str] | None = None,
        days: int = DEFAULT_DAYS,
        render_list: Callable[..., str] | None = None,
        render_detail: Callable[..., str] | None = None,
        browsers_path: str | None = None,
        headless: bool = True,
    ) -> None:
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self.state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        self._fetch_list = fetch_list or _default_fetch_list
        self._fetch_detail = fetch_detail or _default_fetch_detail
        self._extract_body = extract_body or _extract_body
        self.days = days
        #: browser 模式的取数函数。测试注入假函数 → **不起浏览器**(构造函数不做任何 IO)。
        self._render_list = render_list
        self._render_detail = render_detail
        self.browsers_path = browsers_path
        self.headless = headless
        #: 浏览器实例在整轮 collect 里共用(开一次很贵), 用 contextmanager 管生命周期。
        self._browser_cm: Any = None
        self._browser: _Browser | None = None

    # -- 浏览器生命周期 -----------------------------------------------------

    def _ensure_browser(self) -> _Browser | None:
        """需要浏览器时开一个并复用。**playwright 不可用返回 None**, 调用方跳过栏目。

        这样处理的原因: 没装 playwright 是常见部署状态, 不该让同一轮里 http 栏目
        一起挂掉(TASK 纪律: browser 栏目记 warning 跳过, 不影响 http 栏目)。
        """
        if self._browser is not None:
            return self._browser
        if self._browser_cm is None:
            from contextlib import ExitStack
            self._browser_cm = ExitStack()
            self._browser = self._browser_cm.enter_context(
                _Browser(self.browsers_path, headless=self.headless))
        return self._browser

    def _probe_browser(self) -> _Browser | None:
        """试着开浏览器; 起不来记一条 warning 并返回 None(而不是抛)。"""
        try:
            return self._ensure_browser()
        except BrowserUnavailable as exc:
            logger.warning("html_columns: 浏览器不可用, browser 栏目本轮跳过: %s", exc)
            self._browser_cm = None
            self._browser = None
            return None

    def _close_browser(self) -> None:
        if self._browser_cm is not None:
            try:
                self._browser_cm.close()
            except Exception as exc:
                logger.info("html_columns: 关浏览器失败(%s)", exc)
        self._browser_cm = None
        self._browser = None

    def _list_html(self, col: Column) -> str:
        """按 `col.render` 分派列表页取数(http / browser)。"""
        if col.render != "browser":
            return self._fetch_list(col.list_url)
        if self._render_list is not None:          # 测试注入: 不起浏览器
            return self._render_list(col.list_url, col=col)
        browser = self._ensure_browser()
        if browser is None:
            return ""
        return browser.render_html(col.list_url, scroll=col.render_scroll)

    def _detail_html(self, col: Column, url: str) -> str:
        """按 `col.render_detail` 分派详情页取数。实测两站详情页 http 就够, 缺省走 http。"""
        if col.render_detail != "browser":
            return self._fetch_detail(url)
        if self._render_detail is not None:
            return self._render_detail(url, col=col)
        browser = self._ensure_browser()
        if browser is None:
            return ""
        return browser.render_html(url, scroll=0)

    # -- 单个栏目 ---------------------------------------------------------

    def collect_column(
        self,
        col: Column,
        *,
        cutoff: datetime,
        seen: set[str],
        limit: int | None = None,
        stats: dict[str, int] | None = None,
    ) -> list[ItemRecord]:
        # playwright 不可用时**只跳过这一个栏目**: 记 warning + 本栏目 0 条,
        # 其余 http 栏目照跑(这是 TASK 要求的降级行为, 不是抛异常让整轮挂掉)。
        # 10-05 审计 E11: 探活门要跟 _list_html/_detail_html 实际会走浏览器的路径一致 ——
        # 只按「两个注入都为 None」判断的话, 只注入了 render_detail(或反之)时门不成立、
        # 不探活, _list_html 里 _ensure_browser() 直接抛 BrowserUnavailable, 降级承诺失效。
        needs_probe = col.needs_browser and (
            (col.render == "browser" and self._render_list is None)
            or (col.render_detail == "browser" and self._render_detail is None)
        )
        if needs_probe and self._probe_browser() is None:
                logger.warning(
                    "html_columns: 栏目 %s 需要浏览器渲染但 playwright 不可用, 本轮跳过"
                    "(其余栏目照采)", col.key)
                if stats is not None:
                    stats["parsed"] = 0
                    stats["browser_unavailable"] = 1
                return []

        list_html = self._list_html(col)
        raws = parse_list(list_html, col)
        if stats is not None:
            # 「匹配到几条」用于改版监控 —— 和「产出几条」是两件事: 页面结构没变但条目
            # 全在 7 天窗外 / 全已见过时产出 0 条, 那不是改版, 不该记 zero_match。
            stats["parsed"] = len(raws)
            # 10-05 审计 D10: 抓回来的是空串 = 网络失败(超时/DNS/5xx/渲染失败),
            # 不是「页面有正文却零匹配」, 不该把排查引向页面结构。
            if not list_html:
                stats["fetch_failed"] = 1
        cap = col.max_per_run if limit is None else min(col.max_per_run, limit)
        # 10-05 审计 E10: cap 要留**时间上最新**的 cap 条, 不是页面前 cap 条 —— 页序非
        # 「最新在前」(时间正序/编辑推荐位混入)时, 按页序截会让最新条目一直挤不进来,
        # 要等旧条目滚出 7 天窗才轮得到。所以先解析完窗口内候选的时间再排序截断。
        # 本轮内去重单独记(同页重复挂出的条目必须当场挡掉); seen 只记**实际产出**的
        # 条目, 落选(超 cap)与丢弃(日期取不到/窗外)的都不进 seen。
        def make_detail_once(url: str) -> Callable[[], str]:
            """每条 URL 一个带缓存的取数闭包。cache 必须绑定在工厂闭包里 —— 循环体内的
            局部变量会被同帧所有闭包共享(闭包捕获变量, 不是值), 第二阶段再调用时全部
            指向最后一轮的 cache, 正文会串条。"""
            fetched: list[str] = []

            def detail_once(_url: str = url) -> str:
                if not fetched:
                    fetched.append(self._detail_html(col, _url))
                return fetched[0]

            return detail_once

        page_seen: set[str] = set()
        resolved: list[tuple[datetime, RawItem, Callable[[], str]]] = []
        for raw in raws:
            if raw.url in seen or raw.url in page_seen:
                continue
            page_seen.add(raw.url)
            # 详情页按需抓一次并缓存: date_source=detail_meta 的日期与正文都出自这份 HTML,
            # 每条只发一个请求(限速 ≥3.2s, 重复抓既慢又没必要)。
            detail_once = make_detail_once(raw.url)

            ts = _resolve_ts(col, raw, detail_html=detail_once)
            if ts is None:
                logger.warning(
                    "html_columns: %s 条目日期取不到, 丢弃 %s | %s",
                    col.key, raw.url, raw.title[:40])
                continue
            if ts < cutoff:
                continue
            resolved.append((ts, raw, detail_once))
        # 稳定排序: 同 ts 条目保持页面原序, 两轮之间的去留才可复现
        resolved.sort(key=lambda entry: entry[0], reverse=True)
        records: list[ItemRecord] = []
        for ts, raw, detail_once in resolved[:cap]:
            detail_html = detail_once(raw.url)
            body = self._extract_body(detail_html)[:BODY_LIMIT]
            # 10-05 审计 D16: 详情页抓取失败(空串)不是付费墙 —— is_paywalled("") 会因
            # 正文短于 400 字误判 True, 把网络故障标成付费墙。失败时明示 fetch_failed。
            paywalled = is_paywalled(body) if detail_html else False
            payload: dict[str, Any] = {"kind": col.kind, "column": col.name,
                                       "paywalled": paywalled}
            if not detail_html:
                payload["detail_fetch_failed"] = True
            records.append(ItemRecord(
                item=Item(
                    id=compute_item_id(ADAPTER_NAME, url=raw.url),
                    source=col.source,
                    url=raw.url,
                    title=raw.title[:512],
                    body=body,
                    author=None,
                    ts=ts,
                    lang=col.lang,
                    tags=["人情味", col.name],
                ),
                adapter_name=ADAPTER_NAME,
                source_payload_json=json.dumps(payload, ensure_ascii=False),
                media_urls=[],
            ))
            seen.add(raw.url)
        return records

    # -- 全部栏目 ---------------------------------------------------------

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable[ItemRecord]:
        cutoff = since.astimezone(timezone.utc) if since else (
            datetime.now(timezone.utc) - timedelta(days=self.days))
        columns = load_columns(self.config_path)
        if not columns:
            logger.info("html_columns: 无栏目配置(%s), 本轮 0 条", self.config_path)
            return []

        seen_list, zero_map = _load_state(self.state_path)
        seen = _OrderedSeen(seen_list)
        records: list[ItemRecord] = []
        first_error: BaseException | None = None
        try:
            for col in columns:
                stats: dict[str, int] = {}
                try:
                    found = self.collect_column(col, cutoff=cutoff, seen=seen,
                                                limit=limit, stats=stats)
                except Exception as exc:
                    # 10-05 审计 E12: 单个栏目的意外异常(如无 host 的 href 让
                    # canonicalize_url 抛 ValueError)只跳过该栏目 —— 否则余下栏目全不采,
                    # 且 _save_state 被跳过, seen/zero_match 计数整轮丢失(改版报警被吃掉)。
                    logger.warning("html_columns: 栏目 %s 采集异常(%s), 跳过该栏目",
                                   col.key, f"{type(exc).__name__}: {exc}"[:160])
                    if first_error is None:
                        first_error = exc
                    continue
                records.extend(found)
                # playwright 不可用 / 网络抓取失败导致的 0 条**都不算改版**, 别去动
                # zero_match 计数
                if stats.get("browser_unavailable") or stats.get("fetch_failed"):
                    continue
                self._note_match(col, stats.get("parsed", 0), zero_map)
        finally:
            # 浏览器开一次很贵, 但整轮结束必须关干净(否则进程退不出)
            self._close_browser()

        _save_state(self.state_path, seen, zero_map)
        if first_error is not None and not records:
            # 整轮**所有栏目**都异常且毫无产出时, 把第一个异常冒出去(状态已落盘、
            # 浏览器已关) —— 调用方需要知道这轮采集整体失败; 只要有产出就按降级处理。
            raise first_error
        records.sort(key=lambda r: r.item.ts, reverse=True)
        return records[:limit] if limit is not None else records

    # -- 改版监控 ---------------------------------------------------------

    @staticmethod
    def _note_match(col: Column, n_parsed: int, zero_map: dict[str, int]) -> None:
        """`n_parsed` 是列表页正则**匹配到的条数**(不是产出的条数)。"""
        if n_parsed > 0:
            if zero_map.get(col.key):
                logger.info("html_columns: %s 恢复正常匹配 %d 条", col.key, n_parsed)
            zero_map[col.key] = 0
            return
        runs = zero_map.get(col.key, 0) + 1
        zero_map[col.key] = runs
        msg = "html_columns: %s 本轮零匹配(页面可能改版) %d/%d 轮: %s"
        if runs >= ZERO_MATCH_ERROR_AT:
            logger.error(msg, col.key, runs, ZERO_MATCH_ERROR_AT, col.list_url)
        else:
            logger.warning(msg, col.key, runs, ZERO_MATCH_ERROR_AT, col.list_url)


_THROTTLE = _Throttle()
