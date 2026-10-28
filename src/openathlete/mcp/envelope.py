"""统一工具 envelope：structuredContent 与文本内容保持一致。"""
import json
import uuid
from datetime import datetime, timezone

from mcp_types import CallToolResult, TextContent

SCHEMA_VERSION = "0.1"
DEFAULT_UNITS = {
    "distance_m": "m",
    "duration_s": "s",
    "avg_speed_mps": "m/s",
    "pace_s_per_km": "s/km",
    "trimp": "au",
    "ctl": "au",
    "atl": "au",
    "tsb": "au",
}


def base_meta(tool, meta=None):
    values = {
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "request_id": str(uuid.uuid4()),
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z"),
        "timezone": "UTC",
        "units": dict(DEFAULT_UNITS),
        "coverage": {},
        "warnings": [],
        "sync": {},
    }
    if meta:
        values.update(meta)
    return values


def _result(envelope, is_error):
    text = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return CallToolResult(
        content=[TextContent(text=text)],
        structured_content=envelope,
        is_error=is_error,
    )


def success(tool, data, meta=None):
    return _result({"ok": True, "data": data, "meta": base_meta(tool, meta)}, False)


def failure(tool, code, message, retryable=False, next_action="", details=None, meta=None):
    error = {
        "code": code,
        "message": message,
        "retryable": bool(retryable),
        "next_action": next_action,
        "details": details or {},
    }
    envelope = {
        "ok": False,
        "data": None,
        "error": error,
        "meta": base_meta(tool, meta),
    }
    return _result(envelope, True)
