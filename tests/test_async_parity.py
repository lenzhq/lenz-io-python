"""``AsyncLenz`` sends what ``Lenz`` sends, waits as it waits and ends as it
ends: every case of the request and outcome freezes, run against the async
client and compared with the same golden files.

So for every public call form, both clients put the same URL, query, body
bytes, ordered headers (the User-Agent aside: masked here, checked below)
and per-attempt timeouts on the wire, at the same moments under the same
fake clock, sleep the same sleeps (the retry ladder, the poll pacing, the
batch rounds, which stay sequential), and end with the same result or the
same error, attribute for attribute.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from async_adapter import SyncView
from freeze_cases import CASES
from freeze_harness import API_KEY, async_clients, outcome, outcome_detail, recording
from test_outcome_freeze import GOLDEN as OUTCOMES
from test_request_freeze import GOLDEN as REQUESTS

from lenz_io import AsyncLenz


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LENZ_API_KEY", raising=False)
    monkeypatch.delenv("LENZ_BASE_URL", raising=False)


@pytest.fixture()
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


def _observe(monkeypatch: pytest.MonkeyPatch, loop: asyncio.AbstractEventLoop, name: str) -> dict[str, Any]:
    _, client_name, call, answers = next(c for c in CASES if c[0] == name)
    with recording(monkeypatch, answers, async_client=True) as rec:
        client = SyncView(async_clients()[client_name](), loop)
        try:
            ended = outcome(lambda: call(client))  # type: ignore[arg-type]
        finally:
            client.close()
    with recording(monkeypatch, answers, async_client=True):
        client = SyncView(async_clients()[client_name](), loop)
        try:
            detail = outcome_detail(lambda: call(client))  # type: ignore[arg-type]
        finally:
            client.close()
    return {"requests": rec.requests, "sleeps": rec.clock.sleeps, "outcome": ended, "detail": detail}


@pytest.mark.parametrize("name", [c[0] for c in CASES])
def test_the_async_client_matches_the_frozen_sync_client(
    monkeypatch: pytest.MonkeyPatch, loop: asyncio.AbstractEventLoop, name: str
) -> None:
    requests = json.loads(REQUESTS.read_text())[name]
    outcomes = json.loads(OUTCOMES.read_text())[name]
    seen = _observe(monkeypatch, loop, name)
    assert seen["requests"] == requests["requests"]
    assert seen["sleeps"] == requests["sleeps"]
    assert seen["outcome"] == requests["outcome"]
    assert seen["detail"] == outcomes


def test_the_user_agent_names_the_async_client() -> None:
    async def run() -> str:
        with respx.mock(assert_all_called=False) as router:
            route = router.get("https://lenz.io/api/v1/me/usage").respond(200, json={})
            async with AsyncLenz(api_key=API_KEY) as client:
                await client.usage()
            return str(route.calls.last.request.headers["User-Agent"])

    agent = asyncio.run(run())
    assert agent.startswith("lenz-io-python/")
    assert agent.endswith("; async)")
    # The API reads the leading token to tell the Python SDK from other clients.
    assert agent.split()[0].split("/")[0] == "lenz-io-python"
