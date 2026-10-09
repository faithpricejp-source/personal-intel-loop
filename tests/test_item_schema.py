from __future__ import annotations

import pytest
from pydantic import ValidationError

from personal_intel_loop.schemas import Item
from tests.conftest import make_item


def test_valid_item():
    item = make_item()
    assert item.id == "rss:test-item"
    assert item.url == "https://example.com/article"


def test_item_rejects_too_long_id():
    with pytest.raises(ValidationError):
        make_item(item_id="x" * 129)


def test_item_requires_title():
    with pytest.raises(ValidationError):
        Item(
            id="rss:test",
            source="rss_briefing:test_feed",
            url="https://example.com/article",
            title="",
            body="body",
            author="Author",
            ts="2026-04-19T00:00:00Z",
            lang="en",
            tags=[],
        )
