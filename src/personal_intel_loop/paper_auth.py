"""登录态健康(契约第 6.1 节)。

每个需要登录的通道登记一个探针 {"key","label","relogin_hint","probe","relogin"}。
真实探针由各平台的接入方注入(run_probes 的 channels 参数 / register_channels),
本模块只负责: 探针结果的状态迁移(since_failing 由真转假记时、恢复清空)、读接口、
以及 relogin——**只从注册表里取命令**, 前端传入的任何命令字符串都不执行。
单个探针抛异常算 ok=false, detail 记异常类名。
"""
from __future__ import annotations

import sqlite3
import subprocess
from typing import Callable

from personal_intel_loop.schemas import normalize_dt_to_utc_z

# 首批通道(契约 6.1)。probe=None = 尚未接入真实探针, run_probes 会跳过;
# 接入方调 register_channels 把带 probe 的定义补进来(或直接把 channels 传给 run_probes)。
# relogin.value 只是登记字符串: command 由 _default_start 按白名单执行, url 交回前端/App 打开。
DEFAULT_CHANNELS: list[dict] = [
    {
        "key": "weibo",
        "label": "微博",
        "relogin_hint": "微博登录已失效: 用你的登录脚本刷新 storage_state(PIL_WEIBO_STORAGE_STATE)",
        "probe": None,
        "relogin": {"kind": "url", "value": "https://weibo.com/login.php"},
    },
    {
        "key": "zhihu",
        "label": "知乎",
        "relogin_hint": "知乎登录已失效: 重新跑 zhihu-cli login 更新 cookies",
        "probe": None,
        "relogin": {"kind": "command", "value": "zhihu-cli login"},
    },
    {
        "key": "weread",
        "label": "微信读书",
        "relogin_hint": "微信读书登录已失效: 在你的导出工具里重新扫码登录",
        "probe": None,
        "relogin": {"kind": "url", "value": "https://weread.qq.com/#login"},
    },
    {
        "key": "caixin",
        "label": "财新",
        "relogin_hint": "财新登录已失效: 在浏览器 profile 里重新登录",
        "probe": None,
        "relogin": {"kind": "url", "value": "https://my.caixin.com/"},
    },
    {
        "key": "youtube",
        "label": "YouTube",
        "relogin_hint": "YouTube 登录已失效: 重新登录 Google 账号后同步订阅",
        "probe": None,
        "relogin": {"kind": "url", "value": "https://accounts.google.com"},
    },
]

_REGISTERED: list[dict] = [dict(channel) for channel in DEFAULT_CHANNELS]


def register_channels(channels) -> None:
    """接入方注册/更新真实探针: 同 key 覆盖。"""
    for channel in channels:
        key = channel.get("key")
        if not key:
            continue
        for index, existing in enumerate(_REGISTERED):
            if existing.get("key") == key:
                merged = dict(existing)
                merged.update(channel)
                _REGISTERED[index] = merged
                break
        else:
            _REGISTERED.append(dict(channel))


def default_registry() -> list[dict]:
    """当前注册表快照(每项一份浅拷贝, 防调用方误改)。"""
    return [dict(channel) for channel in _REGISTERED]


def _probe_result(channels_item: dict) -> tuple[bool, str | None]:
    """跑一个探针: 返回 (ok, detail)。探针抛异常 = 失败, detail 记异常类名。"""
    probe = channels_item.get("probe")
    if not callable(probe):
        return False, "probe 未配置"
    try:
        out = probe()
    except Exception as exc:  # noqa: BLE001 — 探针异常按契约记为失效
        return False, type(exc).__name__
    if not isinstance(out, dict):
        return False, "bad probe result"
    ok = bool(out.get("ok"))
    detail = out.get("detail")
    return ok, (str(detail) if detail is not None else None)


def run_probes(conn: sqlite3.Connection, channels, *, now_utc) -> list[dict]:
    """跑一轮探针并写 auth_status。channels 是注入的通道列表(形状见模块 docstring)。

    状态迁移: 由真转假(或首次失效)记 since_failing=now; 持续失效保留原值; 恢复清 NULL。
    没配 probe 的通道跳过(不写行)。返回本轮探测结果(label/hint 取自传入定义)。
    """
    now_z = normalize_dt_to_utc_z(now_utc)
    results: list[dict] = []
    for channel in channels:
        key = str(channel.get("key") or "").strip()
        if not key or not callable(channel.get("probe")):
            continue
        ok, detail = _probe_result(channel)
        row = conn.execute("SELECT ok, since_failing FROM auth_status WHERE key=?", (key,)).fetchone()
        if ok:
            since_failing = None
        else:
            prev_ok = bool(row["ok"]) if row is not None else False
            prev_since = row["since_failing"] if row is not None else None
            since_failing = prev_since if (row is not None and not prev_ok and prev_since) else now_z
        conn.execute(
            "INSERT OR REPLACE INTO auth_status (key, ok, checked_at, detail, since_failing) VALUES (?, ?, ?, ?, ?)",
            (key, 1 if ok else 0, now_z, detail, since_failing),
        )
        relogin_def = channel.get("relogin") if isinstance(channel.get("relogin"), dict) else {}
        results.append(
            {
                "key": key,
                "label": str(channel.get("label") or key),
                "ok": ok,
                "checked_at": now_z,
                "detail": detail,
                "since_failing": since_failing,
                "relogin_hint": str(channel.get("relogin_hint") or ""),
                "relogin": dict(relogin_def),
            }
        )
    conn.commit()
    return results


def auth_overview(conn: sqlite3.Connection, *, registry=None) -> list[dict]:
    """全部通道的当前状态(契约 GET /api/paper/auth 的 channels 元素)。"""
    registry = default_registry() if registry is None else registry
    by_key = {str(channel.get("key")): channel for channel in registry if channel.get("key")}
    keys = list(by_key)
    # 注册表之外的存量状态行也带出来(探针被注销后历史不丢)
    for row in conn.execute("SELECT key FROM auth_status"):
        if row["key"] not in by_key:
            keys.append(row["key"])
    out = []
    for key in keys:
        channel = by_key.get(key, {})
        row = conn.execute("SELECT * FROM auth_status WHERE key=?", (key,)).fetchone()
        relogin_def = channel.get("relogin") if isinstance(channel.get("relogin"), dict) else {}
        out.append(
            {
                "key": key,
                "label": str(channel.get("label") or key),
                "ok": (bool(row["ok"]) if row is not None else None),
                "checked_at": row["checked_at"] if row is not None else None,
                "detail": row["detail"] if row is not None else None,
                "since_failing": row["since_failing"] if row is not None else None,
                "relogin_hint": str(channel.get("relogin_hint") or ""),
                "relogin_kind": relogin_def.get("kind"),
            }
        )
    return out


def auth_alerts(conn: sqlite3.Connection, *, registry=None) -> list[dict]:
    """任一 ok=false 的通道(Edition 顶层 auth_alerts)。"""
    return [channel for channel in auth_overview(conn, registry=registry) if channel["ok"] is False]


def _default_start(value: str) -> None:
    """登记命令的后台启动(契约: subprocess.Popen(..., start_new_session=True))。

    安全约束(命令只许来自本模块注册表, argv 逐条字面量): 按登记字符串白名单分派;
    注册表有而这里没有分支的命令拒绝启动(接入方注入自己的 starter 即可)。
    """
    command = str(value or "").strip()
    if command == "zhihu-cli login":
        subprocess.Popen(["zhihu-cli", "login"], shell=False, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        return
    raise ValueError(f"relogin command not in executable allowlist: {command!r}")


def relogin(payload: dict, *, registry=None, starter: Callable[[str], None] | None = None) -> dict:
    """POST /api/paper/auth/relogin {"key"}: 只认注册表里的动作。

    kind=command → 后台启动登记好的命令(返回 {"ok","started"}); kind=url → 返回
    {"ok","open_url"} 由前端/App 打开。payload 里的其他任何字段(包括命令字符串)一律忽略。
    未登记的 key → KeyError(web 层 404)。
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    key = str(payload.get("key") or "").strip()
    if not key:
        raise ValueError("key is required")
    registry = default_registry() if registry is None else registry
    channel = next((c for c in registry if str(c.get("key")) == key), None)
    if channel is None:
        raise KeyError(f"unknown auth channel: {key}")
    relogin_def = channel.get("relogin") if isinstance(channel.get("relogin"), dict) else {}
    kind = relogin_def.get("kind")
    value = relogin_def.get("value")
    if kind == "command":
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"channel {key} has no relogin command registered")
        run = starter if starter is not None else _default_start
        try:
            run(value)
        except Exception as exc:  # noqa: BLE001 — 启动失败如实返回, 不抛 500
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "started": True}
    if kind == "url":
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"channel {key} has no relogin url registered")
        return {"ok": True, "open_url": value}
    raise ValueError(f"channel {key} has unsupported relogin kind: {kind!r}")
