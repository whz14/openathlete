import io
import sys
import types
import zipfile

from openathlete.adapters.garmin import GarminClient


def test_archive_fit_unpacks_network_zip_to_canonical_fit(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("activity.fit", b"synthetic-fit")
    activity = {
        "activityId": 123,
        "activityType": {"typeKey": "running"},
        "startTimeLocal": "2026-10-07 12:00:00",
    }
    client = GarminClient("unused", "unused", home=tmp_path)
    paths = client.archive_fit(activity, buf.getvalue())
    expected = tmp_path / "fit" / "2026" / "10" / "20261007-120000-run-garmin-123.fit"
    assert paths == [expected]
    assert expected.read_bytes() == b"synthetic-fit"
    assert not list(tmp_path.rglob("*.zip"))
    assert client.archive_fit(activity, buf.getvalue()) == [expected]


def test_ensure_login_removes_user_agent_on_token_resume(tmp_path, monkeypatch):
    class Session:
        headers = {"User-Agent": "default"}

    class FakeGarth:
        def __init__(self):
            self.client = type("Client", (), {"sess": Session()})()

        def resume(self, _path):
            return None

    fake = FakeGarth()
    client = GarminClient("unused", "unused", home=tmp_path)
    monkeypatch.setattr(client, "_garth", lambda: fake)
    client.ensure_login()
    assert "User-Agent" not in fake.client.sess.headers


def test_workout_create_read_and_schedule_payloads(tmp_path, monkeypatch):
    calls = []

    class Response:
        status_code = 200
        content = b"{}"

        def json(self):
            return {"workoutId": 123}

    class Client:
        @staticmethod
        def post(_subdomain, path, json, api):
            calls.append(("POST", path, json, api))
            return Response()

    def connectapi(path, params=None):
        calls.append(("GET", path, params))
        if path == "/workout-service/workout/123":
            return {"workoutId": 123, "workoutName": "test"}
        if path == "/workout-service/workouts":
            return []
        return {"calendarItems": []}

    monkeypatch.setitem(sys.modules, "garth", types.SimpleNamespace(
        client=Client(), connectapi=connectapi))
    client = GarminClient("unused", "unused", home=tmp_path)
    workout = {"workoutName": "test", "workoutSegments": []}
    assert client.create_workout(workout) == {"workoutId": 123}
    assert calls[-1] == ("POST", "/workout-service/workout", workout, True)
    assert client.get_workout(123)["workoutName"] == "test"
    assert calls[-1] == ("GET", "/workout-service/workout/123", None)
    assert client.list_workouts() == []
    assert calls[-1][1] == "/workout-service/workouts"
    assert client.schedule_workout(123, "2026-10-25") == {"workoutId": 123}
    assert calls[-1] == ("POST", "/workout-service/schedule/123", {"date": "2026-10-25"}, True)
    assert client.get_scheduled_workouts(2026, 10) == {"calendarItems": []}
    assert calls[-1] == ("GET", "/calendar-service/year/2026/month/9", None)


def test_thresholds_keep_sport_max_hr_and_use_current_valid_from(tmp_path, monkeypatch):
    client = GarminClient("unused", "unused", home=tmp_path)
    zones = [
        {"sport": "CYCLING", "maxHeartRateUsed": 180,
         "lactateThresholdHeartRateUsed": 165},
        {"sport": "DEFAULT", "maxHeartRateUsed": 190},
        {"sport": "RUNNING", "maxHeartRateUsed": 185},
    ]

    def api(path, params=None):
        if path.endswith("latestLactateThreshold"):
            return [{"hearRate": 170, "speed": 0.4, "calendarDate": "2026-10-18"}]
        if path.endswith("heartRateZones"):
            return zones
        if path.endswith("powerZones/sports/all"):
            return [{"sport": "CYCLING", "functionalThresholdPower": 200}]
        raise AssertionError(path)

    monkeypatch.setattr(client, "_api", api)
    monkeypatch.setattr(client, "fetch_resting_hr", lambda today: ("2026-10-31", 42))
    result = client.fetch_thresholds(today="2026-11-02")
    assert result["run"]["max_hr"] == 185
    assert result["ride"]["max_hr"] == 180
    assert result["run"]["resting_hr"] == result["ride"]["resting_hr"] == 42
    assert result["run"]["resting_hr_date"] == "2026-10-31"
    assert result["run"]["valid_from"] == result["ride"]["valid_from"] == "2026-11-02"
    assert result["run"]["lt_date"] == "2026-10-18"
    assert result["run"]["threshold_pace_s"] == 250
    assert result["ride"]["lt2_hr"] == 165 and result["ride"]["ftp_w"] == 200


def test_thresholds_use_default_max_hr_only_for_missing_sport(tmp_path, monkeypatch):
    client = GarminClient("unused", "unused", home=tmp_path)

    def api(path, params=None):
        if path.endswith("latestLactateThreshold"):
            return []
        if path.endswith("heartRateZones"):
            return [{"sport": "DEFAULT", "maxHeartRateUsed": 190},
                    {"sport": "RUNNING", "maxHeartRateUsed": 185}]
        if path.endswith("powerZones/sports/all"):
            return [{"sport": "CYCLING", "functionalThresholdPower": 200}]
        raise AssertionError(path)

    monkeypatch.setattr(client, "_api", api)
    monkeypatch.setattr(client, "fetch_resting_hr", lambda today: (None, None))
    result = client.fetch_thresholds(today="2026-11-02")
    assert result["run"]["max_hr"] == 185
    assert result["ride"]["max_hr"] == 190


def test_resting_hr_uses_latest_nonempty_value_within_seven_days(tmp_path, monkeypatch):
    client = GarminClient("unused", "unused", home=tmp_path)
    monkeypatch.setattr(client, "display_name", lambda: "athlete")
    calls = []

    def api(path, params=None):
        calls.append((path, params))
        return {"allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [
            {"calendarDate": "2026-10-28", "value": 45},
            {"calendarDate": "2026-11-01", "value": 42},
            {"calendarDate": "2026-11-02", "value": None},
        ]}}}

    monkeypatch.setattr(client, "_api", api)
    assert client.fetch_resting_hr("2026-11-02") == ("2026-11-01", 42)
    assert calls[0][1] == {"fromDate": "2026-10-27", "untilDate": "2026-11-02",
                           "metricId": 60}


def test_resting_hr_falls_back_to_summary_and_allows_missing(tmp_path, monkeypatch):
    client = GarminClient("unused", "unused", home=tmp_path)
    monkeypatch.setattr(client, "display_name", lambda: "athlete")
    responses = iter([{}, {"restingHeartRate": 41}])
    monkeypatch.setattr(client, "_api", lambda path, params=None: next(responses))
    assert client.fetch_resting_hr("2026-11-02") == ("2026-11-02", 41)
    responses = iter([{}, {}])
    monkeypatch.setattr(client, "_api", lambda path, params=None: next(responses))
    assert client.fetch_resting_hr("2026-11-02") == (None, None)
