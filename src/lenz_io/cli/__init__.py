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
            # Only hoist with its value when the next token is a real value. If
            # it's missing or itself an option (`--api-key --json`), leave the
            # opt in place so Click reports a clear error instead of us silently
            # swallowing the following flag as the value.
            if i + 1 < n and not argv[i + 1].startswith("-"):
                front.append(tok)
                i += 1
                front.append(argv[i])
            else:
                rest.append(tok)
        else:
            rest.append(tok)
        i += 1
    return front + rest


def force_utf8_streams() -> None:
    """Make the CLI's own stdin/stdout/stderr UTF-8, whatever the locale says.

    Python picks the standard streams' encoding from the ambient locale, so a
    ``C``/``POSIX`` (or otherwise non-UTF-8) environment gives them an **ascii**
    codec. Claim text is full of characters ascii cannot carry — en dashes,
    arrows, Greek letters, any non-English language — so without this the CLI
    dies on perfectly valid work with ``'ascii' codec can't encode characters
    in position 44-45``, reported through the generic error handler as if the
    *input* had been rejected — the user is left hunting for "the character
    Lenz doesn't like" in a document that is fine. The same locale breaks the
    input side: ``lenz extract - < file.txt`` cannot even decode the document
    it was handed.

    ``errors="replace"`` is the deliberate second half: a byte sequence that
    isn't valid UTF-8 (a latin-1 paste) becomes U+FFFD instead of an exception,
    because a mangled character is a far better outcome than a dead command.

    Only the ``lenz`` console script calls this — importing ``lenz_io`` as a
    library must never reconfigure a host application's streams.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue  # already replaced (pytest capture, a pipe wrapper, …)
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):
            pass  # detached or exotic stream — nothing to fix, nothing to break


def main() -> None:
    """Entry point for the ``lenz`` console script."""
    force_utf8_streams()
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


__all__ = ["force_utf8_streams", "main", "normalize_argv"]
