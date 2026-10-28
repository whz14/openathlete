#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2.2,<3"]
# ///
"""高驰官方 MCP 的本地 stdio 代理：修复 Moonshot API 不接受的 tool schema。

背景：高驰 MCP 的 6 个写工具（createScheduledWorkout / updateScheduledWorkout /
createTrainingPlan / updateTrainingPlan / createSingleWorkout / updateWorkoutDetails）
的 inputSchema 把 $defs 嵌在 properties.course（或 courseList）内部，而 $ref 写的是
根级引用 "#/$defs/Section"。Moonshot 的 walle 校验器只在 schema 根部找 $defs，
于是整个会话的 LLM 请求被 400 拒绝。本代理把嵌套 $defs 提升到根部，语义不变。

用法（推荐 `uv run` 直跑，PEP 723 内联依赖自动解析；仓库开发期也可用 ./.venv/Scripts/python.exe）：

    uv run ./coros_mcp_proxy.py --import-kimi-tokens   # 从 kimi-code 凭据库导入已有 OAuth token
    uv run ./coros_mcp_proxy.py --selftest             # 连通性 + schema 修复自检（含一次只读调用）
    uv run ./coros_mcp_proxy.py --login                # 浏览器重新走 OAuth（token 失效时用）
    uv run ./coros_mcp_proxy.py                        # stdio MCP server 模式（给宿主 agent 用）

kimi-code 的 .kimi-code/mcp.json 配置见 AGENTS.md。stdio 模式下 stdout 只走协议。
首轮工具声明来自 coros_tools.json 或较新的运行时缓存，不等待官方连接；
官方连接与声明刷新在后台完成，过早调用返回 STARTING，初始化失败返回 STARTUP_FAILED。
运行状态（OAuth token、日志）存 $OPENATHLETE_HOME（默认 ~/.openathlete/）下；
检测到仓库旧布局 ./data/ 时一次性复制迁移，不删旧文件。
"""

import argparse
import asyncio
import copy
import json
import logging
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from openathlete.locks import LockBusy, home_lock
from openathlete.mcp.envelope import failure
from openathlete.mcp.startup import BackgroundStartup, wait_for_startup

OPENATHLETE_HOME = Path(os.environ["OPENATHLETE_HOME"]).expanduser() \
    if os.environ.get("OPENATHLETE_HOME") else Path.home() / ".openathlete"
STORE_DIR = OPENATHLETE_HOME / "coros-mcp-proxy"
_LEGACY_STORE_DIR = ROOT / "data" / "coros-mcp-proxy"
if not STORE_DIR.exists() and _LEGACY_STORE_DIR.exists():
    try:
        shutil.copytree(_LEGACY_STORE_DIR, STORE_DIR)
    except Exception as e:
        print(f"旧 data/ 布局迁移失败（忽略，按无 token 处理）: {e}", file=sys.stderr)
CLIENT_PATH = STORE_DIR / "oauth-client.json"
TOKEN_PATH = STORE_DIR / "oauth-tokens.json"
LOG_PATH = OPENATHLETE_HOME / "logs" / "coros-mcp-proxy.log"
FIXED_DUMP = OPENATHLETE_HOME / "logs" / "coros_tools_fixed.json"
BUNDLED_TOOLS = ROOT / "coros_tools.json"
STARTUP_TIMEOUT_S = 20.0
STARTUP_RETRY_AFTER_S = 30

UPSTREAM_URL = "https://mcpcn.coros.com/mcp"
REDIRECT_URI = "http://127.0.0.1:14327/callback"  # 与 kimi-code 已注册的动态客户端一致
OAUTH_SCOPE = "openid mcp.tools offline_access"
KIMI_CRED_DIR = Path.home() / ".kimi-code" / "credentials" / "mcp"

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_PATH), level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("coros-mcp-proxy")


# ---------------------------------------------------------------- schema 修复

def _rewrite_refs(node, old, new):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and v == old:
                node[k] = new
            else:
                _rewrite_refs(v, old, new)
    elif isinstance(node, list):
        for v in node:
            _rewrite_refs(v, old, new)


def hoist_nested_defs(schema):
    """把根部以外出现的 $defs 提升到根部；返回 (新 schema, 提升的定义名列表)。"""
    schema = copy.deepcopy(schema)
    root_defs = {}
    hoisted = []

    def walk(node):
        if isinstance(node, dict):
            defs = node.pop("$defs", None)
            if isinstance(defs, dict):
                for name, definition in defs.items():
                    if name in root_defs and root_defs[name] != definition:
                        i = 2
                        while f"{name}_{i}" in root_defs:
                            i += 1
                        new_name = f"{name}_{i}"
                        _rewrite_refs(definition, f"#/$defs/{name}", f"#/$defs/{new_name}")
                        log.warning("$defs 名称冲突：%s 重命名为 %s", name, new_name)
                        root_defs[new_name] = definition
                        hoisted.append(new_name)
                    else:
                        root_defs[name] = definition
                        hoisted.append(name)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    if root_defs:
        schema["$defs"] = root_defs
    return schema, hoisted


def check_schema(schema):
    """自检：对照 walle 的关键规则——$ref 必须指向根级 $defs，且根部以外不得有 $defs。"""
    problems = []

    def walk(node, path):
        if isinstance(node, dict):
            if "$defs" in node and path != "$":
                problems.append(f"嵌套 $defs 残留于 {path}")
            ref = node.get("$ref")
            if isinstance(ref, str):
                if not ref.startswith("#/$defs/"):
                    problems.append(f"{path}: $ref 不是根级 $defs 引用: {ref}")
                elif ref[len("#/$defs/"):] not in schema.get("$defs", {}):
                    problems.append(f"{path}: $defs 中找不到 {ref}")
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(schema, "$")
    return problems


# ---------------------------------------------------------------- OAuth 存储

class FileTokenStorage:
    """OAuthClientProvider 的文件存储协议实现（token/client 各一个 JSON）。"""

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        if not TOKEN_PATH.exists():
            return None
        try:
            return OAuthToken.model_validate_json(TOKEN_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("读取 token 失败：%s", e)
            return None

    async def set_tokens(self, tokens):
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(tokens.model_dump_json(exclude_none=True), encoding="utf-8")
        log.info("token 已更新并存盘")

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        if not CLIENT_PATH.exists():
            return None
        try:
            return OAuthClientInformationFull.model_validate_json(CLIENT_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("读取 client 信息失败：%s", e)
            return None

    async def set_client_info(self, client_info):
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        CLIENT_PATH.write_text(client_info.model_dump_json(exclude_none=True), encoding="utf-8")
        log.info("动态客户端注册信息已存盘")


def _make_callback_handler():
    """在 127.0.0.1:14327 等待 OAuth 回调，返回 AuthorizationCodeResult。"""
    from mcp.shared.auth import AuthorizationCodeResult
    results = queue.Queue()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urlparse(self.path)
            if u.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            q = parse_qs(u.query)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h3>高驰授权完成，请回到终端。</h3>".encode("utf-8"))
            results.put({"code": q.get("code", [None])[0], "state": q.get("state", [None])[0]})

        def log_message(self, *args):
            pass

    async def callback_handler():
        port = int(urlparse(REDIRECT_URI).port)
        server = HTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.info("等待 OAuth 回调：%s", REDIRECT_URI)
        try:
            loop = asyncio.get_running_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, results.get), timeout=300)
            return AuthorizationCodeResult(code=result["code"], state=result["state"])
        finally:
            server.shutdown()

    return callback_handler


def _make_oauth(interactive):
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    metadata = OAuthClientMetadata(
        client_name="openathlete coros schema-fix proxy",
        redirect_uris=[REDIRECT_URI],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        scope=OAUTH_SCOPE,
    )

    async def open_browser(url):
        print("请在打开的浏览器页面中完成高驰授权（5 分钟内）：\n" + url, flush=True)
        webbrowser.open(url)

    async def refuse(url):
        msg = (f"上游 OAuth 需要重新授权，但 stdio 模式不能交互。"
               f"请先运行: uv run {Path(__file__).resolve()} --login")
        log.error("%s (auth url: %s)", msg, url)
        raise RuntimeError(msg)

    return OAuthClientProvider(
        server_url=UPSTREAM_URL,
        client_metadata=metadata,
        storage=FileTokenStorage(),
        redirect_handler=open_browser if interactive else refuse,
        callback_handler=_make_callback_handler(),
    )


# ---------------------------------------------------------------- 上游连接

async def _connect(interactive):
    """返回 (exit_stack, session)。调用方负责 aclose。"""
    from contextlib import AsyncExitStack
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    stack = AsyncExitStack()
    try:
        http = await stack.enter_async_context(
            create_mcp_http_client(auth=_make_oauth(interactive)))
        read, write = await stack.enter_async_context(
            streamable_http_client(UPSTREAM_URL, http_client=http))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        log.info("上游连接成功：%s", UPSTREAM_URL)
        return stack, session
    except BaseException:
        await stack.aclose()
        raise


async def list_all_tools(session):
    from mcp_types import PaginatedRequestParams
    tools, cursor = [], None
    while True:
        params = PaginatedRequestParams(cursor=cursor) if cursor else None
        result = await session.list_tools(params=params)
        tools.extend(result.tools)
        cursor = result.next_cursor
        if not cursor:
            return tools


def fix_tools(tools):
    fixed, touched = [], []
    for t in tools:
        new_schema, hoisted = hoist_nested_defs(t.input_schema or {})
        problems = check_schema(new_schema)
        if problems:
            log.error("工具 %s 修复后仍有问题：%s", t.name, problems)
            raise RuntimeError(f"工具 {t.name} schema 修复后未通过自检: {problems}")
        if hoisted:
            touched.append(t.name)
        fixed.append(t.model_copy(update={"input_schema": new_schema}))
    return fixed, touched


CACHE_LOCK_TIMEOUT_S = 5.0


def _write_fixed_dump(tools):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + CACHE_LOCK_TIMEOUT_S
    while True:
        lock = home_lock(OPENATHLETE_HOME, "coros-tools")
        try:
            lock.acquire()
            break
        except LockBusy:
            if time.monotonic() >= deadline:
                raise TimeoutError("COROS 工具声明缓存锁等待超时")
            time.sleep(0.05)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".coros-tools-", suffix=".json",
                                        dir=LOG_PATH.parent)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump([t.model_dump(mode="json", exclude_none=True) for t in tools],
                      handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, FIXED_DUMP)
    finally:
        lock.release()
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _read_tool_file(path):
    from mcp_types import Tool

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("工具声明为空或格式错误")
    tools = [Tool.model_validate(item) for item in data]
    if len({tool.name for tool in tools}) != len(tools):
        raise ValueError("工具声明存在重名")
    fixed, _ = fix_tools(tools)
    return fixed


def _load_initial_tools():
    candidates = []
    for path in (FIXED_DUMP, BUNDLED_TOOLS):
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            pass
    candidates.sort(key=lambda item: item[0], reverse=True)
    for _mtime, path in candidates:
        try:
            tools = _read_tool_file(path)
            log.info("首轮工具声明加载完成：%s（%d 个）", path, len(tools))
            return tools
        except Exception as exc:
            log.warning("读取工具声明失败，尝试下一候选：%s：%s", path, exc)
    raise RuntimeError("没有可用的 COROS 首轮工具声明")


async def _refresh_tools(session):
    upstream_tools, touched = fix_tools(await list_all_tools(session))
    if not upstream_tools:
        raise RuntimeError("上游工具清单为空")
    await asyncio.to_thread(_write_fixed_dump, upstream_tools)
    log.info("工具加载完成：%d 个，其中 %d 个做了 $defs 提升: %s",
             len(upstream_tools), len(touched), touched)
    return upstream_tools, touched


def _startup_error(name, exc):
    request_id = str(uuid.uuid4())
    log.exception("COROS 初始化失败 request_id=%s", request_id)
    return failure(
        name,
        "STARTUP_FAILED",
        "COROS 初始化失败，请查看运行日志。",
        retryable=False,
        next_action="根据日志中的 request_id 排查；修复后重启 MCP。",
        details={"exception_type": type(exc).__name__},
        meta={"request_id": request_id},
    )


# ---------------------------------------------------------------- stdio 服务

async def serve_stdio():
    from mcp.server.mcpserver import MCPServer

    state = {"session": None, "tools": _load_initial_tools()}
    ready = asyncio.get_running_loop().create_future()

    async def prepare():
        stack = None
        try:
            stack, session = await _connect(interactive=False)
            state["tools"], _ = await _refresh_tools(session)
            state["session"] = session
            if not ready.done():
                ready.set_result(None)
            await asyncio.Future()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            raise
        finally:
            if stack is not None:
                await stack.aclose()

    startup = BackgroundStartup(prepare, ready=ready, cancel_on_close=True)

    class CorosProxy(MCPServer):
        async def list_tools(self):
            return state["tools"]

        async def call_tool(self, name, arguments, context=None):
            pending = startup.start()
            try:
                initialized = await wait_for_startup(pending, STARTUP_TIMEOUT_S)
                if not initialized:
                    return failure(
                        name,
                        "STARTING",
                        "COROS 正在初始化，尚未准备好执行该工具。",
                        retryable=True,
                        next_action="初始化仍在进行，请约 30 秒后重试。",
                        details={"retry_after_s": STARTUP_RETRY_AFTER_S},
                    )
            except Exception as exc:
                return _startup_error(name, exc)
            log.info("转发工具调用: %s", name)
            return await state["session"].call_tool(name, arguments or {})

    server = CorosProxy(name="coros", version="0.1.0")
    try:
        startup.start()
        await server.run_stdio_async()
    finally:
        await startup.close()


# ---------------------------------------------------------------- 命令行模式

async def selftest():
    stack, session = await _connect(interactive=False)
    async with stack:
        fixed, touched = await _refresh_tools(session)
        print(f"[1/3] 上游 tools/list 成功，共 {len(fixed)} 个工具")
        print(f"[2/3] schema 修复+自检通过；修复了 {len(touched)} 个: {', '.join(touched)}")
        print(f"      修复后工具清单已写入 {FIXED_DUMP}")
        result = await session.call_tool("queryDevices", {})
        if getattr(result, "is_error", False):
            raise RuntimeError(f"queryDevices 返回错误: {result.content}")
        print("[3/3] 真实调用 queryDevices 成功（OAuth 有效、转发链路可用）")
    print("自检全部通过。")


async def login():
    stack, _session = await _connect(interactive=True)
    async with stack:
        pass
    print("授权完成，token 已存到", TOKEN_PATH)


def import_kimi_tokens():
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

    clients = sorted(KIMI_CRED_DIR.glob("coros-*-client.json"))
    tokens = sorted(KIMI_CRED_DIR.glob("coros-*-tokens.json"))
    if not clients or not tokens:
        raise SystemExit(f"在 {KIMI_CRED_DIR} 没找到 coros 的 kimi-code 凭据，"
                         "请先在 kimi-code 里 /mcp-config login coros，或用 --login 重新授权")
    client = json.loads(clients[-1].read_text(encoding="utf-8"))
    token = json.loads(tokens[-1].read_text(encoding="utf-8"))
    keep = {"access_token", "token_type", "expires_in", "scope", "refresh_token"}

    STORE_DIR.mkdir(parents=True, exist_ok=True)
    CLIENT_PATH.write_text(
        OAuthClientInformationFull.model_validate(client).model_dump_json(exclude_none=True),
        encoding="utf-8")
    TOKEN_PATH.write_text(
        OAuthToken.model_validate({k: v for k, v in token.items() if k in keep})
        .model_dump_json(exclude_none=True),
        encoding="utf-8")
    print(f"已从 {clients[-1].name} / {tokens[-1].name} 导入 OAuth client 与 token（未显示内容）。")
    print(f"下一步：uv run {Path(__file__).resolve()} --selftest")


def main():
    ap = argparse.ArgumentParser(description="高驰 MCP schema 修复代理")
    ap.add_argument("--selftest", action="store_true", help="连通性与 schema 修复自检")
    ap.add_argument("--login", action="store_true", help="浏览器交互式重新授权")
    ap.add_argument("--import-kimi-tokens", action="store_true",
                    help="从 kimi-code 凭据库导入 coros OAuth token")
    args = ap.parse_args()

    if args.import_kimi_tokens:
        import_kimi_tokens()
    elif args.selftest:
        asyncio.run(selftest())
    elif args.login:
        asyncio.run(login())
    else:
        try:
            asyncio.run(serve_stdio())
        except Exception as e:
            log.exception("stdio 服务启动失败")
            print(f"coros 代理启动失败: {e}", file=sys.stderr, flush=True)
            sys.exit(1)


if __name__ == "__main__":
    main()
