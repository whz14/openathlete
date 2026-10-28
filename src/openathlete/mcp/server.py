"""低层 MCP SDK 生命周期；网络客户端只在具体工具调用时创建。"""
import asyncio
import logging
import uuid

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp_types import ListToolsResult

from ..home import athlete_db_path, openathlete_home
from .envelope import failure
from .schemas import TOOL_NAMES, TOOLS
from .startup import BackgroundStartup, wait_for_startup

log = logging.getLogger(__name__)

STARTUP_TIMEOUT_S = 20.0
STARTUP_RETRY_AFTER_S = 30


def create_server(dispatch=None, startup=None):
    home = openathlete_home()

    async def _dispatch(name, args):
        if dispatch is not None:
            return await dispatch(name, args, home)
        from .tools import call_tool
        return await call_tool(name, args, home)

    async def on_list_tools(_ctx, _params):
        return ListToolsResult(tools=list(TOOLS))

    async def on_call_tool(_ctx, params):
        if params.name not in TOOL_NAMES:
            raise ValueError("未知工具：{}".format(params.name))
        pending = startup.start() if startup is not None else None
        if pending is not None:
            try:
                ready = await wait_for_startup(pending, STARTUP_TIMEOUT_S)
                if not ready:
                    return failure(
                        params.name,
                        "STARTING",
                        "OpenAthlete 正在初始化，尚未准备好执行该工具。",
                        retryable=True,
                        next_action="初始化仍在进行，请约 30 秒后重试。",
                        details={"retry_after_s": STARTUP_RETRY_AFTER_S},
                    )
            except Exception as exc:  # noqa: BLE001 初始化失败统一映射并消毒
                request_id = str(uuid.uuid4())
                log.exception("OpenAthlete 初始化失败 request_id=%s", request_id)
                return failure(
                    params.name,
                    "STARTUP_FAILED",
                    "OpenAthlete 初始化失败，请查看运行日志。",
                    retryable=False,
                    next_action="根据日志中的 request_id 排查；修复后重启 MCP。",
                    details={"exception_type": type(exc).__name__},
                    meta={"request_id": request_id},
                )
        try:
            return await _dispatch(params.name, params.arguments or {})
        except Exception as exc:  # noqa: BLE001 未预期异常统一消毒
            request_id = str(uuid.uuid4())
            log.exception("工具 %s 未预期失败 request_id=%s", params.name, request_id)
            return failure(
                params.name,
                "INTERNAL_ERROR",
                "工具执行发生未预期错误，请查看运行日志。",
                retryable=False,
                next_action="根据日志中的 request_id 排查；不要重复平台写操作。",
                details={"exception_type": type(exc).__name__},
                meta={"request_id": request_id},
            )

    return Server(
        "openathlete",
        version="0.1.0",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def _local_startup(home):
    home.mkdir(parents=True, exist_ok=True)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(home / "logs" / "openathlete-mcp.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        encoding="utf-8",
    )
    from ..db.athlete_db import connect, init_db
    from ..sync_state import init_sync_db
    from ..workout import init_workout_db

    conn = connect(athlete_db_path(home))
    try:
        init_db(conn)
    finally:
        conn.close()
    init_sync_db(home)
    init_workout_db(home)


async def _run():
    home = openathlete_home()
    startup = BackgroundStartup(lambda: asyncio.to_thread(_local_startup, home))
    try:
        server = create_server(startup=startup)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        await startup.close()


def main():
    asyncio.run(_run())
