from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from personal_intel_loop.adapters.follow_builders import FollowBuildersAdapter


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    return path


def _make_fixture(tmp_path: Path) -> dict[str, Path]:
    x_feed = {
        "generatedAt": "2026-04-22T00:00:00Z",
        "lookbackHours": 24,
        "x": [
            {
                "source": "x",
                "name": "Swyx",
                "handle": "swyx",
                "bio": "builder bio",
                "tweets": [
                    {
                        "id": "101",
                        "text": "ship the boring plumbing first",
                        "createdAt": "2026-04-21T12:00:00Z",
                        "url": "https://x.com/swyx/status/101",
                        "likes": 5,
                        "retweets": 1,
                        "replies": 0,
                        "isQuote": False,
                        "quotedTweetId": None,
                    }
                ],
            }
        ],
        "stats": {"builders": 1, "tweets": 1},
    }
    blogs_feed = {
        "generatedAt": "2026-04-22T00:00:00Z",
        "lookbackHours": 24,
        "blogs": [
            {
                "source": "blog",
                "name": "Claude Blog",
                "title": "Preparing your security program for AI offense",
                "url": "https://claude.com/blog/security-program",
                "publishedAt": "Apr 10, 2026",
                "author": "",
                "description": "security writeup",
                "content": "Longer blog body here.",
            }
        ],
        "stats": {"blogs": 1},
    }
    podcasts_feed = {
        "generatedAt": "2026-04-22T00:00:00Z",
        "lookbackHours": 24,
        "podcasts": [
            {
                "source": "podcast",
                "name": "No Priors",
                "title": "Agentic economy",
                "guid": "abc-123",
                "url": "https://www.youtube.com/watch?v=agentic",
                "publishedAt": "2026-04-22T03:00:00.000Z",
                "transcript": "Speaker 1 | 00:01 opening line. Speaker 2 | 00:10 deeper line.",
            }
        ],
        "stats": {"podcasts": 1},
    }
    return {
        "x": _write_json(tmp_path / "feed-x.json", x_feed),
        "blogs": _write_json(tmp_path / "feed-blogs.json", blogs_feed),
        "podcasts": _write_json(tmp_path / "feed-podcasts.json", podcasts_feed),
    }


def test_follow_builders_collects_all_three_feed_types(tmp_path: Path) -> None:
    feeds = _make_fixture(tmp_path)
    adapter = FollowBuildersAdapter(feed_sources=feeds)
    records = list(adapter.collect())

    assert len(records) == 3
    item_ids = {record.item.id for record in records}
    assert any(item_id.startswith("follow_builders:") for item_id in item_ids)

    by_source = {record.item.source: record for record in records}
    assert "follow_builders:x:swyx" in by_source
    assert "follow_builders:blog:claude_blog" in by_source
    assert "follow_builders:podcast:no_priors" in by_source

    tweet_payload = json.loads(by_source["follow_builders:x:swyx"].source_payload_json)
    assert tweet_payload["kind"] == "x"
    assert tweet_payload["handle"] == "swyx"

    blog_record = by_source["follow_builders:blog:claude_blog"]
    assert blog_record.item.author == "Claude Blog"
    assert "Longer blog body" in blog_record.item.body

    podcast_record = by_source["follow_builders:podcast:no_priors"]
    assert podcast_record.item.transcript.startswith("Speaker 1")
    assert podcast_record.item.body.startswith("Speaker 1")


def test_follow_builders_respects_since_and_limit(tmp_path: Path) -> None:
    feeds = _make_fixture(tmp_path)
    adapter = FollowBuildersAdapter(feed_sources=feeds)

    since = datetime(2026, 4, 22, 0, 0, tzinfo=timezone.utc)
    records = list(adapter.collect(since=since, limit=1))

    assert len(records) == 1
    assert records[0].item.source == "follow_builders:podcast:no_priors"


def test_follow_builders_parses_non_iso_blog_date_to_utc(tmp_path: Path) -> None:
    feeds = _make_fixture(tmp_path)
    adapter = FollowBuildersAdapter(feed_sources=feeds)

    records = {record.item.source: record for record in adapter.collect()}
    blog_ts = records["follow_builders:blog:claude_blog"].item.ts

    assert blog_ts == datetime(2026, 4, 10, 0, 0, tzinfo=timezone.utc)
