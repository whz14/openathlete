"""athlete.db v0：SQLite 分析库——FIT 归档入库 + 跑/骑课型结构解析 + TRIMP/强度分布。

本模块是 `openathlete.db.athlete_db`（包内实现）；仓库根 `athlete_db.py` 是兼容 shim
（PEP 723，`uv run ./athlete_db.py ...` 行为不变）。包安装后也可走统一 CLI：
`openathlete-mcp db init|ingest|anchor|config|stats|rebuild-load`。

  uv run ./athlete_db.py init                 # 建库建表（WAL）
  uv run ./athlete_db.py ingest [--limit N] [--rebuild]   # 递归扫描规范 .fit 归档入库
  uv run ./athlete_db.py anchor set --sport run --valid-from 2026-01-01 [--lt1-hr ...]
  uv run ./athlete_db.py anchor list
  uv run ./athlete_db.py config set [--primary-platform garmin] [--ride-training-min-km 15]
  uv run ./athlete_db.py stats                # 按运动类型/结构类型/来源计数 + 未入库清单
  uv run ./athlete_db.py rebuild-load         # 由 activity.trimp 全量重算 PMC

--rebuild 只重建 FIT 派生表，保留手动锚点与平台健康数据（锚点补全后靠它全量重算）。
multisport 按 session 拆分入库（每段一行，platform_activity_id 加段序号后缀）。
游泳/越野/其他类型不做结构解析（DESIGN.md §3 跨类型分派），structure_json 置 NULL。
跨来源去重走活动同一性（开始时间 ±5 分钟 + 段序号）；canonical 按 FIT 哈希与主平台确定性选择。
"""
import argparse
import bisect
import hashlib
import io
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from ..config import (
    DEFAULT_RIDE_TRAINING_MIN_KM, configured_tzinfo, load_config, primary_platform,
    ride_threshold_confirmed, ride_training_min_km, set_config_values,
)
from ..home import athlete_db_path as _db_path, fit_dir as _fit_dir, openathlete_home
from ..structure import (
    Lap, PARSER_VERSION, ride_is_training, summarize, summarize_commute_ride,
    summarize_ride, validate_structure_dict,
)

OPENATHLETE_HOME = openathlete_home()
DB_PATH = _db_path(OPENATHLETE_HOME)
FIT_DIR = _fit_dir(OPENATHLETE_HOME)

# Edwards TRIMP：5 区 %HRR 权重 1-5（Edwards 1993；Karvonen %HRR 分区）
EDWARDS_HRR_BANDS = (0.50, 0.60, 0.70, 0.80, 0.90)
# 7 细区间边界推导：Z3/4、Z4/5、Z5/6、Z6/7 取 0.94/1.00/1.03/1.06×LT2
# （Coggan 七区 %LTHR 分带）；Z1/2 取 0.90×LT1（恢复/耐力由实测 LT1 锚定）
ZONE1_LT1_PCT = 0.90
ZONE_LT2_PCTS = (0.94, 1.00, 1.03, 1.06)
# 记录间隔超该值按停表处理，超出时长不计入负荷与分布
MAX_RECORD_GAP_S = 30
# 时区偏移对齐粒度（15 分钟，全球时区最小单位）
TZ_GRANULARITY_S = 900
# FIT 时间戳以 1989-12-31 UTC 为零点（Garmin epoch），与 Unix epoch 的差值；
# activity 消息的 local_timestamp 是原始 uint32，换算时区偏移需补回该差值
FIT_EPOCH_OFFSET_S = 631065600

# 运动类型归一（DESIGN.md §3：run/ride/swim 归一词汇 + track/trail/indoor 子类型）
_SUB_MAP = {
    "running": ("run", {"track": "track", "trail": "trail", "treadmill": "indoor",
                        "indoor_running": "indoor", "generic": None}),
    "cycling": ("ride", {"indoor_cycling": "indoor", "mountain": "mtb",
                         "gravel": "gravel", "road": "road", "generic": None}),
    "swimming": ("swim", {"lap_swimming": "pool", "open_water": "open_water",
                          "generic": None}),
}
# 做圈级结构解析的跑步子类型；越野配速失真、其余类型无解析器（DESIGN 跨类型分派）
STRUCTURE_RUN_SUBS = {None, "track", "indoor"}
# HR 系指标的跨类型锚点回退：跑步与骑行互为近似（LT 心率接近），游泳不回退（DESIGN §3）
HR_ANCHOR_FALLBACK = {"run": ("run", "ride"), "ride": ("ride", "run")}

DDL = """
create table if not exists activity (
  id integer primary key,
  start_ts integer, tz_offset_s integer,
  sport text, sub_sport text,
  duration_s real, distance_m real, ascent_m real,
  avg_hr real, max_hr real, avg_power_w real, avg_cadence_per_min real, calories real,
  avg_speed_mps real,
  trimp real,
  structure_json text,
  type text, rep_count integer, rep_dist_modal_m real, rep_dur_modal_s real,
  rep_pace_mean_s real, interval_dist_total_m real,
  zone_boundaries_json text,
  raw_meta_json text,
  parser_version text
);
create table if not exists activity_source (
  id integer primary key,
  activity_id integer references activity(id),
  platform text, platform_activity_id text, fit_path text, fit_sha256 text,
  is_canonical integer, note text,
  unique(platform, platform_activity_id)
);
create unique index if not exists idx_activity_source_one_canonical
  on activity_source(activity_id) where is_canonical = 1;
create table if not exists time_in_zone (
  activity_id integer references activity(id),
  zone integer, time_s real,
  primary key (activity_id, zone)
);
create table if not exists threshold_anchor (
  id integer primary key,
  sport text, valid_from text, valid_to text,
  lt1_hr real, lt2_hr real, max_hr real, resting_hr real,
  threshold_pace_s real, ftp_w real, critical_speed_mps real,
  source text, note text
);
create table if not exists daily_metrics (
  date text, source text,
  resting_hr real, hrv_ms real,
  sleep_total_s real, sleep_deep_s real, sleep_light_s real, sleep_rem_s real,
  sleep_awake_s real, sleep_score real,
  spo2_pct real, stress_avg real, body_battery real,
  steps real, calories real,
  primary key (date, source)
);
create table if not exists daily_load (
  date text primary key,
  trimp real, ctl real, atl real, tsb real
);
"""

# --rebuild 只删 FIT 可重算的派生表；threshold_anchor 是用户输入、daily_metrics
# 是平台拉取，均不可由 FIT 归档重建，必须保留
_DROP_TABLES = ("time_in_zone", "activity_source", "activity", "daily_load")


def connect(db_path):
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma busy_timeout=5000")
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma foreign_keys=on")
    return conn


SCHEMA_VERSION = 1
_FIT_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _has_table(conn, name):
    return conn.execute(
        "select 1 from sqlite_master where type = 'table' and name = ?", (name,)).fetchone() \
        is not None


def _apply_schema(conn):
    for statement in DDL.split(";"):
        if statement.strip():
            conn.execute(statement)
    source_columns = {r["name"] for r in conn.execute("pragma table_info(activity_source)")}
    if "fit_sha256" not in source_columns:
        conn.execute("alter table activity_source add column fit_sha256 text")


def _valid_fit_sha256(value):
    return isinstance(value, str) and _FIT_SHA256_RE.fullmatch(value) is not None


def _database_home(conn):
    for row in conn.execute("pragma database_list"):
        if row["name"] == "main" and row["file"]:
            return Path(row["file"]).resolve().parent
    return OPENATHLETE_HOME


def _source_fit_bytes(source, archive_root):
    if not source["fit_path"]:
        raise FileNotFoundError("来源没有 fit_path")
    path = Path(source["fit_path"])
    if not path.is_absolute():
        path = Path(archive_root) / path
    return path.read_bytes()


def _note_source_conflict(conn, source, reason):
    note = source["note"]
    conn.execute(
        "update activity_source set note = ? where id = ?",
        ("{}; {}".format(note, reason) if note else reason, source["id"]))
    if source["activity_id"] is None:
        conn.execute("update activity_source set is_canonical = 0 where id = ?", (source["id"],))
    else:
        conn.execute(
            "update activity_source set is_canonical = 0 where activity_id = ?",
            (source["activity_id"],))


def _migrate_legacy_rows(conn):
    conn.execute(
        "update activity set start_ts = start_ts - tz_offset_s "
        "where start_ts is not null and tz_offset_s is not null")
    archive_root = _database_home(conn)
    sources = conn.execute("select * from activity_source order by id").fetchall()
    for source in sources:
        stored = source["fit_sha256"]
        try:
            raw = _source_fit_bytes(source, archive_root)
        except (OSError, ValueError) as exc:
            _note_source_conflict(conn, source, "legacy_fit_conflict: {}".format(exc))
            continue
        actual = hashlib.sha256(raw).hexdigest()
        if stored in (None, ""):
            conn.execute(
                "update activity_source set fit_sha256 = ? where id = ?", (actual, source["id"]))
        elif not _valid_fit_sha256(stored) or stored != actual:
            _note_source_conflict(
                conn, source,
                "legacy_fit_conflict: stored_sha256={} actual_sha256={}".format(stored, actual))
    _rebuild_daily_load(conn)


def init_db(conn):
    version = conn.execute("pragma user_version").fetchone()[0]
    legacy = version == 0 and (_has_table(conn, "activity") or
                              _has_table(conn, "activity_source"))
    if conn.in_transaction:
        conn.commit()
    conn.execute("begin immediate")
    try:
        _apply_schema(conn)
        if legacy:
            _migrate_legacy_rows(conn)
        if version < SCHEMA_VERSION:
            conn.execute("pragma user_version = {}".format(SCHEMA_VERSION))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def normalize_sport(raw_sport, raw_sub):
    raw_sport = (raw_sport or "").lower() or None
    raw_sub = (raw_sub or "").lower() or None
    if raw_sport in _SUB_MAP:
        sport, subs = _SUB_MAP[raw_sport]
        return sport, subs.get(raw_sub, raw_sub)
    return raw_sport, raw_sub


def _epoch(dt):
    return int(dt.timestamp()) if dt is not None else None


def tz_offset_from_activity(utc_epoch, local_timestamp):
    """activity 消息的 local_timestamp（Garmin epoch 原始值）→ 时区偏移秒。"""
    if not utc_epoch or not local_timestamp:
        return None
    return int(round((local_timestamp + FIT_EPOCH_OFFSET_S - utc_epoch)
                     / TZ_GRANULARITY_S)) * TZ_GRANULARITY_S


def decode_fit(raw):
    """FIT 字节 → sessions/laps/records/tz 等纯数据结构（IO 与解析分离的边界）。"""
    from garmin_fit_sdk import Decoder, Stream
    messages, errors = Decoder(Stream.from_bytes_io(io.BytesIO(raw))).read()
    if errors:
        logging.debug("FIT 解码告警：%s", errors[:3])
    sessions = messages.get("session_mesgs") or []
    laps = [(_epoch(m.get("start_time")), Lap(
        dist_m=m.get("total_distance") or 0.0,
        time_s=m.get("total_elapsed_time") or m.get("total_timer_time") or 0.0,
        pace=((m.get("total_elapsed_time") or 0.0) / ((m.get("total_distance") or 0.0) / 1000))
             if (m.get("total_distance") or 0.0) > 1
             and (m.get("total_elapsed_time") or 0.0) > 0 else None,
        avg_hr=m.get("avg_heart_rate"),
        max_hr=m.get("max_heart_rate"),
        avg_power=m.get("avg_power"),
        trigger=m.get("lap_trigger"),
    )) for m in (messages.get("lap_mesgs") or [])]
    records = sorted(
        ((_epoch(m.get("timestamp")), m.get("heart_rate"))
         for m in (messages.get("record_mesgs") or []) if m.get("timestamp")),
        key=lambda r: r[0])
    tz_offset = None
    for a in (messages.get("activity_mesgs") or []):
        tz_offset = tz_offset_from_activity(_epoch(a.get("timestamp")),
                                            a.get("local_timestamp"))
        if tz_offset is not None:
            break
    workout = (messages.get("workout_mesgs") or [None])[0]
    return {
        "sessions": sessions, "laps": laps, "records": records,
        "tz_offset_s": tz_offset,
        "workout_name": (workout or {}).get("wkt_name"),
        "workout_step_count": len(messages.get("workout_step_mesgs") or []),
    }


def session_windows(sessions):
    starts = [_epoch(s.get("start_time")) for s in sessions]
    return [(starts[i], starts[i + 1] if i + 1 < len(starts) else None)
            for i in range(len(starts))]


def in_window(t, window):
    return t is not None and t >= window[0] and (window[1] is None or t < window[1])


def parse_structure(sport, sub_sport, session_dict, laps,
                    ride_min_km=DEFAULT_RIDE_TRAINING_MIN_KM):
    """按运动类型分派结构解析；一切输入缺失退化为 None（只入汇总，不报错）。"""
    if not laps:
        return None
    if sport == "run" and sub_sport in STRUCTURE_RUN_SUBS:
        struct = summarize(session_dict, laps)[1]
    elif sport == "ride":
        if ride_is_training(session_dict, laps, ride_min_km):
            struct = summarize_ride(session_dict, laps)[1]
        else:
            struct = summarize_commute_ride(session_dict, laps)[1]
    else:
        return None
    d = struct.to_dict()
    if d.get("type") == "unknown":
        return None
    errs = validate_structure_dict(d)
    if errs:
        raise ValueError("结构校验失败：" + "；".join(errs))
    return d


def derived_columns(struct):
    cols = {"type": None, "rep_count": None, "rep_dist_modal_m": None,
            "rep_dur_modal_s": None, "rep_pace_mean_s": None,
            "interval_dist_total_m": None}
    if not struct:
        return cols
    cols["type"] = struct.get("type")
    if struct.get("type") != "intervals":
        return cols
    reps = struct.get("reps") or []
    cols["rep_count"] = len(reps)
    groups = [b for b in struct.get("blocks", []) if b.get("kind") == "group"]
    if groups and len(groups[0].get("steps", [])) == 1:
        step = groups[0]["steps"][0]
        cols["rep_dist_modal_m"] = step.get("dist_m")
        cols["rep_dur_modal_s"] = step.get("time_s")
    paces = [r["time_s"] / (r["dist_m"] / 1000) for r in reps
             if r.get("dist_m", 0) > 1 and r.get("time_s", 0) > 0]
    if paces:
        cols["rep_pace_mean_s"] = round(sum(paces) / len(paces), 1)
    dists = [r["dist_m"] for r in reps if r.get("dist_m")]
    if dists:
        cols["interval_dist_total_m"] = round(sum(dists), 0)
    return cols


def find_anchor(conn, sport, date_str):
    """当日有效锚点；resting HR 独立按全局最近生效版本取值。"""
    for s in HR_ANCHOR_FALLBACK.get(sport, (sport,)):
        row = conn.execute(
            "select * from threshold_anchor where sport = ? and valid_from <= ? "
            "and (valid_to is null or valid_to >= ?) "
            "order by valid_from desc, id desc limit 1",
            (s, date_str, date_str)).fetchone()
        if row:
            result = dict(row)
            resting = conn.execute(
                "select resting_hr from threshold_anchor where resting_hr is not null "
                "and valid_from <= ? and (valid_to is null or valid_to >= ?) "
                "order by valid_from desc, id desc limit 1",
                (date_str, date_str)).fetchone()
            if resting:
                result["resting_hr"] = resting["resting_hr"]
            return result
    return None


def edwards_trimp(records, resting_hr, max_hr):
    """records: [(epoch_s, hr)] 已按时间排序。返回 TRIMP 或 None（输入不足）。"""
    hrr = max_hr - resting_hr
    if not records or hrr <= 0:
        return None
    bounds = [resting_hr + p * hrr for p in EDWARDS_HRR_BANDS]
    total = 0.0
    for (t0, hr0), (t1, _h1) in zip(records, records[1:]):
        dt = min(t1 - t0, MAX_RECORD_GAP_S)
        if dt <= 0 or not hr0:
            continue
        total += dt / 60 * max(1, sum(hr0 >= b for b in bounds))
    return round(total, 1)


def zone_boundaries(lt1_hr, lt2_hr):
    return [round(ZONE1_LT1_PCT * lt1_hr, 1), round(lt1_hr, 1),
            round(ZONE_LT2_PCTS[0] * lt2_hr, 1), round(ZONE_LT2_PCTS[1] * lt2_hr, 1),
            round(ZONE_LT2_PCTS[2] * lt2_hr, 1), round(ZONE_LT2_PCTS[3] * lt2_hr, 1)]


def time_in_zone(records, boundaries):
    times = [0.0] * 7
    for (t0, hr0), (t1, _h1) in zip(records, records[1:]):
        dt = min(t1 - t0, MAX_RECORD_GAP_S)
        if dt <= 0 or not hr0:
            continue
        times[bisect.bisect_right(boundaries, hr0)] += dt
    return [round(t, 0) for t in times]


def _activity_local_date(conn, start_ts, tz_offset_s, local_tz=None):
    if start_ts is None:
        return None
    if tz_offset_s is None:
        local_tz = local_tz or configured_tzinfo(load_config(_database_home(conn)))
        return datetime.fromtimestamp(start_ts, tz=local_tz).date()
    return datetime.fromtimestamp(start_ts + tz_offset_s, tz=timezone.utc).date()


def build_activity_row(conn, sess, laps, records, meta, label,
                       ride_min_km=DEFAULT_RIDE_TRAINING_MIN_KM,
                       ride_confirmed=True):
    sport, sub = normalize_sport(sess.get("sport"), sess.get("sub_sport"))
    session_dict = {"distance_m": sess.get("total_distance"),
                    "time_s": sess.get("total_elapsed_time"),
                    "avg_power": sess.get("avg_power")}
    ride_candidate = sport == "ride" and not ride_is_training(
        session_dict, laps, ride_min_km)
    struct = None
    try:
        struct = parse_structure(sport, sub, session_dict, laps, ride_min_km)
    except Exception as e:
        logging.warning("结构解析失败 %s：%s（降级为仅汇总）", label, e)
    cols = derived_columns(struct)

    start_ts = _epoch(sess.get("start_time"))
    tz = meta.get("tz_offset_s")
    local_date = _activity_local_date(conn, start_ts, tz)
    date_str = local_date.isoformat() if local_date is not None else None
    trimp = tiz = zb_json = None
    if date_str:
        anchor = find_anchor(conn, sport, date_str)
        if anchor and records:
            if anchor["resting_hr"] and anchor["max_hr"]:
                trimp = edwards_trimp(records, anchor["resting_hr"], anchor["max_hr"])
            if anchor["lt1_hr"] and anchor["lt2_hr"]:
                bounds = zone_boundaries(anchor["lt1_hr"], anchor["lt2_hr"])
                tiz = time_in_zone(records, bounds)
                zb_json = json.dumps(bounds)

    raw_meta = {"garmin_sport": sess.get("sport"), "garmin_sub_sport": sess.get("sub_sport")}
    if ride_candidate and not ride_confirmed:
        raw_meta["ride_threshold_pending"] = True
    if meta.get("workout_name"):
        raw_meta["workout_name"] = meta["workout_name"]
    if meta.get("workout_step_count"):
        raw_meta["workout_step_count"] = meta["workout_step_count"]
    if meta.get("multisport_segment"):
        raw_meta["multisport_segment"] = meta["multisport_segment"]

    duration = sess.get("total_elapsed_time")
    distance = sess.get("total_distance")
    row = {
        "start_ts": start_ts, "tz_offset_s": tz,
        "sport": sport, "sub_sport": sub,
        "duration_s": duration, "distance_m": distance,
        "ascent_m": sess.get("total_ascent"),
        "avg_hr": sess.get("avg_heart_rate"), "max_hr": sess.get("max_heart_rate"),
        "avg_power_w": sess.get("avg_power"), "avg_cadence_per_min": sess.get("avg_cadence"),
        "calories": sess.get("total_calories"),
        "avg_speed_mps": round(distance / duration, 3)
        if distance and duration else None,
        "trimp": trimp,
        "structure_json": json.dumps(struct, ensure_ascii=False) if struct else None,
        "zone_boundaries_json": zb_json,
        "raw_meta_json": json.dumps(raw_meta, ensure_ascii=False),
        "parser_version": PARSER_VERSION,
    }
    row.update(cols)
    return row, tiz


# 活动同一性（DESIGN.md §3）：开始时间 ±5 分钟 + multisport 段序号；
# 逻辑活动匹配不依赖 canonical，发生冲突的来源副本仍能继续合并。
IDENTITY_WINDOW_S = 300

# 新归档命名（DESIGN.md §3）：<本地开始 yyyymmdd-hhmmss>-<运动类型>-<来源平台>-<平台活动id>.fit
ARCHIVE_FIT_NAME_RE = re.compile(r"^\d{8}-\d{6}-[a-z0-9_]+-([a-z]+)-(.+)$", re.IGNORECASE)

_ACTIVITY_COLUMNS = (
    "start_ts", "tz_offset_s", "sport", "sub_sport", "duration_s", "distance_m",
    "ascent_m", "avg_hr", "max_hr", "avg_power_w", "avg_cadence_per_min", "calories",
    "avg_speed_mps", "trimp", "structure_json", "type", "rep_count",
    "rep_dist_modal_m", "rep_dur_modal_s", "rep_pace_mean_s", "interval_dist_total_m",
    "zone_boundaries_json", "raw_meta_json", "parser_version",
)


def _source_segment_number(pid):
    match = re.search(r"#(\d+)$", pid or "")
    return int(match.group(1)) if match else None


def _identity_candidates(conn, start_ts, segment_number=None):
    if start_ts is None:
        return []
    rows = conn.execute(
        "select a.id aid, a.start_ts, s.platform_activity_id pid from activity a "
        "join activity_source s on s.activity_id = a.id "
        "where a.start_ts between ? and ? order by abs(a.start_ts - ?), a.id, s.id",
        (start_ts - IDENTITY_WINDOW_S, start_ts + IDENTITY_WINDOW_S, start_ts)).fetchall()
    matches = {}
    for row in rows:
        if _source_segment_number(row["pid"]) == segment_number:
            matches.setdefault(row["aid"], row)
    return list(matches.values())


def find_canonical_near(conn, start_ts, segment_number=None):
    """唯一匹配返回逻辑活动 ID；无匹配或多个歧义候选均返回 None。"""
    candidates = _identity_candidates(conn, start_ts, segment_number)
    return candidates[0]["aid"] if len(candidates) == 1 else None


def _build_from_messages(conn, msgs, pid, ride_min_km, ride_confirmed,
                         segment_number=None):
    sessions = msgs["sessions"]
    if not sessions:
        raise ValueError("无 session 消息")
    single = len(sessions) == 1
    if single:
        i = 0
    else:
        number = segment_number or _source_segment_number(pid)
        if number is None or not 1 <= number <= len(sessions):
            raise ValueError("multisport 来源段序号无效：{}".format(pid))
        i = number - 1
    sess = sessions[i]
    if single:
        laps = [lap for _t, lap in msgs["laps"]]
        records = msgs["records"]
    else:
        window = session_windows(sessions)[i]
        laps = [lap for t, lap in msgs["laps"] if in_window(t, window)]
        records = [r for r in msgs["records"] if in_window(r[0], window)]
    meta = dict(msgs)
    meta["multisport_segment"] = None if single else "{}/{}".format(i + 1, len(sessions))
    row, tiz = build_activity_row(conn, sess, laps, records, meta, pid,
                                  ride_min_km=ride_min_km, ride_confirmed=ride_confirmed)
    session_dict = {"distance_m": sess.get("total_distance"),
                    "time_s": sess.get("total_elapsed_time"),
                    "avg_power": sess.get("avg_power")}
    ride_candidate = row["sport"] == "ride" and not ride_is_training(
        session_dict, laps, ride_min_km)
    return row, tiz, ride_candidate


def _replace_time_in_zone(conn, activity_id, tiz):
    conn.execute("delete from time_in_zone where activity_id = ?", (activity_id,))
    if tiz:
        conn.executemany(
            "insert into time_in_zone (activity_id, zone, time_s) values (?, ?, ?)",
            [(activity_id, z + 1, t) for z, t in enumerate(tiz)])


def _insert_activity(conn, row):
    columns = ", ".join(_ACTIVITY_COLUMNS)
    values = ", ".join(":{}".format(c) for c in _ACTIVITY_COLUMNS)
    cur = conn.execute("insert into activity ({}) values ({})".format(columns, values), row)
    return cur.lastrowid


def _replace_activity(conn, activity_id, row, tiz):
    assignments = ", ".join("{} = :{}".format(c, c) for c in _ACTIVITY_COLUMNS)
    values = dict(row)
    values["activity_id"] = activity_id
    conn.execute("update activity set {} where id = :activity_id".format(assignments), values)
    _replace_time_in_zone(conn, activity_id, tiz)


def _source_sort_key(source):
    return source["platform"] or "", source["platform_activity_id"] or ""


def _select_canonical(sources, primary_platform_name):
    if not sources or any(not _valid_fit_sha256(s["fit_sha256"]) for s in sources):
        return None
    hashes = [s["fit_sha256"] for s in sources]
    if len(set(hashes)) == 1:
        return min(sources, key=_source_sort_key)
    primary = str(primary_platform_name).strip().lower() \
        if primary_platform_name is not None else ""
    candidates = [s for s in sources if (s["platform"] or "").lower() == primary] \
        if primary else []
    return min(candidates, key=_source_sort_key) if candidates else None


def _read_source_messages(source, archive_root):
    root = Path(archive_root) if archive_root is not None else OPENATHLETE_HOME
    raw = _source_fit_bytes(source, root)
    actual = hashlib.sha256(raw).hexdigest()
    if source["fit_sha256"] != actual:
        raise ValueError(
            "SOURCE_HASH_CONFLICT: {} 的归档字节已变化".format(source["fit_path"]))
    return decode_fit(raw)


def _reconcile_canonical(conn, activity_id, archive_root, primary_platform_name,
                         ride_min_km, ride_confirmed, review, new_source_id, new_msgs):
    sources = conn.execute(
        "select * from activity_source where activity_id = ? "
        "order by platform, platform_activity_id, id", (activity_id,)).fetchall()
    chosen = _select_canonical(sources, primary_platform_name)
    if chosen is None:
        conn.execute("update activity_source set is_canonical = 0 where activity_id = ?",
                     (activity_id,))
        if review is not None:
            review.append({"type": "canonical_conflict", "activity_id": activity_id})
        return
    current = conn.execute(
        "select id from activity_source where activity_id = ? and is_canonical = 1",
        (activity_id,)).fetchone()
    if current and current["id"] == chosen["id"]:
        return
    try:
        msgs = new_msgs if chosen["id"] == new_source_id else _read_source_messages(
            chosen, archive_root)
        row, tiz, _ride_candidate = _build_from_messages(
            conn, msgs, chosen["platform_activity_id"], ride_min_km, ride_confirmed)
    except (OSError, ValueError) as exc:
        _note_source_conflict(conn, chosen, "canonical_fit_conflict: {}".format(exc))
        if review is not None:
            review.append({"type": "canonical_conflict", "activity_id": activity_id})
        return
    _replace_activity(conn, activity_id, row, tiz)
    conn.execute("update activity_source set is_canonical = 0 where activity_id = ?",
                 (activity_id,))
    conn.execute("update activity_source set is_canonical = 1 where id = ?", (chosen["id"],))


def ingest_fit_payload(conn, raw, platform, pid_base, rel_path, archive_root=None,
                       primary_platform_name=None,
                       ride_min_km=DEFAULT_RIDE_TRAINING_MIN_KM, ride_confirmed=True,
                       review=None, _defer_daily_load=False):
    """一份 FIT（字节）入库：同来源判重 → 跨来源同一性合并 → 新活动插入。

    返回 (inserted, duplicated, skipped)：新入 activity 数 / 同一性合并副本数 / 已入库跳过数。
    """
    fit_sha256 = hashlib.sha256(raw).hexdigest()
    msgs = decode_fit(raw)
    sessions = msgs["sessions"]
    if not sessions:
        raise ValueError("无 session 消息")
    rel_path = str(rel_path).replace("\\", "/")
    single = len(sessions) == 1
    base_segment = _source_segment_number(pid_base)
    if not single and base_segment is not None:
        raise ValueError("分段归档文件不应再包含多个 session：{}".format(pid_base))
    inserted = duplicated = skipped = 0
    load_dirty = False
    try:
        for i, _sess in enumerate(sessions):
            segment_number = base_segment if single else i + 1
            pid = pid_base if single else "{}#{}".format(pid_base, segment_number)
            existing = conn.execute(
                "select * from activity_source where platform = ? and platform_activity_id = ?",
                (platform, pid)).fetchone()
            if existing is not None:
                if existing["fit_sha256"] == fit_sha256:
                    skipped += 1
                    continue
                reason = ("SOURCE_HASH_CONFLICT platform={} pid={} stored_sha256={} "
                          "incoming_sha256={} incoming_fit_path={}").format(
                              platform, pid, existing["fit_sha256"], fit_sha256, rel_path)
                _note_source_conflict(conn, existing, reason)
                load_dirty = True
                if review is not None:
                    review.append({
                        "type": "source_hash_conflict", "activity_id": existing["activity_id"],
                        "platform": platform, "platform_activity_id": pid,
                        "stored_sha256": existing["fit_sha256"],
                        "incoming_sha256": fit_sha256, "incoming_fit_path": rel_path,
                    })
                raise ValueError(reason)

            row, tiz, ride_candidate = _build_from_messages(
                conn, msgs, pid, ride_min_km, ride_confirmed, segment_number)
            candidates = _identity_candidates(conn, row["start_ts"], segment_number)
            if not candidates:
                activity_id = _insert_activity(conn, row)
                _replace_time_in_zone(conn, activity_id, tiz)
                conn.execute(
                    "insert into activity_source (activity_id, platform, platform_activity_id, "
                    "fit_path, fit_sha256, is_canonical, note) values (?, ?, ?, ?, ?, 1, null)",
                    (activity_id, platform, pid, rel_path, fit_sha256))
                if ride_candidate and review is not None:
                    review.append({"type": "ride_threshold", "activity_id": activity_id,
                                   "platform_activity_id": pid})
                inserted += 1
                load_dirty = True
                continue
            if len(candidates) > 1:
                candidate_ids = sorted(r["aid"] for r in candidates)
                activity_id = _insert_activity(conn, row)
                _replace_time_in_zone(conn, activity_id, tiz)
                note = "identity_conflict: ambiguous activity_ids={}".format(candidate_ids)
                conn.execute(
                    "insert into activity_source (activity_id, platform, platform_activity_id, "
                    "fit_path, fit_sha256, is_canonical, note) values (?, ?, ?, ?, ?, 0, ?)",
                    (activity_id, platform, pid, rel_path, fit_sha256, note))
                conn.execute(
                    "update activity_source set is_canonical = 0 where activity_id in ({})".format(
                        ",".join("?" for _ in candidate_ids)), candidate_ids)
                if review is not None:
                    if ride_candidate:
                        review.append({"type": "ride_threshold", "activity_id": activity_id,
                                       "platform_activity_id": pid})
                    review.append({
                        "type": "canonical_conflict", "activity_id": activity_id,
                        "reason": "identity_ambiguous", "candidate_activity_ids": candidate_ids,
                    })
                inserted += 1
                load_dirty = True
                continue

            near = candidates[0]["aid"]
            cur = conn.execute(
                "insert into activity_source (activity_id, platform, platform_activity_id, "
                "fit_path, fit_sha256, is_canonical, note) values (?, ?, ?, ?, ?, 0, ?)",
                (near, platform, pid, rel_path, fit_sha256,
                 "duplicate: 开始时间 ±{}s 同一性合并".format(IDENTITY_WINDOW_S)))
            if ride_candidate and review is not None:
                review.append({"type": "ride_threshold", "activity_id": near,
                               "platform_activity_id": pid})
            duplicated += 1
            load_dirty = True
            _reconcile_canonical(conn, near, archive_root, primary_platform_name,
                                 ride_min_km, ride_confirmed, review, cur.lastrowid, msgs)
    except Exception:
        if load_dirty and not _defer_daily_load:
            _rebuild_daily_load(conn)
        raise
    if load_dirty and not _defer_daily_load:
        _rebuild_daily_load(conn)
    return inserted, duplicated, skipped


def ingest_archive_file(conn, path, fit_dir, archive_root=None, primary_platform_name=None,
                        ride_min_km=DEFAULT_RIDE_TRAINING_MIN_KM, ride_confirmed=True,
                        review=None, _defer_daily_load=False):
    """按 DESIGN 文件名入库一个裸 FIT，返回 (inserted, duplicated, skipped)。"""
    rel = str(path.relative_to(fit_dir.parent)).replace("\\", "/")
    options = {"archive_root": archive_root or fit_dir.parent,
               "primary_platform_name": primary_platform_name,
               "ride_min_km": ride_min_km, "ride_confirmed": ride_confirmed, "review": review,
               "_defer_daily_load": _defer_daily_load}
    if path.suffix.lower() != ".fit":
        raise ValueError("长期归档只接受裸 .fit：{}".format(path.name))
    m = ARCHIVE_FIT_NAME_RE.match(path.stem)
    if not m:
        raise ValueError(
            "文件名不符归档命名 <yyyymmdd-hhmmss>-<sport>-<platform>-<id>：{}".format(path.name))
    platform, pid = m.group(1).lower(), m.group(2)
    return ingest_fit_payload(conn, path.read_bytes(), platform, pid, rel, **options)


def cmd_init(conn, db_path):
    init_db(conn)
    print("已建库：{}（WAL，6 张表）".format(db_path))


def _review_ids(review, kind):
    return {r["activity_id"] for r in review if r.get("type") == kind}


def _ask_ride_threshold(default):
    answer = input(
        "检测到短距离无功率骑行。训练/通勤解析分界用 {}km 吗？"
        "回车确认，输入新公里数，或 s 跳过：".format(default)).strip().lower()
    if answer in ("", "y", "yes"):
        return default
    if answer in ("s", "skip", "n", "no"):
        return None
    try:
        value = float(answer)
    except ValueError:
        return None
    return value if value > 0 else None


def cmd_ingest(conn, fit_dir, limit=None, rebuild=False):
    if rebuild and limit is not None:
        print("--rebuild 必须全量处理，不能与 --limit 同用")
        return 2
    home = fit_dir.parent
    config = load_config(home)
    primary = primary_platform(config)
    ride_min_km = ride_training_min_km(config)
    ride_confirmed = ride_threshold_confirmed(config)
    if rebuild:
        for t in _DROP_TABLES:
            conn.execute("drop table if exists {}".format(t))
        init_db(conn)
        print("已重建 FIT 派生表（保留 threshold_anchor 与 daily_metrics）")
    if not fit_dir.exists():
        print("找不到 FIT 归档目录：{}".format(fit_dir))
        return 1
    files = sorted(fit_dir.rglob("*.fit"))
    if limit is not None:
        files = files[:limit]
    ok = fail = skip = dup_files = rows = dups = 0
    failures = []
    review = []
    for i, p in enumerate(files, 1):
        try:
            inserted, dup, _skipped = ingest_archive_file(
                conn, p, fit_dir, primary_platform_name=primary,
                ride_min_km=ride_min_km, ride_confirmed=ride_confirmed, review=review,
                _defer_daily_load=True)
            rows += inserted
            dups += dup
            if inserted:
                ok += 1
            elif dup:
                dup_files += 1
            else:
                skip += 1
        except Exception as e:
            fail += 1
            failures.append((p.name, str(e)))
            logging.error("ingest 失败 %s：%s", p.name, e)
        if i % 50 == 0:
            conn.commit()
            print("  进度 {}/{}（入库 {} 活动，失败 {}）".format(i, len(files), rows, fail))
    _rebuild_daily_load(conn)
    conn.commit()
    print("完成：扫描 {} 个文件，新入库 {} 个（{} 活动），同一性合并 {} 个（{} 副本），"
          "跳过已入库 {}，失败 {}"
          .format(len(files), ok, rows, dup_files, dups, skip, fail))
    for name, err in failures:
        print("  失败 {}：{}".format(name, err))
    if failures:
        print("失败详情见日志：{}".format(OPENATHLETE_HOME / "logs" / "athlete-db.log"))
    conflicts = _review_ids(review, "canonical_conflict")
    if conflicts:
        print("canonical 冲突 {} 个：缺少可用 primary_platform，未进入统一统计。".format(
            len(conflicts)))
    ride_candidates = _review_ids(review, "ride_threshold")
    if ride_candidates and not ride_confirmed:
        print("骑行阈值候选 {} 个（当前按 {}km 分派）。".format(
            len(ride_candidates), ride_min_km))
        new_threshold = None
        if sys.stdin.isatty():
            new_threshold = _ask_ride_threshold(ride_min_km)
            if new_threshold is not None:
                set_config_values(home, ride_training_min_km=new_threshold)
                print("阈值已保存，全量重算骑行结构与待确认标记。")
                return cmd_ingest(conn, fit_dir, rebuild=True)
        if new_threshold is None:
            print("阈值尚未确认；可用 `athlete_db.py config set --ride-training-min-km N` "
                  "保存，再执行 `athlete_db.py ingest --rebuild`。")
    return 1 if fail or conflicts else 0


ANCHOR_FIELDS = ("lt1_hr", "lt2_hr", "max_hr", "resting_hr", "threshold_pace_s",
                 "ftp_w", "critical_speed_mps")


def write_anchor(conn, sport, fields, source):
    """写入完整快照；空更新 no-op，同日幂等，历史插入维护相邻有效期。"""
    valid_from = fields["valid_from"]
    datetime.strptime(valid_from, "%Y-%m-%d")
    supplied = [c for c in ANCHOR_FIELDS if fields.get(c) is not None]
    if not supplied:
        return []
    same_day = conn.execute(
        "select * from threshold_anchor where sport = ? and valid_from = ? "
        "order by id desc limit 1", (sport, valid_from)).fetchone()
    base = same_day or conn.execute(
        "select * from threshold_anchor where sport = ? and valid_from < ? "
        "order by valid_from desc, id desc limit 1", (sport, valid_from)).fetchone()
    next_row = conn.execute(
        "select valid_from from threshold_anchor where sport = ? and valid_from > ? "
        "order by valid_from asc limit 1", (sport, valid_from)).fetchone()
    valid_to = fields.get("valid_to")
    if valid_to is None and next_row:
        valid_to = (datetime.strptime(next_row["valid_from"], "%Y-%m-%d").date()
                    - timedelta(days=1)).isoformat()
    elif valid_to is None and same_day:
        valid_to = same_day["valid_to"]
    if valid_to:
        datetime.strptime(valid_to, "%Y-%m-%d")
        if valid_to < valid_from:
            raise ValueError("valid_to 不能早于 valid_from")
        if next_row and valid_to >= next_row["valid_from"]:
            raise ValueError("valid_to 与下一锚点版本重叠")
    merged = {c: (base[c] if base else None) for c in ANCHOR_FIELDS}
    merged.update({c: fields[c] for c in supplied})
    note = fields.get("note") if fields.get("note") is not None \
        else (base["note"] if base else None)
    conn.execute("delete from threshold_anchor where sport = ? and valid_from = ?",
                 (sport, valid_from))
    conn.execute(
        "update threshold_anchor set valid_to = date(?, '-1 day') "
        "where sport = ? and valid_from < ? and (valid_to is null or valid_to >= ?)",
        (valid_from, sport, valid_from, valid_from))
    conn.execute(
        "insert into threshold_anchor (sport, valid_from, valid_to, lt1_hr, lt2_hr, max_hr, "
        "resting_hr, threshold_pace_s, ftp_w, critical_speed_mps, source, note) "
        "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sport, valid_from, valid_to) + tuple(merged[c] for c in ANCHOR_FIELDS) +
        (source or (same_day["source"] if same_day else None) or "manual", note))
    conn.commit()
    return supplied


def cmd_anchor_set(conn, args):
    if not any(v is not None for v in (args.lt1_hr, args.lt2_hr, args.max_hr,
                                       args.resting_hr, args.threshold_pace_s,
                                       args.ftp_w, args.critical_speed_mps)):
        print("至少给一个阈值字段（--lt1-hr / --lt2-hr / --max-hr / --resting-hr / "
              "--threshold-pace-s / --ftp-w / --critical-speed-mps）")
        return 1
    fields = {c: getattr(args, c) for c in ANCHOR_FIELDS}
    fields.update({"valid_from": args.valid_from, "valid_to": args.valid_to,
                   "note": args.note})
    write_anchor(conn, args.sport, fields, args.source)
    print("锚点已写入：{} 自 {} 生效（{}）".format(args.sport, args.valid_from,
                                                args.source or "manual"))
    print("提示：锚点补全后用 ingest --rebuild 全量重算 trimp / time_in_zone")


def cmd_anchor_list(conn):
    rows = conn.execute(
        "select * from threshold_anchor order by sport, valid_from").fetchall()
    if not rows:
        print("尚无锚点。用 anchor set 写入（--sport/--valid-from 必填）。")
        return
    for r in rows:
        vals = []
        for k in ("lt1_hr", "lt2_hr", "max_hr", "resting_hr", "threshold_pace_s",
                  "ftp_w", "critical_speed_mps"):
            if r[k] is not None:
                vals.append("{}={}".format(k, r[k]))
        print("{} {}~{}  {}  ({})".format(
            r["sport"], r["valid_from"], r["valid_to"] or "至今",
            " ".join(vals), r["source"] or "-"))


def cmd_stats(conn, fit_dir):
    canonical = (
        "exists (select 1 from activity_source s where s.activity_id = activity.id "
        "and s.is_canonical = 1)")
    total = conn.execute(
        "select count(*) c from activity where " + canonical).fetchone()["c"]
    print("activity 共 {} 行（仅 canonical）".format(total))
    print("\n按运动类型：")
    for r in conn.execute(
            "select sport, count(*) c, round(sum(distance_m)/1000, 1) km, "
            "round(sum(duration_s)/3600, 1) h from activity where " + canonical +
            " group by sport order by c desc"):
        print("  {:<12} {:>4} 次  {:>9} km  {:>7} h".format(
            r["sport"], r["c"], r["km"], r["h"]))
    print("\n按结构类型：")
    for r in conn.execute(
            "select coalesce(type, 'NULL(无结构)') t, count(*) c from activity where " +
            canonical + " group by type order by c desc"):
        print("  {:<16} {:>4}".format(r["t"], r["c"]))
    cov = conn.execute(
        "select count(trimp) t, count(zone_boundaries_json) z from activity where " +
        canonical).fetchone()
    print("\n派生覆盖：trimp {} 行，time_in_zone {} 行（无锚点覆盖的日期置 NULL 属预期）"
          .format(cov["t"], cov["z"]))
    anchors = conn.execute("select count(*) c from threshold_anchor").fetchone()["c"]
    print("threshold_anchor {} 行".format(anchors))
    print("\n来源与副本：")
    for r in conn.execute(
            "select platform, count(*) c, sum(is_canonical) canon from activity_source "
            "group by platform order by c desc"):
        print("  {:<10} {:>4} 条（canonical {}，副本 {}）"
              .format(r["platform"], r["c"], r["canon"], r["c"] - r["canon"]))
    conflicts = conn.execute(
        "select count(*) c from activity a where "
        "exists (select 1 from activity_source s where s.activity_id = a.id) and "
        "not exists (select 1 from activity_source s where s.activity_id = a.id "
        "and s.is_canonical = 1)").fetchone()["c"]
    print("canonical 冲突 {} 个（无 canonical，不进入统一统计或负荷）".format(conflicts))
    pending = conn.execute(
        "select count(*) c from activity where raw_meta_json like '%ride_threshold_pending%' "
        "and " + canonical).fetchone()["c"]
    print("骑行阈值待确认 {} 个".format(pending))
    dm = conn.execute(
        "select count(*) c, min(date) d0, max(date) d1 from daily_metrics").fetchone()
    dl = conn.execute(
        "select count(*) c, min(date) d0, max(date) d1 from daily_load").fetchone()
    print("daily_metrics {} 行（{}~{}），daily_load {} 天（{}~{}）"
          .format(dm["c"], dm["d0"], dm["d1"], dl["c"], dl["d0"], dl["d1"]))
    if fit_dir.exists():
        ingested = set()
        for r in conn.execute(
                "select platform p, platform_activity_id pid from activity_source"):
            ingested.add((r["p"], r["pid"].split("#")[0]))
        missing = []
        for p in sorted(fit_dir.rglob("*.fit")):
            m = ARCHIVE_FIT_NAME_RE.match(p.stem)
            plat = m.group(1).lower() if m else None
            pid = m.group(2).split("#")[0] if m else p.stem
            if (plat, pid) not in ingested:
                missing.append(p.name)
        print("\n归档中未入库 {} 个：".format(len(missing)))
        for name in missing:
            print("  {}（原因见 logs/athlete-db.log）".format(name))


DAILY_METRICS_FIELDS = (
    "resting_hr", "hrv_ms", "sleep_total_s", "sleep_deep_s", "sleep_light_s",
    "sleep_rem_s", "sleep_awake_s", "sleep_score", "spo2_pct", "stress_avg",
    "body_battery", "steps", "calories",
)


def upsert_daily_metrics(conn, rows):
    """健康时序入库。rows: [{"date": "YYYY-MM-DD", "source": str, <字段>: 值|None}]。

    冲突时只覆盖本次传入非 None 的字段——不同抓取通道各补各的字段
    （如高驰 web API 给 rhr/hrv，官方 MCP 给睡眠分期），互不抹除。
    返回写入行数。
    """
    cols = ", ".join(("date", "source") + DAILY_METRICS_FIELDS)
    update = ", ".join(
        "{f} = coalesce(excluded.{f}, daily_metrics.{f})".format(f=f)
        for f in DAILY_METRICS_FIELDS)
    sql = ("insert into daily_metrics ({}) values ({}) "
           "on conflict(date, source) do update set {}".format(
               cols, ", ".join("?" for _ in DAILY_METRICS_FIELDS + ("date", "source")),
               update))
    n = 0
    for r in rows:
        conn.execute(sql, [r.get("date"), r.get("source")] +
                     [r.get(f) for f in DAILY_METRICS_FIELDS])
        n += 1
    conn.commit()
    return n


# PMC 时间常数（天）：CTL 42 / ATL 7（TrainingPeaks 惯例；DESIGN.md §3 负荷会计）
CTL_TAU_DAYS = 42
ATL_TAU_DAYS = 7


def _rebuild_daily_load(conn, today=None):
    per_day = {}
    unknown_per_day = {}
    local_tz = configured_tzinfo(load_config(_database_home(conn)))
    for r in conn.execute(
            "select a.start_ts, a.tz_offset_s, a.trimp from activity a "
            "where a.start_ts is not null "
            "and exists (select 1 from activity_source s where s.activity_id = a.id "
            "and s.is_canonical = 1)"):
        d = _activity_local_date(conn, r["start_ts"], r["tz_offset_s"], local_tz)
        per_day.setdefault(d, 0.0)
        if r["trimp"] is None:
            unknown_per_day[d] = unknown_per_day.get(d, 0) + 1
        else:
            per_day[d] += r["trimp"]
    conn.execute("delete from daily_load")
    if not per_day:
        return 0
    day = min(per_day)
    last = today or datetime.now(local_tz).date()
    if last < max(per_day):
        last = max(per_day)
    ctl = atl = 0.0
    trimp_complete = True
    n = 0
    while day <= last:
        unknown = unknown_per_day.get(day, 0) > 0
        t = None if unknown else per_day.get(day, 0.0)
        if unknown:
            trimp_complete = False
        if trimp_complete:
            ctl += (t - ctl) / CTL_TAU_DAYS
            atl += (t - atl) / ATL_TAU_DAYS
            pmc = (round(ctl, 1), round(atl, 1), round(ctl - atl, 1))
        else:
            pmc = (None, None, None)
        conn.execute(
            "insert into daily_load (date, trimp, ctl, atl, tsb) values (?, ?, ?, ?, ?)",
            (day.isoformat(), round(t, 1) if t is not None else None) + pmc)
        n += 1
        day += timedelta(days=1)
    return n


def rebuild_daily_load(conn, today=None):
    """由 canonical activity.trimp 全量重算 PMC（派生缓存，可删可重建）。

    指数均值从 0 起步爬升；覆盖序列内无活动日 trimp=0；任一活动缺 TRIMP 的
    日期 trimp=NULL，且首个缺负荷日起 PMC 三值均为 NULL。序列延展到配置时区
    今天。返回写入天数。
    """
    n = _rebuild_daily_load(conn, today=today)
    conn.commit()
    return n


def cmd_rebuild_load(conn):
    n = rebuild_daily_load(conn)
    print("daily_load 已全量重算：{} 天".format(n))
    row = conn.execute(
        "select * from daily_load order by date desc limit 1").fetchone()
    if row:
        print("最新 {}：trimp {} CTL {} ATL {} TSB {}".format(
            row["date"], row["trimp"], row["ctl"], row["atl"], row["tsb"]))


def cmd_config_set(args):
    from ..config import activity_since, configured_timezone, workout_platform

    values = {}
    if args.primary_platform is not None:
        platform = args.primary_platform.strip().lower()
        if not platform:
            print("primary_platform 不能为空")
            return 1
        values["primary_platform"] = platform
    if args.ride_training_min_km is not None:
        if args.ride_training_min_km <= 0:
            print("ride_training_min_km 必须是正数")
            return 1
        values["ride_training_min_km"] = args.ride_training_min_km
    try:
        if args.timezone is not None:
            values["timezone"] = configured_timezone({"timezone": args.timezone})
        if args.activity_since is not None:
            since = activity_since({"activity_since": args.activity_since})
            if since > datetime.now().astimezone().date().isoformat():
                print("activity_since 不能晚于今天")
                return 1
            values["activity_since"] = since
        if args.workout_platform is not None:
            values["workout_platform"] = workout_platform(
                {"workout_platform": args.workout_platform})
    except ValueError as exc:
        print(str(exc))
        return 1
    if not values:
        print("至少设置 --primary-platform、--ride-training-min-km、--timezone、"
              "--activity-since 或 --workout-platform")
        return 1
    path = set_config_values(OPENATHLETE_HOME, **values)
    print("配置已保存：{}".format(path))
    if any(key in values for key in ("ride_training_min_km", "primary_platform", "timezone")):
        print("相关派生数据用 `athlete_db.py ingest --rebuild` 全量重算")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="athlete.db v0：SQLite 分析库 + FIT 归档入库")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("init", help="建库建表")
    p_ing = sub.add_parser("ingest", help="递归扫描规范 .fit 解析入库")
    p_ing.add_argument("--limit", type=int, help="只处理前 N 个 FIT")
    p_ing.add_argument("--rebuild", action="store_true", help="重建 FIT 派生表后全量入库")
    p_anchor = sub.add_parser("anchor", help="阈值锚点")
    anchor_sub = p_anchor.add_subparsers(dest="anchor_cmd")
    p_set = anchor_sub.add_parser("set", help="写入锚点版本")
    p_set.add_argument("--sport", required=True, help="run / ride / swim / ...")
    p_set.add_argument("--valid-from", required=True, help="生效起日 YYYY-MM-DD")
    p_set.add_argument("--valid-to", help="生效止日 YYYY-MM-DD（缺省至今）")
    p_set.add_argument("--lt1-hr", type=float)
    p_set.add_argument("--lt2-hr", type=float)
    p_set.add_argument("--max-hr", type=float)
    p_set.add_argument("--resting-hr", type=float)
    p_set.add_argument("--threshold-pace-s", type=float, help="阈值配速 s/km")
    p_set.add_argument("--ftp-w", type=float)
    p_set.add_argument("--critical-speed-mps", type=float)
    p_set.add_argument("--source", help="来源（manual / garmin / coros / race 等）")
    p_set.add_argument("--note")
    anchor_sub.add_parser("list", help="列出锚点")
    p_config = sub.add_parser("config", help="非密配置")
    config_sub = p_config.add_subparsers(dest="config_cmd")
    p_config_set = config_sub.add_parser("set", help="保存非密配置")
    p_config_set.add_argument("--primary-platform")
    p_config_set.add_argument("--ride-training-min-km", type=float)
    p_config_set.add_argument("--timezone", help="IANA 时区，如 Asia/Shanghai")
    p_config_set.add_argument("--activity-since", help="首次活动同步起点 YYYY-MM-DD")
    p_config_set.add_argument("--workout-platform", help="默认课表写回平台 coros / garmin")
    sub.add_parser("stats", help="按运动类型/结构类型计数与未入库清单")
    sub.add_parser("rebuild-load", help="由 activity.trimp 全量重算 PMC（daily_load）")
    args = ap.parse_args(argv)

    (OPENATHLETE_HOME / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(OPENATHLETE_HOME / "logs" / "athlete-db.log"),
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "init":
        OPENATHLETE_HOME.mkdir(parents=True, exist_ok=True)
        cmd_init(connect(DB_PATH), DB_PATH)
        return 0
    if not DB_PATH.exists():
        print("库不存在，先跑 init：uv run ./athlete_db.py init")
        return 1
    conn = connect(DB_PATH)
    init_db(conn)
    if args.cmd == "ingest":
        return cmd_ingest(conn, FIT_DIR, args.limit, args.rebuild)
    if args.cmd == "anchor":
        if args.anchor_cmd == "set":
            return cmd_anchor_set(conn, args) or 0
        if args.anchor_cmd == "list":
            cmd_anchor_list(conn)
            return 0
        p_anchor.print_help()
        return 2
    if args.cmd == "config":
        if args.config_cmd == "set":
            return cmd_config_set(args)
        p_config.print_help()
        return 2
    if args.cmd == "stats":
        return cmd_stats(conn, FIT_DIR) or 0
    if args.cmd == "rebuild-load":
        return cmd_rebuild_load(conn) or 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
