# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "garth==0.4.38",
#     "requests>=2.28",
#     "urllib3>=1.26",
#     "certifi",
# ]
# ///
"""只读探针：验证佳明活动列表与高驰 /activity/query 的时间戳字段能否对齐。

背景：sync_garmin_to_coros.py 判重只靠本地 sync.db，db 丢失会重复上传。
修复方向是"上传前查高驰已有活动按开始时间判重"，本探针确认：
1. 高驰 /activity/query 返回哪些字段、startTime 是秒还是毫秒、是否 UTC；
2. 同一活动在两侧的数值差多少（决定匹配容差）。

只调用列表接口，不下载、不上传。注意高驰单点登录：探针登录会踢开高驰 App/网页。
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "upstream" / "scripts"))

OPENATHLETE_HOME = Path(os.environ["OPENATHLETE_HOME"]).expanduser() \
    if os.environ.get("OPENATHLETE_HOME") else Path.home() / ".openathlete"


def load_env(env_path: Path) -> None:
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


def epoch_seconds(value):
    """数值时间戳统一成秒（>1e11 视为毫秒）。"""
    if value is None or value == "":
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds > 100_000_000_000:
        seconds /= 1_000
    return seconds


def fmt(epoch_s):
    return datetime.fromtimestamp(epoch_s, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def main() -> None:
    load_env(OPENATHLETE_HOME / "credentials")
    load_env(ROOT / ".env")
    for name in ("GARMIN_EMAIL", "GARMIN_PASSWORD", "COROS_EMAIL", "COROS_PASSWORD"):
        if not os.environ.get(name):
            print(f"缺少凭据 {name}，退出。")
            sys.exit(2)

    from coros.coros_client import CorosClient
    from garmin.garmin_client import GarminClient

    coros = CorosClient(os.environ["COROS_EMAIL"], os.environ["COROS_PASSWORD"])
    coros.login()
    print(f"高驰登录成功 regionId={coros.regionId}")

    page = coros.getActivities(size=30, page=1)
    data_list = page["data"]["dataList"]
    print(f"高驰第 1 页拿到 {len(data_list)} 条，totalPage={page['data'].get('totalPage')}")
    if data_list:
        print("\n高驰首条原始字段：")
        print(json.dumps(data_list[0], ensure_ascii=False, indent=2)[:2000])

    garmin = GarminClient(
        os.environ["GARMIN_EMAIL"], os.environ["GARMIN_PASSWORD"],
        os.environ.get("GARMIN_AUTH_DOMAIN", "COM"), 0,
    )
    g_acts = garmin.getActivities(start=0, limit=100)
    print(f"\n佳明最近 {len(g_acts)} 条活动。")
    if g_acts:
        sample = g_acts[0]
        print("佳明首条时间字段："
              f"startTimeLocal={sample.get('startTimeLocal')!r} "
              f"startTimeGMT={sample.get('startTimeGMT')!r} "
              f"beginTimestamp={sample.get('beginTimestamp')!r}")

    # 建佳明 beginTimestamp(毫秒) 索引，逐条对高驰列表找最近邻，输出差值分布。
    g_index = {}
    for a in g_acts:
        ts = epoch_seconds(a.get("beginTimestamp"))
        if ts:
            g_index[a["activityId"]] = ts
    g_times = sorted(g_index.values())

    import bisect
    diffs = []
    print("\n逐条对比（高驰最近 30 条 vs 佳明最近 100 条）：")
    for c in data_list:
        c_ts = epoch_seconds(c.get("startTime"))
        if not c_ts:
            print(f"  高驰 {c.get('labelId')} 无 startTime：{list(c)[:8]}")
            continue
        pos = bisect.bisect_left(g_times, c_ts)
        candidates = [t for t in (g_times[pos - 1] if pos else None,
                                  g_times[pos] if pos < len(g_times) else None) if t]
        best = min(candidates, key=lambda t: abs(t - c_ts), default=None)
        if best is None:
            continue
        diff = c_ts - best
        diffs.append(diff)
        mark = "匹配" if abs(diff) <= 120 else "无对应"
        print(f"  高驰 {fmt(c_ts)} sportType={c.get('sportType')} -> "
              f"佳明最近 {fmt(best)} 差 {diff:+.0f}s [{mark}]")
    matched = [d for d in diffs if abs(d) <= 120]
    print(f"\n容差 120s 内匹配 {len(matched)}/{len(diffs)} 条；"
          f"差值集合：{sorted(set(int(d) for d in matched)) or '无'}")


if __name__ == "__main__":
    main()
