"""A recorder for what the client puts on the wire, for the request freeze
(``test_request_freeze.py``) and the request-option tests.

Every request is recorded with its method, URL (query string as sent), raw
body, ordered header pairs and the four ``httpx.Timeout`` components the
client passed to httpx; every sleep with its length, under a fake clock that
only moves when the client sleeps. Responses are scripted per (method, path).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import respx

from lenz_io import Lenz, LenzError, client as client_module

BASE = "https://lenz.io/api/v1"
API_KEY = "lenz_" + "0" * 32
PINNED = "pinned-key-1"
_RANDOM_KEY = re.compile(r"^[0-9a-f]{32}$")

#: A scripted answer: ``(status, json_body)``, ``(status, json_body, headers)``,
#: an exception instance to raise from the transport, a :class:`Slow` answer or
#: a :class:`Raw` one.
Answer = Any


@dataclass(frozen=True)
class Slow:
    """An answer that takes ``seconds`` of the fake clock to arrive."""

    seconds: float
    answer: Any


@dataclass(frozen=True)
class Raw:
    """An answer whose body is sent as these bytes, not as JSON."""

    status: int
    content: bytes
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeClock:
    now: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _FakeTime:
    """Stands in for the ``time`` module inside ``lenz_io.client`` only."""

    def __init__(self, clock: FakeClock) -> None:
        self.monotonic = clock.monotonic
        self.sleep = clock.sleep


def _mask_headers(raw: list[tuple[bytes, bytes]]) -> list[list[str]]:
    out = []
    for name_b, value_b in raw:
        name, value = name_b.decode("latin-1"), value_b.decode("latin-1")
        lower = name.lower()
        if lower == "user-agent" and value.startswith("lenz-io-python/"):
            value = "<sdk user agent>"
        elif lower == "accept-encoding":
            value = "<httpx default>"  # depends on which decoders are installed
        elif lower == "idempotency-key" and value != PINNED and _RANDOM_KEY.match(value):
            value = "<random>"
        out.append([name, value])
    return out


@dataclass
class Recording:
    requests: list[dict[str, Any]] = field(default_factory=list)
    clock: FakeClock = field(default_factory=FakeClock)

    def summary(self) -> list[dict[str, Any]]:
        return self.requests


class Script:
    """Scripted answers per (method, path below the base URL). The last answer
    of a list repeats."""

    def __init__(self, answers: dict[tuple[str, str], list[Answer]], recording: Recording) -> None:
        self._answers = {k: list(v) for k, v in answers.items()}
        self._rec = recording

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/v1"):
            path = path[len("/api/v1") :]
        timeout = request.extensions.get("timeout") or {}
        self._rec.requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": _mask_headers(request.headers.raw),
                "body": request.content.decode("utf-8"),
                "timeout": {k: timeout.get(k) for k in ("connect", "read", "write", "pool")},
                "at": round(self._rec.clock.now - 1000.0, 6),
            }
        )
        queue = self._answers.get((request.method, path))
        if not queue:
            raise AssertionError(f"unscripted request {request.method} {path}")
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Slow):
            self._rec.clock.now += answer.seconds
            answer = answer.answer
        if isinstance(answer, Raw):
            return httpx.Response(answer.status, content=answer.content, headers=answer.headers)
        if isinstance(answer, Exception):
            raise answer
        status, body, *rest = answer
        headers = rest[0] if rest else {}
        if body is None:
            return httpx.Response(status, headers=headers)
        return httpx.Response(status, json=body, headers=headers)


@contextmanager
def recording(
    monkeypatch: pytest.MonkeyPatch, answers: dict[tuple[str, str], list[Answer]], *, async_client: bool = False
) -> Iterator[Recording]:
    """Record a case. ``async_client``: the fake clock drives ``AsyncLenz``'s
    sleep and clock seams instead of the sync client's ``time``."""
    rec = Recording()
    if async_client:
        from lenz_io import async_client as async_module

        async def sleep(seconds: float) -> None:
            rec.clock.sleep(seconds)

        monkeypatch.setattr(async_module, "_sleep", sleep)
        monkeypatch.setattr(async_module, "_monotonic", rec.clock.monotonic)
    else:
        monkeypatch.setattr(client_module, "time", _FakeTime(rec.clock))
    with respx.mock(assert_all_called=False) as router:
        router.route().mock(side_effect=Script(answers, rec))
        yield rec


def outcome(call: Callable[[], Any]) -> dict[str, Any]:
    """What a call ended with: the result's type, or the error's class and
    the fields a caller reads."""
    try:
        result = call()
    except Exception as exc:  # every outcome is recorded
        key = getattr(exc, "idempotency_key", None)
        if isinstance(key, str) and key != PINNED and _RANDOM_KEY.match(key):
            key = "<random>"
        return {
            "error": type(exc).__name__,
            "status_code": getattr(exc, "status_code", None),
            "code": getattr(exc, "code", None),
            "message": str(getattr(exc, "message", exc)),
            "idempotency_key": key,
        }
    if isinstance(result, list):
        return {"result": [type(r).__name__ for r in result]}
    if isinstance(result, Iterator) or hasattr(result, "__next__"):
        return {"result": [type(r).__name__ for r in result]}
    return {"result": type(result).__name__}


def _plain(value: Any) -> Any:
    """``value`` if it is plain JSON data, else its type's name."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return {k: _plain(v) for k, v in value.items()}
    return f"<{type(value).__name__}>"


def outcome_detail(call: Callable[[], Any]) -> dict[str, Any]:
    """Every attribute of the error a call raised (plain values as they are,
    anything else by its type), its ``str()``, and the types of its
    ``__cause__`` and ``__context__``: what a caller can read off it."""
    try:
        result = call()
    except Exception as exc:  # every outcome is recorded
        attrs = {k: _plain(v) for k, v in sorted(vars(exc).items())}
        key = attrs.get("idempotency_key")
        if isinstance(key, str) and key != PINNED and _RANDOM_KEY.match(key):
            attrs["idempotency_key"] = "<random>"
        if not isinstance(exc, LenzError):
            # Not ours (a ValueError from a body that is not JSON, pydantic's
            # ValidationError): its text and internals belong to the library
            # and the Python version, so only what the SDK adds is pinned.
            return {
                "error": type(exc).__name__,
                "idempotency_key": attrs.get("idempotency_key"),
                "cause": type(exc.__cause__).__name__ if exc.__cause__ is not None else None,
            }
        return {
            "error": type(exc).__name__,
            "str": str(exc),
            "cause": type(exc.__cause__).__name__ if exc.__cause__ is not None else None,
            "context": type(exc.__context__).__name__ if exc.__context__ is not None else None,
            "suppress_context": exc.__suppress_context__,
            "attrs": attrs,
        }
    if isinstance(result, Iterator) or hasattr(result, "__next__"):
        result = list(result)
    values: Any
    if isinstance(result, list):
        values = [_plain(r.model_dump(mode="json")) if hasattr(r, "model_dump") else _plain(r) for r in result]
    elif hasattr(result, "model_dump"):
        values = _plain(result.model_dump(mode="json"))
    else:
        values = _plain(result)
    return {"result": values, "repr": repr(result)}


CLIENTS: dict[str, Callable[[], Lenz]] = {
    "default": lambda: Lenz(api_key=API_KEY),
    "keyless": lambda: Lenz(api_key="", base_url=BASE),
    "timeout_none": lambda: Lenz(api_key=API_KEY, timeout=None),
    "timeout_5_read_200": lambda: Lenz(api_key=API_KEY, timeout=httpx.Timeout(5, read=200)),
    "timeout_200_read_5": lambda: Lenz(api_key=API_KEY, timeout=httpx.Timeout(200, read=5)),
    "timeout_30_read_none": lambda: Lenz(api_key=API_KEY, timeout=httpx.Timeout(30, read=None)),
    "timeout_120": lambda: Lenz(api_key=API_KEY, timeout=120.0),
    "borrowed_10": lambda: Lenz(api_key=API_KEY, http_client=httpx.Client(timeout=10.0)),
    "borrowed_300": lambda: Lenz(api_key=API_KEY, http_client=httpx.Client(timeout=300.0)),
    "max_retries_0": lambda: Lenz(api_key=API_KEY, max_retries=0),
    "max_retries_1": lambda: Lenz(api_key=API_KEY, max_retries=1),
}


def async_clients() -> dict[str, Callable[[], Any]]:
    """``CLIENTS`` for ``AsyncLenz``: the same configurations, made from an
    ``httpx.AsyncClient`` where the sync one is borrowed."""
    from lenz_io import AsyncLenz

    return {
        "default": lambda: AsyncLenz(api_key=API_KEY),
        "keyless": lambda: AsyncLenz(api_key="", base_url=BASE),
        "timeout_none": lambda: AsyncLenz(api_key=API_KEY, timeout=None),
        "timeout_5_read_200": lambda: AsyncLenz(api_key=API_KEY, timeout=httpx.Timeout(5, read=200)),
        "timeout_200_read_5": lambda: AsyncLenz(api_key=API_KEY, timeout=httpx.Timeout(200, read=5)),
        "timeout_30_read_none": lambda: AsyncLenz(api_key=API_KEY, timeout=httpx.Timeout(30, read=None)),
        "timeout_120": lambda: AsyncLenz(api_key=API_KEY, timeout=120.0),
        "borrowed_10": lambda: AsyncLenz(api_key=API_KEY, http_client=httpx.AsyncClient(timeout=10.0)),
        "borrowed_300": lambda: AsyncLenz(api_key=API_KEY, http_client=httpx.AsyncClient(timeout=300.0)),
        "max_retries_0": lambda: AsyncLenz(api_key=API_KEY, max_retries=0),
        "max_retries_1": lambda: AsyncLenz(api_key=API_KEY, max_retries=1),
    }
