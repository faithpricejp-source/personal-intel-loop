"""Kimi 审查发现（I+J 路 11 条）逐条复核的回归测试。

约定：每条测试直接调用真文件里的真函数/真方法复现问题，修复前必须失败。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------- I-1

def test_html_columns_load_state_bad_zero_match_value(tmp_path):
    """I-1: state 是合法 JSON 但 zero_match 值不是数字时, _load_state 不许抛,
    坏值按 0 计（否则 collect() 每轮在同一位置崩, state 永远留在坏形状）。"""
    from personal_intel_loop.adapters.html_columns import _load_state

    path = tmp_path / "html_columns_state.json"
    path.write_text(json.dumps({
        "seen": ["a", "b"],
        "zero_match": {
            "col_dict_bad": {"zero_match_runs": "bad"},
            "col_scalar_bad": "worse",
            "col_dict_ok": {"zero_match_runs": 2},
            "col_scalar_ok": 3,
        },
    }), "utf-8")

    seen, zero_map = _load_state(path)

    assert seen == ["a", "b"]
    assert zero_map == {
        "col_dict_bad": 0,
        "col_scalar_bad": 0,
        "col_dict_ok": 2,
        "col_scalar_ok": 3,
    }


# ---------------------------------------------------------------- I-2

def test_home_alerts_save_state_atomic(tmp_path, monkeypatch):
    """I-2: 模拟写入中途被杀(写到一半抛 OSError, 目标文件已截断),
    已有 state 文件必须原样保留 —— 直写 path.write_text 的旧实现会把
    state 打成半截 JSON, 下轮 _load_state 当空状态, 在警疾病全部假「发布」。"""
    import personal_intel_loop.adapters.home_alerts as ha

    path = tmp_path / "home_alerts_state.json"
    old_state = {"alerts": {"デング熱": "注意報"}, "jma_souten": {"last_key": "k1"}}
    ha._save_state(path, old_state)

    real_write_text = Path.write_text

    def _die_mid_write(self, data, *args, **kwargs):
        real_write_text(self, data[:5], encoding="utf-8")  # 截断并只写一半
        raise OSError(28, "simulated death mid-write")

    monkeypatch.setattr(Path, "write_text", _die_mid_write)
    ha._save_state(path, {"alerts": {}})
    monkeypatch.setattr(Path, "write_text", real_write_text)

    # 旧 state 必须原样在, 且 tmp 文件不残留
    assert json.loads(path.read_text("utf-8")) == old_state
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


# ---------------------------------------------------------------- I-4

def test_tokyo_events_bad_url_entry_does_not_kill_collect(tmp_path):
    """I-4: 单条 entry 的 url 过不了 Item validator(如 ntj API 某行给出
    javascript: 串, urljoin 原样透传)时, 只跳过该条, 其余条目与其他源照常
    产出、state 落盘 —— 旧行为是 ValidationError 冒出 collect, _save_state
    执行不到, 下一轮在同一行再崩, adapter 永久停摆。"""
    import personal_intel_loop.adapters.tokyo_events as te

    today = date(2026, 10, 5)
    now = datetime(2026, 10, 5, 4, 0, 0, tzinfo=timezone.utc)
    bad_ntj = json.dumps({"rows": [
        {"title": "正常公演", "theatre_name": "国立劇場", "genre": "歌舞伎",
         "start_date": "10月1日", "end_date": "10月20日", "year": 2026,
         "url": "schedule/kokuritsu_l/2026/0810/"},
        {"title": "坏URL公演", "theatre_name": "国立劇場", "genre": "歌舞伎",
         "start_date": "11月2日", "end_date": "11月3日", "year": 2026,
         "url": "javascript:alert(1)"},
    ]}, ensure_ascii=False).encode("utf-8")
    tnm_raw = (Path(__file__).parent / "fixtures" / "tokyo_events" / "tnm_cid1.xml").read_bytes()
    blobs = {"ntj": bad_ntj, "tnm": tnm_raw}
    adapter = te.TokyoEventsAdapter(
        fetch=lambda s: blobs.get(s.venue_key, b""),
        state_path=tmp_path / "state.json",
        today=today,
        clock=lambda: now,
        sources=tuple(s for s in te.SOURCES if s.venue_key in ("ntj", "tnm")),
    )

    records = list(adapter.collect())

    srcs = {r.item.source for r in records}
    assert "tokyo_events:ntj" in srcs          # 同源的正常行照常产出
    assert "tokyo_events:tnm" in srcs          # 其他源不受坏条目拖累
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert state["seen"]                       # state 仍然落盘


# ---------------------------------------------------------------- J-1

def test_mofa_anzen_limit_truncation_marks_seen_correctly(tmp_path):
    """J-1: records.sort 重排之后 zip(produced_keys, records) 按位置错位 ——
    limit 截断时必须把**实际返回**的 key_cd 记进 seen。旧行为把被丢弃的旧条目
    记成 seen、漏掉返回的新条目: 旧条目永久丢失(下轮被 seen 跳过), 新条目重采。"""
    import personal_intel_loop.adapters.mofa_anzen as ma

    xml = (
        "<dataset>"
        "<mail><infoType>T40</infoType><keyCd>2026T001</keyCd>"
        "<title>危険情報・テスト旧</title><lead>リード旧</lead>"
        "<country><name>テスト国</name></country>"
        "<infoUrl>https://example.com/2026T001.html</infoUrl>"
        "<leaveDate>2026/09/01 00:00:00</leaveDate></mail>"
        "<mail><infoType>T40</infoType><keyCd>2026T002</keyCd>"
        "<title>危険情報・テスト新</title><lead>リード新</lead>"
        "<country><name>テスト国</name></country>"
        "<infoUrl>https://example.com/2026T002.html</infoUrl>"
        "<leaveDate>2026/09/20 00:00:00</leaveDate></mail>"
        "</dataset>"
    )
    adapter = ma.MofaAnzenAdapter(
        fetch_open_data=lambda: xml,
        fetch_rss=lambda: "",
        state_path=tmp_path / "mofa_state.json",
    )

    records = list(adapter.collect(
        limit=1, since=datetime(2026, 8, 1, tzinfo=timezone.utc)))

    assert len(records) == 1
    returned_key = json.loads(records[0].source_payload_json)["key_cd"]
    assert returned_key == "2026T002"          # 返回的是较新的 T002
    seen = ma._load_state(tmp_path / "mofa_state.json")
    assert seen == {"2026T002"}                # 修复前: {"2026T001"} —— 记错


# ---------------------------------------------------------------- J-2

def test_enso_status_limit_truncation_marks_state_correctly(tmp_path):
    """J-2: records.sort 重排之后 zip(produced, records) 按位置错位 ——
    limit 截断时必须把**实际返回**机构的 (org, stamp) 写进 last_published。
    旧行为把没返回的机构标成「已产出」(月更通报被 suppression, 丢一个月),
    返回的机构反而没进 state(下轮重复产出)。"""
    import personal_intel_loop.adapters.enso_status as es

    fix = Path(__file__).parent / "fixtures" / "risk"
    noaa_html = (fix / "noaa_ensodisc.html").read_text("utf-8")     # 2026-09-10
    jma_html = (fix / "jma_kanshi.html").read_text("utf-8").replace(
        "令和8年9月9日", "令和8年10月9日")                            # 2026-10-09, 更新
    adapter = es.EnsoStatusAdapter(
        fetch_noaa=lambda: noaa_html,
        fetch_jma=lambda: jma_html,
        state_path=tmp_path / "state.json",
    )

    records = list(adapter.collect(
        limit=1, since=datetime(2026, 9, 1, tzinfo=timezone.utc)))

    assert len(records) == 1
    assert records[0].item.source == "enso_status:jma"   # 返回较新的 JMA
    state = es._load_state(tmp_path / "state.json")
    assert state == {"jma": "2026-10-09"}       # 修复前: {"noaa": "2026-09-10"} —— 记错


# ---------------------------------------------------------------- J-3

def test_thepaper_warm_naive_since_interpreted_as_cst(tmp_path):
    """J-3: naive `since`(cli 的 --since 2026-10-01)按站点时区(东八区)解释,
    不随宿主 TZ 漂移 —— 与 _parse_absolute 对同一站点字符串的东八区口径一致。

    本测试把进程 TZ 强制成 UTC 来区分两种解释: 卡片 ts=2026-09-28 04:30 UTC,
    naive since=2026-09-28 06:30 —— 按宿主 UTC 解释则 cutoff 在卡片之后(丢),
    按东八区解释则 since 落在 7 天窗之外(留)。
    """
    import os
    import time as _time
    import personal_intel_loop.adapters.thepaper_warm as tw

    samples = json.loads(
        (Path(__file__).parent / "fixtures" / "warmth"
         / "thepaper_warm_nextdata_samples.json").read_text("utf-8"))
    card = dict(samples["26911"]["cards"][0])
    ts_utc = datetime(2026, 9, 28, 4, 30, tzinfo=timezone.utc)
    epoch_ms = int(ts_utc.timestamp() * 1000)
    card.update(pubTimeLong=epoch_ms, trackPublishTime=epoch_ms,
                publishTime="2026-09-28 12:30:00")

    adapter = tw.ThepaperWarmAdapter(
        fetch_list=lambda url: [card],
        fetch_body=lambda url: "正文",
        state_path=tmp_path / "state.json",
        now_fn=lambda: datetime(2026, 10, 5, 0, 30, tzinfo=timezone.utc),
    )

    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    _time.tzset()
    try:
        records = list(adapter.collect(since=datetime(2026, 9, 28, 6, 30)))  # naive
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        _time.tzset()

    assert len(records) == 1   # 修复前(TZ=UTC): 0 条 —— naive 被按宿主时区解释


# ---------------------------------------------------------------- J-4

def test_thepaper_warm_parse_pub_time_out_of_range_epoch_returns_none():
    """J-4: epoch 数值越界(站点字段异常/17 位以上数字串)不许抛 OSError/ValueError
    炸掉整轮 —— 按函数自己的承诺「认不出来返回 (None, False), 调用方丢掉这条」;
    epoch 坏但绝对时间串好的卡片应落到下一档取到精确时间。"""
    import personal_intel_loop.adapters.thepaper_warm as tw

    now = datetime(2026, 10, 5, tzinfo=timezone.utc)

    # 数值型越界: 10**17 毫秒 → 年份超范围, 旧行为 ValueError: year must be in 1..9999
    assert tw._parse_pub_time({"pubTimeLong": 10**17}, now=now) == (None, False)
    # 数字字符串越界: 20 位, 旧行为 OSError: [Errno 84] Value too large
    assert tw._parse_pub_time(
        {"pubTimeLong": "99999999999999999999"}, now=now) == (None, False)
    # epoch 坏但 publishTime 绝对串好 → 落到下一档, 按东八区取精确时间
    ts, approx = tw._parse_pub_time(
        {"pubTimeLong": 10**17, "publishTime": "2026-10-04 12:59:05"}, now=now)
    assert (ts, approx) == (datetime(2026, 10, 4, 4, 59, 5, tzinfo=timezone.utc), False)


# ---------------------------------------------------------------- J-5

def test_hotspot_block_bad_share_of_window_degrades_not_crashes(tmp_path):
    """J-5: hotspots_<city>.json 是生成文件, 某行 share_of_window 是非数值
    (「abc」/dict/…) 时 `_hotspot_block` 不许抛 ValueError/TypeError ——
    与 D26 同按退化语义(坏值按 0% 计、行保留), 不让 load_reference 整体中断。"""
    import personal_intel_loop.risk_reference as rr

    root = tmp_path / "ref"
    (root / "hotspots").mkdir(parents=True)
    payload = {
        "city_name_zh": "纽约", "city_name_en": "New York",
        "source": {"publisher": "NYPD", "portal": "https://example.com",
                   "group_key": "precinct"},
        "window": {"start": "2026-09-01", "end": "2026-09-30", "days": 30},
        "generated_at": "2026-10-01T00:00:00Z",
        "hotspots": [
            {"precinct_name": "Midtown South", "boro": "Manhattan", "hour_band_zh": "夜间",
             "count": 12, "share_of_window": "abc"},           # 坏字符串 → float() ValueError
            {"precinct_name": "Midtown North", "boro": "Manhattan", "hour_band": "day",
             "count": 5, "share_of_window": {"pct": 0.1}},     # dict → float() TypeError
            {"precinct_name": "Lower East", "boro": "Manhattan", "hour_band": "night",
             "count": 3, "share_of_window": 0.1234},           # 正常值照常渲染
        ],
    }
    (root / "hotspots" / "hotspots_nyc.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    out = rr._hotspot_block(root, "unitedstates", "newyork", None)

    assert out != ""
    assert "12 件（0.0%）" in out      # 坏值按 0 退化, 行保留
    assert "3 件（12.34%）" in out     # 正常值不受影响


# ---------------------------------------------------------------- J-7

@pytest.mark.parametrize("modname", ["mofa_anzen", "enso_status"])
def test_state_save_atomic_in_mofa_and_enso(tmp_path, monkeypatch, modname):
    """J-7: mofa_anzen / enso_status 的 `_save_state` 都是直写 write_text(先截断后写),
    写入中途被杀会留半截 JSON —— 下轮 `_load_state` 按坏 JSON 退化成空, seen/
    last_published 被静默重置(窗口内全量重抓 / 两机构当期通报重新产出)。
    同项目 thepaper_warm(E19)/html_columns(D13)/tokyo_events(D21) 都已改原子替换。"""
    import importlib

    mod = importlib.import_module(f"personal_intel_loop.adapters.{modname}")
    path = tmp_path / f"{modname}_state.json"
    # 注意各 adapter 的 _load_state 返回形状不同: mofa 返回 set, enso 返回内层 dict
    if modname == "mofa_anzen":
        expected = {"k1", "k2"}
        mod._save_state(path, {"k1", "k2"})
    else:
        expected = {"noaa": "2026-09-10"}
        mod._save_state(path, {"noaa": "2026-09-10"})

    real_write_text = Path.write_text

    def _die_mid_write(self, data, *args, **kwargs):
        real_write_text(self, data[:5], encoding="utf-8")  # 截断并只写一半
        raise OSError(28, "simulated death mid-write")

    monkeypatch.setattr(Path, "write_text", _die_mid_write)
    if modname == "mofa_anzen":
        mod._save_state(path, {"k9"})
    else:
        mod._save_state(path, {})
    monkeypatch.setattr(Path, "write_text", real_write_text)

    assert mod._load_state(path) == expected   # 修复前: 半截 JSON → 空状态
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
