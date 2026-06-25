"""Lenz command-line interface.

Ships inside the ``lenz-io`` package behind the ``[cli]`` extra. The
console entry point is :func:`main` (wired as ``lenz`` in pyproject), NOT
the Typer ``app`` object directly — ``main`` lazily imports the CLI deps so
a plain ``pip install lenz-io`` user who runs ``lenz`` gets a friendly nudge
to install the extra instead of an ``ImportError`` traceback.
"""

from __future__ import annotations

import importlib
import os
import sys

_CLI_DEPS = {"typer", "rich", "click", "platformdirs", "shellingham"}

# Global options accepted in any position (kubectl-style). Click can't do this
# natively — global flags belong before the subcommand — so we hoist them to
# the front of argv before Typer parses. Value-less flags vs value-taking opts.
_GLOBAL_FLAGS = ("--json", "--no-color")
_GLOBAL_VALUE_OPTS = ("--api-key", "--base-url")


def normalize_argv(argv: list[str]) -> list[str]:
    """Move recognized global options to the front so they work in any position.

    Stops at ``--`` (end-of-options): tokens after it are positional and left
    untouched, so ``lenz extract -- --json`` extracts the literal text. Safe for
    this CLI because no command value-option ever takes a value that looks like
    a global flag.
    """
    front: list[str] = []
    rest: list[str] = []
    i, n = 0, len(argv)
    end_of_opts = False
    while i < n:
        tok = argv[i]
        if end_of_opts:
            rest.append(tok)
        elif tok == "--":
            end_of_opts = True
            rest.append(tok)
        elif tok in _GLOBAL_FLAGS or any(tok.startswith(o + "=") for o in _GLOBAL_VALUE_OPTS):
            front.append(tok)
        elif tok in _GLOBAL_VALUE_OPTS:
            front.append(tok)
            if i + 1 < n:  # carry its value along
                i += 1
                front.append(argv[i])
        else:
            rest.append(tok)
        i += 1
    return front + rest


def main() -> None:
    """Entry point for the ``lenz`` console script."""
    try:
        cli_app = importlib.import_module("lenz_io.cli.app")
    except ModuleNotFoundError as exc:  # CLI extra not installed
        if (getattr(exc, "name", "") or "") in _CLI_DEPS:
            sys.stderr.write('The Lenz CLI needs extra dependencies.\nRun: pip install "lenz-io[cli]"\n')
            raise SystemExit(1) from None
        raise
    sys.argv = [sys.argv[0], *normalize_argv(sys.argv[1:])]
    # --no-color must also reach Typer's own help renderer, which short-circuits
    # before our callback runs. Rich honors NO_COLOR globally, so set it here.
    if "--no-color" in sys.argv:
        os.environ.setdefault("NO_COLOR", "1")
    cli_app.app()


__all__ = ["main", "normalize_argv"]
