"""10-04：抓全文时顺带记文章主图，RSS 没图时头版用它。"""
from personal_intel_loop import fulltext
from personal_intel_loop.store import connect_db, ensure_schema


def test_extract_main_image_variants():
    assert fulltext.extract_main_image('<meta property="og:image" content="https://a.com/x.jpg">') == "https://a.com/x.jpg"
    assert fulltext.extract_main_image('<meta content="/img/y.png" name="twitter:image">', "https://b.com/p/1") == "https://b.com/img/y.png"
    assert fulltext.extract_main_image("<html>no image</html>") is None
    assert fulltext.extract_main_image('<meta property="og:image" content="data:image/png;base64,xx">') is None


def test_ensure_fulltext_stores_image(tmp_path):
    conn = connect_db(tmp_path / "t.sqlite")
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO items(item_id,source,url,title,body,author,ts,lang,tags_json,source_payload_json,content_hash,adapter_name,first_ingested_at,last_seen_at) "
        "VALUES('i1','rss_briefing:a','https://e.com/a','t','b',NULL,'2026-10-04T00:00:00Z','zh','[]','{}','h','rss_briefing','2026-10-04T00:00:00Z','2026-10-04T00:00:00Z')"
    )
    conn.commit()
    html = '<html><head><meta property="og:image" content="https://e.com/main.jpg"></head><body><p>正文</p></body></html>'
    fetcher = lambda url: fulltext.fetch_fulltext(url, http_get=lambda u: html, extract=lambda h: "正文")
    fulltext.ensure_fulltext(conn, ["i1"], fetcher=fetcher, sleep_s=0)
    assert conn.execute("SELECT image_url FROM item_fulltext WHERE item_id='i1'").fetchone()[0] == "https://e.com/main.jpg"


def test_failed_fulltext_retried_after_six_hours(tmp_path):
    # 10-05 审计 F23：失败的全文 6 小时后重抓，未到 6 小时不重抓；成功的永不重抓
    from datetime import datetime, timedelta, timezone

    from personal_intel_loop.fulltext import ensure_fulltext
    from personal_intel_loop.store import connect_db, ensure_schema

    conn = connect_db(tmp_path / "t.sqlite")
    ensure_schema(conn)
    now = datetime.now(timezone.utc)
    for item_id, status, age in (("old_fail", "failed", 7), ("new_fail", "failed", 1), ("ok", "ok", 30)):
        conn.execute(
            "INSERT INTO items(item_id,source,url,title,body,author,ts,lang,tags_json,source_payload_json,content_hash,adapter_name,first_ingested_at,last_seen_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, "rss_briefing:x", f"https://e.com/{item_id}", "t", "b", None, "2026-10-04T00:00:00Z", "zh", "[]", "{}", item_id, "rss_briefing", "2026-10-04T00:00:00Z", "2026-10-04T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO item_fulltext (item_id, text, status, fetched_at) VALUES (?, NULL, ?, ?)",
            (item_id, status, (now - timedelta(hours=age)).isoformat().replace("+00:00", "Z")),
        )
    conn.commit()
    calls = []
    ensure_fulltext(conn, ["old_fail", "new_fail", "ok"], fetcher=lambda url: (calls.append(url) or ("正文" * 200, "ok")), sleep_s=0)
    assert calls == ["https://e.com/old_fail"]
    assert conn.execute("SELECT status FROM item_fulltext WHERE item_id='old_fail'").fetchone()[0] == "ok"


def test_event_ts_with_offset_normalized_to_utc():
    # 10-05 审计 F18
    from personal_intel_loop.paper_events import _clean_event

    row = _clean_event({"kind": "open_item", "ts": "2026-10-05T20:00:00+09:00", "item_id": "x"})
    assert row is not None and row[0].startswith("2026-10-05T11:00:00") and row[0].endswith("Z")
    assert _clean_event({"kind": "open_item", "ts": "2026-10-05T20:00:00", "item_id": "x"}) is None
