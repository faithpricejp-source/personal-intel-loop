"""DeepSeek 错峰判定(主力档用 DeepSeek 时, 批量/定时任务只在非高峰调用以省钱)。

缺省关闭; 环境变量 PIL_OFFPEAK_GATE=deepseek 开启(见 offpeak_gate_enabled)。

官方价目页(2026-10-07 实查 https://api-docs.deepseek.com/zh-cn/quick_start/pricing):
北京时间周一至周五(不含中国法定节假日) 9:00-12:00、14:00-18:00 为高峰, 价格是空闲的两倍;
其余时段(含周末、法定节假日全天)为空闲。
不维护节假日表: 工作日一律按可能高峰处理(节假日少用几段空闲, 不会多花钱)。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

BEIJING_TZ = timezone(timedelta(hours=8))
PEAK_BLOCKS = ((9, 12), (14, 18))  # 北京时间 [start, end) 小时

logger = logging.getLogger(__name__)


def offpeak_gate_enabled() -> bool:
    return (os.environ.get("PIL_OFFPEAK_GATE") or "").strip().lower() == "deepseek"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def deepseek_peak_until(now: datetime) -> datetime | None:
    """now 落在高峰时段则返回该段结束时刻(北京时间), 否则 None。naive datetime 按 UTC 解释。"""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    bj = now.astimezone(BEIJING_TZ)
    if bj.weekday() >= 5:
        return None
    for start, end in PEAK_BLOCKS:
        if start <= bj.hour < end:
            return bj.replace(hour=end, minute=0, second=0, microsecond=0)
    return None


def wait_for_offpeak(*, clock=None, sleep=None, label: str = "deepseek") -> float:
    """高峰时段就睡到下一个空闲时刻(循环再判), 返回等待秒数。每次调模型前调用。"""
    clock = clock or utc_now
    sleep = sleep or time.sleep
    waited = 0.0
    while True:
        now = clock()
        until = deepseek_peak_until(now)
        if until is None:
            return waited
        seconds = max(1.0, (until - now).total_seconds())
        logger.info(
            "%s: DeepSeek 高峰时段(北京 %s), 等待 %.0f 秒到北京 %s 再调模型",
            label, now.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M"), seconds, until.strftime("%Y-%m-%d %H:%M"),
        )
        sleep(seconds)
        waited += seconds
