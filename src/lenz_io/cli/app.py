"""Typer application + global flags. Imports the CLI deps (typer/rich); the
:func:`lenz_io.cli.main` entry point guards the import so missing ``[cli]``
deps produce a friendly nudge, not a traceback.

``lenz`` with no command prints help (inert — no execution, no API call).
"""

from __future__ import annotations

import os

import typer

from lenz_io import __version__
from lenz_io.client import DEFAULT_BASE_URL

from . import commands
from . import verify as verify_mod
from .config import ENV_BASE_URL, ConfigError, resolve_api_key, resolve_base_url
from .context import CLIState
from .render import Output

app = typer.Typer(
    name="lenz",
    help=(
        "Lenz — fact-check AI output from your terminal.\n\n"
        "Quick start:\n"
        "  lenz login\n"
        '  lenz assess "Einstein won the 1921 Nobel for relativity"'
    ),
    no_args_is_help=True,
    add_completion=True,
    rich_markup_mode="rich",
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"lenz-cli {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    ctx: typer.Context,
    json_out: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
    api_key: str = typer.Option(None, "--api-key", help="Override the API key for this call."),
    base_url: str = typer.Option(None, "--base-url", help="Override the API base URL."),
    no_color: bool = typer.Option(False, "--no-color", help="Disable colored output."),
    version: bool = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
    ),
) -> None:
    output = Output(json_mode=json_out, no_color=no_color or bool(os.environ.get("NO_COLOR")))
    try:
        key, source = resolve_api_key(api_key)
        base = resolve_base_url(base_url)
    except ConfigError as exc:
        # A corrupt config file resolves here, before any command — left
        # unhandled it tracebacks and bricks even `logout`/`config`, the very
        # commands needed to recover. Degrade to no-key + a clear warning so
        # those still run (and can clear the bad file).
        output.note(f"[yellow]Warning:[/yellow] {exc} Ignoring it — run `lenz logout` to reset.")
        key, source = "", "none"
        # Resolve base without touching the (corrupt) file: flag → env → default.
        base = (base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
    ctx.obj = CLIState(output=output, api_key=key, key_source=source, base_url=base)


app.command("extract")(commands.extract)
app.command("assess")(commands.assess)
app.command("verify")(verify_mod.verify)
app.command("ask")(commands.ask)
app.command("login")(commands.login)
app.command("logout")(commands.logout)
app.command("config")(commands.config_status)
# Hidden: `lenz help [cmd]` mirrors `lenz [cmd] --help`; not listed in --help.
app.command("help", hidden=True)(commands.help_command)
