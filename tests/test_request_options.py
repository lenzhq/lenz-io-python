"""Request options: ``timeout``, ``max_retries`` and ``extra_headers`` on every
public method, and ``Lenz.with_options``.

* The parity map: every public method and the options it takes, by name.
* Forwarding: a method's options reach every request it makes (submit,
  polls, pages, retries).
* Same bytes: with options set, the URL and body are those sent without them,
  and the headers differ only by the ones asked for.
* Precedence: call over copy over client, the extract / assess floors on an
  inherited timeout only, ``None`` vs ``NOT_GIVEN``.
* Headers, ownership of the shared pool, and validation.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, ClassVar

import httpx
import pytest
from async_adapter import SyncView
from freeze_cases import (
    ACCEPTED,
    ASK_HISTORY,
    ASK_REPLY,
    ASSESSED,
    BATCH,
    CANCEL_CHECK,
    CANCEL_REVIEW,
    CANCEL_TASK,
    CERT,
    CHECK_DONE,
    CID,
    DETAIL,
    DONE,
    DRAFT,
    EXTRACTED,
    LIB_1,
    LIB_2,
    PAGE_1,
    PAGE_2,
    RELATED,
    REVIEW_DONE,
    REVIEW_RUNNING,
    RID,
    RUNNING,
    RUNNING_NO_HINT,
    S503,
    SELECTED,
    TASK,
    USAGE,
)
from freeze_harness import API_KEY, BASE, PINNED, Recording, recording

import lenz_io
from lenz_io import NOT_GIVEN, AsyncLenz, Lenz, NotGiven
from lenz_io.client import EXTRACT_TIMEOUT, WAIT_TIMEOUT

MARK = "X-Marker"


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LENZ_API_KEY", raising=False)
    monkeypatch.delenv("LENZ_BASE_URL", raising=False)


class _Clients:
    """Which client the test runs on (``both_clients``): ``Lenz``, or an
    ``AsyncLenz`` driven through ``SyncView`` on ``loop``."""

    use_async = False
    loop: asyncio.AbstractEventLoop | None = None


@pytest.fixture(autouse=True, params=["sync", "async"])
def both_clients(request: pytest.FixtureRequest) -> Iterator[str]:
    """Every test here runs on both clients, except the ones marked
    ``sync_only`` (subclassing ``Lenz`` itself, or its sync-only surface)."""
    if request.param == "async" and request.node.get_closest_marker("sync_only"):
        pytest.skip("sync client only")
    _Clients.use_async = request.param == "async"
    _Clients.loop = asyncio.new_event_loop() if _Clients.use_async else None
    try:
        yield request.param
    finally:
        if _Clients.loop is not None:
            _Clients.loop.close()
        _Clients.use_async = False
        _Clients.loop = None


def make(**kwargs: Any) -> Any:
    """``Lenz(**kwargs)``, or the same ``AsyncLenz`` behind ``SyncView``."""
    if not _Clients.use_async:
        return Lenz(**kwargs)
    assert _Clients.loop is not None
    return SyncView(AsyncLenz(**kwargs), _Clients.loop)


def close_http(http: Any) -> None:
    if isinstance(http, httpx.AsyncClient):
        assert _Clients.loop is not None
        _Clients.loop.run_until_complete(http.aclose())
    else:
        http.close()


def make_http(**kwargs: Any) -> Any:
    """An ``httpx.Client``, or an ``httpx.AsyncClient`` for the async run."""
    return httpx.AsyncClient(**kwargs) if _Clients.use_async else httpx.Client(**kwargs)


@contextmanager
def recorded(answers: dict[tuple[str, str], list[Any]]) -> Iterator[Recording]:
    """Record the requests of the block, under a fake clock patched for the
    block only (so a test's own monkeypatches are left alone)."""
    with (
        pytest.MonkeyPatch.context() as mp,
        recording(mp, answers, async_client=_Clients.use_async) as rec,
    ):
        yield rec


def _headers(request: dict[str, Any]) -> dict[str, str]:
    return {name.lower(): value for name, value in request["headers"]}


# ── the parity map ─────────────────────────────────────────────────────────
#
# Every public method, the Node SDK's name for it, and the request options it
# takes. ``call``: all three, ``timeout`` = one HTTP attempt (``None`` =
# inherit). ``wait``: ``timeout`` is the wait budget and only ``extra_headers``
# is new (each poll is one request, so no ``max_retries``). ``submit_wait``:
# ``timeout`` is the wait budget, ``max_retries`` the submit's.
CALL = {"timeout", "max_retries", "extra_headers"}
PARITY: dict[str, tuple[str, str]] = {
    "verify": ("verify", "call"),
    "verify_batch": ("verifyBatch", "call"),
    "extract": ("extract", "call"),
    "assess": ("assess", "call"),
    "select": ("select", "call"),
    "get_status": ("getStatus", "call"),
    "cancel": ("cancel", "call"),
    "review": ("review", "call"),
    "get_review": ("getReview", "call"),
    "cancel_review": ("cancelReview", "call"),
    "citecheck": ("citecheck", "call"),
    "get_citecheck": ("getCitecheck", "call"),
    "cancel_citecheck": ("cancelCitecheck", "call"),
    "usage": ("usage", "call"),
    "wait": ("wait", "wait"),
    "verify_and_wait": ("verifyAndWait", "submit_wait"),
    "verify_batch_and_wait": ("verifyBatchAndWait", "submit_wait"),
    "review_and_wait": ("reviewAndWait", "submit_wait"),
    "citecheck_and_wait": ("citecheckAndWait", "submit_wait"),
    "verifications.list": ("verifications.list", "call"),
    "verifications.iter": ("verifications.listAll", "call"),
    "verifications.get": ("verifications.get", "call"),
    "verifications.get_certificate": ("verifications.getCertificate", "call"),
    "verifications.delete": ("verifications.delete", "call"),
    "verifications.related": ("verifications.related", "call"),
    "ask.history": ("ask.history", "call"),
    "ask.send": ("ask.send", "call"),
    "ask.reset": ("ask.reset", "call"),
    "library.list": ("library.list", "call"),
    "library.iter": ("library.listAll", "call"),
    "with_options": ("withOptions", "with_options"),
    "close": ("(none)", "lifecycle"),
}


def _public_methods() -> dict[str, Callable[..., Any]]:
    client = Lenz(api_key=API_KEY)  # the sync surface (the async one: test_async_surface.py)
    out: dict[str, Callable[..., Any]] = {}
    for name, value in inspect.getmembers(client):
        if name.startswith("_"):
            continue
        if name in ("verifications", "ask", "library"):
            for sub, method in inspect.getmembers(value):
                if not sub.startswith("_") and callable(method):
                    out[f"{name}.{sub}"] = method
        elif callable(value):
            out[name] = value
    client.close()
    return out


@pytest.mark.sync_only
def test_every_public_method_is_in_the_parity_map() -> None:
    assert sorted(_public_methods()) == sorted(PARITY)


@pytest.mark.sync_only
@pytest.mark.parametrize("name", sorted(PARITY))
def test_each_method_takes_the_options_the_map_says(name: str) -> None:
    kind = PARITY[name][1]
    params = inspect.signature(_public_methods()[name]).parameters
    options = {n: p for n, p in params.items() if n in CALL}
    if kind == "lifecycle":
        assert options == {}
        return
    for p in options.values():
        assert p.kind is inspect.Parameter.KEYWORD_ONLY, (name, p.name)
    if kind == "call":
        assert set(options) == CALL
        assert all(p.default is None for p in options.values())
    elif kind == "wait":
        assert set(options) == {"timeout", "extra_headers"}
        assert options["timeout"].default == WAIT_TIMEOUT  # the wait budget, unchanged
        assert options["extra_headers"].default is None
    elif kind == "submit_wait":
        assert set(options) == CALL
        assert isinstance(options["timeout"].default, float)  # the wait budget, unchanged
        assert options["max_retries"].default is None and options["extra_headers"].default is None
    else:
        assert kind == "with_options"
        assert set(options) == CALL
        assert options["timeout"].default is NOT_GIVEN
        assert options["max_retries"].default is NOT_GIVEN
        assert options["extra_headers"].default is None


@pytest.mark.sync_only
@pytest.mark.skipif(sys.version_info < (3, 11), reason="typing.get_overloads is 3.11+")
def test_get_review_overloads_all_take_the_options() -> None:
    from typing import get_overloads  # type: ignore[attr-defined,unused-ignore]

    overloads = get_overloads(Lenz.get_review)
    assert len(overloads) == 3
    for fn in overloads:
        assert CALL <= set(inspect.signature(fn).parameters)


# ── every method, run with and without options ─────────────────────────────

Answers = dict[tuple[str, str], list[Any]]
Run = Callable[[Lenz, dict[str, Any]], Any]

# Each method: how to call it, its scripted answers (a 503 first wherever the
# call retries, so a retry is a recorded request too), and its kind.
CALLS: dict[str, tuple[Run, Answers]] = {
    "verify": (lambda c, o: c.verify("A.", idempotency_key=PINNED, **o), {("POST", "/verify"): [S503, ACCEPTED]}),
    "verify_batch": (
        lambda c, o: c.verify_batch(claims=[{"claim": "A."}], idempotency_key=PINNED, **o),
        {("POST", "/verify/batch"): [S503, BATCH]},
    ),
    "extract": (
        lambda c, o: c.extract(text="Doc.", idempotency_key=PINNED, **o),
        {("POST", "/extract"): [S503, EXTRACTED]},
    ),
    "assess": (lambda c, o: c.assess("A.", idempotency_key=PINNED, **o), {("POST", "/assess"): [S503, ASSESSED]}),
    "select": (
        lambda c, o: c.select(TASK, claims=["A."], idempotency_key=PINNED, **o),
        {("POST", f"/verify/{TASK}/select"): [S503, SELECTED]},
    ),
    "get_status": (lambda c, o: c.get_status(TASK, **o), {("GET", f"/verify/status/{TASK}"): [S503, DONE]}),
    "cancel": (
        lambda c, o: c.cancel(CANCEL_TASK["task_id"], **o),
        {("POST", f"/verify/{CANCEL_TASK['task_id']}/cancel"): [S503, (200, CANCEL_TASK)]},
    ),
    "review": (
        lambda c, o: c.review(DRAFT, idempotency_key=PINNED, **o),
        {("POST", "/review"): [S503, (202, {"review_id": RID, "status": "queued"})]},
    ),
    "get_review": (
        lambda c, o: c.get_review(RID, view="issues", **o),
        {("GET", f"/reviews/{RID}"): [S503, (200, REVIEW_DONE)]},
    ),
    "cancel_review": (
        lambda c, o: c.cancel_review(CANCEL_REVIEW["review_id"], **o),
        {("POST", f"/reviews/{CANCEL_REVIEW['review_id']}/cancel"): [S503, (200, CANCEL_REVIEW)]},
    ),
    "citecheck": (
        lambda c, o: c.citecheck(DRAFT, idempotency_key=PINNED, **o),
        {("POST", "/citecheck"): [S503, (202, {"citecheck_id": CID, "status": "queued"})]},
    ),
    "get_citecheck": (
        lambda c, o: c.get_citecheck(CID, **o),
        {("GET", f"/citechecks/{CID}"): [S503, (200, CHECK_DONE)]},
    ),
    "cancel_citecheck": (
        lambda c, o: c.cancel_citecheck(CID, **o),
        {("POST", f"/citechecks/{CID}/cancel"): [S503, (200, CANCEL_CHECK)]},
    ),
    "usage": (lambda c, o: c.usage(**o), {("GET", "/me/usage"): [S503, USAGE]}),
    "wait": (lambda c, o: c.wait(TASK, **o), {("GET", f"/verify/status/{TASK}"): [S503, RUNNING, DONE]}),
    "verify_and_wait": (
        lambda c, o: c.verify_and_wait("A.", idempotency_key=PINNED, **o),
        {("POST", "/verify"): [S503, ACCEPTED], ("GET", f"/verify/status/{TASK}"): [S503, RUNNING, DONE]},
    ),
    "verify_batch_and_wait": (
        lambda c, o: c.verify_batch_and_wait(claims=[{"claim": "A."}, {"claim": "B."}], idempotency_key=PINNED, **o),
        {
            ("POST", "/verify/batch"): [S503, BATCH],
            ("GET", f"/verify/status/{TASK}"): [RUNNING, DONE],
            ("GET", "/verify/status/t2"): [S503, (200, {"status": "completed", "task_id": "t2", "result": {}})],
        },
    ),
    "review_and_wait": (
        lambda c, o: c.review_and_wait(DRAFT, idempotency_key=PINNED, **o),
        {
            ("POST", "/review"): [S503, (202, {"review_id": RID, "status": "queued"})],
            ("GET", f"/reviews/{RID}"): [(200, REVIEW_RUNNING), S503, (200, REVIEW_DONE)],
        },
    ),
    "citecheck_and_wait": (
        lambda c, o: c.citecheck_and_wait(DRAFT, idempotency_key=PINNED, **o),
        {
            ("POST", "/citecheck"): [S503, (202, {"citecheck_id": CID, "status": "queued"})],
            ("GET", f"/citechecks/{CID}"): [S503, (200, CHECK_DONE)],
        },
    ),
    "verifications.list": (
        lambda c, o: c.verifications.list(page=1, **o),
        {("GET", "/verifications"): [S503, PAGE_1]},
    ),
    "verifications.iter": (
        lambda c, o: list(c.verifications.iter(**o)),
        {("GET", "/verifications"): [S503, PAGE_1, S503, PAGE_2]},
    ),
    "verifications.get": (lambda c, o: c.verifications.get("v1", **o), {("GET", "/verifications/v1"): [S503, DETAIL]}),
    "verifications.get_certificate": (
        lambda c, o: c.verifications.get_certificate("v1", **o),
        {("GET", "/verifications/v1/certificate"): [S503, CERT]},
    ),
    "verifications.delete": (
        lambda c, o: c.verifications.delete("v1", **o),
        {("DELETE", "/verifications/v1"): [S503, (200, {"ok": True})]},
    ),
    "verifications.related": (
        lambda c, o: c.verifications.related("v1", limit=3, **o),
        {("GET", "/verifications/v1/related"): [S503, RELATED]},
    ),
    "ask.history": (lambda c, o: c.ask.history("v1", **o), {("GET", "/ask/v1"): [S503, ASK_HISTORY]}),
    "ask.send": (
        lambda c, o: c.ask.send("v1", message="Why?", idempotency_key=PINNED, **o),
        {("POST", "/ask/v1"): [S503, ASK_REPLY]},
    ),
    "ask.reset": (lambda c, o: c.ask.reset("v1", **o), {("DELETE", "/ask/v1"): [S503, (200, {"ok": True})]}),
    "library.list": (lambda c, o: c.library.list(search="x", **o), {("GET", "/library"): [S503, LIB_1]}),
    "library.iter": (
        lambda c, o: list(c.library.iter(search="x", **o)),
        {("GET", "/library"): [S503, LIB_1, S503, LIB_2]},
    ),
}


def _options_for(name: str, *, headers: dict[str, str | None] | None = None) -> dict[str, Any]:
    """Every option the method takes, set: a per-attempt timeout of 7 s, two
    retries and a marker header."""
    kind = PARITY[name][1]
    opts: dict[str, Any] = {"extra_headers": headers if headers is not None else {MARK: "m"}}
    if kind in ("call", "submit_wait"):
        opts["max_retries"] = 2
    if kind == "call":
        opts["timeout"] = 7
    return opts


def _run(monkeypatch: pytest.MonkeyPatch, name: str, opts: dict[str, Any], client: Lenz | None = None) -> Any:
    call, answers = CALLS[name]
    with recorded(answers) as rec:
        c = client or make(api_key=API_KEY)
        call(c, opts)
    return rec


@pytest.mark.sync_only
def test_every_method_with_options_is_exercised() -> None:
    assert sorted(CALLS) == sorted(n for n, (_, kind) in PARITY.items() if kind not in ("with_options", "lifecycle"))


@pytest.mark.parametrize("name", sorted(CALLS))
def test_options_reach_every_request_the_method_makes(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    rec = _run(monkeypatch, name, _options_for(name))
    assert len(rec.requests) >= 2  # a retry, a poll or a second page
    for request in rec.requests:
        assert _headers(request).get(MARK.lower()) == "m", (name, request["url"])


@pytest.mark.parametrize("name", sorted(CALLS))
def test_a_copys_options_reach_every_request_too(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    root = make(api_key=API_KEY)
    rec = _run(monkeypatch, name, {}, client=root.with_options(extra_headers={MARK: "copy"}))
    for request in rec.requests:
        assert _headers(request).get(MARK.lower()) == "copy", (name, request["url"])


@pytest.mark.parametrize("name", sorted(CALLS))
def test_options_change_nothing_but_the_headers_asked_for(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Empty options send exactly the request of a call without them; set
    options send the same URLs and bodies, with only the marker header
    added. The timeouts differ only where a per-attempt timeout was given."""
    plain = _run(monkeypatch, name, {}).requests
    empty_opts: dict[str, Any] = {"extra_headers": {}}
    if PARITY[name][1] != "wait":
        empty_opts["max_retries"] = None
    if PARITY[name][1] == "call":
        empty_opts["timeout"] = None
    empty = _run(monkeypatch, name, empty_opts).requests
    assert empty == plain

    full = _run(monkeypatch, name, _options_for(name)).requests
    assert [(r["method"], r["url"], r["body"]) for r in full] == [(r["method"], r["url"], r["body"]) for r in plain]
    for with_opts, without in zip(full, plain, strict=True):
        assert [h for h in with_opts["headers"] if h[0] != MARK] == without["headers"]
        assert [h for h in with_opts["headers"] if h[0] == MARK] == [[MARK, "m"]]
        if PARITY[name][1] == "call":
            assert with_opts["timeout"] == {"connect": 7, "read": 7, "write": 7, "pool": 7}
        else:
            assert with_opts["timeout"] == without["timeout"]


@pytest.mark.parametrize("name", sorted(n for n in CALLS if PARITY[n][1] != "wait"))
def test_max_retries_reaches_the_request_it_retries(name: str) -> None:
    """Every scripted call answers 503 first: with ``max_retries=0`` that is
    the one request made, and the call fails with it."""
    call, answers = CALLS[name]
    with recorded(answers) as rec, pytest.raises(lenz_io.LenzAPIError):
        call(make(api_key=API_KEY), {"max_retries": 0})
    assert len(rec.requests) == 1


# ── the budget table: what each option reaches ─────────────────────────────


def _count(monkeypatch: pytest.MonkeyPatch, answers: Answers, call: Callable[[Lenz], Any], client: Lenz) -> Any:
    with recorded(answers) as rec:
        try:
            call(client)
        except lenz_io.LenzError:
            pass
    return rec


class TestBudgets:
    def test_max_retries_replaces_the_clients_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answers: Answers = {("GET", "/me/usage"): [S503]}
        client = make(api_key=API_KEY)
        assert len(_count(monkeypatch, answers, lambda c: c.usage(), client).requests) == 4
        assert len(_count(monkeypatch, answers, lambda c: c.usage(max_retries=0), client).requests) == 1
        assert len(_count(monkeypatch, answers, lambda c: c.usage(max_retries=5), client).requests) == 6
        assert len(_count(monkeypatch, answers, lambda c: c.with_options(max_retries=1).usage(), client).requests) == 2
        copy = client.with_options(max_retries=1)
        assert len(_count(monkeypatch, answers, lambda c: copy.usage(max_retries=0), client).requests) == 1

    def test_a_waits_max_retries_is_the_submits_and_never_the_polls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answers: Answers = {
            ("POST", "/verify"): [S503, S503, ACCEPTED],
            ("GET", f"/verify/status/{TASK}"): [S503, S503, DONE],
        }
        rec = _count(
            monkeypatch, answers, lambda c: c.verify_and_wait("A.", max_retries=5, timeout=60), make(api_key=API_KEY)
        )
        submits = [r for r in rec.requests if r["method"] == "POST"]
        polls = [r for r in rec.requests if r["method"] == "GET"]
        assert len(submits) == 3
        # Each poll is one request: the two 503s are two rounds, with a poll sleep between.
        assert len(polls) == 3 and rec.clock.sleeps[-2:] == [2.0, 4.0]

    def test_max_retries_0_on_a_wait_submits_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answers: Answers = {("POST", "/review"): [S503]}
        rec = _count(monkeypatch, answers, lambda c: c.review_and_wait(DRAFT, max_retries=0), make(api_key=API_KEY))
        assert len(rec.requests) == 1

    def test_a_per_call_timeout_reaches_each_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _count(
            monkeypatch,
            {("GET", "/verifications"): [PAGE_1, PAGE_2]},
            lambda c: list(c.verifications.iter(timeout=4)),
            make(api_key=API_KEY),
        )
        assert [r["timeout"]["read"] for r in rec.requests] == [4, 4]

    def test_a_waits_timeout_stays_the_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ``timeout=12`` is how long to wait: the polls' own timeout stays the
        # client's 30 s, capped by what is left of the 12 s.
        rec = _count(
            monkeypatch,
            {("GET", f"/verify/status/{TASK}"): [RUNNING_NO_HINT]},
            lambda c: c.wait(TASK, timeout=12),
            make(api_key=API_KEY),
        )
        assert [r["timeout"]["read"] for r in rec.requests] == [12, 10, 6]

    def test_the_submit_of_a_wait_takes_the_attempt_timeout_not_the_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _count(
            monkeypatch,
            {("POST", "/verify"): [ACCEPTED], ("GET", f"/verify/status/{TASK}"): [DONE]},
            lambda c: c.with_options(timeout=45).verify_and_wait("A.", timeout=5),
            make(api_key=API_KEY),
        )
        assert rec.requests[0]["timeout"]["read"] == 45  # the submit: the copy's attempt timeout
        assert rec.requests[1]["timeout"]["read"] == 5  # the poll: capped by the wait


# ── precedence ─────────────────────────────────────────────────────────────


def _timeout_of(monkeypatch: pytest.MonkeyPatch, call: Callable[[], Any], answers: Answers) -> dict[str, Any]:
    with recorded(answers) as rec:
        call()
    return rec.requests[0]["timeout"]


def _all(value: float | None) -> dict[str, float | None]:
    return {"connect": value, "read": value, "write": value, "pool": value}


_USAGE: Answers = {("GET", "/me/usage"): [USAGE]}
_EXTRACT: Answers = {("POST", "/extract"): [EXTRACTED]}
_ASSESS: Answers = {("POST", "/assess"): [ASSESSED]}


class TestTimeoutPrecedence:
    @pytest.mark.parametrize(
        ("build", "expected"),
        [
            (lambda c: c.usage(), _all(30.0)),
            (lambda c: c.usage(timeout=7), _all(7)),
            (lambda c: c.usage(timeout=None), _all(30.0)),  # None per call = inherit
            (lambda c: c.usage(timeout=httpx.Timeout(None)), _all(None)),  # unbounded for one call
            (lambda c: c.usage(timeout=httpx.Timeout(3, read=9)), {"connect": 3, "read": 9, "write": 3, "pool": 3}),
            (lambda c: c.with_options(timeout=12).usage(), _all(12)),
            (lambda c: c.with_options(timeout=12).usage(timeout=7), _all(7)),
            (lambda c: c.with_options(timeout=12).usage(timeout=None), _all(12)),
            (lambda c: c.with_options(timeout=None).usage(), _all(None)),  # None on a copy = unbounded
            (lambda c: c.with_options(timeout=None).with_options().usage(), _all(None)),  # NOT_GIVEN keeps it
            (lambda c: c.with_options(timeout=12).with_options(max_retries=0).usage(), _all(12)),
            (lambda c: c.with_options(timeout=12).with_options(timeout=20).usage(), _all(20)),
            (lambda c: c.with_options(timeout=12).with_options(timeout=NOT_GIVEN).usage(), _all(12)),
        ],
    )
    def test_a_plain_call(self, monkeypatch: pytest.MonkeyPatch, build: Any, expected: dict[str, Any]) -> None:
        client = make(api_key=API_KEY)
        assert _timeout_of(monkeypatch, lambda: build(client), _USAGE) == expected

    @pytest.mark.parametrize(
        ("constructor", "build", "expected"),
        [
            (30.0, lambda c: c.extract(text="Doc."), _all(EXTRACT_TIMEOUT)),
            (30.0, lambda c: c.extract(text="Doc.", timeout=5), _all(5)),  # explicit: used as given, below the floor
            (30.0, lambda c: c.with_options(timeout=200).extract(text="Doc."), _all(200)),
            # Since 3.2 a copy's timeout is explicit too: used as given, below the floor.
            (30.0, lambda c: c.with_options(timeout=20).extract(text="Doc."), _all(20)),
            (30.0, lambda c: c.with_options(timeout=None).extract(text="Doc."), _all(None)),
            (30.0, lambda c: c.with_options(timeout=20).extract(text="Doc.", timeout=9), _all(9)),
            (
                30.0,
                lambda c: c.with_options(timeout=httpx.Timeout(200, read=5)).extract(text="Doc."),
                {"connect": 200, "read": 5, "write": 200, "pool": 200},
            ),
            (
                30.0,
                lambda c: c.with_options(timeout=httpx.Timeout(5, read=200)).extract(text="Doc."),
                {"connect": 5, "read": 200, "write": 5, "pool": 5},
            ),
            (300.0, lambda c: c.extract(text="Doc."), _all(300.0)),
            (300.0, lambda c: c.with_options(timeout=20).extract(text="Doc."), _all(20)),
            # The constructor's timeout is the client's default: still floored.
            (10.0, lambda c: c.extract(text="Doc."), _all(EXTRACT_TIMEOUT)),
        ],
    )
    def test_extract_floor_applies_to_an_inherited_timeout_only(
        self, monkeypatch: pytest.MonkeyPatch, constructor: float, build: Any, expected: dict[str, Any]
    ) -> None:
        client = make(api_key=API_KEY, timeout=constructor)
        assert _timeout_of(monkeypatch, lambda: build(client), _EXTRACT) == expected

    def test_no_assess_floor_on_a_copy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Since 3.2 the floor applies to the client's default timeout only: a
        copy's timeout and a call's are used as given (3.1 floored a copy's)."""
        client = make(api_key=API_KEY)
        assert _timeout_of(monkeypatch, lambda: client.with_options(timeout=50).assess("A."), _ASSESS) == _all(50)
        assert _timeout_of(monkeypatch, lambda: client.assess("A.", timeout=20), _ASSESS) == _all(20)
        assert _timeout_of(monkeypatch, lambda: client.assess("A."), _ASSESS) == _all(100.0)
        assert _timeout_of(monkeypatch, lambda: make(api_key=API_KEY, timeout=10).assess("A."), _ASSESS) == _all(100.0)

    def test_a_constructor_timeout_applies_through_a_borrowed_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Since 3.2 ``timeout=`` with a borrowed ``http_client=`` is sent on
        each request (3.1 ignored it); the borrowed client is not changed."""
        http = make_http(timeout=300.0)
        client = make(api_key=API_KEY, http_client=http, timeout=12)
        assert _timeout_of(monkeypatch, lambda: client.usage(), _USAGE) == _all(12)
        assert _timeout_of(monkeypatch, lambda: client.extract(text="Doc."), _EXTRACT) == _all(EXTRACT_TIMEOUT)
        assert _timeout_of(monkeypatch, lambda: client.with_options(timeout=4).usage(), _USAGE) == _all(4)
        assert http.timeout.read == 300.0
        # 30.0 passed explicitly counts too; ``None`` is no timeout.
        assert _timeout_of(
            monkeypatch, lambda: make(api_key=API_KEY, http_client=http, timeout=30.0).usage(), _USAGE
        ) == _all(30.0)
        assert _timeout_of(
            monkeypatch, lambda: make(api_key=API_KEY, http_client=http, timeout=None).usage(), _USAGE
        ) == _all(None)
        # Left out, the borrowed client's own applies.
        assert _timeout_of(monkeypatch, lambda: make(api_key=API_KEY, http_client=http).usage(), _USAGE) == _all(300.0)

    def test_a_constructor_timeout_through_a_borrowed_client_bounds_a_waits_polls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = make(api_key=API_KEY, http_client=make_http(timeout=300.0), timeout=httpx.Timeout(4, read=8))
        with recorded({("GET", f"/verify/status/{TASK}"): [RUNNING_NO_HINT, DONE]}) as rec:
            client.wait(TASK, timeout=60)
        assert [r["timeout"] for r in rec.requests] == [{"connect": 4, "read": 8, "write": 4, "pool": 4}] * 2

    def test_a_borrowed_clients_own_timeout_counts_below_a_copy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = make(api_key=API_KEY, http_client=make_http(timeout=300.0))
        assert _timeout_of(monkeypatch, lambda: client.extract(text="Doc."), _EXTRACT) == _all(300.0)
        assert _timeout_of(monkeypatch, lambda: client.with_options(timeout=40).usage(), _USAGE) == _all(40)

    def test_a_copys_timeout_bounds_a_waits_polls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = make(api_key=API_KEY).with_options(timeout=httpx.Timeout(4, read=8))
        with recorded({("GET", f"/verify/status/{TASK}"): [RUNNING_NO_HINT, DONE]}) as rec:
            client.wait(TASK, timeout=60)
        assert [r["timeout"] for r in rec.requests] == [{"connect": 4, "read": 8, "write": 4, "pool": 4}] * 2

    def test_a_zero_budget_poll_uses_the_copys_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = make(api_key=API_KEY).with_options(timeout=9)
        with recorded({("GET", f"/verify/status/{TASK}"): [DONE]}) as rec:
            client.wait(TASK, timeout=0)
        assert rec.requests[0]["timeout"] == _all(9)


# ── headers ────────────────────────────────────────────────────────────────


def _sent_headers(monkeypatch: pytest.MonkeyPatch, call: Callable[[], Any]) -> list[list[str]]:
    with recorded(_USAGE) as rec:
        call()
    return rec.requests[0]["headers"]


class TestHeaders:
    def test_merged_case_insensitively_the_calls_spelling_and_value_win(self, monkeypatch: pytest.MonkeyPatch) -> None:
        copy = make(api_key=API_KEY).with_options(extra_headers={"x-a": "copy", "X-B": "b"})
        sent = _sent_headers(monkeypatch, lambda: copy.usage(extra_headers={"X-A": "call"}))
        names = [h[0] for h in sent]
        assert ["X-A", "call"] in sent and ["X-B", "b"] in sent
        assert [n.lower() for n in names].count("x-a") == 1

    def test_a_copy_of_a_copy_merges_over_its_parent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        parent = make(api_key=API_KEY).with_options(extra_headers={"X-A": "1", "X-B": "2"})
        child = parent.with_options(extra_headers={"x-b": "3", "X-A": None})
        sent = _sent_headers(monkeypatch, child.usage)
        assert ["x-b", "3"] in sent
        assert "x-a" not in [h[0].lower() for h in sent]
        # The parent is unchanged.
        assert ["X-A", "1"] in _sent_headers(monkeypatch, parent.usage)

    def test_none_removes_a_copy_header_and_nothing_else(self, monkeypatch: pytest.MonkeyPatch) -> None:
        copy = make(api_key=API_KEY).with_options(extra_headers={"X-A": "1"})
        sent = _sent_headers(monkeypatch, lambda: copy.usage(extra_headers={"x-a": None, "User-Agent": None}))
        lower = [h[0].lower() for h in sent]
        assert "x-a" not in lower
        assert "user-agent" in lower  # a default is not removed by None

    def test_an_option_header_replaces_a_default_in_any_casing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = make(api_key=API_KEY)
        sent = _sent_headers(monkeypatch, lambda: client.usage(extra_headers={"user-agent": "mine/1", "ACCEPT": "x/y"}))
        uas = [h for h in sent if h[0].lower() == "user-agent"]
        accepts = [h for h in sent if h[0].lower() == "accept"]
        assert uas == [["user-agent", "mine/1"]] and accepts == [["ACCEPT", "x/y"]]

    def test_content_type_is_sent_only_with_a_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Since 3.2 (3.1 sent it on every request)."""
        sent = _sent_headers(monkeypatch, lambda: make(api_key=API_KEY).usage(extra_headers={MARK: "m"}))
        assert "content-type" not in [h[0].lower() for h in sent]

    def test_the_shared_client_is_never_changed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        root = make(api_key=API_KEY)
        before = (list(root._client.headers.raw), root._client.timeout)
        copy = root.with_options(timeout=3, max_retries=0, extra_headers={"User-Agent": "x", MARK: "m"})
        _sent_headers(monkeypatch, copy.usage)
        assert (list(root._client.headers.raw), root._client.timeout) == before
        assert MARK.lower() not in [h[0].lower() for h in _sent_headers(monkeypatch, root.usage)]

    @pytest.mark.parametrize(
        "name",
        [
            "X-Lenz-API-Version",
            "idempotency-key",
            "Authorization",
            "content-type",
            "Content-Length",
            "HOST",
            "Transfer-Encoding",
        ],
    )
    def test_reserved_names_are_refused_everywhere(self, name: str) -> None:
        client = make(api_key=API_KEY)
        for call in (
            lambda: client.usage(extra_headers={name: "x"}),
            lambda: client.verify("A.", extra_headers={name: "x"}),
            lambda: client.wait(TASK, extra_headers={name: "x"}),
            lambda: client.review_and_wait(DRAFT, extra_headers={name: "x"}),
            lambda: client.verifications.iter(extra_headers={name: "x"}),
            lambda: client.with_options(extra_headers={name: "x"}),
        ):
            with pytest.raises(ValueError, match="set by the SDK"):
                call()


# ── ownership of the shared pool ───────────────────────────────────────────


class TestOwnership:
    def test_the_root_that_made_the_pool_closes_it(self) -> None:
        root = make(api_key=API_KEY)
        root.close()
        assert root._client.is_closed

    def test_a_borrowed_pool_is_never_closed(self) -> None:
        http = make_http()
        root = make(api_key=API_KEY, http_client=http)
        root.with_options(timeout=5).close()
        root.close()
        assert not http.is_closed
        close_http(http)

    def test_a_copys_close_and_with_do_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        root = make(api_key=API_KEY)
        with root.with_options(max_retries=0) as copy:
            copy.close()
        assert not root._client.is_closed
        sibling = root.with_options(timeout=5)
        root.with_options().close()
        with recorded(_USAGE):
            sibling.usage()
        root.close()

    def test_a_copy_after_the_root_closed_fails_with_httpxs_error(self) -> None:
        root = make(api_key=API_KEY)
        copy = root.with_options(timeout=5)
        root.close()
        with pytest.raises(RuntimeError, match="closed"):
            copy.usage()

    def test_parent_and_siblings_are_isolated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        root = make(api_key=API_KEY)
        a = root.with_options(timeout=3)
        b = root.with_options(timeout=4)
        assert _timeout_of(monkeypatch, a.usage, _USAGE) == _all(3)
        assert _timeout_of(monkeypatch, b.usage, _USAGE) == _all(4)
        assert _timeout_of(monkeypatch, root.usage, _USAGE) == _all(30.0)
        assert root._client is a._client is b._client

    @pytest.mark.sync_only
    def test_a_subclass_and_its_namespaces_are_kept(self) -> None:
        class MyLenz(Lenz):
            def hello(self) -> str:
                return "hi"

        root = MyLenz(api_key=API_KEY)
        copy = root.with_options(timeout=5)
        assert isinstance(copy, MyLenz) and copy.hello() == "hi"
        assert copy.verifications._p is copy and copy.ask._p is copy and copy.library._p is copy
        assert root.verifications._p is root


# ── validation ─────────────────────────────────────────────────────────────

_BAD_TIMEOUTS = [0, -1, 0.0, math.nan, math.inf, -math.inf, True, "5"]
_BAD_RETRIES = [-1, 1.5, True, "2"]
_BAD_HEADERS: list[Any] = [["X-A"], {"X-A": 5}, {5: "x"}, {"": "x"}]


@pytest.fixture()
def no_key_minted(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> Any:
        raise AssertionError("a key was minted before validation")

    monkeypatch.setattr("lenz_io.client.uuid.uuid4", refuse)


def _per_call_entry_points(client: Lenz) -> list[Callable[..., Any]]:
    return [
        lambda **o: client.verify("A.", **o),
        lambda **o: client.assess("A.", **o),
        lambda **o: client.extract(text="Doc.", **o),
        lambda **o: client.review(DRAFT, **o),
        lambda **o: client.citecheck(DRAFT, **o),
        lambda **o: client.ask.send("v1", message="Why?", **o),
        lambda **o: client.usage(**o),
        lambda **o: client.verifications.iter(**o),
        lambda **o: client.library.iter(**o),
    ]


class TestValidation:
    @pytest.mark.parametrize("bad", _BAD_TIMEOUTS)
    def test_a_bad_timeout_is_refused_before_any_request(self, bad: Any, no_key_minted: None) -> None:
        client = make(api_key=API_KEY)
        with httpx_refused():
            for call in _per_call_entry_points(client):
                with pytest.raises(ValueError, match="timeout"):
                    call(timeout=bad)
            with pytest.raises(ValueError, match="timeout"):
                client.with_options(timeout=bad)
            with pytest.raises(ValueError, match="timeout"):
                make(api_key=API_KEY, timeout=bad)

    @pytest.mark.parametrize("bad", _BAD_RETRIES)
    def test_a_bad_retry_count_is_refused_before_any_request(self, bad: Any, no_key_minted: None) -> None:
        client = make(api_key=API_KEY)
        with httpx_refused():
            for call in _per_call_entry_points(client):
                with pytest.raises(ValueError, match="max_retries"):
                    call(max_retries=bad)
            for call in (
                lambda: client.verify_and_wait("A.", max_retries=bad),
                lambda: client.verify_batch_and_wait(claims=[{"claim": "A."}], max_retries=bad),
                lambda: client.review_and_wait(DRAFT, max_retries=bad),
                lambda: client.citecheck_and_wait(DRAFT, max_retries=bad),
                lambda: client.with_options(max_retries=bad),
                lambda: make(api_key=API_KEY, max_retries=bad),
            ):
                with pytest.raises(ValueError, match="max_retries"):
                    call()

    def test_with_options_refuses_none_retries(self) -> None:
        with pytest.raises(ValueError, match="max_retries"):
            make(api_key=API_KEY).with_options(max_retries=None)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", _BAD_HEADERS)
    def test_bad_headers_are_refused_before_any_request(self, bad: Any, no_key_minted: None) -> None:
        client = make(api_key=API_KEY)
        with httpx_refused():
            for call in _per_call_entry_points(client):
                with pytest.raises(ValueError):
                    call(extra_headers=bad)
            for call in (
                lambda: client.wait(TASK, extra_headers=bad),
                lambda: client.verify_and_wait("A.", extra_headers=bad),
                lambda: client.with_options(extra_headers=bad),
            ):
                with pytest.raises(ValueError):
                    call()

    @pytest.mark.parametrize("good", [1, 0.5, 30, httpx.Timeout(None), httpx.Timeout(5, read=None), None])
    def test_good_timeouts_are_accepted(self, good: Any) -> None:
        make(api_key=API_KEY, timeout=good).with_options(timeout=good).close()

    def test_wait_budgets_are_not_newly_validated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ``<= 0`` keeps meaning "poll once".
        with recorded({("GET", f"/verify/status/{TASK}"): [DONE]}) as rec:
            make(api_key=API_KEY).wait(TASK, timeout=-5)
        assert len(rec.requests) == 1

    @pytest.mark.sync_only
    def test_not_given_is_one_falsy_object(self) -> None:
        assert NotGiven() is NOT_GIVEN and not NOT_GIVEN and repr(NOT_GIVEN) == "NOT_GIVEN"
        assert lenz_io.NOT_GIVEN is NOT_GIVEN


@contextmanager
def httpx_refused() -> Iterator[None]:
    """No request may be sent inside the block."""
    import respx

    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        router.route().mock(side_effect=AssertionError("a request was sent"))
        yield


# ── review follow-ups: 2.21 overrides, snapshots, the httpx tuple form ─────


class Old221(Lenz):
    """Overrides with the 2.21 signatures, which know no request option."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.called: list[str] = []

    def review(  # type: ignore[override]
        self,
        text: str,
        *,
        verdicts: list[str] | None = None,
        confidence: list[str] | None = None,
        max_assessments: int | None = None,
        max_verifications: int | None = None,
        depth: str | None = None,
        max_citations: int | None = None,
        suggest_edits: bool = False,
        language: str = "",
        webhook_url: str | None = None,
        visibility: str = "private",
        idempotency_key: str | None = None,
    ) -> Any:
        self.called.append("review")
        return super().review(
            text,
            verdicts=verdicts,
            confidence=confidence,
            max_assessments=max_assessments,
            max_verifications=max_verifications,
            depth=depth,
            max_citations=max_citations,
            suggest_edits=suggest_edits,
            language=language,
            webhook_url=webhook_url,
            visibility=visibility,
            idempotency_key=idempotency_key,
        )

    def citecheck(  # type: ignore[override]
        self,
        text: str | None = None,
        *,
        pairs: Any = None,
        max_citations: int | None = None,
        language: str = "",
        webhook_url: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        self.called.append("citecheck")
        return super().citecheck(
            text,
            pairs=pairs,
            max_citations=max_citations,
            language=language,
            webhook_url=webhook_url,
            idempotency_key=idempotency_key,
        )

    def wait(self, task: Any, *, timeout: float = WAIT_TIMEOUT, on_progress: Any = None) -> Any:  # type: ignore[override]
        self.called.append("wait")
        return super().wait(task, timeout=timeout, on_progress=on_progress)


class _OldVerifications(lenz_io.client._VerificationsNamespace):
    def list(self, *, page: int = 1) -> Any:  # type: ignore[override]
        return super().list(page=page)


class _OldLibrary(lenz_io.client._LibraryNamespace):
    def list(  # type: ignore[override]
        self,
        *,
        page: int = 1,
        sort: str = "recent",
        search: str = "",
        domain: str = "",
        entity: str = "",
        curated: Any = None,
        verdict: str = "",
    ) -> Any:
        return super().list(
            page=page, sort=sort, search=search, domain=domain, entity=entity, curated=curated, verdict=verdict
        )


_REVIEW_WAIT: Answers = {
    ("POST", "/review"): [(202, {"review_id": RID, "status": "queued"})],
    ("GET", f"/reviews/{RID}"): [(200, REVIEW_DONE)],
}
_CHECK_WAIT: Answers = {
    ("POST", "/citecheck"): [(202, {"citecheck_id": CID, "status": "queued"})],
    ("GET", f"/citechecks/{CID}"): [(200, CHECK_DONE)],
}
_VERIFY_WAIT: Answers = {("POST", "/verify"): [ACCEPTED], ("GET", f"/verify/status/{TASK}"): [DONE]}


@pytest.mark.sync_only
class TestOverridesWritten221:
    """A subclass written for 2.21 keeps working through the helpers that call
    its overridden methods, as long as the caller passes no option (an empty
    ``extra_headers`` counts as none)."""

    @pytest.mark.parametrize("opts", [{}, {"extra_headers": {}}, {"max_retries": None, "extra_headers": None}])
    def test_review_and_wait(self, opts: dict[str, Any]) -> None:
        client = Old221(api_key=API_KEY)
        with recorded(_REVIEW_WAIT):
            client.review_and_wait(DRAFT, **opts)
        assert client.called == ["review"]

    @pytest.mark.parametrize("opts", [{}, {"extra_headers": {}}])
    def test_citecheck_and_wait(self, opts: dict[str, Any]) -> None:
        client = Old221(api_key=API_KEY)
        with recorded(_CHECK_WAIT):
            client.citecheck_and_wait(DRAFT, **opts)
        assert client.called == ["citecheck"]

    @pytest.mark.parametrize("opts", [{}, {"extra_headers": {}}, {"max_retries": 1}])
    def test_verify_and_wait(self, opts: dict[str, Any]) -> None:
        client = Old221(api_key=API_KEY)
        with recorded(_VERIFY_WAIT):
            client.verify_and_wait("A.", **opts)
        assert client.called == ["wait"]

    def test_the_iterators(self) -> None:
        client = make(api_key=API_KEY)
        client.verifications = _OldVerifications(client)
        client.library = _OldLibrary(client)
        with recorded({("GET", "/verifications"): [PAGE_1, PAGE_2], ("GET", "/library"): [LIB_1, LIB_2]}):
            assert len(list(client.verifications.iter())) == 2
            assert len(list(client.library.iter(extra_headers={}))) == 2

    def test_an_option_still_reaches_an_override_that_takes_it(self) -> None:
        client = make(api_key=API_KEY)
        with recorded(_REVIEW_WAIT) as rec:
            client.review_and_wait(DRAFT, extra_headers={MARK: "m"})
        assert all(_headers(r).get(MARK.lower()) == "m" for r in rec.requests)


class TestSnapshots:
    def test_a_copy_keeps_the_timeout_it_was_given(self) -> None:
        t = httpx.Timeout(8)
        a = make(api_key=API_KEY).with_options(timeout=t)
        b = a.with_options()
        t.read = None
        for client in (a, b):
            with recorded(_USAGE) as rec:
                client.usage()
            assert rec.requests[0]["timeout"] == _all(8)

    def test_an_iterator_keeps_the_options_it_was_created_with(self) -> None:
        headers: dict[str, str | None] = {MARK: "first"}
        t = httpx.Timeout(4)
        with recorded({("GET", "/verifications"): [PAGE_1, PAGE_2]}) as rec:
            it = make(api_key=API_KEY).verifications.iter(extra_headers=headers, timeout=t)
            headers[MARK] = "changed"
            next(it)
            headers["X-Late"] = "late"
            t.read = 99
            list(it)
        assert [_headers(r).get(MARK.lower()) for r in rec.requests] == ["first", "first"]
        assert all("x-late" not in _headers(r) for r in rec.requests)
        assert [r["timeout"]["read"] for r in rec.requests] == [4, 4]

    def test_the_library_iterator_too(self) -> None:
        headers: dict[str, str | None] = {MARK: "first"}
        with recorded({("GET", "/library"): [LIB_1, LIB_2]}) as rec:
            it = make(api_key=API_KEY).library.iter(extra_headers=headers)
            next(it)
            headers[MARK] = "changed"
            list(it)
        assert [_headers(r).get(MARK.lower()) for r in rec.requests] == ["first", "first"]


class TestTimeoutForms:
    """What 2.21 accepted keeps working: the httpx tuple form and any real
    number (not ``bool``)."""

    _TUPLE = (5, 30, 6, 7)
    _AS_DICT: ClassVar[dict[str, int]] = {"connect": 5, "read": 30, "write": 6, "pool": 7}

    def test_the_tuple_form_on_the_constructor(self) -> None:
        with recorded(_USAGE) as rec:
            make(api_key=API_KEY, timeout=self._TUPLE).usage()  # type: ignore[arg-type]
        assert rec.requests[0]["timeout"] == self._AS_DICT

    def test_the_tuple_form_on_a_call_and_a_copy(self) -> None:
        client = make(api_key=API_KEY)
        with recorded(_EXTRACT) as rec:
            client.extract(text="Doc.", timeout=self._TUPLE)  # type: ignore[arg-type]
        assert rec.requests[0]["timeout"] == self._AS_DICT
        with recorded(_USAGE) as rec:
            client.with_options(timeout=self._TUPLE).usage()  # type: ignore[arg-type]
        assert rec.requests[0]["timeout"] == self._AS_DICT

    @pytest.mark.parametrize("bad", [(5, 0, 5, 5), (5, math.nan, 5, 5), (5, -1, 5, 5), (5, True, 5, 5), (5, 5), ()])
    def test_a_bad_tuple_is_refused(self, bad: Any) -> None:
        with pytest.raises(ValueError, match="timeout"):
            make(api_key=API_KEY, timeout=bad)
        with pytest.raises(ValueError, match="timeout"):
            make(api_key=API_KEY).usage(timeout=bad)

    def test_a_tuple_with_an_unbounded_part(self) -> None:
        with recorded(_USAGE) as rec:
            make(api_key=API_KEY).usage(timeout=(5, None, 5, 5))  # type: ignore[arg-type]
        assert rec.requests[0]["timeout"]["read"] is None

    def test_any_real_number_and_any_integer_like_retry_count(self) -> None:
        from fractions import Fraction

        class Three:
            def __index__(self) -> int:
                return 3

        client = make(api_key=API_KEY, timeout=Fraction(5), max_retries=Three())  # type: ignore[arg-type]
        client.with_options(timeout=Fraction(1, 2), max_retries=Three())  # type: ignore[arg-type]
        with recorded({("GET", "/me/usage"): [S503]}) as rec, pytest.raises(lenz_io.LenzAPIError):
            client.usage(max_retries=Three(), timeout=7)  # type: ignore[arg-type]
        assert len(rec.requests) == 4


class TestHeaderSyntax:
    @pytest.mark.parametrize(
        "headers",
        [
            {"Bad Name": "x"},
            {"X:A": "x"},
            {"X-Ä": "x"},
            {"X-A\n": "x"},
            {"X-A": "a\r\nInjected: 1"},
            {"X-A": "a\x00"},
            {"X-A": "café"},
        ],
    )
    def test_refused_before_a_key_or_a_request(self, headers: dict[str, str], no_key_minted: None) -> None:
        client = make(api_key=API_KEY)
        with httpx_refused():
            for call in (
                lambda: client.verify("A.", extra_headers=headers),
                lambda: client.review_and_wait(DRAFT, extra_headers=headers),
                lambda: client.with_options(extra_headers=headers),
            ):
                with pytest.raises(ValueError, match="header"):
                    call()

    def test_tab_and_space_are_allowed_in_a_value(self) -> None:
        with recorded(_USAGE) as rec:
            make(api_key=API_KEY).usage(extra_headers={"X-A": "a b\tc", "X-Empty": ""})
        assert _headers(rec.requests[0])["x-a"] == "a b\tc"
        assert _headers(rec.requests[0])["x-empty"] == ""

    @pytest.mark.parametrize("value", ["trace ", " trace", "\ttrace", "trace\t", " ", "\t"])
    def test_whitespace_at_either_end_of_a_value_is_refused(self, value: str, no_key_minted: None) -> None:
        client = make(api_key=API_KEY)
        with httpx_refused():
            for call in (
                lambda: client.verify("A.", extra_headers={"X-A": value}),
                lambda: client.usage(extra_headers={"X-A": value}),
                lambda: client.with_options(extra_headers={"X-A": value}),
            ):
                with pytest.raises(ValueError, match="header"):
                    call()

    def test_a_poll_that_cannot_be_sent_is_not_read_as_an_unreadable_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An error raised while building a poll request (before anything is
        # sent) ends the wait at once instead of being polled to the timeout.
        real = Lenz._request

        def failing(self: Lenz, method: str, path: str, **kw: Any) -> Any:
            if method == "GET":
                raise UnicodeEncodeError("ascii", "é", 0, 1, "not ASCII")
            return real(self, method, path, **kw)

        real_async = AsyncLenz._request

        async def failing_async(self: AsyncLenz, method: str, path: str, **kw: Any) -> Any:
            if method == "GET":
                raise UnicodeEncodeError("ascii", "é", 0, 1, "not ASCII")
            return await real_async(self, method, path, **kw)

        if _Clients.use_async:
            monkeypatch.setattr(AsyncLenz, "_request", failing_async)
        else:
            monkeypatch.setattr(Lenz, "_request", failing)
        with recorded(_REVIEW_WAIT) as rec, pytest.raises(UnicodeEncodeError):
            make(api_key=API_KEY).review_and_wait(DRAFT, timeout=60)
        assert rec.clock.sleeps == []


@pytest.mark.sync_only
def test_a_with_block_on_a_subclass_copy_keeps_the_subclass() -> None:
    class Sub(Lenz):
        pass

    with Sub(api_key=API_KEY).with_options(timeout=5) as c:
        assert isinstance(c, Sub)
    ann = inspect.signature(Lenz.__enter__).return_annotation
    assert "Self" in str(ann)
