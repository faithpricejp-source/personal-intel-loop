"""多语地名匹配: 把用户填的行程地名(任何语言写法)解析成全部可匹配的别名。

问题: `paper_risk.default_search_fn` 原先只按一种写法 LIKE。用户填「曼谷」而库里
外务省条目写「タイ」/「バンコク」、WHO 写「Thailand」、中国领事写「泰国」— 一种写法
匹配不到就等于没风险证据。

本模块读 `config/place_aliases.json`(结构: `{"countries": [{"key","zh","ja","en",
"cities": {<city_key>: {"zh","ja","en","hotspot"?}}}]}`), 对外只暴露 `expand()`:

    expand("曼谷", None)      → {"country_key": "thailand", "city_key": "bangkok", "terms": [...]}
    expand("Bangkok", "泰国")  → 同上
    expand("タイ", None)       → country_key=thailand, city_key=None
    expand(" Chiang Mai ", "タイ") → country_key=thailand, city_key=chiangmai

匹配用「归一化后的精确等值」而不是子串包含: 别名之间不该互相吞并(「曼谷」不该命中
「曼谷郊区」这种另说), 而搜索侧(`paper_risk`)另做子串 LIKE。归一化做 NFKC 宽度折叠、
大小写折叠与空白/中点/连字符/下划线剥离, 不做简繁或片假名转换 — 那些形近字形在
原文里本来也是分开的。

表缺失/损坏时全部退化成「原样返回输入」, 不抛: 别名表是增强, 不是行前简报的硬依赖。
"""
from __future__ import annotations

import json
import threading
import unicodedata
from pathlib import Path

from personal_intel_loop import CONFIG_DIR

ALIAS_FILENAME = "place_aliases.json"

#: 表文件缺失/解析失败时的告警只发一次, 之后每次 expand 都走退化路径。
_warned = False
_lock = threading.Lock()
_cache: dict | None = None
_cache_path: str | None = None


def alias_path_for(config_dir: Path | None = None) -> Path:
    return (Path(config_dir) if config_dir is not None else CONFIG_DIR) / ALIAS_FILENAME


def normalize_term(term: str) -> str:
    """匹配用归一化: NFKC 宽度折叠 + 大小写折叠 + 去空白与 `・-_.`。空串返回 ""。"""
    text = str(term or "").strip()
    if not text:
        return ""
    # 10-05 审计 D06: 先 NFKC 折叠全/半角(IME、PDF 复制的「ＵＳＡ」「ﾆｭｰﾖｰｸ」),
    # 再 casefold —— 不折叠的话全半角写法与表内标准写法不等值, 静默漏匹配。
    text = unicodedata.normalize("NFKC", text)
    out = []
    for ch in text.casefold():
        if ch.isspace() or ch in "・·•-_.ー":
            continue
        out.append(ch)
    return "".join(out)


def load_table(path: Path | None = None) -> dict:
    """读别名表 → `{"countries": [...], "by_term": {归一化别名: (country, city|None)}}`。

    缓存按路径; 表变了(同路径改内容)由调用方 `reload()` 强制刷新。
    """
    global _cache, _cache_path, _warned
    target = alias_path_for() if path is None else Path(path)
    key = str(target)
    with _lock:
        if _cache is not None and _cache_path == key:
            return _cache
        try:
            raw = json.loads(target.read_text("utf-8"))
        # 10-05 审计 D07: UnicodeDecodeError 不是 OSError 子类, 编码损坏的表文件
        # 会把异常直接冒出去 —— docstring 承诺「损坏时退化, 不抛」。
        except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            if not _warned:
                _warned = True
                import logging

                logging.getLogger(__name__).warning("place_aliases: 读不到 %s(%s), 退回原样匹配", target, exc)
            # 10-05 审计 D08: 失败得到的空表**不进缓存** —— 缓存了之后文件恢复正常
            # 也不会重试, 长驻进程里别名增强永久静默失效。每次展开重读, 读成功才缓存。
            return {"countries": [], "by_term": {}}
        else:
            table = _index(raw if isinstance(raw, dict) else {})
        _cache, _cache_path = table, key
        return table


def reload() -> None:
    """丢弃缓存(测试/长驻进程里表被改动后用)。"""
    global _cache, _cache_path
    with _lock:
        _cache, _cache_path = None, None


def _index(raw: dict) -> dict:
    """原始 JSON → 带反查索引的表。同一个别名归谁: 城市名归城市, 否则归国家。"""
    countries = raw.get("countries")
    countries = countries if isinstance(countries, list) else []
    by_term: dict[str, tuple[str, str | None]] = {}
    for entry in countries:
        if not isinstance(entry, dict):
            continue
        country_key = str(entry.get("key") or "").strip()
        if not country_key:
            continue
        for lang in ("zh", "ja", "en"):
            for term in _terms(entry.get(lang)):
                by_term.setdefault(normalize_term(term), (country_key, None))
        cities = entry.get("cities")
        for city_key, city in (cities.items() if isinstance(cities, dict) else []):
            city_key = str(city_key or "").strip()
            if not city_key or not isinstance(city, dict):
                continue
            # 城市别名覆盖国家同形别名(「华盛顿州」vs 华盛顿市这类同形写法归城市)
            for lang in ("zh", "ja", "en"):
                for term in _terms(city.get(lang)):
                    by_term[normalize_term(term)] = (country_key, city_key)
    return {"countries": [c for c in countries if isinstance(c, dict)], "by_term": by_term}


def _terms(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if isinstance(v, str) and v.strip()]


def _entry_for(table: dict, country_key: str) -> dict | None:
    for entry in table.get("countries") or []:
        if isinstance(entry, dict) and str(entry.get("key") or "").strip() == country_key:
            return entry
    return None


def resolve(place: str, country: str | None = None, *, path: Path | None = None) -> tuple[str | None, str | None]:
    """只解析出 (country_key, city_key), 不收集别名。"""
    table = load_table(path)
    by_term = table["by_term"]
    place_norm = normalize_term(place)
    country_hit = by_term.get(normalize_term(country)) if country else None

    if place_norm and place_norm in by_term:
        country_key, city_key = by_term[place_norm]
        if city_key:
            return country_key, city_key
        # 10-05 审计 D09: 地点栏命中国家级时以地点栏为准 —— 国家栏只在地栏解析不到时
        # 兜底。原先冲突时国家栏静默盖掉地点栏(「タイ」+「美国」被路由到美国),
        # hotspot_city 与风险证据全跟错国家。
        return country_key, None
    if country_hit:
        return country_hit[0], country_hit[1]
    return None, None


def expand(place: str, country: str | None = None, *, path: Path | None = None) -> dict:
    """行程地名(任一语言写法) → 全部匹配用别名。

    返回 `{"country_key", "city_key", "terms"}`。`terms` 含用户原始写法(去空白)与
    表里该国家/该城市的所有语言别名, 去重且保持稳定顺序(原输入在前)。表里查不到时
    `country_key`/`city_key` 为 None, `terms` 至少含原输入 — 调用方照旧能匹配。
    """
    raw_place = " ".join(str(place or "").split())
    raw_country = " ".join(str(country or "").split())
    country_key, city_key = resolve(raw_place, raw_country, path=path)

    terms: list[str] = []

    def _add(term: str) -> None:
        term = str(term or "").strip()
        if term and term not in terms:
            terms.append(term)

    _add(raw_place)
    _add(raw_country)

    entry = _entry_for(load_table(path), country_key) if country_key else None
    if entry:
        for lang in ("zh", "ja", "en"):
            for term in _terms(entry.get(lang)):
                _add(term)
        cities = entry.get("cities")
        city = cities.get(city_key) if isinstance(cities, dict) and city_key else None
        if isinstance(city, dict):
            for lang in ("zh", "ja", "en"):
                for term in _terms(city.get(lang)):
                    _add(term)
    return {"country_key": country_key, "city_key": city_key, "terms": terms}


def hotspot_city(country_key: str | None, city_key: str | None, *, path: Path | None = None) -> str | None:
    """城市对应的 `ref/hotspots/hotspots_<X>.json` 段名; 没登记返回 None。"""
    if not city_key:
        return None
    entry = _entry_for(load_table(path), country_key) if country_key else None
    if not entry:
        return None
    cities = entry.get("cities")
    city = cities.get(city_key) if isinstance(cities, dict) else None
    if not isinstance(city, dict):
        return None
    hotspot = str(city.get("hotspot") or "").strip()
    return hotspot or None
