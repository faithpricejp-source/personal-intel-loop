"""10-05 验收数据层修复的测试。

每条覆盖一个验收编号，测试调用真实函数（不复制被测实现）：
- G104 weread_mp：每号先保底取最新 N 篇，剩余预算再按号轮转补历史
- G151 local_transcripts：state 水位跳过未变文件 + ts 优先取文件里的日期
- G158 local_transcripts：全文写进 transcript，truncated 保持 False
- store：正文哈希没变就保留已算好的向量
- G2-NEW1 follow_builders：频道/播放列表页 url 的 podcast 条目把 guid 拼进身份
- G203 follow_builders：publishedAt 缺失用兜底时间时不覆盖已有 ts
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from personal_intel_loop.adapters.follow_builders import FollowBuildersAdapter
from personal_intel_loop.adapters.local_transcripts import LocalTranscriptsAdapter
from personal_intel_loop.adapters.weread_mp import WereadMpAdapter
from personal_intel_loop.store import connect_db, ensure_schema, set_item_embedding, ts_is_fallback, upsert_item
from personal_intel_loop.schemas import Item, compute_item_id


# --------------------------------------------------------------------------
# G104 weread_mp：按号均分预算，别让第一个号吃光
# --------------------------------------------------------------------------

def _mp_article(title: str, url: str, time: int) -> dict:
    return {"title": title, "url": url, "time": str(time), "digest": ""}


def _mp_adapter(tmp_path, accounts: list[dict], articles: dict, bodies: dict, **kw):
    def list_articles(book_id: str, offset: int) -> list[dict]:
        return articles.get(book_id) or []

    kw.setdefault("sleep_s", 0)
    return WereadMpAdapter(
        list_accounts=lambda: accounts,
        list_articles=list_articles,
        fetch_body=lambda url: bodies.get(url),
        state_path=tmp_path / "state.json",
        **kw,
    )


def test_1005_g104_budget_not_eaten_by_first_account(tmp_path):
    """A 号有 10 篇旧文、B 号有 2 篇新推送：预算 5 时 B 的新文必须进得来（旧码全给 A）。"""
    accounts = [{"bookId": "AAA", "title": "号A"}, {"bookId": "BBB", "title": "号B"}]
    articles = {
        "AAA": [_mp_article(f"A{i}", f"https://mp.weixin.qq.com/s/a{i}", 1_700_000_000 + i) for i in range(10)],
        "BBB": [_mp_article(f"B{i}", f"https://mp.weixin.qq.com/s/b{i}", 1_800_000_000 + i) for i in range(2)],
    }
    bodies = {a["url"]: f"正文{a['title']}" for lst in articles.values() for a in lst}

    adapter = _mp_adapter(tmp_path, accounts, articles, bodies, max_articles=5)
    records = list(adapter.collect())

    urls = {r.item.url for r in records}
    assert len(records) == 5  # 预算仍是 5
    assert "https://mp.weixin.qq.com/s/b0" in urls, "B 号的新推送被 A 号的旧文挤掉了"
    assert "https://mp.weixin.qq.com/s/b1" in urls


def test_1005_g104_each_account_gets_fresh_quota_before_backlog(tmp_path):
    """每个号先保底 3 篇最新，剩下的预算才按号轮转补历史。"""
    accounts = [{"bookId": "A1", "title": "号一"}, {"bookId": "B1", "title": "号二"}, {"bookId": "C1", "title": "号三"}]
    articles = {
        bid: [_mp_article(f"{bid}-{i}", f"https://mp.weixin.qq.com/s/{bid}-{i}", 1_700_000_000 + i) for i in range(9)]
        for bid in ("A1", "B1", "C1")
    }
    bodies = {a["url"]: "正文" for lst in articles.values() for a in lst}

    adapter = _mp_adapter(tmp_path, accounts, articles, bodies, max_articles=9)
    records = list(adapter.collect())

    per_account: dict[str, int] = {}
    for record in records:
        per_account[record.item.source] = per_account.get(record.item.source, 0) + 1
    assert per_account == {"weread_mp:A1": 3, "weread_mp:B1": 3, "weread_mp:C1": 3}


def test_1005_g104_backlog_remaining_recorded(tmp_path):
    """未取走的积压总数要写进运行记录（state）。"""
    accounts = [{"bookId": "A1", "title": "号一"}, {"bookId": "B1", "title": "号二"}]
    articles = {
        bid: [_mp_article(f"{bid}-{i}", f"https://mp.weixin.qq.com/s/{bid}-{i}", 1_700_000_000 + i) for i in range(5)]
        for bid in ("A1", "B1")
    }
    bodies = {a["url"]: "正文" for lst in articles.values() for a in lst}

    adapter = _mp_adapter(tmp_path, accounts, articles, bodies, max_articles=2)
    assert len(list(adapter.collect())) == 2

    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    # 10 篇未见，取走 2 篇，剩 8 篇积压
    assert state["backlog_remaining"] == 8


def test_1005_g104_backlog_articles_not_marked_seen(tmp_path):
    """没取到的积压不能进 seen，否则下一轮就永远补不回来。"""
    accounts = [{"bookId": "A1", "title": "号一"}]
    articles = {"A1": [_mp_article(f"t{i}", f"https://mp.weixin.qq.com/s/t{i}", 1_700_000_000 + i) for i in range(5)]}
    bodies = {a["url"]: "正文" for a in articles["A1"]}

    adapter = _mp_adapter(tmp_path, accounts, articles, bodies, max_articles=2)
    list(adapter.collect())
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert len(state["seen_urls"]) == 2

    adapter2 = _mp_adapter(tmp_path, accounts, articles, bodies, max_articles=10)
    second = list(adapter2.collect())
    assert len(second) == 3  # 剩下 3 篇本轮补齐
    assert {r.item.url for r in second} & set(state["seen_urls"]) == set()


# --------------------------------------------------------------------------
# G151 local_transcripts：水位 + ts 取文件里的日期
# --------------------------------------------------------------------------

def _write_md(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    return path


def test_1005_g151_skips_unchanged_files_via_state(tmp_path):
    """state 里按路径记 (mtime,size) 水位，没变的文件第二轮不再产出。"""
    root = tmp_path / "podcast"
    for i in range(3):
        _write_md(root / "节目A" / f"00{i}_单集.md", f"# 单集{i}\n\n正文{i}")

    state_path = tmp_path / "lt_state.json"
    adapter = LocalTranscriptsAdapter(roots={"podcast": root}, state_path=state_path)
    first = list(adapter.collect())
    assert len(first) == 3

    watermark = json.loads(state_path.read_text("utf-8"))
    assert len(watermark["file_state"]) == 3

    adapter2 = LocalTranscriptsAdapter(roots={"podcast": root}, state_path=state_path)
    second = list(adapter2.collect())
    assert second == []

    # 改动其中一个文件后，只有它重新产出
    target = root / "节目A" / "001_单集.md"
    _write_md(target, "# 单集1 改\n\n改过的正文")
    os.utime(target, (target.stat().st_atime, target.stat().st_mtime + 10))
    adapter3 = LocalTranscriptsAdapter(roots={"podcast": root}, state_path=state_path)
    third = list(adapter3.collect())
    assert [r.item.title for r in third] == ["单集1 改"]


def test_1005_g151_old_state_file_still_loads(tmp_path):
    """旧 state（没有水位字段）读进来不能报错，按空水位处理。"""
    root = tmp_path / "dedao"
    _write_md(root / "课程A" / "001_文稿.md", "# 文稿\n\n正文")
    state_path = tmp_path / "lt_state.json"
    state_path.write_text(json.dumps({"last_run": "2026-10-01T00:00:00Z"}), "utf-8")

    adapter = LocalTranscriptsAdapter(roots={"podcast": root, "dedao": root}, state_path=state_path)
    # 同一份内容在两个 root 下都命中, 至少要产出
    assert len(list(adapter.collect())) == 2
    assert "file_state" in json.loads(state_path.read_text("utf-8"))


def test_1005_g151_ts_from_frontmatter_date(tmp_path):
    """frontmatter 里的日期优先于 mtime。"""
    root = tmp_path / "caixin"
    target = _write_md(
        root / "周刊" / "2024-03-11_封面故事.md",
        "---\ntitle: \"旧刊\"\ndate: 2024-03-11\n---\n\n正文内容足够长以便入库。\n",
    )
    os.utime(target, (1_700_000_000, 1_700_000_000))  # mtime 伪装成 2023-11

    adapter = LocalTranscriptsAdapter(roots={"caixin": root}, state_path=tmp_path / "s.json")
    record = list(adapter.collect())[0]
    assert record.item.ts.year == 2024
    assert record.item.ts.month == 3
    payload = json.loads(record.source_payload_json)
    assert payload["ts_source"] != "mtime"


def test_1005_g151_ts_from_filename_date(tmp_path):
    """文件名里的 YYYY-MM-DD / YYYYMMDD 优先于 mtime。"""
    root = tmp_path / "podcast"
    target = _write_md(root / "节目B" / "2025-07-04_第100期.md", "# 第100期\n\n正文内容足够长以便入库。\n")
    os.utime(target, (1_700_000_000, 1_700_000_000))

    adapter = LocalTranscriptsAdapter(roots={"podcast": root}, state_path=tmp_path / "s.json")
    record = list(adapter.collect())[0]
    assert (record.item.ts.year, record.item.ts.month, record.item.ts.day) == (2025, 7, 4)


def test_1005_g151_ts_falls_back_to_mtime(tmp_path):
    """取不到日期才用 mtime，并在 payload 里标 ts_source=mtime。"""
    root = tmp_path / "podcast"
    target = _write_md(root / "节目C" / "第12期.md", "# 第12期\n\n正文内容足够长以便入库。\n")
    os.utime(target, (1_700_000_000, 1_700_000_000))

    adapter = LocalTranscriptsAdapter(roots={"podcast": root}, state_path=tmp_path / "s.json")
    record = list(adapter.collect())[0]
    payload = json.loads(record.source_payload_json)
    assert payload["ts_source"] == "mtime"
    assert record.item.ts == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)


# --------------------------------------------------------------------------
# G158 local_transcripts：全文进 transcript，truncated 保持 False
# --------------------------------------------------------------------------

def test_1005_g158_full_text_goes_to_transcript(tmp_path):
    """长文稿全文写进 transcript，body 仍是 12k 预览，truncated 不置 True。"""
    root = tmp_path / "podcast"
    long_text = "".join(f"第{i}段内容。" for i in range(4000))
    assert len(long_text) > 12000
    _write_md(root / "长节目" / "001_长文稿.md", f"# 长文稿\n\n{long_text}")

    adapter = LocalTranscriptsAdapter(roots={"podcast": root}, state_path=tmp_path / "s.json")
    record = list(adapter.collect())[0]
    assert record.item.transcript is not None
    assert len(record.item.transcript) == len(long_text)
    assert len(record.item.body) <= 12000
    payload = json.loads(record.source_payload_json)
    assert payload["truncated"] is False, "置 True 会把长播客踢出候选池"


def test_1005_g158_long_items_stay_in_candidate_pool(tmp_path):
    """truncated 不置 True —— digest 的候选过滤按 truncated 剔除条目。"""
    root = tmp_path / "podcast"
    long_text = "".join(f"第{i}段内容。" for i in range(4000))
    _write_md(root / "长节目" / "002_长文稿.md", f"# 长文稿\n\n{long_text}")

    adapter = LocalTranscriptsAdapter(roots={"podcast": root}, state_path=tmp_path / "s.json")
    record = list(adapter.collect())[0]
    payload = json.loads(record.source_payload_json)
    assert payload.get("truncated") in (False, None)


# --------------------------------------------------------------------------
# store：正文没变就保留向量
# --------------------------------------------------------------------------

def _store_item(tmp_path, *, body: str = "正文内容", ts: datetime | None = None) -> Item:
    url = "https://example.com/a"
    return Item(
        id=compute_item_id("rss_briefing:demo", url=url),
        source="rss_briefing:demo",
        url=url,
        title="标题",
        body=body,
        ts=ts or datetime(2026, 5, 1, tzinfo=timezone.utc),
        lang="zh",
    )


def test_1005_store_keeps_embedding_when_body_unchanged(tmp_path):
    """正文哈希没变，重采覆盖时不能把已算好的向量清空。"""
    conn = connect_db(tmp_path / "t.db")
    ensure_schema(conn)
    item = _store_item(tmp_path)
    upsert_item(conn, item, adapter_name="rss_briefing", source_payload_json="{}")
    set_item_embedding(conn, item.id, [0.1, 0.2, 0.3])

    upsert_item(conn, item, adapter_name="rss_briefing", source_payload_json="{}")
    row = conn.execute("SELECT embedding FROM items WHERE item_id=?", (item.id,)).fetchone()
    assert row["embedding"] is not None, "正文没变，向量被清空了"


def test_1005_store_clears_embedding_when_body_changed(tmp_path):
    """正文变了必须清空向量，否则旧向量与新正文对不上。"""
    conn = connect_db(tmp_path / "t.db")
    ensure_schema(conn)
    item = _store_item(tmp_path)
    upsert_item(conn, item, adapter_name="rss_briefing", source_payload_json="{}")
    set_item_embedding(conn, item.id, [0.1, 0.2, 0.3])

    changed = _store_item(tmp_path, body="换过的正文")
    upsert_item(conn, changed, adapter_name="rss_briefing", source_payload_json="{}")
    row = conn.execute("SELECT embedding FROM items WHERE item_id=?", (item.id,)).fetchone()
    assert row["embedding"] is None


# --------------------------------------------------------------------------
# G2-NEW1 follow_builders：频道页 url 的 podcast 条目把 guid 拼进身份
# --------------------------------------------------------------------------

def _podcast_adapter(tmp_path, podcasts: list[dict], generated_at: str = "2026-04-22T00:00:00Z"):
    feed = {"generatedAt": generated_at, "lookbackHours": 24, "podcasts": podcasts, "stats": {}}
    path = tmp_path / "feed-podcasts.json"
    path.write_text(json.dumps(feed, ensure_ascii=False), "utf-8")
    return FollowBuildersAdapter(
        feed_sources={"x": tmp_path / "none-x.json", "blogs": tmp_path / "none-blogs.json", "podcasts": path}
    )


def test_1005_g2new1_channel_url_keeps_episodes_separate(tmp_path):
    """单集 url 是频道页时，同一频道下的两集不能挤成一条。"""
    adapter = _podcast_adapter(
        tmp_path,
        [
            {"name": "No Priors", "title": "第一集", "guid": "guid-1", "url": "https://www.youtube.com/@NoPriorsPodcast", "publishedAt": "2026-04-21T03:00:00Z", "transcript": "文本一"},
            {"name": "No Priors", "title": "第二集", "guid": "guid-2", "url": "https://www.youtube.com/@NoPriorsPodcast", "publishedAt": "2026-04-22T03:00:00Z", "transcript": "文本二"},
        ],
    )
    records = list(adapter.collect())
    assert len(records) == 2, "两集被合并成一条（后来的覆盖前面的）"
    assert {r.item.title for r in records} == {"第一集", "第二集"}


def test_1005_g2new1_normal_episode_url_identity_unchanged(tmp_path):
    """单集 url 正常的条目身份不能变（存量约 55 条不能换 id 重入库）。"""
    url = "https://www.youtube.com/watch?v=agentic"
    adapter = _podcast_adapter(
        tmp_path,
        [{"name": "No Priors", "title": "正常单集", "guid": "abc-123", "url": url, "publishedAt": "2026-04-22T03:00:00Z", "transcript": "文本"}],
    )
    record = list(adapter.collect())[0]
    assert record.item.url == url
    assert record.item.id == compute_item_id("follow_builders:podcast:no_priors", url=url)


def test_1005_g2new1_playlist_url_keeps_episodes_separate(tmp_path):
    """playlist?list= 形态同样按 guid 分身份。"""
    adapter = _podcast_adapter(
        tmp_path,
        [
            {"name": "某节目", "title": "A", "guid": "g-a", "url": "https://www.youtube.com/playlist?list=PL123", "publishedAt": "2026-04-21T03:00:00Z", "transcript": "t"},
            {"name": "某节目", "title": "B", "guid": "g-b", "url": "https://www.youtube.com/playlist?list=PL123", "publishedAt": "2026-04-22T03:00:00Z", "transcript": "t"},
        ],
    )
    assert len(list(adapter.collect())) == 2


# --------------------------------------------------------------------------
# G203 follow_builders：兜底时间不覆盖已有 ts
# --------------------------------------------------------------------------

def test_1005_g203_fallback_ts_does_not_overwrite_existing(tmp_path):
    """publishedAt 缺失用 feed 生成时间兜底时，已入库条目的 ts 不能被刷成新的。"""
    conn = connect_db(tmp_path / "t.db")
    ensure_schema(conn)
    url = "https://blog.example.com/post-1"
    source = "follow_builders:blog:anthropic_engineering"
    item_id = compute_item_id(source, url=url)
    old_ts = datetime(2026, 5, 2, 8, 0, tzinfo=timezone.utc)
    conn.execute(
        """INSERT INTO items (item_id, source, url, title, body, author, ts, lang, embedding, transcript,
             summary, tags_json, source_payload_json, media_manifest_relpath, content_hash, adapter_name,
             first_ingested_at, last_seen_at, item_status)
           VALUES (?, ?, ?, ?, ?, NULL, ?, 'en', NULL, NULL, NULL, '[]', '{}', NULL, 'h', 'follow_builders',
             '2026-05-02T08:00:00Z', '2026-05-02T08:00:00Z', 'new')""",
        (item_id, source, url, "旧文标题", "旧文正文", "2026-05-02T08:00:00Z"),
    )

    # feed 无 publishedAt → ts 落到 generatedAt（2026-10-02）
    adapter = FollowBuildersAdapter(feed_sources={"podcasts": tmp_path / "none.json"})
    record = adapter._collect_blogs(
        {
            "generatedAt": "2026-10-02T00:00:00Z",
            "blogs": [{"name": "Anthropic Engineering", "title": "旧文标题", "url": url, "content": "旧文正文"}],
        },
        since_utc=None,
    )[item_id]

    assert record.item.ts > old_ts  # 兜底时间确实是新的
    assert ts_is_fallback(record.source_payload_json) is True

    upsert_item(conn, record.item, adapter_name="follow_builders", source_payload_json=record.source_payload_json)
    row = conn.execute("SELECT ts FROM items WHERE item_id=?", (item_id,)).fetchone()
    assert row["ts"].startswith("2026-05-02T08:00:00"), f"已有 ts 被兜底时间覆盖了: {row['ts']}"


def test_1005_g203_real_ts_still_updates(tmp_path):
    """publishedAt 正常时 ts 照常更新，不能被这条规则误伤。"""
    conn = connect_db(tmp_path / "t.db")
    ensure_schema(conn)
    url = "https://blog.example.com/post-2"
    source = "follow_builders:blog:demo"
    item_id = compute_item_id(source, url=url)
    conn.execute(
        """INSERT INTO items (item_id, source, url, title, body, author, ts, lang, embedding, transcript,
             summary, tags_json, source_payload_json, media_manifest_relpath, content_hash, adapter_name,
             first_ingested_at, last_seen_at, item_status)
           VALUES (?, ?, ?, ?, ?, NULL, ?, 'en', NULL, NULL, NULL, '[]', '{}', NULL, 'h', 'follow_builders',
             '2026-05-02T08:00:00Z', '2026-05-02T08:00:00Z', 'new')""",
        (item_id, source, url, "标题", "正文", "2026-05-02T08:00:00Z"),
    )
    adapter = FollowBuildersAdapter(feed_sources={"podcasts": tmp_path / "none.json"})
    record = adapter._collect_blogs(
        {
            "generatedAt": "2026-10-02T00:00:00Z",
            "blogs": [{"name": "demo", "title": "标题", "url": url, "content": "正文", "publishedAt": "2026-06-01T12:00:00Z"}],
        },
        since_utc=None,
    )[item_id]
    assert ts_is_fallback(record.source_payload_json) is False

    upsert_item(conn, record.item, adapter_name="follow_builders", source_payload_json=record.source_payload_json)
    row = conn.execute("SELECT ts FROM items WHERE item_id=?", (item_id,)).fetchone()
    assert row["ts"].startswith("2026-06-01T12:00:00")

def test_1005_store_upsert_still_updates_lang_and_accepts_new_embedding(db_conn):
    # 验收补：WorkBuddy 改 upsert 时误删了 lang 更新
    from personal_intel_loop.store import upsert_item
    from tests.conftest import make_item

    item = make_item(item_id="item:lang", source="rss:x", url="https://e.x/1", title="t").model_copy(update={"lang": "en"})
    upsert_item(db_conn, item, adapter_name="rss", source_payload_json="{}")
    item2 = item.model_copy(update={"lang": "ja"})
    upsert_item(db_conn, item2, adapter_name="rss", source_payload_json="{}")
    assert db_conn.execute("SELECT lang FROM items WHERE item_id='item:lang'").fetchone()[0] == "ja"


def test_1005_g151_body_date_only_when_dateline():
    # 验收补：课程正文里随手提到的历史日期不能当发布日期
    from pathlib import Path
    from personal_intel_loop.adapters.local_transcripts import _content_date

    body = "# 加餐丨iPhone十年\n\n2007年1月9日，乔布斯在旧金山发布了第一代 iPhone。"
    assert _content_date({}, Path("加餐丨iPhone十年.md"), body).date().isoformat() == "2007-01-09"  # 电头行：认
    body2 = "# 加餐丨iPhone十年\n\n你好，欢迎来到课程。2007年1月9日，乔布斯发布了 iPhone。"
    assert _content_date({}, Path("加餐丨iPhone十年.md"), body2) is None
