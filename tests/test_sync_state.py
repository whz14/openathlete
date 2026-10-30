# -*- coding: utf-8 -*-
"""同步状态机合成数据测试；不触网、不使用真实凭据。"""
import io
import json
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from openathlete import sync_state
from openathlete.adapters.coros import CorosClient
from openathlete.db import athlete_db
from openathlete.sync_state import (
    ACTIVITY_STREAM, METRICS_STREAM, SyncError, backfill_activities, backfill_metrics,
    ensure_fresh, get_coverage, init_sync_db, sync_now,
)

UTC = timezone.utc
NOW = int(datetime(2026, 10, 24, 12, 0, tzinfo=UTC).timestamp())
TS0 = int(datetime(2026, 10, 20, 8, 0, tzinfo=UTC).timestamp())


def fake_msgs(start, sessions=1):
    base = datetime.fromtimestamp(start, UTC)
    session_rows = []
    for index in range(sessions):
        session_rows.append({
            "start_time": base + timedelta(seconds=index * 45),
            "sport": "running", "sub_sport": "generic", "total_distance": 1000.0,
            "total_elapsed_time": 40.0, "total_ascent": 0.0,
            "avg_heart_rate": 150, "max_heart_rate": 170, "avg_power": None,
            "avg_cadence": 180, "total_calories": 50,
        })
    return {"sessions": session_rows, "laps": [], "records": [], "tz_offset_s": 0,
            "workout_name": None, "workout_step_count": 0}


class FakeCorosClient:
    def __init__(self, timezone_name="UTC"):
        self.timezone_name = timezone_name
        self.activities = []
        self.decoded = {}
        self.payloads = {}
        self.records = []
        self.list_calls = []
        self.download_calls = []
        self.metrics_calls = []
        self.login_calls = 0
        self.fail_downloads = set()
        self.fail_page_once = None
        self.download_hook = None

    def ensure_login(self):
        self.login_calls += 1

    def add(self, pid, start, sport=100, sessions=1):
        raw = "fit-{}".format(pid).encode()
        item = {"labelId": str(pid), "sportType": sport, "startTime": int(start),
                "endTime": int(start) + 40, "name": "synthetic"}
        self.activities.append(item)
        self.payloads[str(pid)] = raw
        self.decoded[raw] = fake_msgs(start, sessions)
        return item

    def add_zip(self, pid, start):
        parts = [b"zip-segment-1", b"zip-segment-2"]
        for index, raw in enumerate(parts):
            self.decoded[raw] = fake_msgs(start + index * 45)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("1.fit", parts[0])
            archive.writestr("2.fit", parts[1])
        item = {"labelId": str(pid), "sportType": 100, "startTime": int(start),
                "endTime": int(start) + 100, "name": "synthetic-zip"}
        self.activities.append(item)
        self.payloads[str(pid)] = buf.getvalue()
        return item

    def decode(self, raw):
        return self.decoded[raw]

    def list_activities(self, start_day, end_day, page=1, size=50):
        self.list_calls.append((start_day, end_day, page, size))
        if self.fail_page_once == page:
            self.fail_page_once = None
            raise httpx.ConnectError("synthetic page failure")
        zone = ZoneInfo(self.timezone_name)
        start_day, end_day = int(start_day), int(end_day)
        items = [item for item in self.activities if start_day <= int(
            datetime.fromtimestamp(item["startTime"], zone).strftime("%Y%m%d")) <= end_day]
        items.sort(key=lambda item: (item["startTime"], item["labelId"]), reverse=True)
        offset = (page - 1) * size
        return items[offset:offset + size], len(items)

    def download_fit(self, label_id, sport_type):
        pid = str(label_id)
        self.download_calls.append(pid)
        if self.download_hook is not None:
            self.download_hook(pid)
        if pid in self.fail_downloads:
            raise httpx.ConnectError("synthetic download failure")
        return self.payloads[pid]

    def fetch_daily_records(self, start_day, end_day):
        self.metrics_calls.append((start_day, end_day))
        return [dict(row) for row in self.records
                if int(start_day) <= int(row["date"].replace("-", "")) <= int(end_day)]


@pytest.fixture
def home(tmp_path):
    (tmp_path / "config.toml").write_text(
        'timezone = "UTC"\nactivity_since = "2026-10-20"\nprimary_platform = "coros"\n',
        encoding="utf-8")
    return tmp_path


def fake_client(monkeypatch, timezone_name="UTC"):
    client = FakeCorosClient(timezone_name)
    monkeypatch.setattr(sync_state, "decode_fit", client.decode)
    monkeypatch.setattr(athlete_db, "decode_fit", client.decode)
    return client


def db_rows(home, sql, params=()):
    conn = sqlite3.connect(str(Path(home) / "sync.db"))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def athlete_count(home, table):
    conn = sqlite3.connect(str(Path(home) / "athlete.db"))
    try:
        return conn.execute("select count(*) from {}".format(table)).fetchone()[0]
    finally:
        conn.close()


def test_init_preserves_legacy_garmin_activity(home):
    conn = sqlite3.connect(str(home / "sync.db"))
    conn.execute("create table garmin_activity(activity_id integer primary key, "
                 "start_time_local text, is_sync_coros integer)")
    conn.execute("insert into garmin_activity values (7, '2026-01-01', 1)")
    conn.commit()
    conn.close()
    init_sync_db(home)
    assert db_rows(home, "select * from garmin_activity") == [
        {"activity_id": 7, "start_time_local": "2026-01-01", "is_sync_coros": 1}]
    tables = {row["name"] for row in db_rows(
        home, "select name from sqlite_master where type='table'")}
    assert {"oa_sync_state", "oa_sync_queue", "oa_sync_segment", "garmin_activity"} <= tables


def test_date_pagination_overlap_and_second_filter(home, monkeypatch):
    monkeypatch.setattr(sync_state, "DISCOVERY_PAGE_SIZE", 2)
    client = fake_client(monkeypatch)
    starts = [TS0, TS0 + 3600, TS0 + 2 * 3600]
    for index, start in enumerate(starts):
        client.add("p{}".format(index), start)
    first = sync_now(home, streams=(ACTIVITY_STREAM,), client=client, now_ts=NOW)
    assert first["streams"][ACTIVITY_STREAM]["processed"] == 3
    assert [call[2] for call in client.list_calls] == [1, 2]
    assert client.list_calls[0][:2] == ("20261020", "20261024")
    state = db_rows(home, "select * from oa_sync_state where stream=?",
                    (ACTIVITY_STREAM,))[0]
    assert state["high_water_ts"] == max(starts)
    assert state["discovery_cursor_json"] is None

    high_water = max(starts)
    client.add("late", high_water - 3600)
    client.add("too-old", high_water - sync_state.DISCOVERY_OVERLAP_S - 1)
    client.list_calls.clear()
    second = sync_now(home, streams=(ACTIVITY_STREAM,), client=client, now_ts=NOW + 3600)
    assert second["streams"][ACTIVITY_STREAM]["succeeded"] == 1
    filter_start = max(high_water - sync_state.DISCOVERY_OVERLAP_S,
                       int(datetime(2026, 10, 20, tzinfo=UTC).timestamp()))
    expected_start = datetime.fromtimestamp(filter_start, ZoneInfo("UTC")).strftime("%Y%m%d")
    assert client.list_calls[0][0] == expected_start
    ids = {row["platform_activity_id"] for row in db_rows(
        home, "select platform_activity_id from oa_sync_queue")}
    assert "late" in ids and "too-old" not in ids
    assert db_rows(home, "select high_water_ts from oa_sync_state where stream=?",
                   (ACTIVITY_STREAM,))[0]["high_water_ts"] == high_water


def test_page_crash_resumes_cursor_before_high_water(home, monkeypatch):
    monkeypatch.setattr(sync_state, "DISCOVERY_PAGE_SIZE", 2)
    client = fake_client(monkeypatch)
    for index in range(3):
        client.add("p{}".format(index), TS0 + index * 60)
    client.fail_page_once = 2
    with pytest.raises(SyncError) as caught:
        sync_now(home, streams=(ACTIVITY_STREAM,), client=client, now_ts=NOW)
    assert caught.value.code == "UPSTREAM_UNAVAILABLE"
    state = db_rows(home, "select * from oa_sync_state where stream=?",
                    (ACTIVITY_STREAM,))[0]
    assert state["high_water_ts"] is None and state["coverage_end_date"] is None
    assert json.loads(state["discovery_cursor_json"])["page"] == 2
    result = sync_now(home, streams=(ACTIVITY_STREAM,), client=client,
                      now_ts=NOW + sync_state.GLOBAL_RETRY_S)
    assert result["streams"][ACTIVITY_STREAM]["processed"] == 3
    assert [call[2] for call in client.list_calls] == [1, 2, 2]


def test_twenty_parent_batch_and_ttl_backlog(home, monkeypatch):
    client = fake_client(monkeypatch)
    for index in range(25):
        client.add("p{:02}".format(index), TS0 + index * 60)
    first = sync_now(home, streams=(ACTIVITY_STREAM,), client=client, now_ts=NOW)
    summary = first["streams"][ACTIVITY_STREAM]
    assert summary["processed"] == 20 and summary["queued_remaining"] == 5
    assert summary["complete"] is False and summary["stale"] is True
    assert len(client.download_calls) == 20
    meta, coverage = ensure_fresh(
        home, {ACTIVITY_STREAM: ("2026-10-20", "2026-10-24")},
        client=client, now_ts=NOW + 60)
    assert meta["attempted"] is True
    assert meta["streams"][ACTIVITY_STREAM]["processed"] == 5
    assert coverage[ACTIVITY_STREAM]["complete"] is True
    assert len(client.download_calls) == 25


def test_two_streams_and_manual_ignores_ttl(home, monkeypatch):
    client = fake_client(monkeypatch)
    client.records = [{"date": "2026-10-23", "resting_hr": 50, "hrv_ms": 80}]
    result = sync_now(home, client=client, now_ts=NOW)
    assert set(result["streams"]) == {ACTIVITY_STREAM, METRICS_STREAM}
    assert result["streams"][METRICS_STREAM]["processed"] == 1
    assert client.metrics_calls == [("20260927", "20261024")]
    assert result["complete"] is True
    calls = (len(client.list_calls), len(client.metrics_calls))
    meta, coverage = ensure_fresh(home, {
        ACTIVITY_STREAM: ("2026-10-23", "2026-10-24"),
        METRICS_STREAM: ("2026-10-23", "2026-10-24"),
    }, client=client, now_ts=NOW + 60)
    assert meta["attempted"] is False
    assert all(item["fresh"] for item in coverage.values())
    assert (len(client.list_calls), len(client.metrics_calls)) == calls
    sync_now(home, client=client, now_ts=NOW + 120)
    assert len(client.list_calls) == calls[0] + 1
    assert len(client.metrics_calls) == calls[1] + 1


def test_ttl_coverage_gap_still_runs_one_batch_per_stream(home, monkeypatch):
    client = fake_client(monkeypatch)
    sync_now(home, client=client, now_ts=NOW)
    calls = (len(client.list_calls), len(client.metrics_calls))
    meta, coverage = ensure_fresh(home, {
        ACTIVITY_STREAM: ("2026-10-01", "2026-10-24"),
        METRICS_STREAM: ("2026-09-01", "2026-10-24"),
    }, client=client, now_ts=NOW + 60)
    assert meta["attempted"] is True and meta["stale"] is True
    assert len(client.list_calls) == calls[0] + 1
    assert len(client.metrics_calls) == calls[1] + 1
    assert not coverage[ACTIVITY_STREAM]["covered"]
    assert not coverage[METRICS_STREAM]["covered"]
    assert coverage[ACTIVITY_STREAM]["backfill_hint"]
    assert coverage[METRICS_STREAM]["backfill_hint"]


def test_download_network_callback_has_no_db_transaction(home, monkeypatch):
    client = fake_client(monkeypatch)
    client.add("network", TS0)

    def assert_sync_db_writable(_pid):
        conn = sqlite3.connect(str(home / "sync.db"), timeout=0.2)
        try:
            conn.execute("pragma busy_timeout=200")
            conn.execute("begin immediate")
            conn.execute("update oa_sync_state set last_attempt_ts=123456 where stream=?",
                         (ACTIVITY_STREAM,))
            conn.commit()
        finally:
            conn.close()

    client.download_hook = assert_sync_db_writable
    result = sync_now(home, streams=(ACTIVITY_STREAM,), client=client, now_ts=NOW)
    assert result["streams"][ACTIVITY_STREAM]["succeeded"] == 1
    assert db_rows(home, "select last_attempt_ts from oa_sync_state where stream=?",
                   (ACTIVITY_STREAM,))[0]["last_attempt_ts"] == 123456


def test_activity_backfill_dry_limit_partial_then_continuous(home, monkeypatch):
    client = fake_client(monkeypatch)
    sync_now(home, streams=(ACTIVITY_STREAM,), client=client, now_ts=NOW)
    client.add("old-1", TS0 - 15 * 86400)
    client.add("old-2", TS0 - 10 * 86400)
    original = get_coverage(home, ACTIVITY_STREAM, "2026-10-01", "2026-10-19")
    assert original["covered"] is False
    assert "2026-10-01" in original["backfill_hint"]
    dry = backfill_activities(home, "2026-10-01", "2026-10-19", dry_run=True,
                              client=client, now_ts=NOW)
    assert dry["discovered"] == 2 and not client.download_calls
    assert get_coverage(home, ACTIVITY_STREAM)["coverage_start"] == "2026-10-20"
    limited = backfill_activities(home, "2026-10-01", "2026-10-19", limit=1,
                                  client=client, now_ts=NOW)
    assert limited["processed"] == 1
    assert get_coverage(home, ACTIVITY_STREAM)["coverage_start"] == "2026-10-20"
    client.fail_downloads.add("old-2")
    partial = backfill_activities(home, "2026-10-01", "2026-10-19",
                                  client=client, now_ts=NOW)
    assert partial["queued_remaining"] == 1
    assert get_coverage(home, ACTIVITY_STREAM)["coverage_start"] == "2026-10-20"
    client.fail_downloads.clear()
    done = backfill_activities(home, "2026-10-01", "2026-10-19",
                               client=client, now_ts=NOW + 60)
    assert done["complete"] is True and done["coverage_extended"] is True
    assert get_coverage(home, ACTIVITY_STREAM)["coverage_start"] == "2026-10-01"


def test_metrics_backfill_dry_limit_and_expand(home, monkeypatch):
    client = fake_client(monkeypatch)
    sync_now(home, streams=(METRICS_STREAM,), client=client, now_ts=NOW)
    client.records = [
        {"date": "2026-09-05", "resting_hr": 51, "hrv_ms": 70},
        {"date": "2026-09-10", "resting_hr": 52, "hrv_ms": 71},
    ]
    dry = backfill_metrics(home, "2026-09-01", "2026-09-26", dry_run=True,
                           client=client, now_ts=NOW)
    assert dry["discovered"] == 2 and athlete_count(home, "daily_metrics") == 0
    limited = backfill_metrics(home, "2026-09-01", "2026-09-26", limit=1,
                               client=client, now_ts=NOW)
    assert limited["processed"] == 1
    assert get_coverage(home, METRICS_STREAM)["coverage_start"] == "2026-09-27"
    done = backfill_metrics(home, "2026-09-01", "2026-09-26",
                            client=client, now_ts=NOW)
    assert done["complete"] is True and done["coverage_extended"] is True
    assert get_coverage(home, METRICS_STREAM)["coverage_start"] == "2026-09-01"
    assert athlete_count(home, "daily_metrics") == 2


def test_disjoint_backfill_does_not_invent_continuous_coverage(home, monkeypatch):
    client = fake_client(monkeypatch)
    sync_now(home, streams=(ACTIVITY_STREAM,), client=client, now_ts=NOW)
    result = backfill_activities(home, "2026-09-01", "2026-09-10",
                                 client=client, now_ts=NOW)
    assert result["complete"] is False
    assert result["coverage_extended"] is False
    assert result["coverage_gap"] == {"start": "2026-09-11", "end": "2026-10-19"}
    assert get_coverage(home, ACTIVITY_STREAM)["coverage_start"] == "2026-10-20"


def test_shared_fit_segment_crash_is_idempotent(home, monkeypatch):
    client = fake_client(monkeypatch)
    client.add("tri", TS0, sessions=2)
    original = sync_state._mark_segment_done
    crashed = {"value": False}

    def crash_after_ingest(*args):
        if not crashed["value"]:
            crashed["value"] = True
            raise RuntimeError("synthetic crash after athlete commit")
        return original(*args)

    monkeypatch.setattr(sync_state, "_mark_segment_done", crash_after_ingest)
    first = sync_now(home, streams=(ACTIVITY_STREAM,), client=client, now_ts=NOW)
    assert first["streams"][ACTIVITY_STREAM]["failed"] == 1
    assert athlete_count(home, "activity") == 2
    assert athlete_count(home, "activity_source") == 2
    monkeypatch.setattr(sync_state, "_mark_segment_done", original)
    second = sync_now(home, streams=(ACTIVITY_STREAM,), client=client, now_ts=NOW + 60)
    assert second["streams"][ACTIVITY_STREAM]["succeeded"] == 1
    assert athlete_count(home, "activity") == 2
    assert athlete_count(home, "activity_source") == 2
    segments = db_rows(home, "select * from oa_sync_segment order by segment_index")
    assert [row["status"] for row in segments] == ["done", "done"]
    assert segments[0]["fit_sha256"] != segments[1]["fit_sha256"]
    assert segments[0]["fit_path"] == segments[1]["fit_path"]
    assert db_rows(home, "select status from oa_sync_queue")[0]["status"] == "done"


def test_zip_segment_failure_resumes_only_unfinished(home, monkeypatch):
    client = fake_client(monkeypatch)
    client.add_zip("tri-zip", TS0)
    original = sync_state._ingest_segment
    calls = []
    failed = {"value": False}

    def fail_second_once(home_arg, config, parent_id, plan, rel_path):
        calls.append(plan["index"])
        if plan["index"] == 2 and not failed["value"]:
            failed["value"] = True
            raise RuntimeError("synthetic segment failure")
        return original(home_arg, config, parent_id, plan, rel_path)

    monkeypatch.setattr(sync_state, "_ingest_segment", fail_second_once)
    first = sync_now(home, streams=(ACTIVITY_STREAM,), client=client, now_ts=NOW)
    assert first["streams"][ACTIVITY_STREAM]["failed"] == 1
    assert db_rows(home, "select status from oa_sync_queue")[0]["status"] == "retry"
    calls.clear()
    second = sync_now(home, streams=(ACTIVITY_STREAM,), client=client, now_ts=NOW + 60)
    assert second["streams"][ACTIVITY_STREAM]["succeeded"] == 1
    assert calls == [2]
    assert [row["status"] for row in db_rows(
        home, "select status from oa_sync_segment order by segment_index")] == ["done", "done"]


def test_retry_backoff_and_fifth_attempt_failed(home, monkeypatch):
    client = fake_client(monkeypatch)
    client.add("bad", TS0)
    client.fail_downloads.add("bad")
    now = NOW
    for attempt in range(1, 6):
        result = sync_now(home, streams=(ACTIVITY_STREAM,), client=client, now_ts=now)
        assert result["streams"][ACTIVITY_STREAM]["processed"] == 1
        row = db_rows(home, "select * from oa_sync_queue")[0]
        assert row["attempts"] == attempt
        if attempt < 5:
            assert row["status"] == "retry"
            assert row["not_before_ts"] == now + 60 * (2 ** (attempt - 1))
            now = row["not_before_ts"]
        else:
            assert row["status"] == "failed" and row["not_before_ts"] is None
    calls = len(client.download_calls)
    later = sync_now(home, streams=(ACTIVITY_STREAM,), client=client, now_ts=now + 100000)
    assert later["streams"][ACTIVITY_STREAM]["processed"] == 0
    assert len(client.download_calls) == calls


def test_lease_recovery_does_not_increment_attempts(home, monkeypatch):
    client = fake_client(monkeypatch)
    client.add("lease", TS0)
    init_sync_db(home)
    conn = sqlite3.connect(str(home / "sync.db"))
    conn.execute("insert into oa_sync_queue (platform, platform_activity_id, sport_type, "
                 "start_ts, status, attempts, lease_owner, lease_until_ts, discovered_ts, "
                 "updated_ts) values ('coros', 'lease', 100, ?, 'processing', 2, 'dead', ?, 1, 1)",
                 (TS0, NOW - 1))
    conn.commit()
    conn.close()
    result = sync_now(home, streams=(ACTIVITY_STREAM,), client=client, now_ts=NOW)
    assert result["streams"][ACTIVITY_STREAM]["succeeded"] == 1
    row = db_rows(home, "select * from oa_sync_queue")[0]
    assert row["status"] == "done" and row["attempts"] == 3


def test_missing_credentials_raise_structured_error_not_exit(home, monkeypatch):
    monkeypatch.setattr(sync_state, "load_credentials", lambda: None)
    monkeypatch.delenv("COROS_EMAIL", raising=False)
    monkeypatch.delenv("COROS_PASSWORD", raising=False)
    with pytest.raises(SyncError) as caught:
        sync_now(home, streams=(ACTIVITY_STREAM,), now_ts=NOW)
    assert caught.value.code == "AUTH_REQUIRED"
    assert caught.value.details["missing"] == ["COROS_EMAIL", "COROS_PASSWORD"]


def test_sqlite_busy_is_structured(home, monkeypatch):
    init_sync_db(home)
    holder = sqlite3.connect(str(home / "sync.db"), timeout=0.05)
    holder.execute("begin immediate")
    holder.execute("update oa_sync_state set last_attempt_ts=1 where stream=?",
                   (ACTIVITY_STREAM,))
    monkeypatch.setattr(sync_state, "SQLITE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(sync_state, "SQLITE_BUSY_TIMEOUT_MS", 50)
    try:
        with pytest.raises(SyncError) as caught:
            sync_now(home, streams=(ACTIVITY_STREAM,), client=FakeCorosClient(), now_ts=NOW)
        assert caught.value.code == "DB_BUSY" and caught.value.retryable is True
    finally:
        holder.rollback()
        holder.close()


def test_invalid_timezone_is_structured_setup_error(home):
    (home / "config.toml").write_text(
        'timezone = "Not/AZone"\nactivity_since = "2026-10-20"\n', encoding="utf-8")
    init_sync_db(home)
    with pytest.raises(SyncError) as caught:
        get_coverage(home, ACTIVITY_STREAM, "2026-10-20", "2026-10-24")
    assert caught.value.code == "SETUP_REQUIRED"
    assert "IANA" in caught.value.next_action


def test_bracketed_ipv6_no_proxy_sync_and_dry_run(home, monkeypatch):
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "localhost,127.0.0.1,::1,[::1]")

    def send(_client, request, **_kwargs):
        return httpx.Response(200, json={
            "result": "0000", "data": {"dataList": [], "totalCount": 0},
        }, request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    client = CorosClient("synthetic", "synthetic", home=home, timezone_name="UTC")
    client.access_token = "synthetic"
    result = sync_now(home, streams=(ACTIVITY_STREAM,), client=client, now_ts=NOW)
    assert result["streams"][ACTIVITY_STREAM]["discovery_complete"] is True

    dry = backfill_activities(home, "2026-10-20", "2026-10-21",
                              dry_run=True, client=client, now_ts=NOW)
    assert dry["dry_run"] is True and dry["discovered"] == 0


@pytest.mark.parametrize("dry_run", [False, True], ids=["sync_now", "dry-run"])
def test_unsupported_proxy_config_is_structured(home, monkeypatch, dry_run):
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "localhost,::1/128")

    def forbidden_send(*_args, **_kwargs):
        raise AssertionError("无效代理配置应在发送请求前失败")

    monkeypatch.setattr(httpx.Client, "send", forbidden_send)
    client = CorosClient("synthetic", "synthetic", home=home, timezone_name="UTC")
    client.access_token = "synthetic"
    with pytest.raises(SyncError) as caught:
        if dry_run:
            backfill_activities(home, "2026-10-20", "2026-10-21",
                                dry_run=True, client=client, now_ts=NOW)
        else:
            sync_now(home, streams=(ACTIVITY_STREAM,), client=client, now_ts=NOW)
    assert caught.value.code == "PROXY_CONFIG_INVALID"
    assert "NO_PROXY" in caught.value.next_action
    if dry_run:
        state = db_rows(home, "select * from oa_sync_state where stream=?",
                        (ACTIVITY_STREAM,))[0]
        assert state["last_error_code"] is None
        assert state["discovery_cursor_json"] is None
