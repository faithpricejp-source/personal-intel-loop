"""契约第 9 节: 风险栏 = 当期所有 `kind:"risk"` 条目。设计规格第二节第 1/2/3 项。

采集器入库的官方风险条目(`items.source_payload_json` 里 `"kind": "risk"`, 来自 `home_alerts`、
`mofa_anzen`、`who_don`、`cn_consular`、`enso_status`, 带 `regions` 列表)以前一条都上不了风险栏
—— 风险栏只放 `paper_risk.generate` 生成的行前简报合成条目。常驻东京的读者没填行程时风险栏永远空。

这些官方源的地区名常只出现在 payload 的 `regions` 里, 标题正文一个字都不提, 所以选中靠 regions
命中常驻地/行程地别名, 不走 LIKE。

用 conftest 的内存 sqlite(`db_conn`)造条目; `first_ingested_at` 由 `upsert_item` 写成「现在」,
`_add(age_hours=...)` 把它往前推来测 48 小时窗口。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from personal_intel_loop.paper import build_edition
from personal_intel_loop.paper_risk import (
    HOME_ALERTS_SOURCE_PREFIX,
    RISK_REAL_CAP,
    RISK_REAL_WINDOW_HOURS,
    default_home_terms,
    default_trip_terms,
    select_real_risk_items,
)
from personal_intel_loop.store import upsert_item
from tests._platform_helpers import NOW, ai_json, edition_section_ids, make_dispatch_llm
from tests.conftest import make_item

TODAY = date(2026, 10, 4)
NOW_DT = datetime(2026, 10, 4, 2, tzinfo=timezone.utc)


def _add(conn, item_id, *, source="mofa_anzen:jp", regions=(), age_hours=0, title="", payload_extra=None, ts=NOW):
    """落一条 `kind:"risk"` 条目。`age_hours` 把 first_ingested_at 往前推(测 48h 窗口)。"""
    payload = {"kind": "risk", "issuer": "官方机构", "published": NOW, "level": "レベル２"}
    if regions is not None:
        payload["regions"] = list(regions)
    payload.update(payload_extra or {})
    upsert_item(
        conn,
        make_item(item_id=item_id, source=source, title=title or f"官方风险 {item_id}", ts=ts),
        adapter_name=source.split(":", 1)[0],
        source_payload_json=json.dumps(payload, ensure_ascii=False),
    )
    if age_hours:
        conn.execute(
            "UPDATE items SET first_ingested_at=? WHERE item_id=?",
            ((NOW_DT - timedelta(hours=age_hours)).isoformat().replace("+00:00", "Z"), item_id),
        )
    conn.commit()
    return item_id


def _add_trip(conn, trip_id, place, country, *, start="2026-10-20", end="2026-10-25"):
    conn.execute(
        "INSERT OR REPLACE INTO trips (trip_id, place, country, start_date, end_date, note, created_at)"
        " VALUES (?, ?, ?, ?, ?, NULL, ?)",
        (trip_id, place, country, start, end, NOW),
    )
    conn.commit()


def _selected(conn, **kwargs):
    kwargs.setdefault("date_local", TODAY)
    kwargs.setdefault("now_utc", NOW)
    return select_real_risk_items(conn, **kwargs)


# --------------------------------------------------------------------------- #
# home_alerts: 常驻地, 一律入选
# --------------------------------------------------------------------------- #

def test_home_alerts_always_selected(db_conn):
    """常驻地的灾害预警本来就是低频高价值, 不看 regions 也上版(这里是普通四级)。"""
    _add(db_conn, "home:jma", source=f"{HOME_ALERTS_SOURCE_PREFIX}jma_souten", regions=[])
    assert _selected(db_conn) == ["home:jma"]


def test_home_alerts_wins_over_trip_and_enso(db_conn):
    """排序: home_alerts > 行程地 > ENSO。"""
    _add_trip(db_conn, "t1", "曼谷", "泰国")
    _add(db_conn, "enso:1", source="enso_status:jma", regions=[])
    _add(db_conn, "trip:th", source="mofa_anzen:th", regions=["泰国"])
    _add(db_conn, "home:1", source=f"{HOME_ALERTS_SOURCE_PREFIX}tokyo_idsc", regions=["東京都"])
    assert _selected(db_conn) == ["home:1", "trip:th", "enso:1"]


# --------------------------------------------------------------------------- #
# 无关国家 / 无命中地区: 不入选
# --------------------------------------------------------------------------- #

def test_unrelated_country_not_selected(db_conn):
    """没有行程、regions 写的是巴西 —— 与常驻地无关, 不该进风险栏。"""
    _add(db_conn, "br:1", source="mofa_anzen:br", regions=["ブラジル", "Brazil"])
    assert _selected(db_conn) == []


def test_home_alerts_selected_even_with_unrelated_regions(db_conn):
    """home_alerts 一律入选这条规则优先于 regions 判定(否则灾害预警会漏)。"""
    _add(db_conn, "home:1", source=f"{HOME_ALERTS_SOURCE_PREFIX}jma_souten", regions=["-centric_elsewhere"])
    assert _selected(db_conn) == ["home:1"]


def test_non_risk_payload_never_selected(db_conn):
    """只有 `kind:"risk"` 的条目进风险栏; 普通 rss 条目即使 regions 命中也不进。"""
    upsert_item(
        db_conn,
        make_item(item_id="plain:1", source="rss_briefing:feed", title="东京下雪了"),
        adapter_name="rss_briefing",
        source_payload_json=json.dumps({"feed_name": "某 feed"}, ensure_ascii=False),
    )
    db_conn.commit()
    assert _selected(db_conn) == []


# --------------------------------------------------------------------------- #
# 行程地命中
# --------------------------------------------------------------------------- #

def test_trip_region_hit_selected(db_conn):
    """行程地(曼谷)的官方条目: 用户填中文, 库里的日文/英文别名也算命中。"""
    _add_trip(db_conn, "t1", "曼谷", "泰国")
    _add(db_conn, "th:ja", source="mofa_anzen:th", regions=["タイ"])
    assert _selected(db_conn) == ["th:ja"]


def test_trip_region_hit_via_country_alias(db_conn):
    """只写国家别名(タイ)也算命中行程地。"""
    _add_trip(db_conn, "t1", "Bangkok", "Thailand")
    _add(db_conn, "th:en", source="who_don:global", regions=["Thailand"])
    assert _selected(db_conn) == ["th:en"]


def test_trip_hit_requires_upcoming_trip(db_conn):
    """行程已结束(不在 upcoming_trips 里)则其地区不再算命中。"""
    _add_trip(db_conn, "old", "曼谷", "泰国", start="2026-01-01", end="2026-01-10")
    _add(db_conn, "th:1", source="mofa_anzen:th", regions=["泰国"])
    assert _selected(db_conn) == []


def test_home_region_hit_without_any_trip(db_conn):
    """常驻地的官方风险条目(非 home_alerts 源, 如外务省对日本的提醒)命中常驻地别名 → 入选。
    这正是设计规格说的「读者没填行程时风险栏也不再空」。"""
    _add(db_conn, "jp:mofa", source="mofa_anzen:jp", regions=["日本"])
    got = _selected(db_conn)
    assert got == ["jp:mofa"]


# --------------------------------------------------------------------------- #
# ENSO: 只在发布后首次出现(近 48h 新入库)
# --------------------------------------------------------------------------- #

def test_enso_selected_when_fresh(db_conn):
    _add(db_conn, "enso:1", source="enso_status:jma", regions=[])
    assert _selected(db_conn) == ["enso:1"]


def test_enso_not_selected_when_older_than_window(db_conn):
    """ENSO 条目只在发布后首次出现时入选; 超过 48 小时入库的不再重复上版。"""
    _add(db_conn, "enso:old", source="enso_status:jma", regions=[], age_hours=RISK_REAL_WINDOW_HOURS + 1)
    assert _selected(db_conn) == []


# --------------------------------------------------------------------------- #
# 48 小时窗口
# --------------------------------------------------------------------------- #

def test_item_older_than_window_excluded(db_conn):
    _add(db_conn, "home:old", source=f"{HOME_ALERTS_SOURCE_PREFIX}jma_souten", regions=[], age_hours=RISK_REAL_WINDOW_HOURS + 1)
    assert _selected(db_conn) == []


def test_item_just_inside_window_included(db_conn):
    """窗口是 48 小时: 47 小时前入库的还在, 49 小时前的不在。"""
    _add(db_conn, "home:in", source=f"{HOME_ALERTS_SOURCE_PREFIX}jma_souten", regions=[], age_hours=RISK_REAL_WINDOW_HOURS - 1)
    assert _selected(db_conn) == ["home:in"]


# --------------------------------------------------------------------------- #
# 上限与排序
# --------------------------------------------------------------------------- #

def test_cap_truncates_after_tier_order(db_conn):
    """上限在按档位排好之后截断 —— 留的是档位最高的, 不是入库最早的。"""
    for index in range(4):
        _add(db_conn, f"enso:{index}", source="enso_status:jma", regions=[], age_hours=index)
    _add(db_conn, "home:1", source=f"{HOME_ALERTS_SOURCE_PREFIX}jma_souten", regions=[])
    got = _selected(db_conn, cap=2)
    assert got == ["home:1", "enso:0"], "档位优先: home_alerts 第一, ENSO 里留最新入库的"


def test_default_cap_is_five(db_conn):
    for index in range(RISK_REAL_CAP + 3):
        _add(db_conn, f"home:{index}", source=f"{HOME_ALERTS_SOURCE_PREFIX}jma_souten", regions=[])
    assert len(_selected(db_conn)) == RISK_REAL_CAP


def test_cap_none_returns_all(db_conn):
    for index in range(7):
        _add(db_conn, f"home:{index}", source=f"{HOME_ALERTS_SOURCE_PREFIX}jma_souten", regions=[])
    assert len(_selected(db_conn, cap=None)) == 7


def test_same_tier_sorted_newest_first(db_conn):
    """同档内按入库时间新到旧。"""
    _add(db_conn, "home:old", source=f"{HOME_ALERTS_SOURCE_PREFIX}jma_souten", regions=[], age_hours=5)
    _add(db_conn, "home:new", source=f"{HOME_ALERTS_SOURCE_PREFIX}jma_souten", regions=[], age_hours=1)
    assert _selected(db_conn) == ["home:new", "home:old"]


# --------------------------------------------------------------------------- #
# 去重: 已在别区上版的
# --------------------------------------------------------------------------- #

def test_excluded_ids_dropped_before_cap(db_conn):
    """已在别区上版的先剔除再截断 —— 免得占掉名额把没上版的挤掉。"""
    _add(db_conn, "home:a", source=f"{HOME_ALERTS_SOURCE_PREFIX}jma_souten", regions=[], age_hours=3)
    _add(db_conn, "home:b", source=f"{HOME_ALERTS_SOURCE_PREFIX}jma_souten", regions=[], age_hours=2)
    _add(db_conn, "home:c", source=f"{HOME_ALERTS_SOURCE_PREFIX}jma_souten", regions=[], age_hours=1)
    got = _selected(db_conn, cap=2, exclude_ids={"home:b"})
    assert got == ["home:c", "home:a"], "被排除的不占名额"


# --------------------------------------------------------------------------- #
# home_terms / trip_terms 取值
# --------------------------------------------------------------------------- #

def test_default_home_terms_expand_home_place():
    """别名已归一化(大小写折叠), 所以是 `japan` / `tokyo` 而不是 `Japan` / `Tokyo`。"""
    terms = default_home_terms()
    assert {"東京", "日本", "japan", "东京", "tokyo"} <= terms


def test_default_trip_terms_union_over_trips(db_conn):
    _add_trip(db_conn, "t1", "曼谷", "泰国")
    _add_trip(db_conn, "t2", "纽约", "美国")
    terms = default_trip_terms(db_conn, date_local=TODAY)
    assert "泰国" in terms and "美国" in terms


def test_explicit_terms_override_defaults(db_conn):
    """显式传入 terms 时不再看 HOME_PLACE 与 trips。"""
    _add_trip(db_conn, "t1", "曼谷", "泰国")
    _add(db_conn, "th:1", source="mofa_anzen:th", regions=["泰国"])
    got = _selected(db_conn, home_terms=set(), trip_terms=set())
    assert got == [], "空 terms 下只有 home_alerts 能入选"


# --------------------------------------------------------------------------- #
# build_edition 级: 风险栏里真的出现真实条目
# --------------------------------------------------------------------------- #

def _build(conn, tmp_path, *, layout=None):
    llm = make_dispatch_llm(ai=lambda prompt: ai_json())
    return build_edition(
        conn,
        date_local=TODAY,
        n=6,
        now_utc=NOW,
        select_fn=lambda c, *, date_local, top_k, now_utc: [],
        embed_fn=lambda texts: [[0.0, 0.0, 1.0] for _ in texts],
        fetcher=lambda url: ("正文全文", "ok"),
        llm_call=llm,
        learn_first=False,
        spool_dir=tmp_path / "spool",
        layout_path=layout or (tmp_path / "none.toml"),
    )


def test_build_edition_risk_section_contains_real_items(db_conn, tmp_path):
    """风险栏里出现真实条目(在 items 表里, 不是 risk: 合成的)。"""
    _add(db_conn, "home:jma", source=f"{HOME_ALERTS_SOURCE_PREFIX}jma_souten", regions=["東京都", "東京"], title="气象厅发布早期天候信息")
    _add(db_conn, "br:1", source="mofa_anzen:br", regions=["ブラジル"], title="巴西 somewhere")
    stats = _build(db_conn, tmp_path)

    risk_ids = edition_section_ids(db_conn, TODAY.isoformat(), "risk")
    assert "home:jma" in risk_ids, "常驻地官方预警没上风险栏"
    assert "br:1" not in risk_ids, "无关国家条目不该上风险栏"
    assert stats["risk"] == len(risk_ids)


def test_build_edition_real_risk_item_is_not_duplicated(db_conn, tmp_path):
    """真实风险条目只落风险栏一个分区。"""
    _add(db_conn, "home:jma", source=f"{HOME_ALERTS_SOURCE_PREFIX}jma_souten", regions=["東京都"])
    _build(db_conn, tmp_path)
    rows = db_conn.execute(
        "SELECT section FROM editions WHERE edition_date=? AND item_id='home:jma'",
        (TODAY.isoformat(),),
    ).fetchall()
    assert [row["section"] for row in rows] == ["risk"]


def test_build_edition_real_risk_cap_from_layout(db_conn, tmp_path):
    """`[layout] risk_real` 生效。"""
    layout = tmp_path / "layout.toml"
    layout.write_text("[layout]\nrisk_real = 1\n\n[warmth]\nsources = []\n", encoding="utf-8")
    for index in range(3):
        _add(db_conn, f"home:{index}", source=f"{HOME_ALERTS_SOURCE_PREFIX}jma_souten", regions=[])
    _build(db_conn, tmp_path, layout=layout)
    assert len(edition_section_ids(db_conn, TODAY.isoformat(), "risk")) == 1


def test_build_edition_real_risk_item_appears_in_api(db_conn, tmp_path):
    """真实条目照常走版面: editions 里有行, 且 get_editions 能取到完整 Item。"""
    from personal_intel_loop.paper_api import get_editions

    _add(db_conn, "home:jma", source=f"{HOME_ALERTS_SOURCE_PREFIX}jma_souten", regions=["東京都"], title="气象厅发布关东甲信早期天候信息")
    _build(db_conn, tmp_path)
    edition = get_editions(db_conn, before="2026-10-05", limit=1, profile_path=tmp_path / "profile.md")["editions"][0]
    titles = [item["title"] for item in edition["sections"]["risk"]]
    assert "气象厅发布关东甲信早期天候信息" in titles