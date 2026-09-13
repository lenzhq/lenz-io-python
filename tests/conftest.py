"""Test fixtures for lenz-io.

Reuses one ``respx`` mock router per test via the ``respx_mock`` fixture
shipped with the respx package. No real network in any test.
"""

from __future__ import annotations

import pytest

from lenz_io import Lenz

# What rich reads from the environment to decide whether a Console is a
# terminal, whether it may colour, and how wide it is. The CLI honours these on
# purpose; the tests must not inherit them from whoever runs the suite.
# FORCE_COLOR is the one that bites: rich then treats a StringIO as a terminal
# (``Console.is_terminal``), so bold and italic escapes land in every captured
# render and the plain-text assertions fail — ``no_color=True`` strips colour,
# not style.
_AMBIENT_CONSOLE_ENV = (
    "FORCE_COLOR",
    "NO_COLOR",
    "TTY_COMPATIBLE",
    "TTY_INTERACTIVE",
    "COLUMNS",
    "LINES",
)


@pytest.fixture(autouse=True)
def _no_ambient_console_env(monkeypatch):
    """Every test starts with none of ``_AMBIENT_CONSOLE_ENV`` set. Going through
    ``monkeypatch`` also undoes the CLI's own ``os.environ.setdefault("NO_COLOR")``
    at teardown, so one test's ``--no-color`` cannot leak into the next."""
    for name in _AMBIENT_CONSOLE_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def client():
    """Authed client pointed at the default base URL."""
    with Lenz(api_key="lenz_test_abc123") as c:
        yield c


@pytest.fixture()
def unauth_client():
    """Un-keyed client — only library methods should work."""
    with Lenz() as c:
        yield c


@pytest.fixture()
def custom_base_client():
    with Lenz(api_key="lenz_test", base_url="http://localhost:8001/api/v1") as c:
        yield c
