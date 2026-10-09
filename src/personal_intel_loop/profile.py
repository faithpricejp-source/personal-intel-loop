"""阅读偏好 profile: 系统对用户的模型, 人类可读可改的一页文本 (<画像目录>/reading_profile.md)。

形态是 scrutable recommendation: 推荐以这页为条件, 用户改一句推荐跟着变; 后台不直改, 只往
「待接受的修订」追加提案。这里只管读写与切分, 不做判断。

画像目录(契约第 3 节): 环境变量 PIL_PROFILE_DIR 指定, 缺省 <PIL_HOME>/profile;
目录里两个文件 reading_profile.md + blindspots.md(缺失 = 盲区版为空)。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from personal_intel_loop import APP_HOME, LOCAL_TZ, RUNS_DIR

_PROFILE_DIR_ENV = os.environ.get("PIL_PROFILE_DIR")
PROFILE_DIR = Path(_PROFILE_DIR_ENV).expanduser() if _PROFILE_DIR_ENV else APP_HOME / "profile"
PROFILE_PATH = PROFILE_DIR / "reading_profile.md"
BLINDSPOTS_PATH = PROFILE_DIR / "blindspots.md"
PROPOSALS_HEADER = "## 待接受的修订"
ACCEPTED_HEADER = "## 已接受的修订"


def resolve_profile_dir() -> Path:
    """调用时再读一次环境变量——import 之后才 setenv 的测试也能覆盖。"""
    env = os.environ.get("PIL_PROFILE_DIR")
    return Path(env).expanduser() if env else PROFILE_DIR


def resolve_profile_path() -> Path:
    return resolve_profile_dir() / "reading_profile.md"


def load_profile(path: Path | None = None) -> str:
    if path is None:
        path = resolve_profile_path()  # fix-1007-N-7
    try:
        return path.read_text("utf-8")
    except FileNotFoundError:
        return ""


def load_blindspots(path: Path | None = None) -> list[str]:
    """blindspots.md 每行一条盲区方向(以「- 」开头), 去掉前缀; 文件不存在返回 []。

    path 缺省时在**调用时**解析 PIL_PROFILE_DIR / 缺省目录。
    """
    if path is None:
        path = resolve_profile_dir() / "blindspots.md"
    try:
        text = Path(path).read_text("utf-8")
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and len(stripped) > 2:
            out.append(stripped[2:].strip())
    return out


def profile_body_for_prompt(path: Path | None = None) -> str:
    """喂给模型的部分: 不含未接受的提案——没被用户接受的东西不能影响判断。"""
    if path is None:
        path = resolve_profile_path()  # fix-1007-N-7
    text = load_profile(path)
    idx = text.find(PROPOSALS_HEADER)
    return (text[:idx] if idx >= 0 else text).rstrip()


def pending_proposals(path: Path | None = None) -> list[str]:
    if path is None:
        path = resolve_profile_path()  # fix-1007-N-7
    text = load_profile(path)
    lines = text.splitlines()
    # 与 append_proposal/decide_proposal 同口径, 按整行标题前缀定位段落,
    # 并在下一个 '## ' 章节处截止 —— 读到 EOF 会把后续章节的 '- ' 行算进待接受
    sec_idx = next((i for i, v in enumerate(lines) if v.strip().startswith(PROPOSALS_HEADER)), None)
    if sec_idx is None:
        return []
    end_idx = next(
        (i for i in range(sec_idx + 1, len(lines)) if lines[i].strip().startswith("## ")),
        len(lines),
    )
    out = []
    for line in lines[sec_idx + 1:end_idx]:
        stripped = line.strip()
        if stripped.startswith("- ") and len(stripped) > 2:
            out.append(stripped[2:])
    return out


def append_proposal(line: str, path: Path | None = None) -> None:
    """把一条提案追加到「待接受的修订」末尾; 段落不存在则创建。占位的孤立 '-' 行会被替换。"""
    if path is None:
        path = resolve_profile_path()  # fix-1007-N-7
    text = load_profile(path)
    entry = f"- {line.strip()}"
    lines = text.splitlines()
    # 与 decide_proposal 同口径, 按整行标题前缀定位段落、插进本段落末尾;
    # 原子串 in/split 会把提案追加到全文末尾, 待接受段落后若有别的章节,
    # 后续章节的 '- ' 行会混进 pending_proposals, 新提案也落在错误章节之下
    sec_idx = next((i for i, v in enumerate(lines) if v.strip().startswith(PROPOSALS_HEADER)), None)
    if sec_idx is None:
        text = text.rstrip("\n") + f"\n\n{PROPOSALS_HEADER}\n{entry}\n"
        path.write_text(text, "utf-8")
        return
    end_idx = next(
        (i for i in range(sec_idx + 1, len(lines)) if lines[i].strip().startswith("## ")),
        len(lines),
    )
    block = [l for l in lines[sec_idx + 1:end_idx] if l.strip() != "-"]
    lines[sec_idx + 1:end_idx] = [*block, entry]
    path.write_text("\n".join(lines).rstrip("\n") + "\n", "utf-8")


def decide_proposal(line: str, decision: str, path: Path | None = None) -> int:
    """接受或拒绝一条精确匹配的提案, 返回剩余待接受数量。"""
    if path is None:
        path = resolve_profile_path()  # fix-1007-N-7
    if decision not in {"accept", "reject"}:
        raise ValueError(f"unsupported proposal decision: {decision}")
    text = load_profile(path)
    lines = text.splitlines()
    # 标题允许带尾注(如「## 待接受的修订(后台提案区…)」), 按前缀匹配
    pending_idx = next((i for i, value in enumerate(lines) if value.strip().startswith(PROPOSALS_HEADER)), None)
    if pending_idx is None:
        raise ValueError("pending proposals section not found")
    target_idx = next(
        (i for i in range(pending_idx + 1, len(lines)) if lines[i].strip() == f"- {line.strip()}"),
        None,
    )
    if target_idx is None:
        raise ValueError("proposal not found")
    removed = lines.pop(target_idx)
    if decision == "accept":
        accepted_idx = next((i for i, value in enumerate(lines) if value.strip().startswith(ACCEPTED_HEADER)), None)
        if accepted_idx is None:
            accepted_idx = next((i for i, value in enumerate(lines) if value.strip().startswith(PROPOSALS_HEADER)), len(lines))
            lines[accepted_idx:accepted_idx] = [ACCEPTED_HEADER, ""]
            accepted_idx += 1
        lines.insert(accepted_idx + 1, removed)
    else:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with (RUNS_DIR / "profile_rejected.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp}\t{line.strip()}\n")
    path.write_text("\n".join(lines).rstrip() + "\n", "utf-8")
    return len(pending_proposals(path))


def profile_mtime_local(path: Path | None = None) -> str | None:
    if path is None:
        path = resolve_profile_path()  # fix-1007-N-7
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, LOCAL_TZ).isoformat(timespec="minutes")
    except FileNotFoundError:
        return None
