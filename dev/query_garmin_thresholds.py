# 探针：佳明阈值锚点相关端点实测（乳酸阈值/骑行FTP/心率区间/功率区间）
# 端点清单来自 python-garminconnect 源码（garminconnect/api/metrics.py + base.py）
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sync_garmin_to_coros import load_env, require_env  # noqa: E402

OUT = Path(__file__).resolve().parent / "last_garmin_thresholds.json"
HOME_CREDENTIALS = Path.home() / ".openathlete" / "credentials"
D1 = date.today().isoformat()
D0 = (date.today() - timedelta(days=365)).isoformat()


def main():
    load_env(HOME_CREDENTIALS)
    load_env(ROOT / ".env")
    cfg = require_env("GARMIN_EMAIL", "GARMIN_PASSWORD")

    import garth

    import os
    domain = os.environ.get("GARMIN_AUTH_DOMAIN", "COM").strip() or "COM"
    if domain.upper() == "CN":
        garth.configure(domain="garmin.cn")
    garth.login(cfg["GARMIN_EMAIL"], cfg["GARMIN_PASSWORD"])
    del garth.client.sess.headers["User-Agent"]
    print("佳明登录成功（区域 {}）".format(domain))

    endpoints = {
        # 跑步乳酸阈值（最新）：心率 + 速度
        "lactate_threshold_latest": "/biometric-service/biometric/latestLactateThreshold",
        # 骑行 FTP（最新）
        "cycling_ftp_latest": "/biometric-service/biometric/latestFunctionalThresholdPower/CYCLING",
        # 全部运动的心率区间设置（含 lactateThresholdHeartRateUsed、maxHR、各区间下限）
        "heart_rate_zones": "/biometric-service/heartRateZones",
        # 全部运动的功率区间设置
        "power_zones_all": "/biometric-service/powerZones/sports/all",
        # 乳酸阈值历史（近一年，看版本化可行性）
        "lactate_threshold_hr_range": (
            "/biometric-service/stats/lactateThresholdHeartRate/range/{}/{}"
            "?sport=RUNNING&aggregation=daily&aggregationStrategy=LATEST".format(D0, D1)),
        "lactate_threshold_speed_range": (
            "/biometric-service/stats/lactateThresholdSpeed/range/{}/{}"
            "?sport=RUNNING&aggregation=daily&aggregationStrategy=LATEST".format(D0, D1)),
    }
    results = {}
    for name, path in endpoints.items():
        try:
            data = garth.connectapi(path)
            results[name] = data
            print("== {} OK（{} 字符）".format(name, len(json.dumps(data))))
        except Exception as err:  # noqa: BLE001
            results[name] = {"error": str(err)}
            print("== {} 失败：{}".format(name, err))

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已存 {}".format(OUT))


if __name__ == "__main__":
    main()
