"""The thin verbs — each one resolves input, calls a single SDK method, and
renders. All error/exit handling lives in :func:`._run.execute`; ``verify``
(the only stateful lifecycle) lives in :mod:`.verify`.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

import typer

from lenz_io import Lenz
from lenz_io.errors import LenzError

from ._run import execute, read_text_arg
from .config import ENV_API_KEY, clear_api_key, config_path, mask_key, save_api_key
from .context import CLIState
from .errors import CLIError
from .render import render_ask, render_assess, render_config, render_extract


def extract(
    ctx: typer.Context,
    text: str = typer.Argument(None, help="Text to extract claims from ('-' or pipe = stdin)."),
) -> None:
    """Pull verifiable claims out of text. No credit charge (1000/day) — but needs a key."""
    state: CLIState = ctx.obj
    out = state.output

    def work(client: Lenz) -> None:
        payload = read_text_arg(text)
        with out.working("Extracting claims…"):
            result = client.extract(text=payload)
        render_extract(out, result)

    execute(state, needs_key=True, work=work)


def assess(
    ctx: typer.Context,
    claim: str = typer.Argument(None, help="Claim to assess ('-' or pipe = stdin)."),
) -> None:
    """Fast 3-model verdict (cheaper/quicker than `verify`). Needs a key."""
    state: CLIState = ctx.obj
    out = state.output

    def work(client: Lenz) -> None:
        payload = read_text_arg(claim)
        with out.working("Assessing… (~10s)"):
            result = client.assess(text=payload)
        render_assess(out, result)

    execute(state, needs_key=True, work=work)


def ask(
    ctx: typer.Context,
    verification_id: str = typer.Argument(..., help="The 8-char verification_id (from verify/assess output)."),
    question: str = typer.Argument(None, help="Your question ('-' or pipe = stdin)."),
) -> None:
    """Ask a single grounded question about a past verification. Needs a key."""
    state: CLIState = ctx.obj
    out = state.output

    def work(client: Lenz) -> None:
        message = read_text_arg(question)
        try:
            with out.working("Thinking…"):
                reply = client.ask.send(verification_id, message=message)
        except LenzError as exc:
            if exc.status_code == 404:
                raise CLIError(
                    f"No verification found for id {verification_id!r}. "
                    "A verification_id is the 8-character id printed by `lenz verify` "
                    "(or in an `assess` verification_url) — not a task_id.",
                    code="not_found",
                    status=404,
                ) from None
            raise
        render_ask(out, reply)

    execute(state, needs_key=True, work=work)


def _dashboard_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, "/api-integration", "", ""))


def login(ctx: typer.Context) -> None:
    """Store an API key locally. Paste it, or pass it via `--api-key` / `LENZ_API_KEY`.

    With no key, opens the web dashboard so you can create one (key creation
    stays on the web — the CLI never creates accounts).
    """
    state: CLIState = ctx.obj
    out = state.output
    url = _dashboard_url(state.base_url)

    key = state.api_key.strip() if state.key_source in ("flag", "env") else ""

    # json mode can't prompt — point the caller at the dashboard and stop.
    if not key and out.json_mode:
        out.emit_json({"status": "no_key", "dashboard": url})
        raise SystemExit(0)

    if not key:
        # Show the dashboard URL up front (once) so the user knows where keys
        # come from before deciding — works over SSH/headless too, since they
        # can copy it even if the browser never launches.
        out.note(f"Create a key at {url}")
        key = typer.prompt(
            "Paste your Lenz API key (or press Enter to open that page)",
            default="",
            hide_input=True,
            show_default=False,
        ).strip()

    # No key yet: open the dashboard and KEEP this command open so the user
    # pastes the new key in one go instead of re-running `lenz login`.
    if not key:
        try:
            typer.launch(url)
        except Exception:
            pass
        key = typer.prompt(
            "Opened your browser — paste the key here when ready (or Enter to cancel)",
            default="",
            hide_input=True,
            show_default=False,
        ).strip()

    if not key:
        out.note("No key saved.")
        raise SystemExit(0)

    try:
        path = save_api_key(key)
    except OSError as exc:
        out.error({"error": {"code": "write_failed", "message": str(exc), "status": 0}}, f"Could not save key: {exc}")
        raise SystemExit(1) from None

    if out.json_mode:
        out.emit_json({"status": "ok", "config_file": str(path)})
    else:
        out.console.print(f"[green]✓[/green] Saved API key to {path}")


def logout(ctx: typer.Context) -> None:
    """Remove the locally stored API key (undoes `lenz login`).

    Only clears the key saved on disk — a key supplied via `LENZ_API_KEY` or
    `--api-key` lives in your environment, so it's flagged but left untouched.
    """
    state: CLIState = ctx.obj
    out = state.output
    had_key = clear_api_key()
    env_shadow = bool((os.environ.get(ENV_API_KEY) or "").strip())

    if out.json_mode:
        out.emit_json({"status": "logged_out" if had_key else "no_key", "env_key_present": env_shadow})
    else:
        out.console.print("[green]✓[/green] Cleared the saved API key." if had_key else "No saved API key to clear.")
        if env_shadow:
            out.note(f"Note: {ENV_API_KEY} is still set in your environment — it'll still be used.")


def help_command(
    ctx: typer.Context,
    command: str = typer.Argument(None, help="Show help for this command (e.g. `lenz help verify`)."),
) -> None:
    """Show help for lenz, or for a specific command."""
    # ctx.parent is the top-level group context; its command is the Typer group.
    # It is always present when `help` runs as a subcommand (guarded for typing).
    group_ctx = ctx.parent
    if group_ctx is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()
    group = group_ctx.command
    if command:
        sub = group.get_command(group_ctx, command)  # type: ignore[attr-defined]
        if sub is None:
            typer.echo(f"No such command: {command!r}. Run `lenz help` to list commands.", err=True)
            raise typer.Exit(2)
        # context_class avoids importing click directly (Typer vendors it).
        sub_ctx = sub.context_class(sub, info_name=f"lenz {command}", parent=group_ctx)
        typer.echo(sub.get_help(sub_ctx))
    else:
        typer.echo(group_ctx.get_help())
    raise typer.Exit()


def config_status(ctx: typer.Context) -> None:
    """Show which key is in use (flag/env/file), the base URL, and the config path."""
    state: CLIState = ctx.obj
    render_config(
        state.output,
        {
            "key_source": state.key_source,
            "api_key": mask_key(state.api_key),
            "base_url": state.base_url,
            "config_file": str(config_path()),
        },
    )
