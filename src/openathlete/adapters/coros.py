"""高驰 Training Hub 非官方 web API adapter：活动列表 / FIT 下载 / 日度指标。

端点与字段经三处实战实现交叉验证：running_page `coros_sync.py`（MIT）、
dholliday3/coros-training-mcp（MIT）、本仓库 upstream（佳明→高驰同步在用的登录/上传）。
DESIGN.md §4：数据抓取不经 MCP——本模块直连 web API，高驰官方 MCP 的 FIT 下载
限额（50 个/24h）因此不构成数据层瓶颈。

注意（AGENTS.md 已知坑 #1）：高驰单点登录，登录/抓取期间用户不要打开高驰 App 或网页版。
"""
import hashlib
import io
import json
import os
import tempfile
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from ..home import fit_dir as _fit_dir, openathlete_home

# 登录统一走国区端点；响应里的 regionId 决定后续请求的区域 base（upstream 同款逻辑）
LOGIN_URL = "https://teamcnapi.coros.com/account/login"
REGION_TEAMAPI = {
    1: "https://teamapi.coros.com",
    2: "https://teamcnapi.coros.com",
    3: "https://teameuapi.coros.com",
}
# web token 有效期 24h（coros-training-mcp 实测），缓存避免每次运行都登录互踢
TOKEN_TTL_S = 24 * 3600
FILE_TYPE_FIT = 4
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=15.0)

# 归档文件名用的归一运动词汇（文件名只是人读提示，入库归一以 FIT 内部 sport 字段为准）
_SPORT_FOLDER = [
    ((100, 101, 102, 103), "run"),
    ((104, 105, 106), "hike"),
    ((200, 201, 202, 203, 204, 205, 299), "ride"),
    ((300, 301), "swim"),
    ((900,), "walk"),
]


def archive_sport_name(sport_type) -> str:
    try:
        code = int(sport_type)
    except (TypeError, ValueError):
        return "sport0"
    for codes, name in _SPORT_FOLDER:
        if code in codes:
            return name
    return "sport{}".format(code)


class CorosApiError(Exception):
    """高驰 API 返回非 0000 或结构异常。"""


class ArchiveConflictError(CorosApiError):
    """归档目标已存在但字节不同；原文件必须保留。"""


def _check(body: dict, context: str) -> None:
    if body.get("result") != "0000":
        raise CorosApiError("高驰 {} 失败：{}".format(context, body.get("message", "未知错误")))


class CorosClient:
    """Training Hub web API 客户端（同步 httpx，transport 可注入便于测试）。"""

    def __init__(self, email, password, home=None, transport=None, timezone_name=None):
        self.email = email
        self.password = password
        self.home = Path(home) if home else openathlete_home()
        self.token_path = self.home / "coros-web-token.json"
        self._transport = transport
        self.timezone_name = timezone_name
        self.access_token = None
        self.user_id = None
        self.region_id = None

    # -- 认证 --

    def login(self):
        with self._client() as c:
            resp = c.post(
                LOGIN_URL,
                json={"account": self.email,
                      "pwd": hashlib.md5(self.password.encode()).hexdigest(),
                      "accountType": 2},
                headers={"Accept": "application/json, text/plain, */*",
                         "Content-Type": "application/json;charset=UTF-8"})
        body = resp.json()
        _check(body, "登录")
        data = body["data"]
        self.access_token = data["accessToken"]
        self.user_id = str(data["userId"])
        self.region_id = data["regionId"]
        self.token_path.write_text(json.dumps({
            "access_token": self.access_token, "user_id": self.user_id,
            "region_id": self.region_id, "ts": time.time(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_cached_token(self) -> bool:
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if time.time() - data.get("ts", 0) >= TOKEN_TTL_S:
            return False
        self.access_token = data["access_token"]
        self.user_id = data["user_id"]
        self.region_id = data["region_id"]
        return True

    def ensure_login(self):
        if self.access_token:
            return
        if not self._load_cached_token():
            self.login()

    @property
    def base_url(self) -> str:
        return REGION_TEAMAPI.get(self.region_id, REGION_TEAMAPI[2])

    def _headers(self) -> dict:
        return {
            "Accept": "application/json, text/plain, */*",
            "accesstoken": self.access_token,
            "cookie": "CPL-coros-region={}; CPL-coros-token={}".format(
                self.region_id, self.access_token),
        }

    def _client(self) -> httpx.Client:
        return httpx.Client(transport=self._transport, timeout=DEFAULT_TIMEOUT)

    def _get(self, path, params=None) -> dict:
        with self._client() as c:
            resp = c.get(self.base_url + path, params=params, headers=self._headers())
        body = resp.json()
        _check(body, path)
        return body["data"]

    def _post(self, path, params=None) -> dict:
        with self._client() as c:
            resp = c.post(self.base_url + path, params=params, headers=self._headers())
        body = resp.json()
        _check(body, path)
        return body["data"]

    # -- 活动 --

    def list_activities(self, start_day=None, end_day=None, page=1, size=50):
        """活动列表一页。start_day/end_day 为 YYYYMMDD 字符串（可空=不限）。
        返回 (items, total_count)。"""
        params = {"pageNumber": page, "size": size}
        if start_day:
            params["startDay"] = start_day
        if end_day:
            params["endDay"] = end_day
        data = self._get("/activity/query", params)
        items = data.get("dataList") or []
        total = data.get("totalCount") or data.get("count") or len(items)
        return items, total

    def iter_activities(self, start_day=None, end_day=None, size=50):
        """按时间倒序翻页遍历活动列表项（labelId/sportType/startTime/endTime 等）。"""
        page = 1
        while True:
            items, total = self.list_activities(start_day, end_day, page=page, size=size)
            if not items:
                return
            yield from items
            if page * size >= total:
                return
            page += 1

    def fit_download_url(self, label_id, sport_type) -> str:
        data = self._post("/activity/detail/download", params={
            "labelId": label_id, "sportType": int(sport_type),
            "fileType": FILE_TYPE_FIT,
        })
        url = data.get("fileUrl")
        if not url:
            raise CorosApiError("活动 {} 未返回 FIT 下载地址".format(label_id))
        return url

    def download_fit(self, label_id, sport_type) -> bytes:
        """下载活动 FIT 原始字节（可能是 zip 包——multisport 一段一个 fit）。"""
        url = self.fit_download_url(label_id, sport_type)
        with self._client() as c:
            resp = c.get(url, headers=self._headers())
        resp.raise_for_status()
        return resp.content

    # -- 日度指标 --

    def fetch_daily_records(self, start_day, end_day):
        """日度指标（YYYYMMDD 闭区间）。dayDetail 给长区间（约 24 周）的
        rhr/睡眠 HRV/负荷，analyse 补近 28 天的 vo2max/lthr/ltsp/体能等级。

        返回 [{"date": "YYYY-MM-DD", "resting_hr", "hrv_ms", "extras": {...}}]。
        """
        detail = self._get("/analyse/dayDetail/query",
                           {"startDay": start_day, "endDay": end_day})
        records = {}
        for item in detail.get("dayList") or []:
            date = str(item.get("happenDay") or "")
            if not date:
                continue
            records[date] = {
                "date": "{}-{}-{}".format(date[:4], date[4:6], date[6:8]),
                "resting_hr": item.get("rhr"),
                "hrv_ms": item.get("avgSleepHrv"),
                "extras": {k: item.get(k) for k in (
                    "sleepHrvBase", "trainingLoad", "trainingLoadRatio",
                    "tiredRateNew", "ati", "cti", "distance", "duration")},
            }
        try:
            analyse = self._get("/analyse/query")
        except CorosApiError:
            analyse = {}
        for item in analyse.get("t7dayList") or []:
            date = str(item.get("happenDay") or "")
            rec = records.get(date)
            if not rec:
                continue
            for k in ("vo2max", "lthr", "ltsp", "staminaLevel", "staminaLevel7d"):
                if item.get(k) is not None:
                    rec["extras"][k] = item[k]
        return [records[d] for d in sorted(records)]

    # -- 阈值锚点 --

    def fetch_thresholds(self):
        """高驰阈值锚点：/analyse/query 的 lthr/ltsp/vo2max + dayDetail 最新 rhr。

        max_hr 高驰 web API 无已知端点 → None（需手填）。
        返回 {"run": {...}}，值为 threshold_anchor 列名 + valid_from。
        """
        latest = {}
        for item in (self._get("/analyse/query").get("t7dayList") or []):
            if item.get("lthr") or item.get("ltsp"):
                latest = item  # t7dayList 按日期升序，取最后有值者
        valid_from = str(latest.get("happenDay") or "")
        valid_from = ("{}-{}-{}".format(valid_from[:4], valid_from[4:6], valid_from[6:8])
                      if valid_from else datetime.now().strftime("%Y-%m-%d"))
        ltsp = latest.get("ltsp")
        run = {
            "lt1_hr": None,  # 高驰同样无 LT1 API
            "lt2_hr": latest.get("lthr"),
            "threshold_pace_s": ltsp,
            "critical_speed_mps": round(1000.0 / ltsp, 3) if ltsp else None,
            "max_hr": None,
            "valid_from": valid_from,
        }
        # 静息心率（TRIMP 必需）：dayDetail 最近有值的一天
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
        detail = self._get("/analyse/dayDetail/query",
                           {"startDay": start, "endDay": end})
        for item in reversed(detail.get("dayList") or []):
            if item.get("rhr"):
                run["resting_hr"] = item["rhr"]
                break
        return {"run": run}

    # -- FIT 归档 --

    def archive_fit(self, summary: dict, payload: bytes, home=None, timezone_name=None) -> list:
        return archive_fit(summary, payload, home=home or self.home,
                           timezone_name=timezone_name or self.timezone_name)


def unpack_fit_parts(payload: bytes) -> list:
    """裸 .fit → [bytes]；zip → 逐个 .fit 成员（按文件名排序，与段序一致）。"""
    if zipfile.is_zipfile(io.BytesIO(payload)):
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = sorted(n for n in zf.namelist() if n.lower().endswith(".fit"))
            if not names:
                raise CorosApiError("下载包中没有 .fit 文件")
            return [zf.read(n) for n in names]
    return [payload]


def _write_fit_if_absent(path, fit_bytes):
    if path.exists():
        if path.read_bytes() != fit_bytes:
            raise ArchiveConflictError("FIT 归档同路径字节不同：{}".format(path))
        return
    fd, tmp_name = tempfile.mkstemp(prefix=".{}-".format(path.name), suffix=".tmp",
                                    dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(fit_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            if path.read_bytes() != fit_bytes:
                raise ArchiveConflictError("FIT 归档同路径字节不同：{}".format(path))
            return
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def archive_fit(summary: dict, payload: bytes, home, timezone_name=None) -> list:
    """按 DESIGN 命名原子归档裸 FIT；同路径只接受实际字节完全一致。"""
    root = _fit_dir(home)
    label_id = str(summary["labelId"])
    if not label_id or any(not (char.isalnum() or char in "_-") for char in label_id):
        raise CorosApiError("活动 labelId 不适合作为归档键")
    sport = archive_sport_name(summary.get("sportType"))
    ts = int(summary["startTime"])
    if timezone_name:
        stamp = datetime.fromtimestamp(ts, ZoneInfo(timezone_name)).strftime("%Y%m%d-%H%M%S")
    else:
        stamp = datetime.fromtimestamp(ts).astimezone().strftime("%Y%m%d-%H%M%S")
    month_dir = root / stamp[:4] / stamp[4:6]
    month_dir.mkdir(parents=True, exist_ok=True)
    parts = unpack_fit_parts(payload)
    written = []
    for idx, fit_bytes in enumerate(parts, 1):
        pid = label_id if len(parts) == 1 else "{}#{}".format(label_id, idx)
        path = month_dir / "{}-{}-coros-{}.fit".format(stamp, sport, pid)
        _write_fit_if_absent(path, fit_bytes)
        written.append(path)
    return written
