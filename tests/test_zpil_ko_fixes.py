# 代码审查复核（K+O 路）的回归测试（O-4/O-6 未采纳，其测试已剔除）。
#
# 测试方式说明（K-2 / K-7 为 Swift 代码，pytest 无法直接 import）：
# 把 macapp/main.swift 原文件（仅去掉文件末尾 5 行 App 入口，另把日志根目录
# FileManager.default.homeDirectoryForCurrentUser 一处表达式替换为环境变量，
# 避免测试写真实 ~/Library）与一个 driver 一起用本机 swiftc 编译成可执行文件，
# driver 直接调用真类 AppDelegate 的真方法来复现/验证行为。被测逻辑零改动。
import os
import platform
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAIN_SWIFT = PROJECT_ROOT / "macapp" / "main.swift"

swiftc = pytest.mark.skipif(
    shutil.which("swiftc") is None, reason="本机无 swiftc，无法编译 macapp 真代码"
)


def _compile_driver(tmp_path, driver_body):
    """把真 main.swift（去 App 入口 + 日志根目录重定向）和 driver 编译成可执行文件。

    返回编译产物目录。外部调用全部是字面量参数列表 + cwd 关键字，不走 shell。
    """
    src = MAIN_SWIFT.read_text("utf-8")

    entry = "let app = NSApplication.shared"
    assert src.count(entry) == 1, "main.swift 入口块标记变了，请更新测试"
    src = src[: src.index(entry)]

    home_expr = "FileManager.default.homeDirectoryForCurrentUser"
    assert src.count(home_expr) == 1, "main.swift 日志根目录表达式变了，请更新测试"
    src = src.replace(
        home_expr,
        'URL(fileURLWithPath: ProcessInfo.processInfo.environment["PIL_TEST_HOME"] ?? "/tmp")',
    )

    build = tmp_path / "kbuild"
    build.mkdir()
    (build / "app_under_test.swift").write_text(src, "utf-8")
    (build / "main.swift").write_text(driver_body, "utf-8")

    fake_root = build / "fakeroot"
    pil_dir = fake_root / ".venv" / "bin"
    pil_dir.mkdir(parents=True)
    fake_pil = pil_dir / "pil"
    fake_pil.write_text(
        '#!/bin/sh\necho $$ >> "$PIL_PIDS_FILE"\nexec /bin/sleep 30\n', "utf-8"
    )
    fake_pil.chmod(0o755)

    # App 从 Info.plist 读 PILProjectRoot；可执行文件用 __TEXT 段内嵌同样的 plist
    plist_body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<plist version="1.0"><dict><key>PILProjectRoot</key><string>%s'
        "</string></dict></plist>\n" % fake_root
    )
    (build / "Info.plist").write_text(plist_body, "utf-8")

    arch = platform.machine() or "arm64"
    proc = subprocess.run(
        [
            "swiftc",
            "-target",
            "%s-apple-macos13.0" % arch,
            "-module-cache-path",
            "mcache",
            "-framework",
            "UserNotifications",
            "-framework",
            "ServiceManagement",
            "-o",
            "driver",
            "-Xlinker",
            "-sectcreate",
            "-Xlinker",
            "__TEXT",
            "-Xlinker",
            "__info_plist",
            "-Xlinker",
            "Info.plist",
            "app_under_test.swift",
            "main.swift",
        ],
        cwd=build,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, "swiftc 编译失败:\n%s\n%s" % (
        proc.stdout,
        proc.stderr,
    )
    return build


def _fake_pil_log_home(tmp_path):
    """预创建日志文件：startServer 里 FileHandle(forWritingTo:) 需要文件已存在。"""
    log_dir = tmp_path / "fakehome" / "Library" / "Logs" / "TodayPaper"
    log_dir.mkdir(parents=True)
    (log_dir / "server.log").touch()
    return tmp_path / "fakehome"


K2_DRIVER_BODY = textwrap.dedent(
    '''
    import Foundation

    // 直接调用真 AppDelegate：模拟服务未起时连点两次重试，然后退出 App
    let delegate = AppDelegate()
    let pidsPath = ProcessInfo.processInfo.environment["PIL_PIDS_FILE"] ?? ""

    // 等假 pil 把 pid 写进文件，避免 SIGTERM 先于 echo $$ 的竞态
    func waitForPidCount(_ n: Int) {
        for _ in 0..<150 {
            let s = ((try? String(contentsOfFile: pidsPath, encoding: .utf8)) ?? "")
            if s.split(separator: "\\n").count >= n { return }
            Thread.sleep(forTimeInterval: 0.02)
        }
    }

    delegate.startServer()
    waitForPidCount(1)
    delegate.startServer()
    waitForPidCount(2)

    delegate.applicationWillTerminate(Notification(name: Notification.Name("k2test")))
    Thread.sleep(forTimeInterval: 1.0)   // 给被终止进程一点收割时间

    let lines = ((try? String(contentsOfFile: pidsPath, encoding: .utf8)) ?? "")
        .split(separator: "\\n").map(String.init)
    var alive: [pid_t] = []
    for line in lines {
        guard let n = Int(line) else { continue }
        if kill(pid_t(n), 0) == 0 { alive.append(pid_t(n)) }
    }

    if lines.isEmpty {
        FileHandle.standardError.write(
            Data("K2TEST FAIL: no server process spawned at all\\n".utf8))
        exit(2)
    }
    if alive.isEmpty {
        print("K2TEST PASS: spawned=\\(lines.count), no orphan after terminate")
        exit(0)
    }
    FileHandle.standardError.write(Data(
        "K2TEST FAIL: spawned \\(lines.count), orphan pids \\(alive) survived\\n".utf8))
    for pid in alive { kill(pid, SIGKILL) }   // 清理，判定已在上面完成
    exit(1)
    '''
).strip() + "\n"


@swiftc
def test_k2_double_start_server_leaves_no_orphan(tmp_path):
    """K-2：服务未起时连点两次「重试」（startServer 跑两遍），退出 App 后不许留下孤儿服务进程。

    修复前：两次调用各起一个假 pil 进程，applicationWillTerminate 只 terminate
    第二个（server 句柄被覆盖），第一个成孤儿 → driver 检测到存活 pid，退出码 1。
    """
    build = _compile_driver(tmp_path, K2_DRIVER_BODY)
    pids = build / "pids.txt"
    child_env = dict(os.environ)
    child_env["PIL_TEST_HOME"] = str(_fake_pil_log_home(tmp_path))
    child_env["PIL_PIDS_FILE"] = str(pids)
    proc = subprocess.run(
        ["./driver"],
        cwd=build,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        "修复后仍应无孤儿进程，driver 退出码 %d\nstdout: %s\nstderr: %s"
        % (proc.returncode, proc.stdout, proc.stderr)
    )
    assert "K2TEST PASS" in proc.stdout


def _zhihu_answer_entry(entry_id, target_id, created):
    """构造一条知乎 answer 动态（形状与 tests/fixtures/zhihu_moments.json 一致）。"""
    return {
        "id": entry_id,
        "type": "feed",
        "created_time": created,
        "action_text": "示例action_text",
        "actors": [{"id": "a1", "name": "示例name"}],
        "target": {
            "id": target_id,
            "type": "answer",
            "created_time": created,
            "content": "<p>示例content。这是虚构正文。</p>",
            "question": {"id": "11468613", "title": "示例title"},
            "author": {"id": "示例id", "name": "示例name", "url_token": "示例token"},
            "voteup_count": "514",
            "comment_count": "114",
        },
    }


def test_k3_zhihu_all_ad_page_keeps_paging(tmp_path):
    """K-3：整页全是广告/无 id 条目时不能据此停翻页，后面未读的动态不许被截断。"""
    from personal_intel_loop.adapters.zhihu_moments import ZhihuMomentsAdapter

    ad_page = {
        "data": [
            {"id": "ad1", "type": "feed_advert", "target": None},
            {"id": "", "type": "feed", "target": None},  # 无 id 条目同样不计 fresh
        ],
        "paging": {"is_end": False, "next": "https://example.com/next?p=2"},
    }
    page2 = {
        "data": [_zhihu_answer_entry("e2", "a2", 222)],
        "paging": {"is_end": True, "next": ""},
    }
    pages = [ad_page, page2]
    calls = []

    def fetch(url):
        calls.append(url)
        return pages.pop(0) if pages else {"data": [], "paging": {"is_end": True}}

    adapter = ZhihuMomentsAdapter(
        fetch=fetch, state_path=tmp_path / "state.json", sleep_s=0, max_pages=5
    )
    records = list(adapter.collect())

    # 修复前：第 1 页 fresh==0 就 break，page2 永远取不到
    assert calls == [None, "https://example.com/next?p=2"]
    assert len(records) == 1
    assert records[0].item.url == "https://www.zhihu.com/question/11468613/answer/a2"


def test_k4_weread_published_at_non_string_skips_only_that_article(tmp_path):
    """K-4：某篇 meta 的 published_at 非字符串时只丢那篇的时间，不许整号文章全丢。"""
    import json

    from personal_intel_loop.adapters.weread_mp import default_list_articles

    book = tmp_path / "bookA"
    book.mkdir()
    (book / "good.json").write_text(
        json.dumps(
            {
                "title": "好文",
                "url": "https://mp.weixin.qq.com/s/good",
                "published_at": "2026-10-01 08:00",
            }
        ),
        "utf-8",
    )
    (book / "good.md").write_text("正文", "utf-8")
    (book / "bad.json").write_text(
        json.dumps(
            {
                "title": "坏文",
                "url": "https://mp.weixin.qq.com/s/bad",
                "published_at": 12345,  # 导出器写坏：数字不是字符串
            }
        ),
        "utf-8",
    )
    (book / "bad.md").write_text("正文", "utf-8")

    # 修复前：strptime 抛 TypeError 未捕获，整个号的列表直接炸
    articles = default_list_articles("bookA", 0, profile_dir=tmp_path)
    urls = {a["url"] for a in articles}
    assert "https://mp.weixin.qq.com/s/good" in urls
    assert len(articles) == 2


def test_k5_weibo_fetch_page_cache_keyed_by_storage_state(tmp_path, monkeypatch):
    """K-5：换 storage_state 后 default_fetch_page 必须重建 session，不许复用旧登录态。"""
    from pathlib import Path

    from personal_intel_loop.adapters import weibo_home as wh

    # 可变默认参数是进程级共享的，先清干净保证测试自洽
    wh.default_fetch_page.__kwdefaults__["_cache"].clear()

    state_a = tmp_path / "stateA.json"
    state_b = tmp_path / "stateB.json"
    state_a.write_text("{}", "utf-8")
    state_b.write_text("{}", "utf-8")

    built_for = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"ok": 1}

    class FakeSession:
        def __init__(self, tag):
            self.tag = tag

        def get(self, url, params=None, timeout=None):
            return FakeResponse()

    def fake_session(path):
        built_for.append(str(path))
        return FakeSession(str(path))

    monkeypatch.setattr(wh, "_weibo_session", fake_session)
    monkeypatch.setattr(wh, "_latest_group_id", lambda s, timeout: "gid-1")

    wh.default_fetch_page(0, storage_state=Path(state_a), timeout=0.1)
    wh.default_fetch_page(0, storage_state=Path(state_b), timeout=0.1)

    # 修复前：_cache 共享 → 第二个登录态被静默忽略，session 只建一次
    assert built_for == [str(state_a), str(state_b)]


def test_k6_zhihu_default_fetch_cache_keyed_by_cookies_file(tmp_path, monkeypatch):
    """K-6：换 cookies_file 后 zhihu_moments.default_fetch 必须重建会话，不许复用旧 cookie。"""
    import sys
    import types

    from personal_intel_loop.adapters import zhihu_moments as zm

    zm.default_fetch.__kwdefaults__["_cache"].clear()

    cookies_a = tmp_path / "cookiesA.json"
    cookies_b = tmp_path / "cookiesB.json"
    cookies_a.write_text("{}", "utf-8")
    cookies_b.write_text("{}", "utf-8")

    built_for = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [], "paging": {"is_end": True}}

    class FakeSession:
        def get(self, url, timeout=None):
            return FakeResponse()

    def _session(cookies_file):
        built_for.append(str(cookies_file))
        return FakeSession()

    monkeypatch.setattr(zm, "_session", _session)

    zm.default_fetch(None, cookies_file=cookies_a, timeout=0.1)
    zm.default_fetch(None, cookies_file=cookies_b, timeout=0.1)

    # 修复前：_cache 共享 → 换知乎 cookie 文件后仍用旧会话，session 只建一次
    assert built_for == [str(cookies_a), str(cookies_b)]


def test_o1_cn_consular_limit_with_detail_ts_reorder_keeps_seen_consistent(tmp_path):
    """O-1 复核：同日期条目 + 详情页时分翻转顺序 + limit=1 下，seen 必须恰好等于实际返回的 url。

    现行代码 R13 已在抓详情页**之前**把 candidates 截到 limit 条，collect 末尾的
    `records[:limit]` 恒为 no-op，sort 与 produced 的 zip 错位无从生效。此测试钉住该行为。
    """
    import json

    from personal_intel_loop.adapters.cn_consular import CnConsularAdapter

    url_a = "https://cs.mfa.gov.cn/aqtx/202610/t20261006_1.html"
    url_b = "https://cs.mfa.gov.cn/aqtx/202610/t20261006_2.html"
    list_html = (
        '<ul class="news-list">'
        '<li><a href="./202610/t20261006_1.html">赴甲国安全提醒</a><span>2026-10-06</span></li>'
        '<li><a href="./202610/t20261006_2.html">赴乙国安全提醒</a><span>2026-10-06</span></li>'
        "</ul>"
    )

    def detail(title, hhmm):
        return (
            f'<h1 class="article-title">{title}</h1>'
            f'<div class="article-meta"><span>发布时间：2026-10-06 {hhmm}</span></div>'
            '<div class="article-content"><div class="view_default">正文内容</div></div>'
        )

    pages = {
        "https://cs.mfa.gov.cn/aqtx/": list_html,
        # 列表页顺序 甲→乙；详情页时间 甲 09:00 < 乙 14:55，若发生重排即与 produced 错位
        url_a: detail("赴甲国安全提醒", "09:00"),
        url_b: detail("赴乙国安全提醒", "14:55"),
    }
    adapter = CnConsularAdapter(
        fetch_list=lambda url: pages.get(url, ""),
        fetch_detail=lambda url: pages.get(url, ""),
        state_path=tmp_path / "state.json",
    )
    records = list(adapter.collect(limit=1))

    assert len(records) == 1
    # R13 预截断按列表页顺序保留第 1 条（同日期稳定排序），它就是实际返回且进 seen 的那条
    assert records[0].item.url == url_a
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert state["seen"] == [url_a]  # 未返回的 url_b 不许进 seen（O-1 声称的永久丢失不存在）


def test_o2_disaster_alerts_naive_since_is_host_tz_independent(monkeypatch, tmp_path):
    """O-2：`--since 2026-10-01`（naive）不许随宿主 TZ 漂移，边界条目按北京时间判定。"""
    import os
    import time as time_mod
    from datetime import datetime

    from personal_intel_loop.adapters import disaster_alerts as da
    from personal_intel_loop.adapters.disaster_alerts import DisasterAlertsAdapter

    monkeypatch.setattr(da, "WATCHED_CN", ({"province": "广东", "city": "广州市"},))
    monkeypatch.setattr(da, "LOCATION_FILE", tmp_path / "no_location.json")

    cn_url = "https://www.nmc.cn/publish/alarm/o2.html"
    cn_items = [
        {
            "alertid": "o2",
            "title": "广东省广州市气象台发布暴雨橙色预警信号",
            "issuetime": "2026/10/01 01:00",  # 北京时间 10-01 凌晨, UTC 是 09-30 17:00
            "url": "/publish/alarm/o2.html",
        }
    ]
    adapter = DisasterAlertsAdapter(
        fetch_cn=lambda province: cn_items,
        fetch_jp_feed=lambda: "",
        fetch_jp_doc=lambda url: "",
    )

    results = {}
    orig_tz = os.environ.get("TZ")
    try:
        for tz in ("UTC", "Asia/Tokyo"):
            os.environ["TZ"] = tz
            time_mod.tzset()
            # 修复前: naive since 被 astimezone 按宿主时区解释, TZ 不同 cutoff 差 9 小时
            results[tz] = sorted(
                r.item.url for r in adapter.collect(since=datetime(2026, 10, 1))
            )
    finally:
        if orig_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = orig_tz
        time_mod.tzset()

    assert results["UTC"] == results["Asia/Tokyo"]
    # 北京时间 10-01 01:00 的预警在「--since 2026-10-01」下应当命中
    assert results["UTC"] == [cn_url]


def test_o3_podcast_new_naive_since_is_host_tz_independent(tmp_path):
    """O-3：podcast_new 的 naive `since` 不许随宿主 TZ 漂移（与函数内 now 的 UTC 归一对齐）。"""
    import json as json_mod
    import os
    import time as time_mod
    from datetime import datetime, timezone

    from personal_intel_loop.adapters.podcast_new import PodcastNewAdapter

    root = tmp_path / "podcasts" / "示例节目"
    root.mkdir(parents=True)
    boundary = root / "2026-09-30_边界集.md"
    boundary.write_text("边界正文", "utf-8")
    (root / "2026-09-30_边界集.json").write_text(
        json_mod.dumps({"pub_date": "2026-09-30T23:00:00+00:00"}), "utf-8"
    )
    inside = root / "2026-10-02_窗口内集.md"
    inside.write_text("窗口内正文", "utf-8")
    (root / "2026-10-02_窗口内集.json").write_text(
        json_mod.dumps({"pub_date": "2026-10-02T00:00:00+00:00"}), "utf-8"
    )

    results = {}
    orig_tz = os.environ.get("TZ")
    try:
        for tz in ("UTC", "Asia/Tokyo"):
            os.environ["TZ"] = tz
            time_mod.tzset()
            adapter = PodcastNewAdapter(
                roots=(tmp_path / "podcasts",),
                state_path=tmp_path / f"state_{tz}.json",
                now_fn=lambda: datetime(2026, 10, 7, tzinfo=timezone.utc),
            )
            # 修复前: naive since 按宿主时区解释 → UTC 下 cutoff=10-01T00:00Z, Tokyo 下
            # cutoff=09-30T15:00Z, 23:00Z 的边界集一边被丢一边被收
            results[tz] = sorted(
                r.item.title for r in adapter.collect(since=datetime(2026, 10, 1))
            )
    finally:
        if orig_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = orig_tz
        time_mod.tzset()

    assert results["UTC"] == results["Asia/Tokyo"]
    assert results["UTC"] == ["窗口内集"]


def test_o5_who_don_compound_country_kept_whole():
    """O-5：`\\band\\b` 分隔符不许把 "Trinidad and Tobago" 这类复合国名拆成两个假地区。"""
    from personal_intel_loop.adapters.who_don import regions_from_title

    # 复合国名整体保留
    assert regions_from_title("Yellow fever - Trinidad and Tobago") == [
        "Trinidad and Tobago"
    ]
    assert regions_from_title("Crimean-Congo haemorrhagic fever - Bosnia and Herzegovina") == [
        "Bosnia and Herzegovina"
    ]
    # R08 的并列拆分语义不受影响：真并列照拆、疾病名照滤
    assert regions_from_title(
        "Ebola disease caused by Bundibugyo virus, Democratic Republic of the Congo & Uganda"
    ) == ["Democratic Republic of the Congo", "Uganda"]


def test_o7_cn_consular_detail_without_article_title_keeps_list_title(tmp_path):
    """O-7：详情页缺 `article-title` 时兜底 `<h1>` 会拿到站点 logo，不许覆盖列表页好标题。"""
    from personal_intel_loop.adapters.cn_consular import (
        CnConsularAdapter,
        parse_detail,
    )

    url = "https://cs.mfa.gov.cn/aqtx/202610/t20261006_3.html"
    list_html = (
        '<ul class="news-list">'
        '<li><a href="./202610/t20261006_3.html">赴丙国安全提醒</a><span>2026-10-06</span></li>'
        "</ul>"
    )
    # 站点改版：article-title class 没了，但站点 logo <h1> 和正文容器还在
    detail_html = (
        "<h1>中国领事服务网</h1>"
        '<div class="article-meta"><span>发布时间：2026-10-06 14:55</span></div>'
        '<div class="article-content"><div class="view_default TRS_UEDITOR">正文内容</div></div>'
    )

    # 修复前：parse_detail 把 logo 当标题返回
    assert parse_detail(detail_html)["title"] == ""

    pages = {"https://cs.mfa.gov.cn/aqtx/": list_html, url: detail_html}
    adapter = CnConsularAdapter(
        fetch_list=lambda u: pages.get(u, ""),
        fetch_detail=lambda u: pages.get(u, ""),
        state_path=tmp_path / "state.json",
    )
    records = list(adapter.collect())
    assert len(records) == 1
    assert records[0].item.title == "赴丙国安全提醒"


def test_o8_local_transcripts_unreadable_file_skipped_not_fatal(tmp_path):
    """O-8：文件扫描后被删/被锁（OSError）只跳过该文件，不许挂掉整轮 collect。"""
    import os

    from personal_intel_loop.adapters.local_transcripts import LocalTranscriptsAdapter

    root = tmp_path / "notes"
    series = root / "栏目"
    series.mkdir(parents=True)
    good = series / "好文件.md"
    good.write_text("# 好标题\n\n正文", "utf-8")
    bad = series / "坏文件.md"
    bad.write_text("# 坏标题\n\n正文", "utf-8")
    os.chmod(bad, 0o000)  # 模拟 TCC 拒读/被锁：stat 正常、read 抛 PermissionError

    try:
        adapter = LocalTranscriptsAdapter(
            {"notes": root}, state_path=tmp_path / "state.json"
        )
        # 修复前：PermissionError 沿 generator 穿透，整轮 ingest 挂掉
        titles = [r.item.title for r in adapter.collect()]
    finally:
        os.chmod(bad, 0o644)  # 还原权限让 tmp 清理不炸
    assert titles == ["好标题"]


def test_o9_podcast_new_unreadable_file_skipped_not_fatal(tmp_path):
    """O-9：podcast_new 的 read_text 同样只捕 UnicodeDecodeError，坏文件不许挂掉整轮。"""
    import os
    from datetime import datetime, timezone

    from personal_intel_loop.adapters.podcast_new import PodcastNewAdapter

    root = tmp_path / "podcasts" / "示例节目"
    root.mkdir(parents=True)
    good = root / "2026-10-06_好集.md"
    good.write_text("好集正文", "utf-8")
    bad = root / "2026-10-06_坏集.md"
    bad.write_text("坏集正文", "utf-8")
    os.chmod(bad, 0o000)

    try:
        adapter = PodcastNewAdapter(
            (tmp_path / "podcasts",),
            state_path=tmp_path / "state.json",
            now_fn=lambda: datetime(2026, 10, 7, tzinfo=timezone.utc),
        )
        # 修复前：read_text 抛 PermissionError 未捕，collect 中断
        titles = [r.item.title for r in adapter.collect()]
    finally:
        os.chmod(bad, 0o644)
    assert titles == ["好集"]
