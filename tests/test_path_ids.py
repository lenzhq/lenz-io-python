"""An id goes into the URL path as ONE segment.

httpx normalises dot segments and cuts a URL at ``?`` and ``#``, so an id put
into a path raw can send the request somewhere else: ``cancel_review`` with
``"../citechecks/c1"`` would POST ``/citechecks/c1/cancel``. Every id is
percent-encoded whole, ``"."`` and ``".."`` (which a path normaliser eats) are
refused like an empty id, and an ordinary id is on the wire byte for byte as
before.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import respx
from conftest import make_client

from lenz_io import Lenz

# Every test here runs on both clients (``any_client`` in conftest.py): the
# same calls and assertions against ``Lenz`` and ``AsyncLenz``.
pytestmark = pytest.mark.usefixtures("any_client")


@pytest.fixture()
def client() -> Iterator[Any]:
    with make_client(api_key="lenz_test_abc123") as c:
        yield c


@pytest.fixture()
def unauth_client() -> Iterator[Any]:
    with make_client() as c:
        yield c


BASE = "https://lenz.io/api/v1"
HOSTILE = [
    "../citechecks/c1",
    "a#x",
    "batch?",
    "a/b",
    "a b",
    "ünï/ç",
    "%2e%2e",
    "a%2Fb",
    "x?y=1#z",
    "\\..\\",
    "a;b",
]

#: (label, call(client, id), method, path template)
CALLS: list[tuple[str, Callable[[Lenz, str], Any], str, str]] = [
    ("get_status", lambda c, i: c.get_status(i), "GET", "/verify/status/{}"),
    ("cancel", lambda c, i: c.cancel(i), "POST", "/verify/{}/cancel"),
    ("select", lambda c, i: c.select(i, claims=["A claim."], idempotency=False), "POST", "/verify/{}/select"),
    ("get_review", lambda c, i: c.get_review(i), "GET", "/reviews/{}"),
    ("get_review issues", lambda c, i: c.get_review(i, view="issues"), "GET", "/reviews/{}"),
    ("cancel_review", lambda c, i: c.cancel_review(i), "POST", "/reviews/{}/cancel"),
    ("get_citecheck", lambda c, i: c.get_citecheck(i), "GET", "/citechecks/{}"),
    ("cancel_citecheck", lambda c, i: c.cancel_citecheck(i), "POST", "/citechecks/{}/cancel"),
    ("wait review", lambda c, i: c._wait_review(i, timeout=0), "GET", "/reviews/{}"),
    ("wait citecheck", lambda c, i: c._wait_citecheck(i, timeout=0), "GET", "/citechecks/{}"),
    ("wait", lambda c, i: c.wait(i, timeout=0), "GET", "/verify/status/{}"),
    ("verifications.get", lambda c, i: c.verifications.get(i), "GET", "/verifications/{}"),
    (
        "verifications.get_certificate",
        lambda c, i: c.verifications.get_certificate(i),
        "GET",
        "/verifications/{}/certificate",
    ),
    ("verifications.delete", lambda c, i: c.verifications.delete(i), "DELETE", "/verifications/{}"),
    ("verifications.related", lambda c, i: c.verifications.related(i), "GET", "/verifications/{}/related"),
    ("ask.history", lambda c, i: c.ask.history(i), "GET", "/ask/{}"),
    ("ask.send", lambda c, i: c.ask.send(i, message="Why?", idempotency=False), "POST", "/ask/{}"),
    ("ask.reset", lambda c, i: c.ask.reset(i), "DELETE", "/ask/{}"),
]
IDS = [c[0] for c in CALLS]


def _send(call: Callable[[Lenz, str], Any], ident: str) -> list[httpx.Request]:
    with (
        make_client(api_key="lenz_test_abc123", max_retries=0) as client,
        respx.mock(assert_all_called=False) as router,
    ):
        router.route().respond(200, json={})
        try:
            call(client, ident)
        except Exception:
            # The stub answers {}: whether the call can read it is not the point.
            pass
        return [c.request for c in router.calls]


@pytest.mark.parametrize("ident", HOSTILE)
@pytest.mark.parametrize(("label", "call", "method", "template"), CALLS, ids=IDS)
def test_a_hostile_id_stays_one_segment_of_the_intended_path(
    label: str, call: Callable[[Lenz, str], Any], method: str, template: str, ident: str
) -> None:
    requests = _send(call, ident)
    assert requests, label
    for request in requests:
        assert request.method == method
        assert request.url.raw_path.split(b"?")[0] == ("/api/v1" + template.format(quote(ident, safe=""))).encode()
        assert request.url.fragment == ""


@pytest.mark.parametrize(
    ("call", "path"),
    [
        (lambda c: c.cancel_review("../citechecks/c1"), "/api/v1/reviews/..%2Fcitechecks%2Fc1/cancel"),
        (lambda c: c.cancel("a#x"), "/api/v1/verify/a%23x/cancel"),
        (lambda c: c.cancel("batch?"), "/api/v1/verify/batch%3F/cancel"),
        (lambda c: c.cancel_citecheck("a/b"), "/api/v1/citechecks/a%2Fb/cancel"),
        (lambda c: c.get_status("ünï"), "/api/v1/verify/status/%C3%BCn%C3%AF"),
    ],
)
def test_the_traversal_examples(call: Callable[[Lenz], Any], path: str) -> None:
    requests = _send(lambda c, _i: call(c), "")
    assert [r.url.raw_path.decode() for r in requests] == [path]


@pytest.mark.parametrize("ident", ["", ".", ".."])
@pytest.mark.parametrize(("label", "call", "method", "template"), CALLS, ids=IDS)
def test_an_empty_dot_or_dotdot_id_is_refused_before_any_request(
    label: str, call: Callable[[Lenz, str], Any], method: str, template: str, ident: str
) -> None:
    with (
        make_client(api_key="lenz_test_abc123", max_retries=0) as client,
        respx.mock(assert_all_called=False) as router,
    ):
        router.route().respond(200, json={})
        with pytest.raises(ValueError):
            call(client, ident)
        assert router.calls.call_count == 0


@pytest.mark.parametrize(
    ("label", "call", "method", "template"),
    [c for c in CALLS if c[0] not in ("wait review", "wait citecheck", "wait")],
    ids=[i for i in IDS if i not in ("wait review", "wait citecheck", "wait")],
)
def test_an_ordinary_id_is_on_the_wire_unchanged(
    label: str, call: Callable[[Lenz, str], Any], method: str, template: str
) -> None:
    for ident in ("d6b2bd72", "3f2a9c1e5b7d4a608c1d2e3f4a5b6c7d", "tsk_abc-123.v2~x"):
        requests = _send(call, ident)
        assert requests[0].url.raw_path.split(b"?")[0] == ("/api/v1" + template.format(ident)).encode()


def test_a_dotted_id_that_is_not_all_dots_is_fine() -> None:
    requests = _send(lambda c, i: c.get_review(i), "a..b")
    assert requests[0].url.raw_path == b"/api/v1/reviews/a..b"
