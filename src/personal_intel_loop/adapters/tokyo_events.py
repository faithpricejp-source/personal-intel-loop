"""东京展览/演出采集器 (`tokyo_events`) —— 报纸「闲与美」栏的展演源。

契约见 `docs/paper_v2_contract.md` 10.3(leisure 栏)。与新闻类 adapter 的根本差别:

- **展演没有发布时间**。`Item.ts` 一律取「抓到它的时刻」(本轮抓取时间), 不假装是官方发布日。
- **url 不保证唯一**。见下面「url 会被 canonicalize_url 剥掉 fragment」一节。
- **去重键 = venue + 展演名 + start_date**(设计规格第二节第 3 条), 同一展演只入一次。

## 5 个源(2026-10-05 实测, 原始字节存 `project/tests/fixtures/tokyo_events/`)

| venue_key | 站点 | 接入 | 实测条数 |
|---|---|---|---|
| `ntj` | 国立劇場/新国立劇場(日本芸術文化振興会) | `json` | 87 |
| `tnm` | 東京国立博物館 | `rss` | 3 |
| `t_bunka` | 東京文化会館 | `ssr_html` | 28 |
| `nact` | 国立新美術館 | `ssr_html` | 9 |
| `eiseibunko` | 永青文庫 | `ssr_html` | 4 |

逐站实测结构与坑:

### 1. `ntj` 国立劇場 —— `GET /search/api?when=&theater=&genre=&keyword=`
返回 `{"count": 87, "rows": [...]}`, 字段 `title / theatre_name / genre / year /
start_date / end_date / url`。**注意 `start_date`/`end_date` 是「10月1日」这种不含年的月日**,
但同行有 `year` 字段(实测 87 行全是 `"2026"`), 所以年份优先用 `year`, 缺了才走推断。
`theatre_name` 实测四值: 国立能楽堂 / 国立文楽劇場 / 国立文楽劇場（小ホール）/ **他劇場**
——「他劇場」是本振兴会承办但场地在别处的演出, 不是数据错误, 原样保留。
`url` 是站内相对路径 `schedule/kokuritsu_l/2026/0810/`, 要拼 `https://www.ntj.jac.go.jp/`。

### 2. `tnm` 東京国立博物館 —— RSS 2.0
`?controller=ctg_feed&cid=1&lang=ja`。**素 UA 直接 403**, 必须带浏览器头 +
`Accept-Language: ja`。会期不在结构化字段里, 在 `summary` **末尾**:
`...本館 A室<br />2026年9月29日（火）～2026年12月20日（日）`。
坑: summary 正文里还有「1期：9月29日（火）～10月25日（日）」这种**不带年份**的分期日程,
所以取日期必须用「带年份」的正则, 否则会抽到 1 期而不是会期。
`title` 字段里 `<br />` 是**未转义的裸标签**(feedparser 也不管), 要自己剥。
RSS 只含当前特别展(历史在 `controller=past_list`); `cid=6/7` 是特集/特別企画。

### 3. `t_bunka` 東京文化会館 —— SSR HTML table
`/stage/` 是 `<table id="result">` 里一串 `<tr data-is_last_more=...>`。
- 日期被拆成三个 span: `<span class="yea">2026年</span>` + `<span class="day">10月9日</span>`
  + `<span class="week">fri</span>`。**不能直接搜「2026年10月9日」连续串**, 必须按 class 取再拼。
- 实测 28 行里**只有 20 行带 `<th class="date_row>`**; 同一天多场时用
  `rowspan="2|3"`, 后续行**没有日期单元格**, 必须**继承上一行的日期**(实测 8 行靠继承)。
  照抄「每行都要有日期」的写法会静默丢掉 8 场。
- `<h2><span>` 是演目名; `<li class="gen">` 里多个 `<span>` 是genre(要滤掉 `主催事業` 这种
  非类型标签); `alt="会場"` 那个 `<li>` 是**实际场地**, 常常不是东京文化会馆本身
  (实测「まちなかコンサート」在国立西洋美術館大厅/东博平成馆/东京都美术馆轮流办),
  后面还跟着 `※会場は東京文化会館ではございません。` 的提示与 `（アクセス）` 尾巴, 要清掉。
- 同一个「まちなかコンサート」一周内重复多条(每场场地/时间不同) —— 去重键含 start_date,
  正好一场一条, 不要按标题去重。

### 4. `nact` 国立新美術館 —— SSR HTML, `<time datetime>`(全部调查对象里最规整的日期标记)
`/exhibition_and_event/index.html`, 按 `<h2 class="ttl2">` 切成三段:
`企画展` / `公募展` / `イベント`。每段里 `<li><a href=...>` 是一条。
- `ca_cur=開催中` / `ca_upc=開催予定` 区分当前与即将开幕 —— **「開催予定」正是日报最想要的
  先行信息**, 所以两类都收, 靠窗口过滤而不是靠状态标记。
- `イベント` 段的 `<p class="ex_date">` 与 `<p class="ex_date2">` 会给出**两组** `<time>`
  (实测 006622 是 10-30～11-16 两处重复; 006605 的 ex_date 只有单日 10-04 而 ex_date2 是
  08-20～09-13 另一个区间)。**只取第一个 `ex_date` 里的 time**, 否则会把别的区间当会期。
- `公募展` 段的 href 是页内锚点 `#006414`(详情是同页 `<div id="006414" class="public_item">`)。

### 5. `eiseibunko` 永青文庫 —— SSR HTML, 老式 table
`http://www.eiseibunko.com/exhibition.html`。页面上半是年度スケジュール表(一年到头, 含 2027 年),
下半是四个 `<a name="2026haru|natsu|aki|fuyu">` 锚点 + 各自的详情块。
- 会期在 `会期：` 后面, 且**被 `<br>` + 一堆全角空格(　)隔断**
  (`会期：2027年1月16日（土）<br>　　　　　〜4月11日（日）`), 抽之前必须先 `<br>`→空格、
  压缩全角空格, 否则一条都抽不到(实测)。
- 结束日**不带年份**, 要从起始日继承; `2027年1月16日～4月11日` 这种会落到下一年, 跨年要小心。
- 已结束的展在块里有 `終了いたしました` 标记, 但**不要用它过滤** —— 会期算出来一样会被窗口滤掉,
  官方这个标记还会漏(实测「開催中」一次都没出现)。
- 秋季展标题带 `●` 前缀且是与其他馆的共同企画, 与页首スケジュール表的写法不一致, 以详情块为准。

## 坑(接入前必读)

1. **`url` 会被 `canonicalize_url` 剥掉 fragment**(`Item.url` 的 validator 就是它)。
   `nact` 的公募展 `#006414`、多展共用的 `index.html`, 以及 `eiseibunko` 的
   `exhibition.html#2026fuyu`, 归一化后**全都是同一个 url**。所以:
   - 本 adapter **不**用 `compute_item_id(url=...)`(会全部撞成同一个 id, 三场公募展只剩一场),
     改成按设计规格规定的去重键 `venue + title + start_date` 算 id(`tokyo_events:<sha1>`,
     形状与 `compute_item_id` 一致), 一场一个 id;
   - 入库的 `url` 会丢掉锚点, 详情定位靠 id 而不是 url。**这是既有 schema 行为, 未改动。**
2. **别用 `Accept-Encoding: gzip, deflate, br`**。带 `br` 时 requests 把正文解成乱码
   (日文标题全成 `æ£®ç¾®è¡¨`)。默认钉 `gzip, deflate`。
3. **别信响应的 `Content-Type`  charset**。`eiseibunko` 头里写 `ISO-8859-1`, 但 meta 与正文
   都是 utf-8 —— requests 的 `r.text` 会按 latin-1 解出乱码。所以本 adapter 一律
   **收 bytes, 自己按 meta charset 解码**(见 `_decode`), `fetch` 返回值是 `bytes`。
4. 剥 `<script>` / `<style>` / `<!-- -->` 再抽: 森美術館那类过期链接藏在注释里;
   WP 站 `<style>` 有几万字主题变量。本 adapter 走的 3 个 HTML 源实测不需要,
   但 `_html_to_text` 统一做了, 防后续同类改动。
5. 每站请求间隔 ≥3 秒(设计规格冻结范围), 默认 fetcher 自带 sleep, 可注入替换掉。
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urljoin

import feedparser
import requests

from personal_intel_loop import DATA_DIR, LOCAL_TZ
from personal_intel_loop.schemas import Item

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = DATA_DIR / "cache" / "tokyo_events_state.json"

ADAPTER_NAME = "tokyo_events"
REQUEST_TIMEOUT = 30
MIN_INTERVAL_S = 3.0
#: 只收「尚未结束」的; `WINDOW_DAYS` 只限制未来开幕的上界, 已经开着的长期展没有
#: 开始日下界(还能去的展都该收)。10-05 审计 D23: 原注释写「60 天内开始」与代码不符。
WINDOW_DAYS = 60
SEEN_LIMIT = 4000
BODY_LIMIT = 8_000
#: 会期最长跨度(天)。展演不会开一整年, 用来挡「结束日早于起始日 → 硬推到次年」这种错误。
MAX_SPAN_DAYS = 366
#: 跨年兜底的跨度上限。真正的跨年会期都很短(实测最长的早春展是 1/16~4/11, 但那是同年),
#: 「12月20日～1月5日」这种 16 天才是跨年该有的形状。跨度比这还大的就不是跨年,
#: 是数据本身错了(结束日 12月1日 早于起始日 12月20日) -> 返回 None, 不硬编。
WRAP_MAX_SPAN_DAYS = 120
ZERO_ESCALATE = 3

_TAGS = ["闲与美", "东京展演"]

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_BLOCK_END_RE = re.compile(r"</(p|li|div|tr|h\d|dd|dt|td|th|br)\s*/?>", re.I)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t　]+")
_NL_RE = re.compile(r"\n{2,}")

#: 「2026年9月29日」「2026年9月 9日」「2026年09月29日」——**必须带年份**
_FULL_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
#: 会期里的分隔符实测有这些
_RANGE_SEP_RE = re.compile(r"[～〜~]|－|–|—|―")
#: 说明性文字, 抽日期前先切掉(实测 eiseibunko 有「会期は変更となる場合がございます」)。
#: 10-05 审计 D22: 原模式里的「問い合わせleo」「 Industrias」是别的项目复制来的异物,
#: 日文页面上不可能出现 —— 「問い合わせ」(问询处)永远切不掉, 行尾杂讯进会期。
_NOTE_CUT_RE = re.compile(r"(?:会期は変更|※|主催|協力|問い合わせ)")


@dataclass(frozen=True)
class Source:
    venue_key: str
    venue: str
    kind: str          # json | rss | ssr_html
    url: str
    author: str
    base_url: str = ""  # 相对链接的拼接基准


SOURCES: tuple[Source, ...] = (
    Source(
        venue_key="ntj",
        venue="国立劇場／新国立劇場",
        kind="json",
        url="https://www.ntj.jac.go.jp/search/api?when=&theater=&genre=&keyword=",
        author="独立行政法人日本芸術文化振興会",
        base_url="https://www.ntj.jac.go.jp/",
    ),
    Source(
        venue_key="tnm",
        venue="東京国立博物館",
        kind="rss",
        url=("https://www.tnm.jp/modules/r_exhibition/index.php"
             "?controller=ctg_feed&cid=1&lang=ja"),
        author="東京国立博物館",
    ),
    Source(
        venue_key="t_bunka",
        venue="東京文化会館",
        kind="ssr_html",
        url="https://www.t-bunka.jp/stage/",
        author="公益財団法人 東京文化会館",
    ),
    Source(
        venue_key="nact",
        venue="国立新美術館",
        kind="ssr_html",
        url="https://www.nact.jp/exhibition_and_event/index.html",
        author="国立新美術館",
        base_url="https://www.nact.jp/exhibition_and_event/index.html",
    ),
    Source(
        venue_key="eiseibunko",
        venue="永青文庫",
        kind="ssr_html",
        url="http://www.eiseibunko.com/exhibition.html",
        author="公益財団法人 永青文庫",
        base_url="http://www.eiseibunko.com/exhibition.html",
    ),
)

SOURCES_BY_KEY = {s.venue_key: s for s in SOURCES}


# ---- 编码 / 文本 ------------------------------------------------------------

def _decode(raw: bytes | str | None, declared: str | None = None) -> str:
    """bytes → str。**不信响应头里的 charset**(实测 eiseibunko 头写 ISO-8859-1、正文是 utf-8)。

    顺序: 文档内 meta charset → utf-8 → cp932/shift_jis → latin-1(不抛)。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if not raw:
        return ""
    candidates: list[str] = []
    meta = re.search(rb'charset=["\']?\s*([\w-]+)', raw[:4096], re.I)
    if meta:
        try:
            candidates.append(meta.group(1).decode("ascii").lower())
        except UnicodeDecodeError:
            pass
    candidates += ["utf-8", "cp932"]
    if declared:
        candidates.append(declared.lower())
    for enc in candidates:
        if enc in {"iso-8859-1", "latin-1", "latin1"}:
            continue  # 从不作为首选: 它不会报错, 会静默解出乱码
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", errors="replace")


def _html_to_text(raw: str | None) -> str:
    """HTML 片段 → 纯文本。`<br>` 换行(展演的日期/会期经常是被 `<br>` 切断的)。"""
    if not raw:
        return ""
    text = _COMMENT_RE.sub(" ", raw)
    text = _SCRIPT_RE.sub(" ", text)
    text = _BR_RE.sub("\n", text)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.replace(" ", " ")
    lines = [_WS_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    return _NL_RE.sub("\n", "\n".join(ln for ln in lines if ln))


def _one_line(raw: str) -> str:
    """压成单行。展名里常有 `<br>`(实测 eiseibunko 秋季展标题跨两行), `Item.title` 要单行。"""
    return _WS_RE.sub(" ", _html_to_text(raw).replace("\n", " ")).strip()


# ---- 日期 ------------------------------------------------------------------

def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_full_date(text: str) -> date | None:
    """「2026年9月29日」/「2026年9月 9日」这种**带年份**的日期 → `date`。"""
    m = _FULL_DATE_RE.search(text or "")
    if not m:
        return None
    return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def infer_span(start_md: tuple[int, int], end_md: tuple[int, int], today: date,
               year_hint: int | None = None) -> tuple[date, date] | None:
    """只有月日时的年份推断(设计规格第二节第 2 条: 按「会期结束日不早于今天」定年份)。

    跨年: 结束日**不比今天早** → `end` 落在最近的那个年末/年初; 若起始月日晚于结束月日
    (如 12月20日～1月5日), 则 `start` 往前推一年。
    """
    sm, sd = start_md
    em, ed = end_md
    if year_hint:
        start = _safe_date(year_hint, sm, sd)
        end = _safe_date(year_hint, em, ed)
        if not start or not end:
            return None
        if end < start:            # 跨年: 结束日落到次年
            end = _safe_date(year_hint + 1, em, ed)
            if not end:
                return None
        return (start, end) if end >= start else None

    # 10-05 审计 D18: 「该年不存在的日期」(如非闰年的 2/29)不等于会期不在该年 ——
    # 起始/结束要**成对**落在同一段会期里, 有一边在该年不存在就整体顺延试下一年
    # (可能是闰年), 上限 4 年。
    end_year = today.year
    while end_year <= today.year + 4:
        end = _safe_date(end_year, em, ed)
        if end is not None and end >= today:
            start_year = end_year
            if (sm, sd) > (em, ed):     # 起始月日晚于结束月日 → 起始在上一年(跨年)
                start_year -= 1
            start = _safe_date(start_year, sm, sd)
            if start is not None and end >= start and (end - start).days <= MAX_SPAN_DAYS:
                return start, end
            # start 落在该年不存在的日期(如 2027 的 2/29)→ end_year += 1 再试
        end_year += 1
    return None


def _month_day(text: str) -> tuple[int, int] | None:
    """「10月1日」→ (10, 1)。"""
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", text or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


# ---- 逐源解析 --------------------------------------------------------------
# 统一返回 dict: title / url / venue / genre / start_date / end_date / summary

def _entry(*, title: str, url: str, venue: str, genre: str,
           start: date | None, end: date | None, summary: str = "") -> dict[str, Any]:
    return {
        "title": title.strip(),
        "url": url.strip(),
        "venue": venue.strip(),
        "genre": genre.strip(),
        "start_date": _iso(start),
        "end_date": _iso(end) or _iso(start),
        "summary": summary.strip(),
    }


def parse_ntj_api(raw: bytes | str, today: date) -> list[dict[str, Any]]:
    """国立劇場 JSON API → 展演。日期缺年份, 优先用同行的 `year` 字段。"""
    text = _decode(raw)
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        logger.warning("tokyo_events[ntj] JSON 解析失败: %s", exc)
        return []
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        logger.warning("tokyo_events[ntj] 返回结构不是 {rows:[...]}")
        return []
    source = SOURCES_BY_KEY["ntj"]
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _one_line(str(row.get("title") or ""))
        start_md = _month_day(str(row.get("start_date") or ""))
        end_md = _month_day(str(row.get("end_date") or ""))
        if not (title and start_md):
            continue
        span = infer_span(start_md, end_md or start_md, today,
                          _int_or_none(row.get("year")))
        if not span:
            continue
        theatre = str(row.get("theatre_name") or "").strip()
        genre = str(row.get("genre") or "").strip()
        summary_bits = [
            f"{'主催' if row.get('is_self_organized') else ''}"
            f"{'／他会場' if str(row.get('is_other_theatre') or '') == '1' else ''}"
        ]
        out.append(_entry(
            title=title,
            url=urljoin(source.base_url, str(row.get("url") or "")),
            venue=f"{source.venue} {theatre}".strip(),
            genre=genre or source.venue,
            start=span[0], end=span[1],
            summary=" / ".join(b for b in summary_bits if b.strip("／")),
        ))
    return out


def parse_tnm_feed(raw: bytes | str, today: date) -> list[dict[str, Any]]:
    """東京国立博物館 RSS → 展演。会期在 summary 末尾, 必须用带年份的正则取。"""
    source = SOURCES_BY_KEY["tnm"]
    parsed = feedparser.parse(_decode(raw).encode("utf-8"))
    out: list[dict[str, Any]] = []
    for entry in parsed.entries:
        title = _one_line(str(entry.get("title") or ""))
        link = str(entry.get("link") or "").strip()
        if not (title and link):
            continue
        body = _html_to_text(str(entry.get("summary") or ""))
        found = _tnm_span(body, today)
        if not found:
            logger.debug("tokyo_events[tnm] 抽不到会期, 跳过: %s", title)
            continue
        span, at = found
        out.append(_entry(
            title=title,
            url=link,
            # 会期同一行前面那一小段就是展厅(实测「本館 A室」「平成館 特別展示室」)
            venue=f"{source.venue} {_tnm_room(body, at)}".strip(),
            genre="特別展",
            start=span[0], end=span[1],
            summary=body,
        ))
    return out


def _tnm_span(body: str, today: date) -> tuple[tuple[date, date], int] | None:
    """从 summary 里取会期, 顺带返回**起始日在正文里的位置**(用来切展厅名)。

    正文里有「1期：9月29日（火）～10月25日（日）」这种**不带年份**的分期日程, 所以只认
    「两边都带年份」的写法, 并且取**最后一处**(实测会期在末尾, 分期日程在中间)。
    """
    matches = list(_FULL_DATE_RE.finditer(body))
    for i in range(len(matches) - 1, 0, -1):
        start = _safe_date(*(int(g) for g in matches[i - 1].groups()))
        end = _safe_date(*(int(g) for g in matches[i].groups()))
        if start and end and end >= start:
            return (start, end), matches[i - 1].start()
    if len(matches) == 1:      # 只有起始日 → 单日展
        only = _safe_date(*(int(g) for g in matches[0].groups()))
        return ((only, only), matches[0].start()) if only else None
    return None


def _tnm_room(body: str, at: int) -> str:
    """起始日**同一行**在它前面的那段文字 = 展厅名。"""
    line = body[:at].splitlines()[-1] if body[:at].splitlines() else body[:at]
    return _WS_RE.sub(" ", line).strip("「『(（ 【 ")


def parse_tbunka_html(raw: bytes | str, today: date) -> list[dict[str, Any]]:
    """東京文化会館 SSR table → 展演。单日粒度; rowspan 续行**继承上一行日期**。"""
    text = _decode(raw)
    idx = text.find('<table id="result">')
    if idx < 0:
        logger.warning("tokyo_events[t_bunka] 页面里没有 <table id=\"result\">")
        return []
    body = text[idx:]
    source = SOURCES_BY_KEY["t_bunka"]
    out: list[dict[str, Any]] = []
    current: date | None = None
    for row in re.findall(r"<tr data-is_last_more.*?</tr>", body, re.S):
        head = re.search(r'class="date_row[^"]*"', row)
        if head:
            yea = re.search(r'class="yea">([^<]*)<', row)
            day = re.search(r'class="day">([^<]*)<', row)
            # 10-05 审计 D19: 日期行解析不出来(改版)必须先作废 current —— 不然这行和
            # 它后面的 rowspan 续行会静默继承上一行的旧日期, 场次被安到错误的日期上。
            current = None
            if not (yea and day):
                logger.warning("tokyo_events[t_bunka] 日期行缺 yea/day span(页面可能改版), 跳过该行")
                continue
            joined = f"{_html_to_text(yea.group(1))}{_html_to_text(day.group(1))}"
            current = parse_full_date(joined)
            if current is None:
                logger.debug("tokyo_events[t_bunka] 日期行解析失败: %r", joined)
        if current is None:      # 第一行就续行 = 页面结构变了, 别硬猜
            continue
        link = re.search(r'href="(/stage/\d+/)"', row)
        name = re.search(r"<h2>\s*(?:<span>)?(.*?)(?:</span>)?\s*</h2>", row, re.S)
        if not (link and name):
            continue
        title = _one_line(name.group(1))
        if not title:
            continue
        genres = [g for g in re.findall(r'<li class="gen">.*?</li>', row, re.S)
                  for g in re.findall(r"<span>(.*?)</span>", g, re.S)]
        genres = [_html_to_text(g) for g in genres]
        genre = "・".join(g for g in genres if g and g != "主催事業") or "主催事業"
        venue = _t_bunka_venue(row) or source.venue
        times = _t_bunka_times(row)
        out.append(_entry(
            title=title,
            url=urljoin(source.url, link.group(1)),
            venue=venue,
            genre=genre,
            start=current, end=current,
            summary=times,
        ))
    return out


def _t_bunka_venue(row: str) -> str:
    """`alt="会場"` 那个 `<li>` 的文字, 清掉「※…」提示与「（アクセス）」尾巴。"""
    cell = re.search(r'alt="会場">(.*?)</li>', row, re.S)
    if not cell:
        return ""
    text = re.sub(r'<span class="otherout[^"]*">.*?</span>', " ", cell.group(1), flags=re.S)
    text = _html_to_text(text).replace("\n", " ")
    text = re.split(r"※|（アクセス）|\(アクセス\)", text)[0]
    return _WS_RE.sub(" ", text).strip()


def _t_bunka_times(row: str) -> str:
    """开演时间(纯文本, 进 body 给人看)。"""
    cell = re.search(r'alt="日程">(.*?)</li>', row, re.S)
    if not cell:
        return ""
    text = _html_to_text(cell.group(1)).replace("\n", " ")
    return _WS_RE.sub(" ", text).strip()


def parse_nact_html(raw: bytes | str, today: date) -> list[dict[str, Any]]:
    """国立新美術館 SSR HTML → 展演/活动。按 `<h2 class="ttl2">` 分三段, 日期取 `<time datetime>`。"""
    text = _decode(raw)
    start = text.find("<h1>")
    end = text.find("</main>")
    if start < 0 or end < 0:
        logger.warning("tokyo_events[nact] 页面结构不对(找不到 main 区)")
        return []
    main = text[start:end]
    source = SOURCES_BY_KEY["nact"]
    out: list[dict[str, Any]] = []
    parts = re.split(r'<h2 class="ttl2"[^>]*>(.*?)</h2>', main)
    for i in range(1, len(parts), 2):
        section = _html_to_text(parts[i]).strip()
        for href, chunk in re.findall(r'<li>\s*<a href="([^"]+)"(.*?)</a>\s*</li>',
                                      parts[i + 1], re.S):
            title = _one_line(" ".join(re.findall(r"<h2>(.*?)</h2>", chunk, re.S)))
            if not title:
                continue
            # 只认第一个 ex_date —— 活动段还有 ex_date2, 那是另一个区间
            first = re.search(r'<p class="ex_date">(.*?)</p>', chunk, re.S)
            times = re.findall(r'<time datetime="([^"]+)"', first.group(1) if first else "")
            if not times:
                continue
            span = _times_span(times, today)
            if not span:
                continue
            out.append(_entry(
                title=title,
                url=urljoin(source.base_url, href),
                venue=source.venue,
                genre=section or "展覧会",
                start=span[0], end=span[1],
                summary=_nact_status(chunk),
            ))
    return out


def _times_span(times: list[str], today: date) -> tuple[date, date] | None:
    """`<time datetime="YYYY-MM-DD">` 列表 → 会期。只有一个就是单日。

    10-05 审计 D20: `<time datetime>` 带时刻(`2026-10-30T09:00`, HTML 常见写法)时取
    日期部分 —— `date.fromisoformat` 拒收带时刻的串, 整条 return None 会让该馆条目成批消失。
    """
    parsed = []
    for value in times[:2]:
        text = value.strip().split("T", 1)[0].split(" ", 1)[0]
        try:
            parsed.append(date.fromisoformat(text))
        except ValueError:
            return None
    if not parsed:
        return None
    start = parsed[0]
    end = parsed[1] if len(parsed) > 1 else start
    return (start, end) if end >= start else None


def _nact_status(chunk: str) -> str:
    """`ca_cur=開催中` / `ca_upc=開催予定` / `ca_eve=...` 这些官方标签, 原样留作状态。"""
    labels = [_html_to_text(v) for _, v in
              re.findall(r'<li class="(ca_\w+)">\s*([^<]*?)\s*</li>', chunk)]
    return " / ".join(x for x in labels if x)


def parse_eiseibunko_html(raw: bytes | str, today: date) -> list[dict[str, Any]]:
    """永青文庫 SSR HTML → 展演。会期被 `<br>` + 全角空格隔断, 结束日不带年份。"""
    text = _decode(raw)
    source = SOURCES_BY_KEY["eiseibunko"]
    anchors = [(m.group(1), m.start())
               for m in re.finditer(r'<a name="(\d{4}\w+)"', text)]
    if not anchors:
        logger.warning("tokyo_events[eiseibunko] 页面里没有 <a name=...> 锚点")
        return []
    bounds = anchors + [("", len(text))]
    out: list[dict[str, Any]] = []
    for (anchor, pos), (_, nxt) in zip(bounds, bounds[1:]):
        block = text[pos:nxt]
        season = re.search(r"<b class=\"wh14pxB\">(■[^<]*)</b>", block)
        name = re.search(r"<strong>(.*?)</strong>", block, re.S)
        kai = re.search(r"会期：([^\n]*(?:\n[^\n]*?)?日)", block)
        if not (name and kai):
            continue
        span = _eisei_span(_html_to_text(kai.group(1)), today)
        if not span:
            logger.debug("tokyo_events[eiseibunko] 会期解析失败: %r", kai.group(1))
            continue
        title = _one_line(name.group(1))
        prefix = _html_to_text(season.group(1)).lstrip("■") if season else ""
        if prefix and not title.startswith(prefix):
            title = f"{prefix} {title}".strip()
        out.append(_entry(
            title=title,
            url=urljoin(source.base_url, f"exhibition.html#{anchor}"),
            venue=source.venue,
            genre="展覧会",
            start=span[0], end=span[1],
            summary=_eisei_summary(block),
        ))
    return out


def _eisei_span(kai_text: str, today: date) -> tuple[date, date] | None:
    """`会期：2027年1月16日（土）\n 〜4月11日（日）` → 会期。

    结束日不带年份 → 从起始日继承; 起始日晚于结束日(12月～1月)则结束日落次年。
    """
    cleaned = _NOTE_CUT_RE.split(kai_text)[0]
    cleaned = _WS_RE.sub(" ", cleaned.replace("\n", " ")).strip()
    start = parse_full_date(cleaned)
    if not start:
        return None
    tail = _RANGE_SEP_RE.split(cleaned, maxsplit=1)
    end = parse_full_date(tail[1]) if len(tail) > 1 else None
    if end is None:
        end_md = _month_day(tail[1]) if len(tail) > 1 else None
        if end_md:
            guess = _safe_date(start.year, *end_md)
            if guess and guess < start:
                # 结束日早于起始日 -> 跨年, 落到次年; 但跨度超过 `WRAP_MAX_SPAN_DAYS`
                # 就说明这行数据本身不可信(实测不会出现), 老实返回 None 交给窗口滤掉。
                guess = _safe_date(start.year + 1, *end_md)
                if guess and (guess - start).days > WRAP_MAX_SPAN_DAYS:
                    return None
            end = guess
    if end is None:
        end = start
    return (start, end) if end >= start else None


def _eisei_summary(block: str) -> str:
    """详情块里会期之后那段 `<p align="left">` 就是简介。"""
    after = block.split("会期：", 1)[-1]
    paras = re.findall(r"<p align=\"left\"[^>]*>(.*?)</p>", after, re.S)
    for para in paras:
        text = _html_to_text(para)
        if len(text) > 30:
            return text
    return ""


PARSERS: dict[str, Callable[[bytes | str, date], list[dict[str, Any]]]] = {
    "json": parse_ntj_api,
    "rss": parse_tnm_feed,
    "ssr_html": parse_tbunka_html,   # 会被逐源覆盖, 见 PARSE_BY_KEY
}

PARSE_BY_KEY: dict[str, Callable[[bytes | str, date], list[dict[str, Any]]]] = {
    "ntj": parse_ntj_api,
    "tnm": parse_tnm_feed,
    "t_bunka": parse_tbunka_html,
    "nact": parse_nact_html,
    "eiseibunko": parse_eiseibunko_html,
}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# ---- 网络(可注入) ----------------------------------------------------------

def _build_headers() -> dict[str, str]:
    """浏览器头。`Accept-Language: ja` 尤其重要(东博素 UA 直接 403);
    `Accept-Encoding` 去掉 `br` —— 带 br 时 requests 会把正文解成乱码(见模块 docstring 坑 #2)。"""
    return {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept-Language": "ja,ja-JP;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }


def make_fetcher(*, interval_s: float = MIN_INTERVAL_S,
                 sleep: Callable[[float], None] = time.sleep) -> Callable[[Source], bytes]:
    """默认 fetcher: 逐源 GET, 间隔 ≥`interval_s`, **返回原始 bytes**(不 decoding, 见 `_decode`)。"""

    def fetch(source: Source) -> bytes:
        try:
            resp = requests.get(source.url, headers=_build_headers(), timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("tokyo_events[%s] 请求失败: %s", source.venue_key, exc)
            return b""
        if resp.status_code != 200:
            logger.warning("tokyo_events[%s] non-200: %s", source.venue_key, resp.status_code)
            return b""
        return resp.content

    def fetch_spaced(source: Source) -> bytes:
        if interval_s > 0:
            sleep(interval_s)
        return fetch(source)

    return fetch_spaced


# ---- state -----------------------------------------------------------------
# seen: 去重键; zero_streak: 每源连续零匹配轮数(连续 3 轮升 error; 抓取失败的轮次不计, I-3)

def _load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"seen": [], "zero_streak": {}}
    if not isinstance(data, dict):
        return {"seen": [], "zero_streak": {}}
    seen = data.get("seen")
    streak = data.get("zero_streak")
    return {
        "seen": [str(x) for x in seen] if isinstance(seen, list) else [],
        "zero_streak": ({str(k): int(v) for k, v in streak.items() if isinstance(v, int)}
                        if isinstance(streak, dict) else {}),
    }


def _save_state(path: Path, seen: list[str], streak: dict[str, int]) -> None:
    payload = {
        # 10-05 审计 D21: seen 按插入序(≈采集先后)去重截断 —— sorted 的字典序截断会先丢
        # venue/标题码点靠前的键, 其中可能仍有 60 天窗口内的展, 丢了就重采。
        "seen": list(dict.fromkeys(seen))[-SEEN_LIMIT:],
        "zero_streak": streak,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    # 10-05 审计 D21(同 html_columns D13): 临时文件+原子替换 —— write_text("w") 先截断
    # 后写, 中途被杀留半截 state, 下轮 _load_state 静默清零重采。
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("tokyo_events state 写不进去 %s: %s", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def dedup_key(venue: str, title: str, start_date: str | None) -> str:
    """去重键 = venue + 展演名 + start_date(设计规格第二节第 3 条)。"""
    norm = lambda s: re.sub(r"\s+", " ", _WS_RE.sub(" ", str(s or ""))).strip()
    return "|".join((norm(venue), norm(title), str(start_date or "")))


def compute_event_id(key: str) -> str:
    """按去重键算 item id。

    **不用 `compute_item_id(url=...)`**: `Item.url` 的 validator 会剥掉 fragment, 于是
    nact 的三场公募展(都是 `index.html#00641x`)和 eiseibunko 的四场展(都是
    `exhibition.html#2026xxx`)会算出**同一个 id**, 互相覆盖。id 形状与 `compute_item_id`
    一致(`<prefix>:<sha1>`)。
    """
    return f"{ADAPTER_NAME}:{hashlib.sha1(key.encode('utf-8')).hexdigest()}"


class TokyoEventsAdapter:
    name = ADAPTER_NAME

    def __init__(
        self,
        *,
        fetch: Callable[[Source], bytes] | None = None,
        state_path: Path | None = None,
        window_days: int = WINDOW_DAYS,
        today: date | None = None,
        clock: Callable[[], datetime] | None = None,
        sources: tuple[Source, ...] = SOURCES,
    ) -> None:
        self._fetch = fetch or make_fetcher()
        self.state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        self.window_days = window_days
        self._today = today
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.sources = sources

    def today(self) -> date:
        # 10-05 审计 D17: 「今天」按本地(东京)日期 —— JST 0:00~8:59 之间 UTC 还是昨天,
        # 按 UTC 取会把昨天刚结束的展当「尚未结束」、今天开幕的当明天, 上午 9 点后才自愈。
        return self._today or self._clock().astimezone(LOCAL_TZ).date()

    def in_window(self, start: date | None, end: date | None, today: date) -> bool:
        """只收「尚未结束」的; `window_days` 只限制「未来开始」的上界。

        开始日**没有下界**: 数月前就开始、还没结束的长期展照收(还能去的展都该收)。
        10-05 审计 D23: 原 docstring 写「60 天内开始」与代码不符, 已按代码语义改正。
        起止日期**都要有**: 缺起始日就没法确认窗口关系, 宁可丢掉。
        """
        if not (start and end):
            return False
        if end < today:                       # 已结束
            return False
        return start <= today + timedelta(days=self.window_days)

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable["ItemRecord"]:
        today = self.today()
        cutoff = since.astimezone(timezone.utc) if since else None
        state = _load_state(self.state_path)
        seen: list[str] = state["seen"]
        streak: dict[str, int] = state["zero_streak"]

        records: list[ItemRecord] = []
        for source in self.sources:
            parser = PARSE_BY_KEY.get(source.venue_key)
            if parser is None:
                logger.error("tokyo_events: 没有 %s 的解析函数", source.venue_key)
                continue
            raw_bytes = self._fetch(source)
            # 同 html_columns: fetcher 抓取失败
            # (RequestException / non-200)返回空串。那是网络问题不是页面改版, 不计入零条连击,
            # 也不清零已有计数; 只有抓取成功(非空)但解析出 0 条才计入 zero_streak。
            if not raw_bytes:
                logger.warning("tokyo_events[%s] 抓取失败(返回空), 本轮不计入零条连击",
                               source.venue_key)
                continue
            try:
                entries = parser(raw_bytes, today)
            except Exception:                    # 单源解析炸了不能拖垮其他源
                logger.exception("tokyo_events[%s] 解析异常", source.venue_key)
                entries = []
            fresh = [e for e in entries if e.get("start_date") and e.get("url")]
            if not fresh:
                streak[source.venue_key] = streak.get(source.venue_key, 0) + 1
                count = streak[source.venue_key]
                msg = ("tokyo_events[%s] 一条都没解析到(连续第 %d 轮)"
                       % (source.venue_key, count))
                if count >= ZERO_ESCALATE:
                    logger.error(msg)
                else:
                    logger.warning(msg)
                continue
            streak[source.venue_key] = 0

            for entry in fresh:
                start = date.fromisoformat(entry["start_date"]) if entry["start_date"] else None
                end = date.fromisoformat(entry["end_date"]) if entry["end_date"] else None
                if not self.in_window(start, end, today):
                    continue
                key = dedup_key(entry["venue"], entry["title"], entry["start_date"])
                if key in seen:
                    continue
                ts = self._clock().astimezone(timezone.utc)
                if cutoff and ts < cutoff:
                    continue
                # 单条坏数据(如上游某行给出无 host 的 url, canonicalize_url
                # 抛 ValueError)只跳过该条 —— Item 构造原先在逐源 try 之外, 一条炸掉
                # 整轮, 且 _save_state 执行不到, seen/streak 停在旧值, 之后每轮在同一
                # 行再崩, adapter 永久停摆。
                try:
                    records.append(ItemRecord(
                        item=Item(
                            id=compute_event_id(key),
                            source=f"{ADAPTER_NAME}:{source.venue_key}",
                            url=entry["url"],
                            title=entry["title"][:512],
                            body=self._build_body(entry)[:BODY_LIMIT],
                            author=source.author,
                            ts=ts,               # 展演没有发布时间 → 抓到它的时刻
                            lang="ja",
                            tags=_TAGS + ([entry["genre"]] if entry["genre"] else []),
                        ),
                        adapter_name=self.name,
                        source_payload_json=json.dumps({
                            "kind": "leisure",
                            "venue": entry["venue"],
                            "start_date": entry["start_date"],
                            "end_date": entry["end_date"],
                            "genre": entry["genre"],
                        }, ensure_ascii=False),
                        media_urls=[],
                    ))
                except Exception:
                    logger.exception("tokyo_events[%s] 条目构造异常, 跳过该条: %s",
                                     source.venue_key, str(entry.get("title"))[:80])
                    continue
                seen.append(key)

        _save_state(self.state_path, seen, streak)
        records.sort(key=lambda r: (r.item.ts, r.item.title))
        return records[:limit] if limit is not None else records

    @staticmethod
    def _build_body(entry: dict[str, Any]) -> str:
        """body = 会期 / 场馆 / 类型 / 简介, 纯文本。"""
        venue = entry["venue"]
        head = (
            f"会期：{entry['start_date']}～{entry['end_date']}\n"
            f"会場：{venue}\n"
            f"タイプ：{entry['genre']}"
        )
        return f"{head}\n\n{entry['summary']}".strip() if entry.get("summary") else head


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]
