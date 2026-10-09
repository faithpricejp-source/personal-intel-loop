"""写锁争用时应当**等待**而不是抛 OperationalError。

2026-08-26 实证: busy_timeout=5s 时, 09:00 digest(摘要器每条数十秒)与每 30 分钟的
scan-feedback 撞车, 后者写 source_trust 拿不到锁直接抛 `database is locked`,
静默吞掉一次信任度更新 —— 不报错但数据少一笔, 最难发现的坏法。
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from personal_intel_loop.store import connect_db, ensure_schema


def test_pragmas_set(tmp_path):
    conn = connect_db(tmp_path / "t.sqlite")
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 120_000
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()


def _prepare(db_path):
    """用自建小表测锁, 不耦合 source_trust 的必填列 —— 测的是连接层的锁行为, 与业务表无关。"""
    conn = connect_db(db_path)
    ensure_schema(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS _locktest (k TEXT PRIMARY KEY, v REAL)")
    conn.commit()
    return conn


def _hold_write_lock(db_path, ready, release):
    conn = _prepare(db_path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT OR REPLACE INTO _locktest VALUES ('a', 0.5)")
    ready.set()
    release.wait(timeout=10)
    conn.commit()
    conn.close()


def test_second_writer_waits_instead_of_raising(tmp_path):
    """阳性对照在下面那条: 这里证明现在**不抛**, 那里证明短超时**会抛**——
    两条成对才说明是 busy_timeout 在起作用, 而不是这台机器根本不会争用。"""
    db = tmp_path / "t.sqlite"
    _prepare(db).close()
    ready, release = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold_write_lock, args=(db, ready, release), daemon=True)
    holder.start()
    assert ready.wait(timeout=5), "持锁线程没起来"

    result = {}

    def _write():
        # 连接必须在本线程内建: sqlite3 默认 check_same_thread=True, 跨线程用会抛
        # ProgrammingError(不是 OperationalError), 被漏接的话线程静默死掉,
        # "没有过早失败" 就成了空过的断言。
        conn = connect_db(db)
        try:
            conn.execute("INSERT OR REPLACE INTO _locktest VALUES ('b', 0.7)")
            conn.commit()
            result["ok"] = True
        except Exception as exc:                      # noqa: BLE001 —— 任何异常都要看见
            result["err"] = f"{type(exc).__name__}: {exc}"
        finally:
            conn.close()

    t = threading.Thread(target=_write, daemon=True)
    t.start()
    time.sleep(0.4)                      # 此刻它应当在等锁, 而不是已经炸了
    assert "err" not in result, f"过早失败: {result.get('err')}"
    release.set()
    holder.join(timeout=5)
    t.join(timeout=10)
    assert result.get("ok") is True, f"未能写入: {result}"
    check = connect_db(db)
    assert check.execute("SELECT v FROM _locktest WHERE k='b'").fetchone()[0] == 0.7
    check.close()


def test_short_timeout_still_raises(tmp_path):
    """阴性对照(变异): 把 busy_timeout 调回 5ms, 同样的争用必须炸——
    否则上面那条绿灯可能只是因为没真争用起来。"""
    db = tmp_path / "t.sqlite"
    _prepare(db).close()
    ready, release = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold_write_lock, args=(db, ready, release), daemon=True)
    holder.start()
    assert ready.wait(timeout=5)

    impatient = connect_db(db)
    impatient.execute("PRAGMA busy_timeout=5")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        impatient.execute("INSERT OR REPLACE INTO _locktest VALUES ('c', 0.9)")
        impatient.commit()
    impatient.close()
    release.set()
    holder.join(timeout=5)
