"""6.3 推荐关注: 候选统计(排除已关注)、模型挑选写理由、followed 自动判定、dismiss 接口。"""
from __future__ import annotations

from personal_intel_loop.paper_api import (
    decide_follow_suggestion,
    get_editions,
    get_follow_suggestions,
)
from personal_intel_loop.paper_follow import collect_candidates, refresh_statuses, suggest
from personal_intel_loop.store import upsert_item
from tests._platform_helpers import FOLLOW_JSON, MONDAY, NOW, TODAY, seed_candidate
from tests.conftest import make_item
from tests.test_paper_inbox_edition import _seed_edition


def _seed_mentions(conn):
    # rss 条目里提到三个账号: 456(微博) 两次、789(微博) 一次、111(知乎) 一次
    seed_candidate(conn, 1, payload={"feed_name": "源", "mentioned_accounts": [{"id": "456", "name": "甲", "platform": "weibo"}, {"id": "111", "name": "丙", "platform": "zhihu"}]})
    seed_candidate(conn, 2, payload={"feed_name": "源", "endorsed_by": [{"id": "456", "name": "甲", "platform": "weibo", "url": "https://weibo.example/456"}]})
    seed_candidate(conn, 3, payload={"feed_name": "源", "mentioned_accounts": [{"id": "789", "name": "乙", "platform": "weibo"}]})
    # 456 已经出现在用户关注流里 → 应被排除
    upsert_item(
        conn,
        make_item(item_id="item:home", source="weibo_home:456", url="https://weibo.example/home", title="关注流"),
        adapter_name="weibo_home",
        source_payload_json='{"account_name": "甲"}',
    )
    conn.commit()


def test_collect_candidates_excludes_already_followed(db_conn):
    _seed_mentions(db_conn)
    candidates = collect_candidates(db_conn, now_utc=NOW)
    ids = {entry["account_id"] for entry in candidates}
    assert "456" not in ids, "已在关注流里的账号不重复推荐"
    assert ids == {"789", "111"}
    top = next(entry for entry in candidates if entry["account_id"] == "789")
    assert top["platform"] == "weibo" and top["count"] == 1
    assert top["evidence"] and set(top["evidence"][0].keys()) == {"title", "url"}


def test_suggest_writes_rows_with_reason_and_evidence(db_conn):
    _seed_mentions(db_conn)
    result = suggest(db_conn, llm_call=lambda prompt: FOLLOW_JSON, profile_path=None, now_utc=NOW)
    assert result["suggested"] == 1
    suggestions = get_follow_suggestions(db_conn)["suggestions"]
    assert len(suggestions) == 1
    entry = suggestions[0]
    assert set(entry.keys()) == {"id", "platform", "account_id", "label", "url", "reason", "evidence", "status"}
    assert entry["id"] == "weibo:789" and entry["status"] == "new"
    assert entry["reason"] == "证据「乙转了三条行业数据帖」值得跟", "理由必须引用证据条目"
    # 已在表里的不重复推荐
    assert suggest(db_conn, llm_call=lambda prompt: FOLLOW_JSON, now_utc=NOW)["suggested"] == 0


def test_status_becomes_followed_automatically(db_conn):
    _seed_mentions(db_conn)
    db_conn.execute(
        "INSERT INTO follow_suggestions (id, platform, account_id, label, url, reason, evidence_json, status, created_at, updated_at)"
        " VALUES ('weibo:999', 'weibo', '999', '丙', NULL, '理由', '[]', 'new', ?, ?)",
        (NOW, NOW),
    )
    db_conn.commit()
    # 还没进关注流: 保持 new
    assert refresh_statuses(db_conn, now_utc=NOW) == 0
    upsert_item(
        db_conn,
        make_item(item_id="item:home2", source="weibo_home:999", url="https://weibo.example/999", title="关注流"),
        adapter_name="weibo_home",
        source_payload_json="{}",
    )
    conn_updated = refresh_statuses(db_conn, now_utc=NOW)
    assert conn_updated == 1
    statuses = {entry["id"]: entry["status"] for entry in get_follow_suggestions(db_conn)["suggestions"]}
    assert statuses["weibo:999"] == "followed"


def test_decide_dismiss_and_monday_edition_carries_new_only(db_conn):
    _seed_mentions(db_conn)
    for account_id in ("111", "789"):
        db_conn.execute(
            "INSERT INTO follow_suggestions (id, platform, account_id, label, url, reason, evidence_json, status, created_at, updated_at)"
            " VALUES (?, 'weibo', ?, '标签', NULL, '理由', '[]', 'new', ?, ?)",
            (f"weibo:{account_id}", account_id, NOW, NOW),
        )
    db_conn.commit()
    assert decide_follow_suggestion(db_conn, {"id": "weibo:111", "decision": "dismiss"}, now_utc=NOW) == {
        "ok": True, "id": "weibo:111", "status": "dismissed",
    }
    upsert_item(db_conn, make_item(item_id="item:lead", title="头条"), adapter_name="rss_briefing", source_payload_json="{}")
    db_conn.commit()
    # 周一那期带推荐关注小栏, 只放 status=new
    _seed_edition(db_conn, MONDAY, NOW)
    monday = get_editions(db_conn, before="2026-10-06", limit=1)["editions"][0]
    assert [entry["account_id"] for entry in monday["follow_suggestions"]] == ["789"]
    # 非周一不带
    _seed_edition(db_conn, TODAY, NOW)
    today = get_editions(db_conn, before="2026-10-05", limit=1)["editions"][0]
    assert today["follow_suggestions"] == []

def _seed_zhihu_moment(conn, n, *, author_key, author_name, endorsed_by):
    upsert_item(
        conn,
        make_item(item_id=f"item:zh{n}", source=f"zhihu_moments:{author_key}", url=f"https://www.zhihu.com/pin/{n}", title=f"知乎{n}"),
        adapter_name="zhihu_moments",
        source_payload_json=__import__("json").dumps(
            {"author_key": author_key, "author_name": author_name, "endorsed_by": endorsed_by}, ensure_ascii=False
        ),
    )
    conn.commit()


def test_zhihu_candidates_are_endorsed_authors_not_followees(db_conn):
    # 回归：推荐关注曾推出已关注的人（动态里的动作者），且「去关注」跳回报纸（url 为空）
    _seed_zhihu_moment(db_conn, 1, author_key="followee-a", author_name="张三", endorsed_by=["张三发布了想法", "张三"])
    _seed_zhihu_moment(db_conn, 2, author_key="author-b", author_name="李四", endorsed_by=["张三赞同了回答", "张三"])
    candidates = collect_candidates(db_conn, now_utc=NOW)
    zhihu = {c["account_id"]: c for c in candidates if c["platform"] == "zhihu"}
    assert set(zhihu) == {"author-b"}, "已关注的张三（动态里的动作者）不得被推荐，被赞同的作者才是候选"
    assert zhihu["author-b"]["label"] == "李四"
    assert zhihu["author-b"]["url"] == "https://www.zhihu.com/people/author-b"


def test_zhihu_suggestion_becomes_followed_when_author_shows_up_as_actor(db_conn):
    db_conn.execute(
        "INSERT INTO follow_suggestions (id, platform, account_id, label, url, reason, evidence_json, status, created_at, updated_at)"
        " VALUES ('zhihu:author-b', 'zhihu', 'author-b', '李四', NULL, '理由', '[]', 'new', ?, ?)",
        (NOW, NOW),
    )
    db_conn.commit()
    assert refresh_statuses(db_conn, now_utc=NOW) == 0
    _seed_zhihu_moment(db_conn, 3, author_key="someone", author_name="某人", endorsed_by=["李四赞同了回答", "李四"])
    assert refresh_statuses(db_conn, now_utc=NOW) == 1
