"""``lenz init`` — MCP config merging, path resolution, and the write path.

The merge tests carry the most weight. A developer's MCP config routinely
holds several servers, and a setup command that replaced the file would be
actively destructive on exactly the machines it exists to help.

Kept parallel to ``test/cli.test.ts`` in the Node SDK: same cases, same
expectations, because the two commands are a stated parity pair.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from lenz_io.cli import _run, init_cmd, normalize_argv
from lenz_io.cli import config as cfg
from lenz_io.cli import mcp_config as mcp
from lenz_io.cli.app import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.delenv("LENZ_API_KEY", raising=False)
    monkeypatch.delenv("LENZ_BASE_URL", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(cfg, "config_path", lambda: tmp_path / "config.json")


class _FakeUsage:
    class verify:
        remaining = 42


class _FakeClient:
    def __init__(self, raises=None):
        self._raises = raises

    def usage(self):
        if self._raises:
            raise self._raises
        return _FakeUsage()

    def close(self):
        pass


def _patch_client(monkeypatch, fake):
    # init builds its client through .client.build_client, imported into the
    # module — patch the name init_cmd actually calls.
    monkeypatch.setattr(init_cmd, "build_client", lambda **kw: fake)
    monkeypatch.setattr(_run, "build_client", lambda **kw: fake)


# ── merge rules ─────────────────────────────────────────────────────────────
def test_build_server_config_points_at_the_remote_server():
    cfg_block = mcp.build_server_config("lenz_abc")
    assert cfg_block["type"] == "http"
    assert cfg_block["url"] == "https://lenz.io/mcp"
    assert cfg_block["headers"]["Authorization"] == "Bearer lenz_abc"


def test_merge_creates_the_block_when_there_is_no_config():
    merged = mcp.merge_config(None, "lenz_abc")
    assert list(merged["mcpServers"]) == ["lenz"]


def test_merge_preserves_other_servers():
    existing = {
        "mcpServers": {
            "github": {"type": "http", "url": "https://example.com/mcp"},
            "filesystem": {"command": "npx", "args": ["-y", "server-filesystem"]},
        }
    }

    merged = mcp.merge_config(existing, "lenz_abc")

    assert sorted(merged["mcpServers"]) == ["filesystem", "github", "lenz"]
    assert merged["mcpServers"]["github"]["url"] == "https://example.com/mcp"


def test_merge_preserves_unrelated_top_level_keys():
    assert mcp.merge_config({"theme": "dark", "mcpServers": {}}, "k")["theme"] == "dark"


def test_merge_replaces_a_previous_lenz_entry():
    existing = {"mcpServers": {"lenz": {"type": "http", "url": "old", "headers": {}}}}

    merged = mcp.merge_config(existing, "lenz_new")

    assert merged["mcpServers"]["lenz"]["url"] == "https://lenz.io/mcp"
    assert merged["mcpServers"]["lenz"]["headers"]["Authorization"] == "Bearer lenz_new"


def test_merge_survives_a_malformed_mcpservers_value():
    assert "lenz" in mcp.merge_config({"mcpServers": "nonsense"}, "k")["mcpServers"]


# ── path resolution ─────────────────────────────────────────────────────────
def test_project_scoped_paths(tmp_path):
    assert mcp.config_path_for("claude-code", cwd=tmp_path) == tmp_path / ".mcp.json"
    assert mcp.config_path_for("cursor", cwd=tmp_path) == tmp_path / ".cursor" / "mcp.json"


def test_claude_desktop_path_is_global(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    path = mcp.config_path_for("claude-desktop")
    assert path is not None
    assert path.name == "claude_desktop_config.json"
    assert "Application Support" in str(path)


def test_unknown_client_has_no_path():
    assert mcp.config_path_for("emacs") is None


# ── read / write ────────────────────────────────────────────────────────────
def test_unreadable_config_raises_rather_than_guessing(tmp_path):
    path = tmp_path / ".mcp.json"
    path.write_text("{ this is not json")

    with pytest.raises(mcp.ConfigUnreadable):
        mcp.read_existing(path)


def test_empty_file_reads_as_no_config(tmp_path):
    path = tmp_path / ".mcp.json"
    path.write_text("   \n")
    assert mcp.read_existing(path) is None


def test_write_is_atomic_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "nested" / "mcp.json"

    mcp.write_config(path, {"a": 1})

    assert json.loads(path.read_text())["a"] == 1
    assert [p.name for p in path.parent.iterdir()] == ["mcp.json"]


# ── the command ─────────────────────────────────────────────────────────────
def test_init_writes_the_config_and_verifies(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg.save_api_key("lenz_abc")
    _patch_client(monkeypatch, _FakeClient())

    result = runner.invoke(app, normalize_argv(["init"]))

    assert result.exit_code == 0, result.output
    written = json.loads((tmp_path / ".mcp.json").read_text())
    assert written["mcpServers"]["lenz"]["headers"]["Authorization"] == "Bearer lenz_abc"


def test_init_cursor_creates_the_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg.save_api_key("lenz_abc")
    _patch_client(monkeypatch, _FakeClient())

    result = runner.invoke(app, normalize_argv(["init", "--client", "cursor", "--no-verify"]))

    assert result.exit_code == 0, result.output
    written = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    assert written["mcpServers"]["lenz"]["url"] == "https://lenz.io/mcp"


def test_init_merges_rather_than_replacing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"github": {"type": "http", "url": "https://example.com"}}})
    )
    cfg.save_api_key("lenz_abc")
    _patch_client(monkeypatch, _FakeClient())

    runner.invoke(app, normalize_argv(["init", "--no-verify"]))

    written = json.loads((tmp_path / ".mcp.json").read_text())
    assert written["mcpServers"]["github"]["url"] == "https://example.com"
    assert "lenz" in written["mcpServers"]


def test_init_refuses_to_overwrite_an_unparseable_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".mcp.json").write_text("{ this is not json")
    cfg.save_api_key("lenz_abc")
    _patch_client(monkeypatch, _FakeClient())

    result = runner.invoke(app, normalize_argv(["init", "--no-verify"]))

    assert result.exit_code != 0
    # Left byte-identical — servers configured by hand are not ours to discard.
    assert (tmp_path / ".mcp.json").read_text() == "{ this is not json"


def test_init_print_writes_nothing_and_needs_no_key(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, normalize_argv(["init", "--print"]))

    assert result.exit_code == 0, result.output
    printed = json.loads(result.stdout)
    assert printed["mcpServers"]["lenz"]["headers"]["Authorization"] == "Bearer ${LENZ_API_KEY}"
    assert not (tmp_path / ".mcp.json").exists()


def test_init_without_a_key_errors_rather_than_writing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, normalize_argv(["init", "--no-verify"]))

    assert result.exit_code != 0
    assert not (tmp_path / ".mcp.json").exists()


def test_a_bad_key_reports_the_key_not_the_config(monkeypatch, tmp_path):
    """The config is already written and correct; only the key is in doubt.
    Saying which broke stops people re-running init at a problem it can't fix."""
    monkeypatch.chdir(tmp_path)
    cfg.save_api_key("lenz_bad")
    _patch_client(monkeypatch, _FakeClient(raises=RuntimeError("401 unauthorized")))

    result = runner.invoke(app, normalize_argv(["init"]))

    assert result.exit_code != 0
    assert (tmp_path / ".mcp.json").exists(), "the config write must not be rolled back"


def test_unknown_client_is_rejected(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg.save_api_key("lenz_abc")

    result = runner.invoke(app, normalize_argv(["init", "--client", "emacs"]))

    assert result.exit_code != 0
    assert not (tmp_path / ".mcp.json").exists()


def test_json_mode_emits_the_machine_contract(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg.save_api_key("lenz_abc")
    _patch_client(monkeypatch, _FakeClient())

    result = runner.invoke(app, normalize_argv(["--json", "init", "--no-verify"]))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["client"] == "claude-code"
    assert payload["config_file"].endswith(".mcp.json")


def test_parity_with_the_node_sdk_server_block():
    """Same block both SDKs write. They are a stated parity pair — a developer
    who set one machine up with each must not get two different results."""
    assert mcp.build_server_config("k") == {
        "type": "http",
        "url": "https://lenz.io/mcp",
        "headers": {"Authorization": "Bearer k"},
    }
