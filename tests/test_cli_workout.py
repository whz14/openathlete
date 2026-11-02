import json
from types import SimpleNamespace

from openathlete import cli


def workout():
    return {"sport": "run", "name": "20分钟慢跑", "description": "不设配速",
            "blocks": [{"kind": "steady", "role": "main",
                        "target": {"kind": "duration", "value": 1200}, "intensity": None}]}


def test_workout_dry_run_does_not_initialize_client(tmp_path, monkeypatch, capsys):
    path = tmp_path / "workout.json"
    path.write_text(json.dumps(workout(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(cli, "_garmin_client", lambda: (_ for _ in ()).throw(
        AssertionError("dry-run must not initialize GarminClient")))
    args = SimpleNamespace(file=str(path), date=None, dry_run=True)
    assert cli._garmin_workout_create(args) == 0
    payload = json.loads(capsys.readouterr().out)
    step = payload["workoutSegments"][0]["workoutSteps"][0]
    assert step["endConditionValue"] == 1200
    assert step["targetType"]["workoutTargetTypeKey"] == "no.target"


def test_workout_schedule_cli_uses_existing_id(monkeypatch, capsys):
    class FakeClient:
        def ensure_login(self):
            return None

        def schedule_workout(self, workout_id, date):
            assert workout_id == "123" and date == "2026-10-25"
            return {"workoutScheduleId": 456}

    monkeypatch.setattr(cli, "_garmin_client", lambda: FakeClient())
    args = SimpleNamespace(id="123", date="2026-10-25")
    assert cli._garmin_workout_schedule(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"workoutId": "123", "date": "2026-10-25", "workoutScheduleId": 456}


def threshold_data():
    return {
        "run": {"lt1_hr": None, "lt2_hr": 170, "max_hr": 185, "resting_hr": 42,
                "threshold_pace_s": 211.0, "valid_from": "2026-11-02",
                "resting_hr_date": "2026-11-01"},
        "ride": {"lt1_hr": None, "lt2_hr": None, "max_hr": 180, "resting_hr": 42,
                 "ftp_w": 200, "valid_from": "2026-11-02",
                 "resting_hr_date": "2026-11-01"},
    }


def test_garmin_thresholds_dry_run_reads_without_opening_db(monkeypatch, capsys):
    calls = []

    class FakeClient:
        def ensure_login(self):
            calls.append("login")

        def fetch_thresholds(self, today):
            calls.append(("fetch", today))
            return threshold_data()

    monkeypatch.setattr(cli, "_garmin_client", lambda: FakeClient())
    monkeypatch.setattr(cli.athlete_db, "connect", lambda path: (_ for _ in ()).throw(
        AssertionError("dry-run must not open athlete.db")))
    args = SimpleNamespace(valid_from="2026-11-02", dry_run=True)
    assert cli._garmin_thresholds(args) == 0
    assert calls == ["login", ("fetch", "2026-11-02")]
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True
    assert result["thresholds"]["run"]["max_hr"] == 185
    assert result["thresholds"]["run"]["resting_hr_date"] == "2026-11-01"


def test_garmin_thresholds_write_preserves_manual_fields_and_is_idempotent(
        tmp_path, monkeypatch, capsys):
    from openathlete.db.athlete_db import connect, find_anchor, init_db, write_anchor
    from openathlete.home import athlete_db_path

    monkeypatch.setattr(cli, "OPENATHLETE_HOME", tmp_path)
    conn = connect(athlete_db_path(tmp_path))
    init_db(conn)
    write_anchor(conn, "run", {"valid_from": "2026-10-01", "lt1_hr": 150}, "manual")
    conn.close()

    class FakeClient:
        def ensure_login(self):
            return None

        def fetch_thresholds(self, today):
            return threshold_data()

    monkeypatch.setattr(cli, "_garmin_client", lambda: FakeClient())
    args = SimpleNamespace(valid_from="2026-11-02", dry_run=False)
    assert cli._garmin_thresholds(args) == 0
    assert cli._garmin_thresholds(args) == 0
    capsys.readouterr()
    conn = connect(athlete_db_path(tmp_path))
    assert conn.execute("select count(*) from threshold_anchor where sport='run'").fetchone()[0] == 2
    anchor = find_anchor(conn, "run", "2026-11-02")
    assert anchor["lt1_hr"] == 150 and anchor["lt2_hr"] == 170
    assert anchor["max_hr"] == 185 and anchor["resting_hr"] == 42
    ride = find_anchor(conn, "ride", "2026-11-02")
    assert ride["max_hr"] == 180 and ride["ftp_w"] == 200
    conn.close()


def test_garmin_thresholds_failure_does_not_open_db(monkeypatch, capsys):
    class FakeClient:
        def ensure_login(self):
            raise RuntimeError("synthetic login failure")

    monkeypatch.setattr(cli, "_garmin_client", lambda: FakeClient())
    monkeypatch.setattr(cli.athlete_db, "connect", lambda path: (_ for _ in ()).throw(
        AssertionError("failed read must not open athlete.db")))
    args = SimpleNamespace(valid_from="2026-11-02", dry_run=False)
    assert cli._garmin_thresholds(args) == 1
    assert "synthetic login failure" in capsys.readouterr().out


def test_garmin_thresholds_parser_dispatches_explicit_mode(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_garmin_thresholds", lambda args: seen.append(args) or 0)
    assert cli._garmin_main(["thresholds", "--dry-run", "--valid-from", "2026-11-02"]) == 0
    assert seen[0].dry_run is True and seen[0].write is False
    assert seen[0].valid_from == "2026-11-02"
