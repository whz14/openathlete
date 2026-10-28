import json

import pytest
from jsonschema import ValidationError, validate

from openathlete.mcp.envelope import failure, success
from openathlete.mcp.schemas import TOOL_NAMES, TOOLS_BY_NAME
from openathlete.mcp.server import create_server

EXPECTED_TOOLS = {
    "get_overview",
    "query_activities",
    "get_training_load",
    "get_recovery_status",
    "sync_now",
    "get_current_plan",
    "read_memory",
    "write_memory",
    "create_workout",
}

LOAD_ROW = {"date": "2026-10-24", "trimp": 50.0, "ctl": 40.0, "atl": 45.0, "tsb": -5.0}
RECOVERY_SOURCE = {
    "date": "2026-10-24",
    "source": "coros",
    "hrv_ms": 62.0,
    "resting_hr_bpm": 48.0,
    "sleep_total_s": 28800.0,
    "sleep_deep_s": 7200.0,
    "sleep_light_s": 14400.0,
    "sleep_rem_s": 5400.0,
    "sleep_awake_s": 1800.0,
    "sleep_score": 85.0,
    "spo2_pct": 97.0,
    "stress_avg": 25.0,
    "body_battery": 80.0,
    "steps": 10000.0,
    "calories_kcal": 2200.0,
}
RECOVERY_DATA = dict(
    RECOVERY_SOURCE,
    recovery_pct=None,
    missing=["recovery_pct"],
    sources=[RECOVERY_SOURCE],
)
VALID_DATA = {
    "get_overview": [{
        "window": {"start_date": "2026-10-18", "end_date": "2026-10-24",
                   "timezone": "UTC"},
        "training": {"activity_count": 1, "duration_s": 1800.0,
                     "distance_m": 5000.0, "trimp": 50.0, "unknown_trimp_count": 0},
        "daily": [{
            "date": "2026-10-24", "activity_count": 1, "duration_s": 1800.0,
            "distance_m": 5000.0, "trimp": 50.0, "unknown_trimp_count": 0,
            "ctl": 40.0, "atl": 45.0, "tsb": -5.0,
        }],
        "latest_load": LOAD_ROW,
        "recovery": RECOVERY_DATA,
        "next_race": {"date": "2026-12-06", "name": "东京马拉松", "days_until": 43},
        "sync": {"attempted": False},
    }],
    "query_activities": [{
        "activities": [{
            "activity_id": 1,
            "start_ts": 1790899200,
            "tz_offset_s": 0,
            "sport": "run",
            "sub_sport": None,
            "duration_s": 1800.0,
            "distance_m": 5000.0,
            "ascent_m": 20.0,
            "avg_hr_bpm": 150.0,
            "max_hr_bpm": 170.0,
            "avg_power_w": None,
            "avg_cadence_per_min": 180.0,
            "calories_kcal": 400.0,
            "avg_speed_mps": 2.778,
            "pace_s_per_km": 360.0,
            "trimp": 50.0,
            "type": "continuous",
            "structure": {"type": "continuous", "blocks": []},
            "zone_boundaries": [140, 150, 160, 170, 180, 190],
            "canonical_source": {
                "platform": "garmin", "platform_activity_id": "g1", "fit_sha256": "a" * 64,
            },
            "time_in_zone": [{"zone": 1, "time_s": 300.0}],
        }],
        "returned": 1,
        "truncated": False,
    }],
    "get_training_load": [
        {
            "daily": [LOAD_ROW], "latest": LOAD_ROW,
            "headline": "截至 2026-10-24：CTL 40.0、ATL 45.0、TSB -5.0。",
        },
        {
            "daily": [dict(LOAD_ROW, trimp=None, ctl=None, atl=None, tsb=None)],
            "latest": LOAD_ROW,
            "headline": "截至 2026-10-24：CTL 40.0、ATL 45.0、TSB -5.0。",
        },
    ],
    "get_recovery_status": [RECOVERY_DATA],
    "sync_now": [{
        "trigger": "manual",
        "streams": {"coros-activities": {"complete": False}},
        "complete": False,
        "stale": True,
        "batch_limit": {"coros-activities": 20, "coros-daily-metrics": None},
        "discovered": {"coros-activities": 3},
        "processed": {"coros-activities": 2},
        "succeeded": {"coros-activities": 1},
        "failed": {"coros-activities": 1},
        "queued_remaining": {"coros-activities": 4},
        "discovery_complete": {"coros-activities": False},
        "high_water_ts": {"coros-activities": 1790899200},
        "last_success_ts": {"coros-activities": 1790899200},
        "coverage": {"complete": False, "stale": True},
        "partial": True,
        "item_errors": [{"code": "UPSTREAM_UNAVAILABLE", "stream": "coros-activities"}],
        "next_action": "再次调用 sync_now",
    }],
    "get_current_plan": [{
        "plan": {
            "name": "plan:2026-10-05-冬训",
            "title": "冬训",
            "status": "in_progress",
            "source": "coach",
            "start_date": "2026-10-05",
            "end_date": "2026-11-30",
            "content": "\n# 阶段目标\n",
        },
        "week_logs": {"2026-10-24": "# 2026-10-24\n", "2026-10-25": None},
        "platform_schedule": {"available": False, "reason": "not_in_v0"},
        "warnings": [],
    }],
    "read_memory": [
        {
            "name": "athlete", "exists": True, "content": "# 画像\n",
            "sha256": "a" * 64, "bytes": 12, "modified_ts": 100,
        },
        {"list": "plans", "exists": False, "entries": [], "warnings": []},
    ],
    "write_memory": [{
        "name": "athlete", "sha256": "a" * 64, "bytes": 12,
        "written_ts": 100, "created": True,
    }],
    "create_workout": [
        {
            "status": "preview",
            "platform": "garmin",
            "preview": {
                "platform": "garmin", "date": "2026-10-25", "sport": "run",
                "name": "轻松跑", "description": "仅预览",
                "blocks": [{"kind": "steady", "role": "main"}],
            },
            "payload_hash": "c" * 64,
            "confirm_token": "one-time",
            "expires_ts": 200,
            "executed": False,
        },
        {
            "status": "created", "date": "2026-10-25", "name": "轻松跑",
            "platform": "garmin", "executed": True, "created": True, "readback": True,
            "scheduled": True, "workout_id": 123, "workout_schedule_id": "schedule-1",
        },
    ],
}


def test_exactly_nine_tools_with_output_schemas():
    assert set(TOOL_NAMES) == EXPECTED_TOOLS
    assert len(TOOL_NAMES) == 9
    for name in TOOL_NAMES:
        tool = TOOLS_BY_NAME[name]
        assert tool.input_schema["type"] == "object"
        assert tool.input_schema["additionalProperties"] is False
        assert tool.output_schema["type"] == "object"
        assert tool.output_schema["additionalProperties"] is False
        assert set(tool.output_schema["required"]) == {"ok", "data", "meta"}
        data = tool.output_schema["properties"]["data"]["anyOf"][0]
        assert data["additionalProperties"] is False
        assert data["required"]


def test_read_only_annotations_match_tool_side_effects():
    annotated = {
        name for name in TOOL_NAMES
        if TOOLS_BY_NAME[name].annotations
        and TOOLS_BY_NAME[name].annotations.read_only_hint is True
    }
    assert annotated == {"read_memory", "get_current_plan"}


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_output_schemas_validate_data_envelopes(name):
    schema = TOOLS_BY_NAME[name].output_schema
    for data in VALID_DATA[name]:
        result = success(name, data)
        validate(result.structured_content, schema)
    with pytest.raises(ValidationError):
        validate({}, schema["properties"]["data"])


def test_latest_pmc_rejects_unknown_values():
    unknown = dict(LOAD_ROW, trimp=None, ctl=None, atl=None, tsb=None)
    result = success("get_training_load", {
        "daily": [unknown], "latest": unknown, "headline": "数据不足",
    })
    with pytest.raises(ValidationError):
        validate(result.structured_content,
                 TOOLS_BY_NAME["get_training_load"].output_schema)


def test_read_memory_requires_exactly_one_mode():
    schema = TOOLS_BY_NAME["read_memory"].input_schema
    assert schema["oneOf"] == [{"required": ["name"]}, {"required": ["list"]}]


def test_success_envelope_has_equivalent_text_and_structured_content():
    result = success("get_overview", {"training": {"count": 0}},
                     {"coverage": {"complete": True}, "timezone": "Asia/Shanghai"})
    assert result.is_error is False
    assert result.structured_content["ok"] is True
    assert json.loads(result.content[0].text) == result.structured_content
    assert result.structured_content["meta"]["coverage"]["complete"] is True


def test_business_failure_is_structured_and_sanitized():
    result = failure("create_workout", "CONFIRM_TOKEN_REUSED", "确认已使用",
                     next_action="重新预览", details={"platform": "garmin"})
    assert result.is_error is True
    assert result.structured_content["data"] is None
    assert result.structured_content["error"]["code"] == "CONFIRM_TOKEN_REUSED"
    assert "traceback" not in result.content[0].text


def test_server_construction_does_not_open_network(monkeypatch):
    import socket

    def forbidden(*_args, **_kwargs):
        raise AssertionError("server construction attempted network")

    monkeypatch.setattr(socket, "socket", forbidden)
    server = create_server()
    assert server.get_request_handler("tools/list") is not None
    assert server.get_request_handler("tools/call") is not None
