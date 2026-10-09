from __future__ import annotations

import os
import tempfile

# 包初始化时就按 PIL_HOME 解析数据目录, 所以必须在导入 personal_intel_loop 之前设好:
# 测试的缺省路径(运行记录、缓存、staging)一律落到临时目录, 不碰真实的 Application Support。
os.environ["PIL_HOME"] = tempfile.mkdtemp(prefix="pil_test_home_")
for _name in ("PIL_DATA_DIR", "PIL_DB_PATH", "PIL_STAGING_DIR", "PIL_VAULT_DIR", "PIL_CONFIG_DIR"):
    os.environ.pop(_name, None)

from datetime import datetime, timezone

import pytest

from personal_intel_loop.schemas import Item
from personal_intel_loop.store import connect_db, ensure_schema


def make_item(
    *,
    item_id: str = "rss:test-item",
    source: str = "rss_briefing:reuters_top_news",
    url: str = "https://example.com/article",
    title: str = "Example title",
    body: str = "Example body",
    ts: str = "2026-04-19T00:00:00Z",
) -> Item:
    return Item(
        id=item_id,
        source=source,
        url=url,
        title=title,
        body=body,
        author="Example Author",
        ts=ts,
        lang="en",
        summary="Example summary",
        tags=["news", "example"],
    )


@pytest.fixture
def db_conn(tmp_path):
    conn = connect_db(tmp_path / "intel_loop.sqlite")
    ensure_schema(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _isolate_profile_dir(monkeypatch, tmp_path_factory):
    """测试不得读真实画像（PIL_PROFILE_DIR 下的 reading_profile.md、blindspots.md）。"""
    monkeypatch.setenv("PIL_PROFILE_DIR", str(tmp_path_factory.mktemp("profile_isolated")))


@pytest.fixture(autouse=True)
def _relax_paper_length_floors(monkeypatch):
    """报纸出版的两个正文长度下限对既有测试的短假数据关掉；
    专门的下限测试在 test_paper_length_floors.py 里把它们设回来。"""
    import personal_intel_loop.paper as paper

    monkeypatch.setattr(paper, "BLIND_MIN_BODY_CHARS", 0)
    monkeypatch.setattr(paper, "SOCIAL_SINGLETON_MIN_CHARS", 0)


@pytest.fixture(autouse=True)
def _isolate_zero_streak_state(monkeypatch, tmp_path):
    """_ingest_adapter 每轮写 adapter_zero_streaks.json，测试不许写进真实 data/runs。"""
    from personal_intel_loop import cli

    monkeypatch.setattr(cli, "ZERO_STREAK_PATH", tmp_path / "adapter_zero_streaks.json")
