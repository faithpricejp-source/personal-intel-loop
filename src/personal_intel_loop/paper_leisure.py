"""闲与美栏(契约第 10.3 节): Home Cinema 库近 7 天新增 + 已读账作者的新内容。

Home Cinema 库**只读**打开(sqlite3.connect('file:...?mode=ro', uri=True)), 路径由调用方
注入(缺省链路在 CLI 解析 HOME_CINEMA_DB / 缺省路径——本模块不碰任何真实路径, 测试用
tmp 库)。表结构未知时读 sqlite_master 探表名(列名走参数化 pragma_table_info(?)), 行数据
用 iterdump 文本 + 纯 Python 解析——对外部库**零动态 SQL**。找不到合适表就返回空并记日志。
作者新书读 config/leisure_authors.txt(每行一个作者)在本库 items 标题/正文里匹配近 7 天条目。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from personal_intel_loop.schemas import normalize_dt_to_utc_z

LEISURE_MAX = 4
LOOKBACK_DAYS = 7
HC_ENV_VAR = "HOME_CINEMA_DB"

# 探表用的列名候选(按命中顺序)
_DATE_COLUMNS = ("added_at", "created_at", "date_added", "created", "added", "first_seen_at", "discovered_at", "updated_at")
_TITLE_COLUMNS = ("title", "name", "movie_title", "show_title")
_ENTITY_COLUMNS = ("director", "series", "show_name", "creator", "artist")
_PER_TABLE_ROW_CAP = 50

# sqlite_master 的 CREATE TABLE 解析 / iterdump 的 INSERT 行解析
_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INSERT_RE = re.compile(r'^INSERT INTO "([A-Za-z_][A-Za-z0-9_]*)" VALUES\((.*)\);\s*$', re.S)

logger = logging.getLogger(__name__)


def default_home_cinema_db() -> Path:
    """调用时解析: HOME_CINEMA_DB 覆盖, 否则缺省路径(只给 CLI/生产链路用)。"""
    import os

    env = os.environ.get(HC_ENV_VAR, "").strip()
    if env:
        return Path(env)
    return Path.home() / "Library" / "Application Support" / "HomeCinema" / "library.db"


def default_authors_path() -> Path:
    from personal_intel_loop import CONFIG_DIR

    return CONFIG_DIR / "leisure_authors.txt"


def load_authors(path: Path | None = None) -> list[str]:
    """每行一个作者; 文件缺失/为空 → []。"""
    path = default_authors_path() if path is None else Path(path)
    try:
        lines = path.read_text("utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """参数化表值函数取列名(无动态 SQL); 非法标识符返回 []。"""
    if not _SAFE_IDENT.match(table):
        return []
    try:
        rows = conn.execute("SELECT name FROM pragma_table_info(?)", (table,)).fetchall()
    except sqlite3.Error:
        return []
    return [str(row[0]) for row in rows if _SAFE_IDENT.match(str(row[0]))]


def _pick(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {column.lower(): column for column in columns}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


def _split_sql_values(body: str) -> list[str] | None:
    """把 INSERT 语句的 VALUES(...) 体内文切成逐值文本(处理 '' 转义); 有异常结构返回 None。"""
    values: list[str] = []
    buf: list[str] = []
    in_str = False
    index = 0
    while index < len(body):
        char = body[index]
        if in_str:
            if char == "'":
                if index + 1 < len(body) and body[index + 1] == "'":
                    buf.append("'")
                    index += 2
                    continue
                in_str = False
            buf.append(char)
        elif char == "'":
            in_str = True
            buf.append(char)
        elif char == ",":
            values.append("".join(buf).strip())
            buf = []
        else:
            buf.append(char)
        index += 1
    if in_str:
        return None
    values.append("".join(buf).strip())
    return values


def _coerce(raw: str):
    if raw == "NULL" or raw == "":
        return None
    if len(raw) >= 2 and raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    return raw


def _parse_dump_rows(dump_text: str, wanted: set[str]) -> dict[str, list[list]]:
    """从 iterdump 文本解析 wanted 表的行(纯文本处理, 无 SQL)。解析不了的行跳过。"""
    rows: dict[str, list[list]] = {table: [] for table in wanted}
    buffer: list[str] = []
    for line in dump_text.splitlines():
        statement = " ".join([*buffer, line]).strip()
        if not statement:
            continue
        if not statement.endswith(";"):
            buffer = [statement]
            continue
        buffer = []
        match = _INSERT_RE.match(statement)
        if match is None:
            continue
        table, body = match.group(1), match.group(2)
        if table not in wanted:
            continue
        pieces = _split_sql_values(body)
        if pieces is None:
            continue
        rows[table].append([_coerce(piece) for piece in pieces])
    return rows


def home_cinema_new(db_path, *, today: date, lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """Home Cinema 库近 7 天新增(导演/剧集)。只读打开; 库不存在/没有合适表 → 空列表 + 日志。"""
    path = Path(db_path)
    if not path.is_file():
        logger.info("leisure: home cinema db not found, skip: %s", path)
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.warning("leisure: home cinema db unreadable (%s): %s", path, exc)
        return []
    cutoff = (today - timedelta(days=lookback_days)).isoformat()
    try:
        tables = [str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        probes: dict[str, tuple[str | None, str | None, str | None]] = {}
        for table in tables:
            columns = _table_columns(conn, table)
            date_col = _pick(columns, _DATE_COLUMNS)
            title_col = _pick(columns, _TITLE_COLUMNS)
            entity_col = _pick(columns, _ENTITY_COLUMNS)
            if date_col is None or (title_col is None and entity_col is None):
                continue  # 不是「有标题/实体 + 有日期」的内容表
            probes[table] = (title_col, entity_col, date_col)
        if not probes:
            logger.info("leisure: no title/date table found in home cinema db, skip")
            return []
        # 只读探表 + 行读取全程零动态 SQL: 行数据从 iterdump 文本解析
        rows_by_table = _parse_dump_rows("\n".join(conn.iterdump()), set(probes))
        found: list[dict] = []
        for table, (title_col, entity_col, date_col) in probes.items():
            columns = _table_columns(conn, table)
            scored: list[tuple[str, dict]] = []
            for row in rows_by_table.get(table, []):
                if len(row) != len(columns):
                    continue
                values = dict(zip(columns, row))
                title = str(values.get(title_col) or "").strip()
                entity = str(values.get(entity_col) or "").strip() if entity_col else ""
                added_on = str(values.get(date_col) or "").strip()
                if not added_on or added_on[:10] < cutoff:
                    continue  # 只收近 7 天新增
                if not title and not entity:
                    continue
                heading = f"{entity} 新作/更新: {title}" if entity and title else (entity or title)
                scored.append((added_on, {"title": heading, "added_on": added_on, "table": table}))
            scored.sort(key=lambda pair: pair[0], reverse=True)
            found.extend(entry for _added, entry in scored[:_PER_TABLE_ROW_CAP])
    except (sqlite3.Error, OSError) as exc:
        logger.warning("leisure: home cinema probe failed (%s): %s", path, exc)
        return []
    finally:
        conn.close()
    return found


def author_new_items(conn: sqlite3.Connection, authors: list[str], *, now_utc, lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """已读账作者的新内容: 近 7 天入库、标题/正文含作者名的本库条目。"""
    if not authors:
        return []
    cutoff = normalize_dt_to_utc_z(datetime.fromisoformat(str(now_utc).replace("Z", "+00:00")) - timedelta(days=lookback_days))
    found: list[dict] = []
    seen: set[str] = set()
    for author in authors:
        for row in conn.execute(
            "SELECT item_id, title, url, source, ts FROM items WHERE first_ingested_at >= ? AND (title LIKE ? OR body LIKE ?) ORDER BY ts DESC LIMIT 5",
            (cutoff, f"%{author}%", f"%{author}%"),
        ):
            if row["item_id"] in seen:
                continue
            seen.add(row["item_id"])
            found.append({"item_id": row["item_id"], "title": row["title"], "url": row["url"], "author": author, "source": row["source"]})
    return found


def collect(
    conn: sqlite3.Connection,
    *,
    date_local,
    now_utc,
    home_cinema_db=None,
    authors_path=None,
    max_n: int = LEISURE_MAX,
) -> dict:
    """闲与美候选: {"home_cinema": [描述...], "books": [本库条目...]}。

    上版合成与落库由 paper.py 完成; 这里只做发现, 任何失败降级为空(不挡出版)。
    """
    try:
        home_cinema = home_cinema_new(home_cinema_db, today=date_local) if home_cinema_db is not None else []
    except Exception:  # noqa: BLE001 — 探表失败按无表处理
        logger.exception("leisure: home cinema probe failed")
        home_cinema = []
    try:
        books = author_new_items(conn, load_authors(authors_path), now_utc=now_utc)
    except Exception:  # noqa: BLE001
        logger.exception("leisure: author match failed")
        books = []
    return {"home_cinema": home_cinema[: max(0, max_n)], "books": books[: max(0, max_n)]}
