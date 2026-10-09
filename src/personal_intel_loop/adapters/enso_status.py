"""ENSO(エルニーニョ/ラニーニャ)官方状态 (`enso_status:noaa` / `enso_status:jma`)。

报纸「风险提示栏」里的大尺度气候状态项(契约第 9 节:「气候与季节性灾害(含 ENSO 等大尺度气候
状态对当地的影响,须引用官方机构当期说法」)。两个机构各一条,**只在发布日期变了时**产出一条 ——
月更通报不该天天进日报。

## 接口(2026-10-04 实测)

**NOAA CPC** `GET https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/enso_advisory/ensodisc.shtml`
(目录页 `.../enso_advisory/` 里 `ensodisc.shtml` 是 HTML 正文,另有 `.pdf` / `.doc`)。
- 发布日期: 正文靠前的一行纯文本 `10 September 2026`(**英文月名,无时区**)。
  ⚠ 正文里还有一句 `The next ENSO Diagnostic Discussion is scheduled for 8 October 2026.`
  —— 那是**下次**发布时间, 不能当本期日期。所以只取**第一个**匹配, 且排除前面有
  `scheduled for` 的那个。
- 警报状态: `ENSO Alert System Status: <a …><font …><span style="color:red">El Niño Advisory
  </span>` → 实测值 `El Niño Advisory`。原样取 span 里的文字。
- 摘要: `<u>Synopsis:</u>` 之后加粗那段(`El Niño is strengthening, with a greater than 90%
  chance of a very strong event during the Northern Hemisphere fall and winter 2026-27.`)。
- 正文: 从 `ENSO Alert System Status` 那张表之后到 "This discussion is a consolidated effort"
  之前的全部段落。

**気象庁** `GET https://www.data.jma.go.jp/cpd/elnino/kanshi_joho/kanshi_joho1.html`
(栏目入口 `https://www.data.jma.go.jp/cpd/elnino/` → prints 了一遍「過去のエルニーニョ監視速報」)
- 页内就有本期号与日期: `<h1>エルニーニョ監視速報（No.408）<span class="subTitle stx">
  2026年8月の実況と2026年9月〜2027年3月の見通し</span><div class="s publisherInf">
  気象庁　大気海洋部<br />令和8年9月9日</div>`。**令和8年 = 2026**, 要换算。
- 状态一句话: `<div class="outline" id="summary"><ul><li>2026年春からエルニーニョ現象が
  続いているとみられる。</li><li>今後、冬にかけてエルニーニョ現象が続く見込み（100％）。</li>`
  第 1 条=实况, 第 2 条= outlook。`level` 取第 2 条(官方现状判断的原文)。
- 历史列表 `https://www.data.jma.go.jp/cpd/elnino/houdou/houdou.html` 里有
  `2026年(令和8年)9月9日 エルニーニョ監視速報 No.408` 与各期 PDF, 可交叉核对期号与日期
  (实测一致)。**正文只从 `kanshi_joho1.html` 取, 不用 PDF**(PDF 要解析二进制)。

## 为什么 `days` 缺省不是 14
两个源都是**月更**, 14 天窗口会把上个月发布的当期通报直接丢掉(实测 NOAA 10 Sep 2026 /
気象庁 9月9日, 到 10-05 已过去 25/26 天)。所以这个 adapter 的 `days` 缺省 **400** ——
时间窗在这里的作用是"别把陈年通报当成当期", 不是"只要近两周的"。这一条偏离设计规格第二节的
共同要求, 已写进交付文档「没把握」。

## 坑
- 気象庁日期是**令和年**, 直接 `datetime.strptime("%Y年%m月%d日")` 会解不出来(令和8年 ≠ 8年)。
- 期号(`No.408` / 正文里的 `EL NIÑO/SOUTHERN OSCILLATION (ENSO) DIAGNOSTIC DISCUSSION`)
  用来做 title; 两边格式不同, 各自一套正则。
- `title` 里的「状态一句话」用官方原文, 不翻译、不改写; 年-月取**发布日**的年月(不是通报覆盖的
  实况月 —— 通报 9 月发布讲的是 8 月实况, 但"当期"这个时间戳是发布日)。
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
from pathlib import Path
from typing import Callable, Iterable

import requests

from personal_intel_loop import DATA_DIR
from personal_intel_loop.schemas import Item, compute_item_id

logger = logging.getLogger(__name__)

ISSUER_NOAA = "NOAA Climate Prediction Center"
ISSUER_JMA = "気象庁 大気海洋部"
NOAA_URL = "https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/enso_advisory/ensodisc.shtml"
JMA_URL = "https://www.data.jma.go.jp/cpd/elnino/kanshi_joho/kanshi_joho1.html"
DEFAULT_STATE_PATH = DATA_DIR / "cache" / "enso_status_state.json"

REQUEST_TIMEOUT = 20
#: 月更通报, 窗口按"别拿陈年通报当当期"设, 见文件头"为什么 days 缺省不是 14"。
DEFAULT_DAYS = 400
BODY_LIMIT = 20_000
JST = timezone(timedelta(hours=9))

_MONTHS = {m: i + 1 for i, m in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"))}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t　]+")
#: 10-05 复审 R15: `<meta charset>` 是站点自己的声明; `apparent_encoding` 只是 chardet 的
#: 统计猜测。实测 NOAA 响应头已正确给 `charset=utf-8`, 却被 `apparent_encoding` 猜成
#: `Windows-1252`覆盖掉(纯 ASCII 才侥幸无害); JMA 头无 charset, 猜 utf-8 蒙对。
#: 两个源目前都没出事, 但都是运气 —— 一旦页面加入非 ASCII 原文就产出乱码条目,
#: 而 `parse_noaa`/`parse_jma` 的正则都是 ASCII 锚点, 乱码后仍可能匹配成功, 不触发告警。
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset=["']?\s*([\w-]+)""", re.I)

# ---- NOAA ------------------------------------------------------------------

#: 发布日期行。只取第一个, 且**前面一段窗口内**不许有 "scheduled for"(那是下期日期)。
#:
#: 10-05 复审 R16: 原来是固定宽度 lookbehind `(?<!scheduled for )`, 只在 "scheduled for "
#: 与日期**紧邻**时生效。HTML 源码里两者之间常有换行或标签(实测 `scheduled for\n8 October
#: 2026`、`scheduled for <b>8 October 2026</b>` 两种都漏) → lookbehind 失效, 下期日期被
#: 当本期发布日期采信 → `published` 变未来日期, `stamp` 随之变未来值, `last["noaa"]` 被写成
#: 未来日期, 当期条目 `ts` 落在未来(库里出现 future item)。这里改成「先剥标签, 再看前
#: 40 字符是否以 scheduled for 结尾」。
_NOAA_DATE_RE = re.compile(
    r"(?<!\w)(\d{1,2})\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(20\d{2})(?!\w)")
_NOAA_SCHEDULED = "scheduled for"
_NOAA_DATE_STRIP_TAGS = re.compile(r"<[^>]+>")
_NOAA_DATE_LOOKBACK = 40
_NOAA_STATUS_RE = re.compile(
    r"ENSO Alert System Status:(.*?)</strong>", re.S)
_NOAA_SYNOPSIS_RE = re.compile(r"Synopsis:.*?</u>(.*?)</font>", re.S)
_NOAA_CONSOLIDATED = "This discussion is a consolidated effort"


# ---- 気象庁 ----------------------------------------------------------------

_JMA_TITLE_RE = re.compile(r"エルニーニョ監視速報（(No\.\d+)）")
_JMA_DATE_RE = re.compile(r"令和(\d+)年(\d{1,2})月(\d{1,2})日")
_JMA_SUBTITLE_RE = re.compile(r'class="subTitle[^"]*">(.*?)</span>', re.S)
_JMA_SUMMARY_RE = re.compile(r'<div class="outline"[^>]*>(.*?)</div>', re.S)
_JMA_LI_RE = re.compile(r"<li>(.*?)</li>", re.S)


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


def _text(raw: str) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", raw))).strip()


def _html_to_lines(raw: str) -> str:
    text = re.sub(r"<(script|style).*?</\1>", " ", raw or "", flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|li|div|tr|h\d|font)>", "\n", text, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return "\n".join(_WS_RE.sub(" ", ln).strip() for ln in text.splitlines() if ln.strip())


def _noaa_published(raw: str) -> datetime | None:
    """抽本期发布日期, 跳过「下次发布时间」那句。

    10-05 复审 R16: 前 40 字符窗口内(剥掉 HTML 标签、归一空白后)以 `scheduled for`
    结尾的那个日期是**下期**日期, 不能采信。原固定宽度 lookbehind 挡不住换行/标签分隔。
    """
    # 剥标签 + 归一空白, 让「scheduled for」与日期之间隔着换行/标签的情形也能匹配上
    flat = re.sub(r"\s+", " ", _NOAA_DATE_STRIP_TAGS.sub(" ", raw or ""))
    for m in _NOAA_DATE_RE.finditer(flat):
        prefix = flat[max(0, m.start() - _NOAA_DATE_LOOKBACK):m.start()].rstrip()
        if prefix.lower().endswith(_NOAA_SCHEDULED):
            continue
        return datetime(int(m.group(3)), _MONTHS[m.group(2)],
                        int(m.group(1)), tzinfo=timezone.utc)
    return None


def parse_noaa(page_html: str) -> dict[str, object]:
    """NOAA ENSO Diagnostic Discussion 页 → `{published, status, synopsis, body, number}`。

    任一关键字段抽不到就返回 `{}`(调用方记 warning 返回空, 不抛)。
    """
    raw = page_html or ""
    published = _noaa_published(raw)
    status_match = _NOAA_STATUS_RE.search(raw)
    if published is None or status_match is None:
        return {}

    status = _text(status_match.group(1))
    if not status:
        return {}

    # 正文: 从状态表之后到 "This discussion is a consolidated effort" 之前
    tail = raw[status_match.end():]
    cut = tail.find(_NOAA_CONSOLIDATED)
    body_src = tail[:cut] if cut > 0 else tail
    body = _html_to_lines(body_src)
    synopsis = ""
    syn = _NOAA_SYNOPSIS_RE.search(body_src)
    if syn:
        synopsis = _text(syn.group(1))
    if not body:
        return {}
    return {
        "published": published,
        "status": status,
        "synopsis": synopsis,
        "body": body,
        "number": "ENSO Diagnostic Discussion",
        "url": NOAA_URL,
    }


def parse_jma(page_html: str) -> dict[str, object]:
    """気象庁 エルニーニョ監視速報 页 → `{published, status, synopsis, body, number}`。"""
    raw = page_html or ""
    number_match = _JMA_TITLE_RE.search(raw)
    date_match = _JMA_DATE_RE.search(raw)
    if not (number_match and date_match):
        return {}
    # 令和年: 令和1年=2019, 所以 +2018
    published = datetime(int(date_match.group(1)) + 2018, int(date_match.group(2)),
                         int(date_match.group(3)), tzinfo=timezone.utc)

    summary = _JMA_SUMMARY_RE.search(raw)
    bullets = [_text(x) for x in _JMA_LI_RE.findall(summary.group(1))] if summary else []
    bullets = [b for b in bullets if b]
    if not bullets:
        return {}

    # 正文: 从「解説」那一节往后, 到概率预测表结束
    body_src = raw[summary.end():] if summary else raw
    cut = body_src.find("主文におけるエルニーニョ／ラニーニャ現象の発生確率")
    if cut > 0:
        body_src = body_src[:cut]
    body = _html_to_lines(body_src)
    if not body:
        return {}
    subtitle = _JMA_SUBTITLE_RE.search(raw)
    return {
        "published": published,
        "status": bullets[1] if len(bullets) > 1 else bullets[0],
        "synopsis": bullets[0],
        "body": body,
        "number": number_match.group(1),
        "subtitle": _text(subtitle.group(1)) if subtitle else "",
    }


# ---- 网络(可注入)----------------------------------------------------------

#: 单字节编码(latin/windows-125x 系)永远解不报错, 所以不能靠"试一遍看会不会抛"来决定
#: 用不用 —— 遇上一页真 UTF-8 的内容, 它们会静默产出乱码。若字节本身是含非 ASCII 的合法
#: UTF-8, 就别用这些编码。
_SINGLE_BYTE_ENCODINGS = {
    "iso-8859-1", "iso-8859-2", "iso-8859-15", "latin-1", "latin1",
    "windows-1250", "windows-1251", "windows-1252", "windows-1253", "windows-1254",
    "windows-1255", "windows-1256", "windows-1257", "windows-1258", "cp1252",
}


def _is_mojibake_risk(encoding: str, raw: bytes) -> bool:
    """单字节编码碰上"其实是 UTF-8"的字节时, 会静默产出乱码而不是报错。

    实测 NOAA 页面就处在这种矛盾里: 响应头 `charset=utf-8`, 而页面 `meta charset` 写
    `windows-1252` —— 两者本身对不上, 而字节其实是 UTF-8。所以不能机械地"meta 优先":
    若字节是**含非 ASCII 的合法 UTF-8**, 而候选编码是单字节系, 就别用那个单字节编码。
    """
    name = encoding.lower().replace("_", "-")
    if name not in _SINGLE_BYTE_ENCODINGS:
        return False
    if not any(b > 0x7F for b in raw):
        return False
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _decode(raw: bytes, header_charset: str | None = None) -> str:
    """按 `<meta charset>` → 响应头 charset → UTF-8 解码, 但不盲信单字节编码。

    10-05 复审 R15: 原来是 `r.encoding = r.apparent_encoding or "utf-8"` —— **无条件覆盖**,
    连响应头已经给对 charset 的情况也覆盖。`apparent_encoding` 是 chardet 的统计猜测, 不是
    页面声明: 实测 NOAA 头是 `text/html; charset=utf-8`(requests 已正确设好), 却被猜成
    `Windows-1252`; JMA 头无 charset(requests 退成 ISO-8859-1), 猜 `utf-8` 蒙对了。
    两个源目前都没出事, 但都是运气 —— NOAA 页面恰好纯 ASCII(`ñ` 写成 `Ni&ntilde;o` 实体,
    实测非 ASCII 字节数 = 0), 一旦加入非 ASCII 原文就产出乱码条目; 而
    `parse_noaa`/`parse_jma` 的正则都是 ASCII 锚点, 乱码后仍可能匹配成功, 不触发告警。

    顺序按设计规格定的 `<meta charset>` → 响应头 charset → UTF-8, 但两处调整:
    1) `ISO-8859-1` 是 requests 对无 charset 的 `text/*` 的兜底**不是声明**, 剔除;
    2) 单字节编码遇"其实是 UTF-8"的字节会静默出乱码(见 `_is_mojibake_risk`), 跳过 ——
       这条正是 NOAA 头(meta windows-1252 vs 头 utf-8)矛盾时唯一能解对的路径。
    """
    declared = _META_CHARSET_RE.search(raw[:2048])
    encodings: list[str] = []
    if declared:
        encodings.append(declared.group(1).decode("ascii", "ignore"))
    if header_charset:
        encodings.append(header_charset)
    encodings.append("utf-8")
    for encoding in encodings:
        if encoding.lower().replace("_", "-") in {"iso-8859-1", "latin-1", "latin1"}:
            continue      # requests 的"没找到声明"兜底, 不是站点声明
        if _is_mojibake_risk(encoding, raw):
            continue      # 单字节编码会把真 UTF-8 静默解成乱码
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _get(url: str) -> str:
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    except requests.RequestException as exc:
        logger.warning("enso_status fetch failed %s: %s", url, exc)
        return ""
    if r.status_code != 200:
        logger.warning("enso_status non-200 %s: %s", url, r.status_code)
        return ""
    # 10-05 复审 R15: 不用 `apparent_encoding`(统计猜测会覆盖正确的响应头 charset)
    return _decode(r.content, r.encoding)


# ---- state(记上次的发布日期)-------------------------------------------------

def _load_state(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    seen = data.get("last_published") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in seen.items()} if isinstance(seen, dict) else {}


def _save_state(path: Path, last: dict[str, str]) -> None:
    # 临时文件+原子替换(同 thepaper_warm E19 / html_columns D13) ——
    # 直写 write_text("w") 先截断后写, 中途被杀留半截 state, 下轮 _load_state
    # 按坏 JSON 清空 last_published, 两个机构当期通报都被重新产出。
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"last_published": last}, ensure_ascii=False), "utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("enso_status state 写不进去 %s: %s", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


class EnsoStatusAdapter:
    name = "enso_status"

    def __init__(
        self,
        *,
        fetch_noaa: Callable[[], str] | None = None,
        fetch_jma: Callable[[], str] | None = None,
        state_path: Path | None = None,
        days: int = DEFAULT_DAYS,
    ) -> None:
        self._fetch_noaa = fetch_noaa or (lambda: _get(NOAA_URL))
        self._fetch_jma = fetch_jma or (lambda: _get(JMA_URL))
        self.state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        self.days = days

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable[ItemRecord]:
        # 10-05 复审 R09 同型: naive `since` 按站点所在地时区解释, 不依赖宿主 TZ。
        # NOAA 是美东时间(JMA 是 JST), 但两个源都只给日期没有时刻, 按 JST/美东任一解释
        # 都只差几小时; 这里统一按 UTC 落地日期, 保证不随宿主 TZ 变。
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        cutoff = since.astimezone(timezone.utc) if since else (
            datetime.now(timezone.utc) - timedelta(days=self.days))
        last = _load_state(self.state_path)

        records: list[ItemRecord] = []
        produced: list[tuple[str, str]] = []   # (org, stamp) 与 records 同序(R14)
        for org, issuer, url, parsed in (
            ("noaa", ISSUER_NOAA, NOAA_URL, parse_noaa(self._fetch_noaa())),
            ("jma", ISSUER_JMA, JMA_URL, parse_jma(self._fetch_jma())),
        ):
            if not parsed:
                logger.warning("enso_status %s: 页面结构变了或取不到关键字段,跳过", org)
                continue
            published: datetime = parsed["published"]        # type: ignore[assignment]
            if published < cutoff:
                logger.info("enso_status %s: 发布日 %s 早于窗口,跳过",
                            org, published.date())
                continue
            stamp = published.date().isoformat()
            if last.get(org) == stamp:
                # 官方只月更, 同一期不重复产出 —— 这是本 adapter 存在的理由
                continue
            # 10-05 复审 R14: `last[org] = stamp` **必须等这条真的被返回**才写。
            # 这里的 state 语义是「这一期我已经产出过了」, 与前四个适配器的「见过即跳过」
            # 不同 —— 原来 `last[org] = stamp` 早于 `records.append`, `_save_state` 又早于
            # `records[:limit]` 截断, 于是 `limit=1` 时另一个机构的这一期被永久标记成
            # 「已产出」却没返回。五个适配器里后果最重: 月更通报丢一期 = 丢一个月
            # (要等下个月官方出新通报才会有新条目), 且没有任何日志。
            # 单源失败那条路径不受影响: 抓不到就不进 `produced`, state 不写, 恢复后能补上。

            year_month = f"{published.year:04d}-{published.month:02d}"
            title = f"{issuer.split(' ')[0]} ENSO 状态 {year_month}：{parsed['synopsis']}"
            records.append(ItemRecord(
                item=Item(
                    id=compute_item_id(f"{self.name}:{org}", url=url),
                    source=f"{self.name}:{org}",
                    url=url,
                    title=title[:512],
                    body=str(parsed["body"])[:BODY_LIMIT],
                    author=issuer,
                    ts=published,
                    lang="ja" if org == "jma" else "en",
                    tags=["风险提示", "ENSO", org],
                ),
                adapter_name=self.name,
                source_payload_json=json.dumps(
                    {"kind": "risk", "issuer": issuer,
                     "published": published.astimezone(JST).isoformat(),
                     "regions": [],          # ENSO 是赤道太平洋的大尺度状态, 不属任何国家
                     "level": parsed["status"],
                     "number": parsed["number"],
                     "published_date": stamp},
                    ensure_ascii=False),
                media_urls=[],
            ))
            produced.append((org, stamp))

        # 下面的 sort 会重排 records, 但 produced 不跟着动 —— 原先
        # zip(produced, records) 按位置配对, limit 截断后把「未返回机构」标成已产出
        # (月更通报被 suppression, 丢一个月), 返回的机构反而没进 state(下轮重复产出)。
        # sort 前先按 id(record) 绑定 (org, stamp), 返回哪个就记哪个(R14 的复发形式)。
        produced_by_id = {id(r): ps for ps, r in zip(produced, records)}
        records.sort(key=lambda r: r.item.ts, reverse=True)
        selected = records[:limit] if limit is not None else records
        if limit is not None and len(records) > len(selected):
            logger.info("enso_status 本轮产出 %d 条, limit=%d 只返回 %d 条; "
                        "落选的机构不进 state, 下一轮继续产出",
                        len(records), limit, len(selected), len(records) - len(selected))
        for r in selected:
            org, stamp = produced_by_id[id(r)]
            last[org] = stamp
        _save_state(self.state_path, last)
        return selected
