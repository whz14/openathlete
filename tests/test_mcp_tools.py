import asyncio
from datetime import datetime, timezone

import pytest
from jsonschema import validate

from openathlete.config import set_config_values
from openathlete.db.athlete_db import connect, init_db
from openathlete.mcp.schemas import TOOLS_BY_NAME
from openathlete.mcp.tools import call_tool
from openathlete.sync_state import SyncError


def call(home, name, args=None):
    result = asyncio.run(call_tool(name, args or {}, home))
    validate(result.structured_content, TOOLS_BY_NAME[name].output_schema)
    return result


@pytest.fixture(autouse=True)
def fresh_sync(monkeypatch):
    def ensure_fresh(home, windows):
        coverage = {
            stream: {
                "complete": True,
                "stale": False,
                "actual_start": window[0],
                "actual_end": window[1],
                "last_sync_ts": 100,
                "missing": [],
            }
            for stream, window in windows.items()
        }
        return {"attempted": False, "success": True, "trigger": "ttl"}, coverage

    monkeypatch.setattr("openathlete.sync_state.ensure_fresh", ensure_fresh)


def make_home(tmp_path):
    set_config_values(tmp_path, timezone="UTC", activity_since="2026-01-01",
                      workout_platform="garmin")
    conn = connect(tmp_path / "athlete.db")
    init_db(conn)
    conn.close()
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "races.md").write_text(
        "# 赛事\n\n## 目标赛事\n\n（无）\n\n## 历史赛事\n", encoding="utf-8")
    return tmp_path


def add_activity(home):
    conn = connect(home / "athlete.db")
    init_db(conn)
    start = int(datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp())
    conn.execute("insert into activity (id,start_ts,tz_offset_s,sport,duration_s,distance_m) "
                 "values (1,?,0,'run',1800,5000)", (start,))
    conn.execute("insert into activity_source (activity_id,platform,platform_activity_id,"
                 "fit_path,fit_sha256,is_canonical) values (1,'garmin','g1','x',?,1)",
                 ("b" * 64,))
    conn.commit()
    conn.close()


def test_invalid_arguments_are_business_errors(tmp_path):
    home = make_home(tmp_path)
    for name, args in (
        ("query_activities", {"days": True}),
        ("read_memory", {"name": "athlete", "list": "plans"}),
        ("write_memory", {"name": "athlete"}),
        ("create_workout", {}),
    ):
        result = call(home, name, args)
        assert result.is_error is True
        assert result.structured_content["error"]["code"] == "INVALID_ARGUMENT"


def test_sync_failure_returns_old_data_or_auth_error(tmp_path, monkeypatch):
    home = make_home(tmp_path)

    def unavailable(_home, _windows):
        raise SyncError("AUTH_REQUIRED", "需要配置 COROS web 凭据",
                        next_action="用户自行填写凭据")

    monkeypatch.setattr("openathlete.sync_state.ensure_fresh", unavailable)
    empty = call(home, "query_activities", {"start_date": "2026-10-01"})
    assert empty.is_error is True
    assert empty.structured_content["error"]["code"] == "AUTH_REQUIRED"

    add_activity(home)
    old = call(home, "query_activities", {"start_date": "2026-10-01"})
    assert old.is_error is False
    assert old.structured_content["meta"]["coverage"]["stale"] is True
    assert old.structured_content["meta"]["warnings"][0]["code"] == "AUTH_REQUIRED"


def test_sync_now_flattens_stream_summaries(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    monkeypatch.setattr("openathlete.sync_state.sync_now", lambda _home: {
        "trigger": "manual",
        "streams": {
            "coros-activities": {
                "batch_limit": 20, "discovered": 2, "processed": 2, "succeeded": 2,
                "failed": 0, "queued_remaining": 1, "discovery_complete": False,
                "high_water_ts": 100, "last_success_ts": 90,
                "coverage": {"complete": False, "stale": True},
                "complete": False, "stale": True, "item_errors": [],
            },
            "coros-daily-metrics": {
                "discovered": 2, "processed": 2, "succeeded": 2, "failed": 0,
                "queued_remaining": 0, "discovery_complete": True,
                "high_water_ts": None, "last_success_ts": 90,
                "coverage": {"complete": True, "stale": False},
                "complete": True, "stale": False, "item_errors": [],
            },
        },
        "complete": False, "stale": True, "partial": False,
    })
    synced = call(home, "sync_now")
    assert synced.is_error is False
    data = synced.structured_content["data"]
    assert data["trigger"] == "manual"
    assert data["processed"]["coros-activities"] == 2
    assert data["queued_remaining"] == {"coros-activities": 1, "coros-daily-metrics": 0}
    assert data["next_action"]


def test_real_empty_coverage_is_not_missing_or_insufficient(tmp_path):
    home = make_home(tmp_path)
    result = call(home, "query_activities", {"start_date": "2026-10-01"})
    assert result.is_error is False
    assert result.structured_content["data"]["activities"] == []
    assert result.structured_content["meta"]["coverage"]["complete"] is True
    assert "activities" not in result.structured_content["meta"]["coverage"]["missing"]


def test_no_local_data_and_incomplete_coverage_is_an_error(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    monkeypatch.setattr("openathlete.sync_state.ensure_fresh", lambda _home, windows: (
        {"attempted": True, "success": True},
        {stream: {"complete": False, "stale": True, "missing": ["queue"]}
         for stream in windows},
    ))
    result = call(home, "query_activities", {"start_date": "2026-10-01"})
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "COVERAGE_INSUFFICIENT"


def test_conflicts_are_not_reported_as_empty_activities(tmp_path):
    home = make_home(tmp_path)
    conn = connect(home / "athlete.db")
    init_db(conn)
    start = int(datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp())
    conn.execute("insert into activity (id,start_ts,tz_offset_s,sport) values (1,?,0,'run')",
                 (start,))
    conn.execute("insert into activity_source (activity_id,platform,platform_activity_id,"
                 "fit_path,fit_sha256,is_canonical) values (1,'garmin','g1','x',?,0)",
                 ("b" * 64,))
    conn.commit()
    conn.close()
    result = call(home, "query_activities", {"start_date": "2026-10-01"})
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "CANONICAL_CONFLICT"


def test_pmc_initialized_requires_anchors_and_valid_series(tmp_path):
    home = make_home(tmp_path)
    add_activity(home)
    conn = connect(home / "athlete.db")
    conn.execute("update activity set trimp=null")
    conn.execute("insert into daily_load values ('2026-10-02',0,1,2,-1)")
    conn.commit()
    conn.close()
    result = call(home, "get_training_load", {"days": 7, "end_date": "2026-10-03"})
    assert result.is_error is False
    coverage = result.structured_content["meta"]["coverage"]
    assert coverage["pmc_initialized"] is False
    assert coverage["complete"] is False


def test_pmc_true_empty_coverage_produces_real_zero(tmp_path):
    home = make_home(tmp_path)
    result = call(home, "get_training_load", {"days": 7, "end_date": "2026-10-03"})
    assert result.is_error is False
    assert result.structured_content["meta"]["coverage"]["pmc_initialized"] is True
    assert result.structured_content["data"]["latest"]["trimp"] == 0
    assert len(result.structured_content["data"]["daily"]) == 7


def test_uninitialized_pmc_and_stale_local_coverage_match_schema(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    add_activity(home)
    conn = connect(home / "athlete.db")
    conn.execute("insert into daily_load values ('2026-09-30',40,1,2,-1)")
    conn.execute("insert into daily_load values ('2026-10-01',null,null,null,null)")
    conn.execute("insert into daily_load values ('2026-10-02',50,null,null,null)")
    conn.commit()
    conn.close()

    def unavailable(_home, _windows):
        raise SyncError("SETUP_REQUIRED", "个人时区不可用", next_action="设置 IANA 时区")

    monkeypatch.setattr("openathlete.sync_state.ensure_fresh", unavailable)
    result = call(home, "get_training_load", {"days": 7, "end_date": "2026-10-03"})
    assert result.is_error is False
    data = result.structured_content["data"]
    assert data["latest"]["date"] == "2026-09-30"
    assert data["daily"][1]["trimp"] is None
    assert data["daily"][2]["ctl"] is None
    coverage = result.structured_content["meta"]["coverage"]
    assert coverage["pmc_initialized"] is False
    assert coverage["actual_start"] == "2026-09-30"
    assert coverage["actual_end"] == "2026-09-30"
    assert result.structured_content["meta"]["warnings"][0]["code"] == "SETUP_REQUIRED"


def test_invalid_timezone_does_not_break_error_envelope_or_memory(tmp_path):
    home = make_home(tmp_path)
    set_config_values(home, timezone="Not/AZone")
    result = call(home, "query_activities", {"start_date": "2026-10-01"})
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "SETUP_REQUIRED"
    assert "IANA" in result.structured_content["error"]["next_action"]

    memory = call(home, "read_memory", {"name": "athlete"})
    assert memory.is_error is False
    assert memory.structured_content["data"]["exists"] is False


def test_garmin_execute_success_matches_output_schema(tmp_path, monkeypatch):
    home = make_home(tmp_path)

    class FakeGarmin:
        def __init__(self):
            self.calls = []

        def ensure_login(self):
            self.calls.append("login")

        def create_workout(self, _payload):
            self.calls.append("create")
            return {"workoutId": 11}

        def get_workout(self, workout_id):
            self.calls.append("read")
            return {"workoutId": workout_id}

        def schedule_workout(self, _workout_id, _date):
            self.calls.append("schedule")
            return {"workoutScheduleId": 22}

    client = FakeGarmin()
    monkeypatch.setattr("openathlete.workout._default_garmin_client", lambda _home: client)
    workout = {
        "date": "2026-10-25", "sport": "run", "name": "测试课",
        "description": "仅 fake client",
        "blocks": [{"kind": "steady", "role": "main",
                    "target": {"kind": "duration", "value": 600}, "intensity": None}],
    }
    preview = call(home, "create_workout", {"workout": workout})
    token = preview.structured_content["data"]["confirm_token"]
    created = call(home, "create_workout", {"workout": workout, "confirm_token": token})
    assert created.is_error is False
    assert created.structured_content["data"]["status"] == "created"
    assert created.structured_content["data"]["scheduled"] is True
    assert client.calls == ["login", "create", "read", "schedule"]
