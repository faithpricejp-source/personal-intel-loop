from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

# 本地嵌入模型只允许单进程：sentence-transformers / joblib 默认会起多个 worker，
# 每个占 1G 以上内存。放在包初始化里，先于任何库导入。
os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
os.environ.setdefault("JOBLIB_MULTIPROCESSING", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else default


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent

#: 运行期数据根（sqlite、缓存、媒体、运行记录）。环境变量 PIL_HOME 覆盖。
APP_HOME = _env_path("PIL_HOME", Path.home() / "Library" / "Application Support" / "personal-intel-loop")
DATA_DIR = _env_path("PIL_DATA_DIR", APP_HOME / "data")
RUNS_DIR = DATA_DIR / "runs"
MEDIA_DIR = DATA_DIR / "media"
DB_PATH = _env_path("PIL_DB_PATH", DATA_DIR / "intel_loop.sqlite")
#: schema 迁移脚本随包分发。
MIGRATIONS_DIR = PACKAGE_ROOT / "migrations"
#: 配置目录（信息源清单、版面配置、地名别名等）。缺省用仓库自带的 config/（示例配置）。
CONFIG_DIR = _env_path("PIL_CONFIG_DIR", PROJECT_ROOT / "config")
#: 日报 markdown / 讨论包的输出目录（可以指向一个 Obsidian vault 里的子目录）。
STAGING_DIR = _env_path("PIL_STAGING_DIR", APP_HOME / "staging")
#: 可选：个人笔记库（Obsidian vault）根目录。不设置 = 关闭 vault 相关功能。
VAULT_DIR: Path | None = _env_path("PIL_VAULT_DIR", Path()) if os.environ.get("PIL_VAULT_DIR", "").strip() else None

LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc
DEFAULT_LOCAL_TZ_NAME = os.environ.get("PIL_TZ", "").strip() or "Asia/Tokyo"


def resolve_data_relpath(relpath: str) -> Path:
    """把库里存的 `data/...` 相对路径解析到 DATA_DIR 下。"""
    rel = str(relpath)
    if rel.startswith("data/"):
        return DATA_DIR / rel[len("data/"):]
    return DATA_DIR / rel


def ensure_runtime_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
