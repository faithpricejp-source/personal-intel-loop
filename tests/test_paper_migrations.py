"""迁移 004 在已有 v3 库上能升级; 新表可写。"""
from __future__ import annotations

from personal_intel_loop import MIGRATIONS_DIR
from personal_intel_loop.store import connect_db, ensure_schema, get_user_version, upsert_item
from tests.conftest import make_item

NEW_TABLES = {"item_ai", "item_fulltext", "editions", "item_ratings", "author_trust", "source_overrides", "read_events"}


def _apply_migrations_up_to_v3(conn) -> None:
    for migration in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        if int(migration.name.split("_", 1)[0]) <= 3:
            conn.executescript(migration.read_text("utf-8"))
    conn.execute("PRAGMA user_version=3")


def test_migration_004_upgrades_v3_db(tmp_path):
    conn = connect_db(tmp_path / "db.sqlite")
    try:
        _apply_migrations_up_to_v3(conn)
        upsert_item(conn, make_item(item_id="rss:v3item"), adapter_name="rss_briefing", source_payload_json="{}")
        conn.commit()
        assert get_user_version(conn) == 3

        ensure_schema(conn)

        # TASK2 起 005、TASK3 起 006_inbox、TASK4 起 007_platform 一并应用, schema 版本随迁移数走
        assert get_user_version(conn) == 10  # 010：热路径索引
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert NEW_TABLES <= tables
        # 老数据不动
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        # 新表可写
        conn.execute(
            "INSERT INTO read_events (item_id, dwell_ms, ts) VALUES ('rss:v3item', 1000, '2026-10-04T00:00:00Z')"
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM read_events").fetchone()[0] == 1
    finally:
        conn.close()


def test_ensure_schema_on_fresh_db_creates_004(tmp_path):
    conn = connect_db(tmp_path / "fresh.sqlite")
    try:
        ensure_schema(conn)
        # fresh 库会一路应用到最新迁移(004 之后还有 TASK2 的 005), 版本号随迁移数走
        assert get_user_version(conn) >= 4
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert NEW_TABLES <= tables
    finally:
        conn.close()


def test_editions_and_ratings_check_constraints(tmp_path):
    """editions.section / item_ratings.dim 的 CHECK 约束生效。"""
    conn = connect_db(tmp_path / "db.sqlite")
    try:
        ensure_schema(conn)
        upsert_item(conn, make_item(item_id="rss:c1"), adapter_name="rss_briefing", source_payload_json="{}")
        conn.commit()
        try:
            conn.execute(
                "INSERT INTO editions (edition_date, item_id, section, rank, blind_reason, built_at) VALUES ('2026-10-04', 'rss:c1', 'front', 0, NULL, 'x')"
            )
        except Exception:
            pass
        else:
            raise AssertionError("section CHECK 约束未生效")
        try:
            conn.execute(
                "INSERT INTO item_ratings (item_id, dim, value, author_key, ts, distilled_at) VALUES ('rss:c1', 'bogus', 1, NULL, 'x', NULL)"
            )
        except Exception:
            pass
        else:
            raise AssertionError("dim CHECK 约束未生效")
    finally:
        conn.close()
