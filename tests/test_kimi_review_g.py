"""复核 Kimi 审查发现（G 路）· 4 条。

前端（app.js）的用例走 tests/frontend/kimi_review_g.js：用 vm 在沙箱里执行真实的
app.js（剥掉 IIFE 壳，直接调用文件里的真实函数），本文件只负责跑 node 并转述失败原因。
后端可达性用真实的 personal_intel_loop.schemas.canonicalize_url 直接验证。

各条结论见 ../VERDICT.md：
  G-1 成立 → 已修（trackImpEnter 不再重置已在计时的 start/ok）
  G-3 成立但修法=改 UI 行为 → needs_decision（本用例只记录现状，不是对修复的断言）
  G-4 危险协议在部分后端路径确实无校验，但修法=安全设计 → needs_decision
  G-5 不成立（内层 promise 被 return，外层 catch 覆盖到了；无 unhandled rejection）
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from personal_intel_loop.schemas import Item, canonicalize_url

HARNESS = Path(__file__).resolve().parent / "frontend" / "kimi_review_g.js"


def run_case(case_id: str) -> str:
    """跑前端沙箱用例，失败时把 node 的输出整段抛出来。"""
    node = shutil.which("node")
    if node is None:
        pytest.fail("node 不可用：前端用例无法执行（tests/frontend/kimi_review_g.js）")
    proc = subprocess.run(
        [node, str(HARNESS), case_id],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, f"用例 {case_id} 失败（exit {proc.returncode}）:\n{out}"
    return out


# ---------------------------------------------------------------- G-1 成立


def test_g1_impression_survives_threshold_cross():
    """G-1：IntersectionObserver 在 threshold 交叉时对「仍在视口内」的元素重发 entry，
    trackImpEnter 不得把已积累的可见时长清零；真正离开时要发出 1 条 ms≥800 的 impression。
    修复前该用例失败（start 被重置、impression 整体丢失）。"""
    run_case("G1_impression_survives_threshold_cross")


# ---------------------------------------------------------------- G-5 不成立


def test_g5_inner_refetch_is_handled_by_outer_catch():
    """G-5：addTrip 里 `return api.trips().then(...)` 把内层请求接进了外层链，
    2108 的 .catch 能收到它的失败 → 不存在 Kimi 说的 unhandled rejection。
    用例同时断言：没有 unhandledRejection、flash 拿到了错误信息。"""
    run_case("G5_addtrip_refetch_is_caught")


# ---------------------------------------------------------------- G-3


def test_g3_archive_page2_failure_keeps_loaded_items():
    """翻第 2 页失败：保留已加载条目、末尾追加提示行、游标与计数不动，重试用原游标接着取。"""
    run_case("G3_page2_failure_keeps_loaded_items")


def test_g3_first_page_failure_retry_refetches_first_page():
    run_case("G3_first_page_failure_retry_refetches_first_page")


# ---------------------------------------------------------------- G-4


def test_g4_external_links_only_http_https():
    """前端外链只放行 http/https，其它协议当作没有链接（AI 产出/投递箱的 url 不经后端协议校验）。"""
    run_case("G4_external_links_only_http_https")


def test_g4_item_url_is_blocked_by_canonicalize_url():
    """G-4 可达性（条目主路径）：item.url 入库前过 canonicalize_url，
    没有 host 的 javascript:/data: 会被拒绝，所以「item.url=javascript:alert(1)」这条路堵得住。"""
    for bad in ("javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "vbscript:msgbox(1)"):
        with pytest.raises(ValueError):
            canonicalize_url(bad)
    with pytest.raises(ValidationError):
        Item(
            id="rss:x",
            source="rss_briefing:src1",
            url="javascript:alert(1)",
            title="t",
            body="b",
            ts="2026-10-05T00:00:00Z",
            lang="en",
        )


def test_g4_canonicalize_url_keeps_unknown_scheme_when_host_present():
    """G-4 缺口：canonicalize_url 只要求有 host，不校验协议白名单——
    带 host 的 javascript://… 仍会原样保留 scheme。浏览器会不会执行未验证（无浏览器）。"""
    out = canonicalize_url("javascript://evil.example.com/%0Aalert(1)")
    assert out.startswith("javascript:"), out
    assert canonicalize_url("https://ok.example.com/a") == "https://ok.example.com/a"
