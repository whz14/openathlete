"""从中性 cwd 验证一个 OpenAthlete stdio 入口；不触发平台网络。"""
import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from openathlete.config import set_config_values
from openathlete.mcp.schemas import TOOL_NAMES


async def verify(command):
    with tempfile.TemporaryDirectory(prefix="openathlete-entry-") as directory:
        home = Path(directory)
        set_config_values(home, timezone="UTC", workout_platform="garmin")
        params = StdioServerParameters(
            command=command[0],
            args=command[1:],
            cwd=home,
            env={"OPENATHLETE_HOME": str(home), "PYTHONUTF8": "1"},
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream,
                                     read_timeout_seconds=45) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == set(TOOL_NAMES)
                result = await session.call_tool("read_memory", {"name": "athlete"})
                assert result.structured_content["data"]["exists"] is False
                return {
                    "server": initialized.server_info.name,
                    "tools": len(listed.tools),
                    "call": "read_memory",
                    "ok": result.structured_content["ok"],
                }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("在 -- 后给出启动命令")
    result = asyncio.run(asyncio.wait_for(verify(command), timeout=60))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
