"""非密配置读取与根键更新；凭据不经过本模块。"""
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzlocal import get_localzone_name

from .home import openathlete_home

DEFAULT_RIDE_TRAINING_MIN_KM = 15.0
_ROOT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TIMEZONE_ACTION = "执行 openathlete-mcp db config set --timezone <IANA时区> 后重试"


class ConfigError(ValueError):
    def __init__(self, message, next_action):
        self.next_action = next_action
        super().__init__(message)


def config_path(home=None):
    return (Path(home) if home else openathlete_home()) / "config.toml"


def load_config(home=None):
    path = config_path(home)
    if not path.exists():
        return {}
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    with path.open("rb") as f:
        return tomllib.load(f)


def ride_training_min_km(config):
    value = config.get("ride_training_min_km", DEFAULT_RIDE_TRAINING_MIN_KM)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("ride_training_min_km 必须是正数")
    return float(value)


def ride_threshold_confirmed(config):
    return "ride_training_min_km" in config


def primary_platform(config):
    value = config.get("primary_platform")
    return str(value).strip().lower() or None if value is not None else None


def _validate_timezone(value, system=False):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        try:
            ZoneInfo("UTC")
        except ZoneInfoNotFoundError:
            raise ConfigError(
                "本地时区数据库不可用，请安装或更新 tzdata。",
                "安装或更新 tzdata 后重试",
            ) from exc
        if system:
            raise ConfigError(
                "无法把系统时区解析为可用 IANA 时区：{}".format(value),
                _TIMEZONE_ACTION,
            ) from exc
        raise ConfigError(
            "timezone 必须是可用 IANA 时区：{}".format(value),
            _TIMEZONE_ACTION,
        ) from exc
    return value


def configured_timezone(config):
    value = config.get("timezone")
    if value is not None and not str(value).strip():
        raise ConfigError("timezone 不能为空，必须是可用 IANA 时区", _TIMEZONE_ACTION)
    system = value is None
    if system:
        try:
            value = get_localzone_name()
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(
                "系统时区所需的 tzdata 不可用。",
                "安装或更新 tzdata 后重试",
            ) from exc
    else:
        value = str(value).strip()
    return _validate_timezone(value, system=system)


def configured_tzinfo(config):
    return ZoneInfo(configured_timezone(config))


def timezone_source(config):
    return "config" if config.get("timezone") is not None else "system"


def activity_since(config):
    value = config.get("activity_since")
    if value is None or not str(value).strip():
        return None
    value = str(value).strip()
    datetime.strptime(value, "%Y-%m-%d")
    return value


def workout_platform(config):
    value = config.get("workout_platform")
    if value is None or not str(value).strip():
        return None
    value = str(value).strip().lower()
    if value not in {"coros", "garmin"}:
        raise ValueError("workout_platform 只支持 coros / garmin")
    return value


def _toml_value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    raise TypeError("不支持的配置值类型：{}".format(type(value).__name__))


def set_config_values(home=None, **values):
    home = Path(home) if home else openathlete_home()
    home.mkdir(parents=True, exist_ok=True)
    path = config_path(home)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    section_at = next((i for i, line in enumerate(lines)
                       if line.strip().startswith("[")), len(lines))
    root, rest = lines[:section_at], lines[section_at:]
    for key, value in values.items():
        if not _ROOT_KEY_RE.match(key):
            raise ValueError("非法配置键：{}".format(key))
        pattern = re.compile(r"^\s*{}(\s*)=".format(re.escape(key)))
        rendered = "{} = {}".format(key, _toml_value(value))
        for i, line in enumerate(root):
            if pattern.match(line):
                root[i] = rendered
                break
        else:
            root.append(rendered)
    output = "\n".join(root + rest).rstrip() + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".toml", dir=home)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(output)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path
