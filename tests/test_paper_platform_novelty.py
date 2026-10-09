"""契约 7.1-7.3: novelty 判定与输出、配额替换、lead/top 交换、counter 分区、novelty_mix、
印证占比连升两周触发来信、分歧样例进入预处理提示词。"""
from __future__ import annotations

import json
from datetime import date, timedelta

from personal_intel_loop.paper import build_edition
from personal_intel_loop.paper_ai import preprocess
from personal_intel_loop.paper_api import get_editions, get_item
from personal_intel_loop.paper_calibration import disagreement_samples, weekly_letters
from personal_intel_loop.store import upsert_item
from tests._platform_helpers import (
    NOW,
    ai_json,
    edition_section_ids,
    make_dispatch_llm,
    seed_candidate,
)
from tests.conftest import make_item

TODAY = date(2026, 10, 4)
MONDAY = date(2026, 10, 5)  # 周一


def _seed(conn, count: int):
    for index in range(count):
        seed_candidate(conn, index)
        # 预置全文记录: ensure_fulltext 见到已有记录就跳过, 测试不触网也不等抓取间隔
        conn.execute(
            "INSERT OR REPLACE INTO item_fulltext (item_id, text, status, fetched_at) VALUES (?, '正文全文', 'ok', ?)",
            (f"item:{index:02d}", NOW),
        )
    conn.commit()


def _select(candidates):
    def select(conn, *, date_local, top_k, now_utc):
        return [dict(candidate) for candidate in candidates][:top_k]

    return select


def _candidates(count: int):
    return [
        {
            "item_id": f"item:{index:02d}",
            "source": f"rss_briefing:feed_{index % 3}",
            "source_payload": {"feed_name": f"源 {index % 3}"},
            "title": f"标题 {index}",
            "body": "正文",
            "ts_utc": NOW,
        }
        for index in range(count)
    ]


def _build(conn, tmp_path, *, count=12, n=10, kinds=None, date_local=TODAY):
    """kinds: {index: novelty_kind}; 缺省全known。返回 build_edition 结果。"""
    _seed(conn, count)
    kinds = kinds or {}
    llm = make_dispatch_llm(ai=lambda prompt: ai_json(novelty_kind=kinds.get(_index_of(prompt), "known")))
    return build_edition(
        conn,
        date_local=date_local,
        n=n,
        now_utc=NOW,
        select_fn=_select(_candidates(count)),
        embed_fn=lambda texts: [[0.0, 0.0, 1.0] for _ in texts],
        fetcher=lambda url: ("正文全文", "ok"),
        llm_call=llm,
        learn_first=False,
        spool_dir=tmp_path / "spool",
        layout_path=tmp_path / "none.toml",
    )


def _index_of(prompt: str) -> int:
    import re

    match = re.search(r"- 标题: 标题 (\d+)", prompt)
    return int(match.group(1)) if match else -1


def test_novelty_output_on_item(db_conn, tmp_path):
    _build(db_conn, tmp_path, count=3, n=3, kinds={0: "counter", 1: "new_mechanism"})
    item = get_item(db_conn, "item:00")
    assert item["novelty"] == {"kind": "counter", "why": None, "against": None}
    assert get_item(db_conn, "item:01")["novelty"]["kind"] == "new_mechanism"
    assert get_item(db_conn, "item:02")["novelty"]["kind"] == "known"


def test_quota_replaces_lowest_scored_old_items(db_conn, tmp_path):
    # n=20 → 主线 17 条(盲区 3), 目标 ceil(17*0.4)=7 条新知; 主线只有 5 条(都在 lead+top),
    # 备选池(多预处理 20% = 4 条)里有 2 条新知 → 应替换掉主线里分数最低的 known
    kinds = {index: "known" for index in range(21)}
    kinds.update({index: "new_fact" for index in (0, 1, 2, 3, 4, 17, 18)})
    _build(db_conn, tmp_path, count=21, n=20, kinds=kinds)
    edition = get_editions(db_conn, before="2026-10-05", limit=1, profile_path=tmp_path / "profile.md")["editions"][0]
    main = [item for name in ("lead", "top", "briefs") for item in edition["sections"][name]]
    assert len(main) == 17
    new_share = sum(1 for item in main if (item["novelty"] or {}).get("kind") in ("new_fact", "new_mechanism", "counter"))
    assert new_share >= 7, f"主线新知占比应 ≥40%, 实际 {new_share}/{len(main)}"
    main_ids = {item["item_id"] for item in main}
    assert {"item:17", "item:18"} <= main_ids, "备选池里的新知条目补进主线"
    # 被换下的是排名最靠后的两条 known, 主线条数不变
    assert "item:16" not in main_ids and "item:15" not in main_ids
    assert len(main) == 17


def test_lead_top_swaps_out_old_items(db_conn, tmp_path):
    # 池子前 6 条(lead+top)里 4 条 known, briefs 里也有 known; 交换把 briefs 的新知换上来,
    # briefs 里新知耗尽后停止(能换几个换几个)
    kinds = {index: "known" for index in range(21)}
    kinds.update({index: "new_fact" for index in (0, 1, 17, 18)})
    _build(db_conn, tmp_path, count=21, n=20, kinds=kinds)
    edition = get_editions(db_conn, before="2026-10-05", limit=1, profile_path=tmp_path / "profile.md")["editions"][0]
    lead_top = edition["sections"]["lead"] + edition["sections"]["top"]
    old_in_lead_top = [
        item for item in lead_top if (item["novelty"] or {}).get("kind") in ("known", "confirming")
    ]
    assert len(old_in_lead_top) == 2, "briefs 的新知换上来后 lead+top 只剩 2 条 known(briefs 已无新知可换)"
    assert {"item:17", "item:18"} <= {item["item_id"] for item in lead_top}


def test_counter_section_and_novelty_mix(db_conn, tmp_path):
    kinds = {0: "counter", 1: "counter", 2: "new_fact", 3: "known"}
    _build(db_conn, tmp_path, count=8, n=8, kinds=kinds)
    edition = get_editions(db_conn, before="2026-10-05", limit=1, profile_path=tmp_path / "profile.md")["editions"][0]
    assert set(edition["sections"].keys()) >= {"counter", "warmth", "risk", "opportunity", "settle", "leisure"}
    counter_ids = [item["item_id"] for item in edition["sections"]["counter"]]
    assert counter_ids, "counter 条目单独成栏"
    for item in edition["sections"]["counter"]:
        assert item["novelty"]["kind"] == "counter"
    mix = edition["stats"]["novelty_mix"]
    assert set(mix.keys()) == {"new_fact", "new_mechanism", "counter", "known", "confirming"}
    assert mix["counter"] >= 1


def test_disagreement_samples_feed_preprocess_prompt(db_conn, tmp_path):
    # item:00 被 AI 判 new_fact, 用户打「早知道」
    _build(db_conn, tmp_path, count=2, n=2, kinds={0: "new_fact", 1: "new_mechanism"})
    db_conn.execute(
        "INSERT INTO promotion_events (event_id, item_id, source, event_type, event_weight, origin, event_ts, created_at)"
        " VALUES ('e1', 'item:00', 'rss_briefing:feed_0', 'already_known', -0.5, 'web', ?, ?)",
        (NOW, NOW),
    )
    db_conn.commit()
    samples = disagreement_samples(db_conn)
    assert [sample["item_id"] for sample in samples] == ["item:00"]
    assert samples[0]["ai_kind"] == "new_fact"
    assert "早知道" in samples[0]["line"]

    # 下一条预处理时提示词里带上这条样例
    seed_candidate(db_conn, 5)
    db_conn.commit()
    seen: list[str] = []
    preprocess(db_conn, ["item:05"], llm_call=lambda prompt: seen.append(prompt) or ai_json())
    assert "早知道" in seen[0]


def _mark_opened(conn, item_ids, *, week_start: date):
    """在某周内给一批条目造 open_item 事件(带 item_ai novelty)。"""
    from personal_intel_loop.paper_events import store_events

    ts = f"{week_start.isoformat()}T02:00:00Z"
    store_events(
        conn,
        {"session_id": "s", "events": [
            {"ts": ts, "kind": "open_item", "item_id": item_id, "edition_date": week_start.isoformat(), "ms": None, "meta": {}}
            for item_id in item_ids
        ]},
        now_utc=ts,
    )


def _seed_novelty(conn, item_ids, kinds):
    for item_id, kind in zip(item_ids, kinds):
        seed_candidate(conn, int(item_id.split(":")[1]))
        conn.execute(
            "INSERT OR REPLACE INTO item_ai (item_id, payload_json, model, error, created_at) VALUES (?, ?, 'fake', NULL, ?)",
            (item_id, json.dumps({"novelty": {"kind": kind}}), NOW),
        )
        conn.execute(
            "INSERT OR REPLACE INTO editions (edition_date, item_id, section, rank, blind_reason, built_at) VALUES (?, ?, 'briefs', 0, NULL, ?)",
            (item_id[-2:], item_id, NOW),
        )
    conn.commit()


def test_weekly_letter_when_confirming_share_rises_two_weeks(db_conn, tmp_path):
    # 上上周: 点开的 5 条全是 new_fact; 上周: 4/5 是 confirming → 印证占比上升
    prev_items = [f"item:{index:02d}" for index in range(10, 15)]
    this_items = [f"item:{index:02d}" for index in range(20, 25)]
    _seed_novelty(db_conn, prev_items, ["new_fact"] * 5)
    _seed_novelty(db_conn, this_items, ["confirming"] * 4 + ["new_fact"])
    _mark_opened(db_conn, prev_items, week_start=MONDAY - timedelta(days=14))
    _mark_opened(db_conn, this_items, week_start=MONDAY - timedelta(days=7))

    profile = tmp_path / "profile.md"
    profile.write_text("# 画像\n\n## 待接受的修订\n", encoding="utf-8")
    written = weekly_letters(db_conn, monday=MONDAY, now_utc=NOW, profile_path=profile)
    assert written["calibration"] is True
    assert "印证" in profile.read_text(encoding="utf-8")
    # 幂等: 同一周不重复写
    assert weekly_letters(db_conn, monday=MONDAY, now_utc=NOW, profile_path=profile)["calibration"] is False


def test_no_calibration_letter_when_share_falls(db_conn, tmp_path):
    prev_items = [f"item:{index:02d}" for index in range(30, 35)]
    this_items = [f"item:{index:02d}" for index in range(40, 45)]
    _seed_novelty(db_conn, prev_items, ["confirming"] * 5)
    _seed_novelty(db_conn, this_items, ["new_fact"] * 5)
    _mark_opened(db_conn, prev_items, week_start=MONDAY - timedelta(days=14))
    _mark_opened(db_conn, this_items, week_start=MONDAY - timedelta(days=7))
    profile = tmp_path / "profile.md"
    profile.write_text("# 画像\n\n## 待接受的修订\n", encoding="utf-8")
    assert weekly_letters(db_conn, monday=MONDAY, now_utc=NOW, profile_path=profile)["calibration"] is False