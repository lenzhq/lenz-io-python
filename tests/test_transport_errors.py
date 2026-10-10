"""Every transport failure of a request that may succeed when sent again (a
dropped connection, a server that hung up, a proxy failure) is a
``LenzConnectionError``, retried like a network error, and a wait polls again
after one. A request that could never be sent (an unsupported URL scheme, a
request httpx refuses to write) is a programming error and stays the httpx
exception."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from conftest import AnyClient, make_client

from lenz_io import Lenz, LenzAPIError, LenzConnectionError, LenzRequestTimeoutError

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
_USAGE = {"credits": {"remaining": 1}}
_DONE = {"status": "completed", "task_id": "t", "result": {"verification_id": "v1", "claim": "A."}}


@pytest.fixture(autouse=True)
def _no_sleep(any_client: AnyClient) -> None:
    """``any_client`` records sleeps instead of sleeping."""


_TRANSIENT = [
    httpx.RemoteProtocolError("Server disconnected without sending a response."),
    httpx.ProxyError("proxy refused"),
    httpx.ReadError("reset"),
]


@pytest.mark.parametrize("failure", _TRANSIENT)
def test_raised_as_a_connection_error_after_the_retries(client: Lenz, failure: Exception) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.get("/me/usage").mock(side_effect=failure)
        with pytest.raises(LenzConnectionError) as ei:
            client.usage()
    assert route.call_count == 4
    err = ei.value
    assert type(err) is LenzConnectionError
    assert isinstance(err, LenzAPIError) and not isinstance(err, LenzRequestTimeoutError)
    assert err.__cause__ is failure
    assert err.retryable is True


@pytest.mark.parametrize("failure", _TRANSIENT)
def test_retried_like_a_network_error(client: Lenz, failure: Exception) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.get("/me/usage")
        route.side_effect = [failure, httpx.Response(200, json=_USAGE)]
        client.usage()
    assert route.call_count == 2


@pytest.mark.parametrize("failure", [httpx.UnsupportedProtocol("ftp"), httpx.LocalProtocolError("bad header")])
def test_a_request_that_cannot_be_sent_is_not_wrapped(client: Lenz, failure: Exception) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.get("/me/usage").mock(side_effect=failure)
        with pytest.raises(type(failure)):
            client.usage()
    assert route.call_count == 1


def test_a_server_that_hangs_up_mid_wait_is_polled_again(client: Lenz) -> None:
    with respx.mock(base_url=BASE) as r:
        poll = r.get("/verify/status/t")
        poll.side_effect = [httpx.RemoteProtocolError("Server disconnected"), httpx.Response(200, json=_DONE)]
        assert client.wait("t", timeout=300).verification_id == "v1"
    assert poll.call_count == 2


def test_a_server_that_hangs_up_mid_review_wait_is_polled_again(client: Lenz) -> None:
    done = {"review_id": "r1", "status": "completed", "issues": [], "failures": [], "claims": []}
    with respx.mock(base_url=BASE) as r:
        r.post("/review").respond(202, json={"review_id": "r1", "status": "queued"})
        poll = r.get("/reviews/r1")
        poll.side_effect = [httpx.RemoteProtocolError("Server disconnected"), httpx.Response(200, json=done)]
        assert client.review_and_wait("Draft.").status == "completed"
    assert poll.call_count == 2
