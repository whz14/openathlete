import hashlib
import io
import sqlite3
import zipfile

import sync_garmin_to_coros as sync


def test_sync_archives_bare_fit_and_cleans_upload_zip(tmp_path, monkeypatch):
    payload_buffer = io.BytesIO()
    with zipfile.ZipFile(payload_buffer, "w") as zf:
        zf.writestr("activity.fit", b"synthetic-fit")
    payload = payload_buffer.getvalue()
    activity = {
        "activityId": 123,
        "activityType": {"typeKey": "running"},
        "startTimeLocal": "2026-10-07 12:00:00",
        "beginTimestamp": 1791374400000,
    }

    class FakeGarmin:
        def __init__(self, *args):
            pass

        def getAllActivities(self):
            return [activity]

        def downloadFitActivity(self, activity_id):
            assert activity_id == 123
            return payload

    class FakeCoros:
        def __init__(self, *args):
            self.regionId = 2
            self.userId = "user"
            self.accessToken = "token"

        def login(self):
            return None

        def uploadActivity(self, *args):
            return True

    uploads = []

    class FakeOss:
        def multipart_upload(self, path, remote):
            uploads.append((path, remote))

    import coros.coros_client as coros_module
    import garmin.garmin_client as garmin_module
    monkeypatch.setattr(coros_module, "CorosClient", FakeCoros)
    monkeypatch.setattr(garmin_module, "GarminClient", FakeGarmin)
    monkeypatch.setattr(sync, "load_env", lambda _path: None)
    monkeypatch.setattr(sync, "require_env", lambda *keys: {key: "unused" for key in keys})
    monkeypatch.setattr(sync, "build_oss_client", lambda *_args: FakeOss())
    monkeypatch.setattr(sync.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(sync, "OPENATHLETE_HOME", tmp_path)
    monkeypatch.setattr(sync, "DB_PATH", tmp_path / "sync.db")
    monkeypatch.setattr(sync.sys, "argv", ["sync", "--limit", "1", "--no-server-dedup"])

    sync.main()

    expected = tmp_path / "fit" / "2026" / "10" / "20261007-120000-run-garmin-123.fit"
    assert expected.read_bytes() == b"synthetic-fit"
    assert not list((tmp_path / "fit").rglob("*.zip"))
    assert len(uploads) == 1
    upload_path, remote = uploads[0]
    assert remote == "user/{}.zip".format(hashlib.md5(payload).hexdigest())
    assert not __import__("pathlib").Path(upload_path).exists()
    conn = sqlite3.connect(tmp_path / "sync.db")
    assert conn.execute("select is_sync_coros from garmin_activity where activity_id=123").fetchone()[0] == 1
