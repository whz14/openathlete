#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "garth==0.4.38",
#     "requests>=2.28",
#     "urllib3>=1.26",
#     "certifi",
#     "httpx>=0.27",     # openathlete Garmin adapter 的共享归档依赖
#     "oss2>=2.19",      # 国区（regionId 2）阿里云 OSS 中转
#     "boto3>=1.28",     # 国际区（regionId 1/3）S3 中转
# ]
# ///
"""佳明(Garmin Connect) -> 高驰(COROS) 运动记录同步入口。

复用 upstream/ (XiaoSiHwang/garmin-sync-coros) 的客户端实现，
修复其主脚本在 Windows 下的路径问题，并增加本地运行所需的
参数控制与进度记录。PEP 723 内联依赖，`uv run` 直跑（自动建隔离环境）。

首次使用：复制 .env.example 为 ~/.openathlete/credentials（或仓库 .env）并填写账号密码，然后:
    uv run ./sync_garmin_to_coros.py --limit 3 --dry-run   # 演练，不实际上传
    uv run ./sync_garmin_to_coros.py --limit 3             # 小量验证
    uv run ./sync_garmin_to_coros.py --since YYYY-MM-DD    # 按日期增量同步

注意：高驰平台单点登录，同步运行期间请不要打开高驰 App / 网页，
否则会把本脚本的会话踢下线导致失败。

判重：上传前自动查询高驰侧已有活动，按开始时间（±120s）匹配跳过，
本地 sync.db 丢失（换电脑未迁移）也不会重复上传；--no-server-dedup 关闭。
"""

import argparse
import bisect
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent
UPSTREAM_SCRIPTS = ROOT / "upstream" / "scripts"
sys.path.insert(0, str(UPSTREAM_SCRIPTS))
sys.path.insert(0, str(ROOT / "src"))

from openathlete.adapters.garmin import GarminClient as ArchiveGarminClient

# 同步状态（sync.db + fit/ 归档）统一存 $OPENATHLETE_HOME（默认 ~/.openathlete/），
# 与代理/日志同处——一切用户状态收敛在一个目录，软删/硬删/移植只操作它。
# 检测到仓库旧布局 ./data/ 时一次性迁移（移动，不复制）。
OPENATHLETE_HOME = Path(os.environ["OPENATHLETE_HOME"]).expanduser() \
    if os.environ.get("OPENATHLETE_HOME") else Path.home() / ".openathlete"
DB_PATH = OPENATHLETE_HOME / "sync.db"

_LEGACY_DATA_DIR = ROOT / "data"
if DB_PATH.parent != _LEGACY_DATA_DIR and not DB_PATH.exists() \
        and (_LEGACY_DATA_DIR / "sync.db").exists():
    import shutil
    try:
        OPENATHLETE_HOME.mkdir(parents=True, exist_ok=True)
        for _name in ("sync.db", "fit"):
            _src = _LEGACY_DATA_DIR / _name
            if _src.exists():
                shutil.move(str(_src), str(OPENATHLETE_HOME / _name))
        print(f"检测到旧 data/ 布局，同步状态已迁移到 {OPENATHLETE_HOME}")
    except OSError as _e:
        print(f"旧 data/ 迁移失败，沿用旧目录：{_e}", file=sys.stderr)
        DB_PATH = _LEGACY_DATA_DIR / "sync.db"

REGION_NAMES = {1: "国际区(美)", 2: "中国区", 3: "国际区(欧)"}

# 服务端判重：高驰 startTime 与佳明 beginTimestamp/1000 同源（实测差 0-1 秒），
# 容差取 120 秒覆盖时钟/取整差异（dev/probe_dedup_match.py 可复核）。
MATCH_TOLERANCE_SECONDS = 120


def epoch_seconds(value):
    """数值时间戳统一成秒（>1e11 视为毫秒）；无法解析返回 None。"""
    if value is None or value == "":
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds / 1_000 if seconds > 100_000_000_000 else seconds


def garmin_start_epoch(activity: dict):
    """佳明活动的开始时刻（epoch 秒，UTC）：优先 beginTimestamp，回退 startTimeGMT。"""
    ts = epoch_seconds(activity.get("beginTimestamp"))
    if ts:
        return ts
    try:
        return datetime.strptime(str(activity.get("startTimeGMT", "")),
                                 "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def fetch_coros_start_times(coros, min_epoch: float) -> list:
    """拉取高驰侧不早于 min_epoch 的活动开始时间（epoch 秒），按升序返回。

    高驰 /activity/query 按时间倒序翻页；页内最老一条已早于下限即停。
    实测字段：startTime 为 epoch 秒（UTC），与佳明 beginTimestamp/1000 对齐。
    """
    times = []
    page = 1
    floor = min_epoch - MATCH_TOLERANCE_SECONDS
    while True:
        resp = coros.getActivities(size=200, page=page)
        data = resp.get("data") or {}
        items = data.get("dataList") or []
        page_times = [t for t in (epoch_seconds(i.get("startTime")) for i in items) if t]
        times.extend(t for t in page_times if t >= floor)
        if not items or (page_times and min(page_times) < floor):
            break
        if page >= int(data.get("totalPage") or page):
            break
        page += 1
    return sorted(times)


def nearest_within(sorted_times: list, epoch: float, tolerance: float) -> bool:
    """升序列表中是否存在与 epoch 相差不超过 tolerance 的值。"""
    pos = bisect.bisect_left(sorted_times, epoch)
    for cand in (sorted_times[pos - 1] if pos else None,
                 sorted_times[pos] if pos < len(sorted_times) else None):
        if cand is not None and abs(cand - epoch) <= tolerance:
            return True
    return False


def load_env(env_path: Path) -> None:
    """极简 .env 加载：KEY=VALUE，支持 # 注释，已存在的环境变量优先。"""
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


def require_env(*names: str) -> dict:
    values = {name: os.environ.get(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        print("错误：以下配置项未填写：{}".format(", ".join(missing)))
        print("请填写凭据文件 ~/.openathlete/credentials（或仓库 .env，模板见 .env.example）后重试。")
        sys.exit(2)
    return values


class SyncDB:
    """记录每个佳明活动的同步状态：0 待同步 / 1 已同步 / 2 异常。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS garmin_activity(
                activity_id INTEGER PRIMARY KEY,
                start_time_local TEXT,
                is_sync_coros INTEGER NOT NULL DEFAULT 0,
                create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.commit()

    def register(self, activity_id: int, start_time_local: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO garmin_activity(activity_id, start_time_local) VALUES (?, ?)",
            (activity_id, start_time_local),
        )
        self.conn.commit()

    def unsynced(self) -> list:
        rows = self.conn.execute(
            "SELECT activity_id, start_time_local FROM garmin_activity "
            "WHERE is_sync_coros = 0 ORDER BY start_time_local"
        ).fetchall()
        return rows

    def mark(self, activity_id: int, status: int) -> None:
        self.conn.execute(
            "UPDATE garmin_activity SET is_sync_coros = ?, update_time = CURRENT_TIMESTAMP "
            "WHERE activity_id = ?",
            (status, activity_id),
        )
        self.conn.commit()

    def counts(self) -> dict:
        rows = self.conn.execute(
            "SELECT is_sync_coros, COUNT(*) FROM garmin_activity GROUP BY is_sync_coros"
        ).fetchall()
        return {status: count for status, count in rows}


def build_oss_client(region_id: int, access_token: str = None):
    """按高驰账号所在区域创建 OSS 客户端（STS 凭据由高驰官方接口签发，
    2026-10-03 起国区 STS 需登录 accessToken 鉴权，见上游 PR #42）。"""
    if region_id == 2:
        from oss.ali_oss_client import AliOssClient
        return AliOssClient(access_token=access_token)
    elif region_id in (1, 3):
        from oss.aws_oss_client import AwsOssClient
        return AwsOssClient()
    raise RuntimeError("未知的高驰区域 ID: {}".format(region_id))


def main() -> None:
    parser = argparse.ArgumentParser(description="佳明 -> 高驰 运动记录同步")
    parser.add_argument("--limit", type=int, default=0,
                        help="只处理佳明最近 N 条活动（0 = 全部）")
    parser.add_argument("--since", default="",
                        help="只同步该日期之后开始的活动，格式 YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true",
                        help="只列出将同步的活动，不下载也不上传")
    parser.add_argument("--no-server-dedup", action="store_true",
                        help="跳过服务端判重（默认上传前会查询高驰侧已有活动，"
                             "按开始时间匹配跳过，本地 sync.db 丢失时也不会重复上传）")
    args = parser.parse_args()

    # 凭据优先读数据 home（~/.openathlete/credentials），仓库 .env 为兼容回退；
    # load_env 不覆盖已有变量，先加载者生效，故 home 优先。
    load_env(OPENATHLETE_HOME / "credentials")
    load_env(ROOT / ".env")
    cfg = require_env("GARMIN_EMAIL", "GARMIN_PASSWORD", "COROS_EMAIL", "COROS_PASSWORD")
    garmin_domain = os.environ.get("GARMIN_AUTH_DOMAIN", "COM").strip() or "COM"

    print("注意：同步期间请不要打开高驰 App / 网页（高驰单点登录会互踢）。")

    from coros.coros_client import CorosClient
    from garmin.garmin_client import GarminClient
    from utils.md5_utils import calculate_md5_file

    coros = CorosClient(cfg["COROS_EMAIL"], cfg["COROS_PASSWORD"])
    try:
        coros.login()
    except Exception as err:
        print("高驰登录失败：{}".format(err))
        print("请检查 .env 中的 COROS_EMAIL / COROS_PASSWORD 是否正确（注意是登录邮箱，不是手机号）。")
        sys.exit(1)
    region = REGION_NAMES.get(coros.regionId, "未知区域")
    print("高驰登录成功：userId={} regionId={}（{}）".format(coros.userId, coros.regionId, region))

    garmin = GarminClient(cfg["GARMIN_EMAIL"], cfg["GARMIN_PASSWORD"], garmin_domain, 0)
    print("正在拉取佳明活动列表（区域：{}）...".format(garmin_domain))
    try:
        all_activities = garmin.getAllActivities()
    except Exception as err:
        print("佳明登录/拉取失败：{}".format(err))
        print("请检查 .env 中的 GARMIN_EMAIL / GARMIN_PASSWORD / GARMIN_AUTH_DOMAIN（COM 或 CN）。")
        print("如佳明账号开启了两步验证（MFA），请先在 Garmin 账号设置中关闭后再试。")
        sys.exit(1)
    if not all_activities:
        print("佳明侧没有拉到任何活动，检查账号或区域设置（COM/CN）。")
        return
    # 佳明返回按时间倒序；完整列表同时用于服务端判重的时间索引
    id2epoch = {a["activityId"]: garmin_start_epoch(a) for a in all_activities}
    activity_by_id = {a["activityId"]: a for a in all_activities}
    archive_client = ArchiveGarminClient(
        cfg["GARMIN_EMAIL"], cfg["GARMIN_PASSWORD"],
        home=OPENATHLETE_HOME, domain=garmin_domain)
    activities = all_activities
    if args.since:
        activities = [a for a in activities
                      if str(a.get("startTimeLocal", ""))[:10] >= args.since]
    if args.limit > 0:
        activities = activities[: args.limit]
    print("本次范围内共有 {} 条佳明活动。".format(len(activities)))

    db = SyncDB(DB_PATH)
    for a in activities:
        db.register(a["activityId"], str(a.get("startTimeLocal", "")))
    todo = db.unsynced()
    print("其中待同步 {} 条（本地数据库：{}）。".format(len(todo), DB_PATH))

    # 服务端判重：查高驰账号里已有的活动，按开始时间（±120s）匹配，命中的不再上传。
    # 解决本地 sync.db 丢失（换电脑/未迁移）时的重复上传问题；匹配不上时回退本地判重。
    if todo and not args.no_server_dedup:
        known_epochs = [e for e in (id2epoch.get(aid) for aid, _ in todo) if e]
        try:
            coros_times = fetch_coros_start_times(
                coros, min(known_epochs) if known_epochs else time.time())
        except Exception as err:  # noqa: BLE001 - 查询失败不应阻断同步
            print("高驰侧已有活动查询失败（{}），退回仅本地数据库判重。".format(err))
            coros_times = None
        if coros_times is not None:
            already, rest = [], []
            for activity_id, start_time in todo:
                ts = id2epoch.get(activity_id)
                if ts and nearest_within(coros_times, ts, MATCH_TOLERANCE_SECONDS):
                    already.append((activity_id, start_time))
                else:
                    rest.append((activity_id, start_time))
            for activity_id, start_time in already:
                print("  [高驰已存在，跳过] {} {}".format(start_time, activity_id))
                if not args.dry_run:
                    db.mark(activity_id, 1)
            if already:
                print("按开始时间匹配，高驰侧已存在 {} 条，无需重复上传。".format(len(already)))
            todo = rest

    if args.dry_run:
        for activity_id, start_time in todo:
            print("  [待同步] {} {}".format(start_time, activity_id))
        print("dry-run 结束，未做任何下载/上传。")
        return
    if not todo:
        print("没有需要同步的活动。")
        return

    ok = fail = 0
    consecutive_fail = 0
    for index, (activity_id, start_time) in enumerate(todo, 1):
        fd, upload_name = tempfile.mkstemp(prefix="openathlete-", suffix=".zip")
        os.close(fd)
        zip_path = Path(upload_name)
        try:
            data = garmin.downloadFitActivity(activity_id)
            archive_client.archive_fit(activity_by_id[activity_id], data)
            zip_path.write_bytes(data)
            md5 = calculate_md5_file(str(zip_path))
            oss_client = build_oss_client(coros.regionId, coros.accessToken)
            oss_client.multipart_upload(str(zip_path), "{}/{}.zip".format(coros.userId, md5))
            imported = coros.uploadActivity(
                "fit_zip/{}/{}.zip".format(coros.userId, md5),
                md5,
                "{}.zip".format(activity_id),
                zip_path.stat().st_size,
            )
            if imported:
                db.mark(activity_id, 1)
                ok += 1
                consecutive_fail = 0
                print("[{}/{}] 成功 {} {}".format(index, len(todo), start_time, activity_id))
            else:
                db.mark(activity_id, 2)
                fail += 1
                consecutive_fail += 1
                print("[{}/{}] 高驰拒绝导入 {} {}".format(index, len(todo), start_time, activity_id))
        except Exception as err:  # noqa: BLE001 - 单条失败不中断整体
            db.mark(activity_id, 2)
            fail += 1
            consecutive_fail += 1
            print("[{}/{}] 失败 {} {}：{}".format(index, len(todo), start_time, activity_id, err))
        finally:
            zip_path.unlink(missing_ok=True)
        if consecutive_fail >= 3:
            print("连续失败 3 次，提前中止（可能是登录失效或被限流）。修复后直接重跑即可，已同步的不会重复。")
            break
        time.sleep(1)  # 温和限速，避免触发平台风控

    summary = db.counts()
    print("-" * 50)
    print("本次结果：成功 {} 条，失败 {} 条。".format(ok, fail))
    print("累计：已同步 {} 条，待同步 {} 条，异常 {} 条。".format(
        summary.get(1, 0), summary.get(0, 0), summary.get(2, 0)))
    print("失败的条目下次运行会跳过；若想重试，请删除 {} 后重跑。".format(DB_PATH))


if __name__ == "__main__":
    main()
