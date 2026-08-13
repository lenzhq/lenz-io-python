"""``lenz init`` — wire Lenz into an MCP client.

The odd one out among the CLI's verbs: every other command CALLS the API,
this one writes a config file. It lives here rather than in
:mod:`.commands` for the same reason ``verify`` does — it owns a small
workflow (resolve path → merge → write → verify) instead of being
"call one SDK method and render".

Parity note: this is the same command as ``npx lenz-io init`` in the Node
SDK — same server block, same config locations, same merge-don't-overwrite
rule. A developer who set one machine up with one SDK and another with the
other must not get two different results. Change them together.
"""

from __future__ import annotations

import typer

from lenz_io import Lenz

from .client import build_client
from .context import CLIState
from .errors import CLIError
from .mcp_config import (
    CLIENT_CHOICES,
    CLIENT_LABELS,
    CLIENT_RESTART_NOTES,
    CONSOLE_URL,
    SETUP_URL,
    ConfigUnreadable,
    config_path_for,
    merge_config,
    read_existing,
    write_config,
)


def init(
    ctx: typer.Context,
    client_name: str = typer.Option(
        "claude-code",
        "--client",
        "-c",
        help=f"Which MCP client to configure ({', '.join(CLIENT_CHOICES)}).",
    ),
    print_only: bool = typer.Option(False, "--print", help="Print the config JSON and exit, writing nothing."),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip the authenticated check."),
) -> None:
    """Write the Lenz MCP server into an AI client's config, then check the key works.

    Unlike the other commands, this configures rather than calls: it wires Lenz
    into Claude Code, Claude Desktop or Cursor so they can fact-check inside a
    conversation.
    """
    state: CLIState = ctx.obj
    out = state.output

    if client_name not in CLIENT_CHOICES:
        raise CLIError(
            f"Unknown client {client_name!r}.",
            code="unknown_client",
            fix=f"Choose one of: {', '.join(CLIENT_CHOICES)}.",
            exit_code=2,
        )

    # --print needs no key: its whole purpose is handing someone a config to
    # paste and fill in themselves, including for clients we don't support.
    if print_only:
        key = state.api_key.strip() or "${LENZ_API_KEY}"
        out.emit_json(merge_config(None, key))
        raise SystemExit(0)

    if state.key_source == "none":
        raise CLIError(
            "No API key.",
            code="no_api_key",
            fix=f"Run `lenz login`, set LENZ_API_KEY, or create one at {CONSOLE_URL}",
            exit_code=2,
        )

    path = config_path_for(client_name)
    if path is None:
        raise CLIError(
            f"Can't locate {CLIENT_LABELS[client_name]}'s config on this platform.",
            code="unsupported_platform",
            fix="Run `lenz init --print` and paste the JSON in yourself.",
        )

    try:
        existing = read_existing(path)
    except ConfigUnreadable as exc:
        # Refuse rather than clobber — see mcp_config.read_existing.
        raise CLIError(
            f"{path} exists but is not valid JSON ({exc}).",
            code="config_unreadable",
            fix="Fix or move that file, then re-run — refusing to overwrite a config we can't read.",
        ) from None

    write_config(path, merge_config(existing, state.api_key))

    verified = ""
    if not no_verify:
        verified = _verify_key(state)

    if out.json_mode:
        out.emit_json(
            {
                "status": "ok",
                "client": client_name,
                "config_file": str(path),
                "verified": bool(verified) if not no_verify else None,
            }
        )
        return

    out.console.print(f"Wrote Lenz MCP server to [bold]{path}[/bold]")
    if verified:
        out.console.print(f"[green]Key verified[/green] — {verified}.")
    out.console.print(CLIENT_RESTART_NOTES[client_name])
    out.console.print(f"More setup notes: {SETUP_URL}")


def _verify_key(state: CLIState) -> str:
    """One authenticated call, so the user learns now whether the key works.

    A failure here is NOT a failed init: the config is already written and
    correct, and only the key is in doubt. Saying which of the two broke stops
    people from re-running init at a problem it cannot fix.
    """
    out = state.output
    client: Lenz = build_client(api_key=state.api_key, base_url=state.base_url)
    try:
        with out.working("Checking the key…"):
            usage = client.usage()
    except Exception as exc:
        raise CLIError(
            f"Config written, but the key did not authenticate: {exc}",
            code="key_unverified",
            fix=f"Check it at {CONSOLE_URL}, then edit the config file.",
        ) from None
    finally:
        client.close()

    remaining = getattr(getattr(usage, "verify", None), "remaining", None)
    return f"{remaining} verify calls remaining" if isinstance(remaining, int) else "key accepted"


__all__ = ["init"]
