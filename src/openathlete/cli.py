"""openathlete-mcp 统一 CLI 入口（console script）。

  openathlete-mcp [serve]                                      # 无参数或 serve 启动统一 stdio MCP
  openathlete-mcp db init|ingest|anchor|stats|rebuild-load   # athlete.db 管理
  openathlete-mcp coros login-check                          # 高驰 web API 登录自检
  openathlete-mcp coros pull [--since D] [--until D] [--limit N] [--dry-run]
  openathlete-mcp coros metrics [--days N | --since D --until D]
  openathlete-mcp garmin workout create --file W [--date D] [--dry-run]
  openathlete-mcp garmin workout schedule --id ID --date D
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from .credentials import load_credentials, require_credentials
from .db import athlete_db
from .db.athlete_db import OPENATHLETE_HOME

_REPO_ENV = Path(__file__).resolve().parents[2] / ".env"  # 开发期兼容；安装后不存在自动跳过


def _yyyymmdd(s: str) -> str:
    s = s.strip()
    if len(s) == 10 and s[4] == "-":
        return s.replace("-", "")
    if len(s) == 8 and s.isdigit():
        return s
    raise argparse.ArgumentTypeError("日期格式须为 YYYY-MM-DD 或 YYYYMMDD：{}".format(s))


def _iso(yyyymmdd: str) -> str:
    return "{}-{}-{}".format(yyyymmdd[:4], yyyymmdd[4:6], yyyymmdd[6:8])


def _today():
    from .config import configured_tzinfo, load_config

    return datetime.now(configured_tzinfo(load_config(OPENATHLETE_HOME))).date()


def _coros_client():
    load_credentials(_REPO_ENV)
    cred = require_credentials("COROS_EMAIL", "COROS_PASSWORD")
    from .adapters.coros import CorosClient
    return CorosClient(cred["COROS_EMAIL"], cred["COROS_PASSWORD"])


def _coros_login_check(_args):
    client = _coros_client()
    client.ensure_login()
    items, total = client.list_activities(size=1)
    print("高驰 web API 登录正常：regionId={} userId={}，账号内活动共 {} 条".format(
        client.region_id, client.user_id, total))
    if items:
        a = items[0]
        print("最新一条：{} sportType={} 开始 {}（labelId {}）".format(
            a.get("name") or "-", a.get("sportType"),
            a.get("startTime"), a.get("labelId")))
    print("token 缓存：{}（24h 内复用，失效自动重登）".format(client.token_path))


def _coros_pull(args):
    from .sync_state import SyncError, backfill_activities

    client = _coros_client()
    today = _today()
    since = _iso(_yyyymmdd(args.since)) if args.since else (
        today - timedelta(days=7)).isoformat()
    until = _iso(_yyyymmdd(args.until)) if args.until else today.isoformat()
    try:
        result = backfill_activities(
            OPENATHLETE_HOME, since, until,
            dry_run=args.dry_run, limit=args.limit, client=client)
    except SyncError as exc:
        print("同步失败 {}：{}".format(exc.code, exc))
        if exc.next_action:
            print(exc.next_action)
        return 1
    if args.dry_run:
        for activity in result.get("activities", []):
            print("  {} sportType={} labelId {}".format(
                activity.get("startTime"), activity.get("sportType"),
                activity.get("labelId")))
        print("（dry-run，未下载，未扩展覆盖）")
    print("活动同步：发现 {}，处理 {}，成功 {}，失败 {}，待办 {}；coverage extended={}".format(
        result["discovered"], result["processed"], result["succeeded"],
        result["failed"], result["queued_remaining"], result["coverage_extended"]))
    if result.get("next_action"):
        print(result["next_action"])
    return 1 if result["failed"] else 0


def _coros_metrics(args):
    from .sync_state import SyncError, backfill_metrics

    client = _coros_client()
    today = _today()
    since = _iso(_yyyymmdd(args.since)) if args.since else (
        today - timedelta(days=args.days)).isoformat()
    until = _iso(_yyyymmdd(args.until)) if args.until else today.isoformat()
    try:
        result = backfill_metrics(
            OPENATHLETE_HOME, since, until,
            dry_run=args.dry_run, limit=args.limit, client=client)
    except SyncError as exc:
        print("指标同步失败 {}：{}".format(exc.code, exc))
        if exc.next_action:
            print(exc.next_action)
        return 1
    print("日度指标：发现 {}，入库 {}，coverage extended={}".format(
        result["discovered"], result["processed"], result["coverage_extended"]))
    if args.dry_run:
        print("（dry-run，未入库，未扩展覆盖）")
    if result.get("next_action"):
        print(result["next_action"])
    return 0


def _garmin_client():
    load_credentials(_REPO_ENV)
    cred = require_credentials("GARMIN_EMAIL", "GARMIN_PASSWORD")
    from .adapters.garmin import GarminClient
    return GarminClient(cred["GARMIN_EMAIL"], cred["GARMIN_PASSWORD"],
                        domain=os.environ.get("GARMIN_AUTH_DOMAIN", "COM"))


def _workout_from_file(path):
    with Path(path).open("r", encoding="utf-8") as f:
        workout = json.load(f)
    if not isinstance(workout, dict):
        raise ValueError("课表文件必须是 JSON object")
    return workout


def _garmin_workout_create(args):
    from .workout import WorkoutWriteError, create_garmin_workout, garmin_workout_payload
    try:
        workout = _workout_from_file(args.file)
        if args.dry_run:
            print(json.dumps(garmin_workout_payload(workout), ensure_ascii=False, indent=2))
            return 0
        client = _garmin_client()
        client.ensure_login()
        result = create_garmin_workout(client, workout, args.date)
    except (ValueError, WorkoutWriteError) as exc:
        print("佳明课表写入失败：{}".format(exc))
        if isinstance(exc, WorkoutWriteError) and exc.workout_id:
            print("已创建 workoutId={}，不要重复创建；请先手动核对后重试排期。".format(
                exc.workout_id))
        return 1
    print(json.dumps({
        "workoutName": result["workout"].get("workoutName"),
        "workoutId": result["workout_id"],
        "scheduled": result["scheduled"],
        "date": args.date,
        "workoutScheduleId": result["workout_schedule_id"],
    }, ensure_ascii=False))
    return 0


def _garmin_workout_schedule(args):
    try:
        client = _garmin_client()
        client.ensure_login()
        result = client.schedule_workout(args.id, args.date)
    except Exception as exc:  # noqa: BLE001 CLI 只返回错误，不自动重试
        print("佳明排期失败：{}".format(exc))
        return 1
    schedule_id = result.get("workoutScheduleId") or result.get("scheduleId") or result.get("id")
    print(json.dumps({"workoutId": args.id, "date": args.date,
                      "workoutScheduleId": schedule_id}, ensure_ascii=False))
    return 0


def _garmin_main(argv):
    ap = argparse.ArgumentParser(prog="openathlete-mcp garmin", description="佳明数据与课表接口")
    sub = ap.add_subparsers(dest="garmin_cmd", required=True)
    p_workout = sub.add_parser("workout", help="结构化课表创建与排期")
    workout_sub = p_workout.add_subparsers(dest="workout_cmd", required=True)
    p_create = workout_sub.add_parser("create", help="按统一 JSON 创建课表")
    p_create.add_argument("--file", required=True, help="统一 workout JSON 文件")
    p_create.add_argument("--date", help="可选排期日期 YYYY-MM-DD")
    p_create.add_argument("--dry-run", action="store_true", help="只打印佳明 payload，不触网")
    p_schedule = workout_sub.add_parser("schedule", help="把已有课表排到指定日期")
    p_schedule.add_argument("--id", required=True, help="佳明 workoutId")
    p_schedule.add_argument("--date", required=True, help="排期日期 YYYY-MM-DD")
    args = ap.parse_args(argv)
    if args.workout_cmd == "create":
        return _garmin_workout_create(args)
    if args.workout_cmd == "schedule":
        return _garmin_workout_schedule(args)
    return 2


def _coros_main(argv):
    ap = argparse.ArgumentParser(prog="openathlete-mcp coros",
                                 description="高驰 Training Hub web API（数据抓取不经 MCP）")
    sub = ap.add_subparsers(dest="coros_cmd", required=True)
    sub.add_parser("login-check", help="登录 + 拉一页活动自检")
    p_pull = sub.add_parser("pull", help="下载活动 FIT 到本地归档（幂等）")
    p_pull.add_argument("--since", help="起日 YYYY-MM-DD 或 YYYYMMDD（缺省 7 天前）")
    p_pull.add_argument("--until", help="止日（缺省今天）")
    p_pull.add_argument("--limit", type=int, help="最多处理 N 条（倒序）")
    p_pull.add_argument("--dry-run", action="store_true", help="只列出将下载什么")
    p_metrics = sub.add_parser("metrics", help="拉日度指标（静息心率/睡眠HRV）入 daily_metrics")
    p_metrics.add_argument("--days", type=int, default=28, help="回溯天数（缺省 28）")
    p_metrics.add_argument("--since", help="起日（覆盖 --days）")
    p_metrics.add_argument("--until", help="止日（缺省今天）")
    p_metrics.add_argument("--limit", type=int, help="最多入库 N 条（不扩展覆盖）")
    p_metrics.add_argument("--dry-run", action="store_true", help="只预览，不写入或扩展覆盖")
    args = ap.parse_args(argv)
    return {"login-check": _coros_login_check, "pull": _coros_pull,
            "metrics": _coros_metrics}[args.coros_cmd](args)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        from .mcp.server import main as serve
        serve()
        return 0
    if argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "serve":
        if rest:
            print("serve 不接受额外参数")
            return 2
        from .mcp.server import main as serve
        serve()
        return 0
    if cmd == "db":
        return athlete_db.main(rest)
    if cmd == "coros":
        return _coros_main(rest)
    if cmd == "garmin":
        return _garmin_main(rest)
    print("未知命令 {}：\n".format(cmd) + __doc__.strip())
    return 2


if __name__ == "__main__":
    sys.exit(main())
