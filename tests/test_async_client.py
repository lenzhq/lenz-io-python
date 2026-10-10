"""What only ``AsyncLenz`` has, or only it can get wrong.

The calls themselves (bodies, headers, retries, waits, errors, results) are
held to the sync client's frozen behaviour by ``test_async_parity.py`` and by
the test files that run on both clients (``any_client``). Here: the public
surface and its docstrings, the lazy import, the async callbacks,
cancellation (of a request, a retry sleep, a poll sleep, a callback, and
with ``cancel_on_abort``), the connection pool after a cancellation, many
calls on one client, ownership of the pool, and that nothing blocks the
event loop.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import json
import logging
import subprocess
import sys
import threading
import types
import typing
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

import lenz_io
from lenz_io import (
    AsyncLenz,
    BatchAccepted,
    Lenz,
    LenzAPIError,
    LenzError,
    ReviewFull,
    TaskAccepted,
    async_client as async_module,
)
from lenz_io.async_client import (
    _AsyncAskNamespace,
    _AsyncLibraryNamespace,
    _AsyncVerificationsNamespace,
)
from lenz_io.client import _AskNamespace, _LibraryNamespace, _VerificationsNamespace

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "lenz_io"
KEY = "lenz_" + "0" * 32
BASE = "https://lenz.io/api/v1"
FIXTURES = Path(__file__).parent / "fixtures" / "contract"

RUNNING = {"status": "processing", "task_id": "t1", "progress": {"step": "research"}}
DONE = {"status": "completed", "task_id": "t1", "result": {"verification_id": "v1", "claim": "A."}}


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LENZ_API_KEY", raising=False)
    monkeypatch.delenv("LENZ_BASE_URL", raising=False)


@pytest.fixture()
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The SDK's sleeps return at once (still yielding to the loop)."""
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(async_module, "_sleep", sleep)
    return slept


Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


class Server:
    """A scripted async transport: ``routes[(method, path)]`` is a list of
    answers (the last repeats); an answer is a response, a dict (JSON 200),
    or an async function of the request. Every request is recorded."""

    def __init__(self, routes: dict[tuple[str, str], list[Any]]) -> None:
        self.routes = {k: list(v) for k, v in routes.items()}
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v1")
        queue = self.routes.get((request.method, path))
        if not queue:
            raise AssertionError(f"unscripted {request.method} {path}")
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(answer):
            answer = await answer(request)
        if isinstance(answer, dict):
            return httpx.Response(200, json=answer)
        assert isinstance(answer, httpx.Response)
        return answer

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.removeprefix("/api/v1") == path]

    def client(self, **kwargs: Any) -> AsyncLenz:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self))
        return AsyncLenz(api_key=KEY, http_client=http, **kwargs)


def _blocked(event: asyncio.Event) -> Callable[[httpx.Request], Awaitable[httpx.Response]]:
    async def answer(request: httpx.Request) -> httpx.Response:
        event.set()
        await asyncio.sleep(3600)
        raise AssertionError("never answered")

    return answer


# ── the surface ────────────────────────────────────────────────────────────

PAIRS = [
    (Lenz, AsyncLenz),
    (_VerificationsNamespace, _AsyncVerificationsNamespace),
    (_AskNamespace, _AsyncAskNamespace),
    (_LibraryNamespace, _AsyncLibraryNamespace),
]
#: Sync name -> async name, where they differ.
RENAMED = {"close": "aclose", "__enter__": "__aenter__", "__exit__": "__aexit__"}
#: Plain methods on both clients (not coroutines on the async one).
PLAIN = {"iter", "with_options", "__init__"}
#: The deliberate additions (README "Differences from Lenz").
ASYNC_ONLY_PARAMS = {
    "verify_and_wait": {"cancel_on_abort"},
    "wait": {"cancel_on_abort"},
    "verify_batch_and_wait": {"cancel_on_abort"},
    "review_and_wait": {"cancel_on_abort"},
    "citecheck_and_wait": {"cancel_on_abort"},
}


def _public(cls: type) -> list[str]:
    dunders = set(RENAMED) | set(RENAMED.values())
    return sorted(n for n, v in vars(cls).items() if callable(v) and (not n.startswith("_") or n in dunders))


def _norm(annotation: Any) -> str:
    """An annotation written for the sync client, as the async one spells it."""
    text = str(annotation)
    text = text.replace("httpx.Client", "httpx.AsyncClient").replace("Iterator[", "AsyncIterator[")
    text = text.replace("AsyncAsyncIterator[", "AsyncIterator[")
    for job in ("[str, Progress]", "[ReviewFull]", "[Citecheck]"):
        text = text.replace(f"Callable[{job}, None]", f"Callable[{job}, Awaitable[object] | None]")
    return text.replace("_Client", "_Client")


@pytest.mark.parametrize(("sync_cls", "async_cls"), PAIRS)
def test_every_public_method_has_its_async_twin(sync_cls: type, async_cls: type) -> None:
    sync_names = {RENAMED.get(n, n) for n in _public(sync_cls)}
    assert sync_names == set(_public(async_cls))


@pytest.mark.parametrize(("sync_cls", "async_cls"), PAIRS)
def test_the_signatures_match(sync_cls: type, async_cls: type) -> None:
    for name in _public(sync_cls):
        sync_fn = getattr(sync_cls, name)
        async_fn = getattr(async_cls, RENAMED.get(name, name))
        if name in PLAIN or name in ("__exit__",):
            assert not inspect.iscoroutinefunction(async_fn) or name == "__exit__", name
        else:
            assert inspect.iscoroutinefunction(async_fn), name
        assert not inspect.iscoroutinefunction(sync_fn), name
        sync_params = inspect.signature(sync_fn).parameters
        async_params = dict(inspect.signature(async_fn).parameters)
        for extra in ASYNC_ONLY_PARAMS.get(name, set()) if sync_cls is Lenz else set():
            param = async_params.pop(extra)
            assert param.kind is inspect.Parameter.KEYWORD_ONLY and param.default is False
        assert list(sync_params) == list(async_params), name
        for pname, p in sync_params.items():
            q = async_params[pname]
            assert (p.kind, p.default) == (q.kind, q.default), (name, pname)
            assert _norm(p.annotation) == str(q.annotation), (name, pname)


def test_the_return_types_match() -> None:
    for name in _public(Lenz):
        if name in ("close", "__enter__", "__exit__", "__init__"):
            continue
        sync_ret = str(inspect.signature(getattr(Lenz, name)).return_annotation)
        async_ret = str(inspect.signature(getattr(AsyncLenz, name)).return_annotation)
        assert _norm(sync_ret) == async_ret, name


@pytest.mark.skipif(sys.version_info < (3, 11), reason="typing.get_overloads is 3.11+")
def test_the_get_review_overloads_are_mirrored() -> None:
    sync = typing.get_overloads(Lenz.get_review)  # type: ignore[attr-defined,unused-ignore]
    asyn = typing.get_overloads(AsyncLenz.get_review)  # type: ignore[attr-defined,unused-ignore]
    assert [str(inspect.signature(f)) for f in sync] == [str(inspect.signature(f)) for f in asyn]


def test_the_docstrings_are_the_sync_ones_for_asyncio() -> None:
    spec = importlib.util.spec_from_file_location(
        "sync_async_docstrings", ROOT / "scripts" / "sync_async_docstrings.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["", "--check"]) == 0
    assert "await client.assess(" in (AsyncLenz.assess.__doc__ or "")
    assert "cancel_on_abort" in (AsyncLenz.review_and_wait.__doc__ or "")


def test_async_lenz_is_exported_and_imported_lazily() -> None:
    assert "AsyncLenz" in lenz_io.__all__
    assert lenz_io.AsyncLenz is AsyncLenz
    code = "import sys, lenz_io; print('lenz_io.async_client' in sys.modules or 'AsyncLenz' not in dir(lenz_io))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "False"
    with pytest.raises(AttributeError):
        _ = lenz_io.NoSuchThing  # type: ignore[attr-defined]


# ── one core, two drivers ──────────────────────────────────────────────────


def _functions_catching(path: Path, name: str) -> set[str]:
    tree = ast.parse(path.read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for handler in ast.walk(node):
                if isinstance(handler, ast.ExceptHandler) and isinstance(handler.type, ast.Name):
                    if handler.type.id == name:
                        out.add(node.name)
    return out


def test_error_recovery_lives_in_the_drivers_only() -> None:
    """Every ``except LenzError`` of a facade is in a driver function, the
    same ones on both clients: the per-operation recoveries are ``_core``'s."""
    drivers = {"_recovering", "_poll_tasks", "_poll_to_terminal"}
    sync = _functions_catching(SRC / "client.py", "LenzError")
    asyn = _functions_catching(SRC / "async_client.py", "LenzError")
    assert sync <= drivers and asyn <= drivers
    assert "_recovering" in sync and "_recovering" in asyn


def _imports(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.add(node.module.split(".")[0])
    return out


def _attribute_calls(tree: ast.Module) -> list[tuple[str, str, ast.Call]]:
    """``(owner, attribute, call)`` for every ``owner.attribute(...)`` call."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = ast.unparse(node.func.value)
            out.append((owner, node.func.attr, node))
    return out


def test_the_shared_core_does_no_io_and_the_async_driver_never_blocks() -> None:
    for name in ("_core.py", "_polling.py"):
        tree = ast.parse((SRC / name).read_text())
        assert not _imports(tree) & {"time", "asyncio", "threading"}, name
        calls = {(owner, attr) for owner, attr, _ in _attribute_calls(tree)}
        assert not {c for c in calls if c[1] in ("request", "send", "sleep", "monotonic")}, name
        assert ("httpx", "Client") not in calls and ("httpx", "AsyncClient") not in calls, name
    tree = ast.parse((SRC / "async_client.py").read_text())
    calls = _attribute_calls(tree)
    assert not [c for c in calls if (c[0], c[1]) in (("time", "sleep"), ("httpx", "Client"))]
    awaited = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Await)}
    requests = [call for owner, attr, call in calls if owner == "self._client" and attr == "request"]
    assert requests and all(id(call) in awaited for call in requests)


def test_the_sync_client_still_sleeps_through_its_time_module(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("lenz_io.client.time.sleep", slept.append)
    transport = httpx.MockTransport(lambda r: httpx.Response(503, json={"detail": "x"}))
    with Lenz(api_key=KEY, http_client=httpx.Client(transport=transport), max_retries=1) as client:
        with pytest.raises(LenzAPIError):
            client.usage()
    assert slept == [1.0]


# ── callbacks ──────────────────────────────────────────────────────────────


async def test_an_async_on_progress_is_awaited_before_the_next_poll(no_sleep: list[float]) -> None:
    server = Server({("GET", "/verify/status/t1"): [RUNNING, RUNNING, DONE]})
    log: list[str] = []

    async def on_progress(task_id: str, progress: Any) -> None:
        log.append(f"start {len(server.requests)}")
        await asyncio.sleep(0)
        log.append(f"end {len(server.requests)}")
        progress.step = "mutated"

    async with server.client() as client:
        result = await client.wait("t1", on_progress=on_progress)
    assert result.verification_id == "v1"
    assert log == ["start 1", "end 1", "start 2", "end 2"]


async def test_a_plain_callback_that_returns_an_awaitable_is_awaited(no_sleep: list[float]) -> None:
    server = Server({("GET", "/verify/status/t1"): [RUNNING, DONE]})
    done: list[str] = []

    async def later(step: str) -> None:
        await asyncio.sleep(0)
        done.append(step)

    async with server.client() as client:
        await client.wait("t1", on_progress=lambda task_id, p: later(p.step))
    assert done == ["research"]


async def test_a_callback_that_fails_after_suspending_is_swallowed(
    no_sleep: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    server = Server({("GET", "/verify/status/t1"): [RUNNING, RUNNING, DONE]})

    async def on_progress(task_id: str, progress: Any) -> None:
        await asyncio.sleep(0)
        raise RuntimeError("caller bug")

    caplog.set_level(logging.DEBUG, logger="lenz_io")
    async with server.client() as client:
        result = await client.wait("t1", on_progress=on_progress)
    assert result.verification_id == "v1"
    assert sum("on_progress callback raised" in r.message for r in caplog.records) == 2


async def test_on_update_gets_a_copy_and_is_awaited(no_sleep: list[float]) -> None:
    running, done = _load("review_assessing.json"), _load("review_completed.json")
    rid = done["review_id"]
    server = Server(
        {
            ("POST", "/review"): [httpx.Response(202, json={"review_id": rid})],
            ("GET", f"/reviews/{rid}"): [running, done],
        }
    )
    seen: list[str] = []

    async def on_update(review: ReviewFull) -> None:
        await asyncio.sleep(0)
        seen.append(review.status)
        review.status = "mutated"

    async with server.client() as client:
        result = await client.review_and_wait("Draft.", on_update=on_update)
    assert seen == ["assessing", "completed"]
    assert result.status == "completed"


async def test_cancelling_while_a_callback_is_awaited_propagates(no_sleep: list[float]) -> None:
    server = Server({("GET", "/verify/status/t1"): [RUNNING, DONE]})
    entered = asyncio.Event()

    async def on_progress(task_id: str, progress: Any) -> None:
        entered.set()
        await asyncio.sleep(3600)

    async with server.client() as client:
        task = asyncio.ensure_future(client.wait("t1", on_progress=on_progress))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(server.requests) == 1


# ── cancellation without cancel_on_abort: it only stops waiting ────────────


async def test_cancelling_a_request_stops_at_once_and_sends_nothing_more(no_sleep: list[float]) -> None:
    sent = asyncio.Event()
    server = Server({("POST", "/assess"): [_blocked(sent)]})
    async with server.client() as client:
        task = asyncio.ensure_future(client.assess(claim="A."))
        await sent.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(server.requests) == 1


async def test_cancelling_a_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeping = asyncio.Event()

    async def sleep(seconds: float) -> None:
        sleeping.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(async_module, "_sleep", sleep)
    server = Server({("GET", "/me/usage"): [httpx.Response(503, json={"detail": "x"})]})
    async with server.client() as client:
        task = asyncio.ensure_future(client.usage())
        await sleeping.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(server.requests) == 1


@pytest.mark.parametrize("flag", [False, True])
async def test_cancelling_a_poll_sleep(monkeypatch: pytest.MonkeyPatch, flag: bool) -> None:
    sleeping = asyncio.Event()

    async def sleep(seconds: float) -> None:
        sleeping.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(async_module, "_sleep", sleep)
    server = Server(
        {
            ("GET", "/verify/status/t1"): [RUNNING],
            ("POST", "/verify/t1/cancel"): [{"task_id": "t1", "cancelled": True, "status": "cancelled"}],
        }
    )
    async with server.client() as client:
        task = asyncio.ensure_future(client.wait("t1", cancel_on_abort=flag))
        await sleeping.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(server.calls("GET", "/verify/status/t1")) == 1
    assert len(server.calls("POST", "/verify/t1/cancel")) == (1 if flag else 0)


async def test_a_wait_for_around_a_wait(no_sleep: list[float]) -> None:
    sent = asyncio.Event()
    server = Server({("GET", "/verify/status/t1"): [_blocked(sent)]})
    async with server.client() as client:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(client.wait("t1"), timeout=0.05)
    assert len(server.requests) == 1


# ── cancel_on_abort ────────────────────────────────────────────────────────

CANCELLED = {"task_id": "t1", "cancelled": True, "status": "cancelled"}


async def _cancel_once_polling(client_call: Awaitable[Any], polled: asyncio.Event) -> None:
    task = asyncio.ensure_future(client_call)
    await polled.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancel_on_abort_sends_one_cancel_and_re_raises() -> None:
    polled = asyncio.Event()
    server = Server(
        {
            ("POST", "/verify"): [{"task_id": "t1"}],
            ("GET", "/verify/status/t1"): [_blocked(polled)],
            ("POST", "/verify/t1/cancel"): [CANCELLED],
        }
    )
    async with server.client() as client:
        # verify_and_wait hands the flag to wait: the cancel is sent once.
        await _cancel_once_polling(client.verify_and_wait("A.", cancel_on_abort=True), polled)
    cancels = server.calls("POST", "/verify/t1/cancel")
    assert len(cancels) == 1
    assert "Idempotency-Key" not in cancels[0].headers


async def test_without_the_flag_nothing_is_cancelled() -> None:
    polled = asyncio.Event()
    server = Server({("POST", "/verify"): [{"task_id": "t1"}], ("GET", "/verify/status/t1"): [_blocked(polled)]})
    async with server.client() as client:
        await _cancel_once_polling(client.verify_and_wait("A."), polled)
    assert [r.method for r in server.requests] == ["POST", "GET"]


async def test_nothing_is_cancelled_before_the_job_exists() -> None:
    sent = asyncio.Event()
    server = Server({("POST", "/verify"): [_blocked(sent)]})
    async with server.client() as client:
        await _cancel_once_polling(client.verify_and_wait("A.", cancel_on_abort=True), sent)
    assert [r.url.path for r in server.requests] == ["/api/v1/verify"]


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500, json={"detail": "boom"}),
        httpx.Response(404, json={"code": "not_found"}),
    ],
)
async def test_a_failed_cancel_is_logged_and_the_cancellation_goes_on(
    answer: httpx.Response, caplog: pytest.LogCaptureFixture
) -> None:
    polled = asyncio.Event()
    server = Server({("GET", "/verify/status/t1"): [_blocked(polled)], ("POST", "/verify/t1/cancel"): [answer]})
    caplog.set_level(logging.WARNING, logger="lenz_io")
    async with server.client() as client:
        await _cancel_once_polling(client.wait("t1", cancel_on_abort=True), polled)
    assert len(server.calls("POST", "/verify/t1/cancel")) == 1  # no retry
    [record] = [r for r in caplog.records if "cancel_on_abort" in r.message]
    assert record.levelno == logging.WARNING and "t1" in record.message


async def test_a_hung_cancel_is_given_up_within_the_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(async_module, "_ABORT_CANCEL_BUDGET", 0.05)
    polled, cancel_sent = asyncio.Event(), asyncio.Event()
    server = Server(
        {("GET", "/verify/status/t1"): [_blocked(polled)], ("POST", "/verify/t1/cancel"): [_blocked(cancel_sent)]}
    )
    caplog.set_level(logging.WARNING, logger="lenz_io")
    async with server.client() as client:
        loop = asyncio.get_running_loop()
        started = loop.time()
        await _cancel_once_polling(client.wait("t1", cancel_on_abort=True), polled)
        assert loop.time() - started < 2
    assert any("given up" in r.message for r in caplog.records)


async def test_a_cancel_that_loses_the_race_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    polled = asyncio.Event()
    server = Server(
        {
            ("GET", "/verify/status/t1"): [_blocked(polled)],
            ("POST", "/verify/t1/cancel"): [{"task_id": "t1", "cancelled": False, "status": "completed"}],
        }
    )
    caplog.set_level(logging.WARNING, logger="lenz_io")
    async with server.client() as client:
        await _cancel_once_polling(client.wait("t1", cancel_on_abort=True), polled)
    [record] = [r for r in caplog.records if "cancel_on_abort" in r.message]
    assert "was not cancelled" in record.message and "completed" in record.message


async def test_a_batch_cancels_each_item_still_running_once(no_sleep: list[float]) -> None:
    polled = asyncio.Event()
    batch = {
        "batch_id": "b",
        "items": [
            {"task_id": "done", "claim": "A."},
            {"task_id": "gone", "claim": "B."},
            {"task_id": "", "claim": "C."},
            {"task_id": "run1", "claim": "D."},
            {"task_id": "run2", "claim": "E."},
            {"task_id": "run1", "claim": "D again."},
        ],
    }
    calls = {"n": 0}

    async def second_round(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] >= 2:
            return await _blocked(polled)(request)
        return httpx.Response(200, json={**RUNNING, "task_id": "run1"})

    def cancelled(task_id: str) -> dict[str, Any]:
        return {"task_id": task_id, "cancelled": True, "status": "cancelled"}

    server = Server(
        {
            ("POST", "/verify/batch"): [batch],
            ("GET", "/verify/status/done"): [{**DONE, "task_id": "done"}],
            ("GET", "/verify/status/gone"): [httpx.Response(404, json={"code": "not_found"})],
            ("GET", "/verify/status/run1"): [second_round],
            ("GET", "/verify/status/run2"): [{**RUNNING, "task_id": "run2"}],
            ("POST", "/verify/run1/cancel"): [httpx.Response(500, json={"detail": "x"})],
            ("POST", "/verify/run2/cancel"): [cancelled("run2")],
        }
    )
    async with server.client() as client:
        call = client.verify_batch_and_wait(
            claims=[{"claim": c["claim"]} for c in batch["items"]], cancel_on_abort=True
        )
        await _cancel_once_polling(call, polled)
    cancels = sorted(r.url.path for r in server.requests if r.url.path.endswith("/cancel"))
    # One failing cancel never stops the others; settled and empty ids are left alone.
    assert cancels == ["/api/v1/verify/run1/cancel", "/api/v1/verify/run2/cancel"]


@pytest.mark.parametrize(
    ("submit", "accepted", "poll", "running", "cancel", "cancelled"),
    [
        (
            "/review",
            {"review_id": "r1"},
            "/reviews/r1",
            {"review_id": "r1", "status": "verifying", "issues": [], "failures": [], "claims": []},
            "/reviews/r1/cancel",
            {"review_id": "r1", "status": "cancelled", "issues": [], "failures": [], "claims": []},
        ),
        (
            "/citecheck",
            {"citecheck_id": "c1"},
            "/citechecks/c1",
            {
                "citecheck_id": "c1",
                "status": "checking",
                "citations": [],
                "citation_issues": [],
                "citation_failures": [],
            },
            "/citechecks/c1/cancel",
            {
                "citecheck_id": "c1",
                "status": "cancelled",
                "citations": [],
                "citation_issues": [],
                "citation_failures": [],
            },
        ),
    ],
)
async def test_a_review_and_a_citation_check_are_cancelled_too(
    no_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
    submit: str,
    accepted: dict[str, Any],
    poll: str,
    running: dict[str, Any],
    cancel: str,
    cancelled: dict[str, Any],
) -> None:
    polled = asyncio.Event()
    calls = {"n": 0}

    async def answer(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] >= 2:
            return await _blocked(polled)(request)
        return httpx.Response(200, json=running)

    server = Server(
        {("POST", submit): [httpx.Response(202, json=accepted)], ("GET", poll): [answer], ("POST", cancel): [cancelled]}
    )
    caplog.set_level(logging.WARNING, logger="lenz_io")
    async with server.client() as client:
        method = client.review_and_wait if submit == "/review" else client.citecheck_and_wait
        await _cancel_once_polling(method("Draft.", cancel_on_abort=True), polled)
    assert len(server.calls("POST", cancel)) == 1
    assert not [r for r in caplog.records if "cancel_on_abort" in r.message]


async def test_the_waits_own_timeout_never_cancels_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]

    async def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(async_module, "_sleep", sleep)
    monkeypatch.setattr(async_module, "_monotonic", lambda: clock[0])
    server = Server({("GET", "/verify/status/t1"): [RUNNING]})
    async with server.client() as client:
        with pytest.raises(lenz_io.LenzTimeoutError):
            await client.wait("t1", timeout=5, cancel_on_abort=True)
    assert all(not r.url.path.endswith("/cancel") for r in server.requests)


async def test_an_outside_timeout_counts_as_a_cancellation() -> None:
    polled = asyncio.Event()
    server = Server({("GET", "/verify/status/t1"): [_blocked(polled)], ("POST", "/verify/t1/cancel"): [CANCELLED]})
    async with server.client() as client:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(client.wait("t1", cancel_on_abort=True), timeout=0.05)
    assert len(server.calls("POST", "/verify/t1/cancel")) == 1


async def test_a_second_cancellation_leaves_the_cleanup_running_and_aclose_waits_for_it() -> None:
    polled, cancel_sent, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_cancel(request: httpx.Request) -> httpx.Response:
        cancel_sent.set()
        await release.wait()
        return httpx.Response(200, json=CANCELLED)

    server = Server({("GET", "/verify/status/t1"): [_blocked(polled)], ("POST", "/verify/t1/cancel"): [slow_cancel]})
    client = server.client()
    task = asyncio.ensure_future(client.wait("t1", cancel_on_abort=True))
    await polled.wait()
    task.cancel()
    await cancel_sent.wait()
    task.cancel()  # cancelled again while the cleanup runs
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(client._aborts) == 1  # still running, held by the client
    asyncio.get_running_loop().call_later(0.01, release.set)
    await client.aclose()
    assert not client._aborts
    assert len(server.calls("POST", "/verify/t1/cancel")) == 1


async def test_the_cancelled_error_is_the_callers_own() -> None:
    polled = asyncio.Event()
    server = Server({("GET", "/verify/status/t1"): [_blocked(polled)], ("POST", "/verify/t1/cancel"): [CANCELLED]})
    async with server.client() as client:
        task = asyncio.ensure_future(client.wait("t1", cancel_on_abort=True))
        await polled.wait()
        task.cancel("caller left")
        with pytest.raises(asyncio.CancelledError) as info:
            await task
    if sys.version_info >= (3, 11):
        assert info.value.args == ("caller left",)


# ── the connection pool after a cancellation (a real socket) ───────────────


class _Held(BaseHTTPRequestHandler):
    release = threading.Event()

    def do_GET(self) -> None:
        if self.path.endswith("/hold"):
            self.release.wait(5)
        body = json.dumps({"api": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture()
def local_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Held)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        _Held.release.set()
        server.shutdown()
        server.server_close()
        _Held.release.clear()


async def test_a_cancelled_request_gives_its_connection_back(local_server: str) -> None:
    http = httpx.AsyncClient(limits=httpx.Limits(max_connections=1))
    async with AsyncLenz(api_key=KEY, base_url=local_server, http_client=http, max_retries=0) as client:
        task = asyncio.ensure_future(client._request("GET", "/hold"))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.wait_for(client._request("GET", "/fine"), timeout=5) == {"api": "ok"}
    await http.aclose()


# ── many calls on one client ───────────────────────────────────────────────


async def test_concurrent_calls_keep_their_own_keys() -> None:
    server = Server({("POST", "/assess"): [httpx.Response(422, json={"code": "invalid_request", "detail": "x"})]})
    async with server.client() as client:
        outcomes = await asyncio.gather(*(client.assess(claim=f"C{i}.") for i in range(20)), return_exceptions=True)
    keys = [r.headers["Idempotency-Key"] for r in server.requests]
    assert len(set(keys)) == 20
    by_body = {json.loads(r.content)["text"]: r.headers["Idempotency-Key"] for r in server.requests}
    for i, error in enumerate(outcomes):
        assert isinstance(error, LenzError)
        assert error.idempotency_key == by_body[f"C{i}."]


async def test_a_gather_of_waits_polls_in_parallel(no_sleep: list[float]) -> None:
    server = Server(
        {
            ("GET", "/verify/status/a"): [{**RUNNING, "task_id": "a"}, {**DONE, "task_id": "a"}],
            ("GET", "/verify/status/b"): [{**DONE, "task_id": "b"}],
        }
    )
    async with server.client() as client:
        results = await asyncio.gather(client.wait("a"), client.wait("b"))
    assert [r.verification_id for r in results] == ["v1", "v1"]


# ── ownership and lifecycle ────────────────────────────────────────────────


async def test_an_owned_pool_is_closed_and_a_borrowed_one_never() -> None:
    owned = AsyncLenz(api_key=KEY)
    async with owned as same:
        assert same is owned
        await owned.with_options(timeout=5).aclose()
        assert not owned._client.is_closed
    assert owned._client.is_closed

    http = httpx.AsyncClient()
    async with AsyncLenz(api_key=KEY, http_client=http):
        pass
    assert not http.is_closed
    await http.aclose()


def test_there_is_no_sync_close_or_with() -> None:
    client = AsyncLenz(api_key=KEY)
    assert not hasattr(client, "close")
    with pytest.raises((TypeError, AttributeError)):
        with client:  # type: ignore[attr-defined]
            pass


def test_with_options_is_a_plain_method_returning_an_async_client() -> None:
    root = AsyncLenz(api_key=KEY)
    copy = root.with_options(timeout=5)
    assert isinstance(copy, AsyncLenz) and copy._client is root._client
    assert copy.verifications._p is copy and copy._aborts is root._aborts
    with pytest.raises(ValueError, match="AsyncLenz"):
        AsyncLenz(api_key=KEY, timeout=0)


async def test_iter_checks_its_arguments_at_the_call_and_fetches_lazily() -> None:
    item = _load("verifications_list.json")["items"][0]
    server = Server({("GET", "/verifications"): [{"items": [item, item], "total": 9, "page": 1, "page_size": 2}]})
    async with server.client() as client:
        with pytest.raises(ValueError):
            client.verifications.iter(page=0)
        with pytest.raises(ValueError):
            client.library.iter(sort="random")
        pages = client.verifications.iter()
        assert isinstance(pages, AsyncIterator)
        assert server.requests == []
        async for _ in pages:
            break
    assert len(server.requests) == 1


# ── nothing blocks the event loop ──────────────────────────────────────────


def test_the_async_client_never_blocks_the_loop(caplog: pytest.LogCaptureFixture) -> None:
    async def run() -> None:
        loop = asyncio.get_running_loop()
        loop.slow_callback_duration = 0.05
        server = Server(
            {
                ("POST", "/verify"): [httpx.Response(503, json={"detail": "x"}), {"task_id": "t1"}],
                ("GET", "/verify/status/t1"): [RUNNING, DONE],
                ("GET", "/library"): [{"items": [], "total": 0, "page": 1, "page_size": 20}],
            }
        )
        async with server.client() as client:
            await client.verify_and_wait("A.")
            async for _ in client.library.iter():
                pass

    async def fast_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    caplog.set_level(logging.WARNING, logger="asyncio")
    original = async_module._sleep
    async_module._sleep = fast_sleep
    try:
        asyncio.run(run(), debug=True)
    finally:
        async_module._sleep = original
    assert not [r for r in caplog.records if "took" in r.getMessage() and "Executing" in r.getMessage()]


# ── the FastAPI example: a disconnected caller stops the review ────────────


def _load_example() -> types.ModuleType:
    """``examples/core/fastapi_async.py`` with a stand-in for FastAPI (not a
    dependency of this package): only the helper it defines is exercised."""
    fake = types.ModuleType("fastapi")

    class FastAPI:
        def __init__(self, **kwargs: Any) -> None:
            self.state = types.SimpleNamespace()

        def post(self, *args: Any, **kwargs: Any) -> Callable[[Any], Any]:
            return lambda fn: fn

    fake.FastAPI = FastAPI  # type: ignore[attr-defined]
    fake.Request = object  # type: ignore[attr-defined]
    fake.Response = object  # type: ignore[attr-defined]
    sys.modules.setdefault("fastapi", fake)
    path = ROOT / "examples" / "core" / "fastapi_async.py"
    spec = importlib.util.spec_from_file_location("fastapi_async_example", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_example_stops_the_review_when_the_caller_disconnects(no_sleep: list[float]) -> None:
    example = _load_example()
    polled = asyncio.Event()
    running = {"review_id": "r1", "status": "verifying", "issues": [], "failures": [], "claims": []}
    calls = {"n": 0}

    async def answer(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] >= 2:
            return await _blocked(polled)(request)
        return httpx.Response(200, json=running)

    server = Server(
        {
            ("POST", "/review"): [httpx.Response(202, json={"review_id": "r1"})],
            ("GET", "/reviews/r1"): [answer],
            ("POST", "/reviews/r1/cancel"): [{**running, "status": "cancelled"}],
        }
    )

    class Caller:
        async def is_disconnected(self) -> bool:
            return polled.is_set()

    async with server.client() as client:
        result = await example.until_disconnected(
            Caller(), client.review_and_wait("Draft.", cancel_on_abort=True), every=0.01
        )
    assert result is None
    assert len(server.calls("POST", "/reviews/r1/cancel")) == 1


async def test_the_example_returns_the_result_when_the_caller_stays(no_sleep: list[float]) -> None:
    example = _load_example()

    class Caller:
        async def is_disconnected(self) -> bool:
            return False

    async def work() -> str:
        await asyncio.sleep(0.02)
        return "done"

    assert await example.until_disconnected(Caller(), work(), every=0.005) == "done"


@pytest.mark.parametrize("settle", [0, 3])
def test_a_cleanup_cut_off_by_the_loop_closing_is_logged(caplog: pytest.LogCaptureFixture, settle: int) -> None:
    """``asyncio.run`` cancels every task left when ``main`` returns: a
    ``cancel_on_abort`` cleanup cut off that way (before or after it started)
    says so at WARNING with the job id, instead of vanishing."""
    caplog.set_level(logging.WARNING, logger="lenz_io")

    async def main() -> None:
        polled = asyncio.Event()

        async def slow_cancel(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(3600)
            raise AssertionError("never answered")

        server = Server(
            {("GET", "/verify/status/t1"): [_blocked(polled)], ("POST", "/verify/t1/cancel"): [slow_cancel]}
        )
        client = server.client()
        task = asyncio.ensure_future(client.wait("t1", cancel_on_abort=True))
        await polled.wait()
        task.cancel()
        for _ in range(settle):
            await asyncio.sleep(0)
        # returns without awaiting the task or closing the client

    asyncio.run(main())
    records = [r for r in caplog.records if "event loop is closing" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING


async def test_aclose_lets_a_pending_cancel_go_out() -> None:
    polled = asyncio.Event()
    server = Server({("GET", "/verify/status/t1"): [_blocked(polled)], ("POST", "/verify/t1/cancel"): [CANCELLED]})
    client = server.client()
    task = asyncio.ensure_future(client.wait("t1", cancel_on_abort=True))
    await polled.wait()
    task.cancel()
    await asyncio.sleep(0)  # the wait handles its cancellation and starts the cleanup
    await client.aclose()
    assert len(server.calls("POST", "/verify/t1/cancel")) == 1
    with pytest.raises(asyncio.CancelledError):
        await task


# ── wait: the id is known up front, so an early cancellation cancels it ────


def _wait_server() -> Server:
    return Server(
        {
            ("GET", "/verify/status/t1"): [_blocked(asyncio.Event())],
            ("POST", "/verify/t1/cancel"): [CANCELLED],
        }
    )


@pytest.mark.parametrize("flag", [True, False])
@pytest.mark.parametrize("given", ["id", "task_accepted", "batch_item"])
async def test_a_cancellation_requested_before_the_first_poll(flag: bool, given: str) -> None:
    """The task awaiting ``wait`` was already cancelled when ``wait`` began
    (the asyncio form of an aborted signal): with the flag, the run is
    cancelled at once, without a poll; without it, nothing is cancelled."""
    batch = BatchAccepted.model_validate({"batch_id": "b", "items": [{"task_id": "t1", "claim": "A."}]})
    task_arg: Any = {"id": "t1", "task_accepted": TaskAccepted(task_id="t1"), "batch_item": batch.items[0]}[given]
    server = _wait_server()
    async with server.client() as client:

        async def caller() -> None:
            current = asyncio.current_task()
            assert current is not None
            current.cancel()  # delivered at the wait's first await
            await client.wait(task_arg, cancel_on_abort=flag)

        with pytest.raises(asyncio.CancelledError):
            await asyncio.ensure_future(caller())
    cancels = server.calls("POST", "/verify/t1/cancel")
    if flag:
        assert len(cancels) == 1
        assert server.calls("GET", "/verify/status/t1") == []  # cancelled without a poll
    else:
        assert cancels == []


@pytest.mark.parametrize("flag", [True, False])
async def test_a_cancellation_during_the_first_poll(flag: bool) -> None:
    polled = asyncio.Event()
    server = Server({("GET", "/verify/status/t1"): [_blocked(polled)], ("POST", "/verify/t1/cancel"): [CANCELLED]})
    async with server.client() as client:
        await _cancel_once_polling(client.wait("t1", cancel_on_abort=flag), polled)
    assert len(server.calls("GET", "/verify/status/t1")) == 1
    assert len(server.calls("POST", "/verify/t1/cancel")) == (1 if flag else 0)


async def test_a_task_cancelled_before_it_ever_runs_never_enters_wait() -> None:
    """A coroutine cancelled before its first step runs no code at all, so
    ``wait`` cannot see the cancellation, whatever the flag (documented)."""
    server = _wait_server()
    async with server.client() as client:
        task = asyncio.ensure_future(client.wait("t1", cancel_on_abort=True))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert server.requests == []
