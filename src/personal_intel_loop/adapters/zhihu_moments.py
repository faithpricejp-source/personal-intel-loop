"""知乎关注动态 adapter (`zhihu_moments`)。

关注动态 (`/api/v4/moments`) 是登录态接口。取数函数 `fetch(url)` 注入, 首次 `url=None`
表示第一页, 之后用 `paging.next`。测试喂`fixtures/zhihu_moments.json`。

    fetch(url|None) -> dict
       ↓ data[] →过滤 feed_advert → 按 target 类型映射 → Item
    Item(source="zhihu_moments:<作者 url_token|id>")

接口结构的坑：
- `author` 在 **`target.author`**，不在 entry 顶层（顶层是 null）。「谁赞同了」在
  `action_text`，同一条 target 被多个关注者赞一次就出现多次 → 合并成一个 Item，谁赞同了
  记进 `source_payload.endorsed_by`（推荐关注用）。
- target 三种：`answer`（挂在 `question` 下，标题用问题标题）、`article`（专栏）、
  `pin`（想法）。`question` 本身（有人关注了问题）本轮不收——没有正文，读报纸没意义。
- 时间字段单位不统一：`answer.created_time` / entry `created_time` 是**秒**，
  `pin.created` / `article.created` 是**毫秒**。写错量级会让条目沉到 1970 或未来。
- 风控：知乎风控返回 HTTP 403 或 body 里带 `40352`，一律立刻停 + paused，不重试。
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

from personal_intel_loop import DATA_DIR
from personal_intel_loop.schemas import Item, compute_item_id

logger = logging.getLogger(__name__)

# ---- 缺省常量--------------------------------------------------------------
DEFAULT_STATE_PATH = DATA_DIR / "cache" / "zhihu_moments_state.json"
import os

#: 知乎登录态（cookie JSON）。环境变量 PIL_ZHIHU_COOKIES 覆盖。
DEFAULT_COOKIES_FILE = Path(os.environ.get("PIL_ZHIHU_COOKIES") or "~/.zhihu-cli/cookies.json").expanduser()
# 2026-10-04 实测：v3 moments + desktop=true 用 zhihu-cli 的 cookie 可取（含正文）
MOMENTS_URL = "https://www.zhihu.com/api/v3/moments?limit=10&desktop=true"
# 10-05 验收 F117: 每天只跑一次, 3 页上限偏紧（生产首轮就翻满 3 页）
DEFAULT_MAX_PAGES = 5  # 10-05 验收：原 3 偏紧；知乎风控敏感，先 5 页，看截断告警再定
DEFAULT_SLEEP_S = 3.0
DEFAULT_TIMEOUT_S = 20.0

# ---- 行为常量--------------------------------------------------------------
SEEN_ID_LIMIT = 2000
PAUSE_COOLDOWN = timedelta(hours=6)
TITLE_CHARS = 60
BODY_MAX = 100000
SUMMARY_MAX = 2000
FEED_ADVERT_TYPE = "feed_advert"
SUPPORTED_TARGET_TYPES = frozenset({"answer", "article", "pin"})
ANSWER_URL_TEMPLATE = "https://www.zhihu.com/question/{qid}/answer/{aid}"
ARTICLE_URL_TEMPLATE = "https://zhuanlan.zhihu.com/p/{aid}"
PIN_URL_TEMPLATE = "https://www.zhihu.com/pin/{aid}"
RISKY_MARKERS = ("40352", "403 Forbidden", "请稍后重试", "系统监测到")

#: 段落级闭合标签 → 空行
_PARAGRAPH_BREAK_RE = re.compile(
    r"(?i)<\s*/\s*(?:p|div|h[1-6]|tr|li|blockquote)\s*>"
)
#: <br> 只是换行，不是分段
_LINE_BREAK_RE = re.compile(r"(?i)<\s*br\s*/?\s*>")
_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


def html_to_text(raw_html: str) -> str:
    """知乎 `content` HTML → 纯文本, 段落之间保留换行。"""
    text = str(raw_html or "")
    text = _PARAGRAPH_BREAK_RE.sub("\n\n", text)
    text = _LINE_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    lines = [line.strip() for line in text.splitlines()]
    return _MULTI_NEWLINE_RE.sub("\n\n", "\n".join(lines)).strip()


def coerce_ts(value: object) -> datetime | None:
    """秒或毫秒都吃; 分不出来就当秒。解析不了返回 None。"""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number > 10_000_000_000:  # 毫秒
        number //= 1000
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def target_content_html(target: dict) -> str:
    """取正文 HTML。

    `content` 字段的类型随 target 变：`answer`/`article` 是 HTML 字符串，`pin` 是
    `[{"content": "<p>…</p>", "url": …}]` 这样的列表（图片九宫格也在里面）。直接把list
    `str()` 出来会把Python repr 塞进日报，所以这里分类型取。
    """
    content = target.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                block_html = block.get("content")
                if isinstance(block_html, str) and block_html.strip():
                    return block_html
            elif isinstance(block, str) and block.strip():
                return block
    for key in ("content_html", "excerpt"):
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _looks_risky(payload: object) -> str | None:
    """返回风控原因字符串, 没问题返回 None。"""
    if isinstance(payload, dict):
        code = payload.get("code") or payload.get("error_code")
        if str(code) in {"403", "40352"}:
            return f"知乎风控 {code}"
        for key in ("detail", "error", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                for marker in RISKY_MARKERS:
                    if marker in value:
                        return f"知乎风控: {value.strip()[:200]}"
    return None


def _actor_labels(entry: dict) -> list[str]:
    """actors 里常带同意料的人名; 没有就空。"""
    labels: list[str] = []
    actors = entry.get("actors")
    if isinstance(actors, list):
        for actor in actors:
            if isinstance(actor, dict):
                name = str(actor.get("name") or "").strip()
                if name and name not in labels:
                    labels.append(name)
    return labels


def _target_url(target: dict) -> str:
    target_type = str(target.get("type") or "")
    target_id = str(target.get("id") or "").strip()
    if target_type == "answer":
        question = target.get("question") if isinstance(target.get("question"), dict) else {}
        qid = str(question.get("id") or "").strip()
        if qid and target_id:
            return ANSWER_URL_TEMPLATE.format(qid=qid, aid=target_id)
    elif target_type == "article" and target_id:
        return ARTICLE_URL_TEMPLATE.format(aid=target_id)
    elif target_type == "pin" and target_id:
        return PIN_URL_TEMPLATE.format(aid=target_id)
    return ""


def _target_title(target: dict, body_text: str) -> str:
    target_type = str(target.get("type") or "")
    if target_type == "answer":
        question = target.get("question") if isinstance(target.get("question"), dict) else {}
        title = str(question.get("title") or "").strip()
        if title:
            return title[:TITLE_CHARS]
    title = str(target.get("title") or "").strip()
    if title:
        return title[:TITLE_CHARS]
    # pin 没标题（只有 excerpt_title）→ 用正文前 60 字, 跟微博一个口径
    flat = " ".join(body_text.split())
    return flat[:TITLE_CHARS]


def _author_key(target: dict) -> tuple[str, str]:
    """返回 (作者 url_token|id, 作者展示名)。"""
    author = target.get("author") if isinstance(target.get("author"), dict) else {}
    key = str(author.get("url_token") or author.get("id") or "").strip()
    label = str(author.get("name") or "").strip()
    return key, label


# ---- state ----------------------------------------------------------------


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
        logger.warning("zhihu_moments: 无法写 state %s: %s", path, exc)


def _paused_recently(state: dict, *, now: datetime, cooldown: timedelta) -> bool:
    if not state.get("paused"):
        return False
    stamp = str(state.get("paused_at") or "").strip()
    if not stamp:
        return False
    try:
        paused_at = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if paused_at.tzinfo is None:
        paused_at = paused_at.replace(tzinfo=timezone.utc)
    return (now - paused_at.astimezone(timezone.utc)) < cooldown


_CHROME_MAJOR = "130"


def _session(cookies_file: Path):
    """按 cookie 文件建一个带浏览器请求头的 requests 会话。

    支持两种存法: {"cookies": {...}} / {...} / {"cookies": "k=v; k2=v2"}。
    """
    import requests

    data = json.loads(Path(cookies_file).read_text("utf-8"))
    cookies = data.get("cookies", data) if isinstance(data, dict) else data
    if isinstance(cookies, str):
        cookies = dict(p.strip().split("=", 1) for p in cookies.split(";") if "=" in p)
    s = requests.Session()
    s.headers.update({
        "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{_CHROME_MAJOR}.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.zhihu.com/",
    })
    for k, v in (cookies or {}).items():
        s.cookies.set(k, v, domain=".zhihu.com")
    return s


def default_fetch(
    url: str | None,
    *,
    cookies_file: Path = DEFAULT_COOKIES_FILE,
    timeout: float = DEFAULT_TIMEOUT_S,
    _cache: dict = {},
) -> dict:
    """真实抓取关注动态。会话用 cookies_file 里的知乎登录态（zhihu-cli 的 cookies.json 格式）。"""
    # 缓存以 cookies_file 为 key。可变默认参数跨调用共享, 换知乎 cookie 文件
    # （失效重登）后同进程若仍用旧会话, 每轮都会走风控→暂停路径, 直到进程重启才恢复。
    key = str(cookies_file)
    entry = _cache.get(key)
    if entry is None:
        if not Path(cookies_file).exists():
            raise RuntimeError(f"知乎登录态文件不可用: {cookies_file}")
        entry = {"s": _session(Path(cookies_file))}
        _cache[key] = entry
    response = entry["s"].get(url or MOMENTS_URL, timeout=timeout)
    if response.status_code in (401, 403):
        raise RuntimeError(f"知乎 HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"知乎返回非JSON: {response.text[:200]}") from exc
    if isinstance(payload, dict) and (payload.get("error") or payload.get("code") in (401, 403)):
        raise RuntimeError(f"知乎错误: {str(payload)[:200]}")
    return payload


class ZhihuMomentsAdapter:
    name = "zhihu_moments"

    def __init__(
        self,
        *,
        fetch: Callable[[str | None], dict] | None = None,
        state_path: Path = DEFAULT_STATE_PATH,
        cookies_file: Path = DEFAULT_COOKIES_FILE,
        max_pages: int = DEFAULT_MAX_PAGES,
        sleep_s: float = DEFAULT_SLEEP_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.fetch = fetch or (
            lambda url: default_fetch(url, cookies_file=cookies_file, timeout=timeout_s)
        )
        self.state_path = state_path
        self.max_pages = max_pages
        self.sleep_s = sleep_s
        self.paused_reason: str | None = None
        #: 10-05 验收 F116/F117: 本轮的失败原因, 供 cli 读（项目里没有"失败原因"约定）
        self.last_errors: list[str] = []

    def _persist(
        self,
        *,
        seen: list[str],
        paused: str | None,
        now: datetime,
        truncated_at: str | None = None,
    ) -> None:
        state: dict = {"seen_ids": seen[-SEEN_ID_LIMIT:], "updated_at": now.isoformat()}
        if paused:
            state["paused"] = True
            state["reason"] = paused
            state["paused_at"] = now.isoformat()
        # 10-05 验收 F117: 翻满页数上限是截断, 退出原因要留在 state 里
        if truncated_at:
            state["truncated_at"] = truncated_at
        _save_state(self.state_path, state)

    def _pause(self, seen: list[str], reason: str, now: datetime) -> None:
        self.paused_reason = reason
        self.last_errors.append(reason)
        logger.warning("zhihu_moments: %s, 本轮停止", reason)
        self._persist(seen=seen, paused=reason, now=now)

    def _merge_entry(
        self,
        entry: dict,
        *,
        records: dict[str, ItemRecord],
        since_utc: datetime | None,
    ) -> bool:
        """把一条 entry 并进 records。返回是否产出了/更新了 Item。"""
        target = entry.get("target") if isinstance(entry.get("target"), dict) else None
        if target is None:
            return False
        target_type = str(target.get("type") or "")
        if target_type not in SUPPORTED_TARGET_TYPES:
            return False

        target_id = str(target.get("id") or "").strip()
        url = _target_url(target)
        if not target_id or not url:
            return False

        body = html_to_text(target_content_html(target))
        title = _target_title(target, body)
        if not title:
            return False

        author_key, author_label = _author_key(target)
        source = f"{self.name}:{author_key or target_id}"

        ts = coerce_ts(target.get("created") or target.get("created_time"))
        if ts is None:
            ts = coerce_ts(entry.get("created_time"))
        if ts is None:
            logger.info("zhihu_moments: 跳过 target %s, 时间解析失败", target_id)
            return False
        if since_utc is not None and ts < since_utc:
            return False

        actor = str(entry.get("action_text") or "").strip()
        actors = _actor_labels(entry)

        existing = records.get(url)
        if existing is not None:
            # 同一条被多个关注者赞 → 只收一次, 把「谁赞同了」并进去
            payload = json.loads(existing.source_payload_json)
            endorsed = list(payload.get("endorsed_by") or [])
            for label in ([actor] if actor else []) + actors:
                if label and label not in endorsed:
                    endorsed.append(label)
            payload["endorsed_by"] = endorsed
            payload["endorse_count"] = len(endorsed)
            existing.source_payload_json = json.dumps(payload, ensure_ascii=False)
            return False

        item = Item(
            id=compute_item_id(source, url=url),
            source=source,
            url=url,
            title=title,
            body=body[:BODY_MAX],
            author=author_label or None,
            ts=ts,
            lang="zh",
            summary=body[:SUMMARY_MAX] or None,
            tags=["zhihu", self.name, target_type],
        )
        endorsed: list[str] = []
        for label in ([actor] if actor else []) + actors:
            if label and label not in endorsed:
                endorsed.append(label)
        payload = {
            "target_type": target_type,
            "target_id": target_id,
            "author_key": author_key,
            "author_name": author_label,
            "endorsed_by": endorsed,
            "endorse_count": len(endorsed),
            "action_text": actor or None,
            "url": url,
            "voteup_count": target.get("voteup_count"),
            "comment_count": target.get("comment_count"),
        }
        records[url] = ItemRecord(
            item=item,
            adapter_name=self.name,
            source_payload_json=json.dumps(payload, ensure_ascii=False),
            media_urls=[],
        )
        return True

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable[ItemRecord]:
        now = datetime.now(timezone.utc)
        since_utc = since.astimezone(timezone.utc) if since else None
        state = _load_state(self.state_path)
        self.paused_reason = None
        self.last_errors = []

        if _paused_recently(state, now=now, cooldown=PAUSE_COOLDOWN):
            reason = str(state.get("reason") or "风控暂停中")
            logger.info("zhihu_moments: 处于暂停窗口(%s), 本轮跳过", reason)
            self.paused_reason = reason
            return []

        seen: list[str] = list(state.get("seen_ids") or [])
        seen_set = set(seen)
        records: dict[str, ItemRecord] = {}

        url: str | None = None  # 第一页
        truncated = False
        for page_idx in range(self.max_pages):
            try:
                payload = self.fetch(url)
            except Exception as exc:  # noqa: BLE001
                self._pause(seen, f"第{page_idx + 1}页取数异常: {exc}", now)
                break

            risky = _looks_risky(payload)
            if risky:
                self._pause(seen, f"第{page_idx + 1}页{risky}", now)
                break

            # 10-05 验收 F116: code 非 0 是接口报错（如 {"code":500,"message":"server error"}）,
            # data 不是 list 是改版——都不能当「本轮 0 条」，否则整条 lane 静默断流而 summary 仍写 ok。
            code = payload.get("code") if isinstance(payload, dict) else None
            if code is not None and str(code).strip() not in {"0", ""}:
                message = str(payload.get("message") or "")[:200] if isinstance(payload, dict) else ""
                self._pause(seen, f"第{page_idx + 1}页 code={code}: {message or str(payload)[:200]}", now)
                break

            entries = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(entries, list):
                self._pause(
                    seen,
                    f"第{page_idx + 1}页 data 不是 list: "
                    f"{type(entries).__name__} / {str(payload)[:200]}",
                    now,
                )
                break
            fresh_this_page = 0
            non_ad_count = 0  # 本页「非广告且有 id」条目数, 用来区分「整页全是广告」
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("type") or "") == FEED_ADVERT_TYPE:
                    continue  # 广告直接丢, 不进 seen
                entry_id = str(entry.get("id") or "").strip()
                if not entry_id:
                    continue
                non_ad_count += 1
                if entry_id in seen_set:
                    continue
                seen_set.add(entry_id)
                seen.append(entry_id)
                fresh_this_page += 1
                self._merge_entry(entry, records=records, since_utc=since_utc)

            paging = payload.get("paging") if isinstance(payload, dict) else None
            paging = paging if isinstance(paging, dict) else {}
            next_url = str(paging.get("next") or "").strip()
            is_end = bool(paging.get("is_end"))

            if fresh_this_page == 0 and non_ad_count > 0:
                logger.info("zhihu_moments: 第%d页无新动态, 停止翻页", page_idx + 1)
                break
            # fresh==0 但本页全是广告/无 id 条目时, 不能当成「无新动态」提前停,
            # 否则 is_end=false 时更老的未读动态被静默截断; 继续走下方 paging.next 翻页。
            if is_end or not next_url or next_url == url:
                logger.info("zhihu_moments: 到达末页(is_end=%s), 停止翻页", is_end)
                break
            url = next_url

            if page_idx + 1 < self.max_pages and self.sleep_s > 0:
                time.sleep(self.sleep_s)
        else:
            # 10-05 验收 F117: 循环自然跑完 = 翻满 max_pages 被截断, 此前毫无痕迹
            truncated = True

        if truncated:
            reason = f"翻满页数上限({self.max_pages}页), 本轮被截断"
            logger.warning("zhihu_moments: %s", reason)
            self.last_errors.append(reason)

        self._persist(
            seen=seen,
            paused=self.paused_reason,
            now=now,
            truncated_at=now.isoformat() if truncated else None,
        )
        out = sorted(records.values(), key=lambda record: record.item.ts, reverse=True)
        return out[:limit] if limit is not None else out