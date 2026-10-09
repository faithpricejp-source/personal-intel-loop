"""Vault promotion auto-detection. Fixture vault + real pil sqlite (tmp)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from personal_intel_loop.schemas import Item
from personal_intel_loop.store import upsert_item
from personal_intel_loop.vault_scanner import (
    _classify_object_type,
    _event_id,
    extract_citations,
    load_last_scan_ts,
    save_last_scan_ts,
    scan_vault,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, "utf-8")


def _seed_item(conn, *, item_id: str, url: str, source: str) -> None:
    item = Item(
        id=item_id,
        source=source,
        url=url,
        title="seed",
        body="seed body",
        author="a",
        ts="2026-04-18T00:00:00Z",
        lang="zh",
        tags=[],
    )
    with conn:
        upsert_item(conn, item, adapter_name="weibo_timeline", source_payload_json="{}")


class TestClassifyObjectType:
    def test_jdg(self):
        assert _classify_object_type("JDG_2026-04-18_foo") == "JDG"

    def test_evd(self):
        assert _classify_object_type("EVD_DeepSeekV3TrainedOnH800") == "EVD"

    def test_mec(self):
        assert _classify_object_type("MEC_单点突破与系统收租") == "MEC"

    def test_non_matching(self):
        assert _classify_object_type("README") is None
        assert _classify_object_type("某个笔记") is None


class TestExtractCitations:
    def test_extracts_body_urls(self, tmp_path):
        vault = tmp_path / "vault"
        f = vault / "07 判断与决策" / "Judgments" / "JDG_test.md"
        _write(
            f,
            """---
object_type: judgment
---

## 摘要
参考这条微博 https://m.weibo.cn/status/5000000000000001 里的观点。
""",
        )
        c = extract_citations(f, vault)
        assert c is not None
        assert c.object_type == "JDG"
        assert "https://m.weibo.cn/status/5000000000000001" in c.urls

    def test_extracts_frontmatter_source_field(self, tmp_path):
        vault = tmp_path / "vault"
        f = vault / "03 证据与溯源" / "EVD_sample.md"
        _write(
            f,
            """---
object_type: evidence
source: https://cn.nikkei.com/politicsaeconomy/commodity/62077-2026-04-20-05-00-30.html
---

正文。
""",
        )
        c = extract_citations(f, vault)
        assert c is not None
        assert any("nikkei.com" in u for u in c.urls)

    def test_extracts_frontmatter_list_sources(self, tmp_path):
        vault = tmp_path / "vault"
        f = vault / "01 来源库" / "SRC_alpha.md"
        _write(
            f,
            """---
sources:
  - https://example.com/a
  - https://example.com/b
---

body
""",
        )
        c = extract_citations(f, vault)
        assert c is not None
        assert len(c.urls) >= 2

    def test_extracts_inline_item_id_string(self, tmp_path):
        vault = tmp_path / "vault"
        f = vault / "07 判断与决策" / "Judgments" / "JDG_x.md"
        _write(
            f,
            "## 摘要\n此判断参考 `weibo:1000000001:5000000000000001` 原帖。\n",
        )
        c = extract_citations(f, vault)
        assert c is not None
        assert "weibo:1000000001:5000000000000001" in c.item_ids

    def test_non_tracked_filename_returns_none(self, tmp_path):
        vault = tmp_path / "vault"
        f = vault / "07 判断与决策" / "随笔.md"
        _write(f, "body with https://example.com")
        assert extract_citations(f, vault) is None

    def test_canonicalizes_urls(self, tmp_path):
        """tracking params should be stripped during canonicalization."""
        vault = tmp_path / "vault"
        f = vault / "01 来源库" / "SRC_x.md"
        _write(
            f,
            "body\nhttps://example.com/a?utm_source=foo&utm_medium=bar&ref=xyz",
        )
        c = extract_citations(f, vault)
        assert c is not None
        # canonical form strips utm_* and ref
        assert any(url == "https://example.com/a" for url in c.urls)


class TestScanVault:
    def test_inserts_promotion_event_on_url_match(self, db_conn, tmp_path):
        _seed_item(
            db_conn,
            item_id="weibo:1000000001:5000000000000001",
            url="https://m.weibo.cn/status/5000000000000001",
            source="weibo_timeline:1000000001",
        )
        vault = tmp_path / "vault"
        _write(
            vault / "07 判断与决策" / "Judgments" / "JDG_2026-04-18_test.md",
            """---
object_type: judgment
---

## 摘要
引用 https://m.weibo.cn/status/5000000000000001 作为证据。
""",
        )

        result = scan_vault(
            db_conn,
            vault_root=vault,
            since_utc=None,
            persist_last_scan=False,
        )
        assert result.events_inserted == 1
        rows = db_conn.execute(
            "SELECT item_id, event_type, event_weight, vault_object_type, vault_object_id, origin FROM promotion_events"
        ).fetchall()
        assert len(rows) == 1
        r = rows[0]
        assert r["item_id"] == "weibo:1000000001:5000000000000001"
        assert r["event_type"] == "detected_jdg_reference"
        assert r["event_weight"] == 2.0
        assert r["vault_object_type"] == "JDG"
        assert r["vault_object_id"] == "JDG_2026-04-18_test"
        assert r["origin"] == "vault_detection"

    def test_inserts_for_inline_item_id(self, db_conn, tmp_path):
        _seed_item(
            db_conn,
            item_id="weibo:1000000001:5000000000000001",
            url="https://m.weibo.cn/status/5000000000000001",
            source="weibo_timeline:1000000001",
        )
        vault = tmp_path / "vault"
        _write(
            vault / "04 案例库" / "CAS_foo.md",
            "## 摘要\n参考 weibo:1000000001:5000000000000001 的观察。\n",
        )

        result = scan_vault(db_conn, vault_root=vault, persist_last_scan=False)
        assert result.events_inserted == 1
        rows = db_conn.execute("SELECT event_type FROM promotion_events").fetchall()
        assert rows[0]["event_type"] == "detected_cas_link"

    def test_idempotent_across_reruns(self, db_conn, tmp_path):
        _seed_item(
            db_conn,
            item_id="nikkei:abc",
            url="https://cn.nikkei.com/some/article.html",
            source="nikkei_cn:politicsaeconomy",
        )
        vault = tmp_path / "vault"
        _write(
            vault / "03 证据与溯源" / "EVD_nikkei_test.md",
            """---
source: https://cn.nikkei.com/some/article.html
---
""",
        )

        r1 = scan_vault(db_conn, vault_root=vault, persist_last_scan=False)
        r2 = scan_vault(db_conn, vault_root=vault, persist_last_scan=False)
        assert r1.events_inserted == 1
        assert r2.events_inserted == 0
        assert r2.events_skipped_dup == 1
        count = db_conn.execute("SELECT COUNT(*) FROM promotion_events").fetchone()[0]
        assert count == 1

    def test_skips_when_no_url_match(self, db_conn, tmp_path):
        _seed_item(db_conn, item_id="a", url="https://a.com/1", source="rss_briefing:x")
        vault = tmp_path / "vault"
        _write(
            vault / "07 判断与决策" / "Judgments" / "JDG_mismatch.md",
            "引用 https://b.com/2\n",
        )
        result = scan_vault(db_conn, vault_root=vault, persist_last_scan=False)
        assert result.events_inserted == 0
        assert result.citations_found == 1

    def test_mtime_since_filter(self, db_conn, tmp_path):
        import os
        import time

        _seed_item(
            db_conn,
            item_id="weibo:1:p",
            url="https://m.weibo.cn/status/p",
            source="weibo_timeline:1",
        )
        vault = tmp_path / "vault"

        old_file = vault / "07 判断与决策" / "Judgments" / "JDG_old.md"
        _write(old_file, "引用 https://m.weibo.cn/status/p")
        # set mtime deep in the past
        old_ts = time.time() - 7 * 86400
        os.utime(old_file, (old_ts, old_ts))

        new_file = vault / "07 判断与决策" / "Judgments" / "JDG_new.md"
        _write(new_file, "引用 https://m.weibo.cn/status/p")

        since = datetime.now(timezone.utc) - timedelta(days=1)
        result = scan_vault(db_conn, vault_root=vault, since_utc=since, persist_last_scan=False)
        # only the fresh file should be scanned
        assert result.files_scanned == 1
        assert result.events_inserted == 1

    def test_mec_dia_heu_have_dedicated_event_types(self, db_conn, tmp_path):
        _seed_item(db_conn, item_id="xhs:note1", url="https://www.xiaohongshu.com/explore/note1", source="xhs:u1")
        vault = tmp_path / "vault"
        for prefix, object_type_expected in (("MEC", "detected_mec_enrich"),
                                              ("DIA", "detected_dia_enrich"),
                                              ("HEU", "detected_heu_enrich")):
            _write(
                vault / "05 知识本体" / f"{prefix}_sample_{prefix.lower()}.md",
                f"---\nsource: https://www.xiaohongshu.com/explore/note1\n---\n",
            )
        result = scan_vault(db_conn, vault_root=vault, persist_last_scan=False)
        types = {row["event_type"] for row in db_conn.execute("SELECT event_type FROM promotion_events").fetchall()}
        assert types == {"detected_mec_enrich", "detected_dia_enrich", "detected_heu_enrich"}


class TestPlatformUrlFlexibility:
    """Regression: vault-side URL formats must match pil-side stored URLs
    even when query params / path variants differ (same note_id in path)."""

    def test_xhs_explore_vs_discovery_item_both_match(self, db_conn, tmp_path):
        # pil stored xhs URL in /discovery/item/ form (from XHS-Downloader output)
        _seed_item(
            db_conn,
            item_id="xhs:69e30e1c000000001d01aacf",
            url="https://www.xiaohongshu.com/discovery/item/69e30e1c000000001d01aacf?app_platform=ios&xsec_token=abc",
            source="xhs:62a0b4c20000000021022e3a",
        )
        # vault cites it in /explore/ form (from Safari web / xhs app copy-link)
        vault = tmp_path / "vault"
        _write(
            vault / "01 来源库" / "SRC_xhs_sample.md",
            "- 原帖链接:https://www.xiaohongshu.com/explore/69e30e1c000000001d01aacf?xsec_token=XYZ\n",
        )
        result = scan_vault(db_conn, vault_root=vault, persist_last_scan=False)
        assert result.events_inserted == 1
        row = db_conn.execute("SELECT item_id, event_type FROM promotion_events").fetchone()
        assert row["item_id"] == "xhs:69e30e1c000000001d01aacf"
        assert row["event_type"] == "detected_src_reference"

    def test_weibo_id_match_across_url_variants(self, db_conn, tmp_path):
        _seed_item(
            db_conn,
            item_id="weibo:1000000001:5000000000000001",
            url="https://m.weibo.cn/status/5000000000000001",
            source="weibo_timeline:1000000001",
        )
        # vault cites m.weibo.cn/detail/<id> — different path but same post_id
        vault = tmp_path / "vault"
        _write(
            vault / "07 判断与决策" / "Judgments" / "JDG_weibo_ref.md",
            "- https://weibo.com/1000000001/5000000000000001\n",
        )
        result = scan_vault(db_conn, vault_root=vault, persist_last_scan=False)
        assert result.events_inserted == 1

    def test_nikkei_exact_url_fallback_still_works(self, db_conn, tmp_path):
        url = "https://cn.nikkei.com/politicsaeconomy/commodity/62077-2026-04-20-05-00-30.html"
        _seed_item(db_conn, item_id="nikkei:abc", url=url, source="nikkei_cn:politicsaeconomy")
        vault = tmp_path / "vault"
        _write(
            vault / "03 证据与溯源" / "EVD_nikkei.md",
            f"- {url}\n",
        )
        result = scan_vault(db_conn, vault_root=vault, persist_last_scan=False)
        assert result.events_inserted == 1


class TestLastScanPersistence:
    def test_save_then_load_roundtrip(self, tmp_path):
        path = tmp_path / ".last_vault_scan"
        ts = datetime(2026, 4, 20, 5, 0, 0, tzinfo=timezone.utc)
        save_last_scan_ts(ts, path)
        loaded = load_last_scan_ts(path)
        assert loaded == ts

    def test_missing_file_returns_none(self, tmp_path):
        assert load_last_scan_ts(tmp_path / "nonexistent") is None

    def test_corrupt_file_returns_none(self, tmp_path):
        path = tmp_path / ".last_vault_scan"
        path.write_text("not a timestamp", "utf-8")
        assert load_last_scan_ts(path) is None


class TestEventIdDeterminism:
    def test_same_inputs_same_id(self):
        a = _event_id("07 判断与决策/JDG_x.md", "weibo:1:p", "detected_jdg_reference")
        b = _event_id("07 判断与决策/JDG_x.md", "weibo:1:p", "detected_jdg_reference")
        assert a == b

    def test_different_event_type_different_id(self):
        a = _event_id("path", "id", "detected_jdg_reference")
        b = _event_id("path", "id", "detected_evd_reference")
        assert a != b
