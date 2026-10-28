import json
from concurrent.futures import ThreadPoolExecutor

import coros_mcp_proxy as proxy

WRITE_TOOLS = {
    "createScheduledWorkout",
    "updateScheduledWorkout",
    "createTrainingPlan",
    "updateTrainingPlan",
    "createSingleWorkout",
    "updateWorkoutDetails",
}


def _tool_json(name):
    tools = json.loads(proxy.BUNDLED_TOOLS.read_text(encoding="utf-8"))
    return next(item for item in tools if item["name"] == name)


def test_bundled_tools_are_current_and_schema_valid():
    tools = proxy._read_tool_file(proxy.BUNDLED_TOOLS)
    names = {tool.name for tool in tools}
    assert len(tools) == 33
    assert WRITE_TOOLS <= names
    assert "queryDevices" in names
    assert "analyzeActivityDetail" not in names
    for tool in tools:
        assert proxy.check_schema(tool.input_schema or {}) == []
    for name in WRITE_TOOLS:
        tool = next(item for item in tools if item.name == name)
        assert "$defs" in tool.input_schema


def test_initial_tools_prefer_newer_cache_and_fall_back(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled.json"
    cache = tmp_path / "cache.json"
    bundled.write_text(json.dumps([_tool_json("queryDevices")]), encoding="utf-8")
    cache.write_text(json.dumps([_tool_json("queryUserInfo")]), encoding="utf-8")
    monkeypatch.setattr(proxy, "BUNDLED_TOOLS", bundled)
    monkeypatch.setattr(proxy, "FIXED_DUMP", cache)

    bundled.touch()
    cache.touch()
    import os
    os.utime(bundled, (100, 100))
    os.utime(cache, (200, 200))
    assert [tool.name for tool in proxy._load_initial_tools()] == ["queryUserInfo"]

    os.utime(bundled, (300, 300))
    assert [tool.name for tool in proxy._load_initial_tools()] == ["queryDevices"]

    cache.write_text("{broken", encoding="utf-8")
    os.utime(cache, (400, 400))
    assert [tool.name for tool in proxy._load_initial_tools()] == ["queryDevices"]


def test_fixed_dump_is_atomic_under_concurrent_writers(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "OPENATHLETE_HOME", tmp_path)
    monkeypatch.setattr(proxy, "LOG_PATH", tmp_path / "logs" / "proxy.log")
    monkeypatch.setattr(proxy, "FIXED_DUMP", tmp_path / "logs" / "coros_tools_fixed.json")
    from mcp_types import Tool
    first = [Tool.model_validate(_tool_json("queryDevices"))]
    second = [Tool.model_validate(_tool_json("queryUserInfo"))]

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(proxy._write_fixed_dump, [first, second]))

    data = json.loads(proxy.FIXED_DUMP.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["name"] in {"queryDevices", "queryUserInfo"}
    assert list((tmp_path / "logs").glob(".coros-tools-*.json")) == []
