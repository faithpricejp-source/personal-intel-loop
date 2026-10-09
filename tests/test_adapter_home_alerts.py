"""home_alerts: 常驻地(东京)健康与天候预警 —— 只在有实质变化时出条目。

fixtures 全部是 2026-10-05 实抓的**真实响应原始字节**(`tests/fixtures/home_alerts/`):

| fixture | 抓到的 URL | 用途 |
|---|---|---|
| `idsc_weekly_index.html` | `idsc.tmiph.metro.tokyo.lg.jp/weekly/` | 定位最新一期 PDF |
| `idsc_weekly_2026w39.pdf` | `…/assets/weekly/2026/39.pdf` | 注意報状态(实测 インフルエンザ=注意報) |
| `idsc_weekly_2026w36.pdf` | 同上 w36 | **无**任何警报状态(三条全是「増加傾向」) |
| `idsc_weekly_2026w30.pdf` | 同上 w30 | 警報状态(实测 手足口病=警報) + 增长用例 |
| `idsc_survey_thresholds.html` | `…/survey/` | 注意報基准值表 |
| `jma_souten_20_none.json` | `data.jma.go.jp/cpd/souten/data/20.json` | 関東甲信**无发表**的真实形状 |

PDF 相关用例需要 `pdftotext`(poppler)和三份周报 PDF。PDF 是东京都感染症情报中心公开发布的周报,
但不随本仓库分发: 想跑这些用例, 自己从上表 URL 下载放进 `tests/fixtures/home_alerts/`。
缺 pdftotext 或缺 PDF 时**只跳过** PDF 用例并显式提示 ——
不允许静默把「抽不出文本」当成「解析失败」, 那会把真 bug 藏起来(见 `test_pdf_to_text_missing_binary`)。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from personal_intel_loop.adapters import home_alerts as ha
from personal_intel_loop.adapters.home_alerts import HomeAlertsAdapter

FIX = Path(__file__).parent / "fixtures" / "home_alerts"

INDEX_HTML = (FIX / "idsc_weekly_index.html").read_bytes()
SURVEY_HTML = (FIX / "idsc_survey_thresholds.html").read_bytes()
SOUTEN_NONE = (FIX / "jma_souten_20_none.json").read_bytes()

HAS_PDFTOTEXT = shutil.which("pdftotext") is not None
PDF_FIXTURES = ("idsc_weekly_2026w39.pdf", "idsc_weekly_2026w36.pdf", "idsc_weekly_2026w30.pdf")
HAS_PDF_FIXTURES = all((FIX / name).exists() for name in PDF_FIXTURES)
needs_pdftotext = pytest.mark.skipif(
    not (HAS_PDFTOTEXT and HAS_PDF_FIXTURES),
    reason="需要 poppler 的 pdftotext 和周报 PDF fixture(不随仓库分发, 见模块说明)",
)


def _text_of(pdf_name: str) -> str:
    """用被测代码自己的 `pdf_to_text` 抽 fixture 文本(别在测试里另写一套抽取)。"""
    text = ha.pdf_to_text((FIX / pdf_name).read_bytes())
    assert text.strip(), f"{pdf_name} 抽不出文本"
    return text


def _adapter(tmp_path, *, week_pdf: bytes = b"", survey: bytes = SURVEY_HTML,
             souten=(b"", b""), state: dict | None = None, index: bytes = INDEX_HTML,
             to_text=None) -> HomeAlertsAdapter:
    state_path = tmp_path / "state.json"
    if state is not None:
        state_path.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    return HomeAlertsAdapter(
        fetch_week_index=lambda: index.decode("utf-8", "replace"),
        fetch_pdf=lambda url: week_pdf,
        fetch_survey=lambda: survey.decode("utf-8", "replace"),
        fetch_souten=lambda flag=True: (
            souten[0].decode("utf-8", "replace"),
            souten[1].decode("utf-8", "replace"),
        ),
        to_text=to_text if to_text is not None else (lambda b: _text_of("idsc_weekly_2026w39.pdf")),
        state_path=state_path,
    )


def _payload(rec) -> dict:
    return json.loads(rec.source_payload_json)


# ---- 入口定位 --------------------------------------------------------------

def test_parse_latest_pdf_url_picks_newest_week():
    """索引页里 2025 与 2026 的周报混列, 要取 (年, 周) 最大的, 不是文件里最后一个。"""
    html = INDEX_HTML.decode("utf-8")
    url = ha.parse_latest_pdf_url(html)
    assert url == ("https://idsc.tmiph.metro.tokyo.lg.jp/assets/weekly/2026/39.pdf")


def test_parse_latest_pdf_url_year_boundary():
    """跨年周次按 (年, 周) 字典序取最大: 第1週(2026) 晚于 第52週(2025) —— 周次数字会回绕。"""
    html = ('<a href="/assets/weekly/2025/52.pdf">52</a>'
            '<a href="/assets/weekly/2026/01.pdf">1</a>')
    assert ha.parse_latest_pdf_url(html).endswith("/2026/01.pdf")
    # 只有旧年时也要取得到
    assert ha.parse_latest_pdf_url(
        '<a href="/assets/weekly/2025/52.pdf">52</a>').endswith("/2025/52.pdf")


def test_parse_latest_pdf_url_missing_or_broken():
    assert ha.parse_latest_pdf_url("") == ""
    assert ha.parse_latest_pdf_url("<html>not found</html>") == ""
    assert ha.parse_latest_pdf_url('<a href="/assets/weekly/abc/x.pdf">x</a>') == ""


# ---- 基准值表(注意報阈值的官方出处) ------------------------------------------

def test_parse_thresholds_from_official_survey_page():
    """基准值必须来自 `/survey/` 官方页面原文, 不是猜的。"""
    th = ha.parse_thresholds(SURVEY_HTML.decode("utf-8"))
    assert th["インフルエンザ"] == {"alarm": 30.0, "alarm_end": 10.0, "attention": 10.0}
    assert th["手足口病"] == {"alarm": 5.0, "alarm_end": 2.0, "attention": None}
    # 「-」= 未规定 → None, 不能当 0(0 会让「超过基准一半」永远成立)
    assert th["感染性胃腸炎"]["attention"] is None
    assert th["水痘"]["attention"] == 1.0
    assert len(th) == 11


def test_parse_thresholds_missing_returns_empty():
    assert ha.parse_thresholds("") == {}
    assert ha.parse_thresholds("<html>no table</html>") == {}
    assert ha.parse_thresholds("<p>【参考】以外のページ</p>") == {}


def test_threshold_for_unknown_disease_is_none():
    """新型コロナ/RS ウイルス 不在官方基准表里(表只有 11 个病)→ 无阈值。"""
    th = ha.parse_thresholds(SURVEY_HTML.decode("utf-8"))
    assert ha.threshold_for("新型コロナウイルス感染症（COVID-19）", th) is None
    assert ha.threshold_for("ＲＳウイルス感染症", th) is None
    assert ha.threshold_for("インフルエンザ", th) == 10.0


# ---- 周报定点表 -------------------------------------------------------------

@needs_pdftotext
def test_parse_sentinel_real_pdf_all_tracked_diseases():
    """真实 w39 PDF: 5 个跟踪病全部抽出, 且值与原文一致(9.51 是官方 39 週值)。"""
    s = ha.parse_sentinel(_text_of("idsc_weekly_2026w39.pdf"))
    assert s["year"] == 2026
    assert s["weeks"] == [36, 37, 38, 39]
    got = s["diseases"]
    assert set(got) == {"インフルエンザ", "新型コロナ", "rs", "胃腸炎", "手足口病"}
    assert got["インフルエンザ"]["cur"] == 9.51
    assert got["インフルエンザ"]["prev"] == 15.14   # 38週
    assert got["新型コロナ"]["cur"] == 1.28
    assert got["rs"]["cur"] == 0.86
    assert got["胃腸炎"]["cur"] == 2.40
    assert got["手足口病"]["cur"] == 0.67


@needs_pdftotext
def test_parse_sentinel_three_real_weeks_agree():
    """三期都解析, 且周次表头跟着变(表里是滚动 4 周窗口)。"""
    expect = {
        "idsc_weekly_2026w39.pdf": ([36, 37, 38, 39], 9.51),
        "idsc_weekly_2026w36.pdf": ([33, 34, 35, 36], 5.46),
        "idsc_weekly_2026w30.pdf": ([27, 28, 29, 30], 0.33),
    }
    for name, (weeks, flu) in expect.items():
        s = ha.parse_sentinel(_text_of(name))
        assert s["weeks"] == weeks, name
        assert s["diseases"]["インフルエンザ"]["cur"] == flu, name


def test_parse_sentinel_bogus_text_returns_empty():
    """结构变了/不是周报 → 空 dict, 不抛(网络失败与结构变更都走这里)。"""
    assert ha.parse_sentinel("") == {}
    assert ha.parse_sentinel("<html>503 maintenance</html>") == {}
    assert ha.parse_sentinel("定点把握対象疾患 報告数 だが数字の行がない") == {}


@needs_pdftotext
def test_parse_sentinel_rejects_non_four_number_rows():
    """基幹/眼科的散行(只有 1~2 个数)不能被当成定点值。"""
    s = ha.parse_sentinel(_text_of("idsc_weekly_2026w39.pdf"))
    for info in s["diseases"].values():
        assert len(info["rates"]) == 4


# ---- 注意報/警報状态(在表下面的散文段里, 不在表里) --------------------------

@needs_pdftotext
def test_alert_status_attention_from_real_prose():
    """w39 原文: 「…東京都全体の31.28％に達しているため、注意報レベルが続いています。」"""
    assert ha.parse_alert_status(_text_of("idsc_weekly_2026w39.pdf")) == {
        "インフルエンザ": "注意報",
    }


@needs_pdftotext
def test_alert_status_alarm_from_real_prose():
    """w30 原文: 「手足口病の定点当たり報告数は8.10と減少してきていますが、引き続き警報レベルです。」"""
    assert ha.parse_alert_status(_text_of("idsc_weekly_2026w30.pdf")) == {
        "手足口病": "警報",
    }


@needs_pdftotext
def test_alert_status_none_when_no_alert_words():
    """w36 三条全是「増加傾向です」, 一个注意報/警報词都没有 → 本周无警报状态。"""
    assert ha.parse_alert_status(_text_of("idsc_weekly_2026w36.pdf")) == {}


def test_alert_status_prefers_trailing_conclusion():
    """同一条里先出现保健所级「警報レベル」后出现都级「注意報レベル」时取**靠后**的总结句。"""
    text = (
        "（ 今週の注目される定点把握対象疾患 ）\n"
        "・インフルエンザの定点当たり報告数は9.51です。31保健所中1保健所が警報レベル、"
        "9保健所が注意報レベルであり、保健所管内人口の合計が東京都全体の31.28％に達しているため、"
        "注意報レベルが続いています。\n"
    )
    assert ha.parse_alert_status(text) == {"インフルエンザ": "注意報"}


def test_alert_status_handles_fullwidth_and_halfwidth_rs():
    text = ("（ 今週の注目される定点把握対象疾患 ）\n"
            "・RSウイルス感染症の定点当たり報告数は1.20で、警報レベルです。\n")
    assert ha.parse_alert_status(text) == {"rs": "警報"}


def test_alert_status_no_prose_returns_empty():
    assert ha.parse_alert_status("") == {}
    assert ha.parse_alert_status("今週の注目される定点把握対象疾患 という語だけ") == {}


# ---- 规则①: 状态跃迁(首次发布 / 同状态不重复 / 解除) ------------------------

@needs_pdftotext
def test_first_attention_emits_one_item(tmp_path):
    """state 为空 + 本周有注意報 → 出条(「首次发布」)。"""
    recs = list(_adapter(tmp_path).collect())
    assert len(recs) == 1
    rec = recs[0]
    assert rec.item.source == "home_alerts:tokyo_idsc"
    assert rec.item.title == "东京都发布流感注意報"
    assert "9.51" in rec.item.body and "15.14" in rec.item.body
    p = _payload(rec)
    assert p["kind"] == "risk"
    assert p["level"] == "注意報"
    assert p["regions"] == ["東京都", "東京"]
    assert p["disease"] == "インフルエンザ"
    assert p["rate"] == 9.51 and p["rate_prev"] == 15.14
    assert p["published"]               # ISO
    assert "注意報開始基準値" not in rec.item.body  # 正文用中文写基准, 不混原文
    assert "10" in rec.item.body


@needs_pdftotext
def test_same_status_does_not_emit_again(tmp_path):
    """state 里已是同一个状态 → 不重复出条(低召回的关键)。"""
    state = {"idsc": {"alerts": {"インフルエンザ": "注意報"}, "week": "2026-39"}}
    recs = list(_adapter(tmp_path, state=state).collect())
    assert recs == []


@needs_pdftotext
def test_continued_status_not_treated_as_new_release(tmp_path):
    """从注意報「续报」时 level 没变 → 不出条(避免每周刷屏)。"""
    state = {"idsc": {"alerts": {"インフルエンザ": "注意報"}, "week": "2026-38"}}
    assert list(_adapter(tmp_path, state=state).collect()) == []


@needs_pdftotext
def test_release_emits_item(tmp_path):
    """state 是「注意報」而本周散文里没有 → 出「解除」条, 且 level 写明是解除。"""
    text = _text_of("idsc_weekly_2026w36.pdf")     # w36 全文无警报词
    state = {"idsc": {"alerts": {"インフルエンザ": "注意報"}, "week": "2026-35"}}
    ad = _adapter(tmp_path, state=state, to_text=lambda b: text)
    recs = list(ad.collect())
    assert len(recs) == 1
    assert recs[0].item.title == "东京都解除流感注意報"
    assert _payload(recs[0])["level"] == "注意報解除"


@needs_pdftotext
def test_alarm_status_emits(tmp_path):
    """w30 原文是「警報」→ level 必须是警報原文, 不是自造词。"""
    ad = _adapter(tmp_path, to_text=lambda b: _text_of("idsc_weekly_2026w30.pdf"))
    recs = list(ad.collect())
    assert len(recs) == 1
    assert recs[0].item.title == "东京都发布手足口病警報"
    assert _payload(recs[0])["level"] == "警報"
    assert _payload(recs[0])["rate"] == 8.10


@needs_pdftotext
def test_no_status_no_state_no_alert_item(tmp_path):
    """既没状态、state 也是空 → **不出状态条**(第一次跑不该刷一屏历史注意報)。

    w36 流感 2.66→5.46 确实满足增长规则, 所以这里只断言「没有注意報/警報那条」,
    增长那条是该出的(另见 `test_growth_over_half_threshold_emits`)。
    """
    ad = _adapter(tmp_path, to_text=lambda b: _text_of("idsc_weekly_2026w36.pdf"))
    titles = [r.item.title for r in ad.collect()]
    assert not any("注意報" in t or "警報" in t for t in titles)


@needs_pdftotext
def test_no_status_and_no_growth_emits_nothing(tmp_path):
    """既无警报状态、又没增长(周报存在但一切平稳)→ 一条都不出, state 仍要落盘。"""
    body = (
        "東京都感染症週報\n     2026年第40週\n"
        "（10月5日～10月11日）\n"
        "＊ 2026年10月14日現在の情報により作成しています。\n"
        "                    2026/10/14 11:00集計\n"
        "定点把握対象疾患 報告数 2026年40週\n"
        "        37週         38週           39週        40週\n"
        "        ＲＳウイルス感染症\n"
        "                                       1.20        1.49          1.43       1.50\n"
        "（ 今週の注目される定点把握対象疾患 ）\n"
        "・ＲＳウイルス感染症の定点当たり報告数は1.50で、増加傾向です。\n"
        "【年齢階級別】\n"
    )
    ad = _adapter(tmp_path, to_text=lambda b: body)
    assert list(ad.collect()) == []
    saved = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert saved["idsc"]["alerts"] == {}       # 记住「本期无警报」, 下期有变化才知道是跃迁
    assert saved["idsc"]["week"] == "2026-40"


@needs_pdftotext
def test_state_records_current_alerts_for_next_run(tmp_path):
    """跑完 state 要记住本期状态, 下一轮才比较得出来。"""
    ad = _adapter(tmp_path)
    list(ad.collect())
    saved = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert saved["idsc"]["alerts"] == {"インフルエンザ": "注意報"}
    assert saved["idsc"]["week"] == "2026-39"


# ---- 规则②: 增长阈值 --------------------------------------------------------

@needs_pdftotext
def test_growth_over_half_threshold_emits(tmp_path):
    """w36 インフルエンザ 2.66→5.46(2.05倍) 且 5.46 > 基准10 的一半 5 → 出条。"""
    ad = _adapter(tmp_path, to_text=lambda b: _text_of("idsc_weekly_2026w36.pdf"))
    recs = list(ad.collect())
    assert len(recs) == 1
    assert "大涨" in recs[0].item.title
    assert "2.66" in recs[0].item.title and "5.46" in recs[0].item.title
    p = _payload(recs[0])
    assert p["level"] == "定点報告数の急増"      # 没有官方等级词, 不拿基准值冒充
    assert p["rate"] == 5.46 and p["rate_prev"] == 2.66


@needs_pdftotext
def test_growth_below_half_threshold_silent(tmp_path):
    """同样翻倍但没过基准一半 → 不出条(实测 w30 仙人掌 0.23→0.33 只有 1.43 倍)。"""
    ad = _adapter(tmp_path, to_text=lambda b: _text_of("idsc_weekly_2026w30.pdf"))
    titles = [r.item.title for r in ad.collect()]
    assert all("大涨" not in t for t in titles)


@needs_pdftotext
def test_decline_emits_nothing(tmp_path):
    """w39 インフルエンザ 15.14→9.51 是**下降**, 光有注意報才出条, 不因下降额外出。"""
    ad = _adapter(tmp_path, to_text=lambda b: _text_of("idsc_weekly_2026w39.pdf"),
                  state={"idsc": {"alerts": {"インフルエンザ": "注意報"}, "week": "2026-38"}})
    assert list(ad.collect()) == []


def test_growth_rule_needs_threshold(tmp_path):
    """无注意報基准的病(新型コロナ/RS)即使翻倍也不出条 —— 「超过基准一半」无从判断。"""
    body = (
        "定点把握対象疾患 報告数 2026年39週\n"
        "        36週         37週           38週        39週\n"
        "        ＲＳウイルス感染症\n"
        "                        1.20        1.49          1.43       4.00\n"
        "（ 今週の注目される定点把握対象疾患 ）\n"
        "・ＲＳウイルス感染症の定点当たり報告数は4.00で、増加傾向です。\n"
    )
    ad = _adapter(tmp_path, to_text=lambda b: body)
    # 先确认这份构造文本真的被解析到了(否则断言「不出条」是空过)
    assert ha.parse_sentinel(body)["diseases"]["rs"]["cur"] == 4.00
    assert list(ad.collect()) == []


def test_growth_rule_zero_previous_rate_silent(tmp_path):
    """上周 0 → 增长倍数无定义, 不能当 inf 触发(0 → 4.0 也是「增长」但不可比)。"""
    body = (
        "定点把握対象疾患 報告数 2026年39週\n"
        "        36週         37週           38週        39週\n"
        "        インフルエンザ\n"
        "                        0.00         0.00          0.00       4.00\n"
        "（ 今週の注目される定点把握対象疾患 ）\n"
        "・インフルエンザの定点当たり報告数は4.00です。\n"
    )
    ad = _adapter(tmp_path, to_text=lambda b: body)
    assert ha.parse_sentinel(body)["diseases"]["インフルエンザ"]["prev"] == 0.0
    assert list(ad.collect()) == []


@needs_pdftotext
def test_alert_and_growth_merge_into_one_item(tmp_path):
    """同一病同一周同时满足两条规则 → 只出一条(避免一条病出两条)。"""
    text = _text_of("idsc_weekly_2026w36.pdf").replace(
        "・インフルエンザの定点当たり報告数は5.46で、先週より増加しています。",
        "・インフルエンザの定点当たり報告数は5.46で、警報レベルです。",
    )
    ad = _adapter(tmp_path, to_text=lambda b: text)
    recs = list(ad.collect())
    flu = [r for r in recs if _payload(r)["disease"] == "インフルエンザ"]
    assert len(flu) == 1
    body = flu[0].item.body
    assert "警報" in body            # 级别取状态跃迁
    assert "定点当たり" in body       # 增长条件也记在正文里


# ---- JMA 早期天候情報(関東甲信) ---------------------------------------------

def test_souten_none_publication_real_fixture_returns_none():
    """真实无发表响应 `[{"reportDate_W": "令和8年10月1日"}]` → None(只有日期, 没有 本文)。"""
    assert ha.parse_souten(SOUTEN_NONE.decode("utf-8")) is None


def test_souten_published_emits_item(tmp_path):
    """有 `type == "本文"` 的元素 → 出条, 正文照抄原文。"""
    published = json.dumps([
        {"reportDate_W": "令和8年10月1日", "reportTime_W": "14時30分",
         "publishOffice": "気象庁", "type": "本文",
         "title": "関東甲信地方 早期天候情報（高温）",
         "text": "10月1日から10月7日oxia、平均気温が平年よりかなり高い"},
        {"reportDate_W": "令和8年10月1日", "publishOffice": "気象庁", "type": "高温",
         "reg_ch_text": "関東甲信", "sdate": 20261002, "thres": 3.5, "targetDuration": 5},
    ], ensure_ascii=False).encode("utf-8")
    ad = _adapter(tmp_path, week_pdf=b"", to_text=lambda b: "",
                  souten=(b'{"snow":-9,"temp":1}', published))
    recs = list(ad.collect())
    assert len(recs) == 1
    rec = recs[0]
    assert rec.item.source == "home_alerts:jma_souten"
    assert "高温" in rec.item.title
    assert "早期天候情報" in rec.item.body
    assert "平年よりかなり高い" in rec.item.body   # 原文照抄
    assert "令和8年10月1日" in rec.item.body      # 原文日期
    p = _payload(rec)
    assert p["kind"] == "risk"
    assert p["level"] == "高温"
    assert p["types"] == ["高温"]
    assert "関東甲信地方" in p["regions"]


def test_souten_no_publication_emits_nothing(tmp_path):
    """関東甲信无发表(实测当前就是这个状态)→ 一条都不出。"""
    ad = _adapter(tmp_path, week_pdf=b"", to_text=lambda b: "", souten=(b"", SOUTEN_NONE))
    assert list(ad.collect()) == []


def test_souten_same_publication_not_repeated(tmp_path):
    """同一份情报(同発表日+同类型)不重复出条。"""
    published = json.dumps([
        {"reportDate_W": "令和8年10月1日", "publishOffice": "気象庁", "type": "本文",
         "title": "関東甲信地方 早期天候情報（高温）", "text": "oxia"},
        {"reportDate_W": "令和8年10月1日", "publishOffice": "気象庁", "type": "高温"},
    ], ensure_ascii=False).encode("utf-8")
    key = "令和8年10月1日|高温"
    state = {"jma_souten": {"last_key": key}}
    ad = _adapter(tmp_path, week_pdf=b"", to_text=lambda b: "",
                  souten=(b"", published), state=state)
    assert list(ad.collect()) == []


def test_souten_snow_and_cold_low_types(tmp_path):
    """大雪 / かなりの低温 也要能出条并映射成中文标题。"""
    published = json.dumps([
        {"reportDate_W": "令和8年12月2日", "publishOffice": "気象庁", "type": "本文",
         "title": "関東甲信地方 早期天候情報（大雪）", "text": "降雪量がかなり多い"},
        {"reportDate_W": "令和8年12月2日", "publishOffice": "気象庁", "type": "大雪"},
    ], ensure_ascii=False).encode("utf-8")
    ad = _adapter(tmp_path, week_pdf=b"", to_text=lambda b: "",
                  souten=(b'{"snow":1,"temp":1}', published))
    recs = list(ad.collect())
    assert len(recs) == 1
    assert "大雪" in recs[0].item.title
    assert _payload(recs[0])["level"] == "大雪"


def test_parse_souten_broken_returns_none():
    assert ha.parse_souten("") is None
    assert ha.parse_souten("<html>503</html>") is None
    assert ha.parse_souten('{"not":"a list"}') is None
    assert ha.parse_souten("null") is None


def test_era_date_to_iso():
    assert ha.era_date_to_iso("令和8年10月1日").isoformat() == "2026-10-01T00:00:00+09:00"
    assert ha.era_date_to_iso("平成30年4月1日").year == 2018
    assert ha.era_date_to_iso("2026年10月1日") is None   # 非和暦
    assert ha.era_date_to_iso("") is None
    assert ha.era_date_to_iso(None) is None


def test_souten_reg_no_is_kanto_koshin():
    """関東甲信 = 20, 且由気象庁发布(不是分区台)。这两个值写错了就整个子源落空。"""
    assert ha.SOUTEN_REG_NO == "20"
    assert ha.SOUTEN_REG_NAME == "関東甲信地方"
    bundle = (FIX / "jma_souten_index.html.bundle.js").read_text("utf-8", "replace")
    assert '20:"関東甲信地方"' in bundle          # regData
    assert '20:"気象庁"' in bundle                # publishOfficeData
    assert "./data/flg.json" in bundle


# ---- 网络失败: 返回空不抛 ----------------------------------------------------

def test_network_failures_return_empty_not_raise(tmp_path, caplog):
    """索引页/基准值页/PDF/souten 全挂 → 一条都不出, 且不抛异常。"""
    def boom_bytes(url: str) -> bytes:
        raise AssertionError("不该走到这里")

    ad = HomeAlertsAdapter(
        fetch_week_index=lambda: "",                    # 索引失败
        fetch_pdf=lambda url: b"",
        fetch_survey=lambda: "",                       # 基准值失败
        fetch_souten=lambda flag=True: ("", ""),       # souten 失败
        to_text=lambda b: "",
        state_path=tmp_path / "state.json",
    )
    assert list(ad.collect()) == []

    ad2 = HomeAlertsAdapter(
        fetch_week_index=lambda: INDEX_HTML.decode("utf-8"),
        fetch_pdf=boom_bytes,                          # 有 URL 但抓不到 → 不该调 to_text
        fetch_survey=lambda: "",
        fetch_souten=lambda flag=True: ("", "<html>err</html>"),
        to_text=lambda b: pytest.fail("PDF 为空时不该调 to_text"),
        state_path=tmp_path / "state2.json",
    )
    assert list(ad2.collect()) == []


@needs_pdftotext
def test_empty_pdf_text_returns_no_items(tmp_path):
    """PDF 抓到了但文本抽不出(空)→ 静默跳过, 不产出垃圾条目。"""
    ad = _adapter(tmp_path, week_pdf=b"%PDF-1.4 broken", to_text=lambda b: "")
    assert list(ad.collect()) == []


def test_throttle_enforces_two_second_interval(monkeypatch):
    """同一 host 连续两次请求必须间隔 ≥2 秒(冻结范围的出网纪律)。"""
    slept: list[float] = []
    clock = {"t": 100.0}

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(ha.time, "sleep", fake_sleep, raising=False)
    monkeypatch.setattr(ha, "_HITS", {})
    monkeypatch.setattr(ha, "_CALLS", {})
    monkeypatch.setattr(ha.time, "monotonic", lambda: clock["t"], raising=False)

    ha._throttle("idsc.example")
    assert slept == []                     # 本轮第一次不睡
    clock["t"] += 0.3
    ha._throttle("idsc.example")
    assert slept and slept[-1] >= 1.7# 补足到 2 秒
    # 换一个 host 不受影响
    ha._throttle("jma.example")
    assert len(slept) == 1


def test_throttle_caps_at_25_calls_per_host(monkeypatch):
    """每站 ≤25 次: 第 26 次直接抛(而不是继续出网)。"""
    clock = {"t": 100.0}
    monkeypatch.setattr(ha, "_HITS", {})
    monkeypatch.setattr(ha, "_CALLS", {})
    # 打掉时钟, 否则 24 次真实节流要真的睡 48 秒
    monkeypatch.setattr(ha.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(ha.time, "monotonic", lambda: clock["t"])
    for _ in range(25):
        ha._throttle("idsc.example")
    with pytest.raises(RuntimeError, match="上限"):
        ha._throttle("idsc.example")


def test_request_budget_resets_each_collect(monkeypatch):
    """上限是**每轮**的 —— collect 开头会清零, 不会跨轮累计到第二天锁死。"""
    monkeypatch.setattr(ha, "_HITS", {})
    monkeypatch.setattr(ha, "_CALLS", {"idsc.example": 25})
    ha.reset_request_budget()
    assert ha._CALLS == {}
    ha._throttle("idsc.example")          # 清零后又能发


def test_one_subsource_failure_does_not_kill_other(tmp_path):
    def bad_idsc():
        raise RuntimeError("idsc boom")

    published = json.dumps([
        {"reportDate_W": "令和8年10月1日", "publishOffice": "気象庁", "type": "本文",
         "title": "関東甲信地方 早期天候情報（高温）", "text": "oxia"},
        {"reportDate_W": "令和8年10月1日", "publishOffice": "気象庁", "type": "高温"},
    ], ensure_ascii=False).encode("utf-8")

    ad = HomeAlertsAdapter(
        fetch_week_index=bad_idsc,
        fetch_pdf=lambda url: b"",
        fetch_survey=lambda: "",
        fetch_souten=lambda flag=True: ("", published),
        to_text=lambda b: "",
        state_path=tmp_path / "state.json",
    )
    recs = list(ad.collect())
    assert [r.item.source for r in recs] == ["home_alerts:jma_souten"]


# ---- pdftotext 依赖本身 -----------------------------------------------------

def test_pdf_to_text_missing_binary_returns_empty(monkeypatch):
    """找不到 pdftotext → 返回空字符串, **不抛**(缺依赖不该让整个 adapter 崩)。"""
    monkeypatch.setattr(ha, "_find_pdftotext", lambda: None)
    assert ha.pdf_to_text(b"%PDF-1.4 x") == ""
    assert ha.pdf_to_text(b"") == ""


def test_pdf_to_text_broken_pdf_returns_empty():
    """pdftotext 对坏 PDF 返回非 0 → 空字符串, 不抛。"""
    assert ha.pdf_to_text(b"not a pdf at all") == ""


@needs_pdftotext
def test_pdf_to_text_real_fixture_roundtrip():
    """真实 PDF 能抽出含关键数字的文本(证明这套抽取在本机可用)。"""
    text = ha.pdf_to_text((FIX / "idsc_weekly_2026w39.pdf").read_bytes())
    assert "東京都感染症週報" in text
    assert "9.51" in text
    assert "インフルエンザ" in text


@needs_pdftotext
def test_layout_flag_is_required():
    """不加 `-layout` 定点表两行会串行 → 抽出的定点值不对。这是抽取必须带 -layout 的原因。"""
    exe = shutil.which("pdftotext")
    data = (FIX / "idsc_weekly_2026w39.pdf").read_bytes()
    with_layout = subprocess.run([exe, "-layout", "-", "-"], input=data,
                                 capture_output=True, check=False).stdout.decode("utf-8", "replace")
    without = subprocess.run([exe, "-", "-"], input=data,
                             capture_output=True, check=False).stdout.decode("utf-8", "replace")
    assert ha.parse_sentinel(with_layout)["diseases"]["インフルエンザ"]["cur"] == 9.51
    # 不加 layout 时至少不再是干净的 4 列(可能抽不出, 也可能值不同) —— 断言「不同」即可
    assert without != with_layout


# ---- 注册进 cli -------------------------------------------------------------

def test_registered_in_cli():
    from personal_intel_loop import cli

    assert "home_alerts" in cli.SUPPORTED_ADAPTER_NAMES
    assert isinstance(cli._load_adapter("home_alerts"), HomeAlertsAdapter)


# ---- state 健壮性 -----------------------------------------------------------

@needs_pdftotext
def test_broken_state_file_is_ignored(tmp_path):
    """state 文件坏了 → 当空 state 处理(第一次跑), 不抛。"""
    (tmp_path / "state.json").write_text("{ this is not json", "utf-8")
    ad = _adapter(tmp_path)
    recs = list(ad.collect())
    assert [r.item.source for r in recs] == ["home_alerts:tokyo_idsc"]


@needs_pdftotext
def test_state_of_wrong_shape_is_ignored(tmp_path):
    """state 结构不对(比如 alerts 是数组)→ 不抛。"""
    state = {"idsc": {"alerts": ["インフルエンザ"], "week": 39}, "jma_souten": "x"}
    ad = _adapter(tmp_path, state=state)
    recs = list(ad.collect())
    assert len(recs) == 1


# ---- Item 契约 --------------------------------------------------------------

@needs_pdftotext
def test_item_payload_contract(tmp_path):
    """source_payload 的形状照设计规格第二节: kind/regions/level/published。"""
    rec = list(_adapter(tmp_path).collect())[0]
    p = _payload(rec)
    assert p["kind"] == "risk"
    assert isinstance(p["regions"], list) and "東京都" in p["regions"]
    assert isinstance(p["level"], str) and p["level"]
    # ISO 8601
    from datetime import datetime as _dt
    _dt.fromisoformat(p["published"])
    assert rec.item.lang == "ja"
    assert "风险提示" in rec.item.tags


@needs_pdftotext
def test_distinct_diseases_get_distinct_item_ids(tmp_path):
    """同一条周报里多个病出条时 id 不能撞车(fragment 会被 canonicalize_url 丢掉)。"""
    text = _text_of("idsc_weekly_2026w36.pdf").replace(
        "・インフルエンザの定点当たり報告数は5.46で、先週より増加しています。",
        "・インフルエンザの定点当たり報告数は5.46で、警報レベルです。"
        "・ＲＳウイルス感染症の定点当たり報告数は1.20で、警報レベルです。",
    )
    ad = _adapter(tmp_path, to_text=lambda b: text)
    recs = list(ad.collect())
    assert len(recs) == 2
    assert len({r.item.id for r in recs}) == 2
    assert len({r.item.url for r in recs}) == 2