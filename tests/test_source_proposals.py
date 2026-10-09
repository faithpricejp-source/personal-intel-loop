import json

from personal_intel_loop import source_proposals as sp
from personal_intel_loop.profile import append_proposal, pending_proposals


def _line(kind="rss", url="https://newmandala.org/feed/", target="盲区-东南亚"):
    return sp.format_source_line(date_iso="2026-10-05", name="New Mandala", basis="澳洲国立大学托管", target=target, kind=kind, url=url)


def test_parse_and_accept_rss_adds_feed_once(tmp_path):
    rss = tmp_path / "rss.json"
    rss.write_text(json.dumps([{"name": "x", "url": "https://x/feed"}]), "utf-8")
    kw = dict(rss_path=rss, podcasts_path=tmp_path / "p.json", html_todo_path=tmp_path / "h.json", log_path=tmp_path / "log.jsonl")
    line = _line()
    assert sp.is_source_line(line) and sp.parse_source_line(line)["url"] == "https://newmandala.org/feed/"
    assert sp.apply_decision(line, "accept", **kw)["applied"] == "rss.json"
    sp.apply_decision(line, "accept", **kw)
    feeds = json.loads(rss.read_text("utf-8"))
    assert [f["url"] for f in feeds].count("https://newmandala.org/feed/") == 1 and feeds[-1]["category"] == "盲区-东南亚"
    assert len((tmp_path / "log.jsonl").read_text("utf-8").splitlines()) == 2


def test_reject_changes_nothing_and_podcast_html_routes(tmp_path):
    kw = dict(rss_path=tmp_path / "rss.json", podcasts_path=tmp_path / "p.json", html_todo_path=tmp_path / "h.json", log_path=tmp_path / "log.jsonl")
    assert sp.apply_decision(_line(), "reject", **kw)["applied"] is None and not (tmp_path / "rss.json").exists()
    sp.apply_decision(_line("podcast", "https://feeds.storyfm.cn/storyfm.xml", "播客-普通人"), "accept", **kw)
    sp.apply_decision(_line("html", "https://www.zaobao.com.sg/news/sea"), "accept", **kw)
    assert json.loads((tmp_path / "p.json").read_text("utf-8"))[0]["feed_url"].endswith("storyfm.xml")
    assert json.loads((tmp_path / "h.json").read_text("utf-8"))[0]["url"].endswith("/news/sea")


def test_web_handler_source_line_removed_from_pending_not_added_to_profile(tmp_path, monkeypatch):
    from personal_intel_loop import web

    prof = tmp_path / "reading_profile.md"
    prof.write_text("# p\n- 规则\n\n## 待接受的修订\n", "utf-8")
    line = _line()
    append_proposal(line, prof)
    monkeypatch.setattr(sp, "RSS_FEEDS_PATH", tmp_path / "rss.json")
    monkeypatch.setattr(sp, "DECISIONS_LOG", tmp_path / "log.jsonl")
    monkeypatch.setattr(web, "RUNS_DIR", tmp_path, raising=False)
    import personal_intel_loop.profile as profile_mod
    monkeypatch.setattr(profile_mod, "RUNS_DIR", tmp_path)
    out = web.handle_proposal({"line": line, "decision": "accept"}, profile_path=prof)
    assert out["pending_count"] == 0 and pending_proposals(prof) == []
    assert "New Mandala" not in prof.read_text("utf-8")
