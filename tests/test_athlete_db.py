# -*- coding: utf-8 -*-
"""athlete_db 纯逻辑单测（合成数据 + 内存库，不读真实 FIT/真实 home）。"""
import sqlite3
from datetime import datetime, timezone

import pytest

from openathlete.db.athlete_db import (
    edwards_trimp, find_anchor, in_window, init_db,
    normalize_sport, parse_structure, session_windows, time_in_zone,
    zone_boundaries, build_activity_row, connect, tz_offset_from_activity,
)
from openathlete.structure import Lap


def test_normalize_sport():
    assert normalize_sport("running", "track") == ("run", "track")
    assert normalize_sport("running", "trail") == ("run", "trail")
    assert normalize_sport("running", "treadmill") == ("run", "indoor")
    assert normalize_sport("running", "generic") == ("run", None)
    assert normalize_sport("cycling", "indoor_cycling") == ("ride", "indoor")
    assert normalize_sport("cycling", "generic") == ("ride", None)
    assert normalize_sport("swimming", "lap_swimming") == ("swim", "pool")
    assert normalize_sport("training", "cardio_training") == \
        ("training", "cardio_training")
    assert normalize_sport(None, None) == (None, None)


def mem_db():
    conn = connect(":memory:")
    init_db(conn)
    return conn


def test_init_db_migrates_hash_and_enforces_single_canonical():
    conn = connect(":memory:")
    conn.execute("""
        create table activity_source (
          id integer primary key,
          activity_id integer,
          platform text, platform_activity_id text, fit_path text,
          is_canonical integer, note text,
          unique(platform, platform_activity_id)
        )
    """)
    init_db(conn)
    init_db(conn)
    columns = {r["name"] for r in conn.execute("pragma table_info(activity_source)")}
    assert "fit_sha256" in columns

    activity_id = conn.execute("insert into activity default values").lastrowid
    conn.execute(
        "insert into activity_source (activity_id, platform, platform_activity_id, "
        "fit_sha256, is_canonical) values (?, 'garmin', 'g1', 'abc', 1)",
        (activity_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "insert into activity_source (activity_id, platform, platform_activity_id, "
            "is_canonical) values (?, 'coros', 'c1', 1)", (activity_id,))
    conn.execute(
        "insert into activity_source (activity_id, platform, platform_activity_id, "
        "is_canonical) values (?, 'coros', 'c1', 0)", (activity_id,))
    assert conn.execute(
        "select count(*) c from activity_source where activity_id = ?",
        (activity_id,)).fetchone()["c"] == 2


def add_anchor(conn, sport, valid_from, **kw):
    cols = {"lt1_hr": None, "lt2_hr": None, "max_hr": None, "resting_hr": None,
            "threshold_pace_s": None, "ftp_w": None, "critical_speed_mps": None,
            "valid_to": None, "source": "test", "note": None}
    cols.update(kw)
    conn.execute(
        "insert into threshold_anchor (sport, valid_from, valid_to, lt1_hr, lt2_hr, "
        "max_hr, resting_hr, threshold_pace_s, ftp_w, critical_speed_mps, source, "
        "note) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sport, valid_from, cols["valid_to"], cols["lt1_hr"], cols["lt2_hr"],
         cols["max_hr"], cols["resting_hr"], cols["threshold_pace_s"], cols["ftp_w"],
         cols["critical_speed_mps"], cols["source"], cols["note"]))
    conn.commit()


def test_anchor_fallback_run_ride_not_swim():
    conn = mem_db()
    add_anchor(conn, "run", "2026-01-01", lt1_hr=150, lt2_hr=170)
    # 骑行无锚点 → 回退跑步
    assert find_anchor(conn, "ride", "2026-06-01")["lt1_hr"] == 150
    # 游泳不回退
    assert find_anchor(conn, "swim", "2026-06-01") is None
    # 生效日前无锚点
    assert find_anchor(conn, "run", "2025-12-31") is None
    # 新版本覆盖
    add_anchor(conn, "run", "2026-06-01", lt1_hr=155, lt2_hr=172)
    assert find_anchor(conn, "run", "2026-07-01")["lt1_hr"] == 155
    assert find_anchor(conn, "run", "2026-05-01")["lt1_hr"] == 150
    # valid_to 截断
    add_anchor(conn, "ride", "2026-01-01", valid_to="2026-03-31", lt1_hr=140)
    assert find_anchor(conn, "ride", "2026-02-01")["lt1_hr"] == 140
    assert find_anchor(conn, "ride", "2026-06-01")["lt1_hr"] == 155  # 过期回退跑步新版


def test_edwards_trimp_math():
    # resting 50 / max 190 → HRR 140，边界 [120,134,148,162,176]
    # hr 154 = 74.3%HRR → 第 3 区权重 3；601 条 1s 记录 = 600s = 10min → 30.0
    records = [(1000 + i, 154) for i in range(601)]
    assert edwards_trimp(records, 50, 190) == 30.0
    # hr 100 < 50%HRR → 权重 1（catch-all）
    records = [(1000 + i, 100) for i in range(601)]
    assert edwards_trimp(records, 50, 190) == 10.0
    # hr 180 ≥ 90%HRR → 权重 5
    records = [(1000 + i, 180) for i in range(601)]
    assert edwards_trimp(records, 50, 190) == 50.0
    # 大间隔按停表封顶 30s：3 对记录各计 30s×权重3 = 4.5
    records = [(1000, 154), (1030, 154), (1330, 154), (1360, 154)]
    assert edwards_trimp(records, 50, 190) == 4.5
    # 输入不足：hrr ≤ 0 或无记录
    assert edwards_trimp(records, 190, 50) is None
    assert edwards_trimp([], 50, 190) is None


def test_time_in_zone_boundaries():
    bounds = zone_boundaries(160, 175)
    assert bounds == [144.0, 160.0, 164.5, 175.0, 180.2, 185.5]
    recs = []
    t = 0
    for hr in (130, 150, 162, 170, 178, 183, 190):
        for _ in range(100):
            recs.append((t, hr))
            t += 1
        recs.append((t, 150))  # 过渡点：前 6 个各向 Z2 贡献 1s，末个无后继
        t += 1
    times = time_in_zone(recs, bounds)
    assert times == [100, 106, 100, 100, 100, 100, 100]


def test_tz_offset_from_activity():
    # 2026-10-07 11:53:23 UTC = 1791374003；local_timestamp 原始值（UTC+8）
    assert tz_offset_from_activity(1791374003, 1160337203) == 28800
    assert tz_offset_from_activity(None, 1160337203) is None
    assert tz_offset_from_activity(1791374003, 0) is None


def test_session_windows_and_in_window():
    def sess(ts):
        return {"start_time": datetime.fromtimestamp(ts, tz=timezone.utc)}
    windows = session_windows([sess(100), sess(200), sess(300)])
    assert windows == [(100, 200), (200, 300), (300, None)]
    assert in_window(150, windows[0]) and not in_window(200, windows[0])
    assert in_window(250, windows[1]) and not in_window(150, windows[1])
    assert in_window(999, windows[2]) and not in_window(None, windows[2])


def run_lap(dist, pace_s, hr=160):
    return Lap(dist_m=dist, time_s=pace_s * dist / 1000, pace=float(pace_s),
               avg_hr=hr, trigger="distance")


def test_parse_structure_dispatch():
    laps = [run_lap(1000, 230) for _ in range(10)]
    session = {"distance_m": 10000.0, "time_s": 2300.0}
    assert parse_structure("run", None, session, laps)["type"] == "continuous"
    assert parse_structure("run", "track", session, laps) is not None
    assert parse_structure("run", "trail", session, laps) is None   # 越野不解析
    assert parse_structure("swim", "pool", session, laps) is None   # 游泳留档
    assert parse_structure("training", "cardio_training", session, laps) is None
    assert parse_structure("run", None, session, []) is None        # 无圈退化
    ride_laps = [Lap(dist_m=5000.0, time_s=800.0, avg_hr=145) for _ in range(4)]
    assert parse_structure("ride", None,
                           {"distance_m": 20000.0, "time_s": 3200.0},
                           ride_laps)["type"] == "continuous"


def test_build_activity_row_with_and_without_anchor():
    conn = mem_db()
    laps = [run_lap(1000, 230) for _ in range(10)]
    records = [(1000 + i, 155) for i in range(2301)]
    sess = {"sport": "running", "sub_sport": "generic",
            "total_distance": 10000.0, "total_elapsed_time": 2300.0,
            "start_time": datetime.fromtimestamp(1000, tz=timezone.utc),
            "avg_heart_rate": 155, "max_heart_rate": 170, "total_ascent": 12,
            "total_calories": 600}
    meta = {"tz_offset_s": 28800, "workout_name": None, "workout_step_count": 0,
            "multisport_segment": None}
    row, tiz = build_activity_row(conn, sess, laps, records, meta, "test#1")
    assert row["sport"] == "run" and row["sub_sport"] is None
    assert row["type"] == "continuous"
    assert row["trimp"] is None and tiz is None          # 无锚点置 NULL
    assert row["start_ts"] == 1000
    assert row["tz_offset_s"] == 28800
    assert row["avg_speed_mps"] == round(10000.0 / 2300.0, 3)
    assert row["structure_json"] is not None

    add_anchor(conn, "run", "1970-01-01", lt1_hr=140, lt2_hr=170,
               max_hr=190, resting_hr=50)
    row, tiz = build_activity_row(conn, sess, laps, records, meta, "test#1")
    assert row["trimp"] is not None and row["trimp"] > 0
    assert tiz is not None and len(tiz) == 7 and sum(tiz) > 0
    assert row["zone_boundaries_json"] is not None
