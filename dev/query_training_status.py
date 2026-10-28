# 通过本地 stdio 代理调用高驰官方 MCP，拉取训练分析所需数据并落盘
import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

OUT = Path(__file__).resolve().parent / "last_training_query.json"
D1 = date.today()
D0 = D1 - timedelta(days=45)
SLEEP_START = D1 - timedelta(days=6)

CALLS = [
    ("queryFitnessAssessmentOverview", {}),
    ("queryTrainingLoadAssessment", {"days": 42}),
    ("queryRecoveryStatus", {}),
    ("querySportRecords", {
        "startDate": D0.strftime("%Y%m%d"), "endDate": D1.strftime("%Y%m%d"),
        "sportTypeCodes": [100, 101, 102, 103],
    }),
    ("queryRestingHeartRate", {"days": 14}),
    ("querySleepHrv", {
        "startDate": SLEEP_START.strftime("%Y%m%d"),
        "endDate": D1.strftime("%Y%m%d"), "days": 7,
    }),
]


async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "coros_mcp_proxy.py")],
    )
    results = {}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for name, args in CALLS:
                try:
                    r = await session.call_tool(name, args)
                    text = "\n".join(
                        getattr(c, "text", "") for c in (r.content or [])
                    )
                    results[name] = {"ok": not getattr(r, "is_error", False), "text": text}
                    print("== {} -> {} ({} 字符)".format(
                        name, "OK" if results[name]["ok"] else "ERROR", len(text)))
                except Exception as err:  # noqa: BLE001
                    results[name] = {"ok": False, "text": "调用异常: {}".format(err)}
                    print("== {} -> 异常: {}".format(name, err))
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写入", OUT)


asyncio.run(main())
