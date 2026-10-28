# -*- coding: utf-8 -*-
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from openathlete.db import athlete_db
from openathlete.db.athlete_db import (
    build_activity_row, cmd_ingest, cmd_stats, connect,
    find_canonical_near, init_db, ingest_fit_payload, rebuild_daily_load,
)

DAY0 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=timezone.utc)
TS0 = int(DAY0.timestamp())


def mem_db():
    conn = connect(":memory:")
    init_db(conn)
    return conn


def fake_msgs(start=DAY0, distance=10000.0, duration=600.0, hr=130, tz_offset_s=0):
    ts = int(start.timestamp())
    return {
        "sessions": [{
            "start_time": start, "sport": "running", "sub_sport": "generic",
            "total_distance": distance, "total_elapsed_time": duration,
            "total_ascent": 10.0, "avg_heart_rate": hr, "max_heart_rate": 190,
            "avg_power": None, "avg_cadence": 180, "total_calories": 100,
        }],
        "laps": [],
        "records": [(ts + i, hr) for i in range(601)],
        "tz_offset_s": tz_offset_s,
        "workout_name": None,
        "workout_step_count": 0,
    }


def add_anchor(conn, valid_from="2026-01-01"):
    conn.execute(
        "insert into threshold_anchor (sport, valid_from, lt1_hr, lt2_hr, max_hr, resting_hr) "
        "values ('run', ?, 140, 170, 190, 50)", (valid_from,))


def add_source(conn, start_ts, trimp, fit_path, fit_sha256, tz_offset_s=None):
    activity_id = conn.execute(
        "insert into activity (start_ts, tz_offset_s, trimp) values (?, ?, ?)",
        (start_ts, tz_offset_s, trimp)).lastrowid
    source_id = conn.execute(
        "insert into activity_source (activity_id, platform, platform_activity_id, fit_path, "
        "fit_sha256, is_canonical) values (?, 'test', ?, ?, ?, 1)",
        (activity_id, str(activity_id), fit_path, fit_sha256)).lastrowid
    return activity_id, source_id


def daily_trimp(conn, date="2026-10-01"):
    row = conn.execute("select trimp from daily_load where date = ?", (date,)).fetchone()
    return row["trimp"] if row else None


def test_build_activity_row_keeps_utc_and_uses_local_anchor_day(monkeypatch):
    monkeypatch.setattr(
        athlete_db, "load_config", lambda _home: {"timezone": "Pacific/Honolulu"})
    conn = mem_db()
    start = datetime(2026, 10, 1, 16, 30, tzinfo=timezone.utc)
    msgs = fake_msgs(start=start, tz_offset_s=8 * 3600)
    add_anchor(conn, "2026-10-02")
    row, tiz = build_activity_row(
        conn, msgs["sessions"][0], [], msgs["records"], msgs, "cross-midnight")
    assert row["start_ts"] == int(start.timestamp())
    assert row["tz_offset_s"] == 8 * 3600
    assert row["trimp"] == athlete_db.edwards_trimp(msgs["records"], 50, 190)
    assert tiz is not None


def test_daily_load_uses_utc_plus_offset_local_day(monkeypatch):
    monkeypatch.setattr(
        athlete_db, "load_config", lambda _home: {"timezone": "Pacific/Honolulu"})
    conn = mem_db()
    start = datetime(2026, 10, 1, 16, 30, tzinfo=timezone.utc)
    add_source(conn, int(start.timestamp()), 20.0, "fit/a.fit", "a" * 64, 8 * 3600)
    assert rebuild_daily_load(conn, today=datetime(2026, 10, 2).date()) == 1
    row = conn.execute("select * from daily_load").fetchone()
    assert row["date"] == "2026-10-02"
    assert row["trimp"] == 20.0


def test_build_activity_row_without_offset_uses_configured_local_day(tmp_path):
    (tmp_path / "config.toml").write_text(
        'timezone = "Asia/Shanghai"\n', encoding="utf-8")
    conn = connect(tmp_path / "athlete.db")
    init_db(conn)
    start = datetime(2026, 10, 1, 16, 30, tzinfo=timezone.utc)
    msgs = fake_msgs(start=start, tz_offset_s=None)
    add_anchor(conn, "2026-10-02")

    row, tiz = build_activity_row(
        conn, msgs["sessions"][0], [], msgs["records"], msgs, "configured-timezone")
    assert row["start_ts"] == int(start.timestamp())
    assert row["tz_offset_s"] is None
    assert row["trimp"] == athlete_db.edwards_trimp(msgs["records"], 50, 190)
    assert tiz is not None


def test_daily_load_default_today_uses_configured_timezone(monkeypatch):
    conn = mem_db()
    add_source(conn, TS0, 20.0, "fit/a.fit", "a" * 64, 0)
    monkeypatch.setattr(
        athlete_db, "load_config", lambda _home: {"timezone": "Asia/Shanghai"})
    fixed_utc = datetime(2026, 10, 1, 16, 30, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_utc.astimezone(tz) if tz is not None else fixed_utc.replace(tzinfo=None)

    monkeypatch.setattr(athlete_db, "datetime", FixedDateTime)
    assert rebuild_daily_load(conn) == 2
    rows = conn.execute("select * from daily_load order by date").fetchall()
    assert [r["date"] for r in rows] == ["2026-10-01", "2026-10-02"]
    assert rows[-1]["trimp"] == 0.0
    assert rows[-1]["ctl"] is not None


def test_fresh_memory_db_is_not_legacy_migrated():
    conn = mem_db()
    assert conn.execute("pragma user_version").fetchone()[0] == athlete_db.SCHEMA_VERSION
    activity_id = conn.execute(
        "insert into activity (start_ts, tz_offset_s) values (1000, 28800)").lastrowid
    conn.commit()
    init_db(conn)
    row = conn.execute("select start_ts from activity where id = ?", (activity_id,)).fetchone()
    assert row["start_ts"] == 1000


def test_legacy_start_ts_migration_runs_once_and_rebuild_does_not_repeat(
        tmp_path, monkeypatch):
    conn = connect(tmp_path / "athlete.db")
    init_db(conn)
    activity_id = conn.execute(
        "insert into activity (start_ts, tz_offset_s) values (?, 28800)",
        (TS0 + 28800,)).lastrowid
    conn.execute("pragma user_version = 0")
    conn.commit()

    init_db(conn)
    init_db(conn)
    row = conn.execute("select start_ts from activity where id = ?", (activity_id,)).fetchone()
    assert row["start_ts"] == TS0
    assert conn.execute("pragma user_version").fetchone()[0] == athlete_db.SCHEMA_VERSION

    fit_dir = tmp_path / "fit"
    fit_dir.mkdir()
    (fit_dir / "20261001-180000-run-garmin-g.fit").write_bytes(b"F")
    monkeypatch.setattr(
        athlete_db, "decode_fit", lambda _raw: fake_msgs(tz_offset_s=8 * 3600))
    assert cmd_ingest(conn, fit_dir, rebuild=True) == 0
    row = conn.execute("select start_ts, tz_offset_s from activity").fetchone()
    assert row["start_ts"] == TS0
    assert row["tz_offset_s"] == 28800


def test_legacy_migration_rolls_back_atomically(tmp_path):
    conn = connect(tmp_path / "athlete.db")
    init_db(conn)
    conn.execute(
        "insert into activity (start_ts, tz_offset_s) values (?, 28800)", (TS0 + 28800,))
    conn.execute("pragma user_version = 0")
    conn.execute("""
        create trigger stop_legacy_migration before update on activity
        begin select raise(abort, 'stop migration'); end
    """)
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="stop migration"):
        init_db(conn)
    row = conn.execute("select start_ts from activity").fetchone()
    assert row["start_ts"] == TS0 + 28800
    assert conn.execute("pragma user_version").fetchone()[0] == 0


def test_legacy_source_hashes_are_verified_backfilled_and_conflicts_excluded(tmp_path):
    fit_dir = tmp_path / "fit"
    fit_dir.mkdir()
    raw = b"actual fit bytes"
    (fit_dir / "good.fit").write_bytes(raw)
    actual_hash = hashlib.sha256(raw).hexdigest()
    conn = connect(tmp_path / "athlete.db")
    init_db(conn)
    good_activity, good_source = add_source(conn, TS0, 10.0, "fit/good.fit", None)
    bad_activity, bad_source = add_source(
        conn, TS0, 20.0, "fit/good.fit", hashlib.sha256(b"other").hexdigest())
    missing_activity, missing_source = add_source(conn, TS0, 40.0, "fit/missing.fit", None)
    invalid_activity, invalid_source = add_source(conn, TS0, 60.0, "fit/good.fit", "A" * 64)
    conn.execute("pragma user_version = 0")
    conn.commit()

    init_db(conn)
    sources = {r["id"]: r for r in conn.execute("select * from activity_source")}
    assert sources[good_source]["fit_sha256"] == actual_hash
    assert sources[good_source]["is_canonical"] == 1
    assert sources[bad_source]["fit_sha256"] == hashlib.sha256(b"other").hexdigest()
    assert sources[bad_source]["is_canonical"] == 0
    assert "legacy_fit_conflict" in sources[bad_source]["note"]
    assert sources[missing_source]["is_canonical"] == 0
    assert "legacy_fit_conflict" in sources[missing_source]["note"]
    assert sources[invalid_source]["is_canonical"] == 0
    assert "legacy_fit_conflict" in sources[invalid_source]["note"]
    assert daily_trimp(conn) == 10.0
    assert len({good_activity, bad_activity, missing_activity, invalid_activity}) == 4


def test_same_source_id_requires_identical_payload_hash(monkeypatch):
    decoded = {b"old": fake_msgs(), b"new": fake_msgs(distance=12000.0)}
    monkeypatch.setattr(athlete_db, "decode_fit", lambda raw: decoded[raw])
    conn = mem_db()
    add_anchor(conn)
    assert ingest_fit_payload(conn, b"old", "garmin", "g1", "fit/g1.fit") == (1, 0, 0)
    expected_trimp = athlete_db.edwards_trimp(decoded[b"old"]["records"], 50, 190)
    assert daily_trimp(conn) == expected_trimp
    assert ingest_fit_payload(conn, b"old", "garmin", "g1", "fit/g1.fit") == (0, 0, 1)

    review = []
    with pytest.raises(ValueError, match="SOURCE_HASH_CONFLICT"):
        ingest_fit_payload(conn, b"new", "garmin", "g1", "fit/g1.fit", review=review)
    source = conn.execute("select * from activity_source").fetchone()
    assert source["fit_path"] == "fit/g1.fit"
    assert source["fit_sha256"] == hashlib.sha256(b"old").hexdigest()
    assert source["is_canonical"] == 0
    assert hashlib.sha256(b"new").hexdigest() in source["note"]
    assert review[0]["type"] == "source_hash_conflict"
    assert conn.execute("select count(*) c from activity_source").fetchone()["c"] == 1
    assert conn.execute("select count(*) c from daily_load").fetchone()["c"] == 0


@pytest.mark.parametrize("order", [
    (("garmin", "g1"), ("coros", "c1")),
    (("coros", "c1"), ("garmin", "g1")),
])
def test_same_hash_reverse_ingest_has_identical_final_state(monkeypatch, order):
    msgs = fake_msgs()
    monkeypatch.setattr(athlete_db, "decode_fit", lambda _raw: msgs)
    conn = mem_db()
    add_anchor(conn)
    for platform, pid in order:
        ingest_fit_payload(conn, b"same", platform, pid, "fit/{}.fit".format(pid))
    canonical = conn.execute("select * from activity_source where is_canonical = 1").fetchone()
    assert canonical["platform"] == "coros"
    assert canonical["platform_activity_id"] == "c1"
    assert conn.execute("select count(*) c from activity").fetchone()["c"] == 1
    assert conn.execute("select distance_m from activity").fetchone()["distance_m"] == 10000.0
    assert daily_trimp(conn) == athlete_db.edwards_trimp(msgs["records"], 50, 190)


@pytest.mark.parametrize("order", [
    (("garmin", "G", "g1"), ("coros", "C", "c1")),
    (("coros", "C", "c1"), ("garmin", "G", "g1")),
])
def test_hash_fork_primary_is_reverse_ingest_deterministic(monkeypatch, tmp_path, order):
    decoded = {
        b"G": fake_msgs(distance=10000.0, hr=130),
        b"C": fake_msgs(distance=12000.0, hr=180),
    }
    monkeypatch.setattr(athlete_db, "decode_fit", lambda raw: decoded[raw])
    root = tmp_path / order[0][0]
    fit_dir = root / "fit"
    fit_dir.mkdir(parents=True)
    for raw in decoded:
        (fit_dir / "{}.fit".format(raw.decode().lower())).write_bytes(raw)
    conn = mem_db()
    add_anchor(conn)
    for platform, raw_name, pid in order:
        raw = raw_name.encode()
        ingest_fit_payload(
            conn, raw, platform, pid, "fit/{}.fit".format(pid), archive_root=root,
            primary_platform_name="garmin")

    canonical = conn.execute("select platform from activity_source where is_canonical = 1").fetchone()
    assert canonical["platform"] == "garmin"
    activity = conn.execute("select * from activity").fetchone()
    assert activity["distance_m"] == 10000.0
    expected_trimp = athlete_db.edwards_trimp(decoded[b"G"]["records"], 50, 190)
    assert activity["trimp"] == expected_trimp
    assert daily_trimp(conn) == expected_trimp
    expected_tiz = athlete_db.time_in_zone(
        decoded[b"G"]["records"], athlete_db.zone_boundaries(140, 170))
    actual_tiz = [r["time_s"] for r in conn.execute(
        "select time_s from time_in_zone where activity_id = ? order by zone", (activity["id"],))]
    assert actual_tiz == expected_tiz


def test_ambiguous_identity_candidates_all_leave_unified_load(monkeypatch, tmp_path, capsys):
    decoded = {
        b"A": fake_msgs(start=DAY0),
        b"B": fake_msgs(start=DAY0 + timedelta(seconds=400)),
        b"C": fake_msgs(start=DAY0 + timedelta(seconds=200)),
    }
    monkeypatch.setattr(athlete_db, "decode_fit", lambda raw: decoded[raw])
    conn = mem_db()
    add_anchor(conn)
    ingest_fit_payload(conn, b"A", "garmin", "a", "fit/a.fit")
    ingest_fit_payload(conn, b"B", "garmin", "b", "fit/b.fit")
    old_ids = {r["activity_id"] for r in conn.execute("select activity_id from activity_source")}
    assert len(old_ids) == 2

    review = []
    assert ingest_fit_payload(conn, b"C", "coros", "c", "fit/c.fit", review=review) == (1, 0, 0)
    new_source = conn.execute(
        "select * from activity_source where platform = 'coros'").fetchone()
    assert new_source["activity_id"] not in old_ids
    assert new_source["is_canonical"] == 0
    assert review == [{
        "type": "canonical_conflict", "activity_id": new_source["activity_id"],
        "reason": "identity_ambiguous", "candidate_activity_ids": sorted(old_ids),
    }]
    assert conn.execute("select count(*) c from activity").fetchone()["c"] == 3
    assert conn.execute(
        "select count(*) c from activity_source where is_canonical = 1").fetchone()["c"] == 0
    assert find_canonical_near(conn, TS0 + 200) is None
    assert conn.execute("select count(*) c from daily_load").fetchone()["c"] == 0
    cmd_stats(conn, tmp_path / "fit")
    output = capsys.readouterr().out
    assert "activity 共 0 行（仅 canonical）" in output
    assert "canonical 冲突 3 个" in output


def test_multisport_segments_with_same_start_remain_isolated(monkeypatch):
    multi = fake_msgs()
    multi["sessions"] = [
        fake_msgs(distance=800.0, duration=40.0)["sessions"][0],
        fake_msgs(distance=900.0, duration=45.0)["sessions"][0],
    ]
    monkeypatch.setattr(athlete_db, "decode_fit", lambda _raw: multi)
    conn = mem_db()
    assert ingest_fit_payload(conn, b"M", "garmin", "parent-g", "fit/g.fit") == (2, 0, 0)
    assert ingest_fit_payload(conn, b"M", "coros", "parent-c", "fit/c.fit") == (0, 2, 0)
    rows = conn.execute(
        "select platform, platform_activity_id, activity_id from activity_source").fetchall()
    ids = {(r["platform"], r["platform_activity_id"]): r["activity_id"] for r in rows}
    assert ids[("garmin", "parent-g#1")] != ids[("garmin", "parent-g#2")]
    assert ids[("coros", "parent-c#1")] == ids[("garmin", "parent-g#1")]
    assert ids[("coros", "parent-c#2")] == ids[("garmin", "parent-g#2")]
    assert conn.execute("select count(*) c from activity").fetchone()["c"] == 2
