"""新信源提案：放进画像「待接受的修订」，在报纸的编辑部来信里逐条接受/拒绝（契约 §7「加源需批准」）。

提案行格式（与画像提案同一套解析）：
    [2026-10-05 · 信源 · <名称>] 依据: <为什么> → 目标: <盲区-…|反方-…|播客-…> → 建议: <kind> <url>
kind ∈ rss（接受后加进 config/rss_feeds.json）/ podcast（接受后记进 config/podcast_subscriptions_accepted.json，
由播客管道订阅）/ html（没有 RSS，接受后记进 config/source_adapters_todo.json，等写采集器）。
信源提案的接受/拒绝**不进画像正文**（画像正文会进模型提示词），只从待接受区移除并记一行决定日志。
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from personal_intel_loop import CONFIG_DIR, RUNS_DIR

SOURCE_TAG = "· 信源 ·"
RSS_FEEDS_PATH = CONFIG_DIR / "rss_feeds.json"
PODCASTS_PATH = CONFIG_DIR / "podcast_subscriptions_accepted.json"
HTML_TODO_PATH = CONFIG_DIR / "source_adapters_todo.json"
DECISIONS_LOG = RUNS_DIR / "source_decisions.jsonl"

_SUGGEST_RE = re.compile(r"建议:\s*(rss|podcast|html)\s+(https?://\S+)")


def is_source_line(line: str) -> bool:
    return SOURCE_TAG in (line or "")


def parse_source_line(line: str) -> dict:
    header, _, rest = line.partition("]")
    name = header.split(SOURCE_TAG, 1)[1].strip() if SOURCE_TAG in header else ""
    target = ""
    for seg in rest.split(" → "):
        seg = seg.strip()
        if seg.startswith("目标:"):
            target = seg[len("目标:"):].strip()
    m = _SUGGEST_RE.search(rest)
    if not name or not m:
        raise ValueError("not a source proposal line")
    return {"name": name, "target": target, "kind": m.group(1), "url": m.group(2)}


def format_source_line(*, date_iso: str, name: str, basis: str, target: str, kind: str, url: str) -> str:
    return f"[{date_iso} · 信源 · {name}] 依据: {basis} → 目标: {target} → 建议: {kind} {url}"


def _load_list(path: Path) -> list:
    # 10-05 验收 G227: 只有文件不存在才当空列表; 损坏 JSON 或非 list 直接抛错——
    # 宁可让接受提案报错, 也不能把整份订阅表当空覆盖掉。
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list, got {type(data).__name__}")
    return data


def _save_list(path: Path, data: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 10-05 验收 G227: 写回前把原文件留成 .bak, 出错可恢复
    if path.exists():
        shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    tmp.replace(path)


def apply_decision(line: str, decision: str, *, rss_path: Path | None = None, podcasts_path: Path | None = None,
                   html_todo_path: Path | None = None, log_path: Path | None = None) -> dict:
    """接受：按 kind 落到对应配置（已存在同 URL 则不重复）；拒绝：只记日志。返回 {"applied": where|None}。"""
    rss_path = rss_path or RSS_FEEDS_PATH
    podcasts_path = podcasts_path or PODCASTS_PATH
    html_todo_path = html_todo_path or HTML_TODO_PATH
    log_path = log_path or DECISIONS_LOG
    if decision not in {"accept", "reject"}:
        raise ValueError(f"unsupported decision: {decision}")
    src = parse_source_line(line)
    where = None
    if decision == "accept":
        if src["kind"] == "rss":
            path, entry = rss_path, {"name": src["name"], "url": src["url"], "category": src["target"] or "新信源"}
        elif src["kind"] == "podcast":
            path, entry = podcasts_path, {"name": src["name"], "feed_url": src["url"], "group": src["target"]}
        else:
            path, entry = html_todo_path, {"name": src["name"], "url": src["url"], "group": src["target"]}
        items = _load_list(path)
        key = "feed_url" if src["kind"] == "podcast" else "url"
        if not any(isinstance(x, dict) and x.get(key) == src["url"] for x in items):
            items.append(entry)
            _save_list(path, items)
        where = path.name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "decision": decision, **src, "applied": where},
                            ensure_ascii=False) + "\n")
    return {"applied": where}
