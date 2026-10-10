"""Test fixtures for lenz-io.

Reuses one ``respx`` mock router per test via the ``respx_mock`` fixture
shipped with the respx package. No real network in any test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
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


class AnyClient:
    """What ``any_client`` gives a test: ``make(**kwargs)`` builds the client
    under test (``Lenz``, or an ``AsyncLenz`` driven through ``SyncView``),
    ``slept`` records every sleep either client takes (none really sleeps),
    and ``install_clock`` puts both clients on a fake clock. The client of
    the running test is also reachable as ``make_client`` / ``make_http``."""

    current: AnyClient | None = None

    def __init__(self, kind: str, loop: asyncio.AbstractEventLoop | None, monkeypatch: pytest.MonkeyPatch) -> None:
        self.kind = kind
        self.loop = loop
        self.slept: list[float] = []
        self._mp = monkeypatch
        self._clock: list[float] | None = None
        if loop is None:
            monkeypatch.setattr("lenz_io.client.time.sleep", self._sync_sleep)
        else:
            monkeypatch.setattr("lenz_io.async_client._sleep", self._async_sleep)

    def _sync_sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self._clock is not None:
            self._clock[0] += seconds

    async def _async_sleep(self, seconds: float) -> None:
        self._sync_sleep(seconds)

    def install_clock(self, now: list[float]) -> list[float]:
        """Make ``now[0]`` the clock of both clients; every sleep moves it."""
        self._clock = now
        target = "lenz_io.client.time.monotonic" if self.loop is None else "lenz_io.async_client._monotonic"
        self._mp.setattr(target, lambda: now[0])
        return now

    def make(self, **kwargs: Any) -> Any:
        if self.loop is None:
            return Lenz(**kwargs)
        from async_adapter import SyncView

        from lenz_io import AsyncLenz

        return SyncView(AsyncLenz(**kwargs), self.loop)

    def make_http(self, **kwargs: Any) -> Any:
        if self.loop is None:
            return httpx.Client(**kwargs)
        return _ClosingAsyncClient(self.loop, **kwargs)


class _ClosingAsyncClient(httpx.AsyncClient):
    """An ``httpx.AsyncClient`` a sync ``with`` block can close (for tests
    written against ``with httpx.Client() as http:``)."""

    def __init__(self, loop: asyncio.AbstractEventLoop, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._loop = loop

    def __enter__(self) -> _ClosingAsyncClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self._loop.run_until_complete(self.aclose())

    def close(self) -> None:
        self._loop.run_until_complete(self.aclose())


def make_client(**kwargs: Any) -> Any:
    """The client under test of the running ``any_client`` test (``Lenz``
    when there is none)."""
    return AnyClient.current.make(**kwargs) if AnyClient.current is not None else Lenz(**kwargs)


def make_http(**kwargs: Any) -> Any:
    return AnyClient.current.make_http(**kwargs) if AnyClient.current is not None else httpx.Client(**kwargs)


@pytest.fixture(params=["sync", "async"])
def any_client(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[AnyClient]:
    """Run the test on both clients: the same calls, the same assertions. A
    test marked ``sync_only`` runs on the sync client only."""
    if request.param == "async" and request.node.get_closest_marker("sync_only"):
        pytest.skip("sync client only")
    loop = asyncio.new_event_loop() if request.param == "async" else None
    clients = AnyClient(request.param, loop, monkeypatch)
    AnyClient.current = clients
    try:
        yield clients
    finally:
        AnyClient.current = None
        if loop is not None:
            loop.close()
