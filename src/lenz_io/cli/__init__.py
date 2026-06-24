"""Lenz command-line interface.

Ships inside the ``lenz-io`` package behind the ``[cli]`` extra. The
console entry point is :func:`main` (wired as ``lenz`` in pyproject), NOT
the Typer ``app`` object directly — ``main`` lazily imports the CLI deps so
a plain ``pip install lenz-io`` user who runs ``lenz`` gets a friendly nudge
to install the extra instead of an ``ImportError`` traceback.
"""

from __future__ import annotations

import importlib
import sys

_CLI_DEPS = {"typer", "rich", "click", "platformdirs", "shellingham"}


def main() -> None:
    """Entry point for the ``lenz`` console script."""
    try:
        cli_app = importlib.import_module("lenz_io.cli.app")
    except ModuleNotFoundError as exc:  # CLI extra not installed
        if (getattr(exc, "name", "") or "") in _CLI_DEPS:
            sys.stderr.write('The Lenz CLI needs extra dependencies.\nRun: pip install "lenz-io[cli]"\n')
            raise SystemExit(1) from None
        raise
    cli_app.app()


__all__ = ["main"]
