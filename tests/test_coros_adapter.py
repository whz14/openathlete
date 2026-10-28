# -*- coding: utf-8 -*-
"""高驰 web API adapter 单测（httpx.MockTransport，不触网、不用真实凭据）。"""
import io
import json
import zipfile
from datetime import datetime

import httpx
import pytest

from openathlete.adapters.coros import (
    ArchiveConflictError, CorosApiError, CorosClient, archive_sport_name,
)

A1 = {"labelId": 111, "sportType": 100, "name": "晨跑", "startTime": 1780000000,
      "endTime": 1780003600, "distance": 10000}
A2 = {"labelId": 112, "sportType": 200, "name": "骑行", "startTime": 1780005000,
      "endTime": 1780010000, "distance": 30000}
A3 = {"labelId": 113, "sportType": 100, "name": None, "startTime": 1780015000,
      "endTime": 1780017000, "distance": 5000}


def make_transport(fit_payload=b"FITBYTES"):
    def handler(request):
        path = request.url.path
        if path == "/account/login":
            return httpx.Response(200, json={"result": "0000", "data": {
                "accessToken": "tok", "userId": 123, "regionId": 2}})
        if path == "/activity/query":
            page = int(request.url.params.get("pageNumber", "1"))
            size = int(request.url.params.get("size", "50"))
            pages = [[A1, A2], [A3], []]
            items = pages[min(page, 3) - 1] if size == 2 else [A1]
            return httpx.Response(200, json={"result": "0000", "data": {
                "dataList": items, "totalCount": 3 if size == 2 else 1}})
        if path == "/activity/detail/download":
            return httpx.Response(200, json={"result": "0000",
                                             "data": {"fileUrl": "https://files.example/x.zip"}})
        if request.url.host == "files.example":
            return httpx.Response(200, content=fit_payload)
        if path == "/analyse/dayDetail/query":
            return httpx.Response(200, json={"result": "0000", "data": {"dayList": [
                {"happenDay": 20261001, "rhr": 50, "avgSleepHrv": 80,
                 "trainingLoad": 100, "ati": 11, "cti": 22}]}})
        if path == "/analyse/query":
            return httpx.Response(200, json={"result": "0000", "data": {"t7dayList": [
                {"happenDay": 20261001, "vo2max": 55.1, "lthr": 172, "ltsp": 250}]}})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def make_client(tmp_path, **kw):
    return CorosClient("a@b.c", "pw", home=tmp_path,
                       transport=make_transport(**kw))


def test_login_token_cache_and_expired_relogin(tmp_path):
    client = make_client(tmp_path)
    client.ensure_login()
    assert client.access_token == "tok"
    assert client.region_id == 2
    assert client.base_url == "https://teamcnapi.coros.com"
    assert (tmp_path / "coros-web-token.json").exists()
    client2 = make_client(tmp_path)
    client2.ensure_login()
    assert client2.access_token == "tok"

    (tmp_path / "coros-web-token.json").write_text(json.dumps({
        "access_token": "old", "user_id": "1", "region_id": 2, "ts": 0}),
        encoding="utf-8")
    client = make_client(tmp_path)
    client.ensure_login()
    assert client.access_token == "tok"


def test_iter_activities_pages(tmp_path):
    client = make_client(tmp_path)
    client.ensure_login()
    got = list(client.iter_activities(size=2))
    assert [a["labelId"] for a in got] == [111, 112, 113]


def test_archive_fit_idempotent(tmp_path):
    client = make_client(tmp_path)
    client.ensure_login()
    payload = client.download_fit(111, 100)
    paths = client.archive_fit(A1, payload)
    assert len(paths) == 1
    p = paths[0]
    expect = datetime.fromtimestamp(A1["startTime"]).astimezone().strftime("%Y%m%d-%H%M%S")
    assert p.stem.startswith(expect + "-run-coros-111")
    assert p.parent.parent.parent.name == "fit"
    again = client.archive_fit(A1, payload)
    assert again == [p]
    assert len(list(p.parent.glob("*.fit"))) == 1


def test_archive_fit_multisport_zip(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("1.fit", b"SEG1")
        zf.writestr("2.fit", b"SEG2")
    client = make_client(tmp_path, fit_payload=buf.getvalue())
    client.ensure_login()
    paths = client.archive_fit(A1, client.download_fit(111, 100))
    assert len(paths) == 2
    assert paths[0].read_bytes() == b"SEG1"
    assert "#1" in paths[0].stem and "#2" in paths[1].stem


def test_fetch_daily_records_merges(tmp_path):
    client = make_client(tmp_path)
    client.ensure_login()
    records = client.fetch_daily_records("20261001", "20261007")
    assert len(records) == 1
    rec = records[0]
    assert rec["date"] == "2026-10-01"
    assert rec["resting_hr"] == 50 and rec["hrv_ms"] == 80
    assert rec["extras"]["vo2max"] == 55.1 and rec["extras"]["ltsp"] == 250
    assert rec["extras"]["trainingLoad"] == 100


def test_api_error_raises(tmp_path):
    def handler(_request):
        return httpx.Response(200, json={"result": "9999", "message": "boom"})
    client = CorosClient("a@b.c", "pw", home=tmp_path,
                         transport=httpx.MockTransport(handler))
    with pytest.raises(CorosApiError):
        client.login()


def test_archive_sport_name():
    assert archive_sport_name(100) == "run"
    assert archive_sport_name(201) == "ride"
    assert archive_sport_name(300) == "swim"
    assert archive_sport_name(1200) == "sport1200"
    assert archive_sport_name(None) == "sport0"


def test_archive_same_path_different_bytes_preserves_original(tmp_path):
    client = make_client(tmp_path)
    path = client.archive_fit(A1, b"ORIGINAL")[0]
    with pytest.raises(ArchiveConflictError):
        client.archive_fit(A1, b"DIFFERENT")
    assert path.read_bytes() == b"ORIGINAL"
    assert list(path.parent.glob("*.fit")) == [path]
    assert list(path.parent.glob("*.tmp")) == []
