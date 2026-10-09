"""6.4 讨论综述: 聚类分组(余弦 ≥0.78)、综述 Item 结构、digest 详情带全部成员、
单条社交照普通 Item 上版、媒体类条目 kind=media + media 字段。"""
from __future__ import annotations

import json
from datetime import date

import pytest

from personal_intel_loop.paper import build_edition
from personal_intel_loop.paper_api import get_editions, get_item
from personal_intel_loop.paper_social import cluster_candidates
from personal_intel_loop.store import upsert_item
from tests._platform_helpers import (
    DIGEST_JSON,
    NOW,
    ai_json,
    embed_by_title_index,
    make_dispatch_llm,
    seed_candidate,
)
from tests.conftest import make_item

TODAY = date(2026, 10, 4)


def _candidate(index: int, *, source: str):
    return {
        "item_id": f"item:{index:02d}",
        "source": source,
        "source_payload": {"account_name": f"账号{index}"},
        "title": f"标题 {index}",
        "body": "正文",
        "ts_utc": NOW,
    }


def test_cluster_splits_on_similarity_threshold():
    candidates = [_candidate(0, source="weibo_timeline:a"), _candidate(1, source="weibo_timeline:a"), _candidate(2, source="weibo_timeline:b")]
    # 0/1 高度相似, 2 与谁都远
    vectors = [[1.0, 0.0], [0.99, 0.1], [0.0, 1.0]]
    groups = cluster_candidates(candidates, vectors)
    assert sorted(sorted(group) for group in groups) == [[0, 1], [2]]


def _seed_social(conn, count: int = 6):
    for index in range(count):
        upsert_item(
            conn,
            make_item(
                item_id=f"item:{index:02d}",
                source="weibo_timeline:user1",
                url=f"https://weibo.example/{index}",
                title=f"标题 {index}",
                body="正文",
            ),
            adapter_name="weibo_timeline",
            source_payload_json=json.dumps({"account_name": f"账号{index}"}),
        )
    conn.commit()


def _select(candidates):
    def select(conn, *, date_local, top_k, now_utc):
        return [dict(candidate) for candidate in candidates]

    return select


def _embed(texts):
    return embed_by_title_index({0: [1.0, 0.0], 1: [0.99, 0.1], 2: [0.98, 0.05], 3: [0.0, 1.0]})(texts)


def test_build_edition_groups_social_into_digest(db_conn, tmp_path):
    _seed_social(db_conn, count=4)
    result = build_edition(
        db_conn,
        date_local=TODAY,
        n=10,
        now_utc=NOW,
        select_fn=_select([_candidate(index, source="weibo_timeline:user1") for index in range(4)]),
        embed_fn=_embed,
        llm_call=make_dispatch_llm(ai=ai_json(novelty_kind="new_fact"), digest=DIGEST_JSON),
        learn_first=False,
        layout_path=tmp_path / "none.toml",
    )
    assert result["digests"] == 1
    editions = db_conn.execute(
        "SELECT item_id, section FROM editions WHERE edition_date=? AND item_id LIKE 'digest:%'", (TODAY.isoformat(),)
    ).fetchall()
    assert len(editions) == 1
    digest_id = editions[0]["item_id"]
    assert editions[0]["section"] in ("lead", "top", "briefs"), "综述与普通条目同版面排序"
    members = json.loads(
        db_conn.execute("SELECT member_ids_json FROM digests WHERE digest_id=?", (digest_id,)).fetchone()["member_ids_json"]
    )
    assert set(members) == {"item:00", "item:01", "item:02"}, "0/1/2 相似聚成一组, item:03 单独"

    edition = get_editions(db_conn, before="2026-10-05", limit=1, profile_path=tmp_path / "profile.md")["editions"][0]
    placed = [item for name in ("lead", "top", "briefs") for item in edition["sections"][name]]
    digest_item = next(item for item in placed if item["item_id"] == digest_id)
    assert digest_item["kind"] == "digest"
    assert set(digest_item.keys()) >= {"kind", "quotes", "members", "topic"}
    assert digest_item["quotes"] == [{"text": "加息板上钉钉", "author_label": "甲", "url": "https://weibo.example/1"}]
    assert digest_item["members"] == ["item:00", "item:01", "item:02"]
    # 单条社交照普通 Item 上版, 但仍标 kind
    singleton = next(item for item in placed if item["item_id"] == "item:03")
    assert singleton["kind"] == "article"
    assert singleton["title"] == "标题 3"


def test_get_item_digest_returns_all_members(db_conn, tmp_path):
    _seed_social(db_conn, count=4)
    build_edition(
        db_conn,
        date_local=TODAY,
        n=10,
        now_utc=NOW,
        select_fn=_select([_candidate(index, source="weibo_timeline:user1") for index in range(4)]),
        embed_fn=_embed,
        llm_call=make_dispatch_llm(ai=ai_json(novelty_kind="new_fact"), digest=DIGEST_JSON),
        learn_first=False,
        layout_path=tmp_path / "none.toml",
    )
    digest_id = db_conn.execute(
        "SELECT digest_id FROM digests WHERE edition_date=?", (TODAY.isoformat(),)
    ).fetchone()["digest_id"]
    detail = get_item(db_conn, digest_id)
    assert detail["kind"] == "digest"
    assert [member["item_id"] for member in detail["members"]] == ["item:00", "item:01", "item:02"]
    assert detail["edition_date"] == TODAY.isoformat()
    for member in detail["members"]:
        assert member["title"] and "same_day_url" in member
    with pytest.raises(KeyError):
        get_item(db_conn, "digest:missing")


def test_media_items_carry_kind_and_media_block(db_conn, tmp_path):
    seed_candidate(db_conn, 1)
    upsert_item(
        db_conn,
        make_item(item_id="item:pod", source="podcast_new:show_a", url="https://pod.example/1", title="播客一期", body="转录正文"),
        adapter_name="podcast_new",
        source_payload_json='{"show_name": "某播客"}',
    )
    db_conn.execute("UPDATE items SET transcript=? WHERE item_id='item:pod'", ("转录全文" * 50,))
    db_conn.commit()
    detail = get_item(db_conn, "item:pod")
    assert detail["kind"] == "media"
    assert detail["media"]["type"] == "audio"
    assert detail["media"]["transcript_chars"] == 200
    assert detail["media"]["duration_s"] is None
    # 普通来源不是媒体
    assert get_item(db_conn, "item:01")["kind"] == "article"
    assert get_item(db_conn, "item:01")["media"] is None