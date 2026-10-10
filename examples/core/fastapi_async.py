# mypy: allow-untyped-decorators
"""Check a draft from an async FastAPI app with ``AsyncLenz``.

One ``AsyncLenz`` is created in the app's lifespan (one client per event
loop, never at import time) and closed on shutdown. Two endpoints:

* ``POST /check`` awaits a quick ``assess`` (about 15 s) without blocking the
  event loop: other requests keep flowing while it runs.
* ``POST /review`` awaits a whole review (2-4 minutes). FastAPI does not
  cancel a handler when the browser goes away, so the wait runs as a task
  that is cancelled once ``request.is_disconnected()`` says the caller left;
  with ``cancel_on_abort=True`` the review is then stopped on the server
  too (it refunds what it had not delivered).
* ``POST /verify-later`` starts a deep check and returns at once; its result
  arrives by webhook (see ``fastapi_webhook.py`` for the receiver).

Run:
    pip install fastapi uvicorn
    export LENZ_API_KEY=lenz_...
    uvicorn examples.core.fastapi_async:app --port 8000
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Any, Protocol, TypeVar

from fastapi import FastAPI, Request, Response

from lenz_io import AsyncLenz

T = TypeVar("T")


class _Caller(Protocol):
    async def is_disconnected(self) -> bool: ...


async def until_disconnected(request: _Caller, work: Awaitable[T], *, every: float = 1.0) -> T | None:
    """Await ``work``, but cancel it (and return ``None``) once the caller
    has disconnected, checked every ``every`` seconds. Cancelling a Lenz wait
    made with ``cancel_on_abort=True`` also stops the job on the server."""
    task = asyncio.ensure_future(work)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=every)
            if done:
                return task.result()
            if await request.is_disconnected():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                return None
    finally:
        if not task.done():  # this handler was itself cancelled
            task.cancel()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with AsyncLenz() as client:  # reads LENZ_API_KEY
        app.state.lenz = client
        yield


app = FastAPI(lifespan=lifespan)


@app.post("/check")
async def check(payload: dict[str, str], request: Request) -> dict[str, Any]:
    client: AsyncLenz = request.app.state.lenz
    row = (await client.assess(claim=payload["claim"])).claims[0]
    return {"verdict": row.verdict, "confidence": row.confidence}


@app.post("/review", response_model=None)
async def review(payload: dict[str, str], request: Request) -> dict[str, Any] | Response:
    client: AsyncLenz = request.app.state.lenz
    result = await until_disconnected(request, client.review_and_wait(payload["text"], cancel_on_abort=True))
    if result is None:
        return Response(status_code=499)  # the caller left; the review was stopped
    return {"outcome": result.outcome, "issues": [issue.claim for issue in result.issues]}


@app.post("/verify-later")
async def verify_later(payload: dict[str, str], request: Request) -> dict[str, str]:
    client: AsyncLenz = request.app.state.lenz
    task = await client.verify(claim=payload["claim"], webhook_url=payload["webhook_url"])
    return {"task_id": task.task_id}
