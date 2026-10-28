import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]


def _proxy_params(tmp_path, mode, delay_s, timeout_s):
    driver = tmp_path / "proxy_driver.py"
    driver.write_text(
        "import asyncio\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "from mcp_types import CallToolResult, TextContent\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "import coros_mcp_proxy as proxy\n"
        "class FakeSession:\n"
        "    async def call_tool(self, name, arguments):\n"
        "        return CallToolResult(content=[TextContent(text='ok')], structured_content=None, is_error=False)\n"
        "class FakeStack:\n"
        "    def __init__(self):\n"
        "        self.owner = asyncio.current_task()\n"
        "    async def aclose(self):\n"
        "        assert self.owner is asyncio.current_task(), 'stack closed in another task'\n"
        "        Path(os.environ['PROXY_CLOSE_MARKER']).write_text('closed', encoding='utf-8')\n"
        "async def fake_connect(interactive):\n"
        "    await asyncio.sleep(float(os.environ['PROXY_DELAY_S']))\n"
        "    if os.environ['PROXY_MODE'] == 'error':\n"
        "        raise RuntimeError('synthetic upstream failure')\n"
        "    if os.environ['PROXY_MODE'] == 'timeout':\n"
        "        raise TimeoutError('synthetic startup timeout')\n"
        "    if os.environ['PROXY_MODE'] == 'hang':\n"
        "        await asyncio.Future()\n"
        "    return FakeStack(), FakeSession()\n"
        "async def fake_list_tools(session):\n"
        "    Path(os.environ['PROXY_READY_MARKER']).write_text('ready', encoding='utf-8')\n"
        "    return proxy._read_tool_file(proxy.BUNDLED_TOOLS)\n"
        "if os.environ.get('PROXY_MARK_WRITE'):\n"
        "    original_write = proxy._write_fixed_dump\n"
        "    def marked_write(tools):\n"
        "        Path(os.environ['PROXY_MARK_WRITE']).write_text('writing', encoding='utf-8')\n"
        "        return original_write(tools)\n"
        "    proxy._write_fixed_dump = marked_write\n"
        "proxy._connect = fake_connect\n"
        "proxy.list_all_tools = fake_list_tools\n"
        "proxy.STARTUP_TIMEOUT_S = float(os.environ['PROXY_TIMEOUT_S'])\n"
        "proxy.main()\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    ready = tmp_path / "ready"
    closed = tmp_path / "closed"
    env = dict(os.environ)
    env.update({
        "OPENATHLETE_HOME": str(home),
        "PYTHONUTF8": "1",
        "PROXY_MODE": mode,
        "PROXY_DELAY_S": str(delay_s),
        "PROXY_TIMEOUT_S": str(timeout_s),
        "PROXY_READY_MARKER": str(ready),
        "PROXY_CLOSE_MARKER": str(closed),
    })
    params = StdioServerParameters(
        command=sys.executable, args=[str(driver)], cwd=tmp_path, env=env)
    return params, ready, closed


def test_proxy_lists_before_upstream_and_starting_then_success(tmp_path):
    params, ready, closed = _proxy_params(tmp_path, "ready", delay_s=1.0, timeout_s=0.2)

    async def scenario():
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream,
                                     read_timeout_seconds=30) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert len(listed.tools) == 33
                assert not ready.exists()

                early = await session.call_tool("queryDevices", {})
                assert early.is_error is True
                assert early.structured_content["error"]["code"] == "STARTING"
                assert early.structured_content["error"]["retryable"] is True
                assert not ready.exists()

                await asyncio.sleep(1.2)
                result = await session.call_tool("queryDevices", {})
                assert result.is_error is False
                assert result.content[0].text == "ok"
                assert ready.exists()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))
    assert closed.exists()


def test_proxy_startup_failure_keeps_static_declarations(tmp_path):
    params, _ready, _closed = _proxy_params(tmp_path, "error", delay_s=0.1, timeout_s=1)

    async def scenario():
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream,
                                     read_timeout_seconds=30) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 33
                result = await session.call_tool("queryDevices", {})
                assert result.is_error is True
                error = result.structured_content["error"]
                assert error["code"] == "STARTUP_FAILED"
                assert error["retryable"] is False
                assert error["details"]["exception_type"] == "RuntimeError"

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))


def test_proxy_close_cancels_hung_startup(tmp_path):
    params, _ready, _closed = _proxy_params(tmp_path, "hang", delay_s=0, timeout_s=1)

    async def scenario():
        import time

        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream,
                                     read_timeout_seconds=30) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 33
                close_started = time.monotonic()
        assert time.monotonic() - close_started < 5

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))


def test_proxy_cache_lock_does_not_block_tool_listing(tmp_path):
    from openathlete.locks import home_lock

    params, _ready, closed = _proxy_params(tmp_path, "ready", delay_s=0, timeout_s=1)
    write_started = tmp_path / "write-started"
    params.env["PROXY_MARK_WRITE"] = str(write_started)

    async def scenario():
        lock = home_lock(tmp_path / "home", "coros-tools")
        lock.acquire()
        try:
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream,
                                         read_timeout_seconds=30) as session:
                    await session.initialize()
                    for _ in range(100):
                        if write_started.exists():
                            break
                        await asyncio.sleep(0.02)
                    assert write_started.exists()
                    listed = await asyncio.wait_for(session.list_tools(), timeout=0.5)
                    assert len(listed.tools) == 33
                    lock.release()
                    result = await session.call_tool("queryDevices", {})
                    assert result.is_error is False
                    assert result.content[0].text == "ok"
        finally:
            lock.release()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))
    assert closed.exists()
