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
import platform
import tempfile
from pathlib import Path
from typing import Any

MCP_SERVER_URL = "https://lenz.io/mcp"
CONSOLE_URL = "https://lenz.io/api-integration"
SETUP_URL = "https://lenz.io/setup"

CLIENT_CHOICES = ("claude-code", "claude-desktop", "cursor")

CLIENT_LABELS = {
    "claude-code": "Claude Code",
    "claude-desktop": "Claude Desktop",
    "cursor": "Cursor",
}

# Printed after a successful write — every one of these clients reads its MCP
# config at launch only, so "it didn't work" is nearly always "didn't restart".
CLIENT_RESTART_NOTES = {
    "claude-code": "Restart your Claude Code session so the server loads.",
    "claude-desktop": "Quit and reopen Claude Desktop — it only reads the config at launch.",
    "cursor": "Reload the Cursor window so the server loads.",
}


def build_server_config(api_key: str) -> dict[str, Any]:
    """The Lenz entry for an ``mcpServers`` map."""
    return {
        "type": "http",
        "url": MCP_SERVER_URL,
        "headers": {"Authorization": f"Bearer {api_key}"},
    }


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
    if client == "claude-desktop":
        system = platform.system()
        if system == "Darwin":
            return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        if system == "Windows":
            appdata = os.environ.get("APPDATA")
            return Path(appdata) / "Claude" / "claude_desktop_config.json" if appdata else None
        # Linux builds are unofficial but exist, and follow XDG.
        xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        return Path(xdg) / "Claude" / "claude_desktop_config.json"
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
    one filesystem, where ``os.replace`` is atomic.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".lenz-mcp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


__all__ = [
    "CLIENT_CHOICES",
    "CLIENT_LABELS",
    "CLIENT_RESTART_NOTES",
    "CONSOLE_URL",
    "MCP_SERVER_URL",
    "SETUP_URL",
    "ConfigUnreadable",
    "build_server_config",
    "config_path_for",
    "merge_config",
    "read_existing",
    "write_config",
]
