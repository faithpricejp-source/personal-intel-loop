"""行前风险简报的离线官方参考资料块(契约第 9 节)。

摘录根目录 `ref/` 下现有六类**离线**的官方原文/官方统计(缺目录/缺文件一律返回空串,
不联网, 简报退回只靠库里条目):

  - `official/<国家>.md` 各国官方安全提示逐字原文(日本外务省 / 英国 FCDO / 中国领事服务网);
  - `official/district_time_lines.md` 其中涉及具体街区/时段的句子的索引;
  - `hotspots/hotspots_<city>.json` 美国四城按警署辖区 × 时段的犯罪统计;
  - `health/<国家>.md` 该国 **CDC Travelers' Health + 厚生労働省検疫所 FORTH** 原文,
    `health/cdc_notices.md` 是全部 CDC 旅行健康通知(列表, 当前按国家文件取, 见「没把握」);
  - `advisory_us/us_state_advisories.md` **美国国务院**各国 Travel Advisory 原文, 每国一节;
  - `climate/` 本次厄尔尼诺的官方原文: `travel_lines.md`(各国官方对本次影响的原文行)、
    `sea_enso_impacts.md`(东南亚各国机构按国分节)、`enso_status.md`(NOAA/JMA 当期状态);
  - `home/` 常驻地的季节展望与区防灾资料, 只由 `load_home_reference()` 读。

`load_reference()` 的取舍顺序(`max_chars` 超长时按 `_WEIGHTS` 权重分字数, 高的先保底):

  1. `district_time_lines.md` 里该国的行, 本城的排在前面 — 具体街区/时段最有用;
  2. 美国国务院该国节的等级原文与地区限制句;
  3. 该国健康文件(CDC Notices / FORTH 感染症・医療情報);
  4. 气候: 该国在 `travel_lines.md` / `sea_enso_impacts.md` 的行与节 + 所有人都附的
     NOAA/JMA 当期 ENSO 状态;
  5. 该国文件的「危険レベル（当前）」+「犯罪発生状況、防犯対策」;
  6. 该国文件的「滞在時の留意事項」、英国 FCDO 的「Safety and security」(含其下 #### 子节);
  7. 该城有文件时的 `hotspots_<city>.json` 前 10 条。

每个块有独立字数上限(见 `DEFAULT_*_MAX_CHARS`, 都可由 `load_reference()` 的关键字参数覆盖),
超长时**只在段落/句子边界截断**, 且每段的来源 URL 与日期行必须留下。

每段都带摘录里标注的机构名/来源 URL/日期 —— 提示词要求模型引用时注明机构与日期,
摘录日期旧的要向用户说明, 所以日期不能丢。
"""
from __future__ import annotations

import json
import re
from datetime import date as _date
from pathlib import Path

from personal_intel_loop import DATA_DIR
from personal_intel_loop import place_aliases

#: 官方原文摘录默认根目录(与 ref/official、ref/hotspots 同级)。
DEFAULT_REF_DIRNAME = "risk_reference"
OFFICIAL_DIRNAME = "official"
HOTSPOTS_DIRNAME = "hotspots"
DISTRICT_LINES_FILENAME = "district_time_lines.md"
HEALTH_DIRNAME = "health"
CLIMATE_DIRNAME = "climate"
ADVISORY_US_DIRNAME = "advisory_us"
HOME_DIRNAME = "home"

HEALTH_NOTICES_FILENAME = "cdc_notices.md"
US_ADVISORY_FILENAME = "us_state_advisories.md"
ENSO_STATUS_FILENAME = "enso_status.md"
TRAVEL_LINES_FILENAME = "travel_lines.md"
SEA_ENSO_FILENAME = "sea_enso_impacts.md"
#: 常驻地两个 action_lines 文件 —— 居民行动含义大的原文行。
HOME_ACTION_FILES = ("action_lines.md", "district_action_lines.md")

#: `place_aliases` 的 country_key → `ref/health/` 文件名。写法不一致的在这张表里显式映射,
#: 其余的按 `country_key + ".md"` 找(表里有就优先用表的)。`ref/official/` 用的是
#: `southkorea`/`unitedstates` 连写, `ref/health/` 用的是 `south-korea`/`united-states`。
HEALTH_FILENAME_BY_COUNTRY = {
    "southkorea": "south-korea.md",
    "unitedstates": "united-states.md",
    "unitedkingdom": "united-kingdom.md",
}

#: 参考块总长上限(字)。四类新块(健康/美国国务院/气候)加进来后从 6000 提到 9000。
DEFAULT_MAX_CHARS = 9000
#: 各块独立字数上限(字)。总长不够时先按 `_WEIGHTS` 权重分, 分到各块后再各自截断。
HEALTH_MAX_CHARS = 2000
ADVISORY_US_MAX_CHARS = 1600
CLIMATE_MAX_CHARS = 1800
#: `enso_status.md` 的 NOAA/JMA 两节是所有国家都附的, 单独给一份上限(不占按国那两份)。
ENSO_STATUS_MAX_CHARS = 1400
#: 常驻地参考块总长上限(字)。
HOME_MAX_CHARS = 3000
#: hotspots 取前 N 条。
HOTSPOT_LIMIT = 10

#: 截断优先级(数字越小越优先, 见 `_WEIGHTS`)。街区时段行最有用, 其次是美国国务院的
#: 地区限制, 再是健康通知与气候 —— 官方国家文件的长节与统计表最后。
PRIORITY_DISTRICT = 1
PRIORITY_ADVISORY_US = 2
PRIORITY_HEALTH = 3
PRIORITY_CLIMATE = 4
PRIORITY_OFFICIAL = 5
PRIORITY_OFFICIAL_SUB = 6
PRIORITY_HOTSPOT = 7
#: 单个官方节在参考块里最多占多少字(节内按「是否提到本城」排序后截断)。
SECTION_MAX_CHARS = 2200
#: 目录壳下最多取几个子节(再多就把 FCDO 的 Crime/Scam/Alcohol… 挤出去了)。
MAX_SUBSECTIONS = 6
#: district_time_lines 里某城最多取几行(剩下的国家级行还有余量再补)。
DISTRICT_CITY_ROWS = 8
DISTRICT_COUNTRY_ROWS = 6

#: 块标题关键词(小写比较; 日文原样, 英文小写)。命中即认为该节与行前风险相关。
_SECTION_KEYWORDS = (
    "危険レベル",
    "犯罪発生状況",
    "防犯対策",
    "滞在時の留意事項",
    "査証、出入国審査",
    "安全対策基礎データ",
    "safety and security",
)
#: FCDO `### 1. Safety and security` 之下优先摘的子节(空壳 `#### Crime` 只当目录用)。
_SUBSECTION_KEYWORDS = (
    "犯罪発生状況",
    "防犯対策",
    "滞在時の留意事項",
    "査証、出入国審査",
    "crime",
    "scam",
    "sexual assault",
    "drink spiking",
    "violent crime",
    "protecting yourself",
    "belongings",
    "alcohol",
    "drugs",
    "personal id",
    "laws and cultural",
    "bans",
    "transport risks",
    "road travel",
    "taxis",
    "typhoon",
    "earthquake",
    "natural disasters",
    "demonstrations",
)
_ISO_DATE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")
_URL = re.compile(r"https?://[^\s)｜|）\]>」』]+")
_META_URL = re.compile(r"来源\s*URL\s*[:：]\s*(\S+)")
_META_UPDATED = re.compile(r"(?:更新|标注)日期\s*[:：]\s*(.+)")
_META_PUBLISHED = re.compile(r"标注发布日期\s*[:：]\s*(.+)")
_META_FETCHED = re.compile(r"抓取时间[^:：]*[:：]\s*(.+)")


def default_ref_dir() -> Path:
    return DATA_DIR / DEFAULT_REF_DIRNAME


def _country_file(ref_dir: Path, country_key: str | None) -> Path | None:
    if not country_key:
        return None
    path = Path(ref_dir) / OFFICIAL_DIRNAME / f"{country_key}.md"
    return path if path.is_file() else None


def _read(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.read_text("utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return ""


def _iso_dates(text: str) -> list[str]:
    return [f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" for m in _ISO_DATE.finditer(text or "")]


def _fmt_date(value) -> str:
    return value.isoformat() if isinstance(value, _date) else str(value or "")


# --------------------------------------------------------------------------- #
# district_time_lines.md
# --------------------------------------------------------------------------- #

def _row_cells(line: str) -> list[str]:
    line = line.strip()
    if not line.startswith("|"):
        return []
    cells = [c.strip() for c in line.strip("|").split("|")]
    return cells if len(cells) >= 5 else []


def _row_cells4(line: str) -> list[str]:
    """四列表(「节 | 原文 | URL | 日期」, `ref/home/` 的两张 action_lines)。

    表头行(`| 节 | 原文 | URL | 日期 |`)与分隔行(`|---|---|---|---|`)都要滤掉 ——
    表头不是原文, 混进去会被当成「(来源 URL: URL)」这种占位行。
    """
    line = line.strip()
    if not line.startswith("|"):
        return []
    cells = [c.strip() for c in line.strip("|").split("|")]
    if len(cells) != 4 or all(set(c) <= {"-", ""} for c in cells):
        return []
    if _URL.search(cells[2] or "") and cells[2].startswith("http"):
        return cells
    # URL 列不是 URL 的行 = 表头或分隔行, 不是原文行。
    return []


def _latin_word_hit(norm_cell: str, norm_alias: str) -> bool:
    """纯拉丁单词别名的词边界命中: 命中处前后不能再是拉丁字母。"""
    start = 0
    while True:
        at = norm_cell.find(norm_alias, start)
        if at < 0:
            return False
        before = norm_cell[at - 1] if at > 0 else ""
        after_at = at + len(norm_alias)
        after = norm_cell[after_at] if after_at < len(norm_cell) else ""
        if not (before.isascii() and before.isalpha()) and not (after.isascii() and after.isalpha()):
            return True
        start = at + 1


def _cell_hit(cell: str, aliases: list[str]) -> bool:
    """表格单元格里是否出现某个别名(归一化后子串; 别名至少 2 字)。

    # 10-05 审计 D24: 归一化剥掉点号后 "U.S." 成了 "us", 是 "Russia/Austria/Belarus"
    # 的子串; "America" 是 "South America/Central America" 的子串 —— 裸子串会把
    # 别国的行误归入美国。纯拉丁单词别名改词边界匹配; 多词别名保持子串 ——
    # 归一化剥了空格, "United States" 嵌在 "United States of America" 里必然贴字母;
    # CJK 别名(日文国名)也保持子串 —— 日文没有词边界。
    """
    norm_cell = place_aliases.normalize_term(cell)
    if not norm_cell:
        return False
    for alias in aliases:
        norm_alias = place_aliases.normalize_term(alias)
        if len(norm_alias) < 2 or norm_alias not in norm_cell:
            continue
        multi_word = any(ch.isspace() for ch in alias)
        if not multi_word and norm_alias.isascii() and norm_alias.isalpha() and not _latin_word_hit(norm_cell, norm_alias):
            continue
        return True
    return False


def _district_lines_block(ref_dir: Path, country_key: str, city_key: str | None, alias_path: Path | None) -> str:
    """该国(本城优先)的街区/时段原文行。"""
    text = _read(Path(ref_dir) / OFFICIAL_DIRNAME / DISTRICT_LINES_FILENAME)
    if not text:
        return ""
    table = place_aliases.load_table(alias_path)
    entry = next(
        (c for c in table["countries"] if isinstance(c, dict) and str(c.get("key")) == country_key),
        None,
    )
    if not entry:
        return ""
    country_aliases = [t for lang in ("zh", "ja", "en") for t in place_aliases._terms(entry.get(lang))]
    cities = entry.get("cities") if isinstance(entry.get("cities"), dict) else {}
    city = cities.get(city_key) if city_key and cities else None
    city_aliases = (
        [t for lang in ("zh", "ja", "en") for t in place_aliases._terms(city.get(lang))] if isinstance(city, dict) else []
    )

    city_rows: list[str] = []
    country_rows: list[str] = []
    for line in text.splitlines():
        cells = _row_cells(line)
        if not cells or not _cell_hit(cells[0], country_aliases):
            continue
        _, city_cell, tag, sentence, url = cells[0], cells[1], cells[2], cells[3], cells[4]
        if not sentence:
            continue
        where = city_cell or "-"
        row = f"- [{where}／{tag}] {sentence} (来源 URL: {url})"
        if city_aliases and _cell_hit(city_cell, city_aliases):
            city_rows.append(row)
        else:
            country_rows.append(row)
    if not city_rows and not country_rows:
        return ""

    kept = city_rows[:DISTRICT_CITY_ROWS] + country_rows[:DISTRICT_COUNTRY_ROWS]
    scope = f"{country_key} / {city_key}" if city_key else country_key
    lines = [
        f"## 涉及具体街区・时段的官方原文行（来源: {OFFICIAL_DIRNAME}/{DISTRICT_LINES_FILENAME}, {scope}）",
        *kept,
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 国家文件分节
# --------------------------------------------------------------------------- #

def _prose(body: str) -> str:
    """围栏外的元信息行/项目符号/「原文:」标签行都去掉, 只留真正的文。

    官方原文都在 ``` 围栏里(逐字), 围栏外的 `- xxx` 全是抓取时写的元信息与目录说明。
    """
    kept: list[str] = []
    in_fence = False
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            kept.append(line)
            continue
        if not in_fence:
            if stripped.startswith(("-", "*", "+", "｜")) or stripped.startswith("原文"):
                continue
        kept.append(line)
    return "\n".join(kept).strip()


def _blocks(text: str) -> list[dict]:
    """md → [{level, title, meta, body}]。`meta` 是标题后、首个子标题前的元信息行。"""
    out: list[dict] = []
    current: dict | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
        if m:
            if current:
                out.append(current)
            current = {"level": len(m.group(1)), "title": m.group(2), "lines": []}
        elif current is not None:
            current["lines"].append(line)
    if current:
        out.append(current)
    for block in out:
        block["body"] = "\n".join(block["lines"]).strip()
    return out


def _meta_of(block: dict) -> dict:
    """节内「- 来源 URL:」「- 页面上标注の更新日期:」「- 抓取时间」元信息行。

    只认真实元信息行, **不**从正文里猜 URL: `#### TASK 要求の節「...」` 这种子节正文里
    有一堆引用链接, 随便取第一个当来源就是错的(它的来源在父节里)。
    """
    meta = {"url": "", "updated": "", "fetched": "", "published": ""}
    for line in block["body"].splitlines()[:12]:
        stripped = line.strip()
        if not stripped.startswith("-") and not stripped.startswith("|"):
            if stripped:
                break
            continue
        m = _META_URL.search(stripped)
        if m:
            meta["url"] = meta["url"] or m.group(1).strip()
        m = _META_UPDATED.search(stripped)
        if m:
            meta["updated"] = meta["updated"] or m.group(1).strip()
        m = _META_PUBLISHED.search(stripped)
        if m:
            meta["published"] = meta["published"] or m.group(1).strip()
        m = _META_FETCHED.search(stripped)
        if m:
            meta["fetched"] = meta["fetched"] or m.group(1).strip()
    return meta


def _meta_with_ancestors(blocks: list[dict], index: int) -> dict:
    """本节没有元信息行时, 往上找最近的有元信息的祖先节。

    `#### TASK 要求の節「...」` 这种子节自己不带「来源 URL」, 那些行在父节
    `### 2. 「安全対策基礎データ」` 里 — 不往上找, 这节就没法标注来源与日期。
    """
    meta = _meta_of(blocks[index])
    level = blocks[index]["level"]
    for prev in range(index - 1, -1, -1):
        if blocks[prev]["level"] >= level:
            continue
        parent = _meta_of(blocks[prev])
        level = blocks[prev]["level"]
        for key in ("url", "updated", "fetched", "published"):
            if not meta[key] and parent[key]:
                meta[key] = parent[key]
        if meta["url"] and (meta["updated"] or meta["fetched"]):
            break
    if not meta["url"]:
        # 兜底: 本节正文里的第一个 URL(总比没有来源好)
        m = _URL.search(blocks[index]["body"])
        if m:
            meta["url"] = m.group(0)
    return meta


def _paragraphs(body: str) -> list[str]:
    """节正文 → 段落块(``` 围栏原样保留为一段)。元信息行与「原文:」标签行不算段。"""
    out: list[str] = []
    buf: list[str] = []
    in_fence = False
    for raw in _prose(body).splitlines():
        if raw.strip().startswith("```"):
            in_fence = not in_fence
            buf.append(raw)
            if not in_fence:
                out.append("\n".join(buf).strip())
                buf = []
            continue
        if in_fence or raw.strip():
            buf.append(raw)
        elif buf:
            out.append("\n".join(buf).strip())
            buf = []
    if buf:
        out.append("\n".join(buf).strip())
    return [p for p in out if p]


def _order_paragraphs(paragraphs: list[str], aliases: list[str]) -> list[str]:
    """提到目的地的段排前面, 不相关的排后面。顺序稳定(同级保持原序)。"""
    norm = [place_aliases.normalize_term(a) for a in aliases]
    norm = [a for a in norm if a]

    def rank(paragraph: str) -> int:
        flat = place_aliases.normalize_term(paragraph)
        return 0 if any(a in flat for a in norm) else 1

    return sorted(paragraphs, key=rank)


def _fit(paragraphs: list[str], limit: int, *, note: str = "") -> str:
    """按顺序拼段到 limit 字, 放不下就断在最后一个完整段并标注省略。

    截断只发生在段落边界(单段本身就超 `limit` 时才不得不切一刀, 那是没法 avoidable 的)。
    `note` 覆盖省略标注里「全文见哪」的目录名 —— 不同来源的块指向不同目录。
    """
    tail = note or f"全文见 {OFFICIAL_DIRNAME}/ 文件"
    out: list[str] = []
    used = 0
    for para in paragraphs:
        if used + len(para) + 1 > limit:
            if not out:
                out.append(para[:limit])
                return "\n".join(out) + f"\n…（本节过长, 已截断; {tail}）"
            return "\n".join(out) + f"\n…（本节其余内容已省略; {tail}）"
        out.append(para)
        used += len(para) + 1
    return "\n".join(out)


def _subsections(blocks: list[dict], index: int, keywords: tuple[str, ...]) -> list[int]:
    """`blocks[index]` 的下级块下标(更深 level), 关键词命中的优先; 到同级/更高级标题为止。"""
    base_level = blocks[index]["level"]
    subs: list[int] = []
    for offset in range(index + 1, len(blocks)):
        if blocks[offset]["level"] <= base_level:
            break
        subs.append(offset)
    hit = [i for i in subs if any(k in blocks[i]["title"].lower() for k in keywords)]
    if hit:
        return hit[:MAX_SUBSECTIONS]
    return [i for i in subs if _prose(blocks[i]["body"])][:MAX_SUBSECTIONS]


def _official_sections(
    ref_dir: Path,
    country_key: str,
    city_key: str | None,
    alias_path: Path | None,
) -> list[tuple[int, str]]:
    """该国文件的相关节 → `[(优先级, markdown 块)]`。"""
    text = _read(_country_file(ref_dir, country_key))
    if not text:
        return []
    table = place_aliases.load_table(alias_path)
    entry = next(
        (c for c in table["countries"] if isinstance(c, dict) and str(c.get("key")) == country_key),
        None,
    )
    cities = entry.get("cities") if entry and isinstance(entry.get("cities"), dict) else {}
    city = cities.get(city_key) if city_key and cities else None
    city_aliases = (
        [t for lang in ("zh", "ja", "en") for t in place_aliases._terms(city.get(lang))] if isinstance(city, dict) else []
    )
    country_aliases = [t for lang in ("zh", "ja", "en") for t in place_aliases._terms((entry or {}).get(lang))]

    blocks = _blocks(text)
    picked: list[tuple[int, str]] = []
    emitted: set[int] = set()
    for index, block in enumerate(blocks):
        if index in emitted:
            continue
        title = block["title"]
        low = title.lower()
        meta = _meta_with_ancestors(blocks, index)
        aliases = city_aliases or country_aliases
        is_shell_head = any(k.lower() in low for k in ("危険レベル", "安全対策基礎データ", "safety and security"))
        if is_shell_head and not _prose(block["body"]):
            # 目录壳: `### 2.「安全対策基礎データ」`/`### 1. Safety and security` 自己只有
            # 元信息行, 正文全在下级 #### 里。子节合成**一个**块(共用一份来源/日期头) —
            # 逐个子节各出一个块的话, 每块都要重复 ~200 字头, 总长根本剩不下正文。
            priority = PRIORITY_OFFICIAL_SUB if "safety and security" in low else PRIORITY_OFFICIAL
            subs = _subsections(blocks, index, _SUBSECTION_KEYWORDS)
            emitted.update(subs)
            body_parts = [
                f"〔{blocks[i]['title']}〕\n{_prose(blocks[i]['body'])}"
                for i in subs
                if _prose(blocks[i]["body"]).strip()  # FCDO 的 `#### Crime` 是空壳目录项
            ]
            merged = "\n\n".join(body_parts)
            chunk = _render_section(f"{title}（含 {len(body_parts)} 个子节）", merged, aliases, meta)
            if chunk:
                picked.append((priority, chunk))
        elif is_shell_head:
            priority = PRIORITY_OFFICIAL_SUB if "safety and security" in low else PRIORITY_OFFICIAL
            chunk = _render_section(title, block["body"], aliases, meta)
            if chunk:
                picked.append((priority, chunk))
        elif any(k in title for k in _SECTION_KEYWORDS):
            chunk = _render_section(title, block["body"], aliases, meta)
            if chunk:
                priority = PRIORITY_OFFICIAL if ("犯罪発生状況" in title or "防犯対策" in title) else PRIORITY_OFFICIAL_SUB
                picked.append((priority, chunk))
    return picked


def _render_section(title: str, body: str, aliases: list[str], meta: dict) -> str:
    if not body.strip():
        return ""
    paragraphs = _order_paragraphs(_paragraphs(body), aliases)
    if not paragraphs:
        return ""
    return _render_section_limited(title, paragraphs, meta, SECTION_MAX_CHARS)


def _render_section_limited(title: str, paragraphs: list[str], meta: dict, limit: int, *,
                            note: str = "") -> str:
    """按段拼出处 + 来源/日期头, 正文按 `limit` 字截断。

    `note` 是省略标注里「全文见哪」的目录名 —— 同一段原文在 `official/` 与 `health/`
    下各有一份, 指向错的目录等于让读者去错的地方找全文。
    """
    header = f"### {title}"
    provenance = []
    if meta.get("url"):
        provenance.append(f"来源 URL: {meta['url']}")
    if meta.get("updated"):
        provenance.append(f"官方标注更新日期: {meta['updated']}")
    if meta.get("published"):
        provenance.append(f"官方标注发布日期: {meta['published']}")
    if meta.get("fetched"):
        provenance.append(f"抓取时间: {meta['fetched']}")
    dates = _iso_dates(meta.get("updated") or "") or _iso_dates(meta.get("fetched") or "")
    if dates:
        provenance.append(f"摘录日期(ISO): {dates[0]}")
    if provenance:
        header += "\n" + "\n".join(f"- {p}" for p in provenance)
    return f"{header}\n\n原文:\n\n{_fit(paragraphs, limit, note=note)}"


# --------------------------------------------------------------------------- #
# 健康（ref/health/<国家>.md · CDC Travelers' Health + 厚労省検疫所 FORTH）
# --------------------------------------------------------------------------- #

#: 健康块优先摘的节(标题关键词, 小写比较)。`Notices` 是该国当期旅行健康通知,
#: FORTH 那几节是当地医疗/防疫/气候防病信息 —— 都是「去这个国家会不会生病」的核心。
_HEALTH_SECTION_KEYWORDS = (
    "notices",
    "感染症",
    "医療情報",
    "気候と気をつけたい病気",
    "最新",
    "注意",
)
#: FORTH 页面里「这个小节不存在」的占位说明, 不是原文, 摘出来只会占字数。
_HEALTH_PLACEHOLDER = "页面无此节"


def _health_file(ref_dir: Path, country_key: str | None) -> Path | None:
    """`ref/health/` 里该国的文件。文件名写法与 country_key 不一致时查常量表。"""
    if not country_key:
        return None
    root = Path(ref_dir) / HEALTH_DIRNAME
    for name in (HEALTH_FILENAME_BY_COUNTRY.get(country_key), f"{country_key}.md"):
        if not name:
            continue
        path = root / name
        if path.is_file():
            return path
    return None


def _health_block(ref_dir: Path, country_key: str, city_key: str | None, alias_path: Path | None,
                  limit: int) -> str:
    """该国的 CDC + FORTH 健康原文块(逐字), 按 `limit` 字截断。"""
    text = _read(_health_file(ref_dir, country_key))
    if not text:
        return ""
    table = place_aliases.load_table(alias_path)
    entry = next(
        (c for c in table["countries"] if isinstance(c, dict) and str(c.get("key")) == country_key),
        None,
    )
    cities = entry.get("cities") if entry and isinstance(entry.get("cities"), dict) else {}
    city = cities.get(city_key) if city_key and cities else None
    city_aliases = (
        [t for lang in ("zh", "ja", "en") for t in place_aliases._terms(city.get(lang))] if isinstance(city, dict) else []
    )
    aliases = city_aliases or [t for lang in ("zh", "ja", "en") for t in place_aliases._terms((entry or {}).get(lang))]

    blocks = _blocks(text)
    picked: list[tuple[int, str]] = []
    used = 0
    for index, block in enumerate(blocks):
        title = block["title"]
        if not any(k.lower() in title.lower() for k in _HEALTH_SECTION_KEYWORDS):
            continue
        body = _prose(block["body"])
        if not body or _HEALTH_PLACEHOLDER in body:
            continue
        paragraphs = _order_paragraphs(_paragraphs(body), aliases)
        if not paragraphs:
            continue
        # `limit` 是整块上限(CDC Notices + FORTH 各节一起算), 不是每节各给一份。
        share = max(200, limit - used)
        chunk = _render_section_limited(
            title, paragraphs, _meta_with_ancestors(blocks, index), share,
            note=f"全文见 {HEALTH_DIRNAME}/",
        )
        if chunk:
            # Notices(当期旅行健康通知)排最前, 其余按节内是否提到本城排序后取。
            rank = 0 if "notices" in title.lower() else 1
            picked.append((rank, chunk))
            used += len(chunk) + 2
    if not picked:
        return ""
    name = _health_file(ref_dir, country_key).name
    scope = f"{country_key}" + (f" / {city_key}" if city_key else "")
    header = f"## 健康风险原文（CDC Travelers' Health + 厚生労働省検疫所 FORTH · 来源: {HEALTH_DIRNAME}/{name}, {scope}）"
    return f"{header}\n\n" + "\n\n".join(chunk for _, chunk in sorted(picked, key=lambda p: p[0]))


# --------------------------------------------------------------------------- #
# 美国国务院 Travel Advisory（ref/advisory_us/us_state_advisories.md）
# --------------------------------------------------------------------------- #

#: 该国节里「等级原文 / pubDate / link」三行的标签(逐字摘录里的写法, 全角冒号)。
_US_ADVISORY_LABELS = ("advisory 等级原文", "pubDate", "link")
#: 正文里地区限制句的标志 —— 「去这个国家的某个具体地区要不要额外小心」才是这节的价值。
_US_ADVISORY_REGION_HINTS = (
    "level 2",
    "level 3",
    "level 4",
    "exercise increased caution",
    "do not travel",
    "avoid",
    "reconsider",
    "specific areas",
    "increased risk",
    "border",
    "province",
    "region",
    "district",
    "county",
)


def _advisory_us_sections(text: str) -> list[tuple[str, str]]:
    """`## <国家/地区>` 切节 → `[(节标题, 节正文)]`。"""
    out: list[tuple[str, str]] = []
    current_title: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(?!\#)(.+?)\s*$", line)
        if m:
            if current_title is not None:
                out.append((current_title, "\n".join(buf).strip()))
            current_title, buf = m.group(1), []
        elif current_title is not None:
            buf.append(line)
    if current_title is not None:
        out.append((current_title, "\n".join(buf).strip()))
    return out


def _advisory_us_block(ref_dir: Path, country_key: str, city_key: str | None, alias_path: Path | None,
                       limit: int) -> str:
    """美国国务院该国节的等级原文与地区限制句(逐字)。"""
    text = _read(Path(ref_dir) / ADVISORY_US_DIRNAME / US_ADVISORY_FILENAME)
    if not text:
        return ""
    table = place_aliases.load_table(alias_path)
    entry = next(
        (c for c in table["countries"] if isinstance(c, dict) and str(c.get("key")) == country_key),
        None,
    )
    if not entry:
        return ""
    aliases = [t for lang in ("zh", "ja", "en") for t in place_aliases._terms(entry.get(lang))]

    for title, body in _advisory_us_sections(text):
        if not _cell_hit(title, aliases):
            continue
        lines = [ln.rstrip() for ln in body.splitlines()]
        provenance: list[str] = []
        rest: list[str] = []
        in_body = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("正文"):
                in_body = True
                continue
            if in_body:
                if stripped:
                    rest.append(stripped)
                continue
            payload = stripped.lstrip("-・* ").strip()
            label = re.split(r"[：:]", payload, maxsplit=1)[0].strip()
            if label in _US_ADVISORY_LABELS and re.search(r"[：:]", payload):
                provenance.append(payload)
        if not rest:
            continue
        # 地区限制句排前面: 这个节最有价值的是「哪一带要额外小心」, 不是通用的准备事项。
        ordered = sorted(
            rest, key=lambda line: 0 if any(h in line.lower() for h in _US_ADVISORY_REGION_HINTS) else 1
        )
        header = f"### 美国国务院 Travel Advisory（{title}）"
        if provenance:
            header += "\n" + "\n".join(f"- {p}" for p in provenance)
        header += f"\n- 来源: {ADVISORY_US_DIRNAME}/{US_ADVISORY_FILENAME}（官方 RSS TAsTWs.xml 逐字摘录）"
        dates = _iso_dates(" ".join(provenance))
        if dates:
            header += f"\n- 摘录日期(ISO): {dates[0]}"
        note = f"全文见 {ADVISORY_US_DIRNAME}/{US_ADVISORY_FILENAME}"
        return f"{header}\n\n原文:\n\n{_fit(ordered, limit, note=note)}"
    return ""


# --------------------------------------------------------------------------- #
# 气候（ref/climate/ · travel_lines.md / sea_enso_impacts.md / enso_status.md）
# --------------------------------------------------------------------------- #

def _travel_lines_block(ref_dir: Path, country_key: str, alias_path: Path | None, limit: int) -> str:
    """`travel_lines.md` 里该国的行(该文件是「国家|地区|原文|URL|日期」五列表)。"""
    text = _read(Path(ref_dir) / CLIMATE_DIRNAME / TRAVEL_LINES_FILENAME)
    if not text:
        return ""
    table = place_aliases.load_table(alias_path)
    entry = next(
        (c for c in table["countries"] if isinstance(c, dict) and str(c.get("key")) == country_key),
        None,
    )
    if not entry:
        return ""
    aliases = [t for lang in ("zh", "ja", "en") for t in place_aliases._terms(entry.get(lang))]
    lines = [
        f"## 厄尔尼诺/气候对旅行的官方原文行（来源: {CLIMATE_DIRNAME}/{TRAVEL_LINES_FILENAME}, {country_key}）"
    ]
    for line in text.splitlines():
        cells = _row_cells(line)
        if not cells or not _cell_hit(cells[0], aliases):
            continue
        country_cell, region, quote, url, date = cells[0], cells[1], cells[2], cells[3], cells[4]
        lines.append(f"- [{country_cell}／{region}] {quote} (来源 URL: {url} ｜ 日期: {date})")
    if len(lines) <= 1:
        return ""
    head, rows = lines[0], lines[1:]
    body = _fit(rows, limit)
    return f"{head}\n\n{body}" if body else ""


def _sea_enso_block(ref_dir: Path, country_key: str, alias_path: Path | None, limit: int) -> str:
    """`sea_enso_impacts.md` 里该国的节(按 `## N. <机构名>` 切, 标题含国名)。"""
    text = _read(Path(ref_dir) / CLIMATE_DIRNAME / SEA_ENSO_FILENAME)
    if not text:
        return ""
    table = place_aliases.load_table(alias_path)
    entry = next(
        (c for c in table["countries"] if isinstance(c, dict) and str(c.get("key")) == country_key),
        None,
    )
    if not entry:
        return ""
    aliases = [t for lang in ("zh", "ja", "en") for t in place_aliases._terms(entry.get(lang))]
    blocks = _blocks(text)
    picked: list[tuple[int, str]] = []
    used = 0
    for index, block in enumerate(blocks):
        if block["level"] != 2 or not _cell_hit(block["title"], aliases):
            continue
        sub_meta = _meta_with_ancestors(blocks, index)
        subs = _subsections(blocks, index, ())
        for sub in subs:
            body = _prose(blocks[sub]["body"])
            paragraphs = _paragraphs(body)
            if not paragraphs:
                continue
            meta = dict(sub_meta)
            own = _meta_of(blocks[sub])
            for key in ("url", "updated", "fetched", "published"):
                if own.get(key):
                    meta[key] = own[key]
            # `limit` 是整块上限(该国所有机构节一起算), 不是每节各给一份。
            share = max(200, limit - used)
            chunk = _render_section_limited(
                blocks[sub]["title"], paragraphs, meta, share,
                note=f"全文见 {CLIMATE_DIRNAME}/{SEA_ENSO_FILENAME}",
            )
            if chunk:
                picked.append((0, chunk))
                used += len(chunk) + 2
    if not picked:
        return ""
    header = f"## 本次厄尔尼诺对该国的影响（东南亚/亚洲官方机构原文 · 来源: {CLIMATE_DIRNAME}/{SEA_ENSO_FILENAME}, {country_key}）"
    return f"{header}\n\n" + "\n\n".join(chunk for _, chunk in picked)


def _enso_status_block(ref_dir: Path, limit: int) -> str:
    """`enso_status.md` 的 NOAA 与 JMA 两节要点(所有国家都附 —— 当期 ENSO 状态是全球背景)。"""
    text = _read(Path(ref_dir) / CLIMATE_DIRNAME / ENSO_STATUS_FILENAME)
    if not text:
        return ""
    blocks = _blocks(text)
    picked: list[str] = []
    used = 0
    for index, block in enumerate(blocks):
        if block["level"] != 2:
            continue
        title = block["title"]
        if "NOAA" not in title and "気象庁" not in title:
            continue
        meta = _meta_of(block)
        for sub in _subsections(blocks, index, ()):
            sub_title = blocks[sub]["title"]
            # 只要 Synopsis/状态/要点, 不要 NOAA 的 Discussion 长文 —— 那是气候学讨论不是旅行决策。
            if not any(k.lower() in sub_title.lower() for k in ("synopsis", "status", "主文")):
                continue
            paragraphs = _paragraphs(_prose(blocks[sub]["body"]))
            if not paragraphs:
                continue
            # `limit` 是**整块**的上限(NOAA + JMA 一起算), 不是每节各给一份 ——
            # 否则「所有国家都附」的这块会比别的块多占一份预算。
            share = max(200, limit - used)
            chunk = _render_section_limited(
                f"{title} — {sub_title}", paragraphs, meta, share,
                note=f"全文见 {CLIMATE_DIRNAME}/{ENSO_STATUS_FILENAME}",
            )
            if chunk:
                picked.append(chunk)
                used += len(chunk) + 2
    if not picked:
        return ""
    header = f"## 当期 ENSO 状态（所有目的地共通 · 来源: {CLIMATE_DIRNAME}/{ENSO_STATUS_FILENAME}）"
    return f"{header}\n\n" + "\n\n".join(picked)


# --------------------------------------------------------------------------- #
# hotspots
# --------------------------------------------------------------------------- #

def _hotspot_block(ref_dir: Path, country_key: str | None, city_key: str | None, alias_path: Path | None) -> str:
    city = place_aliases.hotspot_city(country_key, city_key, path=alias_path)
    if not city:
        return ""
    path = Path(ref_dir) / HOTSPOTS_DIRNAME / f"hotspots_{city}.json"
    try:
        data = json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return ""
    # 10-05 审计 D26: 合法 JSON 但不是 object（手编坏/生成器 bug 的 []/null/3）时,
    # data.get 会抛 AttributeError 且不在 except 元组里 —— 与缺文件同按模块
    # docstring 的「一律返回空串」承诺退化, 不让 load_reference 整体中断。
    if not isinstance(data, dict):
        return ""
    rows = data.get("hotspots")
    if not isinstance(rows, list) or not rows:
        return ""
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    lines = [
        f"### 辖区×时段犯罪统计（{data.get('city_name_zh') or city} / {data.get('city_name_en') or city}）",
    ]
    provenance = []
    if source.get("publisher"):
        provenance.append(f"发布机构: {source['publisher']}")
    if source.get("portal"):
        provenance.append(f"来源 URL: {source['portal']}")
    if window.get("start") and window.get("end"):
        provenance.append(f"统计窗口: {window['start']} ~ {window['end']}（{window.get('days', '?')} 天）")
    if data.get("generated_at"):
        provenance.append(f"抓取时间: {data['generated_at']}")
    if window.get("staleness_days") is not None:
        provenance.append(f"数据滞后: {window['staleness_days']} 天（窗口右端非运行当天）")
    lines += [f"- {p}" for p in provenance]
    lines.append(f"按 {source.get('group_key', '?')} 分组取前 {HOTSPOT_LIMIT} 组:")
    for row in rows[:HOTSPOT_LIMIT]:
        if not isinstance(row, dict):
            continue
        place = " ".join(
            str(v).strip()
            for v in (row.get("precinct_name"), row.get("boro"), row.get("district_code"), row.get("precinct"))
            if v not in (None, "")
        )
        top = "、".join(
            f"{o.get('offense_zh') or o.get('offense') or '?'} {o.get('count')}"
            for o in (row.get("top3_offenses") or [])[:2]
            if isinstance(o, dict)
        )
        # share_of_window 是生成文件的自由字段(无 schema 校验), 坏值
        # (「abc」/「12%」/{}/…) 会让 float() 抛 ValueError/TypeError, 且不在上面
        # D26 的 except 元组里, 会穿过 load_reference 直达调用方(无 try) —— 与 D26
        # 同语义: 按 0% 退化, 不让整版块构建中断。
        try:
            share_pct = round(float(row.get("share_of_window") or 0) * 100, 2)
        except (TypeError, ValueError):
            share_pct = 0.0
        lines.append(
            f"- {place or '(辖区名未收录)'}／{row.get('hour_band_zh') or row.get('hour_band') or '?'}: "
            f"{row.get('count')} 件（{share_pct}%）"
            + (f"；主要: {top}" if top else "")
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def load_reference(
    country_key: str | None,
    city_key: str | None = None,
    *,
    ref_dir: Path | str | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    alias_path: Path | None = None,
    health_max_chars: int = HEALTH_MAX_CHARS,
    advisory_us_max_chars: int = ADVISORY_US_MAX_CHARS,
    climate_max_chars: int = CLIMATE_MAX_CHARS,
    enso_status_max_chars: int = ENSO_STATUS_MAX_CHARS,
) -> str:
    """目的地(country_key/city_key)的官方参考块; 目录或文件不存在返回 ""。

    `country_key`/`city_key` 用 `place_aliases` 的 key(不是用户填的写法), 调用方先
    `place_aliases.expand()` 解析。`max_chars` 缺省 9000 字, 超长时按优先级截断:
    高优先级的块分到更多字数, 每块内部再按各自的上限参数(`health_max_chars` 等)截断 —
    而不是「整块丢掉」, 否则同优先级的块会随顺序被随机扔掉一整节(FCDO 那种)。

    优先级(数字小的先保底): 街区时段行 > 美国国务院 > 健康通知 > 气候 > 官方国家文件节
    > hotspots。`enso_status`(当期 ENSO 状态)对所有国家都附, 与气候同级。
    """
    root = Path(ref_dir) if ref_dir is not None else default_ref_dir()
    if not root.is_dir():
        return ""
    if not country_key:
        return ""

    # 别名表里没有这个国家 → 没有任何按国资料(官方/健康/国务院/气候都按国切),
    # 只有「所有国家都附」的 ENSO 状态在, 那不该让一个查无此国的 key 凭空出块。
    known = place_aliases._entry_for(place_aliases.load_table(alias_path), country_key) is not None

    blocks: list[tuple[int, str]] = []
    district = _district_lines_block(root, country_key, city_key, alias_path)
    if district:
        blocks.append((PRIORITY_DISTRICT, district))
    advisory = _advisory_us_block(root, country_key, city_key, alias_path, advisory_us_max_chars)
    if advisory:
        blocks.append((PRIORITY_ADVISORY_US, advisory))
    health = _health_block(root, country_key, city_key, alias_path, health_max_chars)
    if health:
        blocks.append((PRIORITY_HEALTH, health))
    travel = _travel_lines_block(root, country_key, alias_path, climate_max_chars)
    if travel:
        blocks.append((PRIORITY_CLIMATE, travel))
    sea = _sea_enso_block(root, country_key, alias_path, climate_max_chars)
    if sea:
        blocks.append((PRIORITY_CLIMATE, sea))
    enso = _enso_status_block(root, enso_status_max_chars) if known else ""
    if enso:
        blocks.append((PRIORITY_CLIMATE, enso))
    blocks.extend(_official_sections(root, country_key, city_key, alias_path))
    hotspot = _hotspot_block(root, country_key, city_key, alias_path)
    if hotspot:
        blocks.append((PRIORITY_HOTSPOT, hotspot))
    if not blocks:
        return ""

    header = [
        f"# 官方参考资料摘录（{country_key}" + (f" / {city_key}" if city_key else "") + "）",
        "以下均为官方页面原文摘录与官方统计，逐字未改写。引用时须注明机构与日期；"
        "日期明显较旧（超过一年）的结论要向用户说明属旧信息。",
    ]
    head = "\n".join(header)
    if not max_chars or len(head) >= max_chars:
        if not max_chars:
            return head
        # 10-05 审计 D25: head 自己就超总上限时同样要带截断标注, 不能无标注裸切。
        for note in ("…（参考块总长上限过小, 已截断）", "…（截断）"):
            keep = max_chars - len(note) - 1
            if keep > 0:
                return head[:keep] + "\n" + note
        return head[:max_chars]
    ordered = sorted(blocks, key=lambda b: b[0])
    budget = _allocate(ordered, max_chars - len(head) - 2)
    body = "\n\n".join(_fit_text(text, budget.get(id(text), 0)) for _, text in ordered)
    return f"{head}\n\n{body}"


def load_home_reference(
    *,
    ref_dir: Path | str | None = None,
    max_chars: int = HOME_MAX_CHARS,
) -> str:
    """常驻地的官方参考块; `ref/home/` 不存在返回 ""。

    读 `home/action_lines.md`(气象厅季节展望/厄尔尼诺)与 `home/district_action_lines.md`
    (所在区防灾资料)两张「节|原文|URL|日期」表 —— 两份都是**逐字原文行**, 每行自带
    实际请求过的 URL 与日期, 所以这里只做按行筛选与截断, 不改一个字。

    **暂不接入出版流程**: 只有函数与测试, `paper_risk.py` 不调用它。
    """
    root = Path(ref_dir) if ref_dir is not None else default_ref_dir()
    if not root.is_dir():
        return ""
    home = Path(root) / HOME_DIRNAME
    if not home.is_dir():
        return ""

    head = (
        "# 常驻地官方参考资料摘录\n"
        "以下为气象厅季节展望与所在区防灾资料的逐字原文行，附实际请求过的 URL 与日期。"
        "引用时须注明机构与日期；未取得的项目原文行里会写明「未取得」，不要据此推断不存在该风险。"
    )
    if not max_chars or len(head) >= max_chars:
        if not max_chars:
            return head
        # 10-05 审计 D25: 同 load_reference —— head 超上限也要带截断标注。
        for note in ("…（参考块总长上限过小, 已截断）", "…（截断）"):
            keep = max_chars - len(note) - 1
            if keep > 0:
                return head[:keep] + "\n" + note
        return head[:max_chars]

    # 先给头留出字数, 余下按文件数均分 —— 两份资料都要露头, 不能让先来的吃掉全部预算。
    budget = max_chars - len(head) - 2
    share = max(400, budget // len(HOME_ACTION_FILES))
    chunks: list[str] = []
    for name in HOME_ACTION_FILES:
        text = _read(home / name)
        if not text:
            continue
        rows: list[str] = []
        for line in text.splitlines():
            cells = _row_cells4(line)
            if not cells:
                continue
            section, quote, url, date = cells
            if not quote:
                continue
            where = f"[{section}] " if section and section != "-" else ""
            rows.append(f"- {where}{quote} (来源 URL: {url} ｜ 日期: {date})")
        if not rows:
            continue
        title = f"### 常驻地原文行（来源: {HOME_DIRNAME}/{name}）"
        body = _fit(rows, max(200, share - len(title) - 2), note=f"全文见 {HOME_DIRNAME}/{name}")
        chunks.append(f"{title}\n\n{body}")
    if not chunks:
        return head
    return f"{head}\n\n" + "\n\n".join(chunks)


#: 优先级 → 权重。数字越小越优先, 权重越大分到越多字数。
#: 街区时段行 > 美国国务院 > 健康通知 > 气候 > 官方国家文件节 > hotspots。
_WEIGHTS = {1: 6.0, 2: 4.5, 3: 4.0, 4: 3.0, 5: 2.5, 6: 2.5, 7: 3.0}


def _allocate(blocks: list[tuple[int, str]], budget: int) -> dict[int, int]:
    """按优先级权重把总字数分给各块(按块 id 索引返回上限)。"""
    total_weight = sum(_WEIGHTS.get(priority, 1.0) for priority, _ in blocks) or 1.0
    out: dict[int, int] = {}
    assigned = 0
    for position, (priority, text) in enumerate(blocks):
        share = int(budget * (_WEIGHTS.get(priority, 1.0) / total_weight))
        if position == len(blocks) - 1:
            share = budget - assigned
        share = max(share, 0)
        out[id(text)] = min(share, len(text))
        assigned += share
    return out


def _fit_text(text: str, limit: int) -> str:
    """按行把块截到 limit 字, 放不下时标注省略(不丢来源与日期头)。"""
    if limit <= 0 or len(text) <= limit:
        return text
    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        if used + len(line) + 1 > limit:
            break
        kept.append(line)
        used += len(line) + 1
    if not kept:
        # 10-05 审计 D25: 首行自己就超预算时的兜底也必须带截断标注(与下面正常
        # 路径同款), 不能无标注断半句。预算小到标注都放不下时才裸切。
        for note in ("…（本块超出总长上限, 已截断）", "…（截断）"):
            keep = limit - len(note) - 1
            if keep > 0:
                return text[:keep] + "\n" + note
        return text[:limit]
    note = "…（本块超出总长上限, 已截断）"
    if used + len(note) + 1 > limit:
        while kept and used + len(note) + 1 > limit:
            used -= len(kept.pop()) + 1
    kept.append(note)
    return "\n".join(kept)
