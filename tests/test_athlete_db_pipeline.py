# -*- coding: utf-8 -*-
"""athlete_db 数据完整性、daily_metrics 与 daily_load 测试。"""
from datetime import datetime, timedelta, timezone

from openathlete.db import athlete_db
from openathlete.db.athlete_db import (
    connect, find_canonical_near, init_db, ingest_fit_payload,
    rebuild_daily_load, upsert_daily_metrics,
)
from openathlete.structure import Lap

DAY0 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=timezone.utc)
TS0 = int(DAY0.timestamp())


def mem_db():
    conn = connect(":memory:")
    init_db(conn)
    return conn


def fake_msgs(start=DAY0, distance=10000.0, duration=3000.0, avg_hr=150,
              max_hr=170, laps=None, records=None):
    return {
        "sessions": [{"start_time": start, "sport": "running", "sub_sport": "generic",
                      "total_distance": distance, "total_elapsed_time": duration,
                      "total_ascent": 10.0, "avg_heart_rate": avg_hr,
                      "max_heart_rate": max_hr, "avg_power": None, "avg_cadence": 180,
                      "total_calories": 600}],
        "laps": laps or [], "records": records or [], "tz_offset_s": 0,
        "workout_name": None, "workout_step_count": 0,
    }


def run_lap(dist, pace_s, hr=160):
    return Lap(dist_m=dist, time_s=pace_s * dist / 1000, pace=float(pace_s),
               avg_hr=hr, trigger="distance")


def canonical_platform(conn):
    row = conn.execute(
        "select platform from activity_source where is_canonical = 1").fetchone()
    return row["platform"] if row else None


def test_identity_window_boundary(monkeypatch):
    conn = mem_db()
    monkeypatch.setattr(athlete_db, "decode_fit", lambda _raw: fake_msgs())
    assert ingest_fit_payload(conn, b"F", "garmin", "g1", "x") == (1, 0, 0)
    near = find_canonical_near(conn, TS0 + athlete_db.IDENTITY_WINDOW_S)
    assert near is not None
    assert find_canonical_near(conn, TS0 + athlete_db.IDENTITY_WINDOW_S + 1) is None
    assert find_canonical_near(conn, None) is None


def test_hash_fork_without_usable_primary_reports_conflict(monkeypatch):
    decoded = {b"G": fake_msgs(), b"C": fake_msgs(distance=12000.0)}
    monkeypatch.setattr(athlete_db, "decode_fit", lambda raw: decoded[raw])
    for primary in (None, "zwift"):
        conn = mem_db()
        review = []
        ingest_fit_payload(conn, b"G", "garmin", "g1", "fit/g1.fit")
        ingest_fit_payload(conn, b"C", "coros", "c1", "fit/c1.fit",
                           primary_platform_name=primary, review=review)
        activity_id = conn.execute("select id from activity").fetchone()["id"]
        assert review == [{"type": "canonical_conflict", "activity_id": activity_id}]
        assert canonical_platform(conn) is None
        assert find_canonical_near(conn, TS0) == activity_id


def test_existing_noncanonical_winner_rebuilds_activity_and_load(monkeypatch, tmp_path):
    continuous_records = [(TS0 + i, 130) for i in range(601)]
    interval_records = [(TS0 + i, 180) for i in range(601)]
    interval_laps = []
    for i in range(5):
        interval_laps.append(run_lap(1200, 210 + i % 2, hr=170))
        if i < 4:
            interval_laps.append(run_lap(140, 450, hr=150))
    decoded = {
        b"C": fake_msgs(distance=10000.0, duration=2300.0, avg_hr=130, max_hr=190,
                        laps=[(TS0 + i, run_lap(1000, 230, hr=130)) for i in range(10)],
                        records=continuous_records),
        b"G": fake_msgs(distance=6560.0, duration=1800.0, avg_hr=170, max_hr=190,
                        laps=[(TS0 + i, lap) for i, lap in enumerate(interval_laps)],
                        records=interval_records),
        b"Z": fake_msgs(distance=5000.0),
    }
    monkeypatch.setattr(athlete_db, "decode_fit", lambda raw: decoded[raw])
    fit_dir = tmp_path / "fit"
    fit_dir.mkdir()
    for name, raw in (("c1.fit", b"C"), ("g1.fit", b"G"), ("z1.fit", b"Z")):
        (fit_dir / name).write_bytes(raw)

    conn = mem_db()
    conn.execute(
        "insert into threshold_anchor (sport, valid_from, lt1_hr, lt2_hr, max_hr, resting_hr) "
        "values ('run', '2026-01-01', 140, 170, 190, 50)")
    ingest_fit_payload(conn, b"C", "coros", "c1", "fit/c1.fit", archive_root=tmp_path)
    continuous_trimp = athlete_db.edwards_trimp(continuous_records, 50, 190)
    assert conn.execute(
        "select trimp from daily_load where date = '2026-10-01'").fetchone()[
            "trimp"] == continuous_trimp
    review = []
    ingest_fit_payload(conn, b"G", "garmin", "g1", "fit/g1.fit",
                       archive_root=tmp_path, review=review)
    assert canonical_platform(conn) is None
    assert conn.execute("select type from activity").fetchone()["type"] == "continuous"
    assert conn.execute("select count(*) c from daily_load").fetchone()["c"] == 0

    # 第三来源让冲突活动再次被找到；主平台当选的是已有非 canonical 来源。
    assert ingest_fit_payload(
        conn, b"Z", "zwift", "z1", "fit/z1.fit", archive_root=tmp_path,
        primary_platform_name="garmin") == (0, 1, 0)
    assert canonical_platform(conn) == "garmin"
    assert conn.execute("select count(*) c from activity").fetchone()["c"] == 1
    activity = conn.execute("select * from activity").fetchone()
    assert activity["distance_m"] == 6560.0
    assert activity["duration_s"] == 1800.0
    assert activity["type"] == "intervals" and activity["rep_count"] == 5
    assert activity["trimp"] == athlete_db.edwards_trimp(interval_records, 50, 190)
    expected_tiz = athlete_db.time_in_zone(
        interval_records, athlete_db.zone_boundaries(140, 170))
    actual_tiz = [r["time_s"] for r in conn.execute(
        "select time_s from time_in_zone where activity_id = ? order by zone",
        (activity["id"],))]
    assert actual_tiz == expected_tiz
    assert conn.execute(
        "select trimp from daily_load where date = '2026-10-01'").fetchone()[
            "trimp"] == activity["trimp"]


def test_single_session_segment_files_keep_segment_identity(monkeypatch):
    decoded = {
        b"S1": fake_msgs(start=DAY0, distance=800.0, duration=40.0),
        b"S2": fake_msgs(start=DAY0 + timedelta(seconds=45), distance=900.0, duration=45.0),
    }
    monkeypatch.setattr(athlete_db, "decode_fit", lambda raw: decoded[raw])
    conn = mem_db()
    assert ingest_fit_payload(conn, b"S1", "garmin", "parent-g#1", "fit/g1.fit") == (1, 0, 0)
    assert ingest_fit_payload(conn, b"S2", "garmin", "parent-g#2", "fit/g2.fit") == (1, 0, 0)
    assert ingest_fit_payload(conn, b"S1", "coros", "parent-c#1", "fit/c1.fit") == (0, 1, 0)
    assert ingest_fit_payload(conn, b"S2", "coros", "parent-c#2", "fit/c2.fit") == (0, 1, 0)
    assert conn.execute("select count(*) c from activity").fetchone()["c"] == 2
    pairs = conn.execute(
        "select platform, platform_activity_id, activity_id from activity_source "
        "order by platform, platform_activity_id").fetchall()
    ids = {(r["platform"], r["platform_activity_id"]): r["activity_id"] for r in pairs}
    assert ids[("garmin", "parent-g#1")] == ids[("coros", "parent-c#1")]
    assert ids[("garmin", "parent-g#2")] == ids[("coros", "parent-c#2")]
    assert ids[("garmin", "parent-g#1")] != ids[("garmin", "parent-g#2")]


def test_ride_threshold_review(monkeypatch):
    msgs = fake_msgs(distance=5000.0, duration=1200.0)
    msgs["sessions"][0].update({"sport": "cycling", "sub_sport": "road"})
    monkeypatch.setattr(athlete_db, "decode_fit", lambda _raw: msgs)
    conn = mem_db()
    review = []
    assert ingest_fit_payload(conn, b"R", "garmin", "r1", "fit/r1.fit",
                              ride_min_km=15.0, ride_confirmed=False,
                              review=review) == (1, 0, 0)
    activity_id = conn.execute("select id from activity").fetchone()["id"]
    assert review == [{"type": "ride_threshold", "activity_id": activity_id,
                       "platform_activity_id": "r1"}]


def test_upsert_daily_metrics_coalesce():
    conn = mem_db()
    upsert_daily_metrics(conn, [
        {"date": "2026-10-01", "source": "coros", "resting_hr": 50, "hrv_ms": 80}])
    # 同 (date, source) 再来：None 字段不覆盖旧值，非 None 字段更新
    upsert_daily_metrics(conn, [
        {"date": "2026-10-01", "source": "coros", "resting_hr": None, "hrv_ms": 82,
         "sleep_total_s": 27000}])
    # 异来源同日是另一行
    upsert_daily_metrics(conn, [
        {"date": "2026-10-01", "source": "garmin", "resting_hr": 51}])
    rows = conn.execute(
        "select * from daily_metrics order by source").fetchall()
    assert len(rows) == 2
    coros = [r for r in rows if r["source"] == "coros"][0]
    assert coros["resting_hr"] == 50 and coros["hrv_ms"] == 82
    assert coros["sleep_total_s"] == 27000
    garmin = [r for r in rows if r["source"] == "garmin"][0]
    assert garmin["resting_hr"] == 51 and garmin["hrv_ms"] is None


def _insert_activity(conn, ts, trimp, canonical=True):
    activity_id = conn.execute(
        "insert into activity (start_ts, trimp) values (?, ?)", (ts, trimp)).lastrowid
    conn.execute(
        "insert into activity_source (activity_id, platform, platform_activity_id, fit_path, "
        "is_canonical) values (?, 'test', ?, 'fit/test.fit', ?)",
        (activity_id, str(activity_id), int(canonical)))
    return activity_id


def test_rebuild_daily_load_pmc_and_rest_day_decay():
    conn = mem_db()
    for i in range(3):
        _insert_activity(conn, TS0 + i * 86400, 100.0)
    today = (DAY0 + timedelta(days=2)).date()
    assert rebuild_daily_load(conn, today=today) == 3
    rows = conn.execute("select * from daily_load order by date").fetchall()
    ctl = atl = 0.0
    for r in rows:
        ctl += (100.0 - ctl) / 42
        atl += (100.0 - atl) / 7
        assert r["trimp"] == 100.0
        assert r["ctl"] == round(ctl, 1)
        assert r["atl"] == round(atl, 1)
        assert r["tsb"] == round(ctl - atl, 1)

    conn = mem_db()
    _insert_activity(conn, TS0, 420.0)
    today = (DAY0 + timedelta(days=4)).date()
    assert rebuild_daily_load(conn, today=today) == 5
    rows = conn.execute("select * from daily_load order by date").fetchall()
    assert rows[-1]["trimp"] == 0.0
    assert rows[-1]["ctl"] < rows[0]["ctl"]
    assert rows[0]["date"] == "2026-10-01" and rows[-1]["date"] == "2026-10-05"


def test_rebuild_daily_load_requires_canonical_source():
    conn = mem_db()
    _insert_activity(conn, TS0, 100.0)
    _insert_activity(conn, TS0, 500.0, canonical=False)
    conn.execute("insert into activity (start_ts, trimp) values (?, 900)", (TS0,))
    assert rebuild_daily_load(conn, today=DAY0.date()) == 1
    row = conn.execute("select * from daily_load").fetchone()
    assert row["trimp"] == 100.0


def test_unknown_trimp_blocks_later_pmc_until_complete_rebuild():
    conn = mem_db()
    _insert_activity(conn, TS0, 100.0)
    unknown_id = _insert_activity(conn, TS0 + 86400, None)
    _insert_activity(conn, TS0 + 2 * 86400, 200.0)
    today = (DAY0 + timedelta(days=3)).date()

    assert rebuild_daily_load(conn, today=today) == 4
    rows = conn.execute("select * from daily_load order by date").fetchall()
    ctl = 100.0 / 42
    atl = 100.0 / 7
    assert rows[0]["trimp"] == 100.0
    assert (rows[0]["ctl"], rows[0]["atl"], rows[0]["tsb"]) == (
        round(ctl, 1), round(atl, 1), round(ctl - atl, 1))
    assert rows[1]["trimp"] is None
    assert rows[2]["trimp"] == 200.0
    assert rows[3]["trimp"] == 0.0
    for row in rows[1:]:
        assert (row["ctl"], row["atl"], row["tsb"]) == (None, None, None)

    conn.execute("update activity set trimp = 80 where id = ?", (unknown_id,))
    assert rebuild_daily_load(conn, today=today) == 4
    rows = conn.execute("select * from daily_load order by date").fetchall()
    assert rows[1]["trimp"] == 80.0
    for row in rows:
        assert row["ctl"] is not None
        assert row["atl"] is not None
        assert row["tsb"] is not None


def test_same_day_partial_unknown_trimp_makes_daily_total_unknown():
    conn = mem_db()
    _insert_activity(conn, TS0, 100.0)
    _insert_activity(conn, TS0 + 60, None)
    _insert_activity(conn, TS0 + 120, 50.0)

    assert rebuild_daily_load(conn, today=DAY0.date()) == 1
    row = conn.execute("select * from daily_load").fetchone()
    assert row["trimp"] is None
    assert (row["ctl"], row["atl"], row["tsb"]) == (None, None, None)
