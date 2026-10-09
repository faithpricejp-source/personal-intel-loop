"""paper_api: 输出键集合与契约 fixtures 完全一致(递归比较键, 不比较值); next_before 翻页; set_pipeline。"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from personal_intel_loop import PROJECT_ROOT
from personal_intel_loop.paper_api import get_editions, get_item, get_pipeline, set_pipeline
from personal_intel_loop.store import connect_db, ensure_schema, upsert_item
from tests.conftest import make_item

FIXTURES = PROJECT_ROOT / "docs" / "paper_v2_fixtures"
NOW = "2026-10-04T00:00:00Z"
EDITION_DATE = "2026-10-04"
OLDER_DATE = "2026-10-03"

LETTER_LINES = [
    "[2026-10-03 · topic · rss_briefing:harbor_review:x] 依据: 用户三次对港口物流点「这类多来点」 → 目标: 域饱和表 → 建议: 新增行「港口/航运物流 | 不饱和 | 优先」",
    "[2026-10-03 · style · weibo_timeline:北岭老张:y] 依据: 用户两次标记论战体文风不喜欢 → 目标: 文风 → 建议: 论战体且无数据支撑的条目降到简讯末尾",
]


def _shape(value):
    if isinstance(value, dict):
        return {key: _shape(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    return "leaf"


def _assert_same_shape(actual, expected, path=""):
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: 应为 dict"
        assert set(actual.keys()) == set(expected.keys()), f"{path}: 键集合差异 {set(actual) ^ set(expected)}"
        for key in expected:
            _assert_same_shape(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: 应为 list"
        if expected:
            assert actual, f"{path}: 应非空"
            _assert_same_shape(actual[0], expected[0], f"{path}[0]")
    else:
        return  # 叶子: 只比较键, 不比较值


def _ai_payload(*, byline=None, claim=None, backstory=None, so_what=None, style_tags=("数据密集",)):
    return {
        "lede": "导语。",
        "one_liner": "一句话。",
        "backstory": backstory,
        "so_what": so_what,
        "claim": claim,
        "byline": byline,
        "style_tags": list(style_tags),
        "topic": "资源/大宗",
        "profile_hit": "命中句",
        "lane": "1",
    }


@pytest.fixture
def conn(tmp_path):
    c = connect_db(tmp_path / "db.sqlite")
    ensure_schema(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def profile_path(tmp_path):
    path = tmp_path / "reading_profile.md"
    path.write_text("# 阅读偏好\n\n正文\n\n## 待接受的修订\n" + "".join(f"- {line}\n" for line in LETTER_LINES), "utf-8")
    return path


def _seed(conn):
    spec = {
        # item_id: (source, label, byline, claim, backstory, style_tags)
        "item:lead": ("rss_briefing:src1", "源一", None, {"text": "断言", "check_after": "2027-01-01"}, "来龙", []),
        "item:top1": ("rss_briefing:src2", "源二", "作者甲", None, None, ["清单体"]),
        "item:top2": ("rss_briefing:src2", "源二", "作者乙", None, None, ["数据密集"]),
        "item:br1": ("rss_briefing:src3", "源三", None, None, "来龙", ["访谈"]),
        "item:br2": ("rss_briefing:src1", "源一", "作者丁", None, None, ["第一人称"]),
        "item:bl1": ("rss_briefing:src4", "源四", "作者丙", {"text": "断言", "check_after": "2027-01-01"}, "来龙", []),
        "item:bl2": ("rss_briefing:src4", "源四", "作者戊", None, "来龙", ["长叙事"]),
    }
    for item_id, (source, label, byline, claim, backstory, tags) in spec.items():
        payload = {"feed_name": label}
        upsert_item(
            conn,
            make_item(item_id=item_id, source=source, url=f"https://example.com/{item_id}", title=f"标题 {item_id}"),
            adapter_name="rss_briefing",
            source_payload_json=json.dumps(payload, ensure_ascii=False),
        )
        conn.execute(
            "INSERT OR REPLACE INTO item_ai (item_id, payload_json, model, error, created_at) VALUES (?, ?, 'm', NULL, ?)",
            (item_id, json.dumps(_ai_payload(byline=byline, claim=claim, backstory=backstory, style_tags=tags), ensure_ascii=False), NOW),
        )
    # 作者信任: lead 的来源一不建(匹配 fixture lead trust.author=null), 其余建
    for key, up, down in (("作者甲", 3, 1), ("作者乙", 2, 0), ("作者丙", 2, 2), ("源三", 1, 0), ("作者丁", 0, 1), ("作者戊", 1, 1)):
        conn.execute(
            "INSERT OR REPLACE INTO author_trust (author_key, label, n_up, n_down, muted, updated_at) VALUES (?, ?, ?, ?, 0, ?)",
            (key, key, up, down, NOW),
        )
    built = "2026-10-04T09:12:00+09:00"
    rows = [
        (EDITION_DATE, "item:lead", "lead", 0, None),
        (EDITION_DATE, "item:top1", "top", 0, None),
        (EDITION_DATE, "item:top2", "top", 1, None),
        (EDITION_DATE, "item:br1", "briefs", 0, None),
        (EDITION_DATE, "item:br2", "briefs", 1, None),
        (EDITION_DATE, "item:bl1", "blind", 0, "盲区句一"),
        (EDITION_DATE, "item:bl2", "blind", 1, "盲区句二"),
        (OLDER_DATE, "item:br2", "lead", 0, None),
        (OLDER_DATE, "item:top2", "top", 0, None),
        (OLDER_DATE, "item:br1", "briefs", 0, None),
        (OLDER_DATE, "item:bl2", "blind", 0, "盲区句二"),
    ]
    for edition_date, item_id, section, rank, reason in rows:
        conn.execute(
            "INSERT INTO editions (edition_date, item_id, section, rank, blind_reason, built_at) VALUES (?, ?, ?, ?, ?, ?)",
            (edition_date, item_id, section, rank, reason, built),
        )
    # my.*: 一条 overall 评分 + 一条原因码事件
    conn.execute(
        "INSERT OR REPLACE INTO item_ratings (item_id, dim, value, author_key, ts, distilled_at) VALUES ('item:top1', 'overall', 1, NULL, ?, NULL)",
        (NOW,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO promotion_events (event_id, item_id, source, event_type, event_weight, origin, digest_path, vault_object_type, vault_object_id, note, event_ts, created_at) VALUES ('evt1', 'item:top2', 'rss_briefing:src2', 'keep', 1.0, 'web', NULL, NULL, NULL, NULL, ?, ?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT OR REPLACE INTO item_fulltext (item_id, text, status, fetched_at) VALUES ('item:lead', '抓回来的全文', 'ok', ?)",
        (NOW,),
    )
    conn.commit()


def test_get_editions_keys_match_fixture(conn, profile_path):
    _seed(conn)
    fixture = json.loads((FIXTURES / "editions_page1.json").read_text("utf-8"))
    result = get_editions(conn, before="2026-10-05", limit=1, profile_path=profile_path)
    # 契约第 4 节 Edition 新增 reading_note(fixture 冻结在增补前): 先断言存在, 剥离后再对 fixtures 比键
    assert all("reading_note" in edition for edition in result["editions"])
    assert all(edition["reading_note"] is None for edition in result["editions"])  # 无 edition_notes 记录时为 null
    # 契约第 5 节 Edition 新增顶层 flash 与 sections.inbox, 同样先断言存在再剥离
    assert all(edition["flash"] == [] for edition in result["editions"])  # 本测试库无 inbox 投递
    assert all("inbox" in edition["sections"] for edition in result["editions"])
    # TASK4(契约 6-10 节) Edition 新增 auth_alerts/follow_suggestions 与六个新分区 + stats 两键, 同法剥离
    assert all(edition["auth_alerts"] == [] for edition in result["editions"])  # 本测试库无探针记录
    assert all(edition["follow_suggestions"] == [] for edition in result["editions"])  # 非周一且无推荐
    for edition in result["editions"]:
        for section in ("counter", "warmth", "risk", "opportunity", "settle", "leisure"):
            assert edition["sections"][section] == []
        assert set(edition["stats"]["novelty_mix"].keys()) == {"new_fact", "new_mechanism", "counter", "known", "confirming"}
        assert isinstance(edition["stats"]["est_read_min"], int)
        edition.pop("reading_note")
        edition.pop("flash")
        edition.pop("auth_alerts")
        edition.pop("follow_suggestions")
        edition["sections"].pop("inbox")
        for section in ("counter", "warmth", "risk", "opportunity", "settle", "leisure"):
            edition["sections"].pop(section)
        edition["stats"].pop("novelty_mix")
        edition["stats"].pop("est_read_min")
        for item in [entry for entries in edition["sections"].values() for entry in entries]:
            # TASK4 Item 全键: same_day_url 恒有; kind/media/novelty/verification/opportunity/settle 本库全为缺省
            assert item["kind"] == "article"
            for key in ("media", "novelty", "verification", "opportunity", "settle"):
                assert item[key] is None
            assert item["same_day_url"].startswith("/paper/#/archive?source=")
            for key in ("same_day_url", "kind", "media", "novelty", "verification", "opportunity", "settle"):
                item.pop(key)
    _assert_same_shape(result, fixture)
    edition = result["editions"][0]
    # 版面键集合(每个 section 都要有)
    assert set(edition) == {"date", "built_at", "sections", "letters", "stats"}
    assert set(edition["sections"]) == {"lead", "top", "briefs", "blind"}
    assert set(edition["stats"]) == {"pool", "picked", "ai_done"}
    fixture_item_keys = set(fixture["editions"][0]["sections"]["lead"][0].keys())
    for section in ("lead", "top", "briefs", "blind"):
        for item in edition["sections"][section]:
            assert set(item.keys()) == fixture_item_keys, section
            assert set(item["why_here"].keys()) == {"profile_hit", "lane", "blind_reason"}
            assert set(item["my"].keys()) == {"overall", "quality", "author", "style", "topic", "reason_code", "note"}  # 契约第 11 节：批注
            assert set(item["trust"].keys()) == {"source", "author"}
    assert set(edition["letters"][0].keys()) == {"line", "date", "basis", "target", "suggestion"}
    # letters 只给最新一期
    older = get_editions(conn, before=EDITION_DATE, limit=1, profile_path=profile_path)
    assert older["editions"][0]["date"] == OLDER_DATE
    assert older["editions"][0]["letters"] == []


def test_get_item_keys_match_fixture(conn, profile_path):
    _seed(conn)
    fixture = json.loads((FIXTURES / "item_detail.json").read_text("utf-8"))
    result = get_item(conn, "item:lead")
    # TASK4 Item 全键(同 get_editions): 先断言存在与缺省值, 剥离后再对冻结 fixtures 比键
    assert result["kind"] == "article"
    assert result["same_day_url"].startswith("/paper/#/archive?source=")
    for key in ("media", "novelty", "verification", "opportunity", "settle"):
        assert result[key] is None
    assert result["fulltext_source"] == "fetched"
    for key in ("same_day_url", "kind", "media", "novelty", "verification", "opportunity", "settle", "fulltext_source"):
        result.pop(key)
    _assert_same_shape(result, fixture)
    assert set(result.keys()) == set(fixture.keys())
    assert result["fulltext"] == "抓回来的全文"
    assert result["edition_date"] == EDITION_DATE
    # 无全文/无版面的 item 也有这两个键
    bare = get_item(conn, "item:top1")
    for key in ("same_day_url", "kind", "media", "novelty", "verification", "opportunity", "settle", "fulltext_source"):
        bare.pop(key)
    assert set(bare.keys()) == set(fixture.keys())


def test_get_pipeline_keys_match_fixture(conn, profile_path):
    _seed(conn)
    fixture = json.loads((FIXTURES / "pipeline.json").read_text("utf-8"))
    result = get_pipeline(conn, profile_dir="/tmp/any")
    _assert_same_shape(result, fixture)
    assert set(result.keys()) == {"sources", "authors", "recent", "profile_dir", "health"}  # 10-05 验收 C1: +health
    assert set(result["sources"][0].keys()) == {"source", "label", "trust", "items_30d", "on_paper_30d", "muted", "boosted"}
    assert set(result["authors"][0].keys()) == {"author_key", "label", "trust", "n_up", "n_down", "muted"}
    assert set(result["recent"][0].keys()) == {"ts", "item_id", "title", "dim", "value"}
    assert result["profile_dir"] == "/tmp/any"
    # 排序: sources 按 trust 降序, authors 按 n_up+n_down 降序
    trusts = [s["trust"] for s in result["sources"]]
    assert trusts == sorted(trusts, reverse=True)
    counts = [a["n_up"] + a["n_down"] for a in result["authors"]]
    assert counts == sorted(counts, reverse=True)


def test_get_editions_next_before_pagination(conn, profile_path):
    _seed(conn)
    page1 = get_editions(conn, before="2026-10-05", limit=1, profile_path=profile_path)
    assert [e["date"] for e in page1["editions"]] == [EDITION_DATE]
    assert page1["next_before"] == EDITION_DATE  # 还有更早的期
    page2 = get_editions(conn, before=EDITION_DATE, limit=3, profile_path=profile_path)
    assert [e["date"] for e in page2["editions"]] == [OLDER_DATE]
    assert page2["next_before"] is None  # 没有更早的期
    # 缺省 before = 明天 → 从最新一期开始
    default_page = get_editions(conn, profile_path=profile_path)
    assert default_page["editions"][0]["date"] == EDITION_DATE
    with pytest.raises(ValueError):
        get_editions(conn, before="not-a-date", profile_path=profile_path)


def test_my_and_reason_code_and_why_here(conn, profile_path):
    _seed(conn)
    result = get_editions(conn, before="2026-10-05", limit=1, profile_path=profile_path)
    sections = result["editions"][0]["sections"]
    top1 = next(item for item in sections["top"] if item["item_id"] == "item:top1")
    assert top1["my"]["overall"] == 1 and top1["my"]["reason_code"] is None
    assert top1["author_is_byline"] is True and top1["author_key"] == "作者甲"
    top2 = next(item for item in sections["top"] if item["item_id"] == "item:top2")
    assert top2["my"]["reason_code"] == "keep"
    blind_item = sections["blind"][0]
    assert blind_item["why_here"] == {"profile_hit": None, "lane": "blind", "blind_reason": "盲区句一"}
    lead = sections["lead"][0]
    assert lead["why_here"] == {"profile_hit": "命中句", "lane": "1", "blind_reason": None}
    assert lead["one_liner"] == "一句话。"


def test_get_item_unknown_raises(conn):
    _seed(conn)
    with pytest.raises(KeyError):
        get_item(conn, "rss:ghost")


def test_set_pipeline_source_actions(conn):
    _seed(conn)
    payload = {"kind": "source", "key": "rss_briefing:src1", "action": "mute"}
    result = set_pipeline(conn, payload, now_utc=NOW)
    assert result["ok"] is True and result["muted"] is True
    assert result["before"] == result["after"] == pytest.approx(0.35)
    row = conn.execute("SELECT muted, boosted FROM source_overrides WHERE source='rss_briefing:src1'").fetchone()
    assert (row["muted"], row["boosted"]) == (1, 0)
    set_pipeline(conn, {"kind": "source", "key": "rss_briefing:src1", "action": "boost"}, now_utc=NOW)
    row = conn.execute("SELECT muted, boosted FROM source_overrides WHERE source='rss_briefing:src1'").fetchone()
    assert (row["muted"], row["boosted"]) == (1, 1)
    set_pipeline(conn, {"kind": "source", "key": "rss_briefing:src1", "action": "reset"}, now_utc=NOW)
    row = conn.execute("SELECT muted, boosted FROM source_overrides WHERE source='rss_briefing:src1'").fetchone()
    assert (row["muted"], row["boosted"]) == (0, 0)
    set_pipeline(conn, {"kind": "source", "key": "rss_briefing:src1", "action": "unmute"}, now_utc=NOW)
    result = set_pipeline(conn, {"kind": "source", "key": "rss_briefing:src1", "action": "unmute"}, now_utc=NOW)
    assert result["muted"] is False
    with pytest.raises(KeyError):
        set_pipeline(conn, {"kind": "source", "key": "rss_briefing:ghost", "action": "mute"}, now_utc=NOW)


def test_set_pipeline_author_actions(conn):
    _seed(conn)
    result = set_pipeline(conn, {"kind": "author", "key": "作者甲", "action": "mute"}, now_utc=NOW)
    assert result["muted"] is True and result["before"] == result["after"]
    with pytest.raises(ValueError):
        set_pipeline(conn, {"kind": "author", "key": "作者甲", "action": "boost"}, now_utc=NOW)
    result = set_pipeline(conn, {"kind": "author", "key": "作者甲", "action": "reset"}, now_utc=NOW)
    row = conn.execute("SELECT n_up, n_down, muted FROM author_trust WHERE author_key='作者甲'").fetchone()
    assert (row["n_up"], row["n_down"], row["muted"]) == (0, 0, 0)
    assert result["before"] == pytest.approx(5 / 8)
    assert result["after"] == pytest.approx(0.5)
    with pytest.raises(KeyError):
        set_pipeline(conn, {"kind": "author", "key": "ghost", "action": "mute"}, now_utc=NOW)


def test_set_pipeline_validates(conn):
    _seed(conn)
    with pytest.raises(ValueError):
        set_pipeline(conn, {"kind": "magazine", "key": "x", "action": "mute"}, now_utc=NOW)
    with pytest.raises(ValueError):
        set_pipeline(conn, {"kind": "source", "key": "rss_briefing:src1", "action": "explode"}, now_utc=NOW)
    with pytest.raises(ValueError):
        set_pipeline(conn, {"kind": "source", "key": "", "action": "mute"}, now_utc=NOW)


def test_get_item_falls_back_to_feed_body_when_fulltext_missing(conn, profile_path):
    """原文抓不到(无记录或 failed)时回落到 items.body 并标 fulltext_source='feed'。"""
    _seed(conn)
    conn.execute("UPDATE items SET body='采集时带回的公众号正文' WHERE item_id='item:top1'")
    conn.commit()
    got = get_item(conn, "item:top1")
    assert (got["fulltext"], got["fulltext_source"]) == ("采集时带回的公众号正文", "feed")

    conn.execute(
        "INSERT OR REPLACE INTO item_fulltext (item_id, text, status, fetched_at) VALUES ('item:top1', NULL, 'failed', ?)",
        ("2026-10-07T00:00:00Z",),
    )
    conn.commit()
    assert get_item(conn, "item:top1")["fulltext_source"] == "feed"

    conn.execute("UPDATE items SET body='' WHERE item_id='item:top1'")
    conn.commit()
    got = get_item(conn, "item:top1")
    assert (got["fulltext"], got["fulltext_source"]) == (None, None)

    lead = get_item(conn, "item:lead")  # 抓到的原文优先于 body
    assert (lead["fulltext"], lead["fulltext_source"]) == ("抓回来的全文", "fetched")
