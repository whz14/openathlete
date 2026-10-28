import asyncio
from types import SimpleNamespace

import pytest

from openathlete.adapters import coros_official
from openathlete.adapters.coros_official import (
    CorosOfficialError, CorosOfficialToolError, FileTokenStorage,
    create_scheduled_workout,
)


def official_payload():
    return {
        "date": "20261025",
        "course": {
            "sportType": 1,
            "courseName": "合成课表",
            "courseDescription": "只做 mock 测试",
            "sections": [{"sectionType": 2, "targetType": 2, "targetValue": 1200}],
        },
    }


class FakeStack:
    def __init__(self):
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True


class FakeSession:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.error:
            raise self.error
        return self.result


def mock_connect(monkeypatch, session, stack=None):
    stack = stack or FakeStack()

    async def connect(home):
        return stack, session

    monkeypatch.setattr(coros_official, "_connect", connect)
    return stack


def test_official_client_calls_one_tool_with_mocked_session(monkeypatch, tmp_path):
    session = FakeSession(SimpleNamespace(
        is_error=False, structured_content={"status": "created"}, content=[]))
    stack = mock_connect(monkeypatch, session)
    payload = official_payload()
    result = asyncio.run(create_scheduled_workout(tmp_path, payload))
    assert result == {"status": "created"}
    assert session.calls == [("createScheduledWorkout", payload)]
    assert stack.exited is True


def test_official_client_parses_json_text_without_tool_schema(monkeypatch, tmp_path):
    text = SimpleNamespace(text='{"created":true,"scheduled":true}')
    session = FakeSession(SimpleNamespace(
        is_error=False, structured_content=None, content=[text]))
    mock_connect(monkeypatch, session)
    result = asyncio.run(create_scheduled_workout(tmp_path, official_payload()))
    assert result == {"created": True, "scheduled": True}
    assert "tools" not in result and "inputSchema" not in result


def test_official_client_explicit_tool_error_is_not_success(monkeypatch, tmp_path):
    session = FakeSession(SimpleNamespace(
        is_error=True, structured_content={"error": "denied"}, content=[]))
    mock_connect(monkeypatch, session)
    with pytest.raises(CorosOfficialToolError):
        asyncio.run(create_scheduled_workout(tmp_path, official_payload()))
    assert len(session.calls) == 1


def test_official_client_marks_post_request_timeout_unknown(monkeypatch, tmp_path):
    session = FakeSession(error=TimeoutError("timeout"))
    mock_connect(monkeypatch, session)
    with pytest.raises(CorosOfficialError) as err:
        asyncio.run(create_scheduled_workout(tmp_path, official_payload()))
    assert err.value.outcome_unknown is True
    assert err.value.explicit_rejection is False
    assert len(session.calls) == 1


def test_official_client_connect_failure_does_not_call_tool(monkeypatch, tmp_path):
    async def failed_connect(home):
        raise ConnectionError("offline")

    monkeypatch.setattr(coros_official, "_connect", failed_connect)
    with pytest.raises(CorosOfficialError) as err:
        asyncio.run(create_scheduled_workout(tmp_path, official_payload()))
    assert err.value.code == "UPSTREAM_UNAVAILABLE"
    assert err.value.outcome_unknown is False


def test_official_client_validates_arguments_before_connect(monkeypatch, tmp_path):
    async def forbidden(home):
        raise AssertionError("must not connect")

    monkeypatch.setattr(coros_official, "_connect", forbidden)
    with pytest.raises(ValueError):
        asyncio.run(create_scheduled_workout(tmp_path, {"course": {}}))


def test_oauth_storage_reuses_proxy_directory_and_filenames(tmp_path):
    storage = FileTokenStorage(tmp_path)
    assert storage.store_dir == tmp_path / "coros-mcp-proxy"
    assert storage.client_path.name == "oauth-client.json"
    assert storage.token_path.name == "oauth-tokens.json"
