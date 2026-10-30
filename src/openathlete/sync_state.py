"""COROS 读时同步、持久队列与连续覆盖状态机。

所有平台调用都由显式入口触发；初始化与覆盖查询不触网。活动分析库与同步状态库
分别提交：先 athlete.db 入库成功，再把 sync.db 的段/父队列标为完成。
"""
import hashlib
import json
import os
import sqlite3
import time
import uuid
from datetime import date, datetime, time as datetime_time, timedelta
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from . import config as config_module
from .adapters.coros import (
    ArchiveConflictError, CorosApiError, CorosClient, ProxyConfigError,
    archive_fit, unpack_fit_parts,
)
from .credentials import load_credentials
from .db.athlete_db import (
    DAILY_METRICS_FIELDS,
    connect as connect_athlete_db,
    decode_fit,
    init_db,
    ingest_fit_payload,
    rebuild_daily_load,
    upsert_daily_metrics,
)
from .home import athlete_db_path
from .locks import LockBusy, home_lock

SYNC_BATCH_SIZE = 20
SYNC_TTL_S = 6 * 3600
DISCOVERY_OVERLAP_S = 48 * 3600
DISCOVERY_PAGE_SIZE = 50
METRICS_INITIAL_DAYS = 28
MAX_ATTEMPTS = 5
RETRY_BASE_S = 60
RETRY_CAP_S = 1800
SYNC_LEASE_S = 300
SQLITE_TIMEOUT_S = 5.0
SQLITE_BUSY_TIMEOUT_MS = 5000
GLOBAL_RETRY_S = 60
ACTIVITY_STREAM = "coros-activities"
METRICS_STREAM = "coros-daily-metrics"
SYNC_STREAMS = (ACTIVITY_STREAM, METRICS_STREAM)
PLATFORM = "coros"

_DDL = """
CREATE TABLE IF NOT EXISTS oa_sync_state (
  stream TEXT PRIMARY KEY,
  high_water_ts INTEGER,
  coverage_start_date TEXT,
  coverage_end_date TEXT,
  discovery_complete INTEGER NOT NULL DEFAULT 0,
  last_attempt_ts INTEGER,
  last_success_ts INTEGER,
  next_retry_ts INTEGER,
  discovery_cursor_json TEXT,
  last_error_code TEXT,
  last_error_message TEXT
);
CREATE TABLE IF NOT EXISTS oa_sync_queue (
  platform TEXT NOT NULL,
  platform_activity_id TEXT NOT NULL,
  sport_type INTEGER,
  start_ts INTEGER NOT NULL,
  end_ts INTEGER,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  not_before_ts INTEGER,
  lease_owner TEXT,
  lease_until_ts INTEGER,
  discovered_ts INTEGER NOT NULL,
  updated_ts INTEGER NOT NULL,
  last_error_code TEXT,
  last_error_message TEXT,
  PRIMARY KEY (platform, platform_activity_id)
);
CREATE TABLE IF NOT EXISTS oa_sync_segment (
  platform TEXT NOT NULL,
  parent_activity_id TEXT NOT NULL,
  segment_index INTEGER NOT NULL,
  segment_activity_id TEXT NOT NULL,
  fit_path TEXT,
  fit_sha256 TEXT,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error_code TEXT,
  last_error_message TEXT,
  PRIMARY KEY (platform, parent_activity_id, segment_index),
  UNIQUE (platform, segment_activity_id)
);
CREATE INDEX IF NOT EXISTS oa_sync_queue_ready
  ON oa_sync_queue (status, not_before_ts, start_ts, platform_activity_id);
CREATE INDEX IF NOT EXISTS oa_sync_segment_ready
  ON oa_sync_segment (status, parent_activity_id);
"""

_STATE_FIELDS = {
    "high_water_ts", "coverage_start_date", "coverage_end_date",
    "discovery_complete", "last_attempt_ts", "last_success_ts", "next_retry_ts",
    "discovery_cursor_json", "last_error_code", "last_error_message",
}


class SyncError(Exception):
    """可映射为 MCP/CLI 结构化错误的同步异常。"""

    def __init__(self, code, message, retryable=False, next_action="", details=None):
        self.code = code
        self.message = message
        self.retryable = bool(retryable)
        self.next_action = next_action
        self.details = details or {}
        super().__init__(message)

    def to_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "next_action": self.next_action,
            "details": self.details,
        }


def _home(home):
    return Path(home).expanduser().resolve()


def sync_db_path(home):
    return _home(home) / "sync.db"


def connect_sync_db(home):
    path = sync_db_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=SQLITE_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout={}".format(int(SQLITE_BUSY_TIMEOUT_MS)))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _translate_errors(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if "busy" in str(exc).lower() or "locked" in str(exc).lower():
                raise SyncError(
                    "DB_BUSY", "同步数据库正被其他写入者占用", True,
                    "另一写入完成后重试；不要删除 sync.db 或 WAL 文件",
                ) from exc
            raise
    return wrapped


@_translate_errors
def init_sync_db(home):
    """只初始化本地 sync.db/athlete.db，不读取凭据、不发起网络请求。"""
    home = _home(home)
    home.mkdir(parents=True, exist_ok=True)
    conn = connect_sync_db(home)
    try:
        conn.executescript(_DDL)
        for stream in SYNC_STREAMS:
            conn.execute("INSERT OR IGNORE INTO oa_sync_state (stream) VALUES (?)", (stream,))
        conn.commit()
    finally:
        conn.close()
    athlete = connect_athlete_db(athlete_db_path(home))
    try:
        athlete.execute("PRAGMA busy_timeout={}".format(int(SQLITE_BUSY_TIMEOUT_MS)))
        init_db(athlete)
    finally:
        athlete.close()
    return sync_db_path(home)


def _begin(conn):
    conn.execute("BEGIN IMMEDIATE")


def _rollback(conn):
    try:
        conn.rollback()
    except sqlite3.Error:
        pass


def _state(conn, stream):
    row = conn.execute("SELECT * FROM oa_sync_state WHERE stream=?", (stream,)).fetchone()
    if row is None:
        raise SyncError("INTERNAL_ERROR", "同步状态行缺失：{}".format(stream))
    return dict(row)


def _set_state(conn, stream, **fields):
    unknown = set(fields) - _STATE_FIELDS
    if unknown:
        raise ValueError("未知同步状态字段：{}".format(sorted(unknown)))
    if not fields:
        return
    columns = ", ".join("{}=?".format(key) for key in fields)
    conn.execute(
        "UPDATE oa_sync_state SET {} WHERE stream=?".format(columns),
        list(fields.values()) + [stream],
    )


def _config(home):
    return config_module.load_config(home)


def _timezone_name(config):
    try:
        return str(config_module.configured_timezone(config))
    except config_module.ConfigError as exc:
        raise SyncError(
            "SETUP_REQUIRED", str(exc), False, exc.next_action,
        ) from exc


def _activity_since(config):
    value = config_module.activity_since(config)
    if not value:
        raise SyncError(
            "SETUP_REQUIRED", "尚未配置首次活动同步起点", False,
            "先执行 openathlete-mcp db config set --activity-since YYYY-MM-DD；"
            "不会默认抓取全历史",
        )
    return _date(value).isoformat()


def _zone(config):
    return ZoneInfo(_timezone_name(config))


def _now(now_ts):
    return int(time.time() if now_ts is None else now_ts)


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
        return date.fromisoformat(text)
    except ValueError as exc:
        raise SyncError("INVALID_ARGUMENT", "无效日期：{}".format(text), False,
                        "日期使用 YYYY-MM-DD，且必须是真实日期") from exc


def _window(start, end):
    start_date = _date(start) if start is not None else None
    end_date = _date(end) if end is not None else None
    if start_date and end_date and start_date > end_date:
        raise SyncError("INVALID_ARGUMENT", "开始日期晚于结束日期", False,
                        "调整 since/until 后重试")
    return start_date, end_date


def _day_epoch(day, zone):
    return int(datetime.combine(day, datetime_time.min, tzinfo=zone).timestamp())


def _date_window_epochs(start, end, zone):
    start_ts = _day_epoch(start, zone) if start is not None else None
    end_ts = _day_epoch(end + timedelta(days=1), zone) if end is not None else None
    return start_ts, end_ts


def _yyyymmdd(day):
    return day.strftime("%Y%m%d")


def _athlete_db(home):
    conn = connect_athlete_db(athlete_db_path(home))
    conn.execute("PRAGMA busy_timeout={}".format(int(SQLITE_BUSY_TIMEOUT_MS)))
    init_db(conn)
    return conn


def _db_busy_error():
    return SyncError("DB_BUSY", "分析数据库正被其他写入者占用", True,
                     "另一写入完成后重试", {"operation": "athlete.db"})


def _as_sync_error(exc, login=False):
    if isinstance(exc, SyncError):
        return exc
    if isinstance(exc, sqlite3.OperationalError) and (
            "busy" in str(exc).lower() or "locked" in str(exc).lower()):
        return _db_busy_error()
    if isinstance(exc, ArchiveConflictError):
        return SyncError("ARCHIVE_CONFLICT", str(exc), False,
                         "保留原 FIT，人工核查同路径冲突后再同步")
    if isinstance(exc, ProxyConfigError):
        return SyncError(
            "PROXY_CONFIG_INVALID", str(exc), False,
            "检查 MCP/CLI 进程的 HTTP_PROXY、HTTPS_PROXY、NO_PROXY；"
            "NO_PROXY 中的 IPv6 使用 ::1 等不带方括号的地址",
        )
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return SyncError("RATE_LIMITED", "COROS 请求被限流", True,
                             "尊重退避时间后重试")
        if status in (401, 403):
            return SyncError("AUTH_REQUIRED", "COROS web 认证已失效", False,
                             "重新检查 COROS web 凭据并登录")
        return SyncError("UPSTREAM_UNAVAILABLE", "COROS 返回 HTTP {}".format(status), True,
                         "稍后重试；持续失败时检查 Training Hub 状态")
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return SyncError("UPSTREAM_UNAVAILABLE", "COROS 网络请求失败", True,
                         "检查网络后重试")
    if isinstance(exc, CorosApiError):
        if login:
            return SyncError("AUTH_REQUIRED", "COROS web 登录失败", False,
                             "检查 COROS_EMAIL/COROS_PASSWORD 后重试")
        return SyncError("UPSTREAM_CHANGED", str(exc), False,
                         "检查非官方 web API 响应结构并修复 adapter")
    if isinstance(exc, ValueError):
        return SyncError("UPSTREAM_CHANGED", "FIT 或平台数据无法解析", False,
                         "保留原始 FIT 并检查解析器/平台响应")
    return SyncError("INTERNAL_ERROR", "同步发生未预期错误", False,
                     "查看本地日志后重试；不要把凭据或完整响应贴入日志")


def _default_client(home):
    load_credentials()
    values = {name: os.environ.get(name, "").strip()
              for name in ("COROS_EMAIL", "COROS_PASSWORD")}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise SyncError(
            "AUTH_REQUIRED", "缺少 COROS Training Hub web 凭据", False,
            "请自行在 OPENATHLETE_HOME/credentials 填写 COROS_EMAIL/COROS_PASSWORD，"
            "或使用环境变量后重试；不要在对话中贴密码",
            {"missing": missing, "purpose": "coros-web"},
        )
    return CorosClient(
        values["COROS_EMAIL"], values["COROS_PASSWORD"], home=home,
        timezone_name=_timezone_name(_config(home)),
    )


def _ensure_login(client):
    ensure = getattr(client, "ensure_login", None)
    if ensure is not None:
        try:
            ensure()
        except Exception as exc:
            raise _as_sync_error(exc, login=True) from exc


def _queue_counts(conn, start_ts=None, end_ts=None, parent_id=None):
    clauses = ["platform=?", "status!='done'"]
    params = [PLATFORM]
    if start_ts is not None:
        clauses.append("start_ts>=?")
        params.append(start_ts)
    if end_ts is not None:
        clauses.append("start_ts<?")
        params.append(end_ts)
    if parent_id is not None:
        clauses.append("platform_activity_id=?")
        params.append(parent_id)
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM oa_sync_queue WHERE {} "
        "GROUP BY status".format(" AND ".join(clauses)), params).fetchall()
    counts = {row["status"]: row["n"] for row in rows}
    counts["total"] = sum(counts.values())
    return counts


def _segment_remaining(conn, parent_id=None, start_ts=None, end_ts=None):
    if parent_id is not None:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM oa_sync_segment WHERE platform=? "
            "AND parent_activity_id=? AND status!='done'",
            (PLATFORM, parent_id)).fetchone()
    elif start_ts is not None or end_ts is not None:
        clauses = ["s.platform=?", "s.status!='done'"]
        params = [PLATFORM]
        if start_ts is not None:
            clauses.append("q.start_ts>=?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("q.start_ts<?")
            params.append(end_ts)
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM oa_sync_segment s JOIN oa_sync_queue q "
            "ON q.platform=s.platform AND q.platform_activity_id=s.parent_activity_id "
            "WHERE {}".format(" AND ".join(clauses)), params).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM oa_sync_segment WHERE platform=? AND status!='done'",
            (PLATFORM,)).fetchone()
    return row["n"]


def _coverage_row(conn, config, stream, start, end, now_ts):
    state = _state(conn, stream)
    requested_start, requested_end = _window(start, end)
    coverage_start = _date(state["coverage_start_date"]) if state["coverage_start_date"] else None
    coverage_end = _date(state["coverage_end_date"]) if state["coverage_end_date"] else None
    covered = coverage_start is not None and coverage_end is not None
    if requested_start is not None:
        covered = covered and coverage_start <= requested_start
    if requested_end is not None:
        covered = covered and coverage_end >= requested_end
    actual_start = actual_end = None
    if covered:
        actual_start = max(coverage_start, requested_start) if requested_start else coverage_start
        actual_end = min(coverage_end, requested_end) if requested_end else coverage_end
    elif coverage_start is not None:
        left = max(coverage_start, requested_start) if requested_start else coverage_start
        right = min(coverage_end, requested_end) if requested_end else coverage_end
        if left <= right:
            actual_start, actual_end = left, right
    queue_counts = {"total": 0}
    segment_remaining = 0
    if stream == ACTIVITY_STREAM:
        zone = _zone(config)
        start_ts, end_ts = _date_window_epochs(requested_start, requested_end, zone)
        queue_counts = _queue_counts(conn, start_ts, end_ts)
        segment_remaining = _segment_remaining(conn, start_ts=start_ts, end_ts=end_ts)
    discovery_complete = bool(state["discovery_complete"])
    backlog = queue_counts["total"] > 0 or segment_remaining > 0
    complete = covered and discovery_complete and not backlog
    last_success = state["last_success_ts"]
    ttl_fresh = last_success is not None and now_ts - last_success <= SYNC_TTL_S
    fresh = complete and ttl_fresh
    missing = []
    if not covered:
        missing.append("coverage")
    if not discovery_complete:
        missing.append("discovery")
    if queue_counts["total"]:
        missing.append("queue")
    if segment_remaining:
        missing.append("segments")
    if not ttl_fresh:
        missing.append("ttl")
    if state["next_retry_ts"] is not None and state["next_retry_ts"] > now_ts:
        missing.append("retry_backoff")
    command = None
    if not covered and requested_start is not None:
        gap_start = requested_start
        gap_end = requested_end
        if coverage_start is not None and requested_start < coverage_start:
            gap_end = coverage_start - timedelta(days=1)
        if gap_end is not None and gap_start <= gap_end:
            verb = "pull" if stream == ACTIVITY_STREAM else "metrics"
            command = "openathlete-mcp coros {} --since {} --until {}".format(
                verb, gap_start.isoformat(), gap_end.isoformat())
    if command:
        next_action = "覆盖不足，先小量 dry-run，再显式回补连续缺口：{}".format(command)
    elif backlog or not discovery_complete:
        next_action = "再次调用 sync_now，每个 stream 继续一批，直到队列与发现完成"
    elif not fresh:
        next_action = "调用 sync_now 刷新；读取可继续使用带 stale 标记的本地数据"
    else:
        next_action = ""
    return {
        "stream": stream,
        "requested_start": requested_start.isoformat() if requested_start else None,
        "requested_end": requested_end.isoformat() if requested_end else None,
        "actual_start": actual_start.isoformat() if actual_start else None,
        "actual_end": actual_end.isoformat() if actual_end else None,
        "coverage_start": coverage_start.isoformat() if coverage_start else None,
        "coverage_end": coverage_end.isoformat() if coverage_end else None,
        "coverage_start_date": coverage_start.isoformat() if coverage_start else None,
        "coverage_end_date": coverage_end.isoformat() if coverage_end else None,
        "covered": covered,
        "complete": complete,
        "fresh": fresh,
        "stale": not fresh,
        "discovery_complete": discovery_complete,
        "queued_remaining": queue_counts["total"],
        "queue_by_status": {key: value for key, value in queue_counts.items() if key != "total"},
        "segment_remaining": segment_remaining,
        "backlog": backlog,
        "high_water_ts": state["high_water_ts"],
        "last_attempt_ts": state["last_attempt_ts"],
        "last_success_ts": last_success,
        "last_sync_ts": last_success,
        "next_retry_ts": state["next_retry_ts"],
        "last_error_code": state["last_error_code"],
        "last_error_message": state["last_error_message"],
        "missing": missing,
        "backfill_hint": command,
        "next_action": next_action,
    }


@_translate_errors
def get_coverage(home, stream, start=None, end=None):
    """返回单 stream 的连续覆盖、相关 backlog 与 TTL 新鲜度，不触网。"""
    if stream not in SYNC_STREAMS:
        raise SyncError("INVALID_ARGUMENT", "未知同步 stream：{}".format(stream))
    home = _home(home)
    init_sync_db(home)
    config = _config(home)
    conn = connect_sync_db(home)
    try:
        return _coverage_row(conn, config, stream, start, end, _now(None))
    finally:
        conn.close()


def _extend_coverage(conn, stream, start, end):
    """仅在新区间连续且完整时扩展；不相交区间不制造覆盖假象。"""
    state = _state(conn, stream)
    old_start = _date(state["coverage_start_date"]) if state["coverage_start_date"] else None
    old_end = _date(state["coverage_end_date"]) if state["coverage_end_date"] else None
    if old_start is None:
        new_start, new_end = start, end
    elif end < old_start - timedelta(days=1) or start > old_end + timedelta(days=1):
        if end < old_start:
            gap = {"start": (end + timedelta(days=1)).isoformat(),
                   "end": (old_start - timedelta(days=1)).isoformat()}
        else:
            gap = {"start": (old_end + timedelta(days=1)).isoformat(),
                   "end": (start - timedelta(days=1)).isoformat()}
        return False, False, gap
    else:
        new_start, new_end = min(old_start, start), max(old_end, end)
    covered = new_start <= start and new_end >= end
    changed = new_start != old_start or new_end != old_end
    if changed:
        _set_state(conn, stream, coverage_start_date=new_start.isoformat(),
                   coverage_end_date=new_end.isoformat())
    return changed, covered, None


def _normalise_activity(raw):
    try:
        label_id = str(raw["labelId"])
        start_ts = int(raw["startTime"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CorosApiError("活动列表项缺少有效 labelId/startTime") from exc
    end_ts = raw.get("endTime")
    try:
        end_ts = int(end_ts) if end_ts is not None else None
        sport_type = int(raw.get("sportType")) if raw.get("sportType") is not None else None
    except (TypeError, ValueError) as exc:
        raise CorosApiError("活动列表项 sportType/endTime 无效") from exc
    return {
        "labelId": label_id,
        "sportType": sport_type,
        "startTime": start_ts,
        "endTime": end_ts,
    }


def _upsert_discovered(conn, activity, now_ts):
    conn.execute(
        """
        INSERT INTO oa_sync_queue (
          platform, platform_activity_id, sport_type, start_ts, end_ts, status,
          attempts, discovered_ts, updated_ts
        ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)
        ON CONFLICT(platform, platform_activity_id) DO UPDATE SET
          sport_type=excluded.sport_type,
          start_ts=excluded.start_ts,
          end_ts=excluded.end_ts,
          updated_ts=excluded.updated_ts
        """,
        (PLATFORM, activity["labelId"], activity["sportType"],
         activity["startTime"], activity["endTime"], now_ts, now_ts),
    )


def _desired_activity_cursor(conn, config, mode, now_ts, since=None, until=None):
    state = _state(conn, ACTIVITY_STREAM)
    zone = _zone(config)
    today = datetime.fromtimestamp(now_ts, zone).date()
    if mode == "backfill":
        start, end = _window(since, until)
        if start is None or end is None:
            raise SyncError("INVALID_ARGUMENT", "回补必须提供 since 和 until")
        filter_start, filter_end = _date_window_epochs(start, end, zone)
    else:
        configured_start = _date(_activity_since(config))
        filter_start = _day_epoch(configured_start, zone)
        if state["high_water_ts"] is not None:
            filter_start = max(filter_start, state["high_water_ts"] - DISCOVERY_OVERLAP_S)
        start = datetime.fromtimestamp(filter_start, zone).date()
        end = today
        filter_end = now_ts + 1
    return {
        "mode": mode,
        "start_day": _yyyymmdd(start),
        "end_day": _yyyymmdd(end),
        "coverage_start": start.isoformat(),
        "coverage_end": end.isoformat(),
        "filter_start_ts": int(filter_start),
        "filter_end_ts": int(filter_end),
        "page": 1,
        "size": DISCOVERY_PAGE_SIZE,
        "max_start_ts": int(state["high_water_ts"] or 0) if mode == "incremental" else 0,
    }


def _discover_activities(home, client, config, now_ts, mode="incremental",
                         since=None, until=None):
    """发现完整日期窗口；每页队列和下一页游标同事务提交。"""
    conn = connect_sync_db(home)
    discovered = set()
    try:
        state = _state(conn, ACTIVITY_STREAM)
        cursor_text = state["discovery_cursor_json"]
        if cursor_text:
            try:
                cursor = json.loads(cursor_text)
            except ValueError as exc:
                raise SyncError("INTERNAL_ERROR", "活动发现游标损坏", False,
                                "检查 sync.db 的 discovery_cursor_json") from exc
            if cursor.get("mode") != mode:
                raise SyncError(
                    "DISCOVERY_INCOMPLETE", "另一活动发现窗口尚未完成", True,
                    "先恢复原发现/回补窗口，避免覆盖其游标",
                    {"active_mode": cursor.get("mode"), "requested_mode": mode},
                )
            if mode == "backfill":
                expected = _desired_activity_cursor(conn, config, mode, now_ts, since, until)
                if (cursor.get("coverage_start") != expected["coverage_start"] or
                        cursor.get("coverage_end") != expected["coverage_end"]):
                    raise SyncError(
                        "DISCOVERY_INCOMPLETE", "既有回补窗口尚未完成", True,
                        "先用原 since/until 恢复回补",
                        {"since": cursor.get("coverage_start"),
                         "until": cursor.get("coverage_end")},
                    )
        else:
            cursor = _desired_activity_cursor(conn, config, mode, now_ts, since, until)
            _begin(conn)
            try:
                _set_state(conn, ACTIVITY_STREAM, last_attempt_ts=now_ts,
                           discovery_complete=0,
                           discovery_cursor_json=json.dumps(cursor, sort_keys=True))
                conn.commit()
            except Exception:
                _rollback(conn)
                raise
        while True:
            items, total = client.list_activities(
                cursor["start_day"], cursor["end_day"],
                page=cursor["page"], size=cursor["size"],
            )
            accepted = []
            max_start = int(cursor.get("max_start_ts") or 0)
            for raw in items:
                activity = _normalise_activity(raw)
                if (cursor["filter_start_ts"] <= activity["startTime"] <
                        cursor["filter_end_ts"]):
                    accepted.append(activity)
                    max_start = max(max_start, activity["startTime"])
            page = int(cursor["page"])
            size = int(cursor["size"])
            last_page = not items or len(items) < size or page * size >= int(total)
            _begin(conn)
            try:
                for activity in accepted:
                    _upsert_discovered(conn, activity, now_ts)
                    discovered.add(activity["labelId"])
                cursor["max_start_ts"] = max_start
                if last_page:
                    fields = {
                        "discovery_complete": 1,
                        "discovery_cursor_json": None,
                        "last_error_code": None,
                        "last_error_message": None,
                    }
                    if mode == "incremental":
                        high_water = max_start or now_ts
                        fields["high_water_ts"] = high_water
                        _extend_coverage(
                            conn, ACTIVITY_STREAM,
                            _date(cursor["coverage_start"]), _date(cursor["coverage_end"]),
                        )
                    _set_state(conn, ACTIVITY_STREAM, **fields)
                else:
                    cursor["page"] = page + 1
                    _set_state(conn, ACTIVITY_STREAM,
                               discovery_cursor_json=json.dumps(cursor, sort_keys=True))
                conn.commit()
            except Exception:
                _rollback(conn)
                raise
            if last_page:
                return len(discovered)
    finally:
        conn.close()


def _dry_run_activities(client, config, since, until, limit):
    cursor = _desired_activity_cursor_readonly(config, since, until)
    found = []
    complete = False
    while True:
        items, total = client.list_activities(
            cursor["start_day"], cursor["end_day"],
            page=cursor["page"], size=cursor["size"],
        )
        for raw in items:
            activity = _normalise_activity(raw)
            if cursor["filter_start_ts"] <= activity["startTime"] < cursor["filter_end_ts"]:
                found.append(activity)
                if limit is not None and len(found) >= limit:
                    return found, complete
        page = int(cursor["page"])
        if not items or len(items) < cursor["size"] or page * cursor["size"] >= int(total):
            complete = True
            return found, complete
        cursor["page"] = page + 1


def _desired_activity_cursor_readonly(config, since, until):
    start, end = _window(since, until)
    if start is None or end is None:
        raise SyncError("INVALID_ARGUMENT", "回补必须提供 since 和 until")
    zone = _zone(config)
    filter_start, filter_end = _date_window_epochs(start, end, zone)
    return {
        "start_day": _yyyymmdd(start), "end_day": _yyyymmdd(end),
        "filter_start_ts": filter_start, "filter_end_ts": filter_end,
        "page": 1, "size": DISCOVERY_PAGE_SIZE,
    }


def _recover_expired_leases(conn, now_ts):
    _begin(conn)
    try:
        expired = [row["platform_activity_id"] for row in conn.execute(
            "SELECT platform_activity_id FROM oa_sync_queue WHERE platform=? "
            "AND status='processing' AND lease_until_ts IS NOT NULL AND lease_until_ts<=?",
            (PLATFORM, now_ts))]
        if expired:
            conn.execute(
                "UPDATE oa_sync_queue SET status='retry', lease_owner=NULL, "
                "lease_until_ts=NULL, not_before_ts=NULL, last_error_code='LEASE_EXPIRED', "
                "last_error_message='处理租约已过期，恢复重试', updated_ts=? "
                "WHERE platform=? AND status='processing' AND lease_until_ts<=?",
                (now_ts, PLATFORM, now_ts))
            conn.execute(
                "UPDATE oa_sync_segment SET status='retry', "
                "last_error_code='LEASE_EXPIRED', last_error_message='父活动租约已过期' "
                "WHERE platform=? AND status='processing' AND EXISTS ("
                "SELECT 1 FROM oa_sync_queue q WHERE q.platform=oa_sync_segment.platform "
                "AND q.platform_activity_id=oa_sync_segment.parent_activity_id "
                "AND q.status='retry' AND q.last_error_code='LEASE_EXPIRED')",
                (PLATFORM,))
        conn.commit()
    except Exception:
        _rollback(conn)
        raise


def _lease_parents(conn, now_ts, limit, window=None):
    _recover_expired_leases(conn, now_ts)
    owner = uuid.uuid4().hex
    lease_until = now_ts + SYNC_LEASE_S
    leased = []
    _begin(conn)
    try:
        clauses = ["platform=?", "status IN ('pending','retry')",
                   "(not_before_ts IS NULL OR not_before_ts<=?)"]
        params = [PLATFORM, now_ts]
        if window is not None:
            clauses.extend(["start_ts>=?", "start_ts<?"])
            params.extend(window)
        params.append(limit)
        candidates = conn.execute(
            "SELECT * FROM oa_sync_queue WHERE {} ORDER BY start_ts, "
            "platform_activity_id LIMIT ?".format(" AND ".join(clauses)), params).fetchall()
        for candidate in candidates:
            pid = candidate["platform_activity_id"]
            changed = conn.execute(
                "UPDATE oa_sync_queue SET status='processing', attempts=attempts+1, "
                "not_before_ts=NULL, lease_owner=?, lease_until_ts=?, updated_ts=? "
                "WHERE platform=? AND platform_activity_id=? "
                "AND status IN ('pending','retry') "
                "AND (not_before_ts IS NULL OR not_before_ts<=?)",
                (owner, lease_until, now_ts, PLATFORM, pid, now_ts),
            ).rowcount
            if changed:
                row = dict(candidate)
                row["attempts"] += 1
                row["lease_owner"] = owner
                leased.append(row)
        conn.commit()
    except Exception:
        _rollback(conn)
        raise
    return owner, leased


def _segment_plans(payload):
    parts = unpack_fit_parts(payload)
    shared = len(parts) == 1
    plans = []
    if shared:
        sessions = decode_fit(parts[0]).get("sessions") or []
        if not sessions:
            raise ValueError("FIT 无 session")
        source_hash = hashlib.sha256(parts[0]).hexdigest()
        for index in range(1, len(sessions) + 1):
            segment_hash = source_hash
            if len(sessions) > 1:
                segment_hash = hashlib.sha256(
                    "{}#segment:{}".format(source_hash, index).encode("ascii")).hexdigest()
            plans.append({
                "index": index, "raw": parts[0], "path_index": 0,
                "source_sha256": source_hash, "segment_sha256": segment_hash,
                "shared": len(sessions) > 1,
            })
        return plans
    for index, raw in enumerate(parts, 1):
        sessions = decode_fit(raw).get("sessions") or []
        if len(sessions) != 1:
            raise CorosApiError("multisport 独立段 FIT 必须各含一个 session")
        digest = hashlib.sha256(raw).hexdigest()
        plans.append({
            "index": index, "raw": raw, "path_index": index - 1,
            "source_sha256": digest, "segment_sha256": digest,
        })
    return plans


def _register_segments(conn, parent_id, plans):
    expected = {plan["index"]: plan for plan in plans}
    _begin(conn)
    try:
        existing = conn.execute(
            "SELECT * FROM oa_sync_segment WHERE platform=? AND parent_activity_id=?",
            (PLATFORM, parent_id)).fetchall()
        if {row["segment_index"] for row in existing} != set(expected) and existing:
            raise SyncError("SEGMENT_CONFLICT", "活动段清单与已登记清单不一致", False,
                            "保留现有段与 FIT，人工核查平台段序号")
        for row in existing:
            plan = expected[row["segment_index"]]
            if row["fit_sha256"] and row["fit_sha256"] != plan["segment_sha256"]:
                raise SyncError("SEGMENT_CONFLICT", "活动段哈希发生变化", False,
                                "保留新旧证据，不得把不同段标为幂等成功",
                                {"segment": row["segment_activity_id"]})
        for plan in plans:
            segment_id = "{}#{}".format(parent_id, plan["index"])
            conn.execute(
                """
                INSERT INTO oa_sync_segment (
                  platform, parent_activity_id, segment_index, segment_activity_id,
                  fit_sha256, status, attempts
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0)
                ON CONFLICT(platform, parent_activity_id, segment_index) DO UPDATE SET
                  fit_sha256=excluded.fit_sha256
                """,
                (PLATFORM, parent_id, plan["index"], segment_id, plan["segment_sha256"]),
            )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        _rollback(conn)
        raise SyncError("SEGMENT_CONFLICT", "段键与其他父活动冲突", False,
                        "检查 parent#i 段序号和来源键") from exc
    except Exception:
        _rollback(conn)
        raise


def _save_segment_paths(conn, parent_id, plans, paths, home):
    if len(paths) != len({plan["path_index"] for plan in plans}):
        raise CorosApiError("归档段数量与下载段数量不一致")
    _begin(conn)
    try:
        for plan in plans:
            path = Path(paths[plan["path_index"]]).resolve()
            try:
                rel = path.relative_to(home).as_posix()
            except ValueError as exc:
                raise SyncError("ARCHIVE_CONFLICT", "FIT 归档路径超出 OPENATHLETE_HOME",
                                False, "检查归档路径") from exc
            conn.execute(
                "UPDATE oa_sync_segment SET fit_path=? WHERE platform=? "
                "AND parent_activity_id=? AND segment_index=?",
                (rel, PLATFORM, parent_id, plan["index"]),
            )
        conn.commit()
    except Exception:
        _rollback(conn)
        raise


def _mark_segment_processing(conn, parent_id, index):
    _begin(conn)
    try:
        conn.execute(
            "UPDATE oa_sync_segment SET status='processing', attempts=attempts+1, "
            "last_error_code=NULL, last_error_message=NULL WHERE platform=? "
            "AND parent_activity_id=? AND segment_index=? AND status!='done'",
            (PLATFORM, parent_id, index))
        conn.commit()
    except Exception:
        _rollback(conn)
        raise


def _mark_segment_done(conn, parent_id, plan, rel_path):
    _begin(conn)
    try:
        conn.execute(
            "UPDATE oa_sync_segment SET status='done', fit_path=?, fit_sha256=?, "
            "last_error_code=NULL, last_error_message=NULL WHERE platform=? "
            "AND parent_activity_id=? AND segment_index=? AND fit_sha256=?",
            (rel_path, plan["segment_sha256"], PLATFORM, parent_id,
             plan["index"], plan["segment_sha256"]),
        )
        conn.commit()
    except Exception:
        _rollback(conn)
        raise


def _ingest_segment(home, config, parent_id, plan, rel_path):
    source_id = parent_id if plan.get("single") else "{}#{}".format(
        parent_id, plan["index"])
    conn = _athlete_db(home)
    review = []
    try:
        existing = conn.execute(
            "SELECT fit_sha256 FROM activity_source WHERE platform=? "
            "AND platform_activity_id=?", (PLATFORM, source_id)).fetchone()
        if existing and existing["fit_sha256"] and (
                existing["fit_sha256"] != plan["source_sha256"]):
            raise SyncError("ARCHIVE_CONFLICT", "同一来源键已对应不同 FIT 字节", False,
                            "保留原归档和新证据，人工核查来源冲突",
                            {"segment": source_id})
        ingest_pid = parent_id if plan.get("shared") else source_id
        ingest_fit_payload(
            conn, plan["raw"], PLATFORM, ingest_pid, rel_path,
            archive_root=home,
            primary_platform_name=config_module.primary_platform(config),
            ride_min_km=config_module.ride_training_min_km(config),
            ride_confirmed=config_module.ride_threshold_confirmed(config),
            review=review,
        )
        conn.commit()
        return review
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


def _mark_parent_done(conn, parent_id, owner, now_ts):
    _begin(conn)
    try:
        remaining = _segment_remaining(conn, parent_id)
        expected = conn.execute(
            "SELECT COUNT(*) AS n FROM oa_sync_segment WHERE platform=? "
            "AND parent_activity_id=?", (PLATFORM, parent_id)).fetchone()["n"]
        if remaining or not expected:
            raise SyncError("SEGMENT_INCOMPLETE", "仍有活动段未完成", True,
                            "下一批只续未完成段")
        changed = conn.execute(
            "UPDATE oa_sync_queue SET status='done', lease_owner=NULL, "
            "lease_until_ts=NULL, not_before_ts=NULL, updated_ts=?, "
            "last_error_code=NULL, last_error_message=NULL WHERE platform=? "
            "AND platform_activity_id=? AND status='processing' AND lease_owner=?",
            (now_ts, PLATFORM, parent_id, owner),
        ).rowcount
        conn.commit()
        if not changed:
            raise SyncError("LEASE_EXPIRED", "活动处理租约已变化", True,
                            "下一批恢复租约后续传")
    except Exception:
        _rollback(conn)
        raise


def _mark_parent_failure(conn, parent_id, owner, attempts, error, now_ts, segment_index=None):
    permanent = attempts >= MAX_ATTEMPTS
    status = "failed" if permanent else "retry"
    delay = 0 if permanent else min(RETRY_BASE_S * (2 ** max(0, attempts - 1)), RETRY_CAP_S)
    _begin(conn)
    try:
        conn.execute(
            "UPDATE oa_sync_queue SET status=?, not_before_ts=?, lease_owner=NULL, "
            "lease_until_ts=NULL, updated_ts=?, last_error_code=?, last_error_message=? "
            "WHERE platform=? AND platform_activity_id=? AND status='processing' "
            "AND lease_owner=?",
            (status, None if permanent else now_ts + delay, now_ts,
             error.code, error.message, PLATFORM, parent_id, owner),
        )
        if segment_index is not None:
            conn.execute(
                "UPDATE oa_sync_segment SET status=?, last_error_code=?, "
                "last_error_message=? WHERE platform=? AND parent_activity_id=? "
                "AND segment_index=? AND status!='done'",
                (status, error.code, error.message, PLATFORM, parent_id, segment_index),
            )
        conn.commit()
    except Exception:
        _rollback(conn)
        raise
    return status


def _finalize_already_done_parent(conn, parent_id, owner, now_ts):
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM oa_sync_segment WHERE platform=? "
        "AND parent_activity_id=?", (PLATFORM, parent_id)).fetchone()["n"]
    if count and not _segment_remaining(conn, parent_id):
        _mark_parent_done(conn, parent_id, owner, now_ts)
        return True
    return False


def _process_parent(home, client, config, parent, owner, now_ts):
    parent_id = parent["platform_activity_id"]
    conn = connect_sync_db(home)
    current_segment = None
    ingested = 0
    reviews = []
    try:
        if _finalize_already_done_parent(conn, parent_id, owner, now_ts):
            return {"ingested": 0, "reviews": [], "error": None, "permanent": False}
        payload = client.download_fit(parent_id, parent["sport_type"])
        plans = _segment_plans(payload)
        for plan in plans:
            plan["single"] = len(plans) == 1
        _register_segments(conn, parent_id, plans)
        summary = {
            "labelId": parent_id, "sportType": parent["sport_type"],
            "startTime": parent["start_ts"], "endTime": parent.get("end_ts"),
        }
        paths = archive_fit(summary, payload, home=home,
                            timezone_name=_timezone_name(config))
        _save_segment_paths(conn, parent_id, plans, paths, home)
        for plan in plans:
            segment_id = "{}#{}".format(parent_id, plan["index"])
            row = conn.execute(
                "SELECT status, fit_sha256 FROM oa_sync_segment WHERE platform=? "
                "AND parent_activity_id=? AND segment_index=?",
                (PLATFORM, parent_id, plan["index"])).fetchone()
            if row["status"] == "done":
                if row["fit_sha256"] != plan["segment_sha256"]:
                    raise SyncError("SEGMENT_CONFLICT", "已完成段的哈希不一致", False,
                                    "人工核查段冲突", {"segment": segment_id})
                continue
            current_segment = plan["index"]
            _mark_segment_processing(conn, parent_id, current_segment)
            path = Path(paths[plan["path_index"]]).resolve()
            rel_path = path.relative_to(home).as_posix()
            reviews.extend(_ingest_segment(home, config, parent_id, plan, rel_path))
            ingested += 1
            _mark_segment_done(conn, parent_id, plan, rel_path)
            current_segment = None
        _mark_parent_done(conn, parent_id, owner, now_ts)
        return {"ingested": ingested, "reviews": reviews, "error": None, "permanent": False}
    except Exception as exc:
        error = _as_sync_error(exc)
        try:
            status = _mark_parent_failure(
                conn, parent_id, owner, parent["attempts"], error, now_ts, current_segment)
        except Exception:
            _rollback(conn)
            raise
        return {
            "ingested": ingested, "reviews": reviews,
            "error": {"code": error.code, "message": error.message,
                      "retryable": error.retryable, "next_action": error.next_action,
                      "platform_activity_id": parent_id,
                      "segment_index": current_segment,
                      "attempts": parent["attempts"]},
            "permanent": status == "failed",
        }
    finally:
        conn.close()


def _run_activity_batch(home, client, config, now_ts, limit=SYNC_BATCH_SIZE, window=None):
    conn = connect_sync_db(home)
    try:
        owner, parents = _lease_parents(conn, now_ts, limit, window)
    finally:
        conn.close()
    result = {
        "batch_limit": limit, "processed": len(parents), "succeeded": 0,
        "failed": 0, "permanent_failed": 0, "item_errors": [],
        "ingested": 0, "review": [],
    }
    for parent in parents:
        outcome = _process_parent(home, client, config, parent, owner, now_ts)
        result["ingested"] += outcome["ingested"]
        result["review"].extend(outcome["reviews"])
        if outcome["error"] is None:
            result["succeeded"] += 1
        else:
            result["failed"] += 1
            result["permanent_failed"] += int(outcome["permanent"])
            result["item_errors"].append(outcome["error"])
    if result["ingested"]:
        athlete = _athlete_db(home)
        try:
            today = datetime.fromtimestamp(now_ts, _zone(config)).date()
            rebuild_daily_load(athlete, today=today)
        finally:
            athlete.close()
    return result


def _touch_attempt(home, stream, now_ts):
    conn = connect_sync_db(home)
    try:
        _begin(conn)
        try:
            _set_state(conn, stream, last_attempt_ts=now_ts)
            conn.commit()
        except Exception:
            _rollback(conn)
            raise
    finally:
        conn.close()


def _finish_success(home, stream, now_ts):
    conn = connect_sync_db(home)
    try:
        _begin(conn)
        try:
            _set_state(conn, stream, last_success_ts=now_ts, next_retry_ts=None,
                       last_error_code=None, last_error_message=None)
            conn.commit()
        except Exception:
            _rollback(conn)
            raise
    finally:
        conn.close()


def _record_global_failure(home, stream, error, now_ts):
    conn = connect_sync_db(home)
    try:
        _begin(conn)
        try:
            _set_state(
                conn, stream, last_attempt_ts=now_ts,
                next_retry_ts=now_ts + GLOBAL_RETRY_S if error.retryable else None,
                last_error_code=error.code, last_error_message=error.message,
            )
            conn.commit()
        except Exception:
            _rollback(conn)
            raise
    finally:
        conn.close()


def _retry_backoff(home, stream, now_ts):
    conn = connect_sync_db(home)
    try:
        state = _state(conn, stream)
        if state["next_retry_ts"] is None or state["next_retry_ts"] <= now_ts:
            return None
        return state
    finally:
        conn.close()


def _activity_result(home, config, now_ts, discovered, batch, attempted, trigger,
                     blocked=False):
    conn = connect_sync_db(home)
    try:
        state = _state(conn, ACTIVITY_STREAM)
        counts = _queue_counts(conn)
        segments = _segment_remaining(conn)
        coverage = _coverage_row(conn, config, ACTIVITY_STREAM, None, None, now_ts)
    finally:
        conn.close()
    complete = coverage["complete"] and not blocked
    result = {
        "stream": ACTIVITY_STREAM,
        "trigger": trigger,
        "attempted": attempted,
        "batch_limit": batch.get("batch_limit", SYNC_BATCH_SIZE),
        "discovered": discovered,
        "processed": batch.get("processed", 0),
        "succeeded": batch.get("succeeded", 0),
        "failed": batch.get("failed", 0),
        "permanent_failed": batch.get("permanent_failed", 0),
        "ingested": batch.get("ingested", 0),
        "queued_remaining": counts["total"],
        "queue_by_status": {key: value for key, value in counts.items() if key != "total"},
        "segment_remaining": segments,
        "discovery_complete": bool(state["discovery_complete"]),
        "coverage": coverage,
        "high_water_ts": state["high_water_ts"],
        "last_attempt_ts": state["last_attempt_ts"],
        "last_success_ts": state["last_success_ts"],
        "next_retry_ts": state["next_retry_ts"],
        "complete": complete,
        "stale": not complete,
        "partial": batch.get("failed", 0) > 0,
        "item_errors": batch.get("item_errors", []),
        "review": batch.get("review", []),
    }
    if blocked:
        result["error"] = {
            "code": state["last_error_code"], "message": state["last_error_message"],
            "retryable": True, "next_action": "退避到期后重试",
        }
        result["next_action"] = "退避到期后重试，不在 TTL 内紧密轮询"
    elif not complete:
        result["next_action"] = "再次调用 sync_now；每路每次只处理一批"
    else:
        result["next_action"] = ""
    return result


def _metrics_result(home, config, now_ts, discovered, processed, attempted, trigger,
                    blocked=False):
    conn = connect_sync_db(home)
    try:
        state = _state(conn, METRICS_STREAM)
        coverage = _coverage_row(conn, config, METRICS_STREAM, None, None, now_ts)
    finally:
        conn.close()
    complete = coverage["complete"] and not blocked
    result = {
        "stream": METRICS_STREAM,
        "trigger": trigger,
        "attempted": attempted,
        "discovered": discovered,
        "processed": processed,
        "succeeded": processed,
        "failed": 0,
        "queued_remaining": 0,
        "discovery_complete": bool(state["discovery_complete"]),
        "coverage": coverage,
        "high_water_ts": state["high_water_ts"],
        "last_attempt_ts": state["last_attempt_ts"],
        "last_success_ts": state["last_success_ts"],
        "next_retry_ts": state["next_retry_ts"],
        "complete": complete,
        "stale": not complete,
        "partial": False,
        "item_errors": [],
    }
    if blocked:
        result["error"] = {
            "code": state["last_error_code"], "message": state["last_error_message"],
            "retryable": True, "next_action": "退避到期后重试",
        }
        result["next_action"] = "退避到期后重试"
    else:
        result["next_action"] = coverage["next_action"]
    return result


def _sync_activity_stream(home, client_factory, config, now_ts, trigger):
    if _retry_backoff(home, ACTIVITY_STREAM, now_ts) is not None:
        return _activity_result(home, config, now_ts, 0, {}, False, trigger, blocked=True)
    try:
        _touch_attempt(home, ACTIVITY_STREAM, now_ts)
        client = client_factory()
        _ensure_login(client)
        discovered = _discover_activities(home, client, config, now_ts)
        batch = _run_activity_batch(home, client, config, now_ts)
        _finish_success(home, ACTIVITY_STREAM, now_ts)
        return _activity_result(home, config, now_ts, discovered, batch, True, trigger)
    except Exception as exc:
        error = _as_sync_error(exc)
        _record_global_failure(home, ACTIVITY_STREAM, error, now_ts)
        raise error from exc


def _metric_rows(records, start, end):
    rows = []
    for record in records:
        day = _date(record["date"])
        if not start <= day <= end:
            continue
        row = {"date": day.isoformat(), "source": "coros"}
        for field in DAILY_METRICS_FIELDS:
            row[field] = record.get(field)
        rows.append(row)
    return rows


def _metrics_window(conn, config, now_ts):
    state = _state(conn, METRICS_STREAM)
    if state["discovery_cursor_json"]:
        cursor = json.loads(state["discovery_cursor_json"])
        if cursor.get("mode") != "incremental":
            raise SyncError("DISCOVERY_INCOMPLETE", "另一指标窗口尚未完成", True,
                            "先恢复原指标回补窗口")
        return cursor
    today = datetime.fromtimestamp(now_ts, _zone(config)).date()
    if state["coverage_end_date"]:
        start = _date(state["coverage_end_date"])
    else:
        start = today - timedelta(days=METRICS_INITIAL_DAYS - 1)
    return {
        "mode": "incremental", "coverage_start": start.isoformat(),
        "coverage_end": today.isoformat(), "start_day": _yyyymmdd(start),
        "end_day": _yyyymmdd(today),
    }


def _store_metrics_window(home, cursor, records, now_ts, extend_coverage=True):
    start, end = _date(cursor["coverage_start"]), _date(cursor["coverage_end"])
    rows = _metric_rows(records, start, end)
    athlete = _athlete_db(home)
    try:
        upsert_daily_metrics(athlete, rows)
    except Exception:
        _rollback(athlete)
        raise
    finally:
        athlete.close()
    conn = connect_sync_db(home)
    try:
        _begin(conn)
        try:
            if extend_coverage:
                _extend_coverage(conn, METRICS_STREAM, start, end)
            _set_state(conn, METRICS_STREAM, discovery_complete=1,
                       discovery_cursor_json=None, last_success_ts=now_ts,
                       next_retry_ts=None, last_error_code=None, last_error_message=None)
            conn.commit()
        except Exception:
            _rollback(conn)
            raise
    finally:
        conn.close()
    return len(rows)


def _sync_metrics_stream(home, client_factory, config, now_ts, trigger):
    if _retry_backoff(home, METRICS_STREAM, now_ts) is not None:
        return _metrics_result(home, config, now_ts, 0, 0, False, trigger, blocked=True)
    try:
        _touch_attempt(home, METRICS_STREAM, now_ts)
        client = client_factory()
        _ensure_login(client)
        conn = connect_sync_db(home)
        try:
            cursor = _metrics_window(conn, config, now_ts)
            if not _state(conn, METRICS_STREAM)["discovery_cursor_json"]:
                _begin(conn)
                try:
                    _set_state(conn, METRICS_STREAM, discovery_complete=0,
                               discovery_cursor_json=json.dumps(cursor, sort_keys=True))
                    conn.commit()
                except Exception:
                    _rollback(conn)
                    raise
        finally:
            conn.close()
        records = client.fetch_daily_records(cursor["start_day"], cursor["end_day"])
        processed = _store_metrics_window(home, cursor, records, now_ts)
        return _metrics_result(home, config, now_ts, len(records), processed, True, trigger)
    except Exception as exc:
        error = _as_sync_error(exc)
        _record_global_failure(home, METRICS_STREAM, error, now_ts)
        raise error from exc


def _client_factory(home, client):
    holder = {"client": client}
    def factory():
        if holder["client"] is None:
            holder["client"] = _default_client(home)
        return holder["client"]
    return factory


def _streams(streams):
    result = []
    for stream in streams:
        if stream not in SYNC_STREAMS:
            raise SyncError("INVALID_ARGUMENT", "未知同步 stream：{}".format(stream))
        if stream not in result:
            result.append(stream)
    return result


@_translate_errors
def sync_now(home, streams=SYNC_STREAMS, trigger="manual", client=None, now_ts=None):
    """每个指定 stream 最多发现一次并处理一个批次，不在函数内自动循环。"""
    home = _home(home)
    init_sync_db(home)
    selected = _streams(streams)
    now_ts = _now(now_ts)
    config = _config(home)
    factory = _client_factory(home, client)
    try:
        with home_lock(home, "sync"):
            results = {}
            for stream in selected:
                if stream == ACTIVITY_STREAM:
                    results[stream] = _sync_activity_stream(
                        home, factory, config, now_ts, trigger)
                else:
                    results[stream] = _sync_metrics_stream(
                        home, factory, config, now_ts, trigger)
    except LockBusy as exc:
        raise SyncError("SYNC_BUSY", "同一 HOME 已有同步在进行", True,
                        "等待另一同步完成后重试；不要删除锁文件") from exc
    complete = all(item["complete"] for item in results.values())
    return {
        "trigger": trigger,
        "streams": results,
        "complete": complete,
        "stale": not complete,
        "partial": any(item.get("partial", False) for item in results.values()),
    }


def _requested_window(spec):
    if spec is None:
        return None, None
    if isinstance(spec, dict):
        start = spec.get("start", spec.get("start_date"))
        end = spec.get("end", spec.get("end_date"))
        if "since" in spec:
            start = spec["since"]
        if "until" in spec:
            end = spec["until"]
        return _window(start, end)
    if isinstance(spec, (tuple, list)) and len(spec) == 2:
        return _window(spec[0], spec[1])
    raise SyncError("INVALID_ARGUMENT", "stream window 必须是 (start, end) 或日期对象")


@_translate_errors
def ensure_fresh(home, stream_windows: dict, client=None, now_ts=None):
    """供查询使用的读时同步；返回 (sync_meta, coverage)，不自动多批循环。"""
    home = _home(home)
    init_sync_db(home)
    now_ts = _now(now_ts)
    config = _config(home)
    windows = {stream: _requested_window(spec) for stream, spec in stream_windows.items()}
    _streams(windows)
    factory = _client_factory(home, client)
    try:
        with home_lock(home, "sync"):
            conn = connect_sync_db(home)
            try:
                before = {
                    stream: _coverage_row(conn, config, stream, start, end, now_ts)
                    for stream, (start, end) in windows.items()
                }
            finally:
                conn.close()
            summaries = {}
            wanted = False
            attempted = False
            for stream, coverage in before.items():
                if coverage["fresh"]:
                    summaries[stream] = {
                        "stream": stream, "trigger": "none", "attempted": False,
                        "batch_limit": SYNC_BATCH_SIZE if stream == ACTIVITY_STREAM else None,
                        "discovered": 0, "processed": 0, "succeeded": 0, "failed": 0,
                        "permanent_failed": 0, "ingested": 0,
                        "complete": coverage["complete"], "stale": coverage["stale"],
                        "queued_remaining": coverage["queued_remaining"],
                        "segment_remaining": coverage["segment_remaining"],
                        "discovery_complete": coverage["discovery_complete"],
                        "high_water_ts": coverage["high_water_ts"],
                        "last_attempt_ts": coverage["last_attempt_ts"],
                        "last_success_ts": coverage["last_success_ts"],
                        "next_retry_ts": coverage["next_retry_ts"],
                        "coverage": coverage, "partial": False, "item_errors": [],
                        "review": [], "next_action": "",
                    }
                    continue
                wanted = True
                if stream == ACTIVITY_STREAM:
                    summary = _sync_activity_stream(home, factory, config, now_ts, "ttl")
                else:
                    summary = _sync_metrics_stream(home, factory, config, now_ts, "ttl")
                attempted = attempted or summary["attempted"]
                summaries[stream] = summary
            conn = connect_sync_db(home)
            try:
                after = {
                    stream: _coverage_row(conn, config, stream, start, end, now_ts)
                    for stream, (start, end) in windows.items()
                }
            finally:
                conn.close()
    except LockBusy as exc:
        raise SyncError("SYNC_BUSY", "同一 HOME 已有同步在进行", True,
                        "等待另一同步完成后重试；不要删除锁文件") from exc
    return {
        "trigger": "ttl" if wanted else "none",
        "attempted": attempted,
        "streams": summaries,
        "complete": all(item["complete"] for item in after.values()),
        "stale": any(item["stale"] for item in after.values()),
    }, after


def _limit(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SyncError("INVALID_ARGUMENT", "limit 必须是正整数")
    return value


def _backfill_activity_result(home, config, now_ts, since, until, discovered, batch,
                              dry_run, limit, coverage_extended=False, coverage_gap=None,
                              discovery_complete=True, activities=None):
    start, end = _window(since, until)
    start_ts, end_ts = _date_window_epochs(start, end, _zone(config))
    conn = connect_sync_db(home)
    try:
        counts = _queue_counts(conn, start_ts, end_ts)
        state = _state(conn, ACTIVITY_STREAM)
        coverage = _coverage_row(conn, config, ACTIVITY_STREAM, start, end, now_ts)
    finally:
        conn.close()
    eligible = not dry_run and limit is None
    complete = eligible and discovery_complete and counts["total"] == 0 and coverage["covered"]
    result = {
        "stream": ACTIVITY_STREAM, "trigger": "backfill", "dry_run": dry_run,
        "since": start.isoformat(), "until": end.isoformat(),
        "batch_limit": SYNC_BATCH_SIZE, "discovered": discovered,
        "processed": batch.get("processed", 0), "succeeded": batch.get("succeeded", 0),
        "failed": batch.get("failed", 0),
        "permanent_failed": batch.get("permanent_failed", 0),
        "ingested": batch.get("ingested", 0),
        "queued_remaining": counts["total"], "discovery_complete": discovery_complete,
        "coverage_extended": coverage_extended, "coverage_gap": coverage_gap,
        "coverage": coverage, "high_water_ts": state["high_water_ts"],
        "last_success_ts": state["last_success_ts"], "complete": complete,
        "stale": not complete, "partial": batch.get("failed", 0) > 0 or counts["total"] > 0,
        "item_errors": batch.get("item_errors", []), "review": batch.get("review", []),
    }
    if activities is not None:
        result["activities"] = activities
    if dry_run:
        result["next_action"] = "确认后不带 dry-run 执行正式回补"
    elif limit is not None:
        result["next_action"] = "limit 不扩展 coverage；完整回补请不带 limit"
    elif counts["total"]:
        result["next_action"] = "继续同一窗口回补，直到所有父活动和段入库"
    elif coverage_gap:
        result["next_action"] = "先补齐中间缺口：{}".format(coverage_gap)
    else:
        result["next_action"] = coverage["next_action"]
    return result


@_translate_errors
def backfill_activities(home, since, until, dry_run=False, limit=None, client=None,
                        now_ts=None):
    """显式活动回补；只有非 dry-run、无 limit 且全窗口入库才扩连续覆盖。"""
    home = _home(home)
    init_sync_db(home)
    start, end = _window(since, until)
    if start is None or end is None:
        raise SyncError("INVALID_ARGUMENT", "since/until 均必填")
    since, until = start.isoformat(), end.isoformat()
    limit = _limit(limit)
    now_ts = _now(now_ts)
    config = _config(home)
    factory = _client_factory(home, client)
    try:
        with home_lock(home, "sync"):
            if dry_run:
                try:
                    client_obj = factory()
                    _ensure_login(client_obj)
                    activities, discovered_complete = _dry_run_activities(
                        client_obj, config, since, until, limit)
                    return _backfill_activity_result(
                        home, config, now_ts, since, until, len(activities), {}, True, limit,
                        discovery_complete=discovered_complete, activities=activities)
                except Exception as exc:
                    raise _as_sync_error(exc) from exc
            try:
                _touch_attempt(home, ACTIVITY_STREAM, now_ts)
                client_obj = factory()
                _ensure_login(client_obj)
                discovered = _discover_activities(
                    home, client_obj, config, now_ts, mode="backfill", since=since, until=until)
                total = {"processed": 0, "succeeded": 0, "failed": 0,
                         "permanent_failed": 0, "ingested": 0, "item_errors": [], "review": []}
                window = _date_window_epochs(start, end, _zone(config))
                remaining_limit = limit
                while remaining_limit is None or remaining_limit > 0:
                    batch_limit = SYNC_BATCH_SIZE if remaining_limit is None else min(
                        SYNC_BATCH_SIZE, remaining_limit)
                    batch = _run_activity_batch(
                        home, client_obj, config, now_ts, limit=batch_limit, window=window)
                    for key in ("processed", "succeeded", "failed", "permanent_failed", "ingested"):
                        total[key] += batch[key]
                    total["item_errors"].extend(batch["item_errors"])
                    total["review"].extend(batch["review"])
                    if remaining_limit is not None:
                        remaining_limit -= batch["processed"]
                    if batch["processed"] == 0 or batch["processed"] < batch_limit:
                        break
                _finish_success(home, ACTIVITY_STREAM, now_ts)
                coverage_extended = False
                coverage_gap = None
                if limit is None:
                    conn = connect_sync_db(home)
                    try:
                        _begin(conn)
                        try:
                            if _queue_counts(conn, *window)["total"] == 0:
                                coverage_extended, _covered, coverage_gap = _extend_coverage(
                                    conn, ACTIVITY_STREAM, start, end)
                            conn.commit()
                        except Exception:
                            _rollback(conn)
                            raise
                    finally:
                        conn.close()
                return _backfill_activity_result(
                    home, config, now_ts, since, until, discovered, total, False, limit,
                    coverage_extended, coverage_gap)
            except Exception as exc:
                error = _as_sync_error(exc)
                _record_global_failure(home, ACTIVITY_STREAM, error, now_ts)
                raise error from exc
    except LockBusy as exc:
        raise SyncError("SYNC_BUSY", "同一 HOME 已有同步在进行", True,
                        "等待另一同步完成后重试") from exc


def _store_metrics_backfill(home, cursor, records, now_ts, extend):
    start, end = _date(cursor["coverage_start"]), _date(cursor["coverage_end"])
    rows = _metric_rows(records, start, end)
    athlete = _athlete_db(home)
    try:
        upsert_daily_metrics(athlete, rows)
    except Exception:
        _rollback(athlete)
        raise
    finally:
        athlete.close()
    extended = covered = False
    gap = None
    conn = connect_sync_db(home)
    try:
        _begin(conn)
        try:
            if extend:
                extended, covered, gap = _extend_coverage(conn, METRICS_STREAM, start, end)
            fields = {"last_success_ts": now_ts, "next_retry_ts": None,
                      "last_error_code": None, "last_error_message": None}
            if extend and not _state(conn, METRICS_STREAM)["discovery_cursor_json"]:
                fields["discovery_complete"] = 1
            _set_state(conn, METRICS_STREAM, **fields)
            conn.commit()
        except Exception:
            _rollback(conn)
            raise
    finally:
        conn.close()
    return len(rows), extended, covered, gap


def _backfill_metrics_result(home, config, now_ts, since, until, discovered, processed,
                             dry_run, limit, extended=False, gap=None, records=None):
    conn = connect_sync_db(home)
    try:
        coverage = _coverage_row(conn, config, METRICS_STREAM, since, until, now_ts)
        state = _state(conn, METRICS_STREAM)
    finally:
        conn.close()
    complete = not dry_run and limit is None and coverage["covered"]
    result = {
        "stream": METRICS_STREAM, "trigger": "backfill", "dry_run": dry_run,
        "since": _date(since).isoformat(), "until": _date(until).isoformat(),
        "discovered": discovered, "processed": processed, "succeeded": processed,
        "failed": 0, "coverage_extended": extended, "coverage_gap": gap,
        "coverage": coverage, "last_success_ts": state["last_success_ts"],
        "complete": complete, "stale": not complete, "partial": limit is not None,
    }
    if records is not None:
        result["records"] = records
    if dry_run:
        result["next_action"] = "确认后不带 dry-run 执行正式回补"
    elif limit is not None:
        result["next_action"] = "limit 不扩展 coverage；完整回补请不带 limit"
    elif gap:
        result["next_action"] = "先补齐中间缺口：{}".format(gap)
    else:
        result["next_action"] = coverage["next_action"]
    return result


@_translate_errors
def backfill_metrics(home, since, until, dry_run=False, limit=None, client=None, now_ts=None):
    """显式日度指标回补，复用非空合并入库与连续覆盖规则。"""
    home = _home(home)
    init_sync_db(home)
    start, end = _window(since, until)
    if start is None or end is None:
        raise SyncError("INVALID_ARGUMENT", "since/until 均必填")
    limit = _limit(limit)
    now_ts = _now(now_ts)
    config = _config(home)
    factory = _client_factory(home, client)
    cursor = {"coverage_start": start.isoformat(), "coverage_end": end.isoformat(),
              "start_day": _yyyymmdd(start), "end_day": _yyyymmdd(end)}
    try:
        with home_lock(home, "sync"):
            if not dry_run:
                _touch_attempt(home, METRICS_STREAM, now_ts)
            try:
                client_obj = factory()
                _ensure_login(client_obj)
                records = client_obj.fetch_daily_records(cursor["start_day"], cursor["end_day"])
                selected = records[:limit] if limit is not None else records
                if dry_run:
                    return _backfill_metrics_result(
                        home, config, now_ts, start, end, len(records), 0,
                        True, limit, records=selected)
                processed, extended, _covered, gap = _store_metrics_backfill(
                    home, cursor, selected, now_ts, extend=limit is None)
                return _backfill_metrics_result(
                    home, config, now_ts, start, end, len(records), processed,
                    False, limit, extended, gap)
            except Exception as exc:
                error = _as_sync_error(exc)
                if not dry_run:
                    _record_global_failure(home, METRICS_STREAM, error, now_ts)
                raise error from exc
    except LockBusy as exc:
        raise SyncError("SYNC_BUSY", "同一 HOME 已有同步在进行", True,
                        "等待另一同步完成后重试") from exc
