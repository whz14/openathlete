"""九工具的参数校验、同步编排与业务错误映射。"""
import asyncio
import sqlite3
from datetime import datetime, timedelta

from .. import queries
from ..config import ConfigError, configured_timezone, configured_tzinfo, load_config
from .envelope import failure, success

_STREAM_ACTIVITIES = "coros-activities"
_STREAM_METRICS = "coros-daily-metrics"


def _invalid(message, next_action="修正参数后重试。"):
    return ValueError(message + (" " + next_action if next_action else ""))


def _check_args(args, allowed):
    if not isinstance(args, dict):
        raise _invalid("工具参数必须是 object。")
    unknown = sorted(set(args) - set(allowed))
    if unknown:
        raise _invalid("未知参数：{}".format(", ".join(unknown)))
    for key, value in args.items():
        if value is None:
            raise _invalid("参数 {} 不能为 null；省略时才应用默认值。".format(key))


def _integer(args, key, default, minimum, maximum):
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid("{} 必须是整数。".format(key))
    if not minimum <= value <= maximum:
        raise _invalid("{} 必须在 {}..{}。".format(key, minimum, maximum))
    return value


def _boolean(args, key, default=False):
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise _invalid("{} 必须是 boolean。".format(key))
    return value


def _string(args, key):
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _invalid("{} 必须是非空字符串。".format(key))
    return value.strip()


def _date(value, field="date"):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise _invalid("{} 必须是真实的 YYYY-MM-DD 日期。".format(field)) from exc


def _sync_error_details(exc):
    return {
        "code": exc.code,
        "message": str(exc),
        "retryable": exc.retryable,
        "next_action": exc.next_action,
        "details": exc.details,
    }


def _coverage_after_sync_error(home, windows):
    from ..sync_state import get_coverage

    coverage = {}
    for stream, window in windows.items():
        try:
            start, end = window if isinstance(window, (tuple, list)) else (None, None)
            coverage[stream] = get_coverage(home, stream, start, end)
        except Exception:  # noqa: BLE001 保留原同步错误，本地覆盖只作补充
            coverage[stream] = {"complete": False, "stale": True}
    return coverage


def _prepare_sync(home, windows):
    from ..sync_state import SyncError, ensure_fresh

    try:
        sync_meta, coverage = ensure_fresh(home, windows)
        return sync_meta, coverage, None
    except SyncError as exc:
        return (
            {"attempted": True, "success": False, "trigger": "ttl"},
            _coverage_after_sync_error(home, windows),
            exc,
        )
    except (OSError, sqlite3.Error) as exc:
        from ..sync_state import SyncError
        error = SyncError(
            "DB_BUSY" if "busy" in str(exc).lower() or "locked" in str(exc).lower()
            else "INTERNAL_ERROR",
            "本地同步状态读取失败。",
            retryable=True,
            next_action="关闭占用同一 HOME 的其他进程后重试。",
            details={"exception_type": type(exc).__name__},
        )
        return (
            {"attempted": True, "success": False, "trigger": "ttl"},
            _coverage_after_sync_error(home, windows),
            error,
        )


def _coverage_items(remote, windows):
    if not remote:
        return []
    if all(stream in remote for stream in windows):
        return [remote[stream] for stream in windows]
    return [remote]


def _merged_coverage(local, remote, windows, sync_meta, sync_error):
    items = _coverage_items(remote, windows)
    merged = dict(local)
    if items:
        merged["complete"] = all(bool(item.get("complete")) for item in items)
        merged["stale"] = any(bool(item.get("stale", not item.get("complete")))
                              for item in items)
        starts = [item.get("actual_start") or item.get("coverage_start")
                  for item in items]
        ends = [item.get("actual_end") or item.get("coverage_end") for item in items]
        merged["actual_start"] = min(
            (value for value in starts if value), default=local.get("actual_start"))
        merged["actual_end"] = max(
            (value for value in ends if value), default=local.get("actual_end"))
        timestamps = [item.get("last_sync_ts") for item in items]
        merged["last_sync_ts"] = max((value for value in timestamps if value), default=None)
        remote_missing = [missing for item in items for missing in item.get("missing", [])]
        merged["missing"] = list(dict.fromkeys(merged.get("missing", []) + remote_missing))
        merged["streams"] = remote
        for key in ("backfill_hint", "pmc_initialized", "pmc_required_start"):
            values = [item[key] for item in items if item.get(key) is not None]
            if values:
                merged[key] = values[0]
    if sync_error:
        merged["complete"] = False
        merged["stale"] = True
        merged["missing"] = list(dict.fromkeys(
            merged.get("missing", []) + [sync_error.code.lower()]))
    merged["sources"] = list(dict.fromkeys(
        merged.get("sources", []) + ["coros-web", "filesystem"]))
    merged["sync"] = sync_meta
    return merged


def _has_local_data(tool, data):
    if tool == "query_activities":
        return bool(data["activities"])
    if tool == "get_training_load":
        return bool(data["daily"])
    if tool == "get_recovery_status":
        return data.get("source") is not None
    if tool == "get_overview":
        return bool(data["training"]["activity_count"] or data["latest_load"]
                    or data["recovery"].get("source"))
    return bool(data)


def _finish(tool, data, coverage, sync_meta, sync_error):
    warnings = list(coverage.pop("warnings", []))
    meta = {"coverage": coverage, "sync": sync_meta, "warnings": warnings,
            "timezone": coverage.get("timezone") or "UTC"}
    if (tool in {"query_activities", "get_training_load", "get_overview"}
            and coverage.get("canonical_count") == 0
            and coverage.get("excluded_conflict_count", 0) > 0):
        return failure(
            tool, "CANONICAL_CONFLICT", "时间窗内的活动无法确定性选择 canonical。",
            next_action="确认 primary_platform 并全量重建；冲突活动不会当作没有训练。",
            details={"excluded_conflict_count": coverage["excluded_conflict_count"]},
            meta=meta,
        )
    if sync_error and not _has_local_data(tool, data):
        return failure(
            tool,
            sync_error.code,
            str(sync_error),
            retryable=sync_error.retryable,
            next_action=sync_error.next_action,
            details=sync_error.details,
            meta=meta,
        )
    if not sync_error and not coverage.get("complete") and not _has_local_data(tool, data):
        next_action = coverage.get("backfill_hint") or "再次调用 sync_now，或按覆盖提示回补"
        return failure(
            tool, "COVERAGE_INSUFFICIENT", "本地与已同步数据不足以回答该时间窗。",
            retryable=True, next_action=next_action,
            details={"missing": coverage.get("missing", [])}, meta=meta,
        )
    if coverage.get("complete"):
        coverage["missing"] = [item for item in coverage.get("missing", [])
                               if item != "activities"]
    if sync_error:
        warnings.append(_sync_error_details(sync_error))
    if coverage.get("excluded_conflict_count", 0) > 0:
        warnings.append({"code": "CANONICAL_CONFLICT",
                         "count": coverage["excluded_conflict_count"]})
    return success(tool, data, meta)


def _query_meta(home):
    config = load_config(home)
    try:
        return configured_timezone(config)
    except ConfigError:
        return str(config.get("timezone") or "UTC")


async def _activity_query(home, args):
    _check_args(args, {"days", "start_date", "end_date", "sport", "limit", "detail"})
    if "days" in args and "start_date" in args:
        raise _invalid("days 与 start_date 不能同时提供。")
    days = _integer(args, "days", 7, 1, 90)
    limit = _integer(args, "limit", 20, 1, 100)
    detail = _boolean(args, "detail")
    sport = _string(args, "sport") if "sport" in args else None
    start, end = queries.resolve_window(home, days, args.get("end_date"),
                                        args.get("start_date"))
    _date(start, "start_date")
    _date(end, "end_date")
    windows = {_STREAM_ACTIVITIES: (start, end)}
    sync_meta, remote, sync_error = await asyncio.to_thread(_prepare_sync, home, windows)
    data, local = await asyncio.to_thread(
        queries.query_activities, home, start, end, sport, limit, detail)
    coverage = _merged_coverage(local, remote, windows, sync_meta, sync_error)
    coverage["timezone"] = _query_meta(home)
    return _finish("query_activities", data, coverage, sync_meta, sync_error)


async def _load_query(home, args):
    _check_args(args, {"days", "end_date"})
    days = _integer(args, "days", 42, 7, 365)
    start, end = queries.resolve_window(home, days, args.get("end_date"))
    required_start = (_date(start, "start_date") - timedelta(days=42)).isoformat()
    windows = {_STREAM_ACTIVITIES: (required_start, end)}
    sync_meta, remote, sync_error = await asyncio.to_thread(_prepare_sync, home, windows)
    data, local = await asyncio.to_thread(queries.get_training_load, home, start, end)
    coverage = _merged_coverage(local, remote, windows, sync_meta, sync_error)
    anchors = local.get("anchors") or {}
    total = anchors.get("activity_count", 0)
    anchors_complete = all(
        anchors.get(key, {}).get("covered") == anchors.get(key, {}).get("total")
        for key in ("max_hr", "resting_hr")
    )
    no_trimp_gap = anchors.get("unknown_trimp_count", 0) == 0
    no_pmc_gap = (local.get("pmc") or {}).get("unknown_days", 0) == 0
    has_valid_pmc = data.get("latest") is not None
    initialized = bool(
        coverage.get("complete") and anchors_complete and no_trimp_gap and no_pmc_gap
        and (total == 0 or has_valid_pmc)
    )
    if initialized and total == 0 and not data["daily"]:
        daily = []
        day = _date(start, "start_date")
        last = _date(end, "end_date")
        while day <= last:
            daily.append({"date": day.isoformat(), "trimp": 0.0,
                          "ctl": 0.0, "atl": 0.0, "tsb": 0.0})
            day += timedelta(days=1)
        data["daily"] = daily
        data["latest"] = dict(daily[-1])
        data["headline"] = "截至 {}：覆盖窗口内无活动，CTL/ATL/TSB 均为 0。".format(end)
    if initialized:
        coverage["missing"] = [
            item for item in coverage.get("missing", [])
            if item not in {"daily_load", "trimp", "threshold_anchors", "pmc"}
        ]
    coverage["complete"] = initialized
    coverage["stale"] = not initialized or bool(coverage.get("stale"))
    coverage["pmc_initialized"] = initialized
    if isinstance(coverage.get("pmc"), dict):
        coverage["pmc"]["initialized"] = initialized
    coverage["timezone"] = _query_meta(home)
    return _finish("get_training_load", data, coverage, sync_meta, sync_error)


async def _recovery_query(home, args):
    _check_args(args, {"date"})
    config = load_config(home)
    date = args.get("date") or datetime.now(configured_tzinfo(config)).date().isoformat()
    _date(date)
    windows = {_STREAM_METRICS: (date, date)}
    sync_meta, remote, sync_error = await asyncio.to_thread(_prepare_sync, home, windows)
    data, local = await asyncio.to_thread(queries.get_recovery_status, home, date)
    coverage = _merged_coverage(local, remote, windows, sync_meta, sync_error)
    coverage["timezone"] = _query_meta(home)
    return _finish("get_recovery_status", data, coverage, sync_meta, sync_error)


async def _overview_query(home, args):
    _check_args(args, {"days", "end_date"})
    days = _integer(args, "days", 7, 1, 90)
    start, end = queries.resolve_window(home, days, args.get("end_date"))
    windows = {_STREAM_ACTIVITIES: (start, end), _STREAM_METRICS: (end, end)}
    sync_meta, remote, sync_error = await asyncio.to_thread(_prepare_sync, home, windows)
    activity_windows = {_STREAM_ACTIVITIES: windows[_STREAM_ACTIVITIES]}
    activity_remote = {_STREAM_ACTIVITIES: remote.get(_STREAM_ACTIVITIES, {})}
    activity_complete = bool(activity_remote[_STREAM_ACTIVITIES].get("complete"))
    activity_error = sync_error if not activity_complete else None
    coverage_input = _merged_coverage(
        {}, activity_remote, activity_windows, sync_meta, activity_error)
    data, local = await asyncio.to_thread(
        queries.get_overview, home, start, end, coverage_input)
    coverage = _merged_coverage(local, remote, windows, sync_meta, sync_error)
    coverage["timezone"] = _query_meta(home)
    data["sync"] = sync_meta
    return _finish("get_overview", data, coverage, sync_meta, sync_error)


async def _sync_now(home, args):
    _check_args(args, set())
    from ..sync_state import sync_now
    data = await asyncio.to_thread(sync_now, home)
    streams = data.get("streams") or {}
    for field in ("batch_limit", "discovered", "processed", "succeeded", "failed",
                  "queued_remaining", "discovery_complete", "high_water_ts",
                  "last_success_ts"):
        data[field] = {name: item.get(field) for name, item in streams.items()}
    data["item_errors"] = [
        dict(error, stream=name)
        for name, item in streams.items() for error in item.get("item_errors", [])
    ]
    complete = bool(data.get("complete"))
    coverage = data.get("coverage") or {}
    if streams:
        coverage = {
            "streams": {name: item.get("coverage") for name, item in streams.items()},
            "complete": complete,
            "stale": not complete,
            "sources": ["coros-web", "athlete.db", "sync.db"],
        }
    coverage.setdefault("complete", complete)
    coverage.setdefault("stale", not complete)
    data["coverage"] = coverage
    if not complete and not data.get("next_action"):
        data["next_action"] = "再次调用 sync_now，每路继续一批，直到覆盖与队列完成"
    return success("sync_now", data, {
        "coverage": coverage,
        "sync": data,
        "timezone": _query_meta(home),
        "warnings": data["item_errors"],
    })


async def _current_plan(home, args):
    _check_args(args, {"date"})
    config = load_config(home)
    date = args.get("date") or datetime.now(configured_tzinfo(config)).date().isoformat()
    _date(date)
    data, coverage = await asyncio.to_thread(queries.get_current_plan, home, date)
    coverage["timezone"] = _query_meta(home)
    return success("get_current_plan", data, {
        "coverage": coverage,
        "warnings": data.get("warnings", []),
        "timezone": coverage["timezone"],
    })


async def _read_memory(home, args):
    _check_args(args, {"name", "list"})
    if ("name" in args) == ("list" in args):
        raise _invalid("name 与 list 必须恰好提供一个。")
    from ..memory import list_memory, read_memory
    if "name" in args:
        name = _string(args, "name")
        data = await asyncio.to_thread(read_memory, home, name)
    else:
        list_name = _string(args, "list")
        data = await asyncio.to_thread(list_memory, home, list_name)
    return success("read_memory", data, {
        "coverage": {"sources": ["filesystem"], "complete": True, "stale": False},
        "timezone": _query_meta(home),
    })


async def _write_memory(home, args):
    _check_args(args, {"name", "content", "expected_sha256", "allow_create"})
    name = _string(args, "name")
    content = args.get("content")
    if not isinstance(content, str) or not content.strip():
        raise _invalid("content 必须是非空 Markdown 字符串。")
    allow_create = _boolean(args, "allow_create")
    expected = args.get("expected_sha256")
    if expected is not None and (not isinstance(expected, str) or
                                 len(expected) != 64 or
                                 any(c not in "0123456789abcdef" for c in expected)):
        raise _invalid("expected_sha256 必须是 64 位小写 hex。")
    from ..memory import read_memory, write_memory
    current = await asyncio.to_thread(read_memory, home, name)
    if current["exists"] and expected is None:
        raise _invalid("覆盖既有文件必须提供 expected_sha256；请先 read_memory 获取。")
    if not current["exists"] and not allow_create:
        raise _invalid("新建记忆文件必须显式提供 allow_create=true。")
    data = await asyncio.to_thread(
        write_memory, home, name, content, expected, allow_create)
    return success("write_memory", data, {
        "coverage": {"sources": ["filesystem"], "complete": True, "stale": False},
        "timezone": _query_meta(home),
    })


async def _create_workout(home, args):
    _check_args(args, {"workout", "confirm_token"})
    workout = args.get("workout")
    if not isinstance(workout, dict):
        raise _invalid("workout 必须是 object。")
    token = args.get("confirm_token")
    if token is not None and (not isinstance(token, str) or not token.strip()):
        raise _invalid("confirm_token 必须是非空字符串。")
    from ..workout import execute_workout, preview_workout
    if token is None:
        data = await asyncio.to_thread(preview_workout, home, workout)
    else:
        data = await execute_workout(home, workout, token)
    return success("create_workout", data, {
        "coverage": {"sources": [data.get("platform", "local")],
                     "complete": True, "stale": False},
        "timezone": _query_meta(home),
    })


_HANDLERS = {
    "get_overview": _overview_query,
    "query_activities": _activity_query,
    "get_training_load": _load_query,
    "get_recovery_status": _recovery_query,
    "sync_now": _sync_now,
    "get_current_plan": _current_plan,
    "read_memory": _read_memory,
    "write_memory": _write_memory,
    "create_workout": _create_workout,
}


async def call_tool(name, args, home):
    try:
        return await _HANDLERS[name](home, args)
    except ConfigError as exc:
        return failure(name, "SETUP_REQUIRED", str(exc),
                       next_action=exc.next_action,
                       meta={"timezone": _query_meta(home)})
    except (ValueError, TypeError) as exc:
        return failure(name, "INVALID_ARGUMENT", str(exc),
                       next_action="按工具 schema 修正参数后重试。",
                       meta={"timezone": _query_meta(home)})
    except sqlite3.OperationalError as exc:
        busy = "busy" in str(exc).lower() or "locked" in str(exc).lower()
        return failure(name, "DB_BUSY" if busy else "INTERNAL_ERROR", str(exc),
                       retryable=busy,
                       next_action="等待其他本地进程完成后重试。" if busy else "查看运行日志。",
                       meta={"timezone": _query_meta(home)})
    except Exception as exc:  # noqa: BLE001 模块业务错误统一适配
        if all(hasattr(exc, attr) for attr in ("code", "retryable", "next_action", "details")):
            return failure(name, exc.code, str(exc), retryable=exc.retryable,
                           next_action=exc.next_action, details=exc.details,
                           meta={"timezone": _query_meta(home)})
        raise
