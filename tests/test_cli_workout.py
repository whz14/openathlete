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
