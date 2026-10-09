"""日本外務省 海外安全ホームページ(`mofa_anzen`)—— 危険情報/感染症危険情報/スポット情報/広域情報。

报纸「风险提示栏」四条官方源之一(见 `docs/paper_v2_contract.md` 第 9 节)。这条 lane 的判据是
**能转发出去、能让人行动**,所以只收外务省自己认定的那四类「海外安全情報」,不收領事メール
(使馆日常通知,量大且不构成风险等级)。

## 接口(全部 2026-10-04 实测)

**A. オープンデータ(首选,索引)** `GET https://www.ezairyu.mofa.go.jp/opendata/area/newarrival.xml`
  公式说明页 `https://www.ezairyu.mofa.go.jp/html/opendata/index.html`(首页 →「オープンデータ」)。
  说明页写明「5分毎に更新」「XML形式で公開」「新着/すべての地域/地域別/国別 の6種類」。
  返回 `<opendata dataType="N" mailCount="21" odType="02">`,里面 21 个 `<mail>`:
  `{keyCd, infoType, infoName, leaveDate, area/{cd,name}, country/{cd,name}, title, lead,
    mainText, infoUrl, xmlNormalUrl, koukanName}`。
  ⚠ **`/html/opendata/area/newarrival.xml` 是 2018 年的样本文件**(`lastModified="2018/11/22"`,
  `odType="02"`, 条目 title 带「（サンプル）」)。真数据在**去掉 `/html` 的路径**上。
  实测 21 条**全是 `R10` 領事メール(一般)**——`newarrival` 只是"最新 20 条"的意思,
  不是"最近一段时间的全部",四类目标信息当前在里面**一条都没有**。

**B. RSS(索引补充)** `GET https://www.anzen.mofa.go.jp/rss/news.xml`
  「外務省海外安全ホームページ新着海外安全情報」。RSS 2.0,13 个 `<item>`,每条
  `{title, link, description, category, pubDate}`;`category` 正是
  `危険情報 / 感染症危険情報 / スポット情報 / 広域情報`(实测这 13 条覆盖到这 4 类中的 3 类,
  感染症危険情報最近一条是 2026-05-18 的 `2026T081`)。**它比 newarrival 有用得多**,
  所以两个都拉、按 keyCd 去重合并。

**C. 单条详情(正文)** `GET https://www.ezairyu.mofa.go.jp/opendata/mail/<keyCd>.xml`
  (= 索引里的 `xmlNormalUrl`),返回该条的完整 `lead`/`subText`/`mainText`/`riskLevelN`。
  ⚠ **T 系列(危険情報/感染症危険情報, keyCd 形如 `2026T090`)这份 XML 恒返回
  2023 字节的 HTML「对该 URL 的访问受限」维护页**,C 系列(`2026C045`)同路径正常返回 XML。
  实测重试 4 次、加 UA/Referer/间隔 6s 都是同一个维护页 —— 不是频率限制。
  → T 系列拿不到 XML 就退回抓 `infoUrl` 的详情页 HTML
  (`https://www.anzen.mofa.go.jp/info/pc*.html`,`id="contents"` 容器里有
  「【ポイント】/【本文】」全文;`更新日 <span class="block-leave">2026年09月16日</span>`)。

## infoType → 我们要的四类(实测代码表,来自 00L.xml 全量 4201 条的分布)
| infoType | infoName              | 收? |
|----------|-----------------------|-----|
| `T40`    | 危険                  | ✔ 危険情報 |
| `T81`    | 感染症                | ✔ 感染症危険情報 |
| `C30`    | スポット              | ✔ スポット情報 |
| `C31`    | スポット(感染症)      | ✔ |
| `C50`    | 広域                  | ✔ 広域情報 |
| `C51`    | 広域(感染症)          | ✔ |
| `R10`    | 領事メール(一般)      | ✘ 量大、无风险等级 |
| `R20`    | 領事メール(緊急)      | ✘ |

## 坑
- `riskLevel1..4` / `infectionLevel1..4` 是 **0/1 标志位,不是等级本身**:
  ハイチ `2026T088` 是 `riskLevel4=1` 三个 0;解除了的 (`2026T090`) 四个全 0。
  所以 `level` 用**标题里的 `【…レベル…】` 原文**,不自己从标志位拼等级(拼出来的不是官方原话)。
- `leaveDate` 是日本时间且**常常只有日期没有时刻**(`2026/09/16 00:00:00`),按 JST 落地。
- `newarrival.xml` / `00L.xml` / `area/33.xml` 的 `<country>` **在轻量版(L)里只有 `<cd>` 没有 `<name>`**;
  只有 `dataType="N"` 的完整版带国名。要国名(用来拼 `mofa_anzen:<国名>`)就只能用 N 版。
- `広域情報`(C50/C51)本来就没有 country,按设计规格用 `mofa_anzen:広域`。
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from personal_intel_loop import DATA_DIR
from personal_intel_loop.schemas import Item, compute_item_id

logger = logging.getLogger(__name__)

ISSUER = "外務省"
OPEN_DATA_INDEX = "https://www.ezairyu.mofa.go.jp/opendata/area/newarrival.xml"
OPEN_DATA_DETAIL = "https://www.ezairyu.mofa.go.jp/opendata/mail/{key_cd}.xml"
RSS_URL = "https://www.anzen.mofa.go.jp/rss/news.xml"
DEFAULT_STATE_PATH = DATA_DIR / "cache" / "mofa_anzen_state.json"

REQUEST_TIMEOUT = 20
DEFAULT_DAYS = 14
BODY_LIMIT = 20_000
SEEN_LIMIT = 4000
JST = timezone(timedelta(hours=9))

#: 我们收的四种 infoType(见文件头代码表)。其余(領事メール R10/R20)不收。
TARGET_INFO_TYPES = {"T40", "T81", "C30", "C31", "C50", "C51"}
#: RSS 的 `<category>` 白名单,同一件事的两种写法。
TARGET_CATEGORIES = {"危険情報", "感染症危険情報", "スポット情報", "広域情報"}
#: 広域情報 没有 country,按设计规格用这个名字。
WIDE_AREA = "広域"
#: 10-05 复审 R06: 非 `広域情報` 类别但抽不出国名时的 label —— 明确表示"没抽到",
#: 不复用 `広域`(那是官方 `広域情報` 专用的, 复用会把点级信息混进广域桶)。
UNKNOWN_AREA = "未特定"

_MAIL_RE = re.compile(r"<mail>(.*?)</mail>", re.S)
_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t　]+")
_DIV_RE = re.compile(r"<div\b|</div>", re.I)
#: 10-05 复审 R01: 详情页 `<meta charset>` 是唯一的编码声明 —— `anzen.mofa.go.jp` 的响应头
#: 是 `Content-Type: text/html`(**不带 charset**), requests 于是退回 ISO-8859-1。
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset=["']?\s*([\w-]+)""", re.I)
#: 10-05 复审 R03: HTTP 200 的「找不到页面」维护页。正文非空, 所以 `if not body` 拦不住,
#: 不显式识别就会把「页面不存在」告示当正文入库(实测 mofa_blocked_T090.html 就是这一页)。
_MAINTENANCE_MARKERS = (
    "指定されたページが見つかりません",
    "ページが見つかりません",
    "お探しのページは見つかりませんでした",
    "Page Not Found",
)


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


def _tag(mail_xml: str, tag: str) -> str:
    """取第一个 `<tag>…</tag>` 的纯文本(HTML 实体解码 + 标签剥掉 + 空白归一)。"""
    m = re.search(rf"<{tag}>(.*?)</{tag}>", mail_xml, re.S)
    if not m:
        return ""
    raw = _CDATA_RE.sub(r"\1", m.group(1))
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    raw = _TAG_RE.sub(" ", raw)
    return _WS_RE.sub(" ", html.unescape(raw)).strip()


def _nested_name(mail_xml: str, tag: str) -> str:
    """`<area><cd>33</cd><name>中南米</name></area>` → `中南米`(空则返回 "")。"""
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", mail_xml, re.S)
    return _tag(m.group(1), "name") if m else ""


def _parse_leave_date(raw: str) -> datetime | None:
    """`2026/09/16 00:00:00`(日本时间,常常只有日期)。"""
    raw = raw.strip()
    if not raw:
        return None
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=JST).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _parse_rfc822(raw: str) -> datetime | None:
    """RFC 822 `pubDate` → UTC。

    10-05 复审 R05: RFC 822 允许省略时区偏移, `parsedate_to_datetime` 这时返回 **naive**
    datetime, 直接 `.astimezone(utc)` 会按**宿主本地时区**解释它 —— 同一条 RSS 在
    `TZ=UTC` 与 `TZ=Asia/Tokyo` 下算出相差 9 小时的 ts(实测 18:53:21 → 18:53Z / 09:53Z)。
    缺时区时按**站点所在地时区(JST)** 解释, 不依赖宿主 TZ。
    """
    try:
        dt = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(timezone.utc)


def _level_from_title(title: str) -> str | None:
    """官方等级取标题里 `【…】` 的**原文**(例 `危険レベル解除` / `感染症危険情報（レベル１）`)。"""
    for bracket in re.findall(r"【([^】]*)】", title):
        if "レベル" in bracket:
            return bracket.strip()
    return None


def _country_from_title(title: str) -> str:
    """RSS 路线没有 `<country>` 字段,只能从官方标题的固定句式取国名(取不到就返回 "")。

    只认两种实测出现过的句式,不做通用分词:
    「<国名>の危険情報【…】」「<国名>の感染症危険情報（レベル…）」「<国名>における…」。
    広域情報(中東情勢…)不含这些句式 → 返回 "" → 落到 `広域`,不会硬猜。
    """
    m = re.match(r"^(.{2,20}?)(?:の(?:感染症)?危険情報|における)", title)
    return m.group(1).strip() if m else ""


def _balanced_div(html_text: str, anchor: int) -> str:
    """`<div id="contents">` 的内容。数括号取嵌套 div 的结尾。

    10-05 复审 R04: 原正则要求 `id="contents"` 之后紧跟 `</div>` 且其后是
    `<div class="rightBox"` / `<!--//leftBox-->` / 字符串结尾。真实页面(实测 `2026T090`)
    这三个终止条件都不满足 → `seg` 为 None → 兜底成「整页剥标签」, 正文里混进全站导航
    与版权(实测 2744 字符的真正文膨胀成 7587, `【ポイント】` 之后还跟着面包屑/版权/
    联系方式)。数括号对三种详情页模板都成立, 且与 `cn_consular._balanced_div` 同一套做法。
    """
    depth = 0
    for m in _DIV_RE.finditer(html_text, anchor):
        depth += 1 if m.group(0).lower() == "<div" else -1
        if depth == 0:
            open_end = html_text.index(">", anchor) + 1
            return html_text[open_end:m.start()]
    return ""


def _strip_page(html_text: str) -> str:
    """详情页 HTML → 正文纯文本。取 `id="contents"` 容器; 维护页返回 ""。

    10-05 复审 R03: HTTP 200 的「找不到页面」维护页正文非空(`外務省 / 指定されたページが
    見つかりません。/ …`), `if not body` 守卫拦不住, 会把告示当正文入库。这里显式识别。
    10-05 复审 R04: 用数括号取 `id="contents"` 容器, 不再整页剥标签。
    """
    if any(marker in html_text for marker in _MAINTENANCE_MARKERS):
        # 维护页(`<div id="contents">` 里只有一句「页面不存在」) —— 不当正文
        return ""

    anchor = html_text.find('<div id="contents"')
    if anchor < 0:
        anchor = html_text.find('id="contents"')
    body = _balanced_div(html_text, anchor) if anchor >= 0 else html_text
    body = re.sub(r"<script.*?</script>", " ", body, flags=re.S)
    body = re.sub(r"<style.*?</style>", " ", body, flags=re.S)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
    body = re.sub(r"</p>|</li>|</h\d>", "\n", body, flags=re.I)
    body = _TAG_RE.sub(" ", body)
    body = html.unescape(body)
    lines = [_WS_RE.sub(" ", ln).strip() for ln in body.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    if any(marker in text for marker in _MAINTENANCE_MARKERS):
        # 剥完标签才看出是维护页(标记被 `<div>` 拆开的情况)
        return ""
    return text


# ---- 网络(全部可注入)-------------------------------------------------------

def _decode(raw: bytes, header_charset: str | None = None) -> str:
    """按 `<meta charset>` → 响应头 charset → UTF-8 的顺序解码, 不用 `apparent_encoding` 盲猜。

    10-05 复审 R01: 详情页响应头是 `Content-Type: text/html`(**不带 charset**), requests
    对无 charset 的 `text/*` 一律退回 `ISO-8859-1`, **不会去看 HTML 里的 `<meta charset>`**
    (实测 `anzen.mofa.go.jp/info/pchazardspecificinfo_2026T090.html`)。于是走详情页路线的
    正文全是乱码(`外務省` → `æµ·å¤\x96å®\x89å`), `【ポイント】`/`【本文】` 全部匹配不上,
    且乱码非空所以 `if not body` 守卫不生效 → **静默写入脏条目**。
    同型缺陷见 `enso_status._get` 的 `apparent_encoding`(R15): 那是统计猜测, 会覆盖
    已经正确的响应头 charset(NOAA 头给 utf-8, `apparent_encoding` 猜 `Windows-1252`)。
    """
    declared = _META_CHARSET_RE.search(raw[:2048])
    encodings: list[str] = []
    if declared:
        encodings.append(declared.group(1).decode("ascii", "ignore"))
    # latin-1 **不是声明**: requests 对不带 charset 的 `text/*` 一律把 `r.encoding` 设成
    # ISO-8859-1, 那是"没找到声明"的兜底而非站点声明。它又**永远解不报错**, 留在候选里
    # 会把后面真正的 UTF-8 挤掉, 所以显式剔掉。
    if header_charset and header_charset.lower().replace("_", "-") not in {
            "iso-8859-1", "latin-1", "latin1"}:
        encodings.append(header_charset)
    encodings.append("utf-8")
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _get(url: str) -> str | None:
    """网络错/非 200 返回 None。一个源拉不到不该让整轮 ingest 挂掉。"""
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    except requests.RequestException as exc:
        logger.warning("mofa_anzen fetch failed %s: %s", url, exc)
        return None
    if r.status_code != 200:
        logger.warning("mofa_anzen non-200 %s: %s", url, r.status_code)
        return None
    # 10-05 复审 R01: 不用 `r.text`(无 charset 的 text/* 会被 requests 按 ISO-8859-1 解)
    return _decode(r.content, r.encoding)


def _fetch_open_data_index() -> str:
    return _get(OPEN_DATA_INDEX) or ""


def _fetch_rss() -> str:
    return _get(RSS_URL) or ""


def _fetch_detail(key_cd: str) -> str:
    return _get(OPEN_DATA_DETAIL.format(key_cd=key_cd)) or ""


def _fetch_page(url: str) -> str:
    return _get(url) or ""


# ---- 解析 ------------------------------------------------------------------

def parse_open_data(xml_text: str) -> list[dict[str, Any]]:
    """`newarrival.xml`(dataType="N")→ 四类目标条目的 dict。结构变了返回 []。"""
    out: list[dict[str, Any]] = []
    for raw in _MAIL_RE.findall(xml_text or ""):
        info_type = _tag(raw, "infoType")
        if info_type not in TARGET_INFO_TYPES:
            continue
        key_cd = _tag(raw, "keyCd")
        title = _tag(raw, "title")
        if not (key_cd and title):
            continue
        body = "\n".join(x for x in (_tag(raw, "lead"), _tag(raw, "subText"),
                                      _tag(raw, "mainText")) if x)
        out.append({
            "key_cd": key_cd,
            "info_type": info_type,
            "title": title,
            "body": body,
            "country": _nested_name(raw, "country"),
            "area": _nested_name(raw, "area"),
            "url": _tag(raw, "infoUrl"),
            "ts": _parse_leave_date(_tag(raw, "leaveDate")),
        })
    return out


def parse_rss(xml_text: str) -> list[dict[str, Any]]:
    """`rss/news.xml` → 四类目标条目。RSS 里没有国名,靠 keyCd 回查rdf: 略,交给详情补。"""
    out: list[dict[str, Any]] = []
    for raw in _ITEM_RE.findall(xml_text or ""):
        category = _tag(raw, "category")
        if category not in TARGET_CATEGORIES:
            continue
        link = _tag(raw, "link")
        title = _tag(raw, "title")
        m = re.search(r"_(\d{4}[A-Z]\d{3})\.html$", link)
        if not (link and title and m):
            continue
        description = _tag(raw, "description")
        out.append({
            "key_cd": m.group(1),
            "info_type": "",
            "category": category,
            "title": title,
            # RSS 的 <description> 逐字重复 <title>(实测 13/13 条), 拿它当正文等于没正文。
            # 这里直接丢掉, 让 collect 去抓详情 —— 否则会产出一条 body 就是标题的空条目。
            "body": "" if description == title else description,
            "country": "",
            "area": "",
            "url": link,
            "ts": _parse_rfc822(_tag(raw, "pubDate")),
        })
    return out


# ---- state -----------------------------------------------------------------

def _load_state(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    seen = data.get("seen") if isinstance(data, dict) else None
    return {str(x) for x in seen} if isinstance(seen, list) else set()


def _save_state(path: Path, seen: set[str]) -> None:
    # 临时文件+原子替换(同 thepaper_warm E19 / html_columns D13) ——
    # 直写 write_text("w") 先截断后写, 中途被杀留半截 state, 下轮 _load_state
    # 按坏 JSON 退化成空 seen, 窗口内全部 key_cd 重新走详情页抓取。
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"seen": sorted(seen)[-SEEN_LIMIT:]}, ensure_ascii=False), "utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("mofa_anzen state 写不进去 %s: %s", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


class MofaAnzenAdapter:
    name = "mofa_anzen"

    def __init__(
        self,
        *,
        fetch_open_data: Callable[[], str] | None = None,
        fetch_rss: Callable[[], str] | None = None,
        fetch_detail: Callable[[str], str] | None = None,
        fetch_page: Callable[[str], str] | None = None,
        state_path: Path | None = None,
        days: int = DEFAULT_DAYS,
    ) -> None:
        self._fetch_open_data = fetch_open_data or _fetch_open_data_index
        self._fetch_rss = fetch_rss or _fetch_rss
        self._fetch_detail = fetch_detail or _fetch_detail
        self._fetch_page = fetch_page or _fetch_page
        self.state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        self.days = days

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable[ItemRecord]:
        # 10-05 复审 R02: naive `since` 按站点所在地时区(JST)解释, 不依赖宿主 TZ。
        # (`cli.py` 的 `--since 2026-09-01` 就是 naive, 直接 `.astimezone(utc)` 会按宿主时区算)
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=JST)
        cutoff = since.astimezone(timezone.utc) if since else (
            datetime.now(timezone.utc) - timedelta(days=self.days))
        seen = _load_state(self.state_path)

        entries = {e["key_cd"]: e for e in parse_open_data(self._fetch_open_data())}
        for entry in parse_rss(self._fetch_rss()):
            entries.setdefault(entry["key_cd"], entry)

        records: list[ItemRecord] = []
        produced_keys: list[str] = []      # 与 records 同序, 供截断后决定谁进 seen(R02)
        for key_cd, entry in entries.items():
            url = entry["url"]
            if not url or key_cd in seen:
                continue
            ts = entry["ts"]
            if ts is None or ts < cutoff:
                continue

            country = entry["country"]
            if not country:
                # RSS 来的条目没有国名字段;T 系列的单条 XML 又拿不到(见文件头),
                # 只能从官方标题的固定句式里取(「<国名>の危険情報」「<国名>における…」)。
                country = _country_from_title(entry["title"])
            body = entry["body"]
            if not country or not body:
                # RSS 条目的 description 逐字重复 title(= 没有正文), 国名也没有,
                # 所以这两样都得去单条 XML 补;T 系列补不到就抓详情页 HTML。
                detail = self._fetch_detail(key_cd)
                if "<opendata" in detail:
                    for parsed in parse_open_data(detail):
                        if parsed["key_cd"] != key_cd:
                            continue
                        country = country or parsed["country"]
                        body = body or parsed["body"]
                        break
                if not body and url.startswith("http"):
                    body = _strip_page(self._fetch_page(url))
            if not body:
                logger.info("mofa_anzen %s 拿不到正文,跳过", key_cd)
                continue

            # 広域情報(C50/C51)按设计规格落 `mofa_anzen:広域`。注意它**是有 <country> 的**
            # (实测 2026C042「中東情勢」列了 アラブ首長国連邦/イエメン/… 一串国家),
            # 所以不能靠"有没有国名"判断, 只能靠 infoType / RSS category。
            wide = (entry["info_type"] in {"C50", "C51"}
                    or entry.get("category") in {"広域情報"})
            if wide:
                regions = [WIDE_AREA]
                label = WIDE_AREA
            elif country:
                regions = [country]
                label = country
            else:
                # 10-05 复审 R06: 非 `広域情報` 类别(例如 `スポット情報`)但猜不出国名时,
                # **不能**回落成 `広域` —— 那把点级信息混进广域桶, 与上面注释自身的原则矛盾。
                # 抽不出就老实留空 regions, label 用 `未特定` 明确表示"没抽到", 不硬猜。
                regions = []
                label = UNKNOWN_AREA
            source = f"{self.name}:{label}"
            records.append(ItemRecord(
                item=Item(
                    id=compute_item_id(source, url=url),
                    source=source,
                    url=url,
                    title=entry["title"][:512],
                    body=body[:BODY_LIMIT],
                    author=ISSUER,
                    ts=ts,
                    lang="ja",
                    tags=["风险提示", label],
                ),
                adapter_name=self.name,
                source_payload_json=json.dumps(
                    {"kind": "risk", "issuer": ISSUER,
                     "published": ts.astimezone(JST).isoformat(),
                     "regions": regions,
                     "level": _level_from_title(entry["title"]),
                     "key_cd": key_cd, "info_type": entry["info_type"],
                     "area": entry["area"]},
                    ensure_ascii=False),
                media_urls=[],
            ))
            # 10-05 复审 R02: 只把**实际返回**的条目记进 seen。原来的写法对每条产出都
            # `seen.add(key_cd)` 且 state 在 `records[:limit]` 截断**之前**落盘, 于是被
            # limit 截掉的那几条 key_cd 已进 state, 下一轮 `if key_cd in seen: continue`
            # 直接跳过 → 永久丢失, 且没有任何日志。入口是 `cli.py` 的 `--limit` /
            # `--limit-per-adapter`(原样透传到 collect(limit=...))。
            produced_keys.append(key_cd)

        # 下面的 sort 会重排 records, 但 produced_keys 不跟着动 —— 原先
        # zip(produced_keys, records) 按位置配对, limit 截断后把「被丢弃条目」的
        # key_cd 记进 seen、漏掉「实际返回条目」的, 被丢的永久丢失(下轮被 seen 跳过)。
        # sort 前先按 id(record) 绑定 key, 返回哪些就记哪些(R02 的复发形式)。
        key_by_id = {id(r): k for k, r in zip(produced_keys, records)}
        records.sort(key=lambda r: r.item.ts, reverse=True)
        selected = records[:limit] if limit is not None else records
        if limit is not None and len(records) > len(selected):
            logger.info("mofa_anzen 本轮产出 %d 条, limit=%d 只返回 %d 条; "
                        "落选的 %d 条不进 seen, 下一轮继续抓",
                        len(records), limit, len(selected), len(records) - len(selected))
        seen.update(key_by_id[id(r)] for r in selected)
        _save_state(self.state_path, seen)
        return selected
