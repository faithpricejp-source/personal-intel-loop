from personal_intel_loop.web_digest import parse_digest


def test_parse_digest_extracts_claims_tiers_and_optional_fields():
    text = """<!-- PIL_DIGEST date=2026-08-26 ranking_version=v2_vault_aligned -->
- profile: v0 · 待接受提案 1 条
## 可检验断言
- [Reuters] prices fall (核验起点: 2026-09-01) <!-- pil_claim=c1 -->
## 📖 Longform (坐下来读)
<!-- PIL_ITEM_START item_id=rss:a source=rss:one -->
## [rss:a] First
- source: rss:one
- llm_why: because
- profile: [material] 命中
![](./x.png)

The summary.

- [原链](https://example.com/a)
**中文译文**

译文内容很长。
- [本地媒体目录](./pil_media/rss_a/)
- 可检验断言: prices fall
<!-- PIL_ITEM_END -->
## 🔁 Pulse (短内容)
<!-- PIL_ITEM_START item_id=rss:b source=rss:two -->
## [rss:b] Minimal
- source: rss:two

Short summary.

- [原链](https://example.com/b)
<!-- PIL_ITEM_END -->
"""
    view = parse_digest(text)
    assert view.date == "2026-08-26" and view.ranking_version == "v2_vault_aligned"
    assert view.claims[0]["claim_id"] == "c1"
    assert view.items[0]["tier"] == "longform"
    assert view.items[1]["tier"] == "pulse"
    assert view.items[0]["url"] == "https://example.com/a"
    assert view.items[0]["profile_line"] == "[material] 命中"
    assert view.items[0]["claim"] == "prices fall"
    assert view.items[0]["summary"] == "The summary."
    assert view.items[0]["translation"] == "译文内容很长。"
    assert view.items[0]["local_media_dir"] == "./pil_media/rss_a/"
    assert view.items[1]["translation"] is None
