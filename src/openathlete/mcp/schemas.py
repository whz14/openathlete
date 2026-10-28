"""九个统一工具的静态协议 schema；构造过程不执行任何 IO。"""
from mcp_types import Tool, ToolAnnotations


def _object(properties=None, required=None):
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def _output(data_properties=None, required=None, any_of=None):
    error = _object({
        "code": {"type": "string"},
        "message": {"type": "string"},
        "retryable": {"type": "boolean"},
        "next_action": {"type": "string"},
        "details": {"type": "object"},
    }, ["code", "message", "retryable", "next_action", "details"])
    meta = _object({
        "schema_version": {"type": "string"},
        "tool": {"type": "string"},
        "request_id": {"type": "string"},
        "generated_at": {"type": "string"},
        "timezone": {"type": "string"},
        "units": {"type": "object"},
        "coverage": {"type": "object"},
        "warnings": {"type": "array"},
        "sync": {"type": "object"},
    }, ["schema_version", "tool", "request_id", "generated_at", "timezone",
        "units", "coverage", "warnings", "sync"])
    data = _object(data_properties, required)
    if any_of is not None:
        data["anyOf"] = any_of
    return {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "data": {"anyOf": [data, {"type": "null"}]},
            "error": error,
            "meta": meta,
        },
        "required": ["ok", "data", "meta"],
        "additionalProperties": False,
    }


def _tool(name, description, input_schema, data_properties=None, required=None, any_of=None,
          annotations=None):
    return Tool(
        name=name,
        description=description,
        input_schema=input_schema,
        output_schema=_output(data_properties, required, any_of),
        annotations=annotations,
    )


_DAYS_90 = {"type": "integer", "minimum": 1, "maximum": 90, "default": 7}
_DATE = {"type": "string", "format": "date"}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}
_NULL_NUMBER = {"type": ["number", "null"]}
_NULL_STRING = {"type": ["string", "null"]}
_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}
_OBJECT_ARRAY = {"type": "array", "items": {"type": "object"}}
_READ_ONLY = ToolAnnotations(read_only_hint=True)


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def _stream_map(value_type):
    return {"type": "object", "additionalProperties": {"type": value_type}}


_WARNING = _object({
    "code": {"type": "string"},
    "message": {"type": "string"},
    "name": {"type": "string"},
    "line": {"type": "integer"},
}, ["code", "message"])


def _load_row(nullable=False):
    number = _NULL_NUMBER if nullable else {"type": "number"}
    return _object({
        "date": {"type": "string"},
        "trimp": number,
        "ctl": number,
        "atl": number,
        "tsb": number,
    }, ["date", "trimp", "ctl", "atl", "tsb"])


_LOAD_ROW = _load_row(nullable=True)
_VALID_LOAD_ROW = _load_row()

_OVERVIEW_DAY = _object({
    "date": {"type": "string"},
    "activity_count": {"type": "integer"},
    "duration_s": {"type": "number"},
    "distance_m": {"type": "number"},
    "trimp": _NULL_NUMBER,
    "unknown_trimp_count": {"type": "integer"},
    "ctl": _NULL_NUMBER,
    "atl": _NULL_NUMBER,
    "tsb": _NULL_NUMBER,
}, ["date", "activity_count", "duration_s", "distance_m", "trimp",
    "unknown_trimp_count", "ctl", "atl", "tsb"])

_RECOVERY_FIELDS = {
    "hrv_ms": _NULL_NUMBER,
    "resting_hr_bpm": _NULL_NUMBER,
    "sleep_total_s": _NULL_NUMBER,
    "sleep_deep_s": _NULL_NUMBER,
    "sleep_light_s": _NULL_NUMBER,
    "sleep_rem_s": _NULL_NUMBER,
    "sleep_awake_s": _NULL_NUMBER,
    "sleep_score": _NULL_NUMBER,
    "spo2_pct": _NULL_NUMBER,
    "stress_avg": _NULL_NUMBER,
    "body_battery": _NULL_NUMBER,
    "steps": _NULL_NUMBER,
    "calories_kcal": _NULL_NUMBER,
}
_RECOVERY_SOURCE = _object({
    "date": {"type": "string"},
    "source": {"type": "string"},
    **_RECOVERY_FIELDS,
}, ["date", "source", *_RECOVERY_FIELDS])
_RECOVERY = _object({
    "date": {"type": "string"},
    "recovery_pct": _NULL_NUMBER,
    **_RECOVERY_FIELDS,
    "source": _NULL_STRING,
    "missing": _STRING_ARRAY,
    "sources": {"type": "array", "items": _RECOVERY_SOURCE},
}, ["date", "recovery_pct", *_RECOVERY_FIELDS, "source", "missing", "sources"])

_ACTIVITY = _object({
    "activity_id": {"type": "integer"},
    "start_ts": {"type": "integer"},
    "tz_offset_s": {"type": ["integer", "null"]},
    "sport": {"type": "string"},
    "sub_sport": _NULL_STRING,
    "duration_s": _NULL_NUMBER,
    "distance_m": _NULL_NUMBER,
    "ascent_m": _NULL_NUMBER,
    "avg_hr_bpm": _NULL_NUMBER,
    "max_hr_bpm": _NULL_NUMBER,
    "avg_power_w": _NULL_NUMBER,
    "avg_cadence_per_min": _NULL_NUMBER,
    "calories_kcal": _NULL_NUMBER,
    "avg_speed_mps": _NULL_NUMBER,
    "pace_s_per_km": _NULL_NUMBER,
    "trimp": _NULL_NUMBER,
    "type": _NULL_STRING,
    "structure": _nullable({"type": "object"}),
    "zone_boundaries": _nullable({"type": "array", "items": {"type": "number"}}),
    "canonical_source": _object({
        "platform": {"type": "string"},
        "platform_activity_id": {"type": "string"},
        "fit_sha256": {"type": "string"},
    }, ["platform", "platform_activity_id", "fit_sha256"]),
    "time_in_zone": {"type": "array", "items": _object({
        "zone": {"type": "integer"},
        "time_s": {"type": "number"},
    }, ["zone", "time_s"])},
}, [
    "activity_id", "start_ts", "tz_offset_s", "sport", "sub_sport", "duration_s",
    "distance_m", "ascent_m", "avg_hr_bpm", "max_hr_bpm", "avg_power_w",
    "avg_cadence_per_min", "calories_kcal", "avg_speed_mps", "pace_s_per_km",
    "trimp", "type",
])

_NEXT_RACE = _object({
    "date": {"type": "string"},
    "name": {"type": "string"},
    "days_until": {"type": "integer"},
}, ["date", "name", "days_until"])

_PLAN = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "title": {"type": "string"},
        "status": {"type": "string"},
        "source": {"type": "string"},
        "start_date": {"type": "string"},
        "end_date": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["name", "title", "status", "source", "start_date", "end_date", "content"],
}

_MEMORY_ENTRY = _object({
    "name": {"type": "string"},
    "sha256": {"type": "string"},
    "bytes": {"type": "integer"},
    "modified_ts": _NULL_NUMBER,
}, ["name", "sha256", "bytes", "modified_ts"])

_WORKOUT_PREVIEW = _object({
    "platform": {"type": "string"},
    "date": {"type": "string"},
    "sport": {"type": "string"},
    "name": {"type": "string"},
    "description": {"type": "string"},
    "blocks": _OBJECT_ARRAY,
}, ["platform", "date", "sport", "name", "description", "blocks"])

TOOLS = [
    _tool(
        "get_overview",
        "一页查看近期训练、统一负荷、恢复和下一赛事；必要时按覆盖状态增量同步。",
        _object({"days": _DAYS_90, "end_date": _DATE}),
        {
            "window": _object({
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
                "timezone": {"type": "string"},
            }, ["start_date", "end_date", "timezone"]),
            "training": _object({
                "activity_count": {"type": "integer"},
                "duration_s": {"type": "number"},
                "distance_m": {"type": "number"},
                "trimp": _NULL_NUMBER,
                "unknown_trimp_count": {"type": "integer"},
            }, ["activity_count", "duration_s", "distance_m", "trimp",
                "unknown_trimp_count"]),
            "daily": {"type": "array", "items": _OVERVIEW_DAY},
            "latest_load": _nullable(_VALID_LOAD_ROW),
            "recovery": _nullable(_RECOVERY),
            "next_race": _nullable(_NEXT_RACE),
            "sync": {"type": "object"},
        },
        ["window", "training", "daily", "latest_load", "recovery", "next_race", "sync"],
    ),
    _tool(
        "query_activities",
        "查询 canonical 活动；detail=true 时附结构与细区间，不返回原始 FIT 或本地路径。",
        _object({"days": _DAYS_90, "start_date": _DATE, "end_date": _DATE,
                 "sport": {"type": "string"}, "limit": _LIMIT,
                 "detail": {"type": "boolean", "default": False}}),
        {
            "activities": {"type": "array", "items": _ACTIVITY},
            "returned": {"type": "integer"},
            "truncated": {"type": "boolean"},
        },
        ["activities", "returned", "truncated"],
    ),
    _tool(
        "get_training_load",
        "读取本地 Edwards TRIMP 与 PMC 趋势，并显示锚点和历史覆盖范围。",
        _object({"days": {"type": "integer", "minimum": 7, "maximum": 365, "default": 42},
                 "end_date": _DATE}),
        {
            "daily": {"type": "array", "items": _LOAD_ROW},
            "latest": _nullable(_VALID_LOAD_ROW),
            "headline": {"type": "string"},
        },
        ["daily", "latest", "headline"],
    ),
    _tool(
        "get_recovery_status",
        "读取指定日期的静息心率、睡眠 HRV 与缺失项；不生成虚假恢复评分。",
        _object({"date": _DATE}),
        _RECOVERY["properties"],
        _RECOVERY["required"],
    ),
    _tool(
        "sync_now",
        "立即对活动和日度指标各执行至多一个增量批次；长任务由调用方继续。",
        _object(),
        {
            "trigger": {"type": "string"},
            "streams": {"type": "object", "additionalProperties": {"type": "object"}},
            "complete": {"type": "boolean"},
            "stale": {"type": "boolean"},
            "batch_limit": _stream_map(["integer", "null"]),
            "discovered": _stream_map("integer"),
            "processed": _stream_map("integer"),
            "succeeded": _stream_map("integer"),
            "failed": _stream_map("integer"),
            "queued_remaining": _stream_map("integer"),
            "discovery_complete": _stream_map("boolean"),
            "high_water_ts": _stream_map(["integer", "null"]),
            "last_success_ts": _stream_map(["integer", "null"]),
            "coverage": {"type": "object"},
            "partial": {"type": "boolean"},
            "item_errors": _OBJECT_ARRAY,
            "next_action": {"type": "string"},
        },
        [
            "trigger", "streams", "complete", "stale", "batch_limit", "discovered",
            "processed", "succeeded", "failed", "queued_remaining", "discovery_complete",
            "high_water_ts", "last_success_ts", "coverage", "partial", "item_errors",
        ],
    ),
    _tool(
        "get_current_plan",
        "读取当前日期覆盖的唯一进行中计划及本周日志；v0 不读取平台日程。",
        _object({"date": _DATE}),
        {
            "plan": _nullable(_PLAN),
            "week_logs": {"type": "object", "additionalProperties": _NULL_STRING},
            "platform_schedule": _object({
                "available": {"type": "boolean"},
                "reason": {"type": "string"},
            }, ["available", "reason"]),
            "warnings": {"type": "array", "items": _WARNING},
        },
        ["plan", "week_logs", "platform_schedule", "warnings"],
        annotations=_READ_ONLY,
    ),
    _tool(
        "read_memory",
        "读取一个白名单记忆文件，或以 list 枚举白名单档案；name/list 必须二选一。",
        dict(_object({"name": {"type": "string"}, "list": {"type": "string"}}),
             oneOf=[{"required": ["name"]}, {"required": ["list"]}]),
        {
            "name": {"type": "string"},
            "list": {"type": "string"},
            "exists": {"type": "boolean"},
            "content": _NULL_STRING,
            "sha256": _NULL_STRING,
            "bytes": {"type": "integer"},
            "modified_ts": _NULL_NUMBER,
            "entries": {"type": "array", "items": _MEMORY_ENTRY},
            "warnings": {"type": "array", "items": _WARNING},
        },
        ["exists"],
        [
            {"required": ["name", "content", "sha256", "bytes", "modified_ts"]},
            {"required": ["list", "entries", "warnings"]},
        ],
        annotations=_READ_ONLY,
    ),
    _tool(
        "write_memory",
        "以模板校验、原子替换和可选哈希并发控制写入白名单 Markdown 记忆。",
        _object({"name": {"type": "string"}, "content": {"type": "string"},
                 "expected_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                 "allow_create": {"type": "boolean", "default": False}},
                ["name", "content"]),
        {"name": {"type": "string"}, "sha256": {"type": "string"},
         "bytes": {"type": "integer"}, "written_ts": {"type": "number"},
         "created": {"type": "boolean"}},
        ["name", "sha256", "bytes", "written_ts", "created"],
    ),
    _tool(
        "create_workout",
        "预览或确认创建单次课表；按 platform 路由 COROS 官方 MCP 或 Garmin API。",
        _object({
            "workout": _object({
                "platform": {"type": "string", "enum": ["coros", "garmin"]},
                "date": _DATE,
                "sport": {"type": "string", "enum": ["run", "ride", "trail_run"]},
                "name": {"type": "string", "minLength": 1, "maxLength": 100},
                "description": {"type": "string", "minLength": 1},
                "blocks": {"type": "array", "minItems": 1,
                           "items": {"type": "object"}},
            }, ["date", "sport", "name", "description", "blocks"]),
            "confirm_token": {"type": "string"},
        }, ["workout"]),
        {
            "status": {"type": "string", "enum": ["preview", "created"]},
            "preview": _WORKOUT_PREVIEW,
            "payload_hash": {"type": "string"},
            "confirm_token": {"type": "string"},
            "expires_ts": {"type": "number"},
            "executed": {"type": "boolean"},
            "platform": {"type": "string"},
            "date": {"type": "string"},
            "name": {"type": "string"},
            "created": {"type": ["boolean", "null"]},
            "readback": {"type": ["boolean", "null"]},
            "scheduled": {"type": ["boolean", "null"]},
            "workout_id": {"type": ["string", "integer", "null"]},
            "workout_schedule_id": {"type": ["string", "integer", "null"]},
            "outcome_recorded": {"type": "boolean"},
            "warning": {"type": "string"},
        },
        ["status", "platform", "executed"],
        [
            {
                "properties": {"status": {"const": "preview"}},
                "required": ["preview", "payload_hash", "confirm_token", "expires_ts"],
            },
            {
                "properties": {"status": {"const": "created"}},
                "required": ["date", "name", "created", "scheduled"],
            },
        ],
    ),
]

TOOL_NAMES = tuple(tool.name for tool in TOOLS)
TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}
