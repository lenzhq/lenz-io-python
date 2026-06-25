"""The shared command wrapper — the DRY core every verb runs through.

``execute`` owns client construction, the no-key short-circuit, and uniform
error handling (JSON-to-stdout in ``--json`` mode, friendly text to stderr
otherwise) + nonzero exit. Commands shrink to "call the SDK, render the
result." This is the single place the error/render contract lives, so it can
never drift between commands (and the future MCP server reuses it).
"""

from __future__ import annotations

import os
import sys
from typing import Callable

from lenz_io import Lenz

from .client import build_client
from .context import CLIState
from .errors import CLIError, exit_code_for, friendly_text, no_api_key_error, to_payload


def read_text_arg(text: str | None) -> str:
    """Resolve a text argument from the positional value, ``-``, or stdin.

    Reads stdin only when it's explicitly requested (``-``) or actually piped.
    With no argument on an interactive terminal we must NOT call ``read()`` — it
    would block forever waiting for input the user doesn't know to give.
    """
    if text == "-":
        data = sys.stdin.read()
    elif text is None:
        if sys.stdin.isatty():
            raise CLIError(
                "No text provided. Pass it as an argument or pipe it via stdin.",
                code="no_input",
                exit_code=2,
            )
        data = sys.stdin.read()
    else:
        data = text
    data = data.strip()
    if not data:
        raise CLIError(
            "No text provided. Pass it as an argument or pipe it via stdin.",
            code="no_input",
            exit_code=2,
        )
    return data


def execute(state: CLIState, *, needs_key: bool, work: Callable[[Lenz], None]) -> None:
    """Build the client, run ``work``, translate any failure into the contract."""
    out = state.output
    try:
        if needs_key and state.key_source == "none":
            raise no_api_key_error()
        client = build_client(api_key=state.api_key, base_url=state.base_url)
        try:
            work(client)
        finally:
            client.close()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        if os.environ.get("LENZ_CLI_TRACEBACK"):
            raise
        out.error(to_payload(exc), friendly_text(exc))
        raise SystemExit(exit_code_for(exc)) from None
