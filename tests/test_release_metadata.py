import json
from pathlib import Path

from openathlete import __version__

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
SERVER_NAME = "io.github.whz14/openathlete"


def _project():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _server():
    return json.loads((ROOT / "server.json").read_text(encoding="utf-8"))


def test_release_identity():
    project = _project()
    server = _server()
    package = server["packages"][0]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert server["name"] == SERVER_NAME
    assert server["repository"] == {
        "url": "https://github.com/whz14/openathlete",
        "source": "github",
    }
    assert project["name"] == package["identifier"] == "openathlete-mcp"
    assert package["registryType"] == "pypi"
    assert package["registryBaseUrl"] == "https://pypi.org"
    assert package["runtimeHint"] == "uvx"
    assert package["transport"] == {"type": "stdio"}
    assert f"<!-- mcp-name: {SERVER_NAME} -->" in readme


def test_release_versions_match():
    server = _server()

    assert _project()["version"] == __version__ == server["version"]
    assert server["packages"][0]["version"] == __version__
