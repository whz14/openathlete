import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_json(relative_path):
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def test_plugin_manifests_and_shared_assets():
    marketplace = load_json(".agents/plugins/marketplace.json")
    assert marketplace["name"] == "openathlete"
    assert marketplace["plugins"] == [{
        "name": "openathlete", "source": {"source": "local", "path": "./"},
    }]

    manifest = load_json(".codex-plugin/plugin.json")
    assert manifest["name"] == "openathlete"
    assert manifest["version"] == "0.1.0"
    assert manifest["description"]
    assert manifest["skills"] == "./skills/"
    assert (ROOT / manifest["skills"]).is_dir()
    assert manifest["mcpServers"] == "./.codex-plugin/mcp.json"
    assert (ROOT / manifest["mcpServers"]).is_file()
    assert manifest["interface"]["displayName"]
    assert manifest["interface"]["shortDescription"]
    assert "systemPromptPath" not in manifest
    assert "sessionStart" not in manifest

    kimi = load_json("kimi.plugin.json")
    assert (ROOT / kimi["systemPromptPath"]).is_file()
    assert (ROOT / "coros_tools.json").is_file()
    skill = (ROOT / "skills/openathlete/SKILL.md").read_text(encoding="utf-8")
    assert "~/.kimi-code/plugins/managed" not in skill
    assert "<插件根目录>/coach-core.md" in skill


def test_mcp_manifest_parity_and_startup_requirements():
    codex = load_json(".codex-plugin/mcp.json")["mcpServers"]
    kimi = load_json("kimi.plugin.json")["mcpServers"]
    assert set(codex) == set(kimi) == {"openathlete", "coros"}
    for name, codex_server in codex.items():
        kimi_server = kimi[name]
        assert codex_server["command"] == kimi_server["command"]
        assert codex_server["args"] == kimi_server["args"]
        assert (ROOT / codex_server["cwd"]).resolve() == ROOT
        assert (ROOT / codex_server["cwd"]).resolve() == (
            ROOT / kimi_server["cwd"]
        ).resolve()
    assert codex["openathlete"]["required"] is True
    assert codex["openathlete"]["startup_timeout_sec"] == 120
    assert "required" not in codex["coros"]
    generic = load_json(".mcp.json")["mcpServers"]
    assert all("required" not in server for server in generic.values())
