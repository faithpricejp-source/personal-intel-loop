"""微信公众号 adapter（经微信读书，`weread_mp`）。

用户的公众号关注流在微信读书里能拿到（书架上的「公众号」是书），所以不走微信读书没有的
开放接口。所有取数注入：

    list_accounts() -> [{"bookId", "title"}]                # 书架上的公众号
    list_articles(bookId, offset) -> [{"title","url","time","digest"}]
    fetch_body(url) -> str | None                           # 文章正文

    Item(source="weread_mp:<bookId>", author=号名)

两个真实世界的坑：
- 限流时微信读书不报错，返回一个**约 31KB 的空壳页**：只有 `window.cgiData`，没有正文。
  硬抠只会得到一堆前端 JS，所以认出来就停止本轮正文抓取（已拿到的照常产出），下轮再来。
- 一个号失败不能拖垮其它号，所以逐号 try/except。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlsplit

import os

from personal_intel_loop import APP_HOME, DATA_DIR
from personal_intel_loop.schemas import Item, compute_item_id

logger = logging.getLogger(__name__)

# ---- 缺省常量--------------------------------------------------------------
DEFAULT_STATE_PATH = DATA_DIR / "cache" / "weread_mp_state.json"
# 不自己登录抓取：由外部导出工具（例如开源的 wechat-article-exporter）用微信读书登录态
# 把新文章落成 <账号>/<标题>.md + .json；这里只读那批文件（profile_dir 参数沿用名字，含义是文章根目录）。
# 环境变量 PIL_WEREAD_ARTICLES_DIR 覆盖。
DEFAULT_PROFILE_DIR = Path(
    os.environ.get("PIL_WEREAD_ARTICLES_DIR") or (APP_HOME / "weread_articles")
).expanduser()
WEREAD_SHELF_URL = "https://weread.qq.com/web/shelf"
DEFAULT_MAX_ARTICLES = 60
DEFAULT_SLEEP_S = 0.0  # 读本机文件，无需间隔
DEFAULT_TIMEOUT_S = 20.0

# ---- 行为常量--------------------------------------------------------------
SEEN_URL_LIMIT = 4000
BODY_MAX = 100000
SUMMARY_MAX = 2000
TITLE_MAX = 512
FIRST_PAGE_OFFSET = 0  # 每个号只取第1 页 = 最新一批推送
# 每个号先保底取这么多篇最新未见的，剩下的预算才按号轮转补历史。
# 否则总预算被所有号共享、按目录名排序时，第一个号会吃光预算，其余号（含当天新推送）饿死。
FRESH_PER_ACCOUNT = 3
#: 单号待取列表上限：`default_list_articles` 一次给全量历史，这里只留一个够补历史的窗口。
BACKLOG_WINDOW = 200
#: 微信读书限流空壳页判据: 体积接近 31KB 且只有 window.cgiData。
SHELL_PAGE_MIN_CHARS = 20_000
SHELL_MARKER = "window.cgiData"

_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


def is_shell_page(text: str | None) -> bool:
    """微信读书限流空壳页：体积大 + 只有 window.cgiData + 没有正文容器。"""
    body = str(text or "")
    if len(body) < SHELL_PAGE_MIN_CHARS:
        return False
    if SHELL_MARKER not in body:
        return False
    # 有正文容器就说明真拿到了文章
    return "js_content" not in body and "article-content" not in body


def coerce_ts(value: object) -> datetime | None:
    """`time` 是秒级 epoch 字符串; 毫秒也一并吃。"""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number > 10_000_000_000:
        number //= 1000
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def clean_body(text: str) -> str:
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in cleaned.splitlines()]
    return _MULTI_NEWLINE_RE.sub("\n\n", "\n".join(lines)).strip()


def _usable_url(url: str) -> bool:
    """`Item.url` 要过canonicalize_url，带 host 的绝对地址才算能用。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return bool(parts.scheme in ("http", "https") and parts.netloc)


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(path)
    except OSError as exc:  # pragma: no cover
        logger.warning("weread_mp: 无法写 state %s: %s", path, exc)


_MD_BY_URL: dict[str, Path] = {}


def default_list_accounts(
    *,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[dict]:
    if not profile_dir.is_dir():
        raise RuntimeError(f"公众号文章目录不存在: {profile_dir}")
    return [{"bookId": d.name, "title": d.name} for d in sorted(profile_dir.iterdir()) if d.is_dir()]


def default_list_articles(
    book_id: str,
    offset: int,
    *,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[dict]:
    """返回该账号目录下全部文章（按发布时间倒序）；offset>0 返回空（文件一次给全）。"""
    if offset:
        return []
    out = []
    for meta_path in (profile_dir / book_id).glob("*.json"):
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        md = meta_path.with_suffix(".md")
        if not md.exists():
            continue
        ts = None
        if meta.get("published_at"):
            try:
                ts = datetime.strptime(meta["published_at"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
            except (TypeError, ValueError):  # published_at 被写坏成数字/列表时 strptime 抛 TypeError, 只该废这一篇的时间
                ts = None
        link = meta.get("url") or ""
        if not link.startswith("http"):
            continue
        _MD_BY_URL[link] = md
        out.append({"title": meta.get("title") or md.stem, "url": link, "time": int(ts or md.stat().st_mtime), "digest": ""})
    return sorted(out, key=lambda x: x["time"] or 0, reverse=True)


def default_fetch_body(
    url: str,
    *,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> str | None:
    """按 list_articles 记下的映射读本机 .md；去掉 write_out 写的头部（第一个 --- 之前）。"""
    md = _MD_BY_URL.get(url)
    if md is None:
        return None
    text = md.read_text("utf-8")
    head, sep, body = text.partition("\n---\n")
    return (body if sep else text).strip() or None


class WereadMpAdapter:
    name = "weread_mp"

    def __init__(
        self,
        *,
        list_accounts: Callable[[], list[dict]] | None = None,
        list_articles: Callable[[str, int], list[dict]] | None = None,
        fetch_body: Callable[[str], str | None] | None = None,
        state_path: Path = DEFAULT_STATE_PATH,
        profile_dir: Path = DEFAULT_PROFILE_DIR,
        max_articles: int = DEFAULT_MAX_ARTICLES,
        sleep_s: float = DEFAULT_SLEEP_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.list_accounts = list_accounts or (
            lambda: default_list_accounts(profile_dir=profile_dir, timeout=timeout_s)
        )
        self.list_articles = list_articles or (
            lambda book_id, offset: default_list_articles(
                book_id, offset, profile_dir=profile_dir, timeout=timeout_s
            )
        )
        self.fetch_body = fetch_body or (
            lambda url: default_fetch_body(url, profile_dir=profile_dir, timeout=timeout_s)
        )
        self.state_path = state_path
        self.max_articles = max_articles
        self.sleep_s = sleep_s
        #: 本轮是否因限流提前收工
        self.throttled = False

    def _candidates(
        self,
        book_id: str,
        account_name: str,
        *,
        seen_set: set[str],
        since_utc: datetime | None,
    ) -> list[tuple[datetime, dict]]:
        """列该号的候选。只过滤，不动 seen —— 取不取由预算决定。

        保持 list_articles 给的顺序（`default_list_articles` 已按发布时间倒序），
        不在这里重排：限流时要保留「先拿到的那几篇照常产出」的既有语义。
        """
        articles = self.list_articles(book_id, FIRST_PAGE_OFFSET) or []
        out: list[tuple[datetime, dict]] = []
        for article in articles:
            if not isinstance(article, dict):
                continue
            url = str(article.get("url") or "").strip()
            if not url or url in seen_set:
                continue  # 已见过的 url 跳过
            if not _usable_url(url):
                # 列表里混进相对路径 / 非 http 的脏数据，一条坏数据不该毁掉整轮
                logger.info("weread_mp: 跳过不可用 url %r（号「%s」）", url, account_name)
                continue
            ts = coerce_ts(article.get("time"))
            if ts is None:
                logger.info("weread_mp: 跳过 %s, 时间解析失败", url)
                continue
            if since_utc is not None and ts < since_utc:
                continue
            out.append((ts, article))
        return out[:BACKLOG_WINDOW]

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable[ItemRecord]:
        since_utc = since.astimezone(timezone.utc) if since else None
        state = _load_state(self.state_path)
        seen: list[str] = [str(value) for value in (state.get("seen_urls") or [])]
        seen_set = set(seen)
        records: dict[str, ItemRecord] = {}
        self.throttled = False

        try:
            accounts = self.list_accounts() or []
        except Exception as exc:  # noqa: BLE001 - 书架拿不到整轮没意义
            logger.warning("weread_mp: 书架取数失败: %s", exc)
            return []

        fetched_bodies = 0
        #: 阶段二待轮转的积压: [(book_id, account_name, 剩余候选)]
        backlog: list[tuple[str, str, list[tuple[datetime, dict]]]] = []
        backlog_remaining = 0

        def take(book_id: str, account_name: str, ts: datetime, article: dict) -> bool:
            """抓一篇正文入库。返回 False = 限流，本轮立即收工。"""
            nonlocal fetched_bodies
            url = str(article.get("url") or "").strip()
            seen_set.add(url)
            seen.append(url)

            try:
                raw_body = self.fetch_body(url)
            except Exception as exc:  # noqa: BLE001
                logger.info("weread_mp: 正文抓取失败 %s: %s", url, exc)
                raw_body = None

            if is_shell_page(raw_body):
                logger.warning("weread_mp: 号「%s」返回限流空壳页, 停止本轮正文抓取", account_name)
                self.throttled = True
                seen_set.discard(url)
                seen.remove(url)
                return False

            fetched_bodies += 1
            digest = str(article.get("digest") or "").strip()
            body = clean_body(raw_body or "") or digest
            if not body:
                logger.info("weread_mp: 正文与 digest 都空, 跳过 %s", url)
                return True

            title = str(article.get("title") or "").strip() or digest[:120] or url
            source = f"{self.name}:{book_id}"
            item = Item(
                id=compute_item_id(source, url=url),
                source=source,
                url=url,
                title=title[:TITLE_MAX],
                body=body[:BODY_MAX],
                author=account_name[:256],
                ts=ts,
                lang="zh",
                summary=body[:SUMMARY_MAX] or None,
                tags=["weread_mp", "公众号"],
            )
            payload = {
                "book_id": book_id,
                "account_name": account_name,
                "fetched_body": bool(clean_body(raw_body or "")),
                "used_digest": not clean_body(raw_body or ""),
                "digest": digest or None,
                "body_chars": len(body),
            }
            records[item.id] = ItemRecord(
                item=item,
                adapter_name=self.name,
                source_payload_json=json.dumps(payload, ensure_ascii=False),
                media_urls=[],
            )
            return True

        # 阶段一：每个号先保底取最新 FRESH_PER_ACCOUNT 篇（保证当天新推送进得来）。
        for acc_idx, account in enumerate(accounts):
            if self.throttled:
                break
            if not isinstance(account, dict):
                continue
            book_id = str(account.get("bookId") or "").strip()
            account_name = str(account.get("title") or "").strip() or book_id
            if not book_id:
                continue

            try:
                candidates = self._candidates(
                    book_id, account_name, seen_set=seen_set, since_utc=since_utc
                )
            except Exception as exc:  # noqa: BLE001 - 一个号失败不拖垮其它号
                logger.info("weread_mp: 号「%s」列表失败: %s", account_name, exc)
                continue

            fresh_taken = 0
            for ts, article in candidates:
                if fresh_taken >= FRESH_PER_ACCOUNT or fetched_bodies >= self.max_articles:
                    break
                if not take(book_id, account_name, ts, article):
                    break
                fresh_taken += 1
                seen_set.add(str(article.get("url") or "").strip())

            remaining = [
                pair
                for pair in candidates[fresh_taken:]
                if str(pair[1].get("url") or "").strip() not in seen_set
            ]
            backlog_remaining += len(remaining)
            backlog.append((book_id, account_name, remaining))

            if acc_idx + 1 < len(accounts) and self.sleep_s > 0 and not self.throttled:
                time.sleep(self.sleep_s)

        # 阶段二：预算还有余额就按号轮转补历史，避免先来的号把额度吃光。
        while not self.throttled and fetched_bodies < self.max_articles:
            progressed = False
            for book_id, account_name, remaining in backlog:
                if fetched_bodies >= self.max_articles or self.throttled:
                    break
                while remaining:
                    ts, article = remaining.pop(0)
                    url = str(article.get("url") or "").strip()
                    if url in seen_set:
                        continue
                    if not take(book_id, account_name, ts, article):
                        progressed = True
                        break
                    seen_set.add(url)
                    backlog_remaining -= 1
                    progressed = True
                    break
            if not progressed:
                break

        state = {
            "seen_urls": seen[-SEEN_URL_LIMIT:],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            # 10-05 验收 G104：未读积压总数写进运行记录，否则饿死不可见。
            "backlog_remaining": max(backlog_remaining, 0),
        }
        if self.throttled:
            state["throttled"] = True
            state["throttled_at"] = state["updated_at"]
        _save_state(self.state_path, state)

        out = sorted(records.values(), key=lambda record: record.item.ts, reverse=True)
        return out[:limit] if limit is not None else out