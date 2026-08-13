"""Writing the Lenz MCP server block into an AI client's config file.

Pure, side-effect-light helpers so the merge rules can be tested without a
filesystem. The command that drives them lives in :mod:`.commands`.

The behaviour mirrors ``npx lenz-io init`` in the Node SDK — same server
block, same config locations, same merge-don't-overwrite rule — because a
developer who set one machine up with one SDK and another with the other must
not get two different results.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

MCP_SERVER_URL = "https://lenz.io/mcp"
CONSOLE_URL = "https://lenz.io/api-integration"
SETUP_URL = "https://lenz.io/setup"

# The environment variable the SDK already reads, and the one the placeholders
# below name. Shared with the Node SDK — see KEY_PLACEHOLDERS.
KEY_ENV_VAR = "LENZ_API_KEY"

CLAUDE_CONNECTORS_URL = "https://claude.ai/directory/connectors/lenz"

CLIENT_CHOICES = ("claude-code", "cursor", "codex", "claude-desktop")

CLIENT_LABELS = {
    "claude-code": "Claude Code",
    "claude-desktop": "Claude Desktop",
    "cursor": "Cursor",
    "codex": "Codex",
}

# Clients configured through their own interface rather than a config file.
#
# claude_desktop_config.json is documented for local STDIO servers only —
# every example in Anthropic's docs is command/args. A remote streamable-HTTP
# server like ours is added through Settings → Connectors → Add custom
# connector, which runs its own sign-in.
#
# We used to write a ``{"type": "http", "headers": {...}}`` entry into that
# file, which is not a documented shape for it — and Claude Desktop was the one
# client we handed the literal key to, so the likely outcome was a live
# credential sitting in a file nothing reads.
MANUAL_CLIENTS = frozenset({"claude-desktop"})

# The TOML table Codex keys its server on. A second one is a duplicate-table
# error that stops the whole file parsing.
CODEX_TABLE = "[mcp_servers.lenz]"

# How each client spells an environment-variable reference inside a config
# value — None when it cannot resolve one at all.
#
# The syntaxes are NOT interchangeable. Claude Code takes ``${VAR}``; Cursor
# takes ``${env:VAR}`` and treats a bare ``${VAR}`` as literal text, which
# reaches the server as a nonsense bearer token and 401s with no clue why.
#
# Only the JSON clients are here. Codex names the variable in a field of its
# own (``bearer_token_env_var``), and Claude Desktop takes no config file.
KEY_PLACEHOLDERS: dict[str, str | None] = {
    "claude-code": f"${{{KEY_ENV_VAR}}}",
    "cursor": f"${{env:{KEY_ENV_VAR}}}",
}

# (No separate "is project-scoped" set: a non-None placeholder above IS that
# fact. Two tables encoding the same thing is two tables that can disagree.)

# Printed after a successful write — every one of these clients reads its MCP
# config at launch only, so "it didn't work" is nearly always "didn't restart".
CLIENT_RESTART_NOTES = {
    "claude-code": "Restart your Claude Code session so the server loads.",
    "cursor": "Reload the Cursor window so the server loads.",
    "codex": "Restart the Codex session so the server loads.",
}

# Printed for MANUAL_CLIENTS instead of writing anything.
CLIENT_MANUAL_STEPS = {
    "claude-desktop": (
        f"{CLAUDE_CONNECTORS_URL} adds Lenz to your account in one click — no key to paste.\n"
        "\n"
        "Or add it by hand:\n"
        "  1. Claude Desktop → Settings → Connectors\n"
        '  2. "Add" → "Add custom connector"\n'
        f"  3. Paste {MCP_SERVER_URL} and complete the sign-in prompt\n"
        "\n"
        "Claude Desktop takes remote servers through that flow, not through\n"
        "claude_desktop_config.json — that file is for local stdio servers."
    ),
}


def build_server_config(api_key: str) -> dict[str, Any]:
    """The Lenz entry for an ``mcpServers`` map."""
    return {
        "type": "http",
        "url": MCP_SERVER_URL,
        "headers": {"Authorization": f"Bearer {api_key}"},
    }


def credential_for(client: str, api_key: str, *, write_key: bool = False) -> tuple[str, bool]:
    """Return ``(value_for_the_header, is_placeholder)``.

    Project-scoped configs get an environment-variable reference rather than
    the key, because ``.mcp.json`` is a file its own documentation tells teams
    to commit: "Check .mcp.json into version control so everyone on your team
    gets the same MCP tools and services." A setup command whose happy path
    writes a live credential into a tracked file is handing the user a leak.

    ``write_key`` is the opt-out, for a private checkout or a machine where
    exporting a variable is more friction than it is worth.
    """
    placeholder = KEY_PLACEHOLDERS.get(client)
    if placeholder and not write_key:
        return placeholder, True
    return api_key, False


def build_codex_block(api_key: str, *, write_key: bool = False) -> str:
    """Codex's server block. TOML, and no interpolation anywhere.

    ``bearer_token_env_var`` names an environment variable in a field of its
    own, which is what Claude Code and Cursor need ``${VAR}`` / ``${env:VAR}``
    string syntax for — so the default writes no credential at all.
    ``write_key`` uses ``http_headers`` instead, the other documented way to
    authenticate.
    """
    auth = (
        f'http_headers = {{ "Authorization" = "Bearer {api_key}" }}'
        if write_key
        else f'bearer_token_env_var = "{KEY_ENV_VAR}"'
    )
    return f'{CODEX_TABLE}\nurl = "{MCP_SERVER_URL}"\n{auth}\n'


class DuplicateCodexTable(Exception):
    """The config already declares ``[mcp_servers.lenz]``."""


def merge_toml_config(existing: str, block: str) -> str:
    """Append the Lenz table to an existing config.toml, as TEXT.

    Deliberately not a parse → mutate → re-serialize round trip. Every TOML
    library drops comments and reflows formatting, so a round trip hands the
    user back a file that is technically equivalent and visibly not theirs —
    the same objection as clobbering a config we could not read.

    Appending is always valid: TOML tables are order-independent. The one thing
    that is NOT safe is a second ``[mcp_servers.lenz]``, which is a
    duplicate-key error that stops the whole file parsing — every other server
    in it included — so that case raises.
    """
    if re.search(rf"^\s*{re.escape(CODEX_TABLE)}", existing, re.MULTILINE):
        raise DuplicateCodexTable(CODEX_TABLE)
    trimmed = existing.rstrip()
    return f"{trimmed}\n\n{block}" if trimmed else block


def merge_config(existing: Any, api_key: str) -> dict[str, Any]:
    """Merge the Lenz server into an existing MCP config.

    Merges rather than replaces, which is the whole point of this function:
    a developer's config routinely holds several servers, and a setup command
    that overwrote the file would be actively destructive on exactly the
    machines it exists to help. Only the ``lenz`` key is touched, and
    non-dict values anywhere in the path are treated as absent rather than
    crashing.
    """
    base: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    servers = base.get("mcpServers")
    servers = dict(servers) if isinstance(servers, dict) else {}
    servers["lenz"] = build_server_config(api_key)
    base["mcpServers"] = servers
    return base


def config_path_for(client: str, *, cwd: Path | None = None) -> Path | None:
    """Where ``client`` keeps its MCP config, or None on an unsupported platform.

    Claude Code and Cursor are project-scoped: writing into the working
    directory is what those tools expect, and it is the least surprising thing
    a one-shot command can do — it cannot silently change behaviour in every
    other project on the machine. Claude Desktop has no project concept, so
    its config is necessarily global.

    ``cwd`` is a parameter rather than a ``Path.cwd()`` call so the
    project-scoped clients are testable without chdir'ing the test process.
    """
    root = Path(cwd) if cwd is not None else Path.cwd()

    if client == "claude-code":
        return root / ".mcp.json"
    if client == "cursor":
        return root / ".cursor" / "mcp.json"
    if client == "codex":
        # TOML, and project-scoped for the same reason as the two above.
        # ``~/.codex/config.toml`` is the global equivalent; the success note
        # says so rather than writing there behind the user's back.
        return root / ".codex" / "config.toml"
    # claude-desktop included: it is configured through Settings → Connectors,
    # so it has no path. See MANUAL_CLIENTS.
    return None


class ConfigUnreadable(Exception):
    """The config file exists but is not JSON we can safely rewrite."""


def read_existing(path: Path) -> Any:
    """Parse an existing config, or return None when there isn't one.

    Raises :class:`ConfigUnreadable` rather than guessing. Overwriting a file
    we cannot parse could discard servers the user configured by hand, with no
    way to get them back — refusing is the only safe answer.
    """
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigUnreadable(str(exc)) from exc


def write_config(path: Path, data: Any) -> None:
    """Write ``data`` to ``path`` atomically.

    Temp file in the same directory + replace, so a crash mid-write cannot
    leave a truncated config behind — and same-directory keeps the replace on
    one filesystem, where ``os.replace`` is atomic. Staging in the system temp
    directory instead would raise ``OSError: [Errno 18] Invalid cross-device
    link`` anywhere /tmp is its own filesystem, which is most Linux distros and
    every container.

    ``mkstemp`` creates the file 0600 and ``os.replace`` preserves that mode.
    Load-bearing, not incidental: with ``--write-key`` this file holds a live
    credential, and a refactor to ``path.write_text`` would quietly widen it to
    0644. ``test_write_config_is_owner_only`` is the guard.
    """
    write_text_config(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def write_text_config(path: Path, text: str) -> None:
    """The atomic + 0600 write itself, for callers holding text already.

    The TOML path merges textually, so it cannot go through ``write_config``'s
    JSON serialization — but it must not lose the durability or the mode, which
    is why this is one function rather than two write paths.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".lenz-mcp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


__all__ = [
    "CLAUDE_CONNECTORS_URL",
    "CLIENT_CHOICES",
    "CLIENT_LABELS",
    "CLIENT_MANUAL_STEPS",
    "CLIENT_RESTART_NOTES",
    "CODEX_TABLE",
    "CONSOLE_URL",
    "KEY_ENV_VAR",
    "KEY_PLACEHOLDERS",
    "MANUAL_CLIENTS",
    "MCP_SERVER_URL",
    "SETUP_URL",
    "ConfigUnreadable",
    "DuplicateCodexTable",
    "build_codex_block",
    "build_server_config",
    "config_path_for",
    "credential_for",
    "merge_config",
    "merge_toml_config",
    "read_existing",
    "write_config",
    "write_text_config",
]
