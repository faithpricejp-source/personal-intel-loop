"""`paper_risk.default_search_fn` 的多语别名匹配 + payload/ENSO 纳入(设计规格第二节第 2 项),
以及 `generate` 的 `{reference}` 段注入(第 4 项)。

用 conftest 的内存 sqlite(`db_conn`)造条目, 官方源的 `source_payload_json` 按
`adapters/{mofa_anzen,who_don,cn_consular,enso_status}` 实际落库的形状写:
`{"kind": "risk", "issuer": ..., "regions": [...]}`(ENSO 的 regions 是 `[]`)。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from personal_intel_loop.paper_risk import (
    ENSO_SOURCE_PREFIX,
    RISK_ENSO_DAYS,
    RISK_RESULT_LIMIT,
    default_reference_fn,
    default_search_fn,
    generate,
)
from personal_intel_loop.store import upsert_item
from tests.conftest import make_item

NOW = "2026-10-04T00:00:00Z"
TODAY = date(2026, 10, 4)
NOW_DT = datetime(2026, 10, 4, tzinfo=timezone.utc)

RISK_JSON = json.dumps(
    {
        "title": "曼谷行前风险",
        "lede": "证据尚可。",
        "sections": [{"heading": "治安", "points": [{"text": "夜间避免独自行走", "source_title": "FCDO", "source_url": "https://x", "date": "2026-09-28"}]}],
    },
    ensure_ascii=False,
)


def _add(conn, item_id, *, title="", body="", source="rss_briefing:feed", ts=NOW, payload=None, age_days=0):
    """落一条 item。age_days 用来把 first_ingested_at 往前推(测时间窗)。"""
    upsert_item(
        conn,
        make_item(item_id=item_id, source=source, title=title, body=body, ts=ts),
        adapter_name="test",
        source_payload_json=json.dumps(payload, ensure_ascii=False) if payload is not None else "{}",
    )
    if age_days:
        conn.execute(
            "UPDATE items SET first_ingested_at=? WHERE item_id=?",
            ((NOW_DT - timedelta(days=age_days)).isoformat().replace("+00:00", "Z"), item_id),
        )
    conn.commit()


def _risk_payload(regions, issuer="日本外務省"):
    return {"kind": "risk", "issuer": issuer, "published": NOW, "regions": regions, "level": "レベル２"}


def _ids(entries):
    return [e["item_id"] for e in entries]


# --------------------------------------------------------------------------- #
# 别名 LIKE: 一种写法匹配不到就等于没风险证据
# --------------------------------------------------------------------------- #

def test_japanese_alias_matches_for_chinese_typed_place(db_conn):
    """用户填「曼谷」, 库里外务省条目正文写「バンコク」/「タイ」—— 旧实现匹配不到。"""
    _add(db_conn, "item:ja", title="危険情報", body="バンコクでの犯罪が多発しています")
    _add(db_conn, "item:th", title="洪水警報", body="タイ国内で豪雨")
    got = _ids(default_search_fn(db_conn, "曼谷", now_utc=NOW))
    assert "item:ja" in got, "日文别名BAN コク 没命中"
    assert "item:th" in got, "国级别名「タイ」没命中"


def test_english_alias_matches(db_conn):
    _add(db_conn, "item:en", title="Thailand warns of flooding", body="Bangkok")
    assert "item:en" in _ids(default_search_fn(db_conn, "曼谷", now_utc=NOW))


def test_country_only_typed_place_matches_country_terms(db_conn):
    """用户只填「泰国」: 命中写国名任一语言的条目。

    注意不会顺带把某座城市的别名混进来(见 test_place_aliases 的对应断言)——
    只填国家时不知道要去哪座城市, 不该把「曼谷」的条目当成泰国必看的证据。
    """
    _add(db_conn, "item:cn", title="泰国旅游提示", body="")
    _add(db_conn, "item:ja", title="タイの洪水警報", body="")
    _add(db_conn, "item:en", title="Thailand travel advisory", body="")
    _add(db_conn, "item:bkk", title="バンコクの犯罪状況", body="")
    got = _ids(default_search_fn(db_conn, "泰国", now_utc=NOW))
    assert {"item:cn", "item:ja", "item:en"} <= set(got)
    assert "item:bkk" not in got, "只填国家时不该把某座城市的条目拉进来"


def test_city_name_alone_resolves_country_aliases(db_conn):
    """只填「纽约」也要能命中写「United States」或「アメリカ」的条目。"""
    _add(db_conn, "item:us", title="United States travel advisory", body="")
    _add(db_conn, "item:jp", title=" Lump: アメリカ合衆国", body="")
    got = _ids(default_search_fn(db_conn, "纽约", now_utc=NOW))
    assert "item:us" in got and "item:jp" in got


def test_same_kana_written_differently_still_matches(db_conn):
    _add(db_conn, "item:seoul", title="ソウルで窃盗被害", body="")
    assert "item:seoul" in _ids(default_search_fn(db_conn, "首尔", now_utc=NOW))


def test_unrelated_place_not_matched(db_conn):
    _add(db_conn, "item:jp", title="バンコクの犯罪", body="")
    assert _ids(default_search_fn(db_conn, "首尔", now_utc=NOW)) == []


def test_country_argument_widens_match(db_conn):
    """行程表只填了城市名认不出时, 国家列能帮忙定国家别名。"""
    _add(db_conn, "item:th", title="タイの注意", body="")
    got = _ids(default_search_fn(db_conn, "somewhere local", country="タイ", now_utc=NOW))
    assert "item:th" in got


def test_empty_place_returns_empty(db_conn):
    _add(db_conn, "item:jp", title="バンコク", body="")
    assert default_search_fn(db_conn, "", now_utc=NOW) == []
    assert default_search_fn(db_conn, "   ", now_utc=NOW) == []


def test_older_than_window_excluded(db_conn):
    _add(db_conn, "item:old", title="バンコクの犯罪", body="", age_days=45)
    assert _ids(default_search_fn(db_conn, "曼谷", now_utc=NOW)) == []


def test_return_shape_unchanged(db_conn):
    _add(db_conn, "item:a", title="バンコクの犯罪", body="", source="mofa_anzen:タイ", ts="2026-10-01T03:04:05Z")
    entry = default_search_fn(db_conn, "曼谷", now_utc=NOW)[0]
    assert set(entry) == {"item_id", "title", "url", "source", "date"}, "返回结构不能变(下游 paper.py 依赖)"
    assert entry["date"] == "2026-10-01"
    assert entry["source"] == "mofa_anzen:タイ"


# --------------------------------------------------------------------------- #
# kind=="risk" 且 regions 命中 —— 不受 LIKE 限制
# --------------------------------------------------------------------------- #

def test_regions_hit_included_even_without_place_in_text(db_conn):
    """外务省条目标题正文一个字都不提地名, 地区名只在 source_payload.regions 里。"""
    _add(
        db_conn,
        "item:mofa",
        title="Several areas in the country are under travel advisory",
        body="Please see the website for details.",
        source="mofa_anzen:タイ",
        payload=_risk_payload(["タイ"]),
    )
    assert "item:mofa" in _ids(default_search_fn(db_conn, "曼谷", now_utc=NOW))


def test_regions_hit_for_any_alias_spelling(db_conn):
    _add(db_conn, "item:cn", title="安全提醒", body="请注意", source="cn_consular:泰国",
         payload=_risk_payload(["泰国"], issuer="中国领事服务网"))
    _add(db_conn, "item:who", title="Cholera update - Thailand", body="", source="who_don:Thailand",
         payload=_risk_payload(["Thailand"], issuer="WHO"))
    got = _ids(default_search_fn(db_conn, "曼谷", now_utc=NOW))
    assert "item:cn" in got, "regions 写中文国名也要命中"
    assert "item:who" in got, "regions 写英文国名也要命中"


def test_regions_mismatch_excluded(db_conn):
    _add(db_conn, "item:other", title=" advisory", body="", source="mofa_anzen:ベトナム",
         payload=_risk_payload(["ベトナム"]))
    assert _ids(default_search_fn(db_conn, "曼谷", now_utc=NOW)) == []


def test_non_risk_payload_with_matching_regions_excluded(db_conn):
    """kind 不是 risk 的条目即便 regions 命中也不纳入 —— 那是别的栏的语义。

    标题正文刻意不含任何地名别名, 所以「被纳入」只可能来自 regions 通道。
    """
    _add(db_conn, "item:plain", title="採点 관련 정보（第3号）", body="本文には地名がobusくありません",
         source="rss_briefing:feed", payload={"kind": "candidate", "regions": ["タイ"]})
    assert _ids(default_search_fn(db_conn, "曼谷", now_utc=NOW)) == []


def test_broken_payload_json_does_not_raise(db_conn):
    upsert_item(
        db_conn,
        make_item(item_id="item:broken", title="バンコク", body=""),
        adapter_name="test",
        source_payload_json="{ not json",
    )
    conn_first = db_conn.execute("SELECT 1").fetchone()
    assert conn_first is not None
    got = _ids(default_search_fn(db_conn, "曼谷", now_utc=NOW))
    assert "item:broken" in got, "LIKE 命中即可, payload 坏掉不该让整次搜索抛"


def test_regions_not_a_list_does_not_raise(db_conn):
    _add(db_conn, "item:weird", title="x", body="", payload={"kind": "risk", "regions": "タイ"})
    assert isinstance(default_search_fn(db_conn, "曼谷", now_utc=NOW), list)


def test_wide_area_enso_regions_empty_not_matched_by_region(db_conn):
    """広域条目 regions=["広域"] 不该被普通目的地匹配(但它会被 ENSO 规则之外的 LIKE 抓到时另说)。"""
    _add(db_conn, "item:wide", title="広域情報", body="中東情勢", source="mofa_anzen:広域",
         payload=_risk_payload(["広域"]))
    assert "item:wide" not in _ids(default_search_fn(db_conn, "曼谷", now_utc=NOW))


# --------------------------------------------------------------------------- #
# ENSO: 气候状态对任何目的地都相关
# --------------------------------------------------------------------------- #

def test_enso_items_always_included(db_conn):
    _add(db_conn, "item:enso1", title="ENSO watch", body="La Niña", source=f"{ENSO_SOURCE_PREFIX}watch")
    _add(db_conn, "item:enso2", title="ENSO outlook", body="", source=f"{ENSO_SOURCE_PREFIX}outlook")
    got = _ids(default_search_fn(db_conn, "首尔", now_utc=NOW))
    assert "item:enso1" in got and "item:enso2" in got, "气候状态与目的地无关, 必须总纳入"


def test_enso_latest_two_only(db_conn):
    for i in range(5):
        _add(db_conn, f"item:enso{i}", title=f"ENSO {i}", body="",
             source=f"{ENSO_SOURCE_PREFIX}f{i}", ts=f"2026-10-0{i + 1}T00:00:00Z")
    got = _ids(default_search_fn(db_conn, "首尔", now_utc=NOW))
    enso = [i for i in got if i.startswith("item:enso")]
    assert len(enso) == 2, f"只该取最新两条, 实际 {enso}"
    assert set(enso) == {"item:enso3", "item:enso4"}, "要最新的两条"


def test_enso_outside_45_days_excluded(db_conn):
    _add(db_conn, "item:enso_old", title="ENSO old", body="", source=f"{ENSO_SOURCE_PREFIX}old",
         age_days=RISK_ENSO_DAYS + 5)
    assert "item:enso_old" not in _ids(default_search_fn(db_conn, "首尔", now_utc=NOW))


def test_enso_within_45_days_included_even_beyond_search_days(db_conn):
    """ENSO 窗(45 天)比普通搜索窗(30 天)宽: 35 天前的 ENSO 仍要进。"""
    _add(db_conn, "item:enso35", title="ENSO 35d", body="", source=f"{ENSO_SOURCE_PREFIX}d35",
         age_days=35)
    got = _ids(default_search_fn(db_conn, "首尔", now_utc=NOW))
    assert "item:enso35" in got


def test_non_enso_source_with_similar_prefix_not_included(db_conn):
    _add(db_conn, "item:other", title="x", body="", source="enso_status_extra:x")
    assert "item:other" not in _ids(default_search_fn(db_conn, "首尔", now_utc=NOW))


# --------------------------------------------------------------------------- #
# 合并/去重/排序/上限
# --------------------------------------------------------------------------- #

def test_same_item_from_two_sources_deduped(db_conn):
    """一条既命中 LIKE 又 regions 命中, 只出现一次。"""
    _add(db_conn, "item:dup", title="バンコクの注意", body="", source="mofa_anzen:タイ",
         payload=_risk_payload(["タイ"]))
    got = _ids(default_search_fn(db_conn, "曼谷", now_utc=NOW))
    assert got.count("item:dup") == 1


def test_results_sorted_by_ts_desc(db_conn):
    _add(db_conn, "item:old", title="バンコク a", body="", ts="2026-09-20T00:00:00Z")
    _add(db_conn, "item:new", title="バンコク b", body="", ts="2026-10-03T00:00:00Z")
    _add(db_conn, "item:mid", title="バンコク c", body="", ts="2026-09-30T00:00:00Z")
    got = _ids(default_search_fn(db_conn, "曼谷", now_utc=NOW))
    assert got == ["item:new", "item:mid", "item:old"]


def test_result_limit_caps(db_conn):
    for i in range(20):
        _add(db_conn, f"item:x{i:02d}", title=f"バンコク {i}", body="",
             ts=f"2026-10-{(i % 9) + 1:02d}T00:00:00Z")
    got = default_search_fn(db_conn, "曼谷", now_utc=NOW)
    assert len(got) == RISK_RESULT_LIMIT
    assert RISK_RESULT_LIMIT == 10


def test_days_parameter_respected(db_conn):
    _add(db_conn, "item:d20", title="バンコク x", body="", age_days=20)
    assert _ids(default_search_fn(db_conn, "曼谷", now_utc=NOW, days=30)) == ["item:d20"]
    assert _ids(default_search_fn(db_conn, "曼谷", now_utc=NOW, days=10)) == []


# --------------------------------------------------------------------------- #
# generate: {reference} 段与 reference_fn
# --------------------------------------------------------------------------- #

def _make_trip(conn, place, country, start="2026-10-20", end="2026-10-25"):
    from personal_intel_loop.paper_api import create_trip

    create_trip(conn, {"place": place, "country": country, "start_date": start, "end_date": end}, now_utc=NOW)


def test_generate_injects_reference_into_prompt(db_conn):
    _make_trip(db_conn, "曼谷", "泰国")
    seen = {}

    def llm(prompt):
        seen["prompt"] = prompt
        return RISK_JSON

    generate(
        db_conn,
        date_local=TODAY,
        now_utc=NOW,
        llm_call=llm,
        search_fn=lambda place: [],
        reference_fn=lambda place, country: "# 官方参考资料摘录\n Domingos原文: バンコクの犯罪多発",
    )
    assert "官方参考资料摘录" in seen["prompt"]
    assert "バンコクの犯罪多発" in seen["prompt"], "参考块原文要进提示词"
    assert "注明机构与日期" in seen["prompt"], "提示词要说明引用时注机构与日期"


def test_generate_prompt_mentions_stale_date_caveat(db_conn):
    _make_trip(db_conn, "曼谷", "泰国")
    seen = {}
    generate(
        db_conn,
        date_local=TODAY,
        now_utc=NOW,
        llm_call=lambda p: seen.setdefault("prompt", p) and RISK_JSON,
        search_fn=lambda place: [],
        reference_fn=lambda place, country: "旧摘录",
    )
    assert "较旧" in seen["prompt"]


def test_reference_fn_receives_place_and_country(db_conn):
    _make_trip(db_conn, "曼谷", "泰国")
    calls = []
    generate(
        db_conn,
        date_local=TODAY,
        now_utc=NOW,
        llm_call=lambda p: RISK_JSON,
        search_fn=lambda place: [],
        reference_fn=lambda place, country: calls.append((place, country)) or "REF",
    )
    assert ("曼谷", "泰国") in calls


def test_empty_reference_renders_placeholder(db_conn):
    _make_trip(db_conn, "曼谷", "泰国")
    seen = {}
    generate(
        db_conn,
        date_local=TODAY,
        now_utc=NOW,
        llm_call=lambda p: seen.setdefault("prompt", p) and RISK_JSON,
        search_fn=lambda place: [],
        reference_fn=lambda place, country: "",
    )
    section = seen["prompt"].split("## 目的地官方参考资料")[1].split("## 要求")[0]
    assert "(无)" in section


def test_reference_fn_failure_does_not_break_generation(db_conn):
    _make_trip(db_conn, "曼谷", "泰国")

    def boom(place, country):
        raise RuntimeError("ref dir exploded")

    payloads, _members = generate(
        db_conn,
        date_local=TODAY,
        now_utc=NOW,
        llm_call=lambda p: RISK_JSON,
        search_fn=lambda place: [],
        reference_fn=boom,
    )
    assert len(payloads) == 1, "参考块取不到不该让整期简报失败"


def test_generate_without_reference_fn_uses_default_and_survives(db_conn):
    """不传 reference_fn 时走 default_reference_fn; DATA_DIR/risk_reference 不存在 → 空串。"""
    _make_trip(db_conn, "曼谷", "泰国")
    seen = {}
    payloads, _ = generate(
        db_conn,
        date_local=TODAY,
        now_utc=NOW,
        llm_call=lambda p: seen.setdefault("prompt", p) and RISK_JSON,
        search_fn=lambda place: [],
    )
    assert len(payloads) == 1
    assert "## 目的地官方参考资料" in seen["prompt"]


def test_default_reference_fn_empty_without_ref_dir():
    from personal_intel_loop import risk_reference

    if risk_reference.default_ref_dir().is_dir():
        pytest.skip("DATA_DIR/risk_reference 已存在, 跳过")
    assert default_reference_fn("曼谷", "泰国") == ""


def test_default_reference_fn_with_injected_ref_dir(tmp_path):
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "risk_ref"
    import shutil

    shutil.copytree(fixture, tmp_path / "risk_reference")
    out = default_reference_fn("曼谷", "泰国", ref_dir=tmp_path / "risk_reference")
    assert "危険レベル" in out


def test_search_fn_injection_still_wins(db_conn):
    """注入了 search_fn 就只用它(旧行为不变), 不去查库。"""
    _make_trip(db_conn, "曼谷", "泰国")
    _add(db_conn, "item:ja", title="バンコクの犯罪", body="")
    payloads, members = generate(
        db_conn,
        date_local=TODAY,
        now_utc=NOW,
        llm_call=lambda p: RISK_JSON,
        search_fn=lambda place: [{"item_id": "item:fake", "title": "t", "url": None, "source": "s", "date": "2026-10-01"}],
    )
    assert members[payloads[0]["item_id"]] == ["item:fake"]


def test_default_search_used_when_no_search_fn(db_conn):
    _make_trip(db_conn, "曼谷", "泰国")
    _add(db_conn, "item:ja", title="バンコクの犯罪", body="")
    _add(db_conn, "item:enso", title="ENSO", body="", source=f"{ENSO_SOURCE_PREFIX}x")
    payloads, members = generate(db_conn, date_local=TODAY, now_utc=NOW, llm_call=lambda p: RISK_JSON)
    ids = members[payloads[0]["item_id"]]
    assert "item:ja" in ids and "item:enso" in ids


def test_risk_prompt_has_reference_placeholder():
    from personal_intel_loop.paper_risk import RISK_PROMPT

    assert "{reference}" in RISK_PROMPT
    assert "官方参考资料" in RISK_PROMPT
