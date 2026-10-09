"""版面预算(契约第 10.4 节): 各栏条数上限 + 温暖源列表, 读 config/paper_layout.toml。

文件缺失/字段缺失回落到契约缺省值。risk 不设限(有多少行程发多少); inbox 不设限
(按来源折叠); follow 的上限约束每周推荐关注的条数。
"""
from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from personal_intel_loop import CONFIG_DIR

logger = logging.getLogger(__name__)

LAYOUT_FILENAME = "paper_layout.toml"

DEFAULT_CAPS = {
    "lead": 1,
    "top": 5,
    "counter": 5,
    "briefs": 30,
    "blind": 9,
    "warmth": 5,
    "opportunity": 3,
    "settle": 5,
    "leisure": 4,
    "follow": 5,
    # 风险栏里采集器入库的官方真实条目(source_payload.kind=="risk")上限。
    # 同行前简报是两条独立的量: 简报按行程数(UNCAPPED), 真实条目按这个上限截断。
    "risk_real": 5,
}
# 不设上限的栏(risk 按行程生成, inbox 由投递箱决定)
UNCAPPED = ("risk", "inbox")


def layout_path_for(config_dir: Path | None = None) -> Path:
    return (Path(config_dir) if config_dir is not None else CONFIG_DIR) / LAYOUT_FILENAME


def load_layout(path: Path | None = None) -> dict:
    """返回 {"caps": {section: n}, "warmth_sources": [source...]}。文件缺失用全缺省。"""
    path = layout_path_for() if path is None else Path(path)
    caps = dict(DEFAULT_CAPS)
    warmth_sources: list[str] = []
    try:
        data = tomllib.loads(path.read_text("utf-8"))
    except (FileNotFoundError, tomllib.TOMLDecodeError, OSError) as exc:
        if isinstance(exc, tomllib.TOMLDecodeError):
            # 语法错误与文件缺失不同——用户写了配置却整套没生效, 必须留痕
            logger.warning("layout: %s is not valid TOML, falling back to defaults: %s", path, exc)
        return {"caps": caps, "warmth_sources": warmth_sources}
    layout_section = data.get("layout")
    if isinstance(layout_section, dict):
        for section, limit in layout_section.items():
            # 10-05 审计 F19: isinstance(True, int) 为 True, 布尔上限会把该栏静默压到 1/0——显式排除
            if section in caps and isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0:
                caps[section] = limit
    warmth_section = data.get("warmth")
    if isinstance(warmth_section, dict):
        raw_sources = warmth_section.get("sources")
        if isinstance(raw_sources, list):
            warmth_sources = [str(source).strip() for source in raw_sources if isinstance(source, str) and source.strip()]
    return {"caps": caps, "warmth_sources": warmth_sources}


def cap_of(caps: dict, section: str) -> int | None:
    """某栏上限; risk/inbox 无上限返回 None。"""
    if section in UNCAPPED:
        return None
    return caps.get(section)
