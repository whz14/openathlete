"""凭据读取：KEY=VALUE 文件，Agent 不读取内容、只负责引导用户填写。

优先级：已存在的环境变量 > ~/.openathlete/credentials > 仓库 .env（过渡期兼容）。
与 sync_garmin_to_coros.py 的 load_env/require_env 行为一致。
"""
import os
import sys
from pathlib import Path

from .home import openathlete_home


def load_credentials(repo_env: Path | None = None) -> None:
    """按优先级把凭据文件并入环境变量；已存在的环境变量不被覆盖。"""
    candidates = [openathlete_home() / "credentials"]
    if repo_env is not None:
        candidates.append(repo_env)
    for path in candidates:
        _load_env_file(path)


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def require_credentials(*names: str) -> dict:
    values = {name: os.environ.get(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        print("错误：以下配置项未填写：{}".format(", ".join(missing)))
        print("请填写凭据文件 ~/.openathlete/credentials（或仓库 .env，模板见 .env.example）后重试。")
        sys.exit(2)
    return values
