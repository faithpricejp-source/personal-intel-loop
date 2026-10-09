"""微博关注首页 adapter (`weibo_home`)。

关注流 (`/ajax/statuses/friends_timeline`) 是登录态接口，拿不到 cookie 就 403/空，所以本
adapter 只做「取数 → 映射 → 去重 → 风控暂停」这一段薄逻辑，真正的 HTTP 调用全部由构造
参数 `fetch_page` / `fetch_long_text` 注入。测试用 `fixtures/weibo_home_friendstimeline.json`
喂假函数；真实实现见 `default_fetch_page`（需要你自己的登录态，测试不跑）。

    fetch_page(max_id) -> dict                # 首次 max_id=0, 之后用返回的 max_id
    fetch_long_text(mblogid) -> str           # isLongText 为真时补全文, 失败回落 text_raw
       ↓ statuses[] → Item
    Item(source="weibo_home:<user.idstr>")

要点（都来自真实接口的坑，不是洁癖）：
- `statuses` / `max_id` / `ok` 在**顶层**，不在 `data` 下；缺 `max_id` 说明到头了。
- `text` 是 HTML（带 `<a href>` 话题/@链接），`text_raw` 是纯文本但**被截断**；长文走
  `longtext` 接口补全文，补不到就用 `text_raw`（宁可短，不要塞半截 HTML）。
- 广告两种形态：`isAd` 真，或 `mblogtype == 1`。
- 风控：一旦 `ok != 1` 或抛异常，本轮立刻停、不重试（重试只会加深风控），把
  `{"paused": true, "reason": ...}` 写进 state；6 小时内不再重试。

转发内容顺手记 `source_payload.mentioned_accounts`（被转发者），供契约 6.3 的「推荐关注」
被动发现用——用户自己点关注，系统只负责发现。
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

import os

from personal_intel_loop import APP_HOME, DATA_DIR
from personal_intel_loop.schemas import Item, compute_item_id

logger = logging.getLogger(__name__)

# ---- 缺省常量（真实路径/参数；测试一律覆盖）--------------------------------
DEFAULT_STATE_PATH = DATA_DIR / "cache" / "weibo_home_state.json"
#: Playwright storage_state 格式的微博登录态。环境变量 PIL_WEIBO_STORAGE_STATE 覆盖。
DEFAULT_STORAGE_STATE = Path(
    os.environ.get("PIL_WEIBO_STORAGE_STATE") or (APP_HOME / "auth" / "weibo_storage_state.json")
).expanduser()
# 2026-10-04 实测可用：先从 allGroups 取「最新微博」分组 gid 作 list_id，再翻 unreadfriendstimeline
GROUPS_URL = "https://weibo.com/ajax/feed/allGroups?is_new_segment=1&fetch_hot=1"
FRIENDS_TIMELINE_URL = "https://weibo.com/ajax/feed/unreadfriendstimeline"
LONGTEXT_URL_TEMPLATE = "https://weibo.com/ajax/statuses/longtext?id={mblogid}"
DEFAULT_MAX_PAGES = 5
DEFAULT_SLEEP_S = 3.0
DEFAULT_TIMEOUT_S = 20.0

# ---- 行为常量---------------------------------------------------------------
SEEN_ID_LIMIT = 2000  # state 里只记最近 2000 个 id, 防无限膨胀
PAUSE_COOLDOWN = timedelta(hours=6)
TITLE_CHARS = 60
BODY_MAX = 100000
SUMMARY_MAX = 2000
RETWEET_SUFFIX_FMT = "\n//转发自 @{name}：{text}"
AD_MBLOG_TYPES = frozenset({1})  # mblogtype==1 即广告; 0/2/4 都是正常微博形态
WECHAT_DATETIME_FMT = "%a %b %d %H:%M:%S %z %Y"  # Sun Oct 04 16:06:57 +0800 2026

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


def strip_html(text: str) -> str:
    """微博 `text` 字段去标签。`<br>` / `</p>` 视作换行，其余标签直接删。"""
    raw = str(text or "")
    raw = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", raw)
    raw = re.sub(r"(?i)<\s*/\s*(p|div)\s*>", "\n", raw)
    raw = _HTML_TAG_RE.sub("", raw)
    #微博正文里的 &amp; &lt; &gt; &nbsp; 实体
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")):
        raw = raw.replace(entity, char)
    raw = raw.replace("\u200b", "")
    lines = [line.strip() for line in raw.splitlines()]
    return _MULTI_NEWLINE_RE.sub("\n\n", "\n".join(lines)).strip()


def parse_weibo_ts(raw: object) -> datetime | None:
    """`created_at` = `Sun Oct 04 16:06:57 +0800 2026` → UTC datetime。解析不了返回 None。"""
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, WECHAT_DATETIME_FMT)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def pick_media_urls(status: dict) -> list[str]:
    """从 `pic_infos` 取图, 优先 large, 没有就 original。"""
    urls: list[str] = []
    pic_infos = status.get("pic_infos")
    if not isinstance(pic_infos, dict):
        return urls
    for _pid, info in pic_infos.items():
        if not isinstance(info, dict):
            continue
        picked = ""
        for size in ("large", "original"):
            candidate = info.get(size)
            if isinstance(candidate, dict):
                url = str(candidate.get("url") or "").strip()
                if url:
                    picked = url
                    break
        if picked and picked not in urls:
            urls.append(picked)
    return urls


def is_ad(status: dict) -> bool:
    if status.get("isAd"):
        return True
    try:
        return int(status.get("mblogtype") or 0) in AD_MBLOG_TYPES
    except (TypeError, ValueError):
        return False


def mentioned_accounts(status: dict) -> list[dict]:
    """被转发者账号, 给契约 6.3 推荐关注用。"""
    retweeted = status.get("retweeted_status")
    if not isinstance(retweeted, dict):
        return []
    user = retweeted.get("user")
    if not isinstance(user, dict):
        return []
    account_id = str(user.get("idstr") or "").strip()
    if not account_id:
        return []
    return [
        {
            "platform": "weibo",
            "account_id": account_id,
            "label": str(user.get("screen_name") or "").strip() or account_id,
        }
    ]


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
    except OSError as exc:  # pragma: no cover - 磁盘问题不该让整轮崩
        logger.warning("weibo_home: 无法写 state %s: %s", path, exc)


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


# ---- 默认 HTTP 实现（需要真实登录态；本仓库的测试不碰）--------------------


def _weibo_session(storage_state: Path):
    import requests

    try:
        state = json.loads(Path(storage_state).read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"微博登录态文件不可用: {storage_state} ({exc})") from exc
    s = requests.Session()
    for cookie in state.get("cookies") or []:
        if cookie.get("name"):
            s.cookies.set(cookie["name"], str(cookie.get("value") or ""), domain=cookie.get("domain"), path=cookie.get("path") or "/")
    xsrf = next((c.get("value") for c in state.get("cookies") or [] if c.get("name") == "XSRF-TOKEN" and "weibo.com" in str(c.get("domain"))), None)
    s.headers.update({
        "accept": "application/json, text/plain, */*",
        "referer": "https://weibo.com/",
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        ),
        "x-requested-with": "XMLHttpRequest",
    })
    if xsrf:
        s.headers["x-xsrf-token"] = xsrf
    return s


def _latest_group_id(s, timeout: float) -> str:
    groups = s.get(GROUPS_URL, timeout=timeout).json()
    for grp in groups.get("groups") or []:
        for g in grp.get("group") or []:
            if g.get("gid") and str(g.get("title")) in ("最新微博", "全部关注"):
                return str(g["gid"])
    raise RuntimeError("allGroups 里找不到「最新微博」分组（登录态失效或接口改版）")


def default_fetch_page(
    max_id: int,
    *,
    url: str = FRIENDS_TIMELINE_URL,
    storage_state: Path = DEFAULT_STORAGE_STATE,
    timeout: float = DEFAULT_TIMEOUT_S,
    _cache: dict = {},
) -> dict:
    """真实抓取关注流首页/翻页（PIL_WEIBO_STORAGE_STATE 指向的 Playwright storage_state 里的 cookie）。"""
    # 缓存以 storage_state 为 key。可变默认参数跨实例/跨调用共享, 原来换登录态
    # 文件（cookie 过期换新、测试换 state_path）也会复用旧 session/gid, 新登录态静默不生效。
    key = str(storage_state)
    entry = _cache.get(key)
    if entry is None:
        entry = {"s": _weibo_session(storage_state)}
        entry["gid"] = _latest_group_id(entry["s"], timeout)
        _cache[key] = entry
    s = entry["s"]
    params = {"list_id": entry["gid"], "refresh": 4, "since_id": 0, "count": 25}
    if max_id:
        params["max_id"] = max_id
    response = s.get(url, params=params, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"friendstimeline HTTP {response.status_code}")
    return response.json()


def default_fetch_long_text(
    mblogid: str,
    *,
    url_template: str = LONGTEXT_URL_TEMPLATE,
    storage_state: Path = DEFAULT_STORAGE_STATE,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> str:
    """长微博全文。失败一律抛异常, 由collect() 回落text_raw。"""
    import requests

    cookies: dict[str, str] = {}
    state = json.loads(Path(storage_state).read_text("utf-8"))
    for cookie in state.get("cookies") or []:
        name = str(cookie.get("name") or "")
        if name:
            cookies[name] = str(cookie.get("value") or "")
    response = requests.get(
        url_template.format(mblogid=mblogid),
        cookies=cookies,
        headers={
            "accept": "application/json, text/plain, */*",
            "referer": "https://weibo.com/",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                " (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
            ),
            "x-requested-with": "XMLHttpRequest",
        },
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(f"longtext HTTP {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("ok") != 1:
        raise RuntimeError(f"longtext ok!=1: {str(payload)[:200]}")
    return strip_html(str((payload.get('data') or {}).get('longTextContent') or ''))


# ---- adapter --------------------------------------------------------------


class WeiboHomeAdapter:
    name = "weibo_home"

    def __init__(
        self,
        *,
        fetch_page: Callable[[int], dict] | None = None,
        fetch_long_text: Callable[[str], str] | None = None,
        state_path: Path = DEFAULT_STATE_PATH,
        storage_state: Path = DEFAULT_STORAGE_STATE,
        max_pages: int = DEFAULT_MAX_PAGES,
        sleep_s: float = DEFAULT_SLEEP_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.fetch_page = fetch_page or (
            lambda max_id: default_fetch_page(
                max_id, storage_state=storage_state, timeout=timeout_s
            )
        )
        self.fetch_long_text = fetch_long_text or (
            lambda mblogid: default_fetch_long_text(
                mblogid, storage_state=storage_state, timeout=timeout_s
            )
        )
        self.state_path = state_path
        self.max_pages = max_pages
        self.sleep_s = sleep_s
        #: 本轮因风控暂停的原因; None = 正常跑完
        self.paused_reason: str | None = None
        #: 10-05 验收 F102/F103: 本轮的失败原因, 供 cli 读（项目里没有"失败原因"约定）
        self.last_errors: list[str] = []

    # -- state -----------------------------------------------------------
    def _load_seen(self, state: dict) -> list[str]:
        seen = state.get("seen_ids")
        return [str(value) for value in seen] if isinstance(seen, list) else []

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
        # 10-05 验收 F103: 翻满页数上限是截断, 退出原因要留在 state 里
        if truncated_at:
            state["truncated_at"] = truncated_at
        _save_state(self.state_path, state)

    def _pause(self, seen: list[str], reason: str, now: datetime) -> None:
        self.paused_reason = reason
        self.last_errors.append(reason)
        self._persist(seen=seen, paused=reason, now=now)

    # -- 正文 ------------------------------------------------------------
    def _resolve_body_text(self, status: dict) -> tuple[str, bool]:
        """返回 (正文纯文本, 是否走了 longtext 接口)。失败一律回落 text_raw。"""
        if status.get("isLongText"):
            mblogid = str(status.get("mblogid") or "").strip()
            if mblogid:
                try:
                    long_text = self.fetch_long_text(mblogid)
                except Exception as exc:  # noqa: BLE001 - 长文接口不稳是常态
                    logger.info("weibo_home: longtext 失败 %s: %s", mblogid, exc)
                else:
                    cleaned = strip_html(long_text or "")
                    if cleaned:
                        return cleaned, True
        return strip_html(str(status.get("text_raw") or "")), False

    def _build_record(
        self,
        status: dict,
        *,
        since_utc: datetime | None,
    ) -> ItemRecord | None:
        if not isinstance(status, dict) or is_ad(status):
            return None

        mblogid = str(status.get("mblogid") or "").strip()
        weibo_id = str(status.get("idstr") or "").strip()
        if not mblogid or not weibo_id:
            return None

        user = status.get("user") if isinstance(status.get("user"), dict) else {}
        user_id = str(user.get("idstr") or "").strip()
        if not user_id:
            return None

        ts = parse_weibo_ts(status.get("created_at"))
        if ts is None:
            logger.info("weibo_home: 跳过 %s, created_at 解析失败", mblogid)
            return None
        if since_utc is not None and ts < since_utc:
            return None

        own_text, used_long_text = self._resolve_body_text(status)

        retweeted = status.get("retweeted_status")
        retweeted_by = ""
        if isinstance(retweeted, dict):
            rt_user = retweeted.get("user") if isinstance(retweeted.get("user"), dict) else {}
            rt_name = str(rt_user.get("screen_name") or "").strip()
            rt_text = strip_html(str(retweeted.get("text_raw") or ""))
            if rt_name or rt_text:
                retweeted_by = rt_name or str(rt_user.get("idstr") or "")
                own_text = (own_text or "") + RETWEET_SUFFIX_FMT.format(
                    name=retweeted_by, text=rt_text
                )

        title = " ".join((own_text or "").split())[:TITLE_CHARS].strip()
        if not title:
            # 正文空但可能是纯图微博, 用作者名兜底, 否则 Item.title 非空校验过不去
            title = str(user.get("screen_name") or f"微博 {weibo_id}").strip()[:TITLE_CHARS]
        if not title:
            return None

        source = f"{self.name}:{user_id}"
        url = f"https://weibo.com/{user_id}/{mblogid}"
        media_urls = pick_media_urls(status)
        body = (own_text or "")[:BODY_MAX]

        item = Item(
            id=compute_item_id(source, url=url),
            source=source,
            url=url,
            title=title,
            body=body,
            author=str(user.get("screen_name") or "").strip() or None,
            ts=ts,
            lang="zh",
            summary=body[:SUMMARY_MAX] or None,
            tags=["weibo", self.name],
        )
        payload = {
            "weibo_id": weibo_id,
            "mblogid": mblogid,
            "user_id": user_id,
            "screen_name": user.get("screen_name"),
            "is_long_text": bool(status.get("isLongText")),
            "long_text_fetched": used_long_text,
            "is_retweet": isinstance(retweeted, dict),
            "retweeted_by": retweeted_by or None,
            "media_urls": media_urls,
            "mentioned_accounts": mentioned_accounts(status),
        }
        return ItemRecord(
            item=item,
            adapter_name=self.name,
            source_payload_json=json.dumps(payload, ensure_ascii=False),
            media_urls=media_urls,
        )

    # -- 主流程 ----------------------------------------------------------
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
            logger.info("weibo_home: 处于暂停窗口(%s), 本轮跳过", reason)
            self.paused_reason = reason
            return []

        seen: list[str] = self._load_seen(state)
        seen_set = set(seen)
        records: dict[str, ItemRecord] = {}

        max_id = 0  # 首次固定 0, 之后用返回的 max_id
        truncated = False
        for page_idx in range(self.max_pages):
            try:
                payload = self.fetch_page(max_id)
            except Exception as exc:  # noqa: BLE001 - 风控/网络一律立即停
                reason = f"第{page_idx + 1}页取数异常: {exc}"
                logger.warning("weibo_home: %s, 本轮停止", reason)
                self._pause(seen, reason, now)
                break

            if not isinstance(payload, dict) or payload.get("ok") != 1:
                reason = (
                    f"第{page_idx + 1}页 ok!=1: {str(payload)[:200] if payload else '空响应'}"
                )
                logger.warning("weibo_home: %s, 本轮停止", reason)
                self._pause(seen, reason, now)
                break

            # 10-05 验收 F102: ok=1 但 statuses 不是 list = 接口改版（挪到 data 下了）,
            # 不能当「这一页 0 条」——那会让整条 lane 静默断流且 summary 仍写 ok。
            statuses = payload.get("statuses")
            if not isinstance(statuses, list):
                reason = (
                    f"第{page_idx + 1}页 statuses 不是 list: "
                    f"{type(statuses).__name__} / {str(payload)[:200]}"
                )
                logger.warning("weibo_home: %s, 本轮停止", reason)
                self._pause(seen, reason, now)
                break
            fresh_this_page = 0
            for status in statuses:
                if not isinstance(status, dict):
                    continue
                weibo_id = str(status.get("idstr") or "").strip()
                if not weibo_id or weibo_id in seen_set:
                    continue  # 已见过 → 同时也是「翻到底了」的信号, 但下面还要看 max_id
                seen_set.add(weibo_id)
                seen.append(weibo_id)
                fresh_this_page += 1
                record = self._build_record(status, since_utc=since_utc)
                if record is not None:
                    records[record.item.id] = record

            next_max_id = payload.get("max_id")
            try:
                next_max_id = int(next_max_id)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                next_max_id = None

            if fresh_this_page == 0:
                # 这一页没有任何新 id → 关注流已经翻到上次停的地方, 再翻也是重复
                logger.info("weibo_home: 第%d页无新微博, 停止翻页", page_idx + 1)
                break
            if next_max_id is None or next_max_id <= 0 or next_max_id == max_id:
                logger.info("weibo_home: max_id 到底(%s), 停止翻页", next_max_id)
                break
            max_id = next_max_id

            if page_idx + 1 < self.max_pages and self.sleep_s > 0:
                time.sleep(self.sleep_s)
        else:
            # 10-05 验收 F103: 循环自然跑完 = 翻满 max_pages 被截断。关注流不按时间排序,
            # 被截掉的条目下一轮可能重新出现，但退出原因此前完全不可见。
            truncated = True

        if truncated:
            reason = f"翻满页数上限({self.max_pages}页), 本轮被截断"
            logger.warning("weibo_home: %s", reason)
            self.last_errors.append(reason)

        self._persist(
            seen=seen,
            paused=self.paused_reason,
            now=now,
            truncated_at=now.isoformat() if truncated else None,
        )
        out = sorted(records.values(), key=lambda record: record.item.ts, reverse=True)
        return out[:limit] if limit is not None else out