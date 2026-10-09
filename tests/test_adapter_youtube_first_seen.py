"""2026-10-02 复核 NJ09: 合成 ts 每轮刷新, 老视频反复以新鲜身份进日报。"""
from datetime import datetime, timedelta, timezone

import personal_intel_loop.adapters.youtube_followed as yf


def _adapter(tmp_path, monkeypatch, videos):
    monkeypatch.setattr(yf, "load_channels", lambda _f: [{"channel_id": "UC1", "channel_name": "c"}])
    monkeypatch.setattr(yf, "fetch_channel_videos", lambda _cid, limit=15: videos)
    monkeypatch.setattr(yf, "fetch_transcript", lambda _vid: "")
    return yf.YouTubeFollowedAdapter(first_seen_path=tmp_path / "first_seen.json")


def test_ts_is_pinned_to_first_seen(tmp_path, monkeypatch):
    videos = [{"video_id": "v1", "title": "t1"}, {"video_id": "v2", "title": "t2"}]
    a = _adapter(tmp_path, monkeypatch, videos)
    first = {r.item.title: r.item.ts for r in a.collect()}
    second = {r.item.title: r.item.ts for r in a.collect()}
    assert first == second


def test_old_video_drops_out_of_lookback(tmp_path, monkeypatch):
    videos = [{"video_id": "v1", "title": "t1"}]
    a = _adapter(tmp_path, monkeypatch, videos)
    old = datetime.now(timezone.utc) - timedelta(days=10)
    (tmp_path / "first_seen.json").write_text('{"v1": "%s"}' % old.isoformat(), "utf-8")
    assert list(a.collect()) == []
