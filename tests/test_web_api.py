from personal_intel_loop.profile import pending_proposals
from personal_intel_loop.schemas import Item
from personal_intel_loop.store import upsert_item, upsert_claim
from personal_intel_loop.web import handle_claim_resolve, handle_feedback, handle_profile_save, handle_proposal
from tests.conftest import make_item


def test_web_feedback_is_idempotent_and_returns_trust_delta(db_conn, monkeypatch, tmp_path):
    upsert_item(db_conn, make_item(item_id="rss:web", source="rss:websource"), adapter_name="rss_briefing", source_payload_json="{}")
    import personal_intel_loop.discuss as discuss
    monkeypatch.setattr(discuss, "emit_discuss_packet", lambda *a, **kw: None)
    first = handle_feedback(db_conn, {"item_id": "rss:web", "action": "deep_discuss", "digest_date": "2026-08-26"})
    second = handle_feedback(db_conn, {"item_id": "rss:web", "action": "deep_discuss", "digest_date": "2026-08-26"})
    assert first["recorded"] is True and second["recorded"] is False
    assert "trust_before" in first and "trust_after" in first


def test_web_claim_proposal_and_profile_handlers(db_conn, tmp_path, monkeypatch):
    upsert_item(db_conn, make_item(item_id="rss:claim"), adapter_name="rss_briefing", source_payload_json="{}")
    cid = upsert_claim(db_conn, item_id="rss:claim", source="rss:test", claim="will happen", check_after="2026-09-01")
    assert handle_claim_resolve(db_conn, {"claim_id": cid, "outcome": "true"}) == {"ok": True}
    profile = tmp_path / "reading_profile.md"
    profile.write_text("# p\n\n## 待接受的修订\n- accept me\n- reject me\n", "utf-8")
    assert handle_proposal({"line": "accept me", "decision": "accept"}, profile_path=profile)["pending_count"] == 1
    assert "## 已接受的修订" in profile.read_text() and "- accept me" in profile.read_text()
    monkeypatch.setattr("personal_intel_loop.profile.RUNS_DIR", tmp_path / "runs")
    handle_proposal({"line": "reject me", "decision": "reject"}, profile_path=profile)
    assert "reject me" in (tmp_path / "runs" / "profile_rejected.log").read_text()
    save = handle_profile_save({"text": "# changed\n"}, profile_path=profile, runs_dir=tmp_path / "backups")
    assert save["ok"] and profile.read_text() == "# changed\n"
    assert list((tmp_path / "backups").glob("profile_backup_*.md"))


def test_reason_codes_are_mutually_exclusive(db_conn, monkeypatch, tmp_path):
    """改主意 = 替换, 不是叠加。

    2026-08-26 实测缺陷: 用户先点「不感兴趣」(-0.5) 又改点「深挖」(+1.5),
    两条事件都留在库里同时生效, 该源信任被一升一降的矛盾信号污染。
    """
    from personal_intel_loop import web
    from personal_intel_loop.store import upsert_item
    from tests.conftest import make_item

    monkeypatch.setattr(web, "STAGING_DIR", tmp_path)
    upsert_item(db_conn, make_item(item_id="rss:mx"), adapter_name="rss_briefing", source_payload_json="{}")

    first = web.handle_feedback(db_conn, {"item_id": "rss:mx", "action": "not_interested", "digest_date": "2026-08-26"})
    assert first["recorded"] is True and first["replaced"] == []

    second = web.handle_feedback(db_conn, {"item_id": "rss:mx", "action": "deep_discuss", "digest_date": "2026-08-26"})
    assert second["recorded"] is True and second["replaced"] == ["not_interested"]

    kinds = [r[0] for r in db_conn.execute(
        "select event_type from promotion_events where item_id='rss:mx'")]
    assert kinds == ["deep_discuss"]          # 旧的已被删除, 不再叠加


def test_repeated_same_action_is_idempotent(db_conn, monkeypatch, tmp_path):
    from personal_intel_loop import web
    from personal_intel_loop.store import upsert_item
    from tests.conftest import make_item

    monkeypatch.setattr(web, "STAGING_DIR", tmp_path)
    upsert_item(db_conn, make_item(item_id="rss:idem"), adapter_name="rss_briefing", source_payload_json="{}")
    payload = {"item_id": "rss:idem", "action": "keep", "digest_date": "2026-08-26"}
    assert web.handle_feedback(db_conn, payload)["recorded"] is True
    for _ in range(5):                        # 连点 5 下
        r = web.handle_feedback(db_conn, payload)
        assert r["recorded"] is False and r["replaced"] == []
    assert db_conn.execute("select count(*) from promotion_events where item_id='rss:idem'").fetchone()[0] == 1


def test_proposal_card_onclick_attribute_not_truncated():
    """2026-10-02 复核 NJ22: json.dumps 的双引号未转义, 截断 onclick 属性, 按钮点击即 SyntaxError。"""
    from html.parser import HTMLParser
    import json
    from personal_intel_loop.web import _proposal_card

    line = '- [源 "X" 信任] 依据: 连续 3 次「深挖」 → 目标: 提高 → 建议: 加 0.1'
    onclicks = []

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == "button":
                onclicks.append(dict(attrs)["onclick"])

    P().feed(_proposal_card(line))
    assert len(onclicks) == 2
    for js in onclicks:
        assert js.endswith(",this)")
        assert json.dumps(line, ensure_ascii=False) in js
