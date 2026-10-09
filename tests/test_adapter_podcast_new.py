"""podcast_new adapter：三种目录形态（有元数据 / 文件名带日期 / 靠 mtime）、时间窗、去重、
transcript 与 media 字段。"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

from personal_intel_loop.adapters.podcast_new import PodcastNewAdapter

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _adapter(tmp_path, roots, **kw):
    kw.setdefault("days", 3)
    return PodcastNewAdapter(
        roots, state_path=tmp_path / "state.json", now_fn=lambda: NOW, **kw
    )


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")


def _set_mtime(path, when: datetime) -> None:
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))


# ---- 形态一：同名 .json 元数据------------------------------------------------


def test_metadata_shape(tmp_path):
    root = tmp_path / "podcasts"
    _write(root / "示例节目" / "2026-10-03_一期.md", "# 转录正文\n第二行")
    _write(
        root / "示例节目" / "2026-10-03_一期.json",
        json.dumps(
            {
                "title": "第 12 期：某某话题",
                "podcast": "示例节目",
                "pub_date": "2026-10-03T02:00:00Z",
                "url": "https://example.com/ep/12",
                "duration": 3120,
            },
            ensure_ascii=False,
        ),
    )

    records = list(_adapter(tmp_path, [root]).collect())
    assert len(records) == 1

    item = records[0].item
    assert item.source == "podcast_new:示例节目"
    assert item.title == "第 12 期：某某话题"  # 元数据标题优先
    assert item.author == "示例节目"
    assert item.url == "https://example.com/ep/12"
    assert item.ts.isoformat() == "2026-10-03T02:00:00+00:00"
    assert item.transcript == "# 转录正文\n第二行"  # 全文进 transcript
    assert item.body == "# 转录正文\n第二行"  # body 前 2000 字

    payload = json.loads(records[0].source_payload_json)
    assert payload["has_metadata"] is True
    assert payload["ts_source"] == "metadata"
    assert payload["synthetic_url"] is False
    assert payload["media"] == {"type": "audio", "duration_s": 3120.0}


def test_body_is_capped_at_2000(tmp_path):
    root = tmp_path / "podcasts"
    _write(root / "节目" / "长集.md", "字" * 5000)
    _set_mtime(root / "节目" / "长集.md", NOW - timedelta(days=1))

    item = list(_adapter(tmp_path, [root]).collect())[0].item
    assert len(item.body) == 2000
    assert len(item.transcript) == 5000


# ---- 形态二：文件名 `<日期>_<标题>`，无元数据---------------------------------


def test_filename_date_shape(tmp_path):
    root = tmp_path / "podcasts"
    _write(root / "小宇宙节目" / "2026-10-02_嘉宾聊 AI.md", "转录内容")
    _write(root / "小宇宙节目" / "2026-09-01_老一期.md", "老内容")

    records = list(_adapter(tmp_path, [root]).collect())
    assert len(records) == 1  # 9 月那集超出 3 天窗口

    item = records[0].item
    assert item.title == "嘉宾聊 AI"  # 日期前缀剥掉
    assert item.source == "podcast_new:小宇宙节目"
    assert item.ts.strftime("%Y-%m-%d") == "2026-10-02"
    payload = json.loads(records[0].source_payload_json)
    assert payload["has_metadata"] is False
    assert payload["ts_source"] == "filename"
    assert payload["synthetic_url"] is True  # 没链接 → 合成稳定 url
    assert item.url.startswith("https://local.intel-loop/podcast/")


# ---- 形态三：文件名只有标题，日期靠 mtime--------------------------------------


def test_mtime_shape(tmp_path):
    root = tmp_path / "podcasts"
    path = root / "视频节目" / "没有日期的一集.txt"
    _write(path, "纯文本转录")
    _set_mtime(path, NOW - timedelta(hours=6))

    records = list(_adapter(tmp_path, [root]).collect())
    assert len(records) == 1

    item = records[0].item
    assert item.title == "没有日期的一集"
    assert item.source == "podcast_new:视频节目"
    assert item.ts == NOW - timedelta(hours=6)
    payload = json.loads(records[0].source_payload_json)
    assert payload["ts_source"] == "mtime"
    assert payload["media"]["type"] == "audio"
    assert payload["media"]["duration_s"] is None


# ---- 时间窗 / 去重 / 多根目录 ------------------------------------------------


def test_time_window_excludes_old(tmp_path):
    root = tmp_path / "podcasts"
    _write(root / "节目" / "2026-09-20_旧的.md", "旧")
    _write(root / "节目" / "2026-10-03_新的.md", "新")
    _write(root / "节目" / "2026-10-02_窗内的.md", "窗内")

    titles = {r.item.title for r in _adapter(tmp_path, [root]).collect()}
    assert titles == {"新的", "窗内的"}


def test_dedup_across_rounds(tmp_path):
    root = tmp_path / "podcasts"
    _write(root / "节目" / "2026-10-03_一期.md", "内容")
    _write(
        root / "节目" / "2026-10-03_一期.json",
        json.dumps({"url": "https://example.com/ep/1", "pub_date": "2026-10-03T00:00:00Z"}),
    )

    assert len(list(_adapter(tmp_path, [root]).collect())) == 1
    assert list(_adapter(tmp_path, [root]).collect()) == []  # 第二轮已收过


def test_multiple_roots(tmp_path):
    a = tmp_path / "root_a"
    b = tmp_path / "root_b"
    _write(a / "节目A" / "2026-10-03_A.md", "内容A")
    _write(b / "节目B" / "2026-10-03_B.md", "内容B")

    records = list(_adapter(tmp_path, [a, b]).collect())
    assert {r.item.source for r in records} == {"podcast_new:节目A", "podcast_new:节目B"}


def test_missing_root_is_tolerated(tmp_path):
    root = tmp_path / "podcasts"
    _write(root / "节目" / "2026-10-03_一期.md", "内容")
    records = list(_adapter(tmp_path, [tmp_path / "不存在", root]).collect())
    assert len(records) == 1


def test_empty_transcript_skipped(tmp_path):
    root = tmp_path / "podcasts"
    _write(root / "节目" / "2026-10-03_空的.md", "   \n")
    assert list(_adapter(tmp_path, [root]).collect()) == []


def test_limit_caps_output(tmp_path):
    root = tmp_path / "podcasts"
    for i in range(5):
        _write(root / "节目" / f"2026-10-0{i + 1}_第{i}期.md", f"内容{i}")
    assert len(list(_adapter(tmp_path, [root]).collect(limit=2))) == 2


def test_days_param_widens_window(tmp_path):
    root = tmp_path / "podcasts"
    _write(root / "节目" / "2026-09-20_旧的.md", "旧")
    assert len(list(_adapter(tmp_path, [root], days=30).collect())) == 1


def test_alternative_metadata_keys(tmp_path):
    """元数据键名按 fixture 里的几种写法都认：show/published/link/duration_s。"""
    root = tmp_path / "podcasts"
    _write(root / "节目" / "2026-10-03_x.md", "内容")
    _write(
        root / "节目" / "2026-10-03_x.json",
        json.dumps(
            {
                "show": "别名节目",
                "title": "别名标题",
                "published": "2026-10-03",
                "link": "https://example.com/alt",
                "duration_s": 90,
            }
        ),
    )
    records = list(_adapter(tmp_path, [root]).collect())
    payload = json.loads(records[0].source_payload_json)
    assert records[0].item.source == "podcast_new:别名节目"
    assert records[0].item.title == "别名标题"
    assert records[0].item.url == "https://example.com/alt"
    assert payload["media"]["duration_s"] == 90.0