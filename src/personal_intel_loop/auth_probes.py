"""登录态探针（契约 6.1 `auth_health`）。

每个通道一个探针函数，返回 `{"ok": bool, "detail": str}`，**所有 IO 都注入**——本文件不
import requests，也不读任何 cookie 文件。谁想接真实登录态，自己写个闭包传进来：

    probe_weibo(lambda url, **kw: requests.get(url, cookies=..., **kw).json())

探针只回答一件事：「这个通道的登录态现在还能用吗」。判据都挑最便宜的那个接口，能返回
结构化数据就说明 cookie 还在；不真拉关注流（那样太贵，而且会把风控打起来）。

`CHANNELS` 是给人看的登记表：`relogin_hint` 写的是「怎么把人喊回来重新登一次」的中文
人话，不是给脚本看的命令。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: 注入函数的统一形状：`fetch(url) -> dict`（JSON 接口）或 `fetch(url) -> str`（HTML 页）
JsonFetch = Callable[..., Any]
HtmlFetch = Callable[..., Any]

DEFAULT_CAIXIN_LOGIN_MARKER = "userInfo"


def _err(exc: Exception) -> dict:
    return {"ok": False, "detail": f"请求失败: {exc}"}


# ---- 微博：/ajax/feed/allGroups 是最轻的登录态接口 --------------------------


def probe_weibo(fetch_json: JsonFetch, *, url: str = "/ajax/feed/allGroups") -> dict:
    """微博登录态：能拿到分组列表说明 cookie 有效。

    需要登录态的接口在 cookie 失效时返回 `ok: 0` +空 groups，而不是 401，所以判据是
    「ok==1 且 groups 非空」两件事都成立。
    """
    try:
        payload = fetch_json(url)
    except Exception as exc:  # noqa: BLE001 - 探针不能抛，异常也是一种「不健康」
        return _err(exc)

    if not isinstance(payload, dict):
        return {"ok": False, "detail": f"返回不是 JSON 对象: {str(payload)[:120]}"}
    ok = payload.get("ok")
    if ok != 1:
        return {"ok": False, "detail": f"ok={ok!r}（多半是登录态失效）"}

    data = payload.get("data")
    groups: list = []
    if isinstance(data, dict):
        raw_groups = data.get("groups") or data.get("list") or []
        if isinstance(raw_groups, list):
            groups = [g for g in raw_groups if isinstance(g, dict)]
    elif isinstance(payload.get("groups"), list):
        groups = [g for g in payload["groups"] if isinstance(g, dict)]

    if not groups:
        return {"ok": False, "detail": "ok=1 但分组为空，登录态可疑"}
    names = [str(g.get("name") or g.get("title") or "") for g in groups]
    return {"ok": True, "detail": f"{len(groups)} 个分组：{'、'.join(n for n in names if n) or '未命名'}"}


# ---- 知乎：/api/v4/me 是最轻的登录态接口 ---------------------------------


def probe_zhihu(fetch_json: JsonFetch, *, url: str = "/api/v4/me") -> dict:
    """知乎登录态：`/api/v4/me` 有 `id` 就是登录了。"""
    try:
        payload = fetch_json(url)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)

    if isinstance(payload, dict):
        code = payload.get("code")
        if code in (401, 403) or str(code) == "40352":
            return {"ok": False, "detail": f"HTTP/风控 {code}"}
        detail = ""
        if isinstance(payload.get("error"), dict):
            detail = str(payload["error"].get("message") or "")
        if payload.get("id"):
            name = str(payload.get("name") or payload.get("url_token") or "").strip()
            return {"ok": True, "detail": f"已登录：{name or payload.get('id')}"}
        if code is not None or detail:
            return {"ok": False, "detail": detail or f"code={code}"}

    return {"ok": False, "detail": f"返回里没有 id：{str(payload)[:120]}"}


# ---- 微信读书：书架页的 shelfIndexes 非空 ---------------------------------


def probe_weread(fetch_html: HtmlFetch, *, url: str = "https://weread.qq.com/web/shelf") -> dict:
    """微信读书登录态：书架页 `__INITIAL_STATE__.shelf.shelfIndexes` 非空。"""
    try:
        html = fetch_html(url)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)

    text = str(html or "")
    if not text.strip():
        return {"ok": False, "detail": "书架页是空的"}
    if "__INITIAL_STATE__" not in text:
        return {"ok": False, "detail": "页面里没有 __INITIAL_STATE__（多半被重定向到登录页）"}

    idx = text.find("shelfIndexes")
    if idx == -1:
        return {"ok": False, "detail": "__INITIAL_STATE__ 里没有 shelfIndexes"}
    # 不做完整 JSON 解析（前端状态里常有非标准字面量），只看shelfIndexes 后第一对方括号
    # 之间有没有内容。
    # 10-05 验收 G212：只认 shelfIndexes 后紧跟（允许空白）的非空数组为登录；
    # `shelfIndexes:null` 后面别的键再带数组也不算，遇 null / 结构异常判失效。
    head = re.match(r'shelfIndexes"?\s*:\s*(\[|null)', text[idx:])
    if not head:
        return {"ok": False, "detail": "__INITIAL_STATE__ 里没有 shelfIndexes"}
    if head.group(1) == "null":
        return {"ok": False, "detail": "shelfIndexes 为 null，登录态失效"}
    open_at = idx + head.end() - 1
    close_at = text.find("]", open_at)
    if close_at == -1:
        return {"ok": False, "detail": "shelfIndexes 结构异常"}
    if not text[open_at + 1 : close_at].strip():
        return {"ok": False, "detail": "shelfIndexes 为空，登录态失效"}
    return {"ok": True, "detail": "shelfIndexes 非空"}


# ---- 财新：页面里的登录用户标记 --------------------------------------------


def _caixin_identity_logged_in(text: str, marker: str, pos: int) -> bool:
    # 10-05 验收 G213：整页找 marker 子串太脆——登出页也可能出现 userInfo。
    # 这里检查它的值：`userInfo:null`、空对象、或对象里 uid 全为空都判未登录；
    # marker 后面没有可判读的值（如自定义纯标记 `caixinLogined`）时沿用「出现即登录」。
    rest = text[pos + len(marker):]
    head = re.match(r"\"?\s*[=:]\s*(null|undefined|\{)", rest)
    if not head:
        return True
    if head.group(1) != "{":
        return False
    brace = pos + len(marker) + head.end() - 1
    depth = 0
    close = -1
    for i in range(brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                close = i
                break
    body = text[brace + 1 : close] if close != -1 else text[brace + 1:]
    if not body.strip():
        return False
    pairs = re.findall(
        r'["\']?(?:uid|userId|user_id)["\']?\s*:\s*(?:"([^"]*)"|\'([^\']*)\'|([0-9A-Za-z_\-]+))',
        body,
    )
    if pairs:
        return any(v for trip in pairs for v in trip)
    return True


def probe_caixin(
    fetch_html: HtmlFetch,
    *,
    url: str = "https://weekly.caixin.com/",
    marker: str = DEFAULT_CAIXIN_LOGIN_MARKER,
) -> dict:
    """财新登录态：页面里出现登录用户标记（缺省 `userInfo`）且其登录身份非空才算已登录。"""
    try:
        html = fetch_html(url)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)

    text = str(html or "")
    if not text.strip():
        return {"ok": False, "detail": "页面是空的"}
    pos = text.find(marker) if marker else -1
    if pos != -1:
        if _caixin_identity_logged_in(text, marker, pos):
            return {"ok": True, "detail": f"页面里有 {marker}，登录身份非空"}
        return {"ok": False, "detail": f"页面里有 {marker}，但登录身份（uid）为空，登录态失效"}
    return {"ok": False, "detail": f"页面里没有 {marker}，登录态失效"}


# ---- 登记表（给人看的）----------------------------------------------------

CHANNELS: list[dict] = [
    {
        "key": "weibo_home",
        "label": "微博关注流",
        "relogin_hint": "微博：在『今日』里点『重新登录』，会弹出登录窗口，用手机微博扫码。扫完关掉窗口，下一轮自动恢复。",
    },
    {
        "key": "zhihu_moments",
        "label": "知乎关注动态",
        "relogin_hint": "知乎：跑一下 zhihu-cli 的登录命令，手机知乎 App 扫码确认。cookie 落在 ~/.zhihu-cli/cookies.json。",
    },
    {
        "key": "weread_mp",
        "label": "微信公众号（经微信读书）",
        "relogin_hint": "微信读书：用手机微信扫书架页的登录二维码，扫完书架要能看到书才算成功。profile 存在你的导出工具的持久化目录里。",
    },
    {
        "key": "caixin",
        "label": "财新",
        "relogin_hint": "财新：在浏览器 profile 里重新登录，cookies 落盘后下一轮自动恢复。",
    },
    {
        "key": "youtube_followed",
        "label": "YouTube 订阅",
        "relogin_hint": "YouTube：这条通道靠 RSS + 转录，不一定需要登录。若启用了登录态同步，用 Google 账号重新授权一次即可。",
    },
    {
        "key": "podcast_new",
        "label": "播客/小宇宙转录",
        "relogin_hint": "播客通道读的是本机转录文件，不联网、不要登录。出现异常先看转录目录是不是没挂载。",
    },
]


def probe_all(fetches: dict[str, Any]) -> list[dict]:
    """一次跑完所有探针。`fetches` = {"weibo": fetch_json, "weread": fetch_html, ...}。

    传进来的函数缺失 / 抛异常都算不健康，不向上抛——巡检任务不该被一个通道搞挂。
    """
    probes: list[tuple[str, str, Callable[[Any], dict]]] = [
        ("weibo_home", "weibo", probe_weibo),
        ("zhihu_moments", "zhihu", probe_zhihu),
        ("weread_mp", "weread", probe_weread),
        ("caixin", "caixin", probe_caixin),
    ]
    out: list[dict] = []
    for key, fetch_key, probe in probes:
        fetch = fetches.get(fetch_key)
        if fetch is None:
            result = {"ok": False, "detail": "没注册取数函数"}
        else:
            try:
                result = probe(fetch)
            except Exception as exc:  # noqa: BLE001
                result = _err(exc)
        entry = {"key": key, **result}
        hint = next((c["relogin_hint"] for c in CHANNELS if c["key"] == key), None)
        if hint:
            entry["relogin_hint"] = hint
        out.append(entry)
    return out