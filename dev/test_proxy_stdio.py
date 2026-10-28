# 端到端测试：像 kimi-code 一样，通过 stdio 拉起 coros_mcp_proxy.py 并走 MCP 协议
import asyncio, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from coros_mcp_proxy import check_schema

async def main():
    params = StdioServerParameters(
        command=str(ROOT / ".venv" / "Scripts" / "python.exe"),
        args=[str(ROOT / "coros_mcp_proxy.py")],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("server:", init.server_info.name, init.server_info.version)

            result = await session.list_tools()
            tools = result.tools
            expected = len(json.loads((ROOT / "coros_tools.json").read_text(encoding="utf-8")))
            print("tools:", len(tools))
            assert len(tools) == expected, "工具数量不对"

            bad = []
            for t in tools:
                problems = check_schema(t.input_schema or {})
                if problems:
                    bad.append((t.name, problems))
            if bad:
                for name, ps in bad:
                    print("FAIL", name, ps)
                sys.exit(1)
            print(f"全部 {len(tools)} 个工具 schema 通过 walle 关键规则自检")

            fixed = next(t for t in tools if t.name == "createSingleWorkout")
            assert "$defs" in fixed.input_schema, "根部应有 $defs"
            assert "$defs" not in fixed.input_schema["properties"]["course"], "嵌套 $defs 应被移除"
            print("createSingleWorkout: $defs 已在根部，嵌套已移除")

            r = await session.call_tool("queryDevices", {})
            assert not getattr(r, "is_error", False), r.content
            text = r.content[0].text if r.content else ""
            print("queryDevices 透传调用成功，返回前 80 字:", text[:80].replace("\n", " "))
            print("STDIO E2E 全部通过")

asyncio.run(main())
