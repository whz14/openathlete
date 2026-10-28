# 通过 garth 直读佳明 Connect API：成绩预测 / VO2max / 训练状态
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "upstream" / "scripts"))

from sync_garmin_to_coros import load_env, require_env  # noqa: E402

OUT = Path(__file__).resolve().parent / "last_garmin_metrics.json"
D1 = date.today().isoformat()
D0 = (date.today() - timedelta(days=14)).isoformat()


def main():
    load_env(ROOT / ".env")
    cfg = require_env("GARMIN_EMAIL", "GARMIN_PASSWORD")

    import garth

    domain = os.environ.get("GARMIN_AUTH_DOMAIN", "COM").strip() or "COM"
    if domain.upper() == "CN":
        garth.configure(domain="garmin.cn")
    garth.login(cfg["GARMIN_EMAIL"], cfg["GARMIN_PASSWORD"])
    # 与 upstream 一致：登录后去掉 UA，否则活动/指标接口对国区账号返回空
    del garth.client.sess.headers["User-Agent"]
    print("佳明登录成功（区域 {}）".format(domain))

    results = {}

    acts = garth.connectapi(
        "/activitylist-service/activities/search/activities",
        params={"start": "0", "limit": "1"},
    )
    display_name = acts[0]["ownerDisplayName"]
    print("displayName:", display_name)
    results["displayName"] = display_name

    endpoints = {
        "race_predictions_latest": "/metrics-service/metrics/racepredictions/latest/{}".format(display_name),
        "maxmet_latest": "/metrics-service/metrics/maxmet/latest/{}".format(display_name),
        "maxmet_daily": "/metrics-service/metrics/maxmet/daily/{}/{}".format(D0, D1),
        "training_status": "/metrics-service/metrics/trainingstatus/aggregated/{}".format(display_name),
    }
    for name, path in endpoints.items():
        try:
            data = garth.connectapi(path)
            results[name] = data
            print("== {} OK ({} 字符)".format(name, len(json.dumps(data))))
        except Exception as err:  # noqa: BLE001
            results[name] = {"error": str(err)}
            print("== {} 失败: {}".format(name, err))

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("已写入", OUT)


if __name__ == "__main__":
    main()
