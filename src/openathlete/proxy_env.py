"""兼容代理软件常见、但 httpx 0.28 无法解析的 NO_PROXY IPv6 写法。"""
import ipaddress
import os


def _normalize_entry(entry):
    stripped = entry.strip()
    if len(stripped) < 2 or not stripped.startswith("[") or not stripped.endswith("]"):
        return entry
    address = stripped[1:-1]
    try:
        ipaddress.IPv6Address(address)
    except ValueError:
        return entry
    start = len(entry) - len(entry.lstrip())
    end = len(entry.rstrip())
    return entry[:start] + address + entry[end:]


def normalize_no_proxy(value):
    return ",".join(_normalize_entry(entry) for entry in value.split(","))


def normalize_proxy_env(environ=None):
    target = os.environ if environ is None else environ
    for name in ("NO_PROXY", "no_proxy"):
        value = target.get(name)
        if value is not None:
            target[name] = normalize_no_proxy(value)
