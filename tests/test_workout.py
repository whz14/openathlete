import asyncio
import copy
import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone

import pytest

from openathlete.adapters.coros_official import CorosOfficialToolError
from openathlete.workout import (
    WorkoutError, coros_workout_payload, create_garmin_workout, execute_workout,
    garmin_workout_payload, preview_workout, resolve_workout_platform,
)


def steady(role="main", kind="duration", value=1200, intensity=None):
    return {"kind": "steady", "role": role,
            "target": {"kind": kind, "value": value}, "intensity": intensity}


def workout(blocks=None, sport="run"):
    return {"sport": sport, "name": "测试课表", "description": "合成测试",
            "blocks": blocks if blocks is not None else [steady()]}


def test_easy_20min_payload():
    payload = garmin_workout_payload(workout())
    assert payload["estimatedDurationInSecs"] == 1200
    step = payload["workoutSegments"][0]["workoutSteps"][0]
    assert step["type"] == "ExecutableStepDTO"
    assert step["stepType"]["stepTypeKey"] == "main"
    assert step["endCondition"]["conditionTypeKey"] == "time"
    assert step["endConditionValue"] == 1200
    assert step["targetType"]["workoutTargetTypeKey"] == "no.target"


def test_repeat_1km_payload():
    group = {"kind": "group", "cycles": 3, "steps": [steady(kind="distance", value=1000)]}
    payload = garmin_workout_payload(workout([group]))
    assert payload["estimatedDurationInSecs"] == 0
    group = payload["workoutSegments"][0]["workoutSteps"][0]
    assert group["type"] == "RepeatGroupDTO" and group["numberOfIterations"] == 3
    inner = group["workoutSteps"][0]
    assert inner["stepOrder"] == 2
    assert inner["stepType"]["stepTypeKey"] == "interval"
    assert inner["endCondition"]["conditionTypeKey"] == "distance"
    assert inner["endConditionValue"] == 1000
    assert inner["targetType"]["workoutTargetTypeKey"] == "no.target"


def test_intensity_mapping_and_metric_restrictions():
    zone = steady(intensity={"metric": "heart_rate", "kind": "zone", "zone": 3})
    absolute = steady(intensity={"metric": "heart_rate", "kind": "absolute",
                                 "start": 150, "end": 160})
    pace = steady(intensity={"metric": "pace", "kind": "absolute", "start": 240, "end": 300})
    power = steady(intensity={"metric": "power", "kind": "absolute", "start": 200, "end": 220})
    steps = garmin_workout_payload(
        workout([zone, absolute, pace, power]))["workoutSegments"][0]["workoutSteps"]
    assert steps[0]["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert steps[0]["zoneNumber"] == 3
    assert steps[1]["targetValueOne"] == 150 and steps[1]["targetValueTwo"] == 160
    assert steps[2]["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert steps[2]["targetValueOne"] == pytest.approx(1000 / 300)
    assert steps[2]["targetValueTwo"] == pytest.approx(1000 / 240)
    assert steps[3]["targetValueOne"] == 200 and steps[3]["targetValueTwo"] == 220

    block = steady(intensity={"metric": "power", "kind": "zone", "zone": 4})
    step = garmin_workout_payload(workout([block], sport="ride"))["workoutSegments"][0]["workoutSteps"][0]
    assert step["targetType"]["workoutTargetTypeKey"] == "power.zone"
    with pytest.raises(ValueError, match="跑步功率只支持 absolute"):
        garmin_workout_payload(workout([block], sport="run"))
    with pytest.raises(ValueError, match="骑行不使用配速"):
        garmin_workout_payload(workout([
            steady(intensity={"metric": "pace", "kind": "absolute", "start": 240, "end": 300})
        ], sport="ride"))


@pytest.mark.parametrize("bad", [
    {"sport": "trail_run", "name": "x", "description": "x", "blocks": [steady()]},
    {"sport": "run", "name": "x", "description": "x", "blocks": [
        {"kind": "steady", "role": "main", "target": {"kind": "climb", "value": 100},
         "intensity": None}]},
    {"sport": "run", "name": "x", "description": "x", "blocks": [
        {"kind": "steady", "role": "main", "target": {"kind": "duration", "value": 60}}]},
    {"sport": "run", "name": "x", "description": "x", "blocks": [
        steady(intensity={"metric": "heart_rate", "kind": "percent", "start": 80, "end": 90})]},
    {"sport": "run", "name": "x", "description": "x", "blocks": [
        steady(intensity={"metric": "effort_pace", "kind": "zone", "zone": 3})]},
])
def test_unsupported_inputs_fail(bad):
    with pytest.raises(ValueError):
        garmin_workout_payload(bad)


class FakeClient:
    def __init__(self):
        self.calls = []

    def create_workout(self, payload):
        self.calls.append("create")
        return {"workoutId": 11}

    def get_workout(self, workout_id):
        self.calls.append("read")
        return {"workoutId": workout_id}

    def schedule_workout(self, workout_id, date):
        self.calls.append("schedule")
        return {"workoutScheduleId": 22}


def test_invalid_date_fails_before_any_write():
    client = FakeClient()
    with pytest.raises(ValueError):
        create_garmin_workout(client, workout(), "2026-02-30")
    assert client.calls == []


NOW = int(datetime(2026, 10, 24, 12, tzinfo=timezone.utc).timestamp())


def unified(platform="coros", sport="run", blocks=None, date="2026-10-25"):
    value = workout(blocks, sport)
    value["date"] = date
    if platform is not None:
        value["platform"] = platform
    return value


def configure(home, platform="coros", primary="garmin"):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        'timezone = "UTC"\nworkout_platform = "{}"\nprimary_platform = "{}"\n'.format(
            platform, primary), encoding="utf-8")


def successful_coros(calls, response=None):
    async def create(home, payload):
        calls.append((home, payload))
        return response or {"status": "created"}
    return create


def test_coros_run_intensity_formats_and_explicit_null():
    blocks = [
        steady(intensity={"metric": "heart_rate", "kind": "zone", "zone": 6}),
        steady(intensity={"metric": "heart_rate", "kind": "percent", "start": 20, "end": 120}),
        steady(intensity={"metric": "pace", "kind": "absolute", "start": 240, "end": 300}),
        steady(intensity={"metric": "power", "kind": "absolute", "start": 200, "end": 200}),
        steady(intensity=None),
    ]
    course = coros_workout_payload(workout(blocks))
    assert course["sportType"] == 1 and course["courseName"] == "测试课表"
    sections = course["sections"]
    assert sections[0]["intensityType"] == 1 and sections[0]["sectionIntensity"] == 6
    assert sections[1]["intensityPercentStart"] == 20
    assert sections[1]["intensityPercentEnd"] == 120
    assert sections[2]["intensityType"] == 2
    assert sections[2]["intensityValueStart"] == 240
    assert sections[2]["intensityValueEnd"] == 300
    assert sections[3]["intensityType"] == 4 and sections[3]["intensityValueEnd"] == 200
    assert set(sections[4]) == {"sectionType", "targetType", "targetValue"}
    for section in sections[:4]:
        assert len({"sectionIntensity", "intensityPercentStart", "intensityValueStart"}
                   & set(section)) == 1


def test_coros_trail_climb_effort_pace_and_warmup_rules():
    blocks = [
        steady(role="warmup", kind="distance", value=1000),
        steady(role="main", kind="climb", value=300,
               intensity={"metric": "effort_pace", "kind": "percent", "start": 95, "end": 105}),
        steady(role="recovery", kind="distance", value=500,
               intensity={"metric": "effort_pace", "kind": "zone", "zone": 2}),
        steady(role="cooldown", kind="distance", value=1000),
    ]
    course = coros_workout_payload(workout(blocks, sport="trail_run"))
    assert course["sportType"] == 5
    assert [s["targetType"] for s in course["sections"]] == [1, 3, 1, 1]
    assert course["sections"][1]["intensityType"] == 3
    assert course["sections"][1]["intensityPercentStart"] == 95
    assert course["sections"][2]["sectionIntensity"] == 2


def test_coros_ride_power_zone_absolute_and_percent_pace_rules():
    blocks = [
        steady(intensity={"metric": "power", "kind": "zone", "zone": 7}),
        steady(intensity={"metric": "power", "kind": "absolute", "start": 180, "end": 240}),
        steady(intensity={"metric": "heart_rate", "kind": "percent", "start": 80, "end": 90}),
    ]
    sections = coros_workout_payload(workout(blocks, sport="ride"))["sections"]
    assert sections[0]["intensityType"] == 4 and sections[0]["sectionIntensity"] == 7
    assert sections[1]["intensityValueStart"] == 180
    assert sections[2]["intensityType"] == 1


@pytest.mark.parametrize("bad", [
    workout([steady()], sport="trail_run"),
    workout([steady(kind="climb", value=100)]),
    workout([steady(intensity={"metric": "pace", "kind": "zone", "zone": 2})], sport="ride"),
    workout([steady(intensity={"metric": "effort_pace", "kind": "absolute",
                               "start": 240, "end": 300})], sport="trail_run"),
    workout([steady(intensity={"metric": "power", "kind": "zone", "zone": 2})]),
    workout([steady(intensity={"metric": "heart_rate", "kind": "percent",
                               "start": 90, "end": 90})]),
])
def test_coros_unsupported_matrix_fail(bad):
    with pytest.raises(ValueError):
        coros_workout_payload(bad)


def test_coros_group_shape_and_restrictions():
    group = {"kind": "group", "cycles": 4, "steps": [
        steady(kind="distance", value=1000,
               intensity={"metric": "pace", "kind": "percent", "start": 100, "end": 105}),
        steady(role="recovery", value=120),
    ]}
    section = coros_workout_payload(workout([group]))["sections"][0]
    assert section["intervalGroup"] is True and section["repeats"] == 4
    assert set(section) == {"intervalGroup", "repeats", "sets"}
    assert section["sets"][0]["intensityPercentEnd"] == 105
    assert section["sets"][1]["sectionType"] == 3
    bad_groups = [
        {"kind": "group", "cycles": 0, "steps": [steady()]},
        {"kind": "group", "cycles": 21, "steps": [steady()]},
        {"kind": "group", "cycles": 2, "steps": [],},
        {"kind": "group", "cycles": 2, "steps": [steady(role="warmup")]},
        {"kind": "group", "cycles": 2, "steps": [group]},
    ]
    for bad in bad_groups:
        with pytest.raises(ValueError):
            coros_workout_payload(workout([bad]))


def test_preview_uses_workout_platform_not_primary_and_has_zero_network(monkeypatch, tmp_path):
    async def no_coros(*args):
        raise AssertionError("preview must not connect to COROS")

    def no_garmin(*args):
        raise AssertionError("preview must not construct Garmin client")

    monkeypatch.setattr("openathlete.adapters.coros_official._connect", no_coros)
    monkeypatch.setattr("openathlete.workout._default_garmin_client", no_garmin)
    for platform in ("coros", "garmin"):
        home = tmp_path / platform
        configure(home, platform=platform, primary="garmin" if platform == "coros" else "coros")
        result = preview_workout(home, unified(platform=None), NOW)
        assert result["status"] == "preview" and result["executed"] is False
        assert result["platform"] == platform and result["preview"]["platform"] == platform
        assert result["expires_ts"] == NOW + 600
        conn = sqlite3.connect(str(home / "sync.db"))
        try:
            row = conn.execute(
                "select token_hash,payload_hash,platform,consumed_ts,outcome "
                "from oa_write_confirmation").fetchone()
        finally:
            conn.close()
        assert row[0] == hashlib.sha256(result["confirm_token"].encode()).hexdigest()
        assert row[0] != result["confirm_token"]
        assert row[1] == result["payload_hash"] and row[2] == platform
        assert row[3:] == (None, None)
        assert result["confirm_token"].encode() not in (home / "sync.db").read_bytes()


def test_workout_platform_setup_and_explicit_platform_validation(tmp_path):
    (tmp_path / "config.toml").write_text('primary_platform = "garmin"\n', encoding="utf-8")
    with pytest.raises(WorkoutError) as err:
        resolve_workout_platform(tmp_path, unified(platform=None))
    assert err.value.code == "SETUP_REQUIRED"
    with pytest.raises(WorkoutError) as err:
        resolve_workout_platform(tmp_path, unified(platform="other"))
    assert err.value.code == "INVALID_ARGUMENT"


def test_coros_date_window_but_garmin_has_no_90_day_cap(tmp_path):
    with pytest.raises(WorkoutError) as err:
        preview_workout(tmp_path, unified(date="2026-10-23"), NOW)
    assert err.value.code == "INVALID_ARGUMENT"
    latest = preview_workout(tmp_path, unified(date="2027-01-22"), NOW)
    assert latest["preview"]["date"] == "2027-01-22"
    with pytest.raises(WorkoutError):
        preview_workout(tmp_path, unified(date="2027-01-23"), NOW)
    garmin = preview_workout(tmp_path, unified(platform="garmin", date="2027-10-25"), NOW)
    assert garmin["preview"]["date"] == "2027-10-25"
    with pytest.raises(WorkoutError):
        preview_workout(tmp_path, unified(platform="garmin", date="2026-02-30"), NOW)


def test_execute_coros_success_uses_final_payload_without_exposing_internal_id(tmp_path):
    calls = []
    preview = preview_workout(tmp_path, unified(), NOW)
    result = asyncio.run(execute_workout(
        tmp_path, unified(), preview["confirm_token"], NOW,
        coros_create=successful_coros(calls, {"idInPlan": "secret", "status": "created"})))
    assert result["status"] == "created" and result["executed"] is True
    assert result["created"] is True and result["scheduled"] is True
    assert result["date"] == "2026-10-25" and result["name"] == "测试课表"
    assert calls[0][1]["date"] == "20261025"
    assert calls[0][1]["course"]["courseName"] == "测试课表"
    assert "idInPlan" not in json.dumps(result)


def test_token_reuse_does_not_write_twice(tmp_path):
    calls = []
    preview = preview_workout(tmp_path, unified(), NOW)
    asyncio.run(execute_workout(
        tmp_path, unified(), preview["confirm_token"], NOW,
        coros_create=successful_coros(calls)))
    with pytest.raises(WorkoutError) as err:
        asyncio.run(execute_workout(
            tmp_path, unified(), preview["confirm_token"], NOW + 1,
            coros_create=successful_coros(calls)))
    assert err.value.code == "CONFIRM_TOKEN_REUSED" and len(calls) == 1


def test_token_expires_at_600_seconds_without_consumption(tmp_path):
    calls = []
    preview = preview_workout(tmp_path, unified(), NOW)
    with pytest.raises(WorkoutError) as err:
        asyncio.run(execute_workout(
            tmp_path, unified(), preview["confirm_token"], NOW + 600,
            coros_create=successful_coros(calls)))
    assert err.value.code == "CONFIRM_TOKEN_EXPIRED" and calls == []
    conn = sqlite3.connect(str(tmp_path / "sync.db"))
    try:
        assert conn.execute("select consumed_ts from oa_write_confirmation").fetchone()[0] is None
    finally:
        conn.close()


def test_token_concurrent_consumption_has_one_remote_call(tmp_path):
    calls = []

    async def slow_create(home, payload):
        calls.append((home, payload))
        await asyncio.sleep(0.03)
        return {"status": "created"}

    preview = preview_workout(tmp_path, unified(), NOW)

    async def scenario():
        return await asyncio.gather(
            execute_workout(tmp_path, unified(), preview["confirm_token"], NOW,
                            coros_create=slow_create),
            execute_workout(tmp_path, unified(), preview["confirm_token"], NOW,
                            coros_create=slow_create),
            return_exceptions=True)

    results = asyncio.run(scenario())
    assert sum(isinstance(value, dict) for value in results) == 1
    errors = [value for value in results if isinstance(value, WorkoutError)]
    assert len(errors) == 1 and errors[0].code == "CONFIRM_TOKEN_REUSED"
    assert len(calls) == 1


def test_payload_and_platform_mismatch_do_not_consume_or_write(tmp_path):
    for scenario in ("payload", "platform"):
        home = tmp_path / scenario
        home.mkdir()
        calls = []
        client = FakeClient()
        original = unified(platform=None if scenario == "platform" else "coros")
        if scenario == "platform":
            configure(home, "coros")
        preview = preview_workout(home, original, NOW)
        changed = copy.deepcopy(original)
        if scenario == "payload":
            changed["blocks"][0]["target"]["value"] = 1800
        else:
            configure(home, "garmin")
        with pytest.raises(WorkoutError) as err:
            asyncio.run(execute_workout(
                home, changed, preview["confirm_token"], NOW,
                coros_create=successful_coros(calls), garmin_client=client))
        assert err.value.code == "CONFIRM_PAYLOAD_MISMATCH"
        assert calls == [] and client.calls == []
        conn = sqlite3.connect(str(home / "sync.db"))
        try:
            consumed = conn.execute(
                "select consumed_ts from oa_write_confirmation").fetchone()[0]
        finally:
            conn.close()
        assert consumed is None

        if scenario == "platform":
            configure(home, "coros")
        result = asyncio.run(execute_workout(
            home, original, preview["confirm_token"], NOW,
            coros_create=successful_coros(calls), garmin_client=client))
        assert result["created"] is True and len(calls) == 1


def test_coros_unknown_outcome_never_retries_and_allows_new_preview(tmp_path):
    calls = []
    failed = True

    async def flaky_create(home, payload):
        calls.append((home, payload))
        if failed:
            raise TimeoutError("response lost")
        return {"status": "created"}

    preview = preview_workout(tmp_path, unified(), NOW)
    with pytest.raises(WorkoutError) as err:
        asyncio.run(execute_workout(
            tmp_path, unified(), preview["confirm_token"], NOW, coros_create=flaky_create))
    assert err.value.code == "WORKOUT_OUTCOME_UNKNOWN"
    assert err.value.retryable is False and len(calls) == 1
    with pytest.raises(WorkoutError) as reused:
        asyncio.run(execute_workout(
            tmp_path, unified(), preview["confirm_token"], NOW + 1,
            coros_create=flaky_create))
    assert reused.value.code == "CONFIRM_TOKEN_REUSED" and len(calls) == 1
    new_preview = preview_workout(tmp_path, unified(), NOW + 1)
    failed = False
    result = asyncio.run(execute_workout(
        tmp_path, unified(), new_preview["confirm_token"], NOW + 2,
        coros_create=flaky_create))
    assert result["created"] is True and len(calls) == 2


def test_coros_explicit_rejection_is_failed_not_unknown(tmp_path):
    calls = []

    async def rejected(home, payload):
        calls.append((home, payload))
        raise CorosOfficialToolError()

    preview = preview_workout(tmp_path, unified(), NOW)
    with pytest.raises(WorkoutError) as err:
        asyncio.run(execute_workout(
            tmp_path, unified(), preview["confirm_token"], NOW, coros_create=rejected))
    assert err.value.code == "WRITE_FAILED" and len(calls) == 1


class StageFakeClient(FakeClient):
    def __init__(self, create_error=None, read_error=None, schedule_exception=None):
        super().__init__()
        self.create_error = create_error
        self.read_error = read_error
        self.schedule_exception = schedule_exception

    def create_workout(self, payload):
        self.calls.append("create")
        if self.create_error:
            raise self.create_error
        return {"workoutId": 11}

    def get_workout(self, workout_id):
        self.calls.append("read")
        if self.read_error:
            raise self.read_error
        return {"workoutId": workout_id}

    def schedule_workout(self, workout_id, date):
        self.calls.append("schedule")
        if self.schedule_exception:
            raise self.schedule_exception
        return {"workoutScheduleId": 22}


def execute_garmin_with_client(home, client):
    value = unified(platform="garmin")
    preview = preview_workout(home, value, NOW)
    return asyncio.run(execute_workout(
        home, value, preview["confirm_token"], NOW, garmin_client=client))


def test_garmin_readback_failure_preserves_created_id(tmp_path):
    client = StageFakeClient(read_error=RuntimeError("read failed"))
    with pytest.raises(WorkoutError) as err:
        execute_garmin_with_client(tmp_path, client)
    assert err.value.code == "WRITE_FAILED"
    assert err.value.details["stage"] == "readback"
    assert err.value.details["created"] is True and err.value.details["readback"] is False
    assert err.value.details["scheduled"] is False and err.value.details["workout_id"] == 11
    assert client.calls == ["create", "read"]


@pytest.mark.parametrize("exception,code,scheduled", [
    (RuntimeError("denied"), "WORKOUT_SCHEDULE_FAILED", False),
    (TimeoutError("timeout"), "WORKOUT_OUTCOME_UNKNOWN", None),
])
def test_garmin_schedule_partial_success_is_explicit(tmp_path, exception, code, scheduled):
    client = StageFakeClient(schedule_exception=exception)
    with pytest.raises(WorkoutError) as err:
        execute_garmin_with_client(tmp_path, client)
    assert err.value.code == code and err.value.details["created"] is True
    assert err.value.details["scheduled"] is scheduled
    assert err.value.details["stage"] == "schedule" and err.value.details["workout_id"] == 11
    assert client.calls == ["create", "read", "schedule"]


def test_garmin_create_timeout_is_unknown_without_recreate(tmp_path):
    client = StageFakeClient(create_error=TimeoutError("timeout"))
    with pytest.raises(WorkoutError) as err:
        execute_garmin_with_client(tmp_path, client)
    assert err.value.code == "WORKOUT_OUTCOME_UNKNOWN"
    assert err.value.details["created"] is None and client.calls == ["create"]


def fail_outcome_audit(*args):
    raise WorkoutError("DB_BUSY", "合成审计写库故障", retryable=True,
                       next_action="这个审计错误不应覆盖平台结果")


def test_success_audit_failure_preserves_platform_result(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.ERROR, logger="openathlete.workout")
    monkeypatch.setattr("openathlete.workout._record_outcome", fail_outcome_audit)
    client = StageFakeClient()
    result = execute_garmin_with_client(tmp_path, client)
    assert result["status"] == "created" and result["executed"] is True
    assert result["created"] is True and result["scheduled"] is True
    assert result["workout_id"] == 11 and result["workout_schedule_id"] == 22
    assert result["outcome_recorded"] is False
    assert "本地审计" in result["warning"] and "不要重复创建" in result["warning"]
    assert client.calls == ["create", "read", "schedule"]
    assert "本地审计写入失败" in caplog.text


def test_schedule_failure_audit_failure_preserves_original_error(
        monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.ERROR, logger="openathlete.workout")
    monkeypatch.setattr("openathlete.workout._record_outcome", fail_outcome_audit)
    client = StageFakeClient(schedule_exception=RuntimeError("denied"))
    with pytest.raises(WorkoutError) as err:
        execute_garmin_with_client(tmp_path, client)
    error = err.value
    assert error.code == "WORKOUT_SCHEDULE_FAILED" and error.retryable is False
    assert error.details["created"] is True and error.details["readback"] is True
    assert error.details["scheduled"] is False and error.details["stage"] == "schedule"
    assert error.details["workout_id"] == 11
    assert error.details["outcome_recorded"] is False
    assert "--id 11 --date 2026-10-25" in error.next_action
    assert "尚未排期" in error.next_action
    assert "排期被明确拒绝" in error.message
    assert client.calls == ["create", "read", "schedule"]
    assert "本地审计写入失败" in caplog.text


def nested_workout(platform):
    heart = {"metric": "heart_rate", "kind": "zone", "zone": 2}
    return unified(platform=platform, blocks=[
        steady(intensity=heart),
        {"kind": "group", "cycles": 2, "steps": [
            steady(kind="distance", value=400, intensity=dict(heart)),
            steady(role="recovery", value=60),
        ]},
    ])


def add_unknown_field(value, location):
    if location == "root":
        value["unexpected"] = True
    elif location == "steady":
        value["blocks"][0]["unexpected"] = True
    elif location == "group":
        value["blocks"][1]["unexpected"] = True
    elif location == "target":
        value["blocks"][0]["target"]["unit"] = "s"
    elif location == "intensity":
        value["blocks"][0]["intensity"]["basis"] = "max_hr"
    elif location == "group_intensity":
        value["blocks"][1]["steps"][0]["intensity"]["basis"] = "max_hr"
    else:
        raise AssertionError(location)


@pytest.mark.parametrize("platform,location", [
    ("coros", "root"), ("garmin", "root"),
    ("coros", "steady"), ("garmin", "target"),
    ("coros", "intensity"), ("garmin", "group"),
    ("coros", "group_intensity"),
])
def test_unknown_fields_rejected_by_preview_and_execute(tmp_path, platform, location):
    valid = nested_workout(platform)
    bad = copy.deepcopy(valid)
    add_unknown_field(bad, location)
    with pytest.raises(WorkoutError) as preview_error:
        preview_workout(tmp_path, bad, NOW)
    assert preview_error.value.code == "WORKOUT_UNSUPPORTED"
    assert "未知字段" in str(preview_error.value)
    assert not (tmp_path / "sync.db").exists()

    preview = preview_workout(tmp_path, valid, NOW)
    coros_calls = []
    client = FakeClient()
    with pytest.raises(WorkoutError) as execute_error:
        asyncio.run(execute_workout(
            tmp_path, bad, preview["confirm_token"], NOW,
            coros_create=successful_coros(coros_calls), garmin_client=client))
    assert execute_error.value.code == "WORKOUT_UNSUPPORTED"
    assert coros_calls == [] and client.calls == []
    conn = sqlite3.connect(str(tmp_path / "sync.db"))
    try:
        assert conn.execute("select consumed_ts from oa_write_confirmation").fetchone()[0] is None
    finally:
        conn.close()
