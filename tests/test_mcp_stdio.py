import asyncio
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from openathlete.config import set_config_values

ROOT = Path(__file__).resolve().parents[1]



def test_real_stdio_all_nine_tools_with_local_coverage(tmp_path):
    import time
    from datetime import datetime, timezone

    from openathlete.db.athlete_db import connect, init_db, upsert_daily_metrics, write_anchor
    from openathlete.sync_state import connect_sync_db, init_sync_db

    set_config_values(tmp_path, timezone="UTC", workout_platform="garmin",
                      primary_platform="garmin", activity_since="2020-01-01")
    conn = connect(tmp_path / "athlete.db")
    init_db(conn)
    start = int(datetime(2026, 10, 20, 12, tzinfo=timezone.utc).timestamp())
    conn.execute(
        "insert into activity (id,start_ts,tz_offset_s,sport,duration_s,distance_m,"
        "avg_hr,max_hr,trimp,structure_json,zone_boundaries_json) "
        "values (1,?,0,'run',1800,5000,150,170,50,?,?)",
        (start, '{"type":"continuous","blocks":[]}', "[140,150,170,180,190,200]"))
    conn.execute(
        "insert into activity_source (activity_id,platform,platform_activity_id,"
        "fit_path,fit_sha256,is_canonical) values (1,'garmin','g1','x',?,1)",
        ("d" * 64,))
    for day, ctl in ((20, 8), (21, 8), (22, 9), (23, 9), (24, 10)):
        conn.execute(
            "insert into daily_load values (?,?,?,?,?)",
            ("2026-10-{:02d}".format(day), 0 if day != 20 else 50, ctl, ctl + 2, -2))
    write_anchor(conn, "run", {
        "valid_from": "2020-01-01", "max_hr": 185, "resting_hr": 50,
        "lt1_hr": 140, "lt2_hr": 170,
    }, "test")
    upsert_daily_metrics(conn, [{
        "date": "2026-10-24", "source": "coros", "resting_hr": 45, "hrv_ms": 70,
    }])
    conn.close()
    init_sync_db(tmp_path)
    sync_conn = connect_sync_db(tmp_path)
    now = int(time.time())
    sync_conn.execute(
        "update oa_sync_state set coverage_start_date='2020-01-01', "
        "coverage_end_date='2099-01-01', discovery_complete=1, "
        "high_water_ts=?, last_success_ts=?", (now, now))
    sync_conn.commit()
    sync_conn.close()
    (tmp_path / "memory").mkdir(exist_ok=True)
    (tmp_path / "memory" / "races.md").write_text(
        "# 赛事\n\n## 目标赛事\n\n（无）\n\n## 历史赛事\n", encoding="utf-8")

    async def scenario():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(ROOT / "openathlete_mcp.py")],
            cwd=tmp_path,
            env={"OPENATHLETE_HOME": str(tmp_path), "PYTHONUTF8": "1"},
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream,
                                     read_timeout_seconds=30) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 9

                activities = await session.call_tool("query_activities", {
                    "start_date": "2026-10-18", "end_date": "2026-10-24", "detail": True,
                })
                assert activities.structured_content["ok"] is True
                assert activities.structured_content["data"]["returned"] == 1

                load = await session.call_tool("get_training_load", {
                    "days": 7, "end_date": "2026-10-24",
                })
                assert load.structured_content["ok"] is True
                assert load.structured_content["meta"]["coverage"]["pmc_initialized"] is True

                recovery = await session.call_tool("get_recovery_status", {"date": "2026-10-24"})
                assert recovery.structured_content["ok"] is True
                assert recovery.structured_content["data"]["resting_hr_bpm"] == 45

                overview = await session.call_tool("get_overview", {
                    "days": 7, "end_date": "2026-10-24",
                })
                assert overview.structured_content["ok"] is True
                assert overview.structured_content["data"]["training"]["activity_count"] == 1

                synced = await session.call_tool("sync_now", {})
                assert synced.is_error is True
                assert synced.structured_content["error"]["code"] == "AUTH_REQUIRED"

                plan = await session.call_tool("get_current_plan", {"date": "2026-10-24"})
                assert plan.structured_content["ok"] is True

                memory = await session.call_tool("read_memory", {"name": "athlete"})
                assert memory.structured_content["data"]["exists"] is False

                written = await session.call_tool("write_memory", {
                    "name": "athlete",
                    "content": "# 运动员\n\n## 画像\n\n- 测试\n\n## 能力基线\n\n- 待补\n",
                    "allow_create": True,
                })
                assert written.structured_content["ok"] is True

                workout = {
                    "date": "2026-10-25", "sport": "run", "name": "协议测试课",
                    "description": "仅本地预览",
                    "blocks": [{"kind": "steady", "role": "main",
                                "target": {"kind": "duration", "value": 600},
                                "intensity": None}],
                }
                preview = await session.call_tool("create_workout", {"workout": workout})
                assert preview.structured_content["data"]["status"] == "preview"

    asyncio.run(asyncio.wait_for(scenario(), timeout=45))


def test_real_stdio_accepts_unknown_trimp_and_uninitialized_pmc(tmp_path):
    import time

    from openathlete.db.athlete_db import connect, init_db
    from openathlete.sync_state import connect_sync_db, init_sync_db

    set_config_values(tmp_path, timezone="UTC", activity_since="2020-01-01")
    init_sync_db(tmp_path)
    sync_conn = connect_sync_db(tmp_path)
    now = int(time.time())
    sync_conn.execute(
        "update oa_sync_state set coverage_start_date='2020-01-01', "
        "coverage_end_date='2099-01-01', discovery_complete=1, "
        "high_water_ts=?, last_success_ts=?", (now, now))
    sync_conn.commit()
    sync_conn.close()

    conn = connect(tmp_path / "athlete.db")
    init_db(conn)
    conn.execute("insert into daily_load values ('2026-09-30',40,1,2,-1)")
    conn.execute("insert into daily_load values ('2026-10-01',null,null,null,null)")
    conn.execute("insert into daily_load values ('2026-10-02',50,null,null,null)")
    conn.commit()
    conn.close()

    async def scenario():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(ROOT / "openathlete_mcp.py")],
            cwd=tmp_path,
            env={"OPENATHLETE_HOME": str(tmp_path), "PYTHONUTF8": "1"},
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream,
                                     read_timeout_seconds=30) as session:
                await session.initialize()
                result = await session.call_tool("get_training_load", {
                    "days": 7, "end_date": "2026-10-03",
                })
                assert result.structured_content["ok"] is True
                data = result.structured_content["data"]
                assert data["latest"]["date"] == "2026-09-30"
                assert data["daily"][1]["trimp"] is None
                assert data["daily"][2]["ctl"] is None
                coverage = result.structured_content["meta"]["coverage"]
                assert coverage["pmc_initialized"] is False

    asyncio.run(asyncio.wait_for(scenario(), timeout=45))


def _startup_params(tmp_path, mode, delay_s, timeout_s=20):
    set_config_values(tmp_path, timezone="UTC")
    marker = tmp_path / "startup-ready"
    driver = tmp_path / "startup_driver.py"
    driver.write_text(
        "import os\n"
        "import time\n"
        "from pathlib import Path\n"
        "from openathlete.mcp import server\n"
        "def startup(home):\n"
        "    if os.environ['STARTUP_MODE'] == 'error':\n"
        "        raise RuntimeError('synthetic startup failure')\n"
        "    time.sleep(float(os.environ['STARTUP_DELAY_S']))\n"
        "    Path(os.environ['STARTUP_MARKER']).write_text('ready', encoding='utf-8')\n"
        "server._local_startup = startup\n"
        "server.STARTUP_TIMEOUT_S = float(os.environ['STARTUP_TIMEOUT_S'])\n"
        "server.main()\n",
        encoding="utf-8",
    )
    env = {
        "OPENATHLETE_HOME": str(tmp_path),
        "PYTHONUTF8": "1",
        "STARTUP_MODE": mode,
        "STARTUP_DELAY_S": str(delay_s),
        "STARTUP_TIMEOUT_S": str(timeout_s),
        "STARTUP_MARKER": str(marker),
    }
    params = StdioServerParameters(
        command=sys.executable, args=[str(driver)], cwd=tmp_path, env=env)
    return params, marker


def test_real_stdio_lists_before_startup_and_calls_wait(tmp_path):
    params, marker = _startup_params(tmp_path, "ready", delay_s=2)

    async def scenario():
        import time

        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream,
                                     read_timeout_seconds=15) as session:
                await session.initialize()
                assert not marker.exists()
                assert len((await session.list_tools()).tools) == 9
                assert not marker.exists()
                started = time.monotonic()
                result = await session.call_tool("read_memory", {"name": "athlete"})
                assert time.monotonic() - started >= 1.0
                assert result.structured_content["ok"] is True
                assert marker.exists()
                again = await session.call_tool("read_memory", {"name": "athlete"})
                assert again.structured_content["ok"] is True

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))


def test_real_stdio_startup_timeout_is_structured_and_does_not_cancel(tmp_path):
    params, marker = _startup_params(tmp_path, "ready", delay_s=2, timeout_s=0.2)

    async def scenario():
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream,
                                     read_timeout_seconds=15) as session:
                await session.initialize()
                result = await session.call_tool("read_memory", {"name": "athlete"})
                assert result.is_error is True
                error = result.structured_content["error"]
                assert error["code"] == "STARTING"
                assert error["retryable"] is True
                assert error["details"]["retry_after_s"] == 30
                assert not marker.exists()

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    assert marker.exists()


def test_real_stdio_startup_failure_is_structured(tmp_path):
    params, _marker = _startup_params(tmp_path, "error", delay_s=0)

    async def scenario():
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream,
                                     read_timeout_seconds=15) as session:
                await session.initialize()
                result = await session.call_tool("read_memory", {"name": "athlete"})
                assert result.is_error is True
                error = result.structured_content["error"]
                assert error["code"] == "STARTUP_FAILED"
                assert error["retryable"] is False
                assert error["details"]["exception_type"] == "RuntimeError"

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))
