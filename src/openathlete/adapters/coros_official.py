"""COROS 官方远程 MCP 课表写 client；复用既有 OAuth 文件，不经过本地代理。"""
import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path

from ..home import openathlete_home

UPSTREAM_URL = "https://mcpcn.coros.com/mcp"
REDIRECT_URI = "http://127.0.0.1:14327/callback"
OAUTH_SCOPE = "openid mcp.tools offline_access"


class CorosOfficialError(RuntimeError):
    def __init__(self, message, code=None, outcome_unknown=False, explicit_rejection=False):
        super().__init__(message)
        self.code = code
        self.outcome_unknown = outcome_unknown
        self.explicit_rejection = explicit_rejection


class CorosOfficialToolError(CorosOfficialError):
    def __init__(self, message="COROS 官方拒绝了课表创建"):
        super().__init__(message, code="WRITE_FAILED", explicit_rejection=True)


def _read_optional_text(path):
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _write_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class FileTokenStorage:
    """与 coros_mcp_proxy.py 相同的 OAuthClientProvider 文件存储格式。"""

    def __init__(self, home=None):
        home = Path(home) if home is not None else openathlete_home()
        self.store_dir = home / "coros-mcp-proxy"
        self.client_path = self.store_dir / "oauth-client.json"
        self.token_path = self.store_dir / "oauth-tokens.json"

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        try:
            content = await asyncio.to_thread(_read_optional_text, self.token_path)
            if content is None:
                return None
            return OAuthToken.model_validate_json(content)
        except Exception:
            return None

    async def set_tokens(self, tokens):
        content = tokens.model_dump_json(exclude_none=True)
        await asyncio.to_thread(_write_text, self.token_path, content)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        try:
            content = await asyncio.to_thread(_read_optional_text, self.client_path)
            if content is None:
                return None
            return OAuthClientInformationFull.model_validate_json(content)
        except Exception:
            return None

    async def set_client_info(self, client_info):
        content = client_info.model_dump_json(exclude_none=True)
        await asyncio.to_thread(_write_text, self.client_path, content)


def _make_oauth(home):
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    metadata = OAuthClientMetadata(
        client_name="openathlete",
        redirect_uris=[REDIRECT_URI],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        scope=OAUTH_SCOPE,
    )

    async def refuse(_url):
        raise CorosOfficialError(
            "COROS 官方 OAuth 需要重新授权；请运行 coros_mcp_proxy.py --login",
            code="AUTH_REQUIRED")

    async def no_callback():
        raise CorosOfficialError("COROS 官方 OAuth 需要交互授权", code="AUTH_REQUIRED")

    return OAuthClientProvider(
        server_url=UPSTREAM_URL,
        client_metadata=metadata,
        storage=FileTokenStorage(home),
        redirect_handler=refuse,
        callback_handler=no_callback,
    )


async def _connect(home):
    """返回 (exit_stack, 已初始化 session)；不列举工具或转发 schema。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    stack = AsyncExitStack()
    try:
        http = await stack.enter_async_context(
            create_mcp_http_client(auth=_make_oauth(home)))
        read, write = await stack.enter_async_context(
            streamable_http_client(UPSTREAM_URL, http_client=http))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return stack, session
    except Exception:
        await stack.aclose()
        raise


def _content_texts(result):
    texts = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            texts.append(text)
    return texts


def _result_data(result):
    is_error = result.get("isError") if isinstance(result, dict) else getattr(
        result, "is_error", False)
    if is_error:
        raise CorosOfficialToolError()
    if isinstance(result, dict):
        structured = result.get("structuredContent", result)
    else:
        structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    texts = _content_texts(result)
    for text in texts:
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {"content": texts}


async def create_scheduled_workout(home, course):
    """调用一次官方 createScheduledWorkout；course 含 date 与 course 两个参数。"""
    if not isinstance(course, dict) or not isinstance(course.get("date"), str) \
            or not isinstance(course.get("course"), dict):
        raise ValueError("course 必须包含官方 createScheduledWorkout 的 date 与 course")
    try:
        stack, session = await _connect(home)
    except CorosOfficialError:
        raise
    except Exception as exc:
        raise CorosOfficialError(
            "无法连接 COROS 官方 MCP", code="UPSTREAM_UNAVAILABLE") from exc
    async with stack:
        try:
            result = await session.call_tool("createScheduledWorkout", course)
        except Exception as exc:
            raise CorosOfficialError(
                "COROS 创建请求已发出，但响应结果不明", outcome_unknown=True) from exc
    return _result_data(result)
