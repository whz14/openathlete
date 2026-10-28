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
