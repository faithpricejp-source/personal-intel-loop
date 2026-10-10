"""who_don: 只收最近 N 天的 DON, regions 抽不到就空着(不编), 老条目 DonId 为空退回 Id。

fixture 是 2026-10-04 实抓 `?$orderby=PublicationDate desc&$top=5` 的返回, 删到 5 条。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from personal_intel_loop.adapters import who_don as wd
from personal_intel_loop.adapters.who_don import WhoDonAdapter

FIX = Path(__file__).parent / "fixtures" / "risk"
RAW = json.loads((FIX / "who_don.json").read_text("utf-8"))
#: 2026-10-05 实抓的完整 API 响应(`?$orderby=PublicationDate desc&$top=20`, 13 条)。
#: 旧 fixture 是删到 5 条的同标题单国形态, 真实站点的主力标题形态(`,`/`&` 并列多国)
#: 在旧 fixture 里一条都没有 —— 正是 R08 出 bug 的那一侧。
LIVE = json.loads((FIX / "who_don_live.json").read_text("utf-8"))


def _adapter(tmp_path, **kw):
    kw.setdefault("fetch_api", lambda since: "")
    state = tmp_path if tmp_path.name == "state.json" else tmp_path / "state.json"
    return WhoDonAdapter(state_path=state, **kw)


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


# ---- 解析 ------------------------------------------------------------------

def test_parse_api_field_mapping():
    entries = wd.parse_api(_dump(RAW))
    assert len(entries) == 5
    first = entries[0]
    assert first["don_id"] == "2026-DON618"
    assert first["title"] == ("Ebola disease caused by Bundibugyo virus - "
                              "Democratic Republic of the Congo")
    assert first["ts"] == datetime(2026, 9, 25, 15, 30, 18, tzinfo=timezone.utc)
    # ItemDefaultUrl "/2026-DON618" -> item 页 URL(不是 who.int 根下, 那个 404)
    assert first["url"] == ("https://www.who.int/emergencies/disease-outbreak-news/"
                            "item/2026-DON618")
    assert "Bundibugyo virus" in first["body"]


def test_parse_api_html_body_is_plain_text():
    entry = wd.parse_api(_dump(RAW))[0]
    for junk in ("<p", "paraid", "text-align", "&nbsp;", "</span>"):
        assert junk not in entry["body"]


def test_parse_api_broken_returns_empty():
    assert wd.parse_api("") == []
    assert wd.parse_api("<html>maintenance</html>") == []
    assert wd.parse_api('{"value": "not a list"}') == []
    assert wd.parse_api('{"records": []}') == []


def test_old_entry_without_donid_falls_back_to_id():
    raw = {"value": [{"Id": "abc-123", "DonId": "", "Title": "Polio in Congo",
                      "PublicationDate": "2010-11-04T00:00:00Z",
                      "ItemDefaultUrl": "/2010_11_04-en", "Summary": "x"}]}
    entry = wd.parse_api(_dump(raw))[0]
    assert entry["guid"] == "abc-123"
    assert entry["don_id"] == ""
    assert entry["url"].endswith("/item/2010_11_04-en")


def test_regions_from_title():
    assert wd.regions_from_title(
        "Ebola disease caused by Bundibugyo virus - Democratic Republic of the Congo"
    ) == ["Democratic Republic of the Congo"]
    assert wd.regions_from_title("Avian influenza – situation in Egypt") == ["Egypt"]
    # 去掉 update 尾巴之后取地区
    assert wd.regions_from_title("Avian influenza - situation in Egypt - update 4") == ["Egypt"]
    # 整句都是疾病名 -> 抽不出, 老实空着
    assert wd.regions_from_title("Middle East respiratory syndrome coronavirus (MERS-CoV) - update") == []
    assert wd.regions_from_title("") == []


# ---- collect ---------------------------------------------------------------

def test_collect_field_mapping(tmp_path):
    recs = list(_adapter(tmp_path, fetch_api=lambda s: _dump(RAW),
                         days=3650).collect())
    assert len(recs) == 5
    top = recs[0]
    assert top.item.source == "who_don"
    assert top.item.lang == "en"
    assert top.item.author == wd.ISSUER
    assert top.item.ts == datetime(2026, 9, 25, 15, 30, 18, tzinfo=timezone.utc)

    payload = json.loads(top.source_payload_json)
    assert payload["kind"] == "risk"
    assert payload["issuer"] == wd.ISSUER
    assert payload["published"] == "2026-09-25T15:30:18Z"
    assert payload["regions"] == ["Democratic Republic of the Congo"]
    assert payload["level"] is None
    assert payload["don_id"] == "2026-DON618"


def test_collect_sorted_newest_first(tmp_path):
    recs = list(_adapter(tmp_path, fetch_api=lambda s: _dump(RAW), days=3650).collect())
    assert [r.item.ts for r in recs] == sorted((r.item.ts for r in recs), reverse=True)


def test_collect_time_window(tmp_path, monkeypatch):
    """默认 14 天窗口(实测日 2026-10-05): 只有 09-25 那条在窗内, 08/14 两条掉出去。"""
    class FixtureDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 5, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(wd, "datetime", FixtureDateTime)
    recs = list(_adapter(tmp_path, fetch_api=lambda s: _dump(RAW)).collect())
    assert [r.item.url.rsplit("/", 1)[-1] for r in recs] == ["2026-DON618"]
    # 阳性对照: 同一份数据放宽到 3650 天就有 5 条(换 state 文件, 否则被上一轮去重掉)
    wide = _adapter(tmp_path / "wide", fetch_api=lambda s: _dump(RAW), days=3650)
    assert len(list(wide.collect())) == 5


def test_collect_since_wins_over_days(tmp_path):
    recs = list(_adapter(tmp_path, fetch_api=lambda s: _dump(RAW), days=1).collect(
        since=datetime(2026, 8, 15, tzinfo=timezone.utc)))
    assert [r.item.url.rsplit("/", 1)[-1] for r in recs] == [
        "2026-DON618", "2026-DON617", "2026-DON616"]


def test_collect_dedup_by_don_id(tmp_path):
    first = _adapter(tmp_path, fetch_api=lambda s: _dump(RAW), days=3650)
    assert len(list(first.collect())) == 5
    second = _adapter(tmp_path, fetch_api=lambda s: _dump(RAW), days=3650)
    assert list(second.collect()) == []


def test_collect_fetch_failure_returns_empty_not_raise(tmp_path):
    assert list(_adapter(tmp_path, fetch_api=lambda s: "").collect()) == []
    assert list(_adapter(tmp_path, fetch_api=lambda s: "<html>503</html>").collect()) == []
    assert list(_adapter(tmp_path, fetch_api=lambda s: "{broken").collect()) == []


def test_collect_limit(tmp_path):
    recs = list(_adapter(tmp_path, fetch_api=lambda s: _dump(RAW), days=3650).collect(limit=2))
    assert len(recs) == 2


def test_query_string_shape():
    q = wd._build_query(datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert q["$orderby"] == "PublicationDate desc"   # 不排序会拿到 2006 年旧条目
    assert q["$filter"] == "PublicationDate ge 2026-09-01T00:00:00Z"
    assert q["$top"] == str(wd.TOP)
    assert "DonId" in q["$select"]


def test_state_broken_file_not_raise(tmp_path):
    bad = tmp_path / "state.json"
    bad.write_text("{not json", "utf-8")
    recs = list(_adapter(tmp_path, fetch_api=lambda s: _dump(RAW), days=3650).collect())
    assert len(recs) == 5


# ---- 10-05 复审 R07 · limit 截断在 seen 标记之后 -----------------------------

def test_collect_limit_does_not_swallow_dropped_entries(tmp_path):
    """`limit` 落选的条目**不进 seen** —— 下一轮不限量时必须还能拿到。

    修复前: 全部 guid 先入 state 再截断, 落选条目下一轮被 `guid in seen` 跳过 → 永久丢失。
    """
    first = list(_adapter(tmp_path, fetch_api=lambda s: _dump(RAW), days=3650).collect(limit=2))
    assert len(first) == 2

    second = list(_adapter(tmp_path, fetch_api=lambda s: _dump(RAW), days=3650).collect())
    assert len(second) == 3, "落选的 3 条被 seen 吞掉了"
    assert not ({r.item.id for r in first} & {r.item.id for r in second})

    # 第三轮: 5 条都见过了 -> 0 条(阳性对照)
    assert list(_adapter(tmp_path, fetch_api=lambda s: _dump(RAW),
                         days=3650).collect()) == []


# ---- 10-05 复审 R08 · 多国并列标题抽不出 regions -----------------------------

def test_regions_from_live_titles():
    """真实 API 响应里的标题(R08 的 6/13 落空那批)必须抽出地区。"""
    entries = {e["don_id"]: e for e in wd.parse_api(_dump(LIVE))}
    # 并列两国, `,` + `&`
    assert entries["2026-DON613"]["regions"] == [
        "Democratic Republic of the Congo", "Uganda"]
    assert entries["2026-DON612"]["regions"] == [
        "Democratic Republic of the Congo", "Uganda"]
    # `Multi-locations` 不是地名(整句只有并列符, 前半截是疾病名) -> 落空
    assert entries["2026-DON611"]["regions"] == []
    assert entries["2026-DON610"]["regions"] == []      # Yellow fever - Global
    # 单国形态不受影响
    assert entries["2026-DON609"]["regions"] == ["India"]


def test_regions_from_title_multi_country_forms():
    assert wd.regions_from_title(
        "Ebola disease caused by Bundibugyo virus, "
        "Democratic Republic of the Congo & Uganda"
    ) == ["Democratic Republic of the Congo", "Uganda"]
    assert wd.regions_from_title(
        "Cholera - Somalia, Kenya and Ethiopia"
    ) == ["Somalia", "Kenya", "Ethiopia"]
    assert wd.regions_from_title("Cholera - Somalia and Kenya") == ["Somalia", "Kenya"]
    # 全是疾病名 -> 抽不出, 老实空着(不把疾病名当地区)
    assert wd.regions_from_title(
        "Ebola disease caused by Bundibugyo virus, outbreak and cases"
    ) == []


def test_regions_from_title_rejects_non_place_phrases():
    """非地名短语不得被当成地区(修复前靠单词表 fullmatch 挡不住)。"""
    assert wd.regions_from_title("Yellow fever - Global") == []
    assert wd.regions_from_title("Monkeypox - multi-country outbreak") == []
    assert wd.regions_from_title("Hantavirus outbreak linked to cruise ship travel, "
                                 "Multi-locations") == []
    # 阳性对照: 真地名照旧
    assert wd.regions_from_title("Nipah virus disease - India") == ["India"]


def test_collect_live_response_regions_not_empty(tmp_path):
    """端到端: 真实响应里近期疫情那批的 regions 必须落进 payload。"""
    recs = list(_adapter(tmp_path, fetch_api=lambda s: _dump(LIVE), days=3650).collect())
    by_don = {json.loads(r.source_payload_json)["don_id"]: r for r in recs}
    assert json.loads(by_don["2026-DON613"].source_payload_json)["regions"] == [
        "Democratic Republic of the Congo", "Uganda"]


# ---- 10-05 复审 R09 · naive since 随宿主 TZ 漂移 ------------------------------

def test_collect_naive_since_is_tz_independent(monkeypatch, tmp_path):
    """naive `--since` 的解释不能随宿主 TZ 变化(实测差 9 小时)。"""
    import time as _time

    cuts: list[str] = []

    def fetch(since):
        cuts.append(since.isoformat())
        return _dump(RAW)

    results = {}
    for tz in ("UTC", "Asia/Tokyo", "America/New_York"):
        monkeypatch.setenv("TZ", tz)
        _time.tzset()
        cuts.clear()
        list(_adapter(tmp_path / tz.replace("/", "_"), fetch_api=fetch,
                      days=1).collect(since=datetime(2026, 8, 15)))
        results[tz] = list(cuts)
    monkeypatch.delenv("TZ", raising=False)
    _time.tzset()

    assert results["UTC"] == results["Asia/Tokyo"] == results["America/New_York"], \
        f"cutoff 随宿主 TZ 漂移: {results}"
    assert results["UTC"] == ["2026-08-15T00:00:00+00:00"]


# ---- 10-05 复审 R10 · 去重键与入库 id 不一致 --------------------------------

def test_dedup_key_equals_item_id(tmp_path):
    """seen 的去重键必须与入库 `Item.id` 同源, 否则「已见过」与「已入库」会不一致。"""
    recs = list(_adapter(tmp_path, fetch_api=lambda s: _dump(LIVE), days=3650).collect())
    seen = wd._load_state(tmp_path / "state.json")
    assert seen == {r.item.id for r in recs}
    for r in recs:
        assert wd._dedup_key({"url": r.item.url}) == r.item.id


def test_same_url_different_donid_only_ingested_once(tmp_path):
    """两条不同 DON 撞同一个 URL: 修复前 seen 不认为重复, 每轮都重抓。"""
    payload = {"value": [
        {"Id": "id-1", "DonId": "2026-DON900", "Title": "Cholera - Somalia",
         "PublicationDate": "2026-09-20T00:00:00Z",
         "ItemDefaultUrl": "/2026-DON900", "Summary": "x"},
        {"Id": "id-2", "DonId": "2026-DON901", "Title": "Cholera - Somalia",
         "PublicationDate": "2026-09-19T00:00:00Z",
         "ItemDefaultUrl": "/2026-DON900", "Summary": "y"},
    ]}
    first = list(_adapter(tmp_path, fetch_api=lambda s: _dump(payload),
                          days=3650).collect())
    assert len(first) == 1, "同 URL 的两条只应入库一次"
    # 第二轮不应再产出(修复前 seen 用 guid, 两条 guid 不同 -> 每轮都重抓)
    assert list(_adapter(tmp_path, fetch_api=lambda s: _dump(payload),
                         days=3650).collect()) == []


def test_old_entry_same_issue_different_url_not_double_ingested(tmp_path):
    """老条目(DonId 为空)同一期给出不同 ItemDefaultUrl 时, 不产生两个不同 id。"""
    payload = {"value": [{
        "Id": "same-guid", "DonId": "", "Title": "Polio in Congo",
        "PublicationDate": "2026-09-20T00:00:00Z",
        "ItemDefaultUrl": "/2026_09_20-en", "Summary": "x"}]}
    recs = list(_adapter(tmp_path, fetch_api=lambda s: _dump(payload), days=3650).collect())
    assert len(recs) == 1
    # seen 记的键就是入库 id -> 下一轮同一期(哪怕 URL 变了)也只入库一次
    assert wd._load_state(tmp_path / "state.json") == {recs[0].item.id}
    assert list(_adapter(tmp_path, fetch_api=lambda s: _dump(payload),
                         days=3650).collect()) == []
