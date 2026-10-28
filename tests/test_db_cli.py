from datetime import datetime, timezone

from openathlete.db import athlete_db
from openathlete.db.athlete_db import (
    cmd_ingest, connect, init_db, upsert_daily_metrics, write_anchor,
)


def mem_db():
    conn = connect(":memory:")
    init_db(conn)
    return conn


def test_rebuild_preserves_nonderived_tables_and_scans_only_fit(tmp_path, monkeypatch):
    home = tmp_path
    fit_dir = home / "fit"
    fit_dir.mkdir()
    (fit_dir / "20261001-000000-run-garmin-a.fit").write_bytes(b"F")
    (fit_dir / "legacy.zip").write_bytes(b"Z")
    conn = mem_db()
    write_anchor(conn, "run", {"valid_from": "2026-01-01", "lt1_hr": 140}, "manual")
    upsert_daily_metrics(conn, [{"date": "2026-10-01", "source": "coros", "resting_hr": 50}])
    conn.execute("insert into activity (start_ts) values (1)")
    conn.execute("insert into daily_load (date, trimp) values ('2026-10-01', 10)")
    calls = []

    def fake_ingest(conn, path, fit_dir, **kwargs):
        calls.append(path.name)
        return 0, 0, 1

    monkeypatch.setattr(athlete_db, "ingest_archive_file", fake_ingest)
    assert cmd_ingest(conn, fit_dir, rebuild=True) == 0
    assert calls == ["20261001-000000-run-garmin-a.fit"]
    assert conn.execute("select count(*) c from threshold_anchor").fetchone()["c"] == 1
    assert conn.execute("select count(*) c from daily_metrics").fetchone()["c"] == 1
    assert conn.execute("select count(*) c from activity").fetchone()["c"] == 0
    assert conn.execute("select count(*) c from daily_load").fetchone()["c"] == 0


def test_ingest_failures_and_conflicts_return_nonzero(tmp_path, monkeypatch):
    fit_dir = tmp_path / "fit"
    fit_dir.mkdir()
    (fit_dir / "20261001-000000-run-garmin-a.fit").write_bytes(b"F")
    conn = mem_db()
    monkeypatch.setattr(athlete_db, "ingest_archive_file",
                        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad fit")))
    assert cmd_ingest(conn, fit_dir) == 1
    assert cmd_ingest(conn, fit_dir, limit=1, rebuild=True) == 2

    def conflict(conn, path, fit_dir, **kwargs):
        kwargs["review"].append({"type": "canonical_conflict", "activity_id": 1})
        return 0, 1, 0

    monkeypatch.setattr(athlete_db, "ingest_archive_file", conflict)
    assert cmd_ingest(conn, fit_dir) == 1


def test_non_tty_ride_candidate_does_not_prompt_or_save(tmp_path, monkeypatch):
    fit_dir = tmp_path / "fit"
    fit_dir.mkdir()
    (fit_dir / "20261001-000000-ride-garmin-r.fit").write_bytes(b"R")
    msgs = {
        "sessions": [{
            "start_time": datetime(2026, 10, 1, tzinfo=timezone.utc),
            "sport": "cycling", "sub_sport": "road", "total_distance": 5000.0,
            "total_elapsed_time": 1200.0, "avg_heart_rate": 150,
        }],
        "laps": [], "records": [], "tz_offset_s": 0,
        "workout_name": None, "workout_step_count": 0,
    }
    monkeypatch.setattr(athlete_db, "decode_fit", lambda _raw: msgs)
    monkeypatch.setattr(athlete_db.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *args: (_ for _ in ()).throw(
        AssertionError("non-TTY must not prompt")))
    conn = mem_db()
    assert cmd_ingest(conn, fit_dir) == 0
    assert not (tmp_path / "config.toml").exists()
    row = conn.execute("select type, raw_meta_json from activity").fetchone()
    assert row["type"] is None
    assert "ride_threshold_pending" in row["raw_meta_json"]
