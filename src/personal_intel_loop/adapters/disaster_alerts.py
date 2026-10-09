"""灾害预警 adapter —— 只盯配置里列出的几个地方(自己和家人所在地), 别的一律不要。

这条 lane 的价值是**能转发出去、能让人行动**, 不是情报, 所以不要求断言/机制/来龙去脉,
只要求说清哪里、什么时候、多严重。

## 为什么要单独建 adapter
一般新闻源里几乎没有「预警」——只有**灾后**报道(某地暴雨致死 N 人), 那时再转发给家人
已经没用了。所以直接接气象部门的预警发布接口。

## 盯哪儿
`<PIL_CONFIG_DIR>/disaster_alerts.json`(示例见 config/disaster_alerts.example.json):

    {"cn": [{"province": "广东", "city": "广州市"},
            {"province": "浙江", "city": "西湖区", "match": ["西湖区", "杭州市气象台"]}],
     "jp": [{"area_code": "270000", "label": "大阪府"}]}

`match` 是标题里要出现的任一子串, 缺省就是 city 本身(区县级盯点同时收市级台的预警,
因为市级台的标题不带区县)。文件缺失 = 什么都不盯。

临时出行: `PIL_LOCATION_FILE`(缺省 <PIL_HOME>/location.json)里写一个当前位置, 会追加到
盯点里, 不用改主配置; 格式见 `_location_override`。

## 数据源事实(均 2026-08-26 实测)

**中国** `GET https://www.nmc.cn/rest/findAlarm?province=<省>&pageNo=1&pageSize=100`
  中央气象台 alarm.html 背后的 XHR(用浏览器抓网络请求找到的,官方无文档)。免费无鉴权。
  返回 `data.page.list`,每条 {alertid, title, issuetime, pic, url}。
  **县级颗粒度**,title 自带省市县全路径:"陕西省延安市安塞区气象台发布暴雨橙色预警信号",
  所以按市过滤 = title 里 substring 匹配。issuetime 形如 "2026/08/26 20:38"(北京时间,无时区)。
  **必须按 province 拉,不能全国扫**:全国流 300 条只覆盖 3 小时。

**日本** `GET https://www.data.jma.go.jp/developer/xml/feed/extra.xml`(高頻度・随時 Atom)
  条目 link 的文件名自带府県码:`..._VPWW53_130000.xml`(130000 = 東京都, 270000 = 大阪府),
  所以定位到都道府県不用逐个抓 XML。取 title == 気象特別警報・警報・注意報 的最新一条,
  再抓那份 XML,`<Item>` 里 `<Kind><Name>` 是预警名、`<Areas><Area><Name>` 是区市町村。

  **只收 `警報` / `特別警報`,丢掉 `注意報`**:注意報 是常态背景噪音——
  实测一份都市圈文档 166 条里 149 条雷注意報 + 17 条波浪注意報、**零条警報**。
  注意報 不是"能让人行动"的东西,收进来只会把这条 lane 淹掉。

  ⚠ **不要用 `https://www.jma.go.jp/bosai/warning/data/warning/<code>.json`**:
  实测该树曾整体冻结(多个府県 reportDatetime 停在同一天),
  看起来像活的但是死数据。差点建在上面。

Source key 形如 `disaster_alerts:cn:广州市` / `disaster_alerts:jp:大阪府`,按地点分,
好让日报能一眼看出是哪边。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import requests

from personal_intel_loop import APP_HOME, CONFIG_DIR
from personal_intel_loop.schemas import Item, compute_item_id

logger = logging.getLogger(__name__)

CN_BASE = "https://www.nmc.cn"
CN_API = f"{CN_BASE}/rest/findAlarm"
JP_FEED = "https://www.data.jma.go.jp/developer/xml/feed/extra.xml"
JP_WARNING_TITLE = "気象特別警報・警報・注意報"
REQUEST_TIMEOUT = 20
CN_PAGE_SIZE = 100
BODY_LIMIT = 20_000

CST = timezone(timedelta(hours=8))

# 中国预警信号四级: 蓝(IV 最低) < 黄(III) < 橙(II) < 红(I 最高)。
# 实测全国 3 小时 300 条样本: 黄 63% / 蓝 21% / 橙 16% / 红 0% —— **黄色是常态档不是高级档**。
# 所以**橙/红才入库**: 黄色留在库里会把日报预警段占满, 而这条 lane 的判据是"能不能让人行动"。
# 黄色连采都不采 —— 不是"采了不推", 是库里干脆没有。
CN_LEVELS = {"红色": 4, "橙色": 3, "黄色": 2, "蓝色": 1}
CN_MIN_LEVEL = 3          # 橙色及以上才入库


def cn_level(title: str) -> int:
    """标题里的等级 → 1-4; 没写等级的返回 0(不丢, 但也不够格发邮件)。"""
    for word, lv in CN_LEVELS.items():
        if word in title:
            return lv
    return 0

#: 盯点配置文件。
WATCH_CONFIG_PATH = CONFIG_DIR / "disaster_alerts.json"


def load_watch_config(path: Path) -> tuple[tuple[dict, ...], tuple[dict, ...]]:
    """→ (CN 盯点, JP 盯点)。文件缺失/损坏返回空名单(只记一行 warning)。"""
    try:
        cfg = json.loads(Path(path).read_text("utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("顶层应为对象")
    except FileNotFoundError:
        return (), ()
    except Exception as exc:  # noqa: BLE001
        logger.warning("disaster_alerts: 盯点配置不可用(%s), 不盯任何地点", exc)
        return (), ()
    cn = []
    for w in cfg.get("cn") or []:
        if isinstance(w, dict) and w.get("province") and w.get("city"):
            entry = {"province": str(w["province"]), "city": str(w["city"])}
            if w.get("match"):
                m = w["match"]
                entry["match"] = (m,) if isinstance(m, str) else tuple(m)
            cn.append(entry)
    jp = [
        {"area_code": str(w["area_code"]), "label": str(w.get("label") or w["area_code"])}
        for w in cfg.get("jp") or []
        if isinstance(w, dict) and w.get("area_code")
    ]
    return tuple(cn), tuple(jp)


WATCHED_CN, WATCHED_JP = load_watch_config(WATCH_CONFIG_PATH)

# 临时所在地。出门时只改这个文件、不改主配置。文件缺失/损坏一律只用主配置。
LOCATION_FILE = Path(os.environ.get("PIL_LOCATION_FILE") or (APP_HOME / "location.json")).expanduser()


def _location_override() -> tuple[tuple[dict, ...], tuple[dict, ...]]:
    """→ (追加的 CN 盯点, 追加的 JP 盯点)。已在默认名单里的不重复追加。"""
    try:
        # 形状校验放进 try: JSON 合法但顶层是 list/str 时, loc.get 的 AttributeError
        # 会一路崩到 alerts-notify, 每 30 分钟一次的预警投递全停。
        loc = json.loads(LOCATION_FILE.read_text())
        if not isinstance(loc, dict):
            raise ValueError(f"顶层应为对象, 实际是 {type(loc).__name__}")
    except Exception as exc:  # 文件不存在/坏了/形状不对 → 回落默认, 只记一行
        logger.warning("disaster_alerts: 位置文件不可用(%s), 用默认盯点", exc)
        return (), ()
    cn, jp = [], []
    country = (loc.get("country") or "").upper()
    if country == "JP":
        code = loc.get("jp_area_code")
        if code and str(code) not in {w["area_code"] for w in WATCHED_JP}:
            jp.append({"area_code": str(code), "label": loc.get("label") or str(code)})
    elif country == "CN":
        prov, city = loc.get("cn_province"), loc.get("cn_city")
        if prov and city and city not in {w["city"] for w in WATCHED_CN}:
            w = {"province": prov, "city": city}
            if loc.get("cn_match"):
                # 写成字符串时不能 tuple() 拆成单字,
                # 那会让「区」这种字到处命中、预警刷屏。字符串包成单元素元组。
                raw_match = loc["cn_match"]
                if isinstance(raw_match, str):
                    w["match"] = (raw_match,)
                else:
                    w["match"] = tuple(raw_match)
            cn.append(w)
    return tuple(cn), tuple(jp)


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


def _get(url: str, params: dict | None = None) -> requests.Response | None:
    """网络/非 200 一律返回 None 由调用方跳过。一个地方拉不到不该让整轮 ingest 挂掉。"""
    try:
        r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("disaster_alerts fetch failed %s: %s", url, exc)
        return None
    if r.status_code != 200:
        logger.warning("disaster_alerts non-200 %s: %s", url, r.status_code)
        return None
    return r


def _fetch_cn(province: str) -> list[dict[str, Any]]:
    r = _get(CN_API, {"province": province, "pageNo": 1, "pageSize": CN_PAGE_SIZE,
                      "signaltype": "", "signallevel": ""})
    if r is None:
        return []
    try:
        return r.json()["data"]["page"]["list"] or []
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("disaster_alerts cn parse failed (%s): %s", province, exc)
        return []


def _fetch_jp_feed() -> str:
    r = _get(JP_FEED)
    return r.text if r is not None else ""


def _fetch_jp_doc(url: str) -> str:
    r = _get(url)
    return r.text if r is not None else ""


def _parse_cn_ts(raw: str) -> datetime | None:
    """"2026/08/26 20:38" —— 北京时间, 源里不带时区。"""
    try:
        return datetime.strptime(raw.strip(), "%Y/%m/%d %H:%M").replace(tzinfo=CST).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


_ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.S)
_TITLE_RE = re.compile(r"<title>([^<]+)</title>")
_LINK_RE = re.compile(r'<link[^>]*href="([^"]+)"')
_UPDATED_RE = re.compile(r"<updated>([^<]+)</updated>")
_ITEM_RE = re.compile(r"<Item>(.*?)</Item>", re.S)
_KIND_RE = re.compile(r"<Kind>\s*<Name>([^<]+)</Name>")
_AREA_RE = re.compile(r"<Area>\s*<Name>([^<]+)</Name>")
_REPORT_TS_RE = re.compile(r"<ReportDateTime>([^<]+)</ReportDateTime>")


def _latest_jp_doc_url(feed_text: str, area_code: str) -> tuple[str, str] | None:
    """Atom 里挑该府県最新的一份「気象特別警報・警報・注意報」。返回 (url, updated)。"""
    best: tuple[str, str] | None = None
    for raw in _ENTRY_RE.findall(feed_text):
        title = _TITLE_RE.search(raw)
        link = _LINK_RE.search(raw)
        updated = _UPDATED_RE.search(raw)
        if not (title and link and updated):
            continue
        if title.group(1) != JP_WARNING_TITLE:
            continue
        if not link.group(1).endswith(f"_{area_code}.xml"):
            continue
        if best is None or updated.group(1) > best[1]:
            best = (link.group(1), updated.group(1))
    return best


def _jp_warnings(doc_text: str) -> list[tuple[str, str]]:
    """(区市町村, 预警名)。只要名字里含 `警報` 的 —— 命中 大雨警報 / 暴風特別警報,
    漏掉 雷注意報 / 波浪注意報(「注意報」不含「警報」二字, 所以一个 in 判断就够,
    不需要再排除一次: 2026-08-26 变异测试证实那个额外条件是死代码)。"""
    out: list[tuple[str, str]] = []
    for raw in _ITEM_RE.findall(doc_text):
        areas = _AREA_RE.findall(raw)
        if not areas:
            continue
        for kind in _KIND_RE.findall(raw):
            if "警報" in kind:
                out.append((areas[0], kind))
    return out


class DisasterAlertsAdapter:
    name = "disaster_alerts"

    def __init__(
        self,
        *,
        fetch_cn: Callable[[str], list[dict[str, Any]]] | None = None,
        fetch_jp_feed: Callable[[], str] | None = None,
        fetch_jp_doc: Callable[[str], str] | None = None,
    ) -> None:
        self._fetch_cn = fetch_cn or _fetch_cn
        self._fetch_jp_feed = fetch_jp_feed or _fetch_jp_feed
        self._fetch_jp_doc = fetch_jp_doc or _fetch_jp_doc

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable[ItemRecord]:
        # (who_don R09 同型): cli 的 --since 经 datetime.fromisoformat 得到 naive 值,
        # naive.astimezone(utc) 按宿主本地时区解释 → 同一命令在 TZ=UTC 与 TZ=Asia/Tokyo 下
        # cutoff 差 9 小时。按北京时间解释: CN 源 issuetime 本来就是北京时间(见 _parse_cn_ts),
        # 与 cn_consular R09 同口径; JP 源的 cutoff 因此比按 JST 解释晚 1 小时, 换来跨宿主可复现。
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=CST)
        since_utc = since.astimezone(timezone.utc) if since else None
        records: list[ItemRecord] = []
        records.extend(self._collect_cn(since_utc))
        records.extend(self._collect_jp(since_utc))
        records.sort(key=lambda r: r.item.ts, reverse=True)
        return records[:limit] if limit is not None else records

    def _collect_cn(self, since_utc: datetime | None) -> list[ItemRecord]:
        out: list[ItemRecord] = []
        for watch in WATCHED_CN + _location_override()[0]:
            province, city = watch["province"], watch["city"]
            patterns = watch.get("match") or (city,)
            for raw in self._fetch_cn(province):
                title = str(raw.get("title") or "").strip()
                if not any(pat in title for pat in patterns):
                    continue
                # 黄/蓝不采(合计 84% 的量), 不是"要行动"的东西。无等级词的留下(返回 0),
                # 它们进日报不进邮件——宁可多看一眼, 不能把没写等级的重灾情丢了。
                lv = cn_level(title)
                if 0 < lv < CN_MIN_LEVEL:
                    continue
                ts_utc = _parse_cn_ts(str(raw.get("issuetime") or ""))
                if ts_utc is None or (since_utc is not None and ts_utc < since_utc):
                    continue
                # url 是站内相对路径 "/publish/alarm/<id>_<ts>.html", 要补域名。
                raw_url = str(raw.get("url") or "").strip()
                url = (CN_BASE + raw_url) if raw_url.startswith("/") else (raw_url or CN_API)
                source = f"{self.name}:cn:{city}"
                out.append(ItemRecord(
                    item=Item(
                        id=compute_item_id(source, url=url, guid=str(raw.get("alertid") or "") or None),
                        source=source,
                        url=url,
                        title=title[:512],
                        # 这条 lane 不做摘要, body 就是可直接转发的一句话。
                        body=f"{title}(发布时间 {raw.get('issuetime')})"[:BODY_LIMIT],
                        author="中央气象台",
                        ts=ts_utc,
                        lang="zh",
                        tags=["灾害预警", city],
                    ),
                    adapter_name=self.name,
                    source_payload_json=json.dumps(
                        {"alertid": raw.get("alertid"), "issuetime": raw.get("issuetime"),
                         "province": province, "city": city, "pic": raw.get("pic")},
                        ensure_ascii=False),
                    media_urls=[],
                ))
        return out

    def _collect_jp(self, since_utc: datetime | None) -> list[ItemRecord]:
        if not (WATCHED_JP + _location_override()[1]):
            return []
        feed = self._fetch_jp_feed()
        if not feed:
            return []
        out: list[ItemRecord] = []
        for watch in WATCHED_JP + _location_override()[1]:
            code, label = watch["area_code"], watch["label"]
            found = _latest_jp_doc_url(feed, code)
            if found is None:
                logger.info("disaster_alerts jp: feed 里没有 %s 的警報文档", label)
                continue
            doc_url, _updated = found
            doc = self._fetch_jp_doc(doc_url)
            if not doc:
                continue
            report = _REPORT_TS_RE.search(doc)
            ts_utc = _parse_jp_ts(report.group(1)) if report else None
            if ts_utc is None or (since_utc is not None and ts_utc < since_utc):
                continue
            warnings = _jp_warnings(doc)
            if not warnings:
                # 只有注意報 = 没有值得转发的东西。不产出空条目。
                logger.info("disaster_alerts jp: %s 当前无警報级(只有注意報)", label)
                continue
            source = f"{self.name}:jp:{label}"
            areas = sorted({a for a, _ in warnings})
            kinds = sorted({k for _, k in warnings})
            title = f"{label} {'・'.join(kinds)}({len(areas)} 市区町村)"
            body = "\n".join(f"{area}: {kind}" for area, kind in warnings)
            out.append(ItemRecord(
                item=Item(
                    id=compute_item_id(source, url=doc_url, guid=doc_url),
                    source=source,
                    url=doc_url,
                    title=title[:512],
                    body=body[:BODY_LIMIT],
                    author="気象庁",
                    ts=ts_utc,
                    lang="ja",
                    tags=["灾害预警", label],
                ),
                adapter_name=self.name,
                source_payload_json=json.dumps(
                    {"area_code": code, "doc_url": doc_url, "areas": areas, "kinds": kinds},
                    ensure_ascii=False),
                media_urls=[],
            ))
        return out


def _parse_jp_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.strip()).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None
