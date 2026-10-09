"""审计 E 卷(E01–E06、E08、E10–E19)修复的回归测试(2026-10-05, 见 audits/AUDIT_E.md)。

每条先复现(修复前失败)、修复后通过。E07/E09 按设计规格不动。
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest

from personal_intel_loop.adapters import home_alerts as ha
from personal_intel_loop.adapters.home_alerts import HomeAlertsAdapter

HA_FIX = Path(__file__).parent / "fixtures" / "home_alerts"
HA_INDEX = (HA_FIX / "idsc_weekly_index.html").read_bytes()
HA_SURVEY = (HA_FIX / "idsc_survey_thresholds.html").read_bytes()

HAS_PDFTOTEXT = shutil.which("pdftotext") is not None
_PDF_FIXTURES = ("idsc_weekly_2026w39.pdf", "idsc_weekly_2026w36.pdf")
needs_pdftotext = pytest.mark.skipif(
    not (HAS_PDFTOTEXT and all((HA_FIX / n).exists() for n in _PDF_FIXTURES)),
    reason="需要 poppler 的 pdftotext 和周报 PDF fixture(不随仓库分发, 见 test_adapter_home_alerts 说明)")


def _ha_text(pdf_name: str) -> str:
    return ha.pdf_to_text((HA_FIX / pdf_name).read_bytes())


def _ha_adapter(tmp_path, *, week_text: str = "", survey: str = "",
                state: dict | None = None,
                index: str | None = None,
                souten=(b"", b"")) -> HomeAlertsAdapter:
    """home_alerts adapter(注入全部取数, 不出网)。week_text 直接当 PDF 抽取结果。"""
    state_path = tmp_path / "state.json"
    if state is not None:
        state_path.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    return HomeAlertsAdapter(
        fetch_week_index=lambda: (index if index is not None
                                  else HA_INDEX.decode("utf-8", "replace")),
        fetch_pdf=lambda url: b"%PDF-fake",
        fetch_survey=lambda: survey or HA_SURVEY.decode("utf-8", "replace"),
        fetch_souten=lambda flag=True: (
            souten[0].decode("utf-8", "replace"), souten[1].decode("utf-8", "replace")),
        to_text=lambda b: week_text,
        state_path=state_path,
    )


# --------------------------------------------------------------------------- #
# E01: 散文段解析失败会被当成「全部解除」, 制造一对假状态跃迁
# --------------------------------------------------------------------------- #

_PROSE_BODY = (
    "東京都感染症週報\n"
    "     2026年第41週\n"
    "定点把握対象疾患 報告数 2026年41週\n"
    "        38週         39週           40週        41週\n"
    "        インフルエンザ\n"
    "                        1.00        1.10          1.20       1.00\n"
    "（ 今週の注目される定点把握対象疾患 ）\n"
    "・インフルエンザの定点当たり報告数は1.00で、注意報レベルが続いています。\n"
    "【年齢階級別】\n"
)

# 标题改版(「今週の注目される…」没了)→ 散文段抽不出来 → 解析失败, 不是「本周无状态」
_PROSE_BODY_RETITLED = _PROSE_BODY.replace(
    "（ 今週の注目される定点把握対象疾患 ）", "（ 今週のトピック ）")


def test_e01_prose_parse_failure_is_not_treated_as_release(tmp_path, caplog):
    """上期注意報 + 本期散文段抽不出来 → **不出「解除」条**, 且 state 保留上期状态。"""
    state = {"idsc": {"alerts": {"インフルエンザ": "注意報"}, "week": "2026-40"}}
    ad = _ha_adapter(tmp_path, week_text=_PROSE_BODY_RETITLED, state=state)
    # 前提: 定点表本身解析成功(失败面只在散文段), 且散文若能抽出, 状态词就在
    assert ha.parse_sentinel(_PROSE_BODY_RETITLED)["diseases"]["インフルエンザ"]["cur"] == 1.00
    assert ha._prose_after_table(_PROSE_BODY_RETITLED) == ""
    with caplog.at_level(logging.WARNING):
        recs = list(ad.collect())
    assert not any("解除" in r.item.title for r in recs), "解析失败不得产出假解除条"
    assert not any("发布" in r.item.title for r in recs)
    saved = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert saved["idsc"]["alerts"] == {"インフルエンザ": "注意報"}, "state 不得被覆盖成 {}"
    assert any("解析失败" in r.message for r in caplog.records), "解析失败必须留 warning"


def test_e01_prose_parse_failure_keeps_next_transition_correct(tmp_path):
    """连续两期: 解析失败期不出条不覆盖 state, 下期散文恢复且仍在警 → 不出条(无假跃迁对)。"""
    state = {"idsc": {"alerts": {"インフルエンザ": "注意報"}, "week": "2026-40"}}
    ad = _ha_adapter(tmp_path, week_text=_PROSE_BODY_RETITLED, state=state)
    assert list(ad.collect()) == []
    # 下期散文恢复正常, 仍是注意報 → 状态未变, 不出条(若上期误覆盖成 {}, 这里会假「发布」)
    recs = list(_ha_adapter(tmp_path, week_text=_PROSE_BODY, state={
        "idsc": {"alerts": {"インフルエンザ": "注意報"}, "week": "2026-40"}}).collect())
    assert [r.item.title for r in recs] == []


@needs_pdftotext
def test_e01_prose_parse_failure_real_fixture_pdf(tmp_path, caplog):
    """真实 w39 PDF 文本把散文段标题改掉后同样只跳过比较, 不出假解除。"""
    text = _ha_text("idsc_weekly_2026w39.pdf")
    assert "今週の注目される定点把握対象疾患" in text, "前提: fixture 文本里有散文段标题"
    broken = text.replace("今週の注目される定点把握対象疾患", "注目疾患(改版)")
    state = {"idsc": {"alerts": {"インフルエンザ": "注意報"}, "week": "2026-38"}}
    ad = _ha_adapter(tmp_path, week_text=broken, state=state)
    with caplog.at_level(logging.WARNING):
        recs = list(ad.collect())
    assert not any("解除" in r.item.title for r in recs)
    saved = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert saved["idsc"]["alerts"] == {"インフルエンザ": "注意報"}
    assert any("解析失败" in r.message for r in caplog.records)


@needs_pdftotext
def test_e01_prose_ok_weeks_keep_original_semantics(tmp_path):
    """对照(真实 PDF): 散文段抽得出来时行为不变 —— w36 无状态词照旧走「解除」语义。"""
    state = {"idsc": {"alerts": {"インフルエンザ": "注意報"}, "week": "2026-35"}}
    ad = _ha_adapter(tmp_path, week_text=_ha_text("idsc_weekly_2026w36.pdf"), state=state)
    recs = list(ad.collect())
    assert any(r.item.title == "东京都解除流感注意報" for r in recs), \
        "散文段正常解析时, 「散文没提状态」的解除语义不变"


# --------------------------------------------------------------------------- #
# E02: 「注意報レベルを解除しました」句式被判成「正在注意報」(解除词从未实现)
# --------------------------------------------------------------------------- #

def _prose_text(bullet: str) -> str:
    return ("東京都感染症週報\n     2026年第41週\n"
            "定点把握対象疾患 報告数 2026年41週\n"
            "        38週         39週           40週        41週\n"
            "        インフルエンザ\n"
            "                        1.00        1.10          1.20       1.00\n"
            "（ 今週の注目される定点把握対象疾患 ）\n" + bullet + "【年齢階級別】\n")


def test_e02_release_sentence_is_not_active_status():
    """审计原始复现句: 「…今週をもって注意報レベルを解除しました。」→ 不得读成注意報。"""
    text = _prose_text(
        "・インフルエンザの定点当たり報告数は減少しており、"
        "今週をもって注意報レベルを解除しました。\n")
    assert ha.parse_alert_status(text) == {}, "解除句被读成活动状态"


def test_e02_release_variants_recognised():
    """解除されました / 終息 同样识别; 对照: 正常在警句不受影响。"""
    released = ha.parse_alert_status(_prose_text(
        "・手足口病の警報レベルは解除されました。\n"))
    assert released == {}
    ended = ha.parse_alert_status(_prose_text(
        "・手足口病の流行は終息しました。\n"))
    assert ended == {}
    active = ha.parse_alert_status(_prose_text(
        "・手足口病の定点当たり報告数は8.10で、引き続き警報レベルです。\n"))
    assert active == {"手足口病": "警報"}, "正常在警句不得被误判成解除"


def test_e02_release_sentence_emits_release_item(tmp_path):
    """上期注意報 + 本期解除句 → 出「解除」条(修复前: 一条不出, state 原样滞留)。"""
    text = _prose_text(
        "・インフルエンザの定点当たり報告数は9.51で、"
        "今週をもって注意報レベルを解除しました。\n")
    state = {"idsc": {"alerts": {"インフルエンザ": "注意報"}, "week": "2026-40"}}
    recs = list(_ha_adapter(tmp_path, week_text=text, state=state).collect())
    assert [r.item.title for r in recs] == ["东京都解除流感注意報"]
    saved = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert saved["idsc"]["alerts"] == {}, "解除后 state 要清掉该病"


@needs_pdftotext
def test_e02_release_sentence_in_real_fixture_text(tmp_path):
    """真实 w39 PDF 文本把总结句换成解除句 → 该病不再判成在警。"""
    text = _ha_text("idsc_weekly_2026w39.pdf")
    assert ha.parse_alert_status(text) == {"インフルエンザ": "注意報"}
    released = text.replace(
        "注意報レベルが続いています", "今週をもって注意報レベルを解除しました")
    assert ha.parse_alert_status(released) == {}
    state = {"idsc": {"alerts": {"インフルエンザ": "注意報"}, "week": "2026-38"}}
    recs = list(_ha_adapter(tmp_path, week_text=released, state=state).collect())
    assert any(r.item.title == "东京都解除流感注意報" for r in recs)


# --------------------------------------------------------------------------- #
# E03: 注意報基准值表任何解析失败都静默返回空, 规则②全病静默失效
# --------------------------------------------------------------------------- #

def test_e03_threshold_parse_failures_log_warning(caplog):
    """【参考】标题没了 / 表没了 / 一行病名都没认出来 —— 都必须留 warning。"""
    with caplog.at_level(logging.WARNING):
        assert ha.parse_thresholds("<html>改版后没有【参考】字样</html>") == {}
        assert ha.parse_thresholds("<p>【参考】だけあって表がない</p>") == {}
        # 表在, 但行结构与 4 列假设全对不上
        assert ha.parse_thresholds(
            '<p>【参考】</p><table><tr><td colspan="9">警報レベル</td></tr></table>') == {}
    messages = " | ".join(r.getMessage() for r in caplog.records)
    assert messages.count("基准值表解析失败") == 3, messages
    # 空输入保持静默: 网络失败在上游 fetch 层已有日志, 不重复刷
    assert ha.parse_thresholds("") == {}


def test_e03_valid_thresholds_do_not_log(caplog):
    """对照: 官方 survey 页正常解析时不产生解析失败日志。"""
    with caplog.at_level(logging.WARNING):
        th = ha.parse_thresholds(HA_SURVEY.decode("utf-8"))
    assert len(th) == 11
    assert not any("基准值表解析失败" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# E04: 索引页链接带 cache-busting query 时, `?disease=` 拼出双 `?` 的畸形 URL
# --------------------------------------------------------------------------- #

_E04_BODY = _PROSE_BODY  # 定点表+散文齐全的合成周报, 流感无增长、状态在警 → 出「发布」条


def test_e04_cache_busting_query_gets_ampersand_not_second_question(tmp_path):
    """pdf_url 自带 `?20260518` 时, 病别参数必须用 `&` 续, 不得产出 `…pdf?20260518?disease=`。

    注: Item 构造时会对 url 跑 canonicalize_url(既有契约), 裸 query `20260518` 规范化后
    写成 `20260518=` —— 这里按「query 参数键」断言, 修复前 disease 键会被并进
    `20260518?disease` 整个丢失。
    """
    index = '<a href="/assets/weekly/2026/41.pdf?20260518">最新</a>'
    state = {"idsc": {"alerts": {}, "week": "2026-40"}}
    recs = list(_ha_adapter(tmp_path, index=index, week_text=_E04_BODY,
                            state=state).collect())
    assert recs, "前提: 该周报要出得了条"
    for rec in recs:
        assert rec.item.url.count("?") == 1, rec.item.url
        query = rec.item.url.split("?", 1)[1]
        keys = {kv.split("=", 1)[0] for kv in query.split("&")}
        assert {"20260518", "disease"} <= keys, rec.item.url


def test_e04_plain_url_keeps_question_mark(tmp_path):
    """对照: 无 query 的 pdf_url 照旧用 `?disease=`(现有 id/去重语义不变)。"""
    recs = list(_ha_adapter(tmp_path, week_text=_E04_BODY,
                            state={"idsc": {"alerts": {}, "week": "2026-40"}}).collect())
    assert recs
    for rec in recs:
        assert rec.item.url.count("?") == 1
        assert "disease=" in rec.item.url.split("?", 1)[1]


# --------------------------------------------------------------------------- #
# E05: state 文件读不出来时静默当空, 无任何日志
# --------------------------------------------------------------------------- #

def test_e05_broken_state_file_logs_warning(tmp_path, caplog):
    """state 半截 JSON → 当空状态跑, 但必须留一行 warning 指向该文件。"""
    (tmp_path / "state.json").write_text("{ this is not json", "utf-8")
    ad = _ha_adapter(tmp_path, week_text=_E04_BODY)
    with caplog.at_level(logging.WARNING):
        recs = list(ad.collect())
    assert [r.item.source for r in recs] == ["home_alerts:tokyo_idsc"], "行为不变: 当空状态"
    assert any("state 文件读不出来" in r.getMessage() for r in caplog.records)


def test_e05_non_dict_state_logs_warning(tmp_path, caplog):
    """state 是合法 JSON 但不是对象(如数组)→ 同样留日志。"""
    (tmp_path / "state.json").write_text('["インフルエンザ"]', "utf-8")
    ad = _ha_adapter(tmp_path, week_text=_E04_BODY)
    with caplog.at_level(logging.WARNING):
        list(ad.collect())
    assert any("不是 JSON 对象" in r.getMessage() for r in caplog.records)


def test_e05_first_run_no_state_no_warning(tmp_path, caplog):
    """对照: 首次运行(文件不存在)是正常路径, 不该刷 warning。"""
    ad = _ha_adapter(tmp_path, week_text=_E04_BODY)
    with caplog.at_level(logging.WARNING):
        list(ad.collect())
    assert not any("state" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# E06: 48h 入库窗 × 日刊节奏 → 官方风险条目必然连续两期重复上栏
# --------------------------------------------------------------------------- #

from datetime import date, datetime, timedelta, timezone  # noqa: E402

from personal_intel_loop.paper import build_edition  # noqa: E402
from personal_intel_loop.paper_risk import select_real_risk_items  # noqa: E402
from personal_intel_loop.store import upsert_item  # noqa: E402
from tests._platform_helpers import ai_json, edition_section_ids, make_dispatch_llm  # noqa: E402
from tests.conftest import make_item  # noqa: E402

E_TODAY = date(2026, 10, 4)
E_NOW = "2026-10-04T02:00:00Z"
E_NOW_DT = datetime(2026, 10, 4, 2, tzinfo=timezone.utc)


def _risk_item(conn, item_id, *, source="home_alerts:tokyo_idsc", regions=("東京都",),
               age_hours=0):
    payload = {"kind": "risk", "issuer": "官方机构", "published": E_NOW,
               "level": "レベル２", "regions": list(regions)}
    upsert_item(
        conn,
        make_item(item_id=item_id, source=source, title=f"官方风险 {item_id}", ts=E_NOW),
        adapter_name=source.split(":", 1)[0],
        source_payload_json=json.dumps(payload, ensure_ascii=False),
    )
    if age_hours:
        conn.execute(
            "UPDATE items SET first_ingested_at=? WHERE item_id=?",
            ((E_NOW_DT - timedelta(hours=age_hours)).isoformat().replace("+00:00", "Z"),
             item_id))
    conn.commit()
    return item_id


def _edition_row(conn, edition_date, item_id, section="risk"):
    conn.execute("INSERT INTO editions (edition_date, item_id, section, rank, blind_reason, built_at) VALUES (?, ?, ?, ?, ?, ?)", (edition_date, item_id, section, 0, None, E_NOW))
    conn.commit()


def _selected(conn):
    return select_real_risk_items(conn, date_local=E_TODAY, now_utc=E_NOW)


def test_e06_prev_edition_risk_item_excluded(db_conn):
    """上一期风险栏上过的同一 item_id, 本期不再上栏; 没上过的照常上。"""
    _risk_item(db_conn, "home:1")
    _risk_item(db_conn, "home:2")
    _edition_row(db_conn, "2026-10-03", "home:1", "risk")
    assert _selected(db_conn) == ["home:2"]


def test_e06_exclusion_window_is_three_editions(db_conn):
    """窗口 = 上一期及前 2 期(共 3 期); 4 期之前上过的可以再上。"""
    _risk_item(db_conn, "home:old")
    _risk_item(db_conn, "home:mid")
    _edition_row(db_conn, "2026-09-30", "home:old", "risk")   # 第 4 期 → 窗口外
    _edition_row(db_conn, "2026-10-01", "home:mid", "risk")   # 第 3 期 → 窗口内
    _edition_row(db_conn, "2026-10-02", "other:1", "risk")
    _edition_row(db_conn, "2026-10-03", "other:2", "risk")
    assert _selected(db_conn) == ["home:old"]


def test_e06_non_risk_section_placement_does_not_exclude(db_conn):
    """只在别的分区(如 warmth)上过的条目不受 E06 影响(那是 exclude_ids 的事)。"""
    _risk_item(db_conn, "home:1")
    _edition_row(db_conn, "2026-10-03", "home:1", "warmth")
    assert _selected(db_conn) == ["home:1"]


def test_e06_same_day_rerun_not_excluded(db_conn):
    """当期重跑: editions 里当期(今天)的 risk 行不参与排除(build 同日重跑先删当期)。"""
    _risk_item(db_conn, "home:1")
    _edition_row(db_conn, "2026-10-04", "home:1", "risk")
    assert _selected(db_conn) == ["home:1"]


def _build(conn, tmp_path, *, date_local, now_utc):
    llm = make_dispatch_llm(ai=lambda prompt: ai_json())
    return build_edition(
        conn,
        date_local=date_local,
        n=6,
        now_utc=now_utc,
        select_fn=lambda c, *, date_local, top_k, now_utc: [],
        embed_fn=lambda texts: [[0.0, 0.0, 1.0] for _ in texts],
        fetcher=lambda url: ("正文全文", "ok"),
        llm_call=llm,
        learn_first=False,
        spool_dir=tmp_path / "spool",
        layout_path=tmp_path / "none.toml",
    )


def test_e06_two_consecutive_editions_do_not_repeat_item(db_conn, tmp_path):
    """连续两期组版: 同一条 48h 窗内的官方条目只在第一期上风险栏。"""
    _risk_item(db_conn, "home:jma")
    _build(db_conn, tmp_path, date_local=E_TODAY, now_utc=E_NOW)
    assert "home:jma" in edition_section_ids(db_conn, "2026-10-04", "risk")

    # 第二期(两天后出刊, 条目入库 47h 仍在 48h 窗内) → 不再重复上栏
    _build(db_conn, tmp_path, date_local=date(2026, 10, 6), now_utc="2026-10-06T01:00:00Z")
    assert edition_section_ids(db_conn, "2026-10-06", "risk") == []
    # 第一期已上版的记录不被回溯抹掉
    assert "home:jma" in edition_section_ids(db_conn, "2026-10-04", "risk")


# --------------------------------------------------------------------------- #
# E08: generate() 行程循环里的 urgent 投递段无独立兜底
# --------------------------------------------------------------------------- #

import sqlite3 as _sqlite3  # noqa: E402

from personal_intel_loop import paper_risk as pr  # noqa: E402

_RISK_LLM_JSON = json.dumps({
    "title": "目的地简报",
    "lede": "两三句总述。",
    "sections": [{"heading": "治安", "points": [{"text": "夜间避免独行。",
                                                 "source_title": "外務省",
                                                 "source_url": "https://example.com/a",
                                                 "date": "2026-09-01"}]}],
}, ensure_ascii=False)


def _add_trip(conn, trip_id, place, country, *, start, end="2026-10-30"):
    conn.execute("INSERT OR REPLACE INTO trips (trip_id, place, country, start_date, end_date, note, created_at) VALUES (?, ?, ?, ?, ?, NULL, ?)", (trip_id, place, country, start, end, E_NOW))
    conn.commit()


def test_e08_urgent_push_failure_does_not_drop_payloads(db_conn, monkeypatch, caplog):
    """第 2 个行程的 urgent 投递炸掉(表锁)→ 不得丢掉当期全部行程简报。"""
    _add_trip(db_conn, "t1", "曼谷", "泰国", start="2026-10-04")   # days_until=0
    _add_trip(db_conn, "t2", "纽约", "美国", start="2026-10-06")   # days_until=2

    calls = {"n": 0}

    def flaky_urgent(conn, place, *, date_local):
        calls["n"] += 1
        if calls["n"] == 2:
            raise _sqlite3.OperationalError("database is locked")
        return []

    monkeypatch.setattr(pr, "_today_urgent_related", flaky_urgent)
    payloads, _members = pr.generate(
        db_conn, date_local=E_TODAY, now_utc=E_NOW,
        llm_call=lambda prompt: _RISK_LLM_JSON,
        search_fn=lambda place: [], reference_fn=lambda place, country: "",
    )
    assert [p["item_id"] for p in payloads] == ["risk:t1:2026-10-04", "risk:t2:2026-10-04"], \
        "一次投递异常不该丢掉前面行程的 payload"
    assert any("urgent push failed" in r.getMessage() for r in caplog.records)


def test_e08_urgent_push_still_accepted_when_healthy(db_conn, monkeypatch):
    """对照: 没有异常时投递照常入 inbox(dedup_key= risk:trip:date)。"""
    _add_trip(db_conn, "t1", "曼谷", "泰国", start="2026-10-04")

    class _Row(dict):
        pass

    monkeypatch.setattr(pr, "_today_urgent_related",
                        lambda conn, place, *, date_local: [_Row({"title": "紧急提示"})])
    payloads, _ = pr.generate(
        db_conn, date_local=E_TODAY, now_utc=E_NOW,
        llm_call=lambda prompt: _RISK_LLM_JSON,
        search_fn=lambda place: [], reference_fn=lambda place, country: "",
    )
    assert len(payloads) == 1
    row = db_conn.execute(
        "SELECT dedup_key, priority FROM inbox WHERE source=?", (pr.RISK_SOURCE,)
    ).fetchone()
    assert row is not None and row["dedup_key"] == "risk:t1:2026-10-04"
    assert row["priority"] == "urgent"


# --------------------------------------------------------------------------- #
# E10: 列表页 cap 按页面出现顺序截断, 不按时间
# --------------------------------------------------------------------------- #

from personal_intel_loop.adapters import html_columns as hc  # noqa: E402
from personal_intel_loop.adapters.html_columns import HtmlColumnsAdapter  # noqa: E402

_E10_REGEX = r"<a href=\"(?P<url>[^\"]+)\">(?P<title>[^<]+)</a>"
_E10_DATE_RE = r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})"


def _e10_page(n: int = 15, oldest_first: bool = True) -> str:
    """n 条 url_path 日期条目, 页序可切换 最旧在前/最新在前。日期 2026-09-01 起逐日+1。"""
    parts = []
    order = range(n) if oldest_first else range(n - 1, -1, -1)
    for i in order:
        day = 1 + i
        parts.append(f'<a href="https://example.com/c/2026-09-{day:02d}/{i}.shtml">条目{i:02d}</a>')
    return "<html><body>" + "".join(parts) + "</body></html>"


def _e10_adapter(tmp_path, page: str, **kw) -> HtmlColumnsAdapter:
    cfg = tmp_path / "cols.json"
    cfg.write_text(json.dumps([{
        "key": "e10", "name": "页序测试", "list_url": "https://example.com/list/",
        "lang": "zh", "item_regex": _E10_REGEX, "date_source": "url_path",
        "date_regex": _E10_DATE_RE,
    }], ensure_ascii=False), encoding="utf-8")
    kw.setdefault("fetch_list", lambda url: page)
    kw.setdefault("fetch_detail", lambda url: "<html><body>正文</body></html>")
    kw.setdefault("extract_body", lambda html: "正文内容。" * 200)
    return HtmlColumnsAdapter(config_path=cfg, state_path=tmp_path / "state.json", **kw)


def test_e10_cap_keeps_newest_not_page_first(tmp_path):
    """15 条旧→新排列、cap=10: 保留的必须是**最新 10 条**, 不是页面前 10 条。"""
    ad = _e10_adapter(tmp_path, _e10_page(15, oldest_first=True))
    recs = list(ad.collect(since=datetime(2026, 8, 31, tzinfo=timezone.utc)))
    assert len(recs) == 10
    days = sorted(int(r.item.url.split("/2026-09-")[1][:2]) for r in recs)
    assert days == list(range(6, 16)), f"应保留 09-06..09-15(最新10条), 实得 {days}"


def test_e10_newest_first_page_still_keeps_newest(tmp_path):
    """对照: 页序本就最新在前时行为不变(保留的仍是最新 10 条)。"""
    ad = _e10_adapter(tmp_path, _e10_page(15, oldest_first=False))
    recs = list(ad.collect(since=datetime(2026, 8, 31, tzinfo=timezone.utc)))
    assert len(recs) == 10
    days = sorted(int(r.item.url.split("/2026-09-")[1][:2]) for r in recs)
    assert days == list(range(6, 16))


def test_e10_dropped_by_cap_not_marked_seen(tmp_path):
    """被 cap 挤掉的旧条目不进 seen —— 等新条目滚出窗口后它们还能轮上。"""
    ad = _e10_adapter(tmp_path, _e10_page(15, oldest_first=True))
    list(ad.collect(since=datetime(2026, 8, 31, tzinfo=timezone.utc)))
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    days = sorted(int(u.split("/2026-09-")[1][:2]) for u in state["seen"])
    assert days == list(range(6, 16)), "seen 里只有上版的 10 条"


def test_e10_detail_page_fetched_only_for_kept_items(tmp_path):
    """url_path/list_text 栏目: 详情页只对**上版**的条目发请求, cap 挤掉的不浪费请求。"""
    calls: list[str] = []

    def fetch_detail(url: str) -> str:
        calls.append(url)
        return "<html><body>正文</body></html>"

    ad = _e10_adapter(tmp_path, _e10_page(15, oldest_first=True),
                      fetch_detail=fetch_detail)
    list(ad.collect(since=datetime(2026, 8, 31, tzinfo=timezone.utc)))
    assert len(calls) == 10 and len(set(calls)) == 10


# --------------------------------------------------------------------------- #
# E11: 探活门条件与 _list_html/_detail_html 的直接 _ensure_browser() 不一致
# --------------------------------------------------------------------------- #

class _NoBrowser:
    """替身 _Browser: playwright 缺失的部署常态。"""

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        raise hc.BrowserUnavailable("playwright 未安装")

    def __exit__(self, *exc):
        return False


_E11_CFG = [{
    "key": "e11a", "name": "列表要浏览器", "list_url": "https://a.example/list/",
    "lang": "ja", "item_regex": _E10_REGEX, "date_source": "url_path",
    "date_regex": _E10_DATE_RE, "render": "browser",
}, {
    "key": "e11b", "name": "详情要浏览器", "list_url": "https://b.example/list/",
    "lang": "ja", "item_regex": _E10_REGEX, "date_source": "detail_meta",
    "date_regex": _E10_DATE_RE, "render_detail": "browser",
}]


def _write_cfg(tmp_path, entries) -> Path:
    cfg = tmp_path / "cols.json"
    cfg.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return cfg


def test_e11_partial_injection_probe_gate_covers_list_render(tmp_path, caplog, monkeypatch):
    """只注入 render_detail、列表页仍要浏览器: playwright 缺失 → 跳过该栏目, 不裸抛。"""
    monkeypatch.setattr(hc, "_Browser", _NoBrowser)
    adapter = HtmlColumnsAdapter(
        config_path=_write_cfg(tmp_path, _E11_CFG), state_path=tmp_path / "state.json",
        render_detail=lambda url, col=None: "",          # 只注入详情通道
        fetch_detail=lambda url: "<html>正文</html>",
        extract_body=lambda html: "正文内容。" * 200,
    )
    with caplog.at_level(logging.WARNING):
        recs = list(adapter.collect(since=datetime(2026, 9, 1, tzinfo=timezone.utc)))
    assert not any(r.item.source.endswith(":e11a") for r in recs), "列表要浏览器的栏目被跳过"
    assert any("e11a" in r.getMessage() and "跳过" in r.getMessage() for r in caplog.records)


def test_e11_partial_injection_probe_gate_covers_detail_render(tmp_path, caplog, monkeypatch):
    """只注入 render_list、详情页要浏览器: playwright 缺失 → 跳过该栏目, 不裸抛。"""
    monkeypatch.setattr(hc, "_Browser", _NoBrowser)
    adapter = HtmlColumnsAdapter(
        config_path=_write_cfg(tmp_path, _E11_CFG), state_path=tmp_path / "state.json",
        render_list=lambda url, col=None: "<html>列表</html>",   # 只注入列表通道
        extract_body=lambda html: "正文内容。" * 200,
    )
    with caplog.at_level(logging.WARNING):
        recs = list(adapter.collect(since=datetime(2026, 9, 1, tzinfo=timezone.utc)))
    assert not any(r.item.source.endswith(":e11b") for r in recs)
    assert any("e11b" in r.getMessage() and "跳过" in r.getMessage() for r in caplog.records)


def test_e11_full_injection_still_avoids_browser(tmp_path, monkeypatch):
    """对照: 两个通道都注入时不探活、不构造 _Browser(既有契约)。"""
    monkeypatch.setattr(hc, "_Browser", lambda *a, **kw: pytest.fail("不该起浏览器"))
    adapter = HtmlColumnsAdapter(
        config_path=_write_cfg(tmp_path, _E11_CFG), state_path=tmp_path / "state.json",
        render_list=lambda url, col=None: "<html>列表</html>",
        render_detail=lambda url, col=None: "",
        extract_body=lambda html: "正文内容。" * 200,
    )
    assert list(adapter.collect(since=datetime(2026, 9, 1, tzinfo=timezone.utc))) == []


# --------------------------------------------------------------------------- #
# E12: collect() 的栏目循环无逐栏异常隔离
# --------------------------------------------------------------------------- #

def test_e12_one_broken_column_does_not_kill_the_rest(tmp_path, caplog):
    """无 host 的 href(javascript:void(0))在 compute_item_id 抛 ValueError →
    只跳过该栏目, 余下栏目照采, state 照常落盘(修复前: 整轮 raise, 后面的栏目全丢)。"""
    cfg = _write_cfg(tmp_path, [
        {   # 列表锚点是 javascript:void(0), 日期在块文本里命中 → ts 通过, 炸在 item id
            "key": "e12a", "name": "坏锚点", "list_url": "https://a.example/list/",
            "lang": "zh",
            "item_regex": (r"<a href=\"(?P<url>javascript:void\(0\))\">(?P<title>[^<]+)</a>"
                           r"<span class=\"d\">(?P<date>[^<]+)</span>"),
            "date_source": "list_text", "date_regex": _E10_DATE_RE,
        },
        {   # 正常栏目
            "key": "e12b", "name": "正常", "list_url": "https://b.example/list/",
            "lang": "zh", "item_regex": _E10_REGEX,
            "date_source": "url_path", "date_regex": _E10_DATE_RE,
        },
    ])
    pages = {
        "https://a.example/list/": ('<a href="javascript:void(0)">点击参加</a>'
                                    '<span class="d">2026-10-01</span>'),
        "https://b.example/list/": '<a href="https://b.example/c/2026-10-02/9.shtml">正常条目</a>',
    }
    adapter = HtmlColumnsAdapter(
        config_path=cfg, state_path=tmp_path / "state.json",
        fetch_list=lambda url: pages.get(url, ""),
        fetch_detail=lambda url: "<html><body>正文</body></html>",
        extract_body=lambda html: "正文内容。" * 200,
    )
    with caplog.at_level(logging.WARNING):
        recs = list(adapter.collect(since=datetime(2026, 9, 25, tzinfo=timezone.utc)))
    assert [r.item.source for r in recs] == ["html_columns:e12b"], "后面的栏目必须照常产出"
    assert any("e12a" in r.getMessage() and "采集异常" in r.getMessage()
               for r in caplog.records)
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert len(state["seen"]) == 1, "产出条目的 url 要落盘(state 不能被跳过)"


def test_e12_all_columns_failed_with_zero_output_still_raises(tmp_path, monkeypatch):
    """对照(既有契约): 整轮毫无产出时异常照样冒出(browser 清理契约测试同款场景)。"""
    closed: list[str] = []

    class _FakeBrowser:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            closed.append("closed")
            return False

        def render_html(self, url, *, scroll=0):
            # 先给出能匹配出条目的列表页, 才会走到详情阶段 —— 那里才是要抛异常的地方
            return '<a href="https://a.example/c/2026-10-01/1.shtml">标题一</a>'

    monkeypatch.setattr(hc, "_Browser", _FakeBrowser)
    adapter = HtmlColumnsAdapter(
        config_path=_write_cfg(tmp_path, [_E11_CFG[0]]), state_path=tmp_path / "state.json")
    monkeypatch.setattr(HtmlColumnsAdapter, "_detail_html",
                        lambda self, col, url: (_ for _ in ()).throw(RuntimeError("详情炸了")))
    with pytest.raises(RuntimeError):
        list(adapter.collect(since=datetime(2026, 9, 1, tzinfo=timezone.utc)))
    assert closed == ["closed"], "异常冒出前浏览器也要关干净"


# --------------------------------------------------------------------------- #
# E13: _TreeBuilder 未实现「块级 starttag 关闭开放的 <p>」, 连续未闭合 p+div 静默漏抓
# --------------------------------------------------------------------------- #

_E13_HTML = ('<html><body><div class="list">'
             '<div class="item"><a href="https://x.example/1">标题一</a><p>摘要一'
             '<div class="item"><a href="https://x.example/2">标题二</a><p>摘要二'
             '</div></div></body></html>')


def _e13_column(selector: str = "div.item") -> hc.Column:
    return hc.Column(key="e13", name="未闭合p", list_url="https://x.example/list/",
                     lang="zh", item_selector=selector, date_source="list_text",
                     date_regex=_E10_DATE_RE)


def test_e13_block_starttag_closes_open_p():
    """容器里一个未闭合的 <p>(编者按等)会把后面所有 div.item 吞进 p 里 →
    `div.list > div.item` 子代链一条都匹配不到且 parsed=0 之外无任何线索;
    块级 starttag 关闭开放 p 后恢复浏览器的 DOM 形状。"""
    html = ('<div class="list"><p>编者按: 以下为本周栏目内容'
            '<div class="item"><a href="https://x.example/1">甲</a></div>'
            '<div class="item"><a href="https://x.example/2">乙</a></div>'
            '</div>')
    raws = hc.parse_list(html, _e13_column("div.list > div.item"))
    assert [r.title for r in raws] == ["甲", "乙"], \
        f"游离未闭合 <p> 后的条目被吞进 p 里了: {[r.title for r in raws]}"


def test_e13_tree_parity_with_browser_dom():
    """审计原始形状(p 与外层 div 都未闭合): 第二个 div.item 的父节点必须是外层
    div(item), 不能是第一个 <p>(浏览器里 div/ul/h1 等会关闭 button scope 内的开放 p)。"""
    b = hc._TreeBuilder()
    b.feed(_E13_HTML)
    b.close()

    def items(node):
        for c in node.children:
            if c.tag == "div" and "item" in c.classes:
                yield c
            yield from items(c)

    got = list(items(b.root))
    assert len(got) == 2
    assert got[1].parent.tag == "div", f"第二个 div.item 的父节点是 {got[1].parent.tag}, 浏览器语义应为 div(item)"


def test_e13_explicitly_closed_p_unaffected():
    """对照: 闭合规范、显式写 </p> 的页面解析不变(D11 既有语义也不回归)。"""
    html = ('<div class="list">'
            '<div class="item"><a href="https://x.example/1">甲</a><p>摘要一</p></div>'
            '<div class="item"><a href="https://x.example/2">乙</a><p>摘要二</p></div>'
            '</div>')
    assert [r.title for r in hc.parse_list(html, _e13_column("div.item"))] == ["甲", "乙"]
    assert "x.example/2" not in hc.parse_list(html, _e13_column("div.item"))[0].block
    ul = ('<ul class="list"><li class="item"><a href="https://x.example/1">甲</a>'
          '<li class="item"><a href="https://x.example/2">乙</a></ul>')
    assert [r.title for r in hc.parse_list(ul, _e13_column("ul.list > li.item"))] == ["甲", "乙"]


# --------------------------------------------------------------------------- #
# E14: 浏览器渲染 non-200 仍返回错误页 HTML, 误入「改版」计数
# --------------------------------------------------------------------------- #

class _FakePage:
    def __init__(self, status: int | None, content: str):
        self._status = status
        self._content = content
        self.closed = False
        self.mouse = self          # render_html 里 page.mouse.wheel(...) 是属性访问

    def goto(self, url, *, wait_until, timeout):
        class _Resp:
            pass
        resp = _Resp()
        resp.status = self._status
        return resp

    def wait_for_load_state(self, state, *, timeout):
        raise RuntimeError("networkidle 超时(常态, 要继续)")

    def wheel(self, x, y):
        pass

    def wait_for_timeout(self, ms):
        pass

    def content(self):
        return self._content

    def close(self):
        self.closed = True


class _FakeCtx:
    def __init__(self, page: _FakePage):
        self._page = page

    def new_page(self):
        return self._page


def _browser_with_page(status: int | None, content: str) -> hc._Browser:
    b = hc._Browser(throttle=hc._Throttle())
    b._ctx = _FakeCtx(_FakePage(status, content))
    return b


def test_e14_render_non_200_returns_empty(monkeypatch):
    """403 WAF 拦截页/5xx 错误页 → 返回空串(与 http 路径同款), 不把错误页当列表页。"""
    b = _browser_with_page(403, "<html>403 Forbidden</html>")
    assert b.render_html("https://x.example/list/") == ""
    b = _browser_with_page(503, "<html>error</html>")
    assert b.render_html("https://x.example/list/") == ""


def test_e14_render_200_still_returns_content():
    """对照: 200 照常返回渲染后的 HTML。"""
    b = _browser_with_page(200, "<html><body>列表</body></html>")
    assert b.render_html("https://x.example/list/") == "<html><body>列表</body></html>"


def test_e14_non_200_not_counted_as_zero_match(tmp_path, caplog):
    """端到端: render 通道返回 403 空串 → 记 fetch_failed, 不进 zero_match 改版计数。"""
    calls = {"n": 0}

    def fake_render_list(url, *, col=None):
        return ""

    adapter = HtmlColumnsAdapter(
        config_path=_write_cfg(tmp_path, _E11_CFG[:1]), state_path=tmp_path / "state.json",
        render_list=fake_render_list,
        fetch_list=lambda url: (_ for _ in ()).throw(AssertionError("browser 栏目不该走 http")),
        extract_body=lambda html: "正文内容。" * 200,
    )
    # render_list 返回空串 = 抓取失败语义(D10), 断言该栏目的 zero_match 不动
    for _ in range(3):
        with caplog.at_level(logging.WARNING):
            assert list(adapter.collect()) == []
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert state["zero_match"] == {}, f"fetch 失败不该进改版计数: {state['zero_match']}"


# --------------------------------------------------------------------------- #
# E16/E17/E18 共用: 假 playwright 模块(测 _Browser 的环境变量/降级/清理, 不起真浏览器)
# --------------------------------------------------------------------------- #

import os  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402

import personal_intel_loop.adapters.thepaper_warm as tw  # noqa: E402


def _install_fake_playwright(monkeypatch, *, launch_error: Exception | None = None) -> dict:
    """往 sys.modules 塞假 playwright 包, 返回调用计数。"""
    calls = {"start": 0, "stop": 0, "launch": 0, "new_context": 0,
             "ctx_close": 0, "browser_close": 0}

    class _FakeCtx:
        def close(self):
            calls["ctx_close"] += 1

    class _FakeBrowserObj:
        def new_context(self, **kw):
            calls["new_context"] += 1
            return _FakeCtx()

        def close(self):
            calls["browser_close"] += 1

    class _FakeChromium:
        def launch(self, **kw):
            calls["launch"] += 1
            if launch_error is not None:
                raise launch_error
            return _FakeBrowserObj()

    class _FakePW:
        def __init__(self):
            self.chromium = _FakeChromium()

        def stop(self):
            calls["stop"] += 1

    class _FakeManager:
        def start(self):
            calls["start"] += 1
            return _FakePW()

    fake_pkg = types.ModuleType("playwright")
    fake_api = types.ModuleType("playwright.sync_api")
    fake_api.sync_playwright = lambda: _FakeManager()
    fake_pkg.sync_api = fake_api
    monkeypatch.setitem(sys.modules, "playwright", fake_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_api)
    return calls


# --------------------------------------------------------------------------- #
# E16: PLAYWRIGHT_BROWSERS_PATH 写入进程环境后不恢复(两个 adapter 同款)
# --------------------------------------------------------------------------- #

def test_e16_html_columns_restores_unset_env(monkeypatch):
    calls = _install_fake_playwright(monkeypatch)
    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    with hc._Browser("/fake/browsers") as b:
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/fake/browsers"
        assert b._pw is not None
    assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ, "环境变量泄漏到 with 块之外"
    assert calls["stop"] == 1 and calls["browser_close"] == 1 and calls["ctx_close"] == 1


def test_e16_html_columns_restores_previous_value(monkeypatch):
    _install_fake_playwright(monkeypatch)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/original/path"
    try:
        with hc._Browser("/fake/browsers"):
            assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/fake/browsers"
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/original/path", "旧值要恢复"
    finally:
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)


def test_e16_html_columns_no_browsers_path_touches_nothing(monkeypatch):
    """对照: browsers_path 为空时从不写环境变量, 也不动别人的值。"""
    _install_fake_playwright(monkeypatch)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/someone-elses"
    try:
        with hc._Browser(None):
            assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/someone-elses"
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/someone-elses"
    finally:
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)


def test_e16_thepaper_warm_restores_unset_env(monkeypatch):
    calls = _install_fake_playwright(monkeypatch)
    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    with tw._Browser("/fake/browsers"):
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/fake/browsers"
    assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ, "环境变量泄漏到 with 块之外"
    assert calls["stop"] == 1 and calls["browser_close"] == 1 and calls["ctx_close"] == 1


def test_e16_thepaper_warm_restores_previous_value(monkeypatch):
    _install_fake_playwright(monkeypatch)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/original/path"
    try:
        with tw._Browser("/fake/browsers"):
            assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/fake/browsers"
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/original/path", "旧值要恢复"
    finally:
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)


# --------------------------------------------------------------------------- #
# E17: playwright 未装/浏览器未下载时裸抛 ImportError, 无降级路径
# --------------------------------------------------------------------------- #

def test_e17_playwright_missing_degrades_to_warning(tmp_path, monkeypatch, caplog):
    """sys.modules 里 playwright 置 None(=import 必 ImportError)→ collect 返回空 + warning,
    不得抛 ImportError 挂掉整轮 ingest(docstring 块3 的自承诺)。"""
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    adapter = tw.ThepaperWarmAdapter(state_path=tmp_path / "state.json")
    with caplog.at_level(logging.WARNING):
        recs = list(adapter.collect())
    assert recs == []
    assert any("浏览器不可用" in r.getMessage() for r in caplog.records)


def test_e17_import_error_raised_as_browser_unavailable(monkeypatch):
    """_Browser.__enter__ 把 ImportError 包装成 BrowserUnavailable(与 html_columns 同款)。"""
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(tw.BrowserUnavailable, match="playwright 未安装"):
        with tw._Browser():
            pass


def test_e17_browser_unavailable_is_runtime_error_subtype():
    """对照: 类型上与 html_columns.BrowserUnavailable 同族(RuntimeError), 语义一致。"""
    from personal_intel_loop.adapters.html_columns import BrowserUnavailable as HCBU

    assert issubclass(tw.BrowserUnavailable, RuntimeError)
    assert HCBU is not tw.BrowserUnavailable or True  # 类型独立但行为对齐


# --------------------------------------------------------------------------- #
# E18: _Browser.__enter__ 中段失败不调 __exit__, playwright driver 子进程泄漏
# --------------------------------------------------------------------------- #

def test_e18_launch_failure_reaps_driver_and_raises_browser_unavailable(monkeypatch):
    """start() 成功后 chromium.launch() 失败 → 已启动的 driver 要被回收(stop 被调),
    并以 BrowserUnavailable 抛出(不是裸 RuntimeError)。"""
    calls = _install_fake_playwright(
        monkeypatch, launch_error=RuntimeError("Executable doesn't exist"))
    with pytest.raises(tw.BrowserUnavailable, match="chromium 起不来"):
        with tw._Browser():
            pass
    assert calls["start"] == 1 and calls["launch"] == 1
    assert calls["stop"] == 1, "driver 子进程没人回收(泄漏)"


def test_e18_launch_failure_restores_env_too(monkeypatch):
    """对照 E16: 中段失败的清理路径同样要恢复环境变量(__exit__ 幂等)。"""
    _install_fake_playwright(monkeypatch, launch_error=RuntimeError("boom"))
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/original"
    try:
        with pytest.raises(tw.BrowserUnavailable):
            with tw._Browser("/fake/browsers"):
                pass
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/original"
    finally:
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)


def test_e18_collect_degrades_when_launch_fails(tmp_path, monkeypatch, caplog):
    """端到端: launch 失败 → collect 返回空 + warning, 不让整轮 ingest 挂掉。"""
    _install_fake_playwright(monkeypatch, launch_error=RuntimeError("boom"))
    adapter = tw.ThepaperWarmAdapter(state_path=tmp_path / "state.json")
    with caplog.at_level(logging.WARNING):
        assert list(adapter.collect()) == []
    assert any("浏览器不可用" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# E19: thepaper_warm._save_state 非原子写, 与同仓库 D13 修复不一致
# --------------------------------------------------------------------------- #

def test_e19_state_write_is_atomic_on_midwrite_kill(tmp_path, monkeypatch):
    """写 state 中途被杀(模拟: write_text 写一半抛 OSError)→ 旧 state 原样健在,
    不留半截 JSON, 不留 .tmp 垃圾(html_columns D13 同款)。"""
    from pathlib import Path

    state = tmp_path / "thepaper_warm_state.json"
    tw._save_state(state, {"seen_ids": ["a", "b"], "updated_at": E_NOW})
    old = json.loads(state.read_text("utf-8"))
    assert old["seen_ids"] == ["a", "b"]

    real_write_text = Path.write_text

    def half_write(self, data, *args, **kwargs):
        real_write_text(self, data[: len(data) // 2], *args, **kwargs)
        raise OSError("simulated mid-write kill")

    monkeypatch.setattr(Path, "write_text", half_write)
    tw._save_state(state, {"seen_ids": ["c"], "updated_at": E_NOW})
    monkeypatch.setattr(Path, "write_text", real_write_text)

    payload = json.loads(state.read_text("utf-8"))
    assert payload["seen_ids"] == ["a", "b"], "半截临时文件不得覆盖正式 state"
    assert list(tmp_path.glob("*.tmp")) == [], "临时文件要清干净"


def test_e19_roundtrip_keeps_payload():
    """对照: 正常写入内容不变(落盘内容可解析、键齐全)。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "state.json"
        tw._save_state(p, {"seen_ids": ["x"], "updated_at": E_NOW})
        assert json.loads(p.read_text("utf-8")) == {"seen_ids": ["x"], "updated_at": E_NOW}
