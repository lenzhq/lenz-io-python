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

from pathlib import Path

import typer

from lenz_io import Lenz

from .client import build_client
from .context import CLIState
from .errors import CLIError
from .mcp_config import (
    CLIENT_CHOICES,
    CLIENT_LABELS,
    CLIENT_MANUAL_STEPS,
    CLIENT_RESTART_NOTES,
    CODEX_TABLE,
    CONSOLE_URL,
    KEY_ENV_VAR,
    KEY_PLACEHOLDERS,
    MANUAL_CLIENTS,
    SETUP_URL,
    ConfigUnreadable,
    DuplicateCodexTable,
    build_codex_block,
    config_path_for,
    credential_for,
    merge_config,
    merge_toml_config,
    read_existing,
    write_config,
    write_text_config,
)
from .render import Output


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
    write_key: bool = typer.Option(
        False,
        "--write-key",
        help=(
            f"Write the key itself into the config. Off by default: project configs "
            f"are commonly committed, so they get a ${KEY_ENV_VAR} reference instead."
        ),
    ),
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

    # Clients with no config file: print the route and stop. Nothing to write,
    # nothing to verify against — the connector flow runs its own sign-in.
    if client_name in MANUAL_CLIENTS:
        if out.json_mode:
            out.emit_json(
                {
                    "status": "manual",
                    "client": client_name,
                    "config_file": None,
                    "instructions": CLIENT_MANUAL_STEPS[client_name],
                }
            )
        else:
            out.console.print(CLIENT_MANUAL_STEPS[client_name])
        raise SystemExit(0)

    # --print needs no key: its whole purpose is handing someone a config to
    # paste and fill in themselves, including for clients we don't support.
    # What it prints must be what a write for the SAME client would produce,
    # or it stops being a preview.
    if print_only:
        api_key = state.api_key.strip()
        if client_name == "codex":
            out.console.print(build_codex_block(api_key, write_key=write_key))
            raise SystemExit(0)
        if api_key:
            credential, _ = credential_for(client_name, api_key, write_key=write_key)
        else:
            credential = KEY_PLACEHOLDERS.get(client_name) or f"${{{KEY_ENV_VAR}}}"
        out.emit_json(merge_config(None, credential))
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

    if client_name == "codex":
        # TOML merges as TEXT — see mcp_config.merge_toml_config.
        try:
            merged = merge_toml_config(
                path.read_text(encoding="utf-8") if path.exists() else "",
                build_codex_block(state.api_key, write_key=write_key),
            )
        except DuplicateCodexTable:
            raise CLIError(
                f"{path} already has a {CODEX_TABLE} entry.",
                code="duplicate_codex_table",
                fix=(
                    "Edit or remove it and re-run — a second copy is a duplicate-table "
                    "error and would stop the whole file parsing."
                ),
            ) from None
        write_text_config(path, merged)
        is_placeholder = not write_key
    else:
        try:
            existing = read_existing(path)
        except ConfigUnreadable as exc:
            # Refuse rather than clobber — see mcp_config.read_existing.
            raise CLIError(
                f"{path} exists but is not valid JSON ({exc}).",
                code="config_unreadable",
                fix="Fix or move that file, then re-run — refusing to overwrite a config we can't read.",
            ) from None

        credential, is_placeholder = credential_for(client_name, state.api_key, write_key=write_key)
        write_config(path, merge_config(existing, credential))

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
                # So a script can tell whether the config is self-contained or
                # still needs the variable exported.
                "key_in_config": not is_placeholder,
                "key_env_var": KEY_ENV_VAR if is_placeholder else None,
            }
        )
        return

    render_success(
        out,
        client_name=client_name,
        path=path,
        verified=verified,
        is_placeholder=is_placeholder,
        api_key=state.api_key,
    )


def render_success(
    out: Output,
    *,
    client_name: str,
    path: Path,
    verified: str,
    is_placeholder: bool,
    api_key: str,
) -> None:
    """The human-readable tail of a successful init.

    A separate function because ``Output.json_mode`` is ``json_mode or not
    sys.stdout.isatty()``, so under CliRunner it is always JSON and this branch
    is unreachable through ``runner.invoke``. Same shape the render tests in
    ``tests/test_cli.py`` use.
    """
    out.console.print(f"Wrote Lenz MCP server to [bold]{path}[/bold]")
    if verified:
        out.console.print(f"[green]Key verified[/green] — {verified}.")
    if is_placeholder:
        # Without this the run reads as finished — "wrote the config, key
        # verified" — while the client still has nothing to authenticate with.
        out.console.print(
            f"\nThat config references ${KEY_ENV_VAR} instead of storing your key, because "
            f"{path.name} lives in your project and is commonly committed."
        )
        out.console.print("Export the key where your client will see it:\n")
        out.console.print(f"  export {KEY_ENV_VAR}={api_key}\n")
        out.console.print(
            "Add that to your shell profile to make it stick, or re-run with "
            "--write-key to put the key in the file instead.\n"
        )
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


__all__ = ["init", "render_success"]
