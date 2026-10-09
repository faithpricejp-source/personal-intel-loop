"""澎湃新闻「暖闻」栏目 adapter —— 人间温暖栏的中文具名真人故事来源。

合同依据 `docs/paper_v2_contract.md` 第 8 节: 温暖栏要**具名、可核实的真人真事**,
所以这里只收澎湃自己建的暖闻类栏目, 不碰时政/军事/财经。

## 为什么用 playwright(2026-10-05 实测, 上一轮 requests 方案已废弃)

上一轮结论「`www.thepaper.cn` 全站 403, 列表接口缺 sign 拿不到」**在浏览器下不成立**:
headless chromium 直接打开栏目页就是 200, 而且**列表是 SSR 进 HTML 的**,
不需要解 sign —— 数据随 `__NEXT_DATA__` 一起下来。

监听到的 contentapi XHR 只有栏目树/地域/关注状态/侧栏, **没有文章列表 XHR**,
所以 requests 那条路就算拿到 sign 也没有列表接口可调, 必须走浏览器。

实测关键三条:

1. **headless 直接可用**, 不需要 headed / 换 channel / 改 UA。
2. **入口 URL 按栏目类型分两种**(上一轮把 136261 当 list 页探, 方向就不对):
   - `/list_<id>`: 26911 暖闻湃 / 68750 公益湃 / 25427 澎湃人物
   - `/channel/136261`: **暖闻没有 list 页**, `/list_136261` 要么 403 要么静默回落首页。
3. **403 是 WAF 选择性/随机拦截**, 同一路径重试又能过 → 每页允许重试一次。

## 栏目页 `__NEXT_DATA__` 结构(两种外层形状都要处理, 实测都出现)

```jsonc
// A. /list_<id>
{"props":{"pageProps":{"query":{"id":"26911"},"data":{ /* payload */ }}}}
// B. /channel/136261 —— 多一层信封
{"props":{"pageProps":{"data":{"code":200,"data":{ /* 同一个 payload */ }}}}}
```

payload 里 **`list`** 就是文章卡片数组(字段名实测确认, 不是 newsList/contList)。
卡片关键字段:

```
contId:"34198044"  name:"标题"  pubTime:"10小时前"|"2026-09-25"
pubTimeLong:1791089945496         ★ epoch 毫秒(int), 精确 —— 时间主路径
trackPublishTime:1791089945496    ★ 同上
publishTime:"2026-10-04 12:59:05"  ⚠ **字符串**, 不是 epoch
trackAuthor:"澎湃新闻编辑 张新燕 视频来源 徐汇消防"  ★ 具名真人, 作者字段用这个
pic / smallPic / sharePic / nodeInfo / tagList / contType
```

## 详情页正文

`newsDetail_forward_<contId>` 的 `__NEXT_DATA__` 里
**`props.pageProps.detailData.contentDetail`** 有 `content`(HTML) / `summary` / `author` /
绝对 `pubTime` / `publishTime`。正文 XHR 不存在(同样 SSR)。
DOM 兜底选择器 class 带构建哈希会变, **不写死选择器**。

## 坑

1. **暖闻湃大量是视频条目**, `content` 是 `<video>` 标签 → HTML 转文本后正文可能只剩
   `summary`。**不是失败**: summary 本身就是完整可读的叙事摘要。
2. **时间字段名挑错就全丢**。实测: `pubTimeLong` / `trackPublishTime` 是 int epoch 毫秒,
   而 `publishTime` 是**字符串** `"2026-10-04 12:59:05"`。把 `publishTime` 当 epoch 用
   会 TypeError/None, 整条 lane 恒 0 条 —— 这才是上一轮「时间认不出来就丢」的真正原因,
   不是站点没给时间。`pubTime` 另有相对串("10小时前"), 只作最后兜底并标
   `published_approx`。认不出来就丢条目 —— 温暖栏按核实优先, 假时间比不产出更糟。
3. 每页加载都可能吃 403。取不到就跳过该栏目, 不抛异常让整轮 ingest 挂掉。

## 注入接口

浏览器部分包成 `fetch_list(url)` / `fetch_body(url)`, 测试注入假函数不起浏览器。
`browsers_path` 对应 `PLAYWRIGHT_BROWSERS_PATH`(缺省 None = 用 playwright 默认)。

Source key: `thepaper_warm`(单一来源, 不按栏目分 source —— 暖闻类栏目同属一条 lane)。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from personal_intel_loop import DATA_DIR
from personal_intel_loop.schemas import Item, compute_item_id

logger = logging.getLogger(__name__)

# 要收的暖闻类栏目。**入口 URL 按栏目类型分两种**(实测, 见 docstring):
# 136261 暖闻是 nodeType 28 栏目型, 没有 /list_ 页, 必须走 /channel/;
# 26911/68750/25427 是专题型, 走 /list_。
WARM_NODES: tuple[dict[str, Any], ...] = (
    {"node_id": 136261, "name": "暖闻", "url": "https://www.thepaper.cn/channel/136261"},
    {"node_id": 26911, "name": "暖闻湃", "url": "https://www.thepaper.cn/list_26911"},
    {"node_id": 68750, "name": "公益湃", "url": "https://www.thepaper.cn/list_68750"},
    {"node_id": 25427, "name": "澎湃人物", "url": "https://www.thepaper.cn/list_25427"},
)

DEFAULT_STATE_PATH = DATA_DIR / "cache" / "thepaper_warm_state.json"
BODY_LIMIT = 20_000
CST = timezone(timedelta(hours=8))
WARMTH_MAX_AGE_DAYS = 7
SEEN_ID_LIMIT = 2_000

# 浏览器抓取参数
NAV_TIMEOUT_MS = 45_000
SETTLE_MS = 4_500          # 等 __NEXT_DATA__ 里的 SSR 数据就位
MAX_ATTEMPTS = 2           # 403 是选择性的, 重试一次
#: 正文抓取预算 —— 一个条目一个详情页, 所以这个数就是一轮最多开多少个详情页。
#: 10-05 复审 R17: 缺省曾是 8, 而四个栏目一页就有数十条卡片, 预算花完后剩下的条目
#: `item.body` 保持 "" 却照样入库。实测真实站点一轮: 9 条记录里 6 条 body 长度 0–112,
#: 全部低于下游 `paper.py` 的两个下限(`BLIND_MIN_BODY_CHARS=300` /
#: `SOCIAL_SINGLETON_MIN_CHARS=200`), 即约 2/3 的产出进了库却在报纸阶段被整体丢弃 ——
#: 付出了 4 个栏目页 + 8 个详情页的浏览器代价却换不到版面。提到 20 是一轮能覆盖到的
#: 栏目页量级; 真正的修复是「没取到正文就不入库」(见 `_fill_bodies`)。
DEFAULT_BODY_LIMIT = 20

# "3小时前" / "45分钟前" —— epoch 缺失时的兜底, 不是主路径。
_REL_RE = re.compile(r"(\d+)\s*(分钟|小时|天)前")
# "2026-10-04 12:59:05" / "2026-09-30 19:41", 时间部分可省(实测 pubTime 会给 "2026-09-30")。
_ABS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?")


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


# ---------------------------------------------------------------- 浏览器部分


class BrowserUnavailable(RuntimeError):
    """10-05 审计 E17: playwright 没装/浏览器没下载 —— 与 html_columns 同名同语义,
    调用方记 warning 并本轮跳过, 不让 ImportError 裸抛挂掉整轮 ingest。"""


class _Browser:
    """playwright 封装。用法是 `with _Browser(...) as b:`, 退出时必定关干净。

    `browsers_path` 对应环境变量 `PLAYWRIGHT_BROWSERS_PATH`(缺省 None = playwright 默认)。
    这里在 `launch()` 前显式写 `os.environ`: playwright 的 python 绑定在 import 时就读过一次
    路径, 事后设环境变量对它无效, 所以必须在 launch 之前设。
    """

    def __init__(self, browsers_path: str | None = None, *, headless: bool = True) -> None:
        self.browsers_path = browsers_path
        self.headless = headless
        self._pw: Any = None
        self._browser: Any = None
        self._ctx: Any = None
        self._prev_browser_path: str | None = None

    def __enter__(self) -> "_Browser":
        # 10-05 审计 E17: playwright 缺失(部署常态)不再裸抛 ImportError
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable(f"playwright 未安装: {exc}") from exc

        # 10-05 审计 E16: 写全局环境变量前记下旧值, 退出时恢复(与 html_columns 同款) ——
        # 否则整个进程此后的 playwright 都被指到这个路径(跨轮、跨模块泄漏)。
        self._prev_browser_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        if self.browsers_path:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = self.browsers_path
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=self.headless,
                # 去掉 automation 标记, 降低被 WAF 认出来的概率。
                args=["--disable-blink-features=AutomationControlled"],
            )
            self._ctx = self._browser.new_context(
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
        except Exception as exc:
            # 10-05 审计 E18: start() 成功后 launch()/new_context() 失败时, with 语句
            # 语义下不会调 __exit__ —— 必须显式清理, 否则 playwright driver 子进程
            # 在长驻 ingest 进程里每重试一轮泄漏一个(html_columns 同位置同款处理)。
            self.__exit__()
            raise BrowserUnavailable(
                f"chromium 起不来: {type(exc).__name__}: {exc}") from exc
        return self

    def __exit__(self, *exc: Any) -> None:
        for obj, meth in ((self._ctx, "close"), (self._browser, "close"), (self._pw, "stop")):
            if obj is None:
                continue
            try:
                getattr(obj, meth)()
            except Exception as exc:  # 关浏览器失败不该盖掉真正的异常
                logger.info("thepaper_warm: 关浏览器失败(%s)", exc)
        self._restore_browsers_path()

    def _restore_browsers_path(self) -> None:
        """10-05 审计 E16: 恢复进入前旧值; 本对象没写过环境变量时不动全局。"""
        if not self.browsers_path:
            return
        if self._prev_browser_path is None:
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        else:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = self._prev_browser_path

    def next_data(self, url: str) -> dict | None:
        """打开 url 返回 `__NEXT_DATA__` 解析后的对象。拿不到返回 None(不抛)。"""
        if self._ctx is None:
            raise RuntimeError("_Browser 未进入 with 上下文")
        page = self._ctx.new_page()
        try:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                    status = resp.status if resp else None
                    page.wait_for_timeout(SETTLE_MS)
                    raw = page.evaluate(
                        "() => { const e = document.getElementById('__NEXT_DATA__');"
                        " return e ? e.textContent : null; }")
                    if raw:
                        return json.loads(raw)
                    # 200 但没有 __NEXT_DATA__ = 落到了别的页(如 /list_136261 回落首页)
                    logger.info("thepaper_warm: %s 无 __NEXT_DATA__(status=%s, 第 %s 次)",
                                url, status, attempt)
                except Exception as exc:
                    logger.info("thepaper_warm: %s 第 %s 次失败(%s)", url, attempt,
                                f"{type(exc).__name__}: {exc}"[:160])
                if attempt < MAX_ATTEMPTS:
                    page.wait_for_timeout(2_000)
            return None
        finally:
            try:
                page.close()
            except Exception:
                pass


def _payload_of(next_data: dict) -> dict:
    """从 `__NEXT_DATA__` 里挖出栏目 payload —— 两种外层形状都试(见 docstring)。"""
    page_props = (next_data.get("props") or {}).get("pageProps") or {}
    data = page_props.get("data")
    # 形状 B: {code, desc, data:{...}} 信封
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data if isinstance(data, dict) else {}


def fetch_list(url: str, *, browser: _Browser) -> list[dict[str, Any]]:
    """默认列表抓取: 浏览器打开栏目页, 从 `__NEXT_DATA__` 的 `list` 取卡片。"""
    next_data = browser.next_data(url)
    if not next_data:
        return []
    payload = _payload_of(next_data)
    cards = payload.get("list")
    if not isinstance(cards, list):
        logger.info("thepaper_warm: %s 的 payload 里没有 list 字段(键: %s)", url,
                    sorted(payload)[:12])
        return []
    return [c for c in cards if isinstance(c, dict)]


def fetch_body(url: str, *, browser: _Browser) -> str | None:
    """默认正文抓取: 浏览器打开详情页, 从 `detailData.contentDetail` 取正文纯文本。

    取不到返回 None(调用方接受空 body)。视频条目 `content` 是 `<video>` 标签,
    转文本可能为空 —— 这时回落到 `summary`, 它本身就是完整叙事摘要。
    """
    next_data = browser.next_data(url)
    if not next_data:
        return None
    detail = (((next_data.get("props") or {}).get("pageProps") or {})
              .get("detailData") or {})
    content_detail = detail.get("contentDetail") or {}
    html = str(content_detail.get("content") or "")
    text = _html_to_text(html)
    if not text:
        text = _html_to_text(str(content_detail.get("summary") or ""))
    return text or None


# ---------------------------------------------------------------- 解析工具


def _parse_pub_time(card: dict[str, Any], *, now: datetime) -> tuple[datetime | None, bool]:
    """返回 `(时间, 是否近似)`。

    **主路径是 epoch 毫秒**, 但字段名要挑对 —— 实测三栏目卡片上:
      `pubTimeLong` / `trackPublishTime` 是 int epoch 毫秒(精确),
      `publishTime` 是**字符串** "2026-10-04 12:59:05"(不是 epoch, 上一轮按 epoch
      试过, 全返回 None), `pubTime` 是给人看的相对串("10小时前")。

    所以顺序是: epoch 毫秒 → 绝对时间串 → 相对串(标 approx)。
    后两级认不出来返回 `(None, False)` —— 调用方丢掉这条。
    """
    for key in ("pubTimeLong", "trackPublishTime", "publishTime"):
        raw = card.get(key)
        # 字段形态不受控(站点异常数据/17 位以上数字串), 越界 epoch 会让
        # fromtimestamp 抛 OSError/OverflowError/ValueError 炸掉整轮 —— 与本函数
        # 「认不出来返回 (None, False)」的承诺和 _parse_absolute 的 try 包法不一致。
        # 坏值落到下一字段/兜底档, 时间认不出来就丢条目, 不崩轮。
        try:
            if isinstance(raw, (int, float)) and raw > 0:
                return datetime.fromtimestamp(float(raw) / 1000.0, tz=timezone.utc), False
            # 有些卡片把 epoch 写成数字字符串
            if isinstance(raw, str) and raw.isdigit() and int(raw) > 0:
                return datetime.fromtimestamp(int(raw) / 1000.0, tz=timezone.utc), False
        except (OverflowError, OSError, ValueError):
            continue
    # 绝对时间串("2026-10-04 12:59:05" / "2026-09-30 19:41" / "2026-09-30") —— 精确, 不算近似。
    # 这里**只认绝对串**: 相对串("10小时前")精度只到小时/天, 归到下面的兜底档。
    for key in ("publishTime", "pubTime", "pubTimeNew"):
        ts = _parse_absolute(str(card.get(key) or ""))
        if ts is not None:
            return ts, False
    # 相对串兜底 —— 精度只到"小时/天", 必须标 approx
    ts = _parse_pub_time_str(card.get("pubTime") or card.get("pubTimeNew"), now=now)
    return ts, ts is not None



def _parse_absolute(s: str) -> datetime | None:
    """只认绝对时间串("2026-10-04 12:59:05" / "2026-09-30 19:41" / "2026-09-30"),
    按东八区解释成 UTC。相对串("3小时前")**故意不认** —— 留给带 now 的兜底档标 approx。"""
    m = _ABS_RE.search(s)
    if not m:
        return None
    y, mo, d = (int(m.group(i)) for i in range(1, 4))
    hh = int(m.group(4) or 0)
    mm = int(m.group(5) or 0)
    ss = int(m.group(6) or 0)
    try:
        return datetime(y, mo, d, hh, mm, ss, tzinfo=CST).astimezone(timezone.utc)
    except ValueError:
        return None


def _parse_pub_time_str(raw: Any, *, now: datetime) -> datetime | None:
    """绝对时间串或相对串 → datetime。认不出来返回 None。

    绝对串按东八区解释(实测站点给的就是北京时间), 相对串按 `now` 减。
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = _REL_RE.search(s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = {"分钟": timedelta(minutes=n), "小时": timedelta(hours=n),
                 "天": timedelta(days=n)}[unit]
        return now - delta
    return _parse_absolute(s)


def _html_to_text(html: str) -> str:
    """正文 HTML → 纯文本。温暖栏的 body 要的是能读的正文, 不是标签。"""
    if not html:
        return ""
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    # <video>/<img> 这类嵌媒体没有文本, 直接删掉免得留下 src 噪声
    s = re.sub(r"(?is)<(video|audio|iframe)\b.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    s = re.sub(r"[ \t　]+", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _card_url(cont_id: str) -> str:
    return f"https://www.thepaper.cn/newsDetail_forward_{cont_id}"


def _load_state(path: Path) -> dict:
    """读状态文件。任何不是 dict 的内容(JSON 数组/字符串/数字)一律当空状态 ——
    `json.loads` 对合法 JSON 不抛异常, 只有非 dict 时下面 `.get` 会炸。"""
    try:
        data = json.loads(path.read_text("utf-8"))
    except Exception as exc:  # 文件不存在/JSON 坏了 → 当空状态, 只记一行
        logger.info("thepaper_warm: 状态文件不可用(%s), 当空状态", exc)
        return {}
    if not isinstance(data, dict):
        logger.info("thepaper_warm: 状态文件不是 JSON 对象(%s), 当空状态", type(data).__name__)
        return {}
    return data


def _save_state(path: Path, state: dict) -> None:
    # 10-05 审计 E19: 先写临时文件再 os.replace 原子替换(与 html_columns 的 D13 修复同款)
    # —— 直接 write_text("w") 先截断后写, 写到一半进程被杀会留半截 JSON, 下轮
    # _load_state 当空状态, 7 天窗内条目全部重采。
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("thepaper_warm: 状态文件写不进去(%s), 本轮去重只在本进程内生效", exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


class ThepaperWarmAdapter:
    name = "thepaper_warm"

    def __init__(
        self,
        *,
        fetch_list: Callable[[str], list[dict[str, Any]]] | None = None,
        fetch_body: Callable[[str], str | None] | None = None,
        state_path: Path = DEFAULT_STATE_PATH,
        browsers_path: str | None = None,
        body_limit: int = 20,
        headless: bool = True,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        # 测试钉住 now（fixture 是固定日期抓的，用真实 now 会随日子滑出 7 天窗）
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        # 注: 测试注入 fetch_list/fetch_body 时**不会**起浏览器(构造函数不做任何 IO)。
        self._fetch_list = fetch_list
        self._fetch_body = fetch_body
        self.state_path = state_path
        self.browsers_path = browsers_path
        self.body_limit = body_limit
        self.headless = headless

    # -- 浏览器生命周期: 自己开一次, 列表与正文共用 ----------------------

    def _default_fetch_list(self, url: str, browser: _Browser) -> list[dict[str, Any]]:
        return fetch_list(url, browser=browser)

    def _default_fetch_body(self, url: str, browser: _Browser) -> str | None:
        return fetch_body(url, browser=browser)

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable[ItemRecord]:
        now = self._now_fn()
        # 契约第 8 节: 只要最近 7 天。since 给了就取更严的那个。
        # naive `since`(cli 的 --since 就是无时区)按站点所在地时区(东八区,
        # 与下面 _parse_absolute 的口径一致)解释, 不依赖宿主 TZ —— 同 mofa_anzen(JST)/
        # enso_status(UTC)的先例; `.astimezone(utc)` 直接吃 naive 会按宿主时区算,
        # 部署机器一换 cutoff 漂移 8 小时。
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=CST)
        window = now - timedelta(days=WARMTH_MAX_AGE_DAYS)
        cutoff = max(window, since.astimezone(timezone.utc)) if since else window

        state = _load_state(self.state_path)
        seen: list[str] = [str(x) for x in (state.get("seen_ids") or [])]
        seen_set = set(seen)
        # 10-05 复审 R17/R18: `seen` 只记**实际返回**的条目, 所以这轮先不把任何
        # cont_id 写进去 —— `_to_record` 只往 `batch_seen`(本轮去重) 里加, 等
        # 「拿到正文 + 截断」都定下来之后再提交。
        batch_seen = set(seen_set)

        # 两条注入路径: 都注入了就用注入的(不起浏览器); 否则开一次浏览器全轮共用。
        need_browser = self._fetch_list is None or self._fetch_body is None
        records: list[ItemRecord] = []
        body_budget = self.body_limit

        def run(url: str, browser: _Browser | None) -> list[dict[str, Any]]:
            if self._fetch_list is not None:
                return self._fetch_list(url)
            assert browser is not None
            return self._default_fetch_list(url, browser)

        def run_body(url: str, browser: _Browser | None) -> str:
            if self._fetch_body is not None:
                return self._fetch_body(url) or ""
            assert browser is not None
            return self._default_fetch_body(url, browser) or ""

        if need_browser:
            try:
                with _Browser(self.browsers_path, headless=self.headless) as browser:
                    records = self._collect_all(browser, cutoff, batch_seen, run, now)
                    records = self._fill_bodies(records, body_budget, run_body, browser)
            except BrowserUnavailable as exc:
                # 10-05 审计 E17: 浏览器不可用只跳过本轮, 不抛异常让整轮 ingest 挂掉
                # (docstring 坑3 的自承诺; seen 不动, 下轮照常重试)
                logger.warning("thepaper_warm: 浏览器不可用, 本轮跳过(%s)", exc)
                records = []
        else:
            records = self._collect_all(None, cutoff, batch_seen, run, now)
            records = self._fill_bodies(records, body_budget, run_body, None)

        # 10-05 复审 R18: 先截断, 再只把**实际返回**的 cont_id 记进 seen。
        # 原来 `seen_set.add(cont_id)` / `seen.append(cont_id)` 在 `_to_record` 里(截断之前),
        # seen 又在 `return records[:limit]` 之前落盘 -> 落选的 cont_id 已进 `seen_ids`,
        # 下一轮 `if cont_id in seen_set: return None` 直接跳过 → 永久丢失, 无任何日志。
        # 与 R17 叠加时更糟: 既落在 limit 外又没拿到正文的条目就彻底消失。
        selected = records[:limit] if limit is not None else records
        if limit is not None and len(records) > len(selected):
            logger.info("thepaper_warm: 本轮产出 %d 条, limit=%d 只返回 %d 条; "
                        "落选的 %d 条不进 seen, 下一轮继续抓",
                        len(records), limit, len(selected), len(records) - len(selected))
        for rec in selected:
            cont_id = str((rec.source_payload_json and json.loads(rec.source_payload_json)
                           .get("cont_id")) or "")
            if cont_id:
                seen.append(cont_id)
                seen_set.add(cont_id)

        merged = list(dict.fromkeys(seen))
        _save_state(self.state_path, {"seen_ids": merged[-SEEN_ID_LIMIT:],
                                      "updated_at": now.isoformat()})
        return selected

    def _fill_bodies(
        self,
        records: list[ItemRecord],
        body_budget: int,
        run_body: Callable[[str, "_Browser | None"], str],
        browser: "_Browser | None",
    ) -> list[ItemRecord]:
        """给前 `body_budget` 条(调用前已按时间倒序)抓正文, **返回拿到了正文的那些条目**。

        按**新旧**而不是栏目遍历顺序发预算 —— 否则最新那几条(最该有正文)会被前面
        栏目的旧条目把预算花光, 真跑时 newest item body 为空就是这么来的。

        10-05 复审 R17: 返回值从「None(原地改 body)」改成「有正文的条目列表」——
        **没取到正文的条目不入库**。原来预算花完后直接 `break`, 剩下的条目 `item.body`
        保持 `""` 却照样被返回、照样 `upsert_item` 入库(与 mofa/cn_consular 的
        `if not body: continue` 相反), 下游 `paper.py` 再用正文长度下限
        (`BLIND_MIN_BODY_CHARS=300` / `SOCIAL_SINGLETON_MIN_CHARS=200`)整体丢弃它们。
        既然下游会丢, 就不该写进库占位, 也不该占着 seen 让它永远补不上正文。
        """
        records.sort(key=lambda r: r.item.ts, reverse=True)
        kept: list[ItemRecord] = []
        for rec in records:
            if body_budget <= 0:
                # 预算耗尽: 这些条目这轮没有正文。10-05 复审 R17: 不入库、也不进 seen,
                # 下一轮还能重新抓正文(原来它们已进 seen, 永远补不上)。
                logger.info("thepaper_warm: 正文预算(%d)用尽, %d 条留待下一轮",
                            body_budget, len(records) - len(kept))
                break
            try:
                body = run_body(rec.item.url, browser)
            except Exception as exc:
                logger.info("thepaper_warm: %s 正文抓取失败(%s), 本轮留待下一轮", rec.item.url,
                            f"{type(exc).__name__}: {exc}"[:120])
                continue
            if body:
                rec.item.body = body[:BODY_LIMIT]
                body_budget -= 1
                kept.append(rec)
        if body_budget > 0 and len(kept) < len(records):
            # 预算没耗尽但仍有条目没正文(抓取失败, 或源本身就是视频条目只有几十字summary)
            logger.info("thepaper_warm: %d 条没取到正文(抓取失败或源为视频条目), "
                        "本轮不入库, 下一轮再试", len(records) - len(kept))
        return kept

    def _collect_all(
        self,
        browser: "_Browser | None",
        cutoff: datetime,
        batch_seen: set[str],
        run: Callable[[str, "_Browser | None"], list[dict[str, Any]]],
        now: datetime,
    ) -> list[ItemRecord]:
        records: list[ItemRecord] = []
        for node in WARM_NODES:
            node_id = int(node["node_id"])
            url = str(node["url"])
            try:
                cards = run(url, browser)
            except Exception as exc:
                # 一个栏目取数异常不该让整轮 ingest 挂掉
                logger.warning("thepaper_warm: 栏目 %s 取数异常(%s), 跳过", node_id,
                               f"{type(exc).__name__}: {exc}"[:160])
                continue
            if not cards:
                logger.info("thepaper_warm: 栏目 %s 没拿到卡片(403 或结构变化)", node_id)
                continue
            for card in cards:
                rec = self._to_record(card, node_id=node_id,
                                      column_fallback=str(node["name"]),
                                      cutoff=cutoff, batch_seen=batch_seen, now=now)
                if rec is not None:
                    records.append(rec)
        return records

    def _to_record(
        self,
        card: dict[str, Any],
        *,
        node_id: int,
        column_fallback: str,
        cutoff: datetime,
        batch_seen: set[str],
        now: datetime,
    ) -> ItemRecord | None:
        cont_id = str(card.get("contId") or "").strip()
        title = str(card.get("name") or "").strip()
        if not cont_id or not title:
            return None
        if cont_id in batch_seen:
            return None
        # 10-05 复审 R19: `now` 由 `collect` 取一次传下来, 不在这里重新取。
        # 原来每张卡片各取一次 `datetime.now(utc)`, 一批卡片逐个处理跨过若干秒时, 相对时间
        # ("5天前") 算出的 `ts` 彼此相差处理耗时, 之后无法复现同一批 `ts`。
        ts, approx = _parse_pub_time(card, now=now)
        if ts is None or ts < cutoff:
            # 时间认不出来 → 丢。时间窗外的 → 丢。
            return None

        node_info = card.get("nodeInfo") or {}
        column = str(node_info.get("name") or "").strip() or column_fallback
        url = _card_url(cont_id)

        # trackAuthor 才是具名真人("澎湃新闻编辑 张新燕 视频来源 徐汇消防"),
        # nodeInfo.nickName 实测恒为空字符串。
        author = str(card.get("trackAuthor") or "").strip() or str(
            node_info.get("nickName") or "").strip() or "澎湃新闻"

        tags = ["人间温暖", column]
        tag_list = card.get("tagList")
        if isinstance(tag_list, list):
            for t in tag_list:
                if isinstance(t, dict):
                    name = str(t.get("tag") or "").strip()
                    if name and name not in tags:
                        tags.append(name)

        # 只进**本轮**去重集合; 持久化的 seen 由 `collect` 在「拿到正文 + 截断」之后统一提交
        # (10-05 复审 R17/R18)
        batch_seen.add(cont_id)

        payload: dict[str, Any] = {
            "kind": "warmth",
            "published": ts.isoformat(),
            "node_id": node_id,
            "column": column,
            "cont_id": cont_id,
        }
        if approx:
            # 只在时间是从相对串换算来的情况下标 approx(见 _parse_pub_time)
            payload["published_approx"] = True

        return ItemRecord(
            item=Item(
                id=compute_item_id(self.name, url=url, guid=cont_id),
                source=self.name,
                url=url,
                title=title[:512],
                body="",
                author=author[:256],
                ts=ts,
                lang="zh",
                tags=tags,
            ),
            adapter_name=self.name,
            source_payload_json=json.dumps(payload, ensure_ascii=False),
            media_urls=[str(card.get("pic"))] if card.get("pic") else [],
        )
