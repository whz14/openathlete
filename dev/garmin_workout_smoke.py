import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from openathlete.adapters.garmin import GarminClient
from openathlete.credentials import load_credentials, require_credentials

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

RUN_SPORT = {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}
NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}


def executable_step(order, step_type, step_key, condition, condition_key, value):
    return {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": step_type, "stepTypeKey": step_key,
                     "displayOrder": step_type},
        "endCondition": {"conditionTypeId": condition, "conditionTypeKey": condition_key,
                         "displayOrder": condition, "displayable": condition != 7},
        "endConditionValue": value,
        "targetType": NO_TARGET,
    }


def workout_payloads():
    easy = {
        "workoutName": "20分钟慢跑",
        "description": "轻松跑20分钟，不设配速目标",
        "sportType": RUN_SPORT,
        "estimatedDurationInSecs": 1200,
        "author": {},
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": RUN_SPORT,
            "workoutSteps": [executable_step(1, 8, "main", 2, "time", 1200)],
        }],
    }
    interval = executable_step(2, 3, "interval", 3, "distance", 1000)
    repeats = {
        "workoutName": "1km×3",
        "description": "3组1公里，不设配速目标",
        "sportType": RUN_SPORT,
        "estimatedDurationInSecs": 0,
        "author": {},
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": RUN_SPORT,
            "workoutSteps": [{
                "type": "RepeatGroupDTO",
                "stepOrder": 1,
                "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
                "numberOfIterations": 3,
                "workoutSteps": [interval],
                "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations",
                                 "displayOrder": 7, "displayable": False},
                "endConditionValue": 3,
                "smartRepeat": False,
            }],
        }],
    }
    return [easy, repeats]


def find_by_name(client, name):
    for item in client.list_workouts():
        if item.get("workoutName") == name:
            return item
    return None


def workout_id(value):
    if not isinstance(value, dict):
        return None
    return value.get("workoutId") or value.get("id") or (value.get("workout") or {}).get("workoutId")


def ensure_workout(client, payload):
    name = payload["workoutName"]
    existing = find_by_name(client, name)
    if existing:
        wid = workout_id(existing)
        return (client.get_workout(wid) if wid else existing), False
    created = client.create_workout(payload)
    wid = workout_id(created) or workout_id(existing) or workout_id(find_by_name(client, name))
    if not wid:
        raise RuntimeError("创建后未获得 workoutId，停止重试并请在佳明网页核对：{}".format(name))
    return client.get_workout(wid), True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    payloads = workout_payloads()
    if args.dry_run:
        print(json.dumps(payloads, ensure_ascii=False, indent=2))
        return
    load_credentials()
    cred = require_credentials("GARMIN_EMAIL", "GARMIN_PASSWORD")
    client = GarminClient(cred["GARMIN_EMAIL"], cred["GARMIN_PASSWORD"],
                          domain=os.environ.get("GARMIN_AUTH_DOMAIN", "COM"))
    client.ensure_login()
    for payload in payloads:
        workout, created = ensure_workout(client, payload)
        print(json.dumps({
            "workoutName": workout.get("workoutName"),
            "workoutId": workout_id(workout),
            "created": created,
            "scheduled": False,
            "segmentCount": len(workout.get("workoutSegments") or []),
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
