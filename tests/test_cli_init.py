"""``lenz init`` — MCP config merging, path resolution, and the write path.

The merge tests carry the most weight. A developer's MCP config routinely
holds several servers, and a setup command that replaced the file would be
actively destructive on exactly the machines it exists to help.

Kept parallel to ``test/cli.test.ts`` in the Node SDK: same cases, same
expectations, because the two commands are a stated parity pair.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lenz_io.cli import _run, init_cmd, normalize_argv
from lenz_io.cli import config as cfg
from lenz_io.cli import mcp_config as mcp
from lenz_io.cli.app import app
from lenz_io.cli.render import Output

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


def test_claude_desktop_has_no_config_file():
    """claude_desktop_config.json is documented for local STDIO servers only.
    A remote streamable-HTTP server is added through Settings → Connectors, so
    writing that file put a live key somewhere nothing reads it."""
    assert mcp.config_path_for("claude-desktop") is None
    assert "claude-desktop" in mcp.MANUAL_CLIENTS


def test_codex_is_project_scoped_toml(tmp_path):
    assert mcp.config_path_for("codex", cwd=tmp_path) == tmp_path / ".codex" / "config.toml"


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
    # What goes in the Authorization header has its own tests below — see
    # test_the_key_stays_out_of_a_project_config.
    assert written["mcpServers"]["lenz"]["url"] == "https://lenz.io/mcp"


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


def test_parity_pins_the_shared_constants():
    """The block above was the only thing pinned, and it is the only thing that
    never drifted. SETUP_URL meanwhile sat at the pre-rename /welcome/setup in
    the Node SDK and printed a 404 as the last line of every successful run.
    ``test/cli.test.ts`` asserts this same table."""
    assert mcp.MCP_SERVER_URL == "https://lenz.io/mcp"
    assert mcp.CONSOLE_URL == "https://lenz.io/api-integration"
    assert mcp.SETUP_URL == "https://lenz.io/setup"
    assert mcp.KEY_ENV_VAR == "LENZ_API_KEY"


def test_parity_pins_the_per_client_placeholder_syntax():
    """Not interchangeable. Cursor treats a bare ${VAR} as literal text and
    sends it as the bearer token.

    Only the JSON clients are here: Codex names the variable in a field of its
    own, and Claude Desktop takes no config file at all."""
    assert mcp.KEY_PLACEHOLDERS["claude-code"] == "${LENZ_API_KEY}"
    assert mcp.KEY_PLACEHOLDERS["cursor"] == "${env:LENZ_API_KEY}"
    assert sorted(mcp.KEY_PLACEHOLDERS) == ["claude-code", "cursor"]


# ── where the key ends up ───────────────────────────────────────────────────
def test_the_key_stays_out_of_a_project_config(monkeypatch, tmp_path):
    """`.mcp.json` is a file its own docs tell teams to commit. A setup command
    whose happy path writes a live credential there is handing over a leak."""
    monkeypatch.chdir(tmp_path)
    cfg.save_api_key("lenz_secret")
    _patch_client(monkeypatch, _FakeClient())

    runner.invoke(app, normalize_argv(["init", "--no-verify"]))

    raw = (tmp_path / ".mcp.json").read_text()
    assert json.loads(raw)["mcpServers"]["lenz"]["headers"]["Authorization"] == "Bearer ${LENZ_API_KEY}"
    assert "lenz_secret" not in raw


def test_cursor_gets_its_own_placeholder_syntax(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg.save_api_key("lenz_secret")
    _patch_client(monkeypatch, _FakeClient())

    runner.invoke(app, normalize_argv(["init", "--client", "cursor", "--no-verify"]))

    written = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    assert written["mcpServers"]["lenz"]["headers"]["Authorization"] == "Bearer ${env:LENZ_API_KEY}"


def test_the_export_line_is_printed_with_the_actual_key(capsys):
    """Otherwise the run reads as finished while the client still has nothing
    to authenticate with.

    Rendered directly rather than through the runner: Output.json_mode is
    ``json_mode or not sys.stdout.isatty()``, so under CliRunner this branch
    never executes. Same approach as the render tests in test_cli.py.
    """
    out = Output(json_mode=False, no_color=True)
    out.json_mode = False

    init_cmd.render_success(
        out,
        client_name="claude-code",
        path=Path("/proj/.mcp.json"),
        verified="42 verify calls remaining",
        is_placeholder=True,
        api_key="lenz_secret",
    )

    printed = capsys.readouterr().out
    assert "export LENZ_API_KEY=lenz_secret" in printed
    assert "commonly committed" in printed


def test_no_export_line_when_the_key_is_in_the_file(capsys):
    """--write-key makes the config self-contained; telling them to export a
    variable nothing reads would just be noise."""
    out = Output(json_mode=False, no_color=True)
    out.json_mode = False

    init_cmd.render_success(
        out,
        client_name="claude-code",
        path=Path("/proj/.mcp.json"),
        verified="",
        is_placeholder=False,
        api_key="lenz_secret",
    )

    assert "export LENZ_API_KEY" not in capsys.readouterr().out


def test_write_key_puts_the_key_in_the_file(monkeypatch, tmp_path):
    """The opt-out, for a private checkout."""
    monkeypatch.chdir(tmp_path)
    cfg.save_api_key("lenz_secret")
    _patch_client(monkeypatch, _FakeClient())

    runner.invoke(app, normalize_argv(["init", "--write-key", "--no-verify"]))

    written = json.loads((tmp_path / ".mcp.json").read_text())
    assert written["mcpServers"]["lenz"]["headers"]["Authorization"] == "Bearer lenz_secret"


def test_claude_desktop_gets_the_literal_key():
    """Launched from the desktop rather than a shell, so it never inherits an
    exported variable — a placeholder there is simply broken."""
    value, is_placeholder = mcp.credential_for("claude-desktop", "lenz_secret")

    assert value == "lenz_secret"
    assert is_placeholder is False


def test_print_previews_exactly_what_a_write_would_produce(monkeypatch, tmp_path):
    """A preview that differs from the write is worse than no preview."""
    monkeypatch.chdir(tmp_path)
    cfg.save_api_key("lenz_secret")

    result = runner.invoke(app, normalize_argv(["init", "--client", "cursor", "--print"]))

    printed = json.loads(result.stdout)
    assert printed["mcpServers"]["lenz"]["headers"]["Authorization"] == "Bearer ${env:LENZ_API_KEY}"


def test_json_mode_says_whether_the_key_is_in_the_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg.save_api_key("lenz_secret")
    _patch_client(monkeypatch, _FakeClient())

    result = runner.invoke(app, normalize_argv(["--json", "init", "--no-verify"]))

    payload = json.loads(result.stdout)
    assert payload["key_in_config"] is False
    assert payload["key_env_var"] == "LENZ_API_KEY"


def test_write_config_is_owner_only(tmp_path):
    """With --write-key this file holds a live credential. mkstemp gives 0600
    and os.replace preserves it; a refactor to path.write_text would quietly
    widen it to 0644."""
    path = tmp_path / "mcp.json"

    mcp.write_config(path, {"a": 1})

    assert path.stat().st_mode & 0o777 == 0o600


# ── codex: TOML, merged as text ─────────────────────────────────────────────
def test_codex_append_preserves_everything_the_user_wrote(tmp_path):
    """Deliberately not a parse → mutate → re-serialize round trip: every TOML
    library drops comments and reflows formatting, and handing someone back a
    file that is equivalent but visibly not theirs is the same failure as
    clobbering it."""
    existing = '# my own notes\nmodel = "gpt-5"\n\n[mcp_servers.github]\nurl = "https://example.com/mcp"\n'

    merged = mcp.merge_toml_config(existing, mcp.build_codex_block("lenz_abc"))

    assert merged.startswith(existing.rstrip())
    assert "# my own notes" in merged
    assert "[mcp_servers.lenz]" in merged


def test_codex_refuses_a_second_lenz_table(tmp_path):
    """TOML rejects duplicate tables outright, so appending blindly would stop
    the WHOLE file parsing — every other server in it included."""
    with pytest.raises(mcp.DuplicateCodexTable):
        mcp.merge_toml_config('[mcp_servers.lenz]\nurl = "x"\n', mcp.build_codex_block("k"))


def test_codex_block_names_the_env_var_rather_than_the_key():
    block = mcp.build_codex_block("lenz_secret")

    assert 'bearer_token_env_var = "LENZ_API_KEY"' in block
    assert "lenz_secret" not in block


def test_codex_write_key_uses_http_headers():
    """The other documented Codex auth field."""
    block = mcp.build_codex_block("lenz_secret", write_key=True)

    assert 'http_headers = { "Authorization" = "Bearer lenz_secret" }' in block
    assert "bearer_token_env_var" not in block


def test_codex_init_writes_valid_toml_without_the_key(monkeypatch, tmp_path):
    import tomllib

    monkeypatch.chdir(tmp_path)
    cfg.save_api_key("lenz_secret")
    _patch_client(monkeypatch, _FakeClient())

    result = runner.invoke(app, normalize_argv(["init", "--client", "codex", "--no-verify"]))

    assert result.exit_code == 0, result.output
    raw = (tmp_path / ".codex" / "config.toml").read_text()
    assert "lenz_secret" not in raw
    parsed = tomllib.loads(raw)
    assert parsed["mcp_servers"]["lenz"]["url"] == "https://lenz.io/mcp"


def test_claude_desktop_init_writes_nothing_and_prints_the_flow(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg.save_api_key("lenz_secret")

    result = runner.invoke(app, normalize_argv(["init", "--client", "claude-desktop"]))

    assert result.exit_code == 0, result.output
    # No MCP config of any shape. (tmp_path also holds the CLI's own
    # config.json, written by the save_api_key above — not ours to assert on.)
    assert not (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / "claude_desktop_config.json").exists()
    # A flow that never takes a key must not echo one.
    assert "lenz_secret" not in result.output
    assert "Add custom connector" in result.output
