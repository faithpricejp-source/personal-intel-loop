"""home_alerts —— 常驻地(东京)的**健康与天候**预警, 只在有实质变化时出条目。

定位: 报纸风险提示栏(`docs/paper_v2_contract.md` 第 9 节)要覆盖读者常驻地(当前实现只支持东京都的数据源)。
`adapters/disaster_alerts.py` 已经盯东京的**气象警报**(雷/雨/风), 但**气候型**异常
(5 天平均气温「かなり高い」/「かなり低い」、5 天降雪量「かなり多い」)和**传染病流行**
这两块它一条都不管 —— 本 adapter 补的就是这两块。

## 为什么低召回是目标(不是缺陷)

设计规格原话:「只在**有实质变化时**出条目(低召回是目标: 一年几次才健康, 天天报人就麻木了)」。
所以本 adapter 的出条条件是**状态跃迁**, 不是「有数据就报」:

- **IDSC 感染症周报**: 只在 ①某病首次发布/解除「流行注意報」「流行警報」;
  或 ②定点报告数较上周增长 ≥50% **且** 超过该病注意報基准的一半时出条。
  每周的定点报告数**本身不出条**(那是背景噪音, 天天报必然麻木)。
- **JMA 早期天候情報(関東甲信)**: 只在**该地方有発表**时出条(原文照抄)。

## 子源一: 东京都感染症周报(`home_alerts:tokyo_idsc`)

### 入口(2026-10-05 实测, 冻结范围内逐个试过)

| URL | 结果 |
|---|---|
| `https://idsc.tmiph.metro.tokyo.lg.jp/weekly/` | 200, 周报索引页。**周报正文只有 PDF**(`/assets/weekly/2026/NN.pdf`), 页面自己写着「このページでは、全文についてPDFによる情報提供を行っております」 |
| `https://idsc.tmiph.metro.tokyo.lg.jp/assets/weekly/2026/39.pdf` | 200, 1.37MB, 最新一期(2026 第 39 週) |
| `https://idsc.tmiph.metro.tokyo.lg.jp/survey/` | 200, **注意報/警報基准值表**(HTML `<table>`, 机构级保健所口径) |
| `https://survey.tmiph.metro.tokyo.lg.jp/epidinfo/...` | **不在冻结范围内**(另一个 host), 没用 |

**没有 HTML 表也没有 CSV** —— 实测把 `/weekly/` `/diseases/*` `/survey/*` `/epid/` `/new/`
`/archive/` 都翻了一遍: 疾病页上的定点数是 `<img>` 图表(数据在 `survey.tmiph.metro.tokyo.lg.jp`,
出了冻结范围), 定点情报页(`/survey/kobetsu/teitenjoho/`)全是 PDF 链接。
所以**唯一机器可读入口就是 PDF 本身**, 用 `pdftotext -layout` 抽文本(见下)。

### PDF 抽取(踩过的坑)

- PDF 字体是 **CID 字体且没有 ToUnicode CMap**(实测 `ToUnicode` 出现 0 次)——
  纯 Python 抽文本会得到乱码, 但 **`pdftotext -layout` 正常**(实测日文原文完整, 表头对齐)。
  所以走**外部二进制**, 并且**探测不到 `pdftotext` 就返回空**(不抛异常, 不静默给错数据)。
- `-layout` 必须加: 定点表是「上段=報告数 / 下段=定点当たり」双行成对出现,
  不加 `-layout` 两行会串行, 定点值全错。

### 定点表的行结构(实测三期一致)

```
定点種別  対象疾患     36週    37週   38週   39週   報告医療  定点医療
                313     393    375    225
  ＲＳウイルス感染症                 <- 病名行(有时这行前面带 4 个整数=報告数)
                 1.20    1.49   1.43  0.86   <- **定点当たり行**: 4 个数且至少一个带小数点
  咽頭結膜熱              48     39     41     17
                 0.18    0.15   0.16  0.06
```

**锚点规则(实测 w30/w36/w39 三期都成立)**: 病名行本身不带定点值, **紧邻的下一行**才是定点当たり行。
所以解析是「找病名行 → 取下一行 → 该行必须恰好 4 个数字且含小数点」。
- 为什么要求「恰好 4 个」: 基幹定点的 `感染性胃腸炎（ロタウイルス）` 那种行下面跟的是 `*4` 之类,
  不是 4 列; 而眼科/基幹定点的散行只有 1~2 个数。宁可漏, 不猜。
- 周次从表头行 `_WEEK_HEADER_RE`(四个 `N週` 连续列)取, 最后一列 = 本周。
- **脚注号注意**: `インフルエンザ *2` / `マイコプラズマ肺炎` 后面可能带 `*N` 脚注号, 要剥掉。
- `新型コロナウイルス感染症（COVID-19）` 在表里被拆成三行(`急性` / `呼吸器 新型…` / `感染症`),
  病名行本身完整包含 `新型コロナウイルス感染症（COVID-19）`, 直接子串匹配即可。

### 注意報/警報状态: **不在定点表里, 在表下面的散文段**

定点表里的注意報/警報是**用颜色标的**(实测图例: `黒太字表記：注意報レベル` /
`赤太字表記：警報レベル`), 而颜色在 `pdftotext` 里**完全丢失**(实测解 content stream 也拿不到,
CID 字体下 159 个 stream 里找不到颜色算子与数值的可靠关联)。所以**不能**从表里读状态。

真正能读的是表下面那段散文 —— 实测三期原文:

- w39: 「インフルエンザの定点当たり報告数は9.51です。31保健所中1保健所が警報レベル、
  9保健所が注意報レベルであり、保健所管内人口の合計が東京都全体の**31.28％**に達しているため、
  **注意報レベルが続いています**。」
- w30: 「手足口病の定点当たり報告数は8.10と減少してきていますが、**引き続き警報レベル**です。」
- w36: 三条全是「増加傾向です」—— **一个注意報/警報词都没有**, 即「本周无任何警报状态」。

所以状态判定 = 在散文段里按病名找句子 → 句中找 `警報レベル` / `注意報レベル` / `解除` /
`発令` 等词。这是**从原文抄**, 不是推断。

### 阈值(注意報基准) —— 官方页面原文, 附 URL

`https://idsc.tmiph.metro.tokyo.lg.jp/survey/` 的「【参考】注意報・警報の基準値」表
(资料出处页面自己标了: 厚生労働科学研究「効果的な感染症サーベイランスの評価ならびに改良に関する研究」)。
实测抽出来的「警報レベル 開始/終息 + 注意報レベル 開始基准值」:

| 疾病 | 警報開始 | 警報終息 | 注意報開始 |
|---|---|---|---|
| インフルエンザ | 30 | 10 | **10** |
| 咽頭結膜熱 | 3 | 1 | - |
| A群溶血性レンサ球菌咽頭炎 | 8 | 4 | - |
| 感染性胃腸炎 | 20 | 12 | - |
| 水痘 | 2 | 1 | 1 |
| 手足口病 | 5 | 2 | - |
| 伝染性紅斑 | 2 | 1 | - |
| ヘルパンギーナ | 6 | 2 | - |
| 流行性耳下腺炎 | 6 | 2 | 3 |
| 急性出血性結膜炎 | 1 | 0.1 | - |
| 流行性角結膜炎 | 8 | 4 | - |

表里 `-` = 「基準値が特に定められていない」。**阈值从页面现读现用**(HTML 表比 PDF 好解析),
不写死 —— 官方改基准不用改代码。**新型コロナ と RSウイルス不在该表里**(表只有 11 个病),
它们没有注意報基准 → `threshold_for()` 返回 `None` → 增长规则对这两个病**不生效**(见下)。

### 出条规则

规则①(状态跃迁): 某病状态与 state 里上一期不同 → 出条。首次运行 state 为空时,
**只有「有警报状态」才算变化**(无 → 无 不出条, 否则第一次跑就刷一屏历史)。
规则②(增长): 本周定点值 ≥ 上周 ×1.5 **且** > 注意報基准的一半。
- 增长比较用**周报表内倒数第二列**(上周)与**最后一列**(本周), 不用 state —— 周报自带 4 周窗口,
  跨进程也准。
- `prev == 0` 时增长倍数无定义 → 不出条(`float('inf')` 的坑)。
- 没有注意報基准的病(新型コロナ/RS)规则②天然不适用, 因为「超过基准的一半」无从判断。

两条都满足时**合并成一条**(同一病同一周只出一条, level 取更高的那个)。

## 子源二: 気象庁 早期天候情報(`home_alerts:jma_souten`)

### 入口(2026-10-05 实测)

页面 `https://www.data.jma.go.jp/cpd/souten/` **自己没有数据接口**, 数据在 `top/js/bundle.js`
里(实测把 bundle 拉下来反查出来的, 见 `data/` 字面量):

- `GET /cpd/souten/data/flg.json` → `{"snow":-9, "temp":0}` —— **全国**标志位。
  实测 bundle 逻辑: `1===n[e]&&t()` 即值为 1 才启用该元素 tab; `snow == -9` 直接隐藏 snow tab。
  所以 **`flg.json` 是「有没有 anywhere 的発表」, 不是「関東甲信有没有」** —— 只能当快速否决。
- `GET /cpd/souten/data/<reg_no>.json` → 该地方的数据。`<reg_no>` 从页面 usemap 的
  `<area value=...>` 取, 实测 関東甲信地方 = **20**; bundle 里的 `publishOfficeData`
  给出 `20:"気象庁"`(関東甲信由気象庁本厅发布, 不是分区台)。
- `reg_no` 白名单(实测 bundle `u()` 函数): `[11,15,20,21,22,23,26,29,30,31,34]`, 不在表里归 0(全国)。

### 形状(实测)

未发表时(2026-10-05 实测, 関東甲信):
```json
[ { "reportDate_W": "令和8年10月1日" } ]
```
—— **只有一个日期, 没有 `type`/`text`**。bundle 的 `l()` 把它渲染成
「<日期>の\n関東甲信地方の早期天候情報の発表はありません。」

发表时(据 bundle 读取字段反推, 见下「没把握」): 同一数组里多出元素, 带
`type`("本文" 与 具体天气类型如 `高温`/`低温`/`大雪`/`かなりの低温`)、`text`(正文)、
`title`、`publishOffice`、`reportDate_W`、`reportTime_W`、`sdate`/`thres`/`targetDuration`
(阈值的结构化字段)、`reg_ch_text`。

判定「有没有发表」= 数组里**有没有 `type == "本文"` 的元素**(bundle 就是这么 grep 的:
`i.grep(e, function(e){return "本文"===e.type})`)。这个判据是实测的, 不猜。

### 和 `disaster_alerts` 的分工

`disaster_alerts` 抓的是 `extra.xml` 的**即时警報/注意報**(雷/雨/风, 数值预警)。
本子源抓的是**气候型** outlook(未来 6~14 天 5 天平均气温/降雪量异常概率),
两者时间尺度不同、内容不重叠, 不重复。

## 状态(state)

单文件 `home_alerts_state.json`, 两个子源各占一节:

```json
{"idsc": {"alerts": {"インフルエンザ": "注意報"}, "week": "2026-39"},
 "jma_souten": {"last_key": "令和8年10月1日高温", "published": "..."}}
```

`idsc.alerts` 存「上一期各病的状态」用于比较(规则①)。
`jma_souten.last_key` 存「上次已出的情报标识」用于同一份情报不重复出条
(早期天候情報一周最多两条: 炎暑/低温 和 大雪, 标识 = `発表日+类型`)。

## 出网纪律

冻结范围: 只访问 `idsc.tmiph.metro.tokyo.lg.jp` 与 `www.data.jma.go.jp`。
每站 ≤25 次、间隔 ≥2 秒 —— IDSC 侧每轮**最多 2 次请求**(周报索引 + 最新 PDF);
JMA 侧每轮**最多 2 次**(`flg.json` 否决 + `data/20.json`)。
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlsplit

import requests

from personal_intel_loop import DATA_DIR
from personal_intel_loop.schemas import Item, compute_item_id

logger = logging.getLogger(__name__)

#: `compute_item_id` 的 source 前缀(模块级常量, 与 `HomeAlertsAdapter.name` 一致)。
NAME = "home_alerts"

ISSUER_IDSC = "東京都感染症情報センター(東京都健康安全研究センター)"
ISSUER_JMA = "気象庁"

IDSC_BASE = "https://idsc.tmiph.metro.tokyo.lg.jp"
IDSC_WEEKLY_INDEX = f"{IDSC_BASE}/weekly/"
IDSC_SURVEY = f"{IDSC_BASE}/survey/"

JMA_BASE = "https://www.data.jma.go.jp/cpd/souten"
#: 関東甲信地方。usemap 的 `<area value>` 与 bundle 的 `publishOfficeData` 都是这个号
#: (20: "関東甲信地方" / 20: "気象庁")。
SOUTEN_REG_NO = "20"
SOUTEN_REG_NAME = "関東甲信地方"
SOUTEN_FLG = f"{JMA_BASE}/data/flg.json"
SOUTEN_DATA = f"{JMA_BASE}/data/{SOUTEN_REG_NO}.json"

DEFAULT_STATE_PATH = DATA_DIR / "cache" / "home_alerts_state.json"

REQUEST_TIMEOUT = 30
BODY_LIMIT = 20_000
JST = timezone(timedelta(hours=9))

#: 增长规则: 本周 ≥ 上周 × 此值 且 超过注意報基准的一半。
GROWTH_RATIO = 1.5

#: 跟踪的病(定点表里出现的名字 → 稳定 key)。key 用来做 state 的键, 不能用日文原名
#: (官方表里是全角 `ＲＳウイルス感染症`, 散文段里写法还不一样)。
TRACKED = {
    "インフルエンザ": "インフルエンザ",
    "新型コロナ": "新型コロナウイルス感染症（COVID-19）",
    "rs": "ＲＳウイルス感染症",
    "胃腸炎": "感染性胃腸炎",
    "手足口病": "手足口病",
}

#: 早期天候情報の天气类型(实测 bundle 里 `rankStr`/比较用的字面量)。
SOUTEN_TYPES = ("高温", "低温", "大雪", "かなりの低温")

#: 早期天候情報天气类型 → 中文(标题里用)。未列出的类型原样透传, 不硬猜。
_SOUTEN_CN = {
    "高温": "高温",
    "低温": "低温",
    "大雪": "大雪",
    "かなりの低温": "显著低温",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t　]+")
#: 周报 PDF 里定点表头: `36週         37週           38週        39週`
_WEEK_HEADER_RE = re.compile(r"(\d+)週\s+(\d+)週\s+(\d+)週\s+(\d+)週")
#: 定点あり行: 恰好 4 个数, 至少一个带小数点(`1.20 1.49 1.43 0.86`)。
#: 要求「恰好 4 个」是为了挡掉基幹/眼科那些 1~2 个数的散行。
_RATE_LINE_RE = re.compile(
    r"^\s*([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)\s*$"
)
#: 周报标题: `東京都感染症週報\n     2026年第39週` / 表头 `定点把握対象疾患 報告数 2026年39週`
_TITLE_WEEK_RE = re.compile(r"(\d{4})\s*年第\s*(\d+)\s*週")
_TABLE_WEEK_RE = re.compile(r"(\d{4})年\s*(\d+)\s*週")
#: 集計時刻: `2026/9/30 11:00集計`
_AGG_RE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\s*集計")
#: 和暦発表日: `令和8年10月1日`
_ERA_DATE_RE = re.compile(r"(令和|平成|昭和)(\d+)年(\d{1,2})月(\d{1,2})日")
#: 病名后面的脚注号: `インフルエンザ *2`
_FOOTNOTE_RE = re.compile(r"\s*\*\d+\s*$")
#: 10-05 审计 E02: 解除句式。散文段用「…注意報レベルを解除しました」「…は警報レベルが
#: 解除されました」「…流行は終息しました」报告解除 —— 句子里同样含「注意報レベル」等
#: 状态词, 只按状态词判定会把解除读成活动状态(方向完全判反)。命中即视为「该病本期
#: 无在警状态」(病名不进结果, 由既有的「上期有 → 本期无」路径出解除条)。
_RELEASE_RE = re.compile(r"解除(?:され|し|となり)(?:まし)?た|終息")


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


def _clean_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = re.sub(r"<(script|style).*?</\1>", " ", raw, flags=re.S | re.I)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return text


def _norm_ws(raw: str) -> str:
    """全角空格/多余空白归一。PDF 抽出来的文本里有大量全角空格与右突空白。"""
    return _WS_RE.sub(" ", (raw or "").replace("　", " ")).strip()


def _to_float(raw: str) -> float | None:
    """`1,130` / `9.51` → float。抽不出来返回 None。"""
    try:
        return float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None


def era_date_to_iso(raw: str | None) -> datetime | None:
    """`令和8年10月1日` → aware datetime(JST)。抽不出来返回 None。"""
    if not raw:
        return None
    m = _ERA_DATE_RE.search(str(raw))
    if not m:
        return None
    base = {"令和": 2018, "平成": 1988, "昭和": 1925}.get(m.group(1))
    if base is None:
        return None
    try:
        return datetime(base + int(m.group(2)), int(m.group(3)), int(m.group(4)), tzinfo=JST)
    except ValueError:
        return None


def threshold_for(name: str, thresholds: dict[str, dict[str, float | None]]) -> float | None:
    """某病的注意報開始基准值。表里是 `-`(未规定)或压根没这个病 → None。"""
    row = thresholds.get(name)
    if not row:
        return None
    return row.get("attention")


# ---- IDSC: 周报 PDF → 文本 ------------------------------------------------

def _find_pdftotext() -> str | None:
    return shutil.which("pdftotext")


def pdf_to_text(data: bytes) -> str:
    """PDF 原始字节 → 文本。**没有 pdftotext 或失败一律返回 ""**(不抛)。

    PDF 是 CID 字体且无 ToUnicode CMap(实测), 纯 Python 抽出来是乱码, 必须走外部二进制。
    `-layout` 必须加: 定点表是「上段報告数 / 下段定点当たり」双行成对, 不加会串行。
    """
    exe = _find_pdftotext()
    if not exe or not data:
        if not exe:
            logger.warning("home_alerts: 找不到 pdftotext, 周报正文抽不了(返回空)")
        return ""
    try:
        proc = subprocess.run(
            [exe, "-layout", "-", "-"],
            input=data, capture_output=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("home_alerts: pdftotext 跑失败: %s", exc)
        return ""
    if proc.returncode != 0:
        logger.warning("home_alerts: pdftotext 返回 %d", proc.returncode)
        return ""
    # pdftotext 在有 ToUnicode 时会输出 BOM, 去掉免得污染第一行匹配
    return proc.stdout.decode("utf-8", "replace").lstrip("﻿")


def parse_thresholds(html_text: str) -> dict[str, dict[str, float | None]]:
    """`/survey/` 的「注意報・警報の基準値」表 → `{病名: {alarm, alarm_end, attention}}`。

    表结构(实测): 两行表头(警報レベル colspan=2 + 注意報レベル colspan=1),
    之后每行 `病名 / 警報開始 / 警報終息 / 注意報開始`。`-` 表示未规定 → None。
    """
    if not html_text:
        return {}
    # 10-05 审计 E03: 任何一种解析失败都不再静默 —— 返回 {} 会让增长规则②全病停摆,
    # 且采集照常「成功」, 没有日志就无从排查(流感季 50% 跳升时风险栏悄悄少条)。
    i = html_text.find("【参考】")
    if i < 0:
        logger.warning("home_alerts: 基准值表解析失败: 页面里找不到【参考】标题, 增长规则②不生效")
        return {}
    tables = re.findall(r"<table.*?</table>", html_text[i:], flags=re.S)
    if not tables:
        logger.warning("home_alerts: 基准值表解析失败: 【参考】之后没有 <table>, 增长规则②不生效")
        return {}
    out: dict[str, dict[str, float | None]] = {}
    for row in re.findall(r"<tr.*?</tr>", tables[0], flags=re.S):
        cells = [_norm_ws(html.unescape(re.sub(r"<[^>]+>", "", c)))
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.S)]
        if len(cells) != 4:
            continue
        name = cells[0]
        if not name or name in ("疾\u3000病", "疾 病", "疾病"):
            continue
        out[name] = {
            "alarm": _to_float(cells[1]),
            "alarm_end": _to_float(cells[2]),
            "attention": _to_float(cells[3]),
        }
    if not out:
        # 行结构整体对不上(表头 colspan 改版等)——同样是「全病静默失效」的一种
        logger.warning("home_alerts: 基准值表解析失败: 表里一行病名都没认出来, 增长规则②不生效")
    return out


def _week_label(text: str) -> str:
    """`東京都感染症週報 2026年第39週` → `2026-39`。"""
    m = _TITLE_WEEK_RE.search(text)
    return f"{m.group(1)}-{int(m.group(2)):02d}" if m else ""


def _sentinel_section(text: str) -> tuple[int, list[int], list[str]]:
    """定位定点表 → `(年份, 周次列表, 表体行)`。找不到返回 `(-1, [], [])`。"""
    start = -1
    year = 0
    for m in _TABLE_WEEK_RE.finditer(text):
        start = m.start()
        year = int(m.group(1))
        break
    if start < 0:
        return -1, [], []
    # 表体到「年齢階級別」或「黒太字表記」为止(实测这两个都在定点表之后)
    tail = text[start:]
    for stop in ("【年齢階級別】", "黒太字表記", "年齢階級別"):
        idx = tail.find(stop)
        if idx > 0:
            tail = tail[:idx]
            break
    lines = tail.splitlines()
    # 表头往后 14 行内找周次行(`36週  37週  38週  39週`)
    weeks: list[int] = []
    for line in lines[:14]:
        wm = _WEEK_HEADER_RE.search(line)
        if wm:
            weeks = [int(x) for x in wm.groups()]
            break
    return year, weeks, lines


def parse_sentinel(text: str) -> dict[str, Any]:
    """周报文本 → `{year, weeks, diseases: {key: {"name","rates":[...], "prev","cur"}}}`。

    行结构(实测三期一致): 病名行不带定点值, **紧邻的下一行**是定点当たり行
    (恰好 4 个数且含小数点)。定点表里最后一列=本周、倒数第二列=上周。
    """
    year, week_header, body = _sentinel_section(text)
    if year < 0:
        return {}
    # 逐行找病名; 命中就取下一行做定点值
    found: dict[str, dict[str, Any]] = {}
    for idx, line in enumerate(body):
        for key, jp in TRACKED.items():
            if key in found or jp not in line:
                continue
            for nxt in body[idx + 1: idx + 3]:
                m = _RATE_LINE_RE.match(nxt)
                if not m:
                    continue
                nums = [_to_float(x) for x in m.groups()]
                if any(n is None for n in nums) or not any("." in g for g in m.groups()):
                    break
                found[key] = {
                    "name": jp,
                    "rates": nums,
                    "prev": nums[-2],
                    "cur": nums[-1],
                }
                break
    return {
        "year": year,
        "weeks": week_header,
        "diseases": found,
        "week_label": _week_label(text),
    }


def _prose_after_table(text: str) -> str:
    """定点表之后、年龄阶级表之前那一段散文(注意報/警報 状态就在这里)。

    去掉「( 今週の注目される定点把握対象疾患 )」这个标题, 只留正文条目。
    """
    start = text.find("今週の注目される定点把握対象疾患")
    if start < 0:
        return ""
    tail = text[start:]
    # 跳过标题行本身(标题与第一个 `・` 之间可能有换行与全角括号)
    head = tail.find("・")
    if head < 0:
        return ""
    tail = tail[head:]
    for stop in ("【年齢階級別】", "年齢階級別"):
        idx = tail.find(stop)
        if idx > 0:
            tail = tail[:idx]
            break
    # 下一张表的标题与标记在同一行(实测 `定点把握対象疾患 報告数 【年齢階級別】 2026年39週`),
    # 只切到标记会留下半句标题, 所以从那一行的**行首**切。
    lines = tail.splitlines()
    while lines and ("年齢階級別" in lines[-1] or "定点把握対象疾患" in lines[-1]):
        lines.pop()
    tail = "\n".join(lines)
    # 页码行(纯数字)去掉
    return "\n".join(ln for ln in tail.splitlines() if not re.fullmatch(r"\s*\d{1,3}\s*", ln))


def parse_alert_status(text: str) -> dict[str, str]:
    """周报散文段 → `{病名key: "注意報"|"警報"}`。**没提到的病不出现在结果里**。

    状态**不在定点表里**(表用颜色标, pdftotext 把颜色丢了), 唯一能读的是这段散文:
    - w39 「…東京都全体の31.28％に達しているため、注意報レベルが続いています。」→ インフルエンザ=注意報
    - w30 「手足口病…引き続き警報レベルです。」→ 手足口病=警報
    - w36 三条都是「増加傾向です」→ 整段无警报词 → 返回 {} (= 本周无任何警报状态)

    判定: 病名 + 句中同时出现 `警報レベル` → 警報, 否则出现 `注意報レベル` → 注意報。
    两者都出现时(先说保健所级再总结都级)**取靠后那个**(总结句才是都级结论)。
    句中出现解除句式(`解除しました` / `解除されました` / `終息` 等, 见 `_RELEASE_RE`)
    时是**解除**, 该病不进结果(10-05 审计 E02 之前解除词从未实现, 解除句被读成活动状态)。
    """
    prose = _prose_after_table(text)
    if not prose:
        return {}
    # **按 `・` 切条, 不按句号切**: 一条 `・` 就是「关于某个病的一整段话」, 而状态词常在
    # 第二句里(实测 w39: 「インフルエンザの定点当たり報告数は9.51です。31保健所中1保健所が
    # 警報レベル、9保健所が注意報レベルであり、…注意報レベルが続いています。」)。
    # 按句号切会把病名和状态词拆到两段, 就找不到状态了。
    flat = re.sub(r"\s*\n\s*", "", prose)
    bullets = [b.strip() for b in flat.split("・") if b.strip()]
    out: dict[str, str] = {}
    for key, jp in TRACKED.items():
        # 段落里 COVID/RS 的写法与表内不同, 用前缀匹配
        needle = _FOOTNOTE_RE.sub("", jp)
        probes = [needle]
        if key == "新型コロナ":
            probes = ["新型コロナウイルス感染症"]
        elif key == "rs":
            probes = ["ＲＳウイルス感染症", "RSウイルス感染症"]
        for bullet in bullets:
            if not any(p in bullet for p in probes):
                continue
            # 解除句优先于状态词: 「注意報レベルを解除しました」里也有「注意報レベル」
            if _RELEASE_RE.search(bullet):
                break
            hits = [(bullet.rfind("警報レベル"), "警報"), (bullet.rfind("注意報レベル"), "注意報")]
            hits = [(pos, lv) for pos, lv in hits if pos >= 0]
            if not hits:
                continue
            # 靠后的词是总结句(都级结论), 优先取它
            out[key] = max(hits)[1]
            break
    return out


# ---- 网络(可注入)----------------------------------------------------------

#: 冻结范围的出网纪律: 每站 ≤25 次、间隔 ≥2 秒。
#: `_CALLS` 由 `collect()` 每轮开头清零 —— 上限是**每轮**的, 跨轮累计会在第二天直接锁死。
_MIN_INTERVAL_S = 2.0
_MAX_CALLS_PER_HOST = 25
_HITS: dict[str, float] = {}
_CALLS: dict[str, int] = {}


def reset_request_budget() -> None:
    """清空本轮出网计数(每轮 collect 开头调一次)。"""
    _CALLS.clear()


def _throttle(host: str) -> None:
    """同一 host 的两次请求间隔 ≥2 秒; 单轮超过 25 次就拒绝再发。

    用模块级字典记打点 —— 出网纪律是**冻结范围的一部分**, 所以约束落在真正发请求的地方,
    而不是靠调用方自觉。
    """
    n = _CALLS.get(host, 0) + 1
    _CALLS[host] = n
    if n > _MAX_CALLS_PER_HOST:
        raise RuntimeError(f"home_alerts: {host} 本轮请求已超 {_MAX_CALLS_PER_HOST} 次上限, 停止出网")
    last = _HITS.get(host)
    now = time.monotonic()
    if last is not None and now - last < _MIN_INTERVAL_S:
        time.sleep(_MIN_INTERVAL_S - (now - last))
    _HITS[host] = time.monotonic()


def _get(url: str) -> requests.Response | None:
    """带节流的 GET。异常/non-200 返回 None(调用方决定怎么处理)。"""
    host = urlsplit(url).hostname or ""
    try:
        _throttle(host)
        return requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    except requests.RequestException as exc:
        logger.warning("home_alerts: GET %s 失败: %s", url, exc)
        return None


def fetch_idsc_week_index() -> str:
    """周报索引页 HTML。用来找**最新一期 PDF 的 URL**。

    索引页把各年各周的 PDF 直链列出来(`/assets/weekly/2026/39.pdf`),
    所以定位最新一期不用猜周次。
    """
    r = _get(IDSC_WEEKLY_INDEX)
    if r is None:
        return ""
    if r.status_code != 200:
        logger.warning("home_alerts: 周报索引 non-200: %s", r.status_code)
        return ""
    return r.text


def parse_latest_pdf_url(index_html: str) -> str:
    """索引页 HTML → 最新一期周报 PDF 的绝对 URL。抽不到返回 ""。

    形如 `/assets/weekly/2026/39.pdf`; 也有 `?20260518` 这样的 cache-busting query, 保留。
    取 (年, 周) 最大的那个 —— 索引页是按年 ascend 列的, 直接取最后一个容易踩到旧年份的重复链接。
    """
    if not index_html:
        return ""
    best: tuple[int, int, str] | None = None
    for m in re.finditer(r"/assets/weekly/(\d{4})/(\d{1,2})\.pdf(\?[\w.\-]*)?", index_html):
        year, week, query = int(m.group(1)), int(m.group(2)), m.group(3) or ""
        if best is None or (year, week) > (best[0], best[1]):
            best = (year, week, f"{IDSC_BASE}/assets/weekly/{year}/{week:02d}.pdf{query}")
    return best[2] if best else ""


def fetch_idsc_pdf(url: str) -> bytes:
    if not url:
        return b""
    r = _get(url)
    if r is None:
        return b""
    if r.status_code != 200:
        logger.warning("home_alerts: 周报 PDF non-200: %s", r.status_code)
        return b""
    return r.content


def fetch_idsc_survey() -> str:
    r = _get(IDSC_SURVEY)
    if r is None:
        return ""
    if r.status_code != 200:
        logger.warning("home_alerts: 基准值页 non-200: %s", r.status_code)
        return ""
    return r.text


def fetch_souten(flg: bool = True) -> tuple[str, str]:
    """→ `(flg_text, data_text)`。

    先读 `flg.json`(全国标志位): `snow`/`temp` 都不是 1 就**直接返回**——
    实测 bundle 就是 `1===n[e]&&t()` 才启用 tab, 所以「全国都没发表」时不必再抓地方数据。
    但注意 flg 是**全国**的, 值为 1 不代表関東甲信有, 仍要看 `data/20.json` 里有没有 `本文`。
    """
    flg_text = ""
    if flg:
        r = _get(SOUTEN_FLG)
        flg_text = r.text if (r is not None and r.status_code == 200) else ""
    if flg_text:
        try:
            flags = json.loads(flg_text)
        except (ValueError, TypeError):
            flags = {}
        if isinstance(flags, dict) and flags and not any(
            isinstance(v, int) and v == 1 for v in flags.values()
        ):
            logger.info("home_alerts: 早期天候情報 flg=%s, 全国无发表, 跳过", flg_text.strip())
            return flg_text, ""
    r = _get(SOUTEN_DATA)
    if r is None:
        return flg_text, ""
    if r.status_code != 200:
        logger.warning("home_alerts: souten data non-200: %s", r.status_code)
        return flg_text, ""
    return flg_text, r.text


def parse_souten(data_text: str) -> dict[str, Any] | None:
    """`data/<reg>.json` → `{published, title, text, types, report_date}`。

    **没有 `type == "本文"` 的元素就是「没发表」** → 返回 None(实测 bundle 就是这么 grep 的)。
    未发表时的实测形状是 `[{"reportDate_W": "令和8年10月1日"}]`。
    """
    if not data_text:
        return None
    try:
        data = json.loads(data_text)
    except (ValueError, TypeError) as exc:
        logger.warning("home_alerts: souten JSON 解析失败: %s", exc)
        return None
    if not isinstance(data, list):
        return None
    body = [x for x in data if isinstance(x, dict) and x.get("type") == "本文"]
    if not body:
        return None
    main = body[0]
    types = [
        str(x.get("type") or "").strip()
        for x in data
        if isinstance(x, dict) and str(x.get("type") or "").strip() in SOUTEN_TYPES
    ]
    seen: list[str] = []
    for t in types:
        if t not in seen:
            seen.append(t)
    return {
        "title": _norm_ws(str(main.get("title") or "")),
        "text": _clean_text(str(main.get("text") or "")).strip(),
        "types": seen,
        "publish_office": _norm_ws(str(main.get("publishOffice") or "")),
        "report_date": _norm_ws(str(main.get("reportDate_W") or "")),
        "published": era_date_to_iso(main.get("reportDate_W")),
    }


# ---- state ----------------------------------------------------------------

def _load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return {}          # 首次运行本来就没有 state, 正常路径不刷日志
    except (OSError, json.JSONDecodeError) as exc:
        # 10-05 审计 E05: state 读不出来(损坏/权限/目录被清)不能无日志地当「首次运行」——
        # 那会让当期所有在警状态重新以「发布」出条, 且日志里找不到任何指向。
        logger.warning("home_alerts: state 文件读不出来(%s), 当空状态处理: %s", exc, path)
        return {}
    if not isinstance(data, dict):
        logger.warning("home_alerts: state 文件不是 JSON 对象(%s), 当空状态处理",
                       type(data).__name__)
        return {}
    return data


def _save_state(path: Path, state: dict[str, Any]) -> None:
    # 临时文件+原子替换(同 html_columns D13 / tokyo_events D21) ——
    # 直写 write_text("w") 先截断后写, 中途被杀留半截 state, 下轮 _load_state
    # 当空状态, 当期所有在警疾病假「发布」刷屏。
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("home_alerts: state 写不进去 %s: %s", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _item(source: str, *, url: str, title: str, body: str, ts: datetime,
          tags: list[str], payload: dict[str, Any]) -> ItemRecord:
    key = compute_item_id(source, url=url)
    return ItemRecord(
        item=Item(
            id=key,
            source=source,
            url=url,
            title=title[:512],
            body=body[:BODY_LIMIT],
            author=payload.get("issuer") or None,
            ts=ts,
            lang="ja",
            tags=tags,
        ),
        adapter_name=NAME,
        source_payload_json=json.dumps(payload, ensure_ascii=False),
        media_urls=[],
    )


class HomeAlertsAdapter:
    """两个子源合并成一个 adapter(设计规格要求「注册进 cli.py(照 who_don)」)。"""

    name = NAME

    def __init__(
        self,
        *,
        fetch_week_index: Callable[[], str] | None = None,
        fetch_pdf: Callable[[str], bytes] | None = None,
        fetch_survey: Callable[[], str] | None = None,
        fetch_souten: Callable[..., tuple[str, str]] | None = None,
        to_text: Callable[[bytes], str] | None = None,
        state_path: Path | None = None,
    ) -> None:
        self._fetch_week_index = fetch_week_index or fetch_idsc_week_index
        self._fetch_pdf = fetch_pdf or fetch_idsc_pdf
        self._fetch_survey = fetch_survey or fetch_idsc_survey
        self._fetch_souten = fetch_souten or globals()["fetch_souten"]
        self._to_text = to_text or pdf_to_text
        self.state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH

    # ---- IDSC 子源 ------------------------------------------------------

    def _collect_idsc(self, state: dict[str, Any]) -> list[ItemRecord]:
        index_html = self._fetch_week_index()
        pdf_url = parse_latest_pdf_url(index_html)
        if not pdf_url:
            logger.info("home_alerts: 周报索引里没找到 PDF 链接, IDSC 子源跳过")
            return []
        text = self._to_text(self._fetch_pdf(pdf_url))
        if not text.strip():
            logger.info("home_alerts: 周报正文抽不出(%s), IDSC 子源跳过", pdf_url)
            return []
        thresholds = parse_thresholds(self._fetch_survey())
        sentinel = parse_sentinel(text)
        if not sentinel.get("diseases"):
            logger.info("home_alerts: 周报定点表没解析出病, IDSC 子源跳过")
            return []
        statuses = parse_alert_status(text)
        # 10-05 审计 E01: 散文段**解析失败** ≠ 「本周无警报状态」。
        # `_prose_after_table` 因标题改版/「・」缺失返回 "" 时 statuses={} 只是解析失败的
        # 形状, 若照常比较, 上期所有在警的病都会命中「解除」分支(假解除), 且 state 被
        # 覆盖成 {}, 下一期真实状态恢复又误报「发布」—— 一次解析失败制造一对假跃迁。
        # 所以解析失败时: 不比较、不出状态条、不覆盖 state, 只记 warning。
        # w36 那种「散文抽出来了但整段无状态词」是正常的无警报状态, 仍走原语义
        # (现有 test_release_emits_item / test_no_status_and_no_growth_emits_nothing 钉住)。
        prose_ok = bool(_prose_after_table(text).strip())
        if not prose_ok:
            logger.warning(
                "home_alerts: 周报散文段(注意報/警報状态出处)解析失败, 本期跳过状态比较"
                "且不覆盖 state: %s", pdf_url)
        # 公布时刻: 优先「集計」时刻(数据口径), 退 Reported 和暦发行日, 都没有就用 epoch
        # —— **不用今天的时间**, 那样会把一条几十年前的周报伪装成刚发生。
        published: datetime | None = None
        agg = _AGG_RE.search(text)
        if agg:
            try:
                published = datetime(
                    int(agg.group(1)), int(agg.group(2)), int(agg.group(3)),
                    int(agg.group(4)), int(agg.group(5)), tzinfo=JST,
                )
            except ValueError:
                published = None
        if published is None:
            for era in _ERA_DATE_RE.finditer(text):
                published = era_date_to_iso(era.group(0))
                if published is not None:
                    break
        if published is None:
            published = datetime(1970, 1, 1, tzinfo=JST)
        week = _week_label(text)

        idsc_state = state.get("idsc")
        prev_alerts = idsc_state.get("alerts") if isinstance(idsc_state, dict) else None
        if not isinstance(prev_alerts, dict):
            prev_alerts = {}

        out: list[ItemRecord] = []
        for key, info in sentinel["diseases"].items():
            level = statuses.get(key) if prose_ok else None
            before = prev_alerts.get(key) or ""
            # (level 原文, 标题, 触发说明) —— level 一律是**原文措辞**, 增长这种没有官方
            # 等级词的情形就写清楚是「增长触发」, 不拿基准值冒充等级。
            reasons: list[tuple[str, str, str]] = []
            # 规则①: 状态跃迁(首次发布 / 解除)。散文段解析失败时 status 未知, 整段跳过
            if prose_ok:
                if level and level != before:
                    verb = "继续发布" if before else "发布"
                    reasons.append((level, f"东京都{verb}{_cn(key)}{level}",
                                    f"{before or '无'} → {level}"))
                elif before and not level:
                    reasons.append((f"{before}解除", f"东京都解除{_cn(key)}{before}",
                                    f"{before} → 无"))
            # 规则②: 增长 ≥50% 且超注意報基准的一半
            thr = threshold_for(info["name"], thresholds)
            cur, prev = info["cur"], info["prev"]
            if thr and prev and prev > 0:
                ratio = cur / prev
                if ratio >= GROWTH_RATIO and cur > thr / 2:
                    reasons.append((
                        "定点報告数の急増",
                        f"东京都{_cn(key)}定点报告数一周大涨（{prev:g}→{cur:g}）",
                        f"定点当たり {prev:g} → {cur:g}（{ratio:.2f}倍 ≥ {GROWTH_RATIO}倍，"
                        f"且超过注意報基准 {thr:g} 的一半 {thr / 2:g}）",
                    ))
            if not reasons:
                continue
            level_text, title, trigger = reasons[0]
            body_lines = [
                f"{info['name']}：本周（第{week or '?'}週）定点当たり报告数 {cur:g}"
                f"（上周 {prev:g}）",
            ]
            if thr:
                body_lines.append(f"注意報开始基准值：{thr:g}（基准一半 {thr / 2:g}）")
            body_lines.append(f"触发条件：{trigger}")
            for lv, _, why in reasons[1:]:
                body_lines.append(f"同时触发：{lv} —— {why}")
            prose = _prose_after_table(text)
            if prose:
                body_lines.append("原文：" + _norm_ws(prose)[:600])
            body_lines.append(f"出典：{pdf_url}")
            # **病别必须体现在 URL 里**: 一份周报 PDF 会同时产出多个病的条目, 而
            # `compute_item_id` / `canonicalize_url` 会把 `#fragment` 丢掉 —— 用 fragment
            # 区分会让这些条目拿到**同一个 id**(实测 `test_distinct_diseases_get_distinct_item_ids`
            # 抓到的就是这个坑), `upsert_item` 于是把它们互相覆盖。
            # query 不会被 canonicalize_url 去掉, 所以走 `?disease=`。
            # 10-05 审计 E04: 索引页链接自带 cache-busting query(如 `?20260518`)时,
            # 再拼 `?` 会产出 `…39.pdf?20260518?disease=rs` 的畸形 URL —— 第二个 `?`
            # 语义错误, disease 参数丢失; 已有 query 就用 `&` 续。
            sep = "&" if "?" in pdf_url else "?"
            url = f"{pdf_url}{sep}disease={quote(key)}"
            out.append(_item(
                "home_alerts:tokyo_idsc",
                url=url,
                title=title,
                body="\n".join(body_lines),
                ts=published.astimezone(timezone.utc),
                tags=["风险提示", "传染病", "东京"],
                payload={
                    "kind": "risk",
                    "issuer": ISSUER_IDSC,
                    "regions": ["東京都", "東京"],
                    "level": level_text,
                    "published": published.isoformat(),
                    "disease": key,
                    "disease_ja": info["name"],
                    "rate": cur,
                    "rate_prev": prev,
                    "week": week,
                    "url_source": pdf_url,
                },
            ))

        # 10-05 审计 E01: 解析失败时保留上一期状态, 下期散文恢复正常才能比较出真跃迁
        if prose_ok:
            state["idsc"] = {"alerts": statuses, "week": week}
        return out

    # ---- JMA 子源 -------------------------------------------------------

    def _collect_souten(self, state: dict[str, Any]) -> list[ItemRecord]:
        _flg, data_text = self._fetch_souten(True)
        info = parse_souten(data_text)
        if not info:
            logger.info("home_alerts: 関東甲信早期天候情報无发表(flg/本文判定)")
            return []
        types = info["types"] or ["早期天候情報"]
        report = info["report_date"] or ""
        key = f"{report}|{'/'.join(types)}"
        souten_state = state.get("jma_souten")
        prev_key = souten_state.get("last_key") if isinstance(souten_state, dict) else None
        if prev_key == key:
            logger.info("home_alerts: 早期天候情報状态未变(%s), 不重复出条", key)
            return []
        published = info["published"] or datetime(1970, 1, 1, tzinfo=JST)
        lv = "/".join(types)
        cn = "、".join(_SOUTEN_CN.get(t, t) for t in types)
        title = f"气象厅发布关东甲信{cn}早期天候信息"
        if info["title"]:
            title += f" —— {info['title']}"
        body_lines = [
            f"关东甲信地方 早期天候情報（{lv}）",
            f"発表日：{report}",
        ]
        if info["publish_office"]:
            body_lines.append(f"発表機関：{info['publish_office']}")
        if info["text"]:
            body_lines.append("原文：" + _norm_ws(info["text"])[:1500])
        body_lines.append(f"出典：{SOUTEN_DATA}")
        rec = _item(
            "home_alerts:jma_souten",
            # 同 IDSC: 情报每次更新都是独立一期, id 必须随 (発表日+类型) 变,
            # 否则新的一期会被 `upsert_item` 并进旧的那条。
            url=f"{JMA_BASE}/?reg_no={SOUTEN_REG_NO}&issue={quote(key)}",
            title=title,
            body="\n".join(body_lines),
            ts=published.astimezone(timezone.utc),
            tags=["风险提示", "天候", "东京"],
            payload={
                "kind": "risk",
                "issuer": ISSUER_JMA,
                "regions": ["東京都", "東京", "関東甲信地方"],
                "level": lv,
                "published": published.isoformat(),
                "report_date": report,
                "types": types,
                "url_source": SOUTEN_DATA,
            },
        )
        state["jma_souten"] = {"last_key": key, "published": published.isoformat()}
        return [rec]

    # ---- 汇总 -----------------------------------------------------------

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable[ItemRecord]:
        state = _load_state(self.state_path)
        reset_request_budget()
        records: list[ItemRecord] = []
        for fn in (self._collect_idsc, self._collect_souten):
            try:
                got = fn(state)
            except Exception as exc:  # 单个子源炸了不能拖垮另一个
                logger.warning("home_alerts: 子源 %s 失败: %s", fn.__name__, exc)
                got = []
            if got:
                records.extend(got)
        _save_state(self.state_path, state)
        records.sort(key=lambda r: r.item.ts, reverse=True)
        return records[:limit] if limit is not None else records


def _cn(key: str) -> str:
    """病名 key → 中文病名(标题里用)。"""
    return {
        "インフルエンザ": "流感",
        "新型コロナ": "新冠",
        "rs": "呼吸道合胞病毒",
        "胃腸炎": "感染性胃肠炎",
        "手足口病": "手足口病",
    }.get(key, key)