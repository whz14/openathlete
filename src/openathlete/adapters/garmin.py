"""佳明 Connect 非官方 API adapter（garth）：活动列表 / FIT 下载 / 日度健康 / 阈值锚点。

端点经 python-garminconnect 源码（MIT）核实；维护探针见
`dev/query_garmin_metrics.py`、`dev/query_garmin_thresholds.py` 和
`dev/query_garmin_daily.py`。原始响应仅保存在本机，不随仓库发布。

注意：佳明需关闭两步验证（garth 不支持 MFA，AGENTS.md 已知坑 #2）；
登录后必须删除 User-Agent 头，否则国区账号多数接口返回空（upstream 同款处理）。
"""
from datetime import datetime, timedelta
from pathlib import Path

from ..home import fit_dir as _fit_dir, openathlete_home
from .coros import unpack_fit_parts

# 佳明 typeKey → 归档文件名运动词汇（文件名只是人读提示，入库归一以 FIT 内部为准）
_TYPEKEY_FOLDER = {
    "running": "run", "track_running": "run", "trail_running": "run",
    "treadmill_running": "run", "cycling": "ride", "indoor_cycling": "ride",
    "mountain_biking": "ride", "gravel_cycling": "ride", "e_biking": "ride",
    "lap_swimming": "swim", "open_water_swimming": "swim",
    "walking": "walk", "hiking": "hike",
}


class GarminApiError(Exception):
    """佳明 API 调用失败（登录/端点/结构异常）。"""


class GarminClient:
    """佳明 Connect 客户端。token 持久化在 <home>/garth-tokens/（garth.save/resume）。"""

    def __init__(self, email, password, home=None, domain=None):
        self.email = email
        self.password = password
        self.home = Path(home) if home else openathlete_home()
        self.domain = (domain or "COM").strip().upper()
        self.token_dir = self.home / "garth-tokens"
        self._display_name = None

    # -- 认证 --

    def _garth(self):
        import garth  # 延迟加载，避免只读启动路径初始化 Garmin SDK
        if self.domain == "CN":
            garth.configure(domain="garmin.cn")
        return garth

    def login(self):
        garth = self._garth()
        garth.login(self.email, self.password)
        # 国区账号不去 UA 多数接口返回空（upstream/dev 探针同款处理）
        if "User-Agent" in garth.client.sess.headers:
            del garth.client.sess.headers["User-Agent"]
        self.token_dir.mkdir(parents=True, exist_ok=True)
        garth.save(str(self.token_dir))

    def ensure_login(self):
        try:
            garth = self._garth()
            garth.resume(str(self.token_dir))
            if "User-Agent" in garth.client.sess.headers:
                del garth.client.sess.headers["User-Agent"]
            return
        except Exception:  # noqa: BLE001 token 缺失/过期一律重登
            self.login()

    def _api(self, path, params=None):
        import garth
        try:
            return garth.connectapi(path, params=params)
        except Exception as e:  # noqa: BLE001
            raise GarminApiError("佳明接口失败 {}：{}".format(path, e)) from e

    def _download(self, path):
        import garth
        try:
            return garth.download(path)
        except Exception as e:  # noqa: BLE001
            raise GarminApiError("佳明下载失败 {}：{}".format(path, e)) from e

    def _write_api(self, method, path, payload):
        import garth
        if method not in {"post", "put"}:
            raise ValueError("不支持的写方法：{}".format(method))
        try:
            response = getattr(garth.client, method)("connectapi", path, json=payload, api=True)
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        except Exception as e:  # noqa: BLE001
            raise GarminApiError("佳明写接口失败 {} {}：{}".format(
                method.upper(), path, type(e).__name__)) from e

    def display_name(self):
        if not self._display_name:
            acts = self._api("/activitylist-service/activities/search/activities",
                             {"start": "0", "limit": "1"})
            if not acts:
                raise GarminApiError("账号内没有活动，无法获取 displayName")
            self._display_name = acts[0]["ownerDisplayName"]
        return self._display_name

    # -- 活动 --

    def iter_activities(self, earliest_local_date=None, page_size=100):
        """按时间倒序遍历活动项。earliest_local_date（YYYY-MM-DD）提前停止
        （佳明列表项 startTimeLocal 是本地时间字符串）。"""
        start = 0
        while True:
            page = self._api("/activitylist-service/activities/search/activities",
                             {"start": str(start), "limit": str(page_size)})
            if not page:
                return
            for a in page:
                yield a
            if earliest_local_date:
                oldest = min(str(a.get("startTimeLocal", "9999"))[:10] for a in page)
                if oldest < earliest_local_date:
                    return
            if len(page) < page_size:
                return
            start += page_size

    def download_fit(self, activity_id) -> bytes:
        """下载活动原始文件（通常是 zip，内含 .fit）。"""
        return self._download("/download-service/files/activity/{}".format(activity_id))

    def archive_fit(self, activity: dict, payload: bytes, home=None) -> list:
        """按 DESIGN.md §3 命名落盘（幂等）。activity 需含
        activityId / activityType.typeKey / startTimeLocal（本地 'YYYY-MM-DD HH:MM:SS'）。"""
        root = _fit_dir(home or self.home)
        aid = str(activity["activityId"])
        type_key = ((activity.get("activityType") or {}).get("typeKey") or "")
        sport = _TYPEKEY_FOLDER.get(type_key, "other")
        local = str(activity.get("startTimeLocal", "")).replace(":", "").replace(" ", "-")
        local = local.replace("-", "", 2)  # YYYYMMDD-HHMMSS
        month_dir = root / local[:4] / local[4:6]
        month_dir.mkdir(parents=True, exist_ok=True)
        parts = unpack_fit_parts(payload)
        written = []
        for idx, fit_bytes in enumerate(parts, 1):
            pid = aid if len(parts) == 1 else "{}#{}".format(aid, idx)
            path = month_dir / "{}-{}-garmin-{}.fit".format(local, sport, pid)
            if not path.exists():
                path.write_bytes(fit_bytes)
            written.append(path)
        return written

    # -- 课表 --

    def list_workouts(self, start=0, limit=100):
        return self._api("/workout-service/workouts", {
            "start": start, "limit": limit, "myWorkoutsOnly": "true"}) or []

    def get_workout(self, workout_id):
        return self._api("/workout-service/workout/{}".format(workout_id))

    def create_workout(self, workout):
        if not isinstance(workout, dict):
            raise ValueError("workout 必须是 JSON object")
        return self._write_api("post", "/workout-service/workout", workout)

    def schedule_workout(self, workout_id, date_str):
        datetime.strptime(date_str, "%Y-%m-%d")
        return self._write_api(
            "post", "/workout-service/schedule/{}".format(workout_id), {"date": date_str})

    def get_scheduled_workouts(self, year, month):
        if not 1 <= month <= 12:
            raise ValueError("month 必须在 1..12")
        return self._api("/calendar-service/year/{}/month/{}".format(year, month - 1))

    # -- 日度健康 --

    def fetch_daily_records(self, start_date, end_date):
        """日度健康（YYYY-MM-DD 闭区间）。睡眠统计区间端点（28 天/块）给睡眠分期/
        夜间HRV/血氧/静息心率；user summary 按天补压力/Body Battery/步数/热量。

        返回 daily_metrics 形状：[{"date", "source": "garmin", resting_hr, hrv_ms,
        sleep_total_s, ..., steps, calories}]。睡眠归属醒来日（与高驰同惯例）。
        """
        sleep = {}
        day = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
        while day <= end:
            chunk_end = min(day + timedelta(days=27), end)
            data = self._api("/sleep-service/stats/sleep/daily/{}/{}".format(day, chunk_end))
            for row in (data or {}).get("individualStats") or []:
                if row.get("calendarDate") and row.get("values"):
                    sleep[row["calendarDate"]] = row["values"]
            day = chunk_end + timedelta(days=1)

        records = {}
        day = datetime.strptime(start_date, "%Y-%m-%d").date()
        while day <= end:
            date = day.isoformat()
            rec = {"date": date, "source": "garmin"}
            v = sleep.get(date) or {}
            rec.update({
                "resting_hr": v.get("restingHeartRate"),
                "hrv_ms": v.get("avgOvernightHrv"),
                "sleep_total_s": v.get("totalSleepTimeInSeconds"),
                "sleep_deep_s": v.get("deepTime"),
                "sleep_light_s": v.get("lightTime"),
                "sleep_rem_s": v.get("remTime"),
                "sleep_awake_s": v.get("awakeTime"),
                "sleep_score": v.get("sleepScore"),
                "spo2_pct": v.get("spO2"),
            })
            try:
                summary = self._api(
                    "/usersummary-service/usersummary/daily/{}".format(self.display_name()),
                    {"calendarDate": date}) or {}
                rec.update({
                    "resting_hr": rec["resting_hr"] or summary.get("restingHeartRate"),
                    "stress_avg": summary.get("averageStressLevel"),
                    "body_battery": summary.get("bodyBatteryChargedValue"),
                    "steps": summary.get("totalSteps"),
                    "calories": summary.get("totalKilocalories"),
                })
            except GarminApiError:
                pass  # summary 单日失败不阻断整批（睡眠字段已落）
            if any(rec.get(f) is not None for f in (
                    "resting_hr", "hrv_ms", "sleep_total_s", "stress_avg", "steps")):
                records[date] = rec
            day += timedelta(days=1)
        return [records[d] for d in sorted(records)]

    # -- 阈值锚点 --

    def fetch_thresholds(self):
        """阈值锚点（端点清单见 AGENTS.md"阈值锚点数据源"）。

        返回 {"run": {...}, "ride": {...}}，值为 threshold_anchor 列名 + valid_from；
        拿不到的字段置 None（lt1_hr 佳明无 API，需手填或估算）。
        """
        out = {"run": {}, "ride": {}}
        # 跑步乳酸阈值（LT2 心率 + 阈值速度；speed 是缩放值，配速 s/km = 100/speed）
        lt_hr = lt_speed = lt_date = None
        for row in self._api("/biometric-service/biometric/latestLactateThreshold") or []:
            if row.get("hearRate"):
                lt_hr = row["hearRate"]
                lt_date = (row.get("calendarDate") or "")[:10] or lt_date
            if row.get("speed"):
                lt_speed = 100.0 / row["speed"]
                lt_date = (row.get("calendarDate") or "")[:10] or lt_date
        out["run"].update({
            "lt2_hr": lt_hr,
            "threshold_pace_s": round(lt_speed, 0) if lt_speed else None,
            "critical_speed_mps": round(1000.0 / lt_speed, 3) if lt_speed else None,
            "valid_from": lt_date,
        })
        # 心率区间设置：maxHR（按类型，DEFAULT 回退）与骑行 LT 心率
        max_hr = ride_lthr = None
        for row in self._api("/biometric-service/heartRateZones") or []:
            if row.get("sport") == "DEFAULT":
                max_hr = max_hr or row.get("maxHeartRateUsed")
            elif row.get("sport") == "RUNNING":
                max_hr = row.get("maxHeartRateUsed") or max_hr
            elif row.get("sport") == "CYCLING":
                ride_lthr = row.get("lactateThresholdHeartRateUsed") or None
                max_hr = row.get("maxHeartRateUsed") or max_hr
        # 骑行 FTP：功率区间配置（latestFunctionalThresholdPower 常为空，作回退）
        ftp = None
        for row in self._api("/biometric-service/powerZones/sports/all") or []:
            if row.get("sport") == "CYCLING" and row.get("functionalThresholdPower"):
                ftp = row["functionalThresholdPower"]
        if ftp is None:
            latest = self._api(
                "/biometric-service/biometric/latestFunctionalThresholdPower/CYCLING") or {}
            ftp = latest.get("functionalThresholdPower")
        # 静息心率（Edwards TRIMP 必需输入）：取今日 user summary
        resting = None
        try:
            summary = self._api(
                "/usersummary-service/usersummary/daily/{}".format(self.display_name()),
                {"calendarDate": datetime.now().strftime("%Y-%m-%d")}) or {}
            resting = summary.get("restingHeartRate")
        except GarminApiError:
            pass
        for sport in ("run", "ride"):
            out[sport]["max_hr"] = max_hr
            out[sport]["resting_hr"] = resting
            out[sport]["lt1_hr"] = None  # 佳明无 LT1 API（AGENTS.md 阈值锚点数据源）
            out[sport]["valid_from"] = out[sport].get("valid_from") or \
                datetime.now().strftime("%Y-%m-%d")
        out["ride"].update({"lt2_hr": ride_lthr, "ftp_w": ftp})
        return out
