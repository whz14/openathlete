"""OPENATHLETE_HOME 解析：一切用户状态（库/归档/记忆/token/日志）的唯一落点。

约定（DESIGN.md §3 路径约定）：`OPENATHLETE_HOME` 环境变量优先，默认 `~/.openathlete/`。
"""
import os
from pathlib import Path


def openathlete_home() -> Path:
    env = os.environ.get("OPENATHLETE_HOME")
    return Path(env).expanduser() if env else Path.home() / ".openathlete"


def fit_dir(home: Path | None = None) -> Path:
    return (home or openathlete_home()) / "fit"


def athlete_db_path(home: Path | None = None) -> Path:
    return (home or openathlete_home()) / "athlete.db"
