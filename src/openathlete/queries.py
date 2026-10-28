"""本地分析查询；同步触发与 envelope 由 MCP 工具层负责。"""
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone

from .config import (
    configured_timezone, configured_tzinfo, load_config, primary_platform,
    timezone_source,
)
from .db.athlete_db import connect, find_anchor, init_db
from .home import athlete_db_path


def resolve_window(home, days, end_date=None, start_date=None):
    if start_date is not None:
        start = _parse_date(start_date)
        end = _parse_date(end_date) if end_date else _today(home)
    else:
        end = _parse_date(end_date) if end_date else _today(home)
        start = end - timedelta(days=days - 1)
    if start > end:
        raise ValueError("start_date 不能晚于 end_date")
    return start.isoformat(), end.isoformat()


def _today(home):
    return datetime.now(configured_tzinfo(load_config(home))).date()


def _parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "year") and not isinstance(value, str):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _connection(home):
    conn = connect(athlete_db_path(home))
    init_db(conn)
    return conn


def _local_date(start_ts, offset, config):
    if offset is not None:
        return datetime.fromtimestamp(start_ts + offset, timezone.utc).date().isoformat()
    return datetime.fromtimestamp(start_ts, configured_tzinfo(config)).date().isoformat()


def _in_window(row, start, end, config):
    if row["start_ts"] is None:
        return False
    return start <= _local_date(row["start_ts"], row["tz_offset_s"], config) <= end


def _activity_rows(conn, config, start, end, sport=None):
    columns = (
        "a.*, s.platform, s.platform_activity_id, s.fit_sha256"
    )
    rows = conn.execute(
        "select {} from activity a join activity_source s on s.activity_id=a.id "
        "where s.is_canonical=1 and a.start_ts is not null "
        "order by a.start_ts desc, a.id desc".format(columns)).fetchall()
    selected = [
        row for row in rows
        if _in_window(row, start, end, config) and (sport is None or row["sport"] == sport)
    ]
    conflicts = conn.execute(
        "select a.id, a.start_ts, a.tz_offset_s from activity a "
        "where a.start_ts is not null and not exists ("
        "select 1 from activity_source s where s.activity_id=a.id and s.is_canonical=1)"
    ).fetchall()
    excluded = sum(1 for row in conflicts if _in_window(row, start, end, config))
    return selected, excluded


def _activity_dict(row, detail=False):
    distance = row["distance_m"]
    duration = row["duration_s"]
    value = {
        "activity_id": row["id"],
        "start_ts": row["start_ts"],
        "tz_offset_s": row["tz_offset_s"],
        "sport": row["sport"],
        "sub_sport": row["sub_sport"],
        "duration_s": duration,
        "distance_m": distance,
        "ascent_m": row["ascent_m"],
        "avg_hr_bpm": row["avg_hr"],
        "max_hr_bpm": row["max_hr"],
        "avg_power_w": row["avg_power_w"],
        "avg_cadence_per_min": row["avg_cadence_per_min"],
        "calories_kcal": row["calories"],
        "avg_speed_mps": row["avg_speed_mps"],
        "pace_s_per_km": (duration * 1000.0 / distance)
        if duration is not None and distance else None,
        "trimp": row["trimp"],
        "type": row["type"],
    }
    if detail:
        value.update({
            "structure": json.loads(row["structure_json"]) if row["structure_json"] else None,
            "zone_boundaries": json.loads(row["zone_boundaries_json"])
            if row["zone_boundaries_json"] else None,
            "canonical_source": {
                "platform": row["platform"],
                "platform_activity_id": row["platform_activity_id"],
                "fit_sha256": row["fit_sha256"],
            },
        })
    return value


def _base_coverage(home, start, end):
    config = load_config(home)
    return {
        "requested_start": start,
        "requested_end": end,
        "actual_start": start,
        "actual_end": end,
        "sources": ["athlete.db"],
        "activity_count": 0,
        "canonical_count": 0,
        "excluded_conflict_count": 0,
        "trimp_count": 0,
        "zone_count": 0,
        "complete": False,
        "missing": [],
        "stale": True,
        "last_sync_ts": None,
        "timezone_source": timezone_source(config),
    }


def query_activities(home, start, end, sport=None, limit=20, detail=False):
    config = load_config(home)
    with closing(_connection(home)) as conn:
        rows, excluded = _activity_rows(conn, config, start, end, sport)
        zone_counts = {}
        if detail and rows:
            ids = [row["id"] for row in rows]
            marks = ",".join("?" for _ in ids)
            zone_counts = {
                row["activity_id"]: [
                    {"zone": item["zone"], "time_s": item["time_s"]}
                    for item in conn.execute(
                        "select zone,time_s from time_in_zone where activity_id=? order by zone",
                        (row["activity_id"],)).fetchall()
                ]
                for row in conn.execute(
                    "select distinct activity_id from time_in_zone where activity_id in ({})"
                    .format(marks), ids).fetchall()
            }
    returned = rows[:limit]
    activities = [_activity_dict(row, detail) for row in returned]
    if detail:
        for item in activities:
            item["time_in_zone"] = zone_counts.get(item["activity_id"], [])
    canonical_count = len(rows)
    coverage = _base_coverage(home, start, end)
    coverage.update({
        "activity_count": canonical_count,
        "canonical_count": canonical_count,
        "excluded_conflict_count": excluded,
        "trimp_count": sum(row["trimp"] is not None for row in rows),
        "unknown_trimp_count": sum(row["trimp"] is None for row in rows),
        "zone_count": sum(row["zone_boundaries_json"] is not None for row in rows),
        "timezone_estimated": any(row["tz_offset_s"] is None for row in rows),
        "actual_start": min(
            (_local_date(row["start_ts"], row["tz_offset_s"], config) for row in rows),
            default=None,
        ),
        "actual_end": max(
            (_local_date(row["start_ts"], row["tz_offset_s"], config) for row in rows),
            default=None,
        ),
    })
    if not rows:
        coverage["missing"].append("activities")
    return {
        "activities": activities,
        "returned": len(activities),
        "truncated": len(rows) > len(returned),
    }, coverage


def _anchor_coverage(conn, config, start, end):
    rows, _ = _activity_rows(conn, config, start, end)
    counts = {key: 0 for key in ("max_hr", "resting_hr", "lt1_hr", "lt2_hr")}
    for row in rows:
        anchor = find_anchor(
            conn, row["sport"], _local_date(row["start_ts"], row["tz_offset_s"], config))
        if not anchor:
            continue
        for key in counts:
            if anchor[key] is not None:
                counts[key] += 1
    total = len(rows)
    trimp_count = sum(row["trimp"] is not None for row in rows)
    return {
        "activity_count": total,
        "trimp_count": trimp_count,
        "unknown_trimp_count": total - trimp_count,
        **{key: {"covered": value, "total": total} for key, value in counts.items()},
    }


def get_training_load(home, start, end):
    config = load_config(home)
    required_start = (_parse_date(start) - timedelta(days=42)).isoformat()
    with closing(_connection(home)) as conn:
        rows = conn.execute(
            "select date,trimp,ctl,atl,tsb from daily_load "
            "where date between ? and ? order by date", (start, end)).fetchall()
        latest_row = conn.execute(
            "select date,trimp,ctl,atl,tsb from daily_load where date<=? "
            "and ctl is not null and atl is not null and tsb is not null "
            "order by date desc limit 1", (end,)).fetchone()
        earliest = conn.execute(
            "select min(date) d from daily_load where ctl is not null").fetchone()["d"]
        unknown_days = conn.execute(
            "select count(*) c from daily_load where date between ? and ? and "
            "(trimp is null or ctl is null or atl is null or tsb is null)",
            (required_start, end)).fetchone()["c"]
        anchors = _anchor_coverage(conn, config, required_start, end)
        activity_rows, excluded = _activity_rows(conn, config, start, end)
    daily = [dict(row) for row in rows]
    latest = dict(latest_row) if latest_row else None
    headline = "本地负荷数据不足，请先完成同步与锚点配置。"
    if latest:
        headline = "截至 {date}：CTL {ctl:.1f}、ATL {atl:.1f}、TSB {tsb:.1f}。".format(**latest)
        first_valid = next((row for row in daily if row["ctl"] is not None), None)
        if first_valid and first_valid["date"] != latest["date"]:
            headline += " 窗口内 CTL 变化 {:+.1f}。".format(
                latest["ctl"] - first_valid["ctl"])
    coverage = _base_coverage(home, start, end)
    coverage.update({
        "activity_count": len(activity_rows),
        "canonical_count": len(activity_rows),
        "excluded_conflict_count": excluded,
        "trimp_count": sum(row["trimp"] is not None for row in activity_rows),
        "unknown_trimp_count": sum(row["trimp"] is None for row in activity_rows),
        "zone_count": sum(row["zone_boundaries_json"] is not None for row in activity_rows),
        "timezone_estimated": any(row["tz_offset_s"] is None for row in activity_rows),
        "actual_start": earliest,
        "actual_end": latest["date"] if latest else None,
        "anchors": anchors,
        "pmc": {
            "required_start": required_start,
            "actual_start": earliest,
            "actual_end": latest["date"] if latest else None,
            "unknown_days": unknown_days,
            "initialized": False,
        },
        "pmc_required_start": required_start,
        "pmc_initialized": False,
    })
    if anchors["activity_count"] and not daily:
        coverage["missing"].append("daily_load")
    if unknown_days or anchors["unknown_trimp_count"]:
        coverage["missing"].append("trimp")
    if any(anchors[key]["covered"] < anchors[key]["total"]
           for key in ("max_hr", "resting_hr")):
        coverage["missing"].append("threshold_anchors")
    return {"daily": daily, "latest": latest, "headline": headline}, coverage


def _recovery_source(row):
    value = {
        "date": row["date"],
        "source": row["source"],
        "hrv_ms": row["hrv_ms"],
        "resting_hr_bpm": row["resting_hr"],
        "calories_kcal": row["calories"],
    }
    for field in ("sleep_total_s", "sleep_deep_s", "sleep_light_s", "sleep_rem_s",
                  "sleep_awake_s", "sleep_score", "spo2_pct", "stress_avg",
                  "body_battery", "steps"):
        value[field] = row[field]
    return value


def get_recovery_status(home, date):
    config = load_config(home)
    with closing(_connection(home)) as conn:
        rows = conn.execute(
            "select * from daily_metrics where date=? order by source", (date,)).fetchall()
        latest = conn.execute(
            "select max(date) d from daily_metrics where date<=? and "
            "(resting_hr is not null or hrv_ms is not null)", (date,)).fetchone()["d"]
    primary = primary_platform(config)
    selected = next((row for row in rows if row["source"] == primary), None)
    selected = selected or (rows[0] if rows else None)
    data = {
        "date": date,
        "recovery_pct": None,
        "hrv_ms": selected["hrv_ms"] if selected else None,
        "resting_hr_bpm": selected["resting_hr"] if selected else None,
        "source": selected["source"] if selected else None,
        "calories_kcal": selected["calories"] if selected else None,
        "missing": [],
        "sources": [_recovery_source(row) for row in rows],
    }
    for field in ("sleep_total_s", "sleep_deep_s", "sleep_light_s", "sleep_rem_s",
                  "sleep_awake_s", "sleep_score", "spo2_pct", "stress_avg",
                  "body_battery", "steps"):
        data[field] = selected[field] if selected else None
    required = ["recovery_pct", "hrv_ms", "resting_hr_bpm", "sleep_total_s",
                "sleep_deep_s", "sleep_light_s", "sleep_rem_s", "sleep_awake_s",
                "sleep_score"]
    data["missing"] = [field for field in required if data.get(field) is None]
    coverage = _base_coverage(home, date, date)
    coverage.update({
        "sources": ["athlete.db", "coros-web"],
        "actual_start": date if selected else None,
        "actual_end": date if selected else None,
        "latest_metric_date": latest,
        "missing": list(data["missing"]),
    })
    if not selected:
        coverage["missing"].append("daily_metrics")
    return data, coverage


def get_overview(home, start, end, sync_coverage=None):
    config = load_config(home)
    with closing(_connection(home)) as conn:
        rows, excluded = _activity_rows(conn, config, start, end)
        load_rows = conn.execute(
            "select date,trimp,ctl,atl,tsb from daily_load where date between ? and ?",
            (start, end)).fetchall()
    complete = bool((sync_coverage or {}).get("complete"))
    activity_dates = {
        _local_date(row["start_ts"], row["tz_offset_s"], config) for row in rows
    }
    trimp_days = {
        _local_date(row["start_ts"], row["tz_offset_s"], config)
        for row in rows if row["trimp"] is not None
    }
    by_day = {}
    for row in rows:
        date = _local_date(row["start_ts"], row["tz_offset_s"], config)
        item = by_day.setdefault(date, {
            "date": date, "activity_count": 0, "duration_s": 0.0,
            "distance_m": 0.0, "trimp": 0.0, "unknown_trimp_count": 0,
            "ctl": None, "atl": None, "tsb": None,
        })
        item["activity_count"] += 1
        item["duration_s"] += row["duration_s"] or 0
        item["distance_m"] += row["distance_m"] or 0
        if row["trimp"] is not None:
            item["trimp"] += row["trimp"]
        else:
            item["unknown_trimp_count"] += 1
    for row in load_rows:
        item = by_day.setdefault(row["date"], {
            "date": row["date"], "activity_count": 0, "duration_s": 0.0,
            "distance_m": 0.0, "trimp": row["trimp"], "unknown_trimp_count": 0,
            "ctl": row["ctl"], "atl": row["atl"], "tsb": row["tsb"],
        })
        item.update({"ctl": row["ctl"], "atl": row["atl"], "tsb": row["tsb"]})
        if row["date"] not in activity_dates:
            item["trimp"] = row["trimp"]
    daily = []
    day = _parse_date(start)
    last = _parse_date(end)
    while day <= last:
        key = day.isoformat()
        item = by_day.get(key)
        if item is None:
            item = {"date": key, "activity_count": 0, "duration_s": 0.0,
                    "distance_m": 0.0, "trimp": 0.0 if complete else None,
                    "unknown_trimp_count": 0, "ctl": None, "atl": None, "tsb": None}
        if item["unknown_trimp_count"] or (
                not complete and key not in trimp_days):
            item["trimp"] = None
        daily.append(item)
        day += timedelta(days=1)
    trimp_values = [row["trimp"] for row in rows if row["trimp"] is not None]
    unknown_trimp_count = sum(row["trimp"] is None for row in rows)
    if unknown_trimp_count:
        total_trimp = None
    elif trimp_values:
        total_trimp = sum(trimp_values)
    else:
        total_trimp = 0.0 if complete else None
    training = {
        "activity_count": len(rows),
        "duration_s": sum(row["duration_s"] or 0 for row in rows),
        "distance_m": sum(row["distance_m"] or 0 for row in rows),
        "trimp": total_trimp,
        "unknown_trimp_count": unknown_trimp_count,
    }
    latest_load = max(
        (dict(row) for row in load_rows if row["ctl"] is not None),
        key=lambda row: row["date"], default=None)
    recovery, recovery_coverage = get_recovery_status(home, end)
    from .memory import parse_next_race
    next_race, race_warnings = parse_next_race(home, _parse_date(end))
    coverage = _base_coverage(home, start, end)
    coverage.update({
        "activity_count": len(rows),
        "canonical_count": len(rows),
        "excluded_conflict_count": excluded,
        "trimp_count": len(trimp_values),
        "unknown_trimp_count": unknown_trimp_count,
        "zone_count": sum(row["zone_boundaries_json"] is not None for row in rows),
        "timezone_estimated": any(row["tz_offset_s"] is None for row in rows),
        "recovery": recovery_coverage,
        "warnings": race_warnings,
    })
    if not rows:
        coverage["missing"].append("activities")
    if unknown_trimp_count:
        coverage["missing"].append("trimp")
    data = {
        "window": {"start_date": start, "end_date": end,
                   "timezone": configured_timezone(config)},
        "training": training,
        "daily": daily,
        "latest_load": latest_load,
        "recovery": recovery,
        "next_race": next_race,
        "sync": sync_coverage or {},
    }
    return data, coverage


def get_current_plan(home, date):
    from .memory import get_current_plan as read_plan
    data = read_plan(home, date)
    coverage = _base_coverage(home, date, date)
    coverage.update({"sources": ["filesystem"], "complete": True, "stale": False,
                     "actual_start": date, "actual_end": date})
    return data, coverage
