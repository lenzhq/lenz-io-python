"""The thin verbs — each one resolves input, calls a single SDK method, and
renders. All error/exit handling lives in :func:`._run.execute`; ``verify``
(the only stateful lifecycle) lives in :mod:`.verify`.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import typer

from lenz_io import Lenz

from ._run import execute, read_text_arg
from .config import config_path, mask_key, save_api_key
from .context import CLIState
from .render import render_ask, render_assess, render_config, render_extract


def extract(
    ctx: typer.Context,
    text: str = typer.Argument(None, help="Text to extract claims from ('-' or pipe = stdin)."),
) -> None:
    """Pull verifiable claims out of text. Free (1000/day, no charge) — needs a key."""
    state: CLIState = ctx.obj

    def work(client: Lenz) -> None:
        render_extract(state.output, client.extract(text=read_text_arg(text)))

    execute(state, needs_key=True, work=work)


def assess(
    ctx: typer.Context,
    claim: str = typer.Argument(None, help="Claim to assess ('-' or pipe = stdin)."),
) -> None:
    """Fast 3-model verdict (cheaper/quicker than `verify`). Needs a key."""
    state: CLIState = ctx.obj

    def work(client: Lenz) -> None:
        render_assess(state.output, client.assess(text=read_text_arg(claim)))

    execute(state, needs_key=True, work=work)


def ask(
    ctx: typer.Context,
    verification_id: str = typer.Argument(..., help="The verification_id to ask about."),
    question: str = typer.Argument(None, help="Your question ('-' or pipe = stdin)."),
) -> None:
    """Ask a single grounded question about a past verification. Needs a key."""
    state: CLIState = ctx.obj

    def work(client: Lenz) -> None:
        reply = client.ask.send(verification_id, message=read_text_arg(question))
        render_ask(state.output, reply)

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

    key = state.api_key.strip() if state.key_source in ("flag", "env") else ""
    if not key and not out.json_mode:
        key = typer.prompt(
            "Paste your Lenz API key (press Enter to open the dashboard)",
            default="",
            hide_input=True,
            show_default=False,
        ).strip()

    if not key:
        url = _dashboard_url(state.base_url)
        out.note(f"No key entered. Opening {url} to create one…")
        try:
            typer.launch(url)
        except Exception:
            pass
        if out.json_mode:
            out.emit_json({"status": "no_key", "dashboard": url})
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
