"""Drive an ``AsyncLenz`` through the sync client's calling convention, so one
test (and one freeze case) runs against both clients.

``SyncView(client, loop)`` returns, for every method, a function that runs
the coroutine to completion on ``loop``; a namespace, a ``with_options`` copy
and an iterator come back wrapped the same way. An async iterator is bridged
one ``__anext__()`` per ``next()``, so a test that counts the pages fetched
while it consumes items sees exactly what the async iterator fetched.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterator
from typing import Any

from lenz_io.async_client import (
    AsyncLenz,
    _AsyncAskNamespace,
    _AsyncLibraryNamespace,
    _AsyncVerificationsNamespace,
)

_WRAPPED = (AsyncLenz, _AsyncAskNamespace, _AsyncLibraryNamespace, _AsyncVerificationsNamespace)


class SyncIter(Iterator[Any]):
    def __init__(self, it: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._it = it
        self._loop = loop

    def __iter__(self) -> SyncIter:
        return self

    def __next__(self) -> Any:
        try:
            return self._loop.run_until_complete(self._it.__anext__())
        except StopAsyncIteration:
            raise StopIteration from None


class SyncView:
    def __init__(self, target: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._target = target
        self._loop = loop

    @property
    def target(self) -> Any:
        return self._target

    def _wrap(self, value: Any) -> Any:
        if isinstance(value, _WRAPPED):
            return SyncView(value, self._loop)
        if hasattr(value, "__anext__"):
            return SyncIter(value, self._loop)
        return value

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._target, name)
        if isinstance(value, _WRAPPED):
            return SyncView(value, self._loop)
        if not callable(value):
            return value

        def call(*args: Any, **kwargs: Any) -> Any:
            result = value(*args, **kwargs)
            if inspect.isawaitable(result):
                result = self._loop.run_until_complete(result)
            return self._wrap(result)

        return call

    def close(self) -> None:
        self._loop.run_until_complete(self._target.aclose())

    def __enter__(self) -> SyncView:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
