"""profile 画像接口: blindspots 读取 + PIL_PROFILE_DIR 覆盖(含调用时解析)。"""
from __future__ import annotations

import importlib

from personal_intel_loop import PROJECT_ROOT
from personal_intel_loop import profile as profile_mod


def test_load_blindspots_reads_dash_entries(tmp_path):
    path = tmp_path / "blindspots.md"
    path.write_text(
        "# 头部说明\n\n- 与自己立场相反的一手叙述\n- 小农户视角\n\n- 第二条列表项\n无前缀的普通行不算\n-   保留内部空格\n",
        "utf-8",
    )
    assert profile_mod.load_blindspots(path) == ["与自己立场相反的一手叙述", "小农户视角", "第二条列表项", "保留内部空格"]


def test_load_blindspots_missing_file_is_empty(tmp_path):
    assert profile_mod.load_blindspots(tmp_path / "nope.md") == []


def test_profile_dir_env_override(tmp_path, monkeypatch):
    """PIL_PROFILE_DIR 决定 PROFILE_DIR / PROFILE_PATH / BLINDSPOTS_PATH(reload 后可见)。"""
    monkeypatch.setenv("PIL_PROFILE_DIR", str(tmp_path))
    reloaded = importlib.reload(profile_mod)
    try:
        assert reloaded.PROFILE_DIR == tmp_path
        assert reloaded.PROFILE_PATH == tmp_path / "reading_profile.md"
        assert reloaded.BLINDSPOTS_PATH == tmp_path / "blindspots.md"
    finally:
        monkeypatch.undo()
        importlib.reload(reloaded)


def test_load_blindspots_resolves_env_at_call_time(tmp_path, monkeypatch):
    """不 reload 模块, load_blindspots() 调用时也能吃到环境变量。"""
    (tmp_path / "blindspots.md").write_text("- 干旱带农业\n", "utf-8")
    monkeypatch.setenv("PIL_PROFILE_DIR", str(tmp_path))
    assert profile_mod.load_blindspots() == ["干旱带农业"]
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("PIL_PROFILE_DIR", str(other))
    assert profile_mod.load_blindspots() == []


def test_existing_profile_functions_keep_explicit_path(tmp_path):
    """现有函数签名不变: 显式 path 优先于环境变量。"""
    custom = tmp_path / "reading_profile.md"
    custom.write_text("# p\n\n## 待接受的修订\n- 一条\n", "utf-8")
    monkey_dir = tmp_path / "elsewhere"
    monkey_dir.mkdir()
    import os

    old = os.environ.get("PIL_PROFILE_DIR")
    os.environ["PIL_PROFILE_DIR"] = str(monkey_dir)
    try:
        assert profile_mod.pending_proposals(custom) == ["一条"]
        assert profile_mod.load_blindspots(custom.parent / "blindspots.md") == []
    finally:
        if old is None:
            os.environ.pop("PIL_PROFILE_DIR", None)
        else:
            os.environ["PIL_PROFILE_DIR"] = old


def test_template_profile_is_well_formed():
    """仓库自带的虚构模板要保持契约第 3 节的结构(4–6 条盲区)。"""
    template_dir = PROJECT_ROOT / "config" / "profile_template"
    reading = (template_dir / "reading_profile.md").read_text("utf-8")
    assert "## 待接受的修订" in reading
    assert "## 已接受的修订" in reading
    spots = profile_mod.load_blindspots(template_dir / "blindspots.md")
    assert 4 <= len(spots) <= 6
