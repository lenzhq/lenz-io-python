"""The suite must not inherit the console environment of whoever runs it.

CI runs the tests with ``FORCE_COLOR`` set (``.github/workflows/ci.yml``) so
this file fails there if the autouse ``_no_ambient_console_env`` fixture in
``conftest.py`` ever goes: rich treats a ``StringIO`` as a terminal whenever
``FORCE_COLOR`` is set, and bold escapes then land in every captured render.
"""

from __future__ import annotations

import io
import os

from rich.console import Console


def test_a_captured_console_is_not_a_terminal():
    buf = io.StringIO()
    console = Console(file=buf, width=100)
    assert console.is_terminal is False
    console.print("[bold]plain[/bold]")
    assert buf.getvalue() == "plain\n"


def test_force_color_does_not_reach_a_test():
    assert "FORCE_COLOR" not in os.environ
