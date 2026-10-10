"""Opt-in smoke tests of ``AsyncLenz`` against a real Lenz environment.

Tagged ``smoke``, like ``test_smoke_staging.py``: the release workflow runs
them (``smoke.yml``) with ``LENZ_E2E_KEY`` set and publishes only when they
pass, so a release cannot ship a broken async path. About a minute and 8
credits: one ``assess``, a ``gather`` of two, a ``depth="low"`` deep check,
and two runs stopped right after they start (a stopped run is not charged).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

from lenz_io import AsyncLenz, LenzPipelineError

pytestmark = pytest.mark.smoke

LENZ_E2E_KEY = os.environ.get("LENZ_E2E_KEY", "")
LENZ_BASE_URL = os.environ.get("LENZ_BASE_URL", "")  # empty -> production


@pytest.fixture()
async def client() -> AsyncIterator[AsyncLenz]:
    if not LENZ_E2E_KEY:
        pytest.skip("LENZ_E2E_KEY not set; smoke tests opt-in")
    kwargs: dict[str, Any] = {"api_key": LENZ_E2E_KEY}
    if LENZ_BASE_URL:
        kwargs["base_url"] = LENZ_BASE_URL
    async with AsyncLenz(**kwargs) as c:
        yield c


async def test_assess(client: AsyncLenz) -> None:
    out = await client.assess(claim="The Great Wall of China is visible from space with the naked eye.")
    assert out.claims and out.claims[0].verdict


async def test_a_gather_of_two_assess_calls(client: AsyncLenz) -> None:
    a, b = await asyncio.gather(
        client.assess(claim="Water boils at 100 degrees Celsius at sea level."),
        client.assess(claim="Mount Everest is the tallest mountain above sea level."),
    )
    assert a.claims[0].verdict and b.claims[0].verdict


async def test_verify_and_wait_at_low_depth(client: AsyncLenz) -> None:
    v = await client.verify_and_wait(claim="Sharks don't get cancer", depth="low", timeout=150)
    assert v.verdict


async def test_a_run_can_be_cancelled(client: AsyncLenz) -> None:
    """``cancel`` answers 200 whatever the state of the run (stopped, or
    already ended), so either branch passes."""
    started = await client.verify(claim="The Eiffel Tower is in Paris", depth="low")
    result = await client.cancel(started.task_id)
    assert result.task_id == started.task_id
    if result.cancelled:
        assert result.status == "cancelled"
        with pytest.raises(LenzPipelineError) as raised:
            await client.wait(started.task_id, timeout=30)
        assert raised.value.failure_class == "cancelled"
    else:
        assert result.status in ("completed", "failed")


async def test_cancel_on_abort_stops_the_run(client: AsyncLenz) -> None:
    """A wait cancelled from outside, with ``cancel_on_abort``, stops the run
    on the server (or finds it already ended: the race is allowed)."""
    started = await client.verify(claim="The Colosseum is in Rome", depth="low")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(client.wait(started.task_id, cancel_on_abort=True), timeout=2)
    status = await client.get_status(started.task_id)
    assert status.status in ("cancelled", "completed", "failed")


async def test_library_iter_first_item(client: AsyncLenz) -> None:
    async for item in client.library.iter():
        assert item.verification_id
        break
