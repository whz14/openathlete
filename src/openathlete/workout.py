"""统一课表到处方平台的映射、确认预览与写入服务。"""
import asyncio
import copy
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import configured_tzinfo, load_config, workout_platform
from .home import openathlete_home

RUN_SPORT = {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}
RIDE_SPORT = {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2}
SPORTS = {"run": RUN_SPORT, "ride": RIDE_SPORT}
NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
TARGET_TYPES = {
    "heart_rate": (4, "heart.rate.zone"),
    "pace": (6, "pace.zone"),
    "power": (2, "power.zone"),
}
ABSOLUTE_BOUNDS = {"heart_rate": (30, 240), "pace": (120, 1499), "power": (10, 2000)}
STEP_ROLES = {
    "warmup": (1, "warmup"), "cooldown": (2, "cooldown"),
    "main": (8, "main"), "recovery": (4, "recovery"),
}
GROUP_ROLES = {"main": (3, "interval"), "recovery": (4, "recovery")}
TARGET_CONDITIONS = {"duration": (2, "time"), "distance": (3, "distance")}


class WorkoutWriteError(RuntimeError):
    def __init__(self, message, stage, workout_id=None):
        super().__init__(message)
        self.stage = stage
        self.workout_id = workout_id


def _positive(value, label, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("{} 必须是正数".format(label))
    if maximum is not None and value > maximum:
        raise ValueError("{} 超出上限 {}".format(label, maximum))
    return value


def _target(block):
    target = block.get("target")
    if not isinstance(target, dict):
        raise ValueError("每个 plain block 必须有 target")
    kind = target.get("kind")
    if kind not in TARGET_CONDITIONS:
        raise ValueError("target.kind 只支持 distance / duration")
    value = _positive(target.get("value"), "target.value")
    condition, key = TARGET_CONDITIONS[kind]
    return {
        "conditionTypeId": condition,
        "conditionTypeKey": key,
        "displayOrder": condition,
        "displayable": True,
    }, value


def _intensity(block, sport):
    if "intensity" not in block:
        raise ValueError("每个 plain block 必须显式提供 intensity 或 null")
    intensity = block["intensity"]
    if intensity is None:
        return {"targetType": NO_TARGET}
    if not isinstance(intensity, dict):
        raise ValueError("intensity 必须是 object 或 null")
    metric = intensity.get("metric")
    if metric not in TARGET_TYPES:
        raise ValueError("intensity.metric 只支持 heart_rate / pace / power")
    if sport == "ride" and metric == "pace":
        raise ValueError("骑行不使用配速目标，请用心率或功率")
    kind = intensity.get("kind")
    if sport == "run" and metric == "power" and kind == "zone":
        raise ValueError("跑步功率只支持 absolute")
    target_id, target_key = TARGET_TYPES[metric]
    target = {"workoutTargetTypeId": target_id,
              "workoutTargetTypeKey": target_key, "displayOrder": 1}
    result = {"targetType": target}
    if kind == "zone":
        zone = _positive(intensity.get("zone"), "intensity.zone", 7)
        if int(zone) != zone:
            raise ValueError("intensity.zone 必须是整数")
        result["zoneNumber"] = int(zone)
        return result
    if kind != "absolute":
        raise ValueError("intensity.kind 只支持 zone / absolute；percent 与 effort_pace 不支持")
    start = _positive(intensity.get("start"), "intensity.start")
    end = _positive(intensity.get("end"), "intensity.end")
    low, high = ABSOLUTE_BOUNDS[metric]
    if start < low or end > high or start > end:
        raise ValueError("intensity 绝对范围不合法：{} ~ {}，允许 {} ~ {} 且 start<=end".format(
            start, end, low, high))
    if metric == "pace":
        result["targetValueOne"] = 1000.0 / end
        result["targetValueTwo"] = 1000.0 / start
    else:
        result["targetValueOne"] = start
        result["targetValueTwo"] = end
    return result


def _step(block, order, sport, in_group=False):
    role = block.get("role")
    roles = GROUP_ROLES if in_group else STEP_ROLES
    if role not in roles:
        raise ValueError("group 内只允许 main/recovery" if in_group else "未知 block role")
    condition, value = _target(block)
    step_type, step_key = roles[role]
    step = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": step_type, "stepTypeKey": step_key,
                     "displayOrder": step_type},
        "endCondition": condition,
        "endConditionValue": value,
    }
    step.update(_intensity(block, sport))
    return step


def _duration_estimate(block):
    kind = block.get("kind")
    if kind == "steady":
        target = block.get("target") or {}
        return target.get("value", 0) if target.get("kind") == "duration" else 0
    if kind == "group":
        cycles = block.get("cycles")
        if isinstance(cycles, bool) or not isinstance(cycles, int) or not 1 <= cycles <= 20:
            raise ValueError("group.cycles 必须是 1..20 的整数")
        return cycles * sum(_duration_estimate(step) for step in block.get("steps") or [])
    raise ValueError("block.kind 只支持 steady / group")


def garmin_workout_payload(workout):
    """把统一 workout 映射为 Garmin 原生 JSON；无副作用。"""
    if not isinstance(workout, dict):
        raise ValueError("workout 必须是 JSON object")
    sport = workout.get("sport")
    if sport not in SPORTS:
        raise ValueError("佳明写回本切片只支持 run / ride")
    name, description = _course_text(workout)
    blocks = workout.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("blocks 必须是非空数组")
    steps = []
    order = 1
    for block in blocks:
        kind = block.get("kind")
        if kind == "steady":
            steps.append(_step(block, order, sport))
            order += 1
        elif kind == "group":
            if "target" in block or "intensity" in block:
                raise ValueError("group 容器不得带 target/intensity")
            cycles = block.get("cycles")
            if isinstance(cycles, bool) or not isinstance(cycles, int) or not 1 <= cycles <= 20:
                raise ValueError("group.cycles 必须是 1..20 的整数")
            members = block.get("steps")
            if not isinstance(members, list) or not members:
                raise ValueError("group.steps 必须是非空数组")
            group_order = order
            order += 1
            group_steps = []
            for member in members:
                if member.get("kind") != "steady":
                    raise ValueError("group 内禁止嵌套 group")
                group_steps.append(_step(member, order, sport, in_group=True))
                order += 1
            steps.append({
                "type": "RepeatGroupDTO",
                "stepOrder": group_order,
                "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
                "numberOfIterations": cycles,
                "workoutSteps": group_steps,
                "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations",
                                 "displayOrder": 7, "displayable": False},
                "endConditionValue": cycles,
                "smartRepeat": False,
            })
        else:
            raise ValueError("block.kind 只支持 steady / group")
    sport_type = SPORTS[sport]
    return {
        "workoutName": name,
        "description": description,
        "sportType": sport_type,
        "estimatedDurationInSecs": sum(_duration_estimate(block) for block in blocks),
        "author": {},
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": sport_type,
            "workoutSteps": steps,
        }],
    }


def _workout_id(value):
    if not isinstance(value, dict):
        return None
    return value.get("workoutId") or value.get("id") or (value.get("workout") or {}).get("workoutId")


def _schedule_id(value):
    if not isinstance(value, dict):
        return None
    return value.get("workoutScheduleId") or value.get("scheduleId") or value.get("id")


def create_garmin_workout(client, workout, date=None):
    """创建、读回并按需排期；任何后续失败都不自动重试创建。"""
    if date is not None:
        datetime.strptime(date, "%Y-%m-%d")
    payload = garmin_workout_payload(workout)
    try:
        created = client.create_workout(payload)
    except Exception as exc:
        raise WorkoutWriteError("佳明课表创建失败：{}".format(exc), "create") from exc
    workout_id = _workout_id(created)
    if not workout_id:
        raise WorkoutWriteError("创建响应缺少 workoutId，请在佳明网页核对", "create")
    try:
        readback = client.get_workout(workout_id)
    except Exception as exc:
        raise WorkoutWriteError("课表已创建但读回失败：{}".format(exc),
                                "readback", workout_id) from exc
    result = {"workout": readback, "workout_id": workout_id,
              "workout_schedule_id": None, "scheduled": False}
    if date is not None:
        try:
            scheduled = client.schedule_workout(workout_id, date)
        except Exception as exc:
            raise WorkoutWriteError("课表已创建但排期失败：{}".format(exc),
                                    "schedule", workout_id) from exc
        result.update({"workout_schedule_id": _schedule_id(scheduled), "scheduled": True,
                       "schedule": scheduled})
    return result


WORKOUT_MAPPER_VERSION = "unified-workout-v1"
CONFIRM_TOKEN_TTL_S = 600
COROS_SPORTS = {"run": 1, "ride": 2, "trail_run": 5}
COROS_SECTION_TYPES = {"warmup": 1, "main": 2, "recovery": 3, "cooldown": 4}
COROS_TARGET_TYPES = {"distance": 1, "duration": 2, "climb": 3}
COROS_INTENSITY_TYPES = {"heart_rate": 1, "pace": 2, "effort_pace": 3, "power": 4}
COROS_METRICS = {
    "run": {"heart_rate", "pace", "power"},
    "ride": {"heart_rate", "power"},
    "trail_run": {"heart_rate", "pace", "effort_pace", "power"},
}
WORKOUT_CONFIRMATION_DDL = """
create table if not exists oa_write_confirmation (
  token_hash TEXT PRIMARY KEY,
  payload_hash TEXT NOT NULL,
  platform TEXT NOT NULL,
  created_ts INTEGER NOT NULL,
  expires_ts INTEGER NOT NULL,
  consumed_ts INTEGER,
  outcome TEXT,
  result_json TEXT
)
"""
_TOKEN_NEXT_ACTION = "请重新预览课表，核对日期、内容与平台后再次明确确认。"
_UNKNOWN_NEXT_ACTION = (
    "不要后台重试或沿用旧 token；先查看目标平台日程/课表库。课表可能已经创建；"
    "知情并接受重复风险后可重新预览、再次确认。"
)


class WorkoutError(RuntimeError):
    def __init__(self, code, message, retryable=False, next_action="", details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.next_action = next_action
        self.details = details or {}


def _integer(value, label, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("{} 必须是整数".format(label))
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ValueError("{} 超出合法范围".format(label))
    return value


def _course_text(workout):
    name = workout.get("name")
    description = workout.get("description")
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError("name 必须是非空且不超过 100 字符")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description 必须非空")
    return name.strip(), description.strip()


def _coros_target(block, sport):
    target = block.get("target")
    if not isinstance(target, dict):
        raise ValueError("每个 plain block 必须有 target")
    kind = target.get("kind")
    if kind not in COROS_TARGET_TYPES:
        raise ValueError("target.kind 只支持 distance / duration / climb")
    if kind == "duration" and sport == "trail_run":
        raise ValueError("trail_run 不支持 duration target")
    if kind == "climb" and sport != "trail_run":
        raise ValueError("climb target 仅支持 trail_run")
    role = block.get("role")
    if sport == "trail_run" and role in {"warmup", "cooldown"} and kind != "distance":
        raise ValueError("trail_run 的 warmup/cooldown 只允许 distance target")
    value = _integer(target.get("value"), "target.value", minimum=1)
    return {"targetType": COROS_TARGET_TYPES[kind], "targetValue": value}


def _coros_intensity(block, sport):
    if "intensity" not in block:
        raise ValueError("每个 plain block 必须显式提供 intensity 或 null")
    intensity = block["intensity"]
    if intensity is None:
        return {}
    if not isinstance(intensity, dict):
        raise ValueError("intensity 必须是 object 或 null")
    metric = intensity.get("metric")
    if metric not in COROS_INTENSITY_TYPES or metric not in COROS_METRICS[sport]:
        raise ValueError("该运动不支持 intensity.metric：{}".format(metric))
    kind = intensity.get("kind")
    if metric == "effort_pace" and kind not in {"zone", "percent"}:
        raise ValueError("effort_pace 只支持 zone / percent")
    if metric == "power" and sport in {"run", "trail_run"} and kind != "absolute":
        raise ValueError("跑步与越野功率只支持 absolute")
    if kind == "percent" and metric == "power":
        raise ValueError("功率不支持 percent")
    if kind not in {"zone", "percent", "absolute"}:
        raise ValueError("intensity.kind 只支持 zone / percent / absolute")
    result = {"intensityType": COROS_INTENSITY_TYPES[metric]}
    if kind == "zone":
        maximum = 7 if sport == "ride" and metric == "power" else 6
        result["sectionIntensity"] = _integer(
            intensity.get("zone"), "intensity.zone", minimum=1, maximum=maximum)
    elif kind == "percent":
        maximum = 120 if metric == "heart_rate" else 140
        start = _integer(intensity.get("start"), "intensity.start", minimum=20, maximum=maximum)
        end = _integer(intensity.get("end"), "intensity.end", minimum=20, maximum=maximum)
        if start >= end:
            raise ValueError("intensity percent 范围必须 start < end")
        result.update({"intensityPercentStart": start, "intensityPercentEnd": end})
    else:
        low, high = ABSOLUTE_BOUNDS[metric]
        start = _integer(intensity.get("start"), "intensity.start", minimum=low, maximum=high)
        end = _integer(intensity.get("end"), "intensity.end", minimum=low, maximum=high)
        result.update({"intensityValueStart": start, "intensityValueEnd": end})
    return result


def _coros_section(block, sport, in_group=False):
    if not isinstance(block, dict) or block.get("kind") != "steady":
        raise ValueError("group 内禁止嵌套 group" if in_group else "block.kind 只支持 steady / group")
    role = block.get("role")
    if role not in COROS_SECTION_TYPES or in_group and role not in {"main", "recovery"}:
        raise ValueError("group 内只允许 main/recovery" if in_group else "未知 block role")
    section = {"sectionType": COROS_SECTION_TYPES[role]}
    section.update(_coros_target(block, sport))
    section.update(_coros_intensity(block, sport))
    return section


def coros_workout_payload(workout):
    """把统一 workout 映射为官方 createScheduledWorkout 的 course；无副作用。"""
    if not isinstance(workout, dict):
        raise ValueError("workout 必须是 JSON object")
    sport = workout.get("sport")
    if sport not in COROS_SPORTS:
        raise ValueError("COROS 只支持 run / ride / trail_run")
    name, description = _course_text(workout)
    blocks = workout.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("blocks 必须是非空数组")
    sections = []
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError("block 必须是 object")
        if block.get("kind") == "steady":
            sections.append(_coros_section(block, sport))
            continue
        if block.get("kind") != "group":
            raise ValueError("block.kind 只支持 steady / group")
        if "target" in block or "intensity" in block:
            raise ValueError("group 容器不得带 target/intensity")
        cycles = _integer(block.get("cycles"), "group.cycles", minimum=1, maximum=20)
        members = block.get("steps")
        if not isinstance(members, list) or not members:
            raise ValueError("group.steps 必须是非空数组")
        sections.append({
            "intervalGroup": True,
            "repeats": cycles,
            "sets": [_coros_section(member, sport, in_group=True) for member in members],
        })
    return {
        "sportType": COROS_SPORTS[sport],
        "courseName": name,
        "courseDescription": description,
        "sections": sections,
    }


def _home_path(home):
    return Path(home) if home is not None else openathlete_home()


def _resolve_platform(config, workout):
    if not isinstance(workout, dict):
        raise WorkoutError("INVALID_ARGUMENT", "workout 必须是 JSON object")
    if "platform" in workout:
        value = workout["platform"]
        if not isinstance(value, str) or value.strip().lower() not in {"coros", "garmin"}:
            raise WorkoutError("INVALID_ARGUMENT", "workout.platform 只支持 coros / garmin")
        return value.strip().lower()
    try:
        value = workout_platform(config)
    except ValueError as exc:
        raise WorkoutError(
            "SETUP_REQUIRED", str(exc),
            next_action="请把 config.toml 的 workout_platform 设置为 coros 或 garmin。") from exc
    if value is None:
        raise WorkoutError(
            "SETUP_REQUIRED", "尚未配置课表写回平台",
            next_action="请设置 config.toml 的 workout_platform，或显式提供 workout.platform。")
    return value


def resolve_workout_platform(home, workout):
    """解析显式平台或 config.workout_platform；绝不回退 primary_platform。"""
    return _resolve_platform(load_config(_home_path(home)), workout)


def _workout_date(workout):
    value = workout.get("date")
    if not isinstance(value, str):
        raise WorkoutError("INVALID_ARGUMENT", "workout.date 必须是 YYYY-MM-DD")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise WorkoutError("INVALID_ARGUMENT", "workout.date 必须是真实的 YYYY-MM-DD 日期") from exc
    if parsed.isoformat() != value:
        raise WorkoutError("INVALID_ARGUMENT", "workout.date 必须严格使用 YYYY-MM-DD")
    return parsed


def _now_seconds(now_ts):
    if now_ts is None:
        return int(time.time())
    if isinstance(now_ts, bool) or not isinstance(now_ts, (int, float)):
        raise WorkoutError("INVALID_ARGUMENT", "now_ts 必须是 Unix 秒")
    return int(now_ts)


_WORKOUT_ROOT_FIELDS = frozenset({"platform", "date", "sport", "name", "description", "blocks"})
_WORKOUT_STEADY_FIELDS = frozenset({"kind", "role", "target", "intensity"})
_WORKOUT_GROUP_FIELDS = frozenset({"kind", "cycles", "steps"})
_WORKOUT_TARGET_FIELDS = frozenset({"kind", "value"})
_WORKOUT_INTENSITY_FIELDS = frozenset({"metric", "kind", "zone", "start", "end"})


def _check_workout_fields(value, allowed, path):
    if not isinstance(value, dict):
        raise ValueError("{} 必须是 object".format(path))
    unknown = sorted(set(value) - allowed, key=str)
    if unknown:
        raise ValueError("{} 含未知字段：{}".format(
            path, ", ".join(str(field) for field in unknown)))


def _validate_workout_fields(workout):
    _check_workout_fields(workout, _WORKOUT_ROOT_FIELDS, "workout")

    def validate_block(block, path):
        if not isinstance(block, dict):
            raise ValueError("{} 必须是 object".format(path))
        kind = block.get("kind")
        if kind == "steady":
            _check_workout_fields(block, _WORKOUT_STEADY_FIELDS, path)
            if "target" in block:
                _check_workout_fields(block["target"], _WORKOUT_TARGET_FIELDS,
                                      "{}.target".format(path))
            if block.get("intensity") is not None:
                _check_workout_fields(block["intensity"], _WORKOUT_INTENSITY_FIELDS,
                                      "{}.intensity".format(path))
        elif kind == "group":
            _check_workout_fields(block, _WORKOUT_GROUP_FIELDS, path)
            steps = block.get("steps")
            if isinstance(steps, list):
                for index, member in enumerate(steps):
                    validate_block(member, "{}.steps[{}]".format(path, index))

    blocks = workout.get("blocks")
    if isinstance(blocks, list):
        for index, block in enumerate(blocks):
            validate_block(block, "workout.blocks[{}]".format(index))


def _validate_unified_numbers(blocks):
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("blocks 必须是非空数组")

    def plain(block):
        if not isinstance(block, dict):
            raise ValueError("block 必须是 object")
        target = block.get("target")
        if isinstance(target, dict) and "value" in target:
            _integer(target["value"], "target.value", minimum=1)
        intensity = block.get("intensity")
        if isinstance(intensity, dict):
            for key in ("zone", "start", "end"):
                if key in intensity:
                    _integer(intensity[key], "intensity.{}".format(key))

    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError("block 必须是 object")
        if block.get("kind") == "steady":
            plain(block)
        elif block.get("kind") == "group":
            members = block.get("steps")
            if isinstance(members, list):
                for member in members:
                    if isinstance(member, dict) and member.get("kind") == "steady":
                        plain(member)


def _prepare_workout(home, workout, now_ts):
    home = _home_path(home)
    config = load_config(home)
    platform = _resolve_platform(config, workout)
    date = _workout_date(workout)
    now = _now_seconds(now_ts)
    if platform == "coros":
        today = datetime.fromtimestamp(now, configured_tzinfo(config)).date()
        if not today <= date <= today + timedelta(days=90):
            raise WorkoutError(
                "INVALID_ARGUMENT", "COROS 排期日期必须在今天至今天+90天（含）之间",
                details={"date": date.isoformat(), "earliest": today.isoformat(),
                         "latest": (today + timedelta(days=90)).isoformat()})
    try:
        _validate_workout_fields(workout)
        name, description = _course_text(workout)
        _validate_unified_numbers(workout.get("blocks"))
        normalized = {
            "platform": platform,
            "date": date.isoformat(),
            "sport": workout.get("sport"),
            "name": name,
            "description": description,
            "blocks": copy.deepcopy(workout["blocks"]),
        }
        if platform == "coros":
            payload = {"date": date.strftime("%Y%m%d"),
                       "course": coros_workout_payload(normalized)}
        else:
            payload = garmin_workout_payload(normalized)
    except (KeyError, ValueError) as exc:
        raise WorkoutError(
            "WORKOUT_UNSUPPORTED", str(exc),
            next_action="请按目标平台能力矩阵修正运动、target、intensity 或重复组。") from exc
    hash_input = {
        "mapper_version": WORKOUT_MAPPER_VERSION,
        "platform": platform,
        "workout": normalized,
        "payload": payload,
    }
    encoded = json.dumps(
        hash_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")
    return normalized, payload, hashlib.sha256(encoded).hexdigest(), now


def _connect_workout_db(home):
    conn = sqlite3.connect(str(_home_path(home) / "sync.db"), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma busy_timeout=5000")
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma foreign_keys=on")
    return conn


def _db_error(exc):
    message = str(exc).lower()
    if "busy" in message or "locked" in message:
        return WorkoutError("DB_BUSY", "课表确认数据库正忙", retryable=True,
                            next_action="请等待其他本地写入完成后重试。")
    return WorkoutError("INTERNAL_ERROR", "课表确认数据库操作失败")


def init_workout_db(home):
    """只初始化 sync.db 中的课表确认表；不访问网络。"""
    home = _home_path(home)
    home.mkdir(parents=True, exist_ok=True)
    try:
        conn = _connect_workout_db(home)
        try:
            conn.execute(WORKOUT_CONFIRMATION_DDL)
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise _db_error(exc) from exc
    return home / "sync.db"


def preview_workout(home, workout, now_ts=None):
    """本地生成 10 分钟一次性确认 token；数据库只保存 token SHA-256。"""
    normalized, _payload, payload_hash, now = _prepare_workout(home, workout, now_ts)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires = now + CONFIRM_TOKEN_TTL_S
    init_workout_db(home)
    try:
        conn = _connect_workout_db(home)
        try:
            conn.execute("begin immediate")
            conn.execute(
                "insert into oa_write_confirmation "
                "(token_hash,payload_hash,platform,created_ts,expires_ts) values (?,?,?,?,?)",
                (token_hash, payload_hash, normalized["platform"], now, expires))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise _db_error(exc) from exc
    return {
        "status": "preview",
        "platform": normalized["platform"],
        "preview": normalized,
        "payload_hash": payload_hash,
        "confirm_token": token,
        "expires_ts": expires,
        "executed": False,
    }


def _consume_confirmation(home, confirm_token, payload_hash, platform, now):
    if not isinstance(confirm_token, str) or not confirm_token:
        raise WorkoutError("CONFIRM_TOKEN_INVALID", "确认 token 无效",
                           next_action=_TOKEN_NEXT_ACTION)
    token_hash = hashlib.sha256(confirm_token.encode("utf-8")).hexdigest()
    init_workout_db(home)
    try:
        conn = _connect_workout_db(home)
        try:
            conn.execute("begin immediate")
            row = conn.execute(
                "select * from oa_write_confirmation where token_hash=?",
                (token_hash,)).fetchone()
            if row is None:
                raise WorkoutError("CONFIRM_TOKEN_INVALID", "确认 token 无效",
                                   next_action=_TOKEN_NEXT_ACTION)
            if row["consumed_ts"] is not None:
                raise WorkoutError("CONFIRM_TOKEN_REUSED", "确认 token 已使用",
                                   next_action=_TOKEN_NEXT_ACTION)
            if now >= row["expires_ts"]:
                raise WorkoutError("CONFIRM_TOKEN_EXPIRED", "确认 token 已过期",
                                   next_action=_TOKEN_NEXT_ACTION)
            if row["payload_hash"] != payload_hash or row["platform"] != platform:
                raise WorkoutError(
                    "CONFIRM_PAYLOAD_MISMATCH", "课表 payload 或有效平台与预览不符",
                    next_action="课表或默认平台已变化；请重新预览并再次确认。")
            changed = conn.execute(
                "update oa_write_confirmation set consumed_ts=? "
                "where token_hash=? and consumed_ts is null and expires_ts>? "
                "and payload_hash=? and platform=?",
                (now, token_hash, now, payload_hash, platform)).rowcount
            if changed != 1:
                raise WorkoutError("CONFIRM_TOKEN_REUSED", "确认 token 已被其他执行使用",
                                   next_action=_TOKEN_NEXT_ACTION)
            conn.commit()
            return token_hash
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise _db_error(exc) from exc


def _record_outcome(home, token_hash, outcome, summary):
    try:
        conn = _connect_workout_db(home)
        try:
            conn.execute("begin immediate")
            conn.execute(
                "update oa_write_confirmation set outcome=?, result_json=? where token_hash=?",
                (outcome, json.dumps(summary, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")), token_hash))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise _db_error(exc) from exc


_OUTCOME_AUDIT_WARNING = (
    "平台结果已确定，但本地审计写入失败；请按返回的平台结果处理，不要重复创建。"
)


async def _try_record_outcome(home, token_hash, outcome, summary):
    try:
        await asyncio.to_thread(_record_outcome, home, token_hash, outcome, summary)
    except Exception:
        logging.getLogger(__name__).exception(
            "课表平台结果已确定，但本地审计写入失败；保留平台原始结果")
        return False
    return True


def _outcome_unknown_exception(exc):
    names = {
        "Timeout", "TimeoutError", "TimeoutException", "ConnectTimeout", "ReadTimeout",
        "WriteTimeout", "PoolTimeout", "ConnectionError", "ConnectError", "ReadError",
        "WriteError", "NetworkError", "RemoteProtocolError", "RemoteDisconnected",
        "IncompleteRead", "ServerDisconnectedError",
    }
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (TimeoutError, ConnectionError)):
            return True
        if any(cls.__name__ in names for cls in type(current).__mro__):
            return True
        current = current.__cause__
    return False


def _default_garmin_client(home):
    from .credentials import load_credentials
    load_credentials()
    missing = [key for key in ("GARMIN_EMAIL", "GARMIN_PASSWORD")
               if not os.environ.get(key, "").strip()]
    if missing:
        raise WorkoutError(
            "CREDENTIALS_MISSING", "缺少 Garmin API 凭据",
            next_action="请自行在 ~/.openathlete/credentials 配置 Garmin 凭据后重试。")
    from .adapters.garmin import GarminClient
    return GarminClient(
        os.environ["GARMIN_EMAIL"], os.environ["GARMIN_PASSWORD"], home=home,
        domain=os.environ.get("GARMIN_AUTH_DOMAIN", "COM"))


def _garmin_action(workout_id, date, unknown=False):
    command = "openathlete-mcp garmin workout schedule --id {} --date {}".format(
        workout_id, date)
    if unknown:
        return ("不要重建课表；先检查 Garmin 日程确认是否已排期。若尚未排期，"
                "核对后可使用 `{}`，或在 Garmin Connect 手动排期。").format(command)
    return ("课表已在 Garmin 库中、尚未排期；不要重建。核对后可使用 `{}`，"
            "或在 Garmin Connect 手动排期。").format(command)


def _garmin_execution_error(exc, date):
    stage = exc.stage
    workout_id = exc.workout_id
    details = {"stage": stage, "workout_id": workout_id}
    if stage == "readback":
        details.update({"created": True, "readback": False, "scheduled": False})
        return WorkoutError(
            "WRITE_FAILED", "Garmin 课表已创建，但读回失败", next_action=_garmin_action(
                workout_id, date), details=details)
    if stage == "schedule":
        unknown = _outcome_unknown_exception(exc)
        details.update({"created": True, "readback": True,
                        "scheduled": None if unknown else False})
        if unknown:
            return WorkoutError(
                "WORKOUT_OUTCOME_UNKNOWN", "Garmin 排期请求结果不明；课表已创建",
                next_action=_garmin_action(workout_id, date, unknown=True), details=details)
        return WorkoutError(
            "WORKOUT_SCHEDULE_FAILED", "Garmin 课表已创建，但排期被明确拒绝",
            next_action=_garmin_action(workout_id, date), details=details)
    unknown = _outcome_unknown_exception(exc) or workout_id is None and exc.__cause__ is None
    details.update({"created": None if unknown else False,
                    "readback": None if unknown else False,
                    "scheduled": None if unknown else False})
    if unknown:
        return WorkoutError(
            "WORKOUT_OUTCOME_UNKNOWN", "Garmin 创建请求结果不明，课表可能已经创建",
            next_action=_UNKNOWN_NEXT_ACTION, details=details)
    return WorkoutError(
        "WRITE_FAILED", "Garmin 明确拒绝了课表创建",
        next_action="请核对 Garmin Connect 与课表内容；不要自动重试。", details=details)


def _coros_execution_error(exc):
    code = getattr(exc, "code", None)
    if code == "AUTH_REQUIRED":
        return WorkoutError(
            code, "COROS 官方 OAuth 需要重新授权",
            next_action="请运行 uv run ./coros_mcp_proxy.py --login 完成官方 OAuth。",
            details={"created": False, "scheduled": False, "stage": "auth"})
    if code and code not in {"WORKOUT_OUTCOME_UNKNOWN", "WRITE_FAILED"}:
        return WorkoutError(
            code, "COROS 官方连接失败", retryable=code == "UPSTREAM_UNAVAILABLE",
            next_action="请检查网络与官方 OAuth 状态后重试。",
            details={"created": False, "scheduled": False, "stage": "connect"})
    unknown = getattr(exc, "outcome_unknown", False)
    explicit = getattr(exc, "explicit_rejection", False)
    if unknown or not explicit:
        return WorkoutError(
            "WORKOUT_OUTCOME_UNKNOWN", "COROS 创建请求结果不明，课表可能已经创建",
            next_action=_UNKNOWN_NEXT_ACTION,
            details={"created": None, "scheduled": None, "stage": "create"})
    return WorkoutError(
        "WRITE_FAILED", "COROS 官方明确拒绝了课表创建",
        next_action="请核对课表内容与 COROS 日程；不要自动重试。",
        details={"created": False, "scheduled": False, "stage": "create"})


async def execute_workout(home, workout, confirm_token, now_ts=None,
                          coros_create=None, garmin_client=None):
    """原子消费确认 token 后执行一次平台写入；网络期间不持 SQLite 事务。"""
    home = _home_path(home)
    normalized, payload, payload_hash, now = await asyncio.to_thread(
        _prepare_workout, home, workout, now_ts)
    platform = normalized["platform"]
    token_hash = await asyncio.to_thread(
        _consume_confirmation, home, confirm_token, payload_hash, platform, now)
    try:
        if platform == "coros":
            if coros_create is None:
                from .adapters.coros_official import create_scheduled_workout
                coros_create = create_scheduled_workout
            try:
                await coros_create(home, payload)
            except Exception as exc:
                raise _coros_execution_error(exc) from exc
            result = {
                "status": "created", "date": normalized["date"], "name": normalized["name"],
                "platform": platform, "executed": True, "created": True, "scheduled": True,
            }
        else:
            client = garmin_client
            if client is None:
                client = await asyncio.to_thread(_default_garmin_client, home)
                try:
                    await asyncio.to_thread(client.ensure_login)
                except Exception as exc:
                    raise WorkoutError(
                        "AUTH_REQUIRED", "Garmin API 登录失败",
                        next_action="请核对 Garmin 凭据、区服与 MFA 设置后重试。",
                        details={"created": False, "readback": False,
                                 "scheduled": False, "stage": "auth"}) from exc
            try:
                written = await asyncio.to_thread(
                    create_garmin_workout, client, normalized, normalized["date"])
            except WorkoutWriteError as exc:
                raise _garmin_execution_error(exc, normalized["date"]) from exc
            result = {
                "status": "created", "date": normalized["date"], "name": normalized["name"],
                "platform": platform, "executed": True, "created": True, "readback": True,
                "scheduled": written["scheduled"], "workout_id": written["workout_id"],
                "workout_schedule_id": written["workout_schedule_id"],
            }
    except WorkoutError as exc:
        outcome = "outcome_unknown" if exc.code == "WORKOUT_OUTCOME_UNKNOWN" else "failed"
        recorded = await _try_record_outcome(
            home, token_hash, outcome, {"code": exc.code, "details": exc.details})
        if not recorded:
            exc.details["outcome_recorded"] = False
        raise
    recorded = await _try_record_outcome(home, token_hash, "succeeded", {
        "created": result["created"], "scheduled": result["scheduled"],
        "platform": platform, "date": normalized["date"],
    })
    if not recorded:
        result["outcome_recorded"] = False
        result["warning"] = _OUTCOME_AUDIT_WARNING
    return result
