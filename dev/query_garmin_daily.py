# 探针：佳明日度健康端点实测（daily_metrics 填充用，2 天窗口）
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sync_garmin_to_coros import load_env, require_env  # noqa: E402

OUT = Path(__file__).resolve().parent / "last_garmin_daily.json"
D1 = date.today().isoformat()
D0 = (date.today() - timedelta(days=1)).isoformat()


def main():
    load_env(Path.home() / ".openathlete" / "credentials")
    load_env(ROOT / ".env")
    cfg = require_env("GARMIN_EMAIL", "GARMIN_PASSWORD")

    import os
    import garth

    if os.environ.get("GARMIN_AUTH_DOMAIN", "COM").strip().upper() == "CN":
        garth.configure(domain="garmin.cn")
    garth.login(cfg["GARMIN_EMAIL"], cfg["GARMIN_PASSWORD"])
    del garth.client.sess.headers["User-Agent"]
    print("佳明登录成功")

    acts = garth.connectapi(
        "/activitylist-service/activities/search/activities",
        params={"start": "0", "limit": "1"})
    name = acts[0]["ownerDisplayName"]
    print("displayName:", name)

    endpoints = {
        "user_summary": ("/usersummary-service/usersummary/daily/" + name,
                         {"calendarDate": D1}),
        "sleep_stats_range": ("/sleep-service/stats/sleep/daily/{}/{}".format(D0, D1), None),
        "hrv_daily_range": ("/hrv-service/hrv/daily/{}/{}".format(D0, D1), None),
        "stress_day": ("/wellness-service/wellness/dailyStress/" + D1, None),
        "body_battery_range": ("/wellness-service/wellness/bodyBattery/reports/daily",
                               {"startDate": D0, "endDate": D1}),
        "rhr_range": ("/userstats-service/wellness/daily/" + name,
                      {"fromDate": D0, "untilDate": D1, "metricId": 60}),
    }
    results = {}
    for key, (path, params) in endpoints.items():
        try:
            data = garth.connectapi(path, params=params)
            results[key] = data
            print("== {} OK（{} 字符）".format(key, len(json.dumps(data))))
        except Exception as err:  # noqa: BLE001
            results[key] = {"error": str(err)}
            print("== {} 失败：{}".format(key, err))

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已存 {}".format(OUT))


if __name__ == "__main__":
    main()
