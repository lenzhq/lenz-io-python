"""Three small gaps, on both clients:

* ``page_size`` on ``verifications.list()`` / ``verifications.iter()``: sent
  only when given, 1 to 100, checked before any request.
* ``api_key=""`` (or whitespace) is no key: it never reads ``LENZ_API_KEY``.
* ``user_agent=`` (and the SDK's own User-Agent) reaches the wire through a
  client given ``http_client=``, without changing that client's headers.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from conftest import make_client, make_http

from lenz_io import LenzAuthError

pytestmark = pytest.mark.usefixtures("any_client")

BASE = "https://lenz.io/api/v1"
KEY = "lenz_test_abc123"
_USAGE = {"plan": "free", "credits_used": 0, "credits_total": 10}


def _page(start: int, count: int, page: int, page_size: int, total: int = 100) -> dict[str, Any]:
    items = [{"verification_id": f"v{start + i}", "claim": f"C{start + i}."} for i in range(count)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@pytest.fixture()
def client() -> Iterator[Any]:
    with make_client(api_key=KEY) as c:
        yield c


class TestPageSize:
    def test_list_sends_page_size_when_given(self, client: Any) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.get("/verifications").respond(200, json=_page(0, 2, 1, 50))
            result = client.verifications.list(page_size=50)
        assert route.calls.last.request.url.params["page_size"] == "50"
        assert route.calls.last.request.url.params["page"] == "1"
        assert result.page_size == 50

    def test_list_sends_no_page_size_by_default(self, client: Any) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.get("/verifications").respond(200, json=_page(0, 2, 1, 20))
            client.verifications.list()
            client.verifications.list(page_size=None)
        assert all("page_size" not in c.request.url.params for c in route.calls)
        assert str(route.calls[0].request.url) == f"{BASE}/verifications?page=1"

    @pytest.mark.parametrize("size", [1, 100])
    def test_the_bounds_are_accepted(self, client: Any, size: int) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.get("/verifications").respond(200, json=_page(0, 0, 1, size, total=0))
            client.verifications.list(page_size=size)
        assert route.calls.last.request.url.params["page_size"] == str(size)

    @pytest.mark.parametrize("size", [0, -1, 101, True, False, 2.0, "20"])
    def test_a_bad_page_size_raises_before_any_request(self, client: Any, size: Any) -> None:
        with respx.mock(base_url=BASE, assert_all_called=False) as r:
            route = r.get("/verifications").respond(200, json=_page(0, 0, 1, 20, total=0))
            with pytest.raises(ValueError, match="page_size"):
                client.verifications.list(page_size=size)
            with pytest.raises(ValueError, match="page_size"):
                client.verifications.iter(page_size=size)
        assert route.call_count == 0

    def test_iter_sends_page_size_on_every_page(self, client: Any) -> None:
        pages = {1: _page(0, 3, 1, 3, total=7), 2: _page(3, 3, 2, 3, total=7), 3: _page(6, 1, 3, 3, total=7)}
        with respx.mock(base_url=BASE) as r:
            route = r.get("/verifications")
            route.side_effect = lambda request: httpx.Response(200, json=pages[int(request.url.params["page"])])
            items = list(client.verifications.iter(page_size=3))
        assert [i.verification_id for i in items] == [f"v{n}" for n in range(7)]
        assert [c.request.url.params["page"] for c in route.calls] == ["1", "2", "3"]
        assert [c.request.url.params["page_size"] for c in route.calls] == ["3", "3", "3"]

    def test_iter_sends_no_page_size_by_default(self, client: Any) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.get("/verifications").respond(200, json=_page(0, 1, 1, 20, total=1))
            list(client.verifications.iter())
        assert "page_size" not in route.calls.last.request.url.params


class TestEmptyApiKey:
    @pytest.mark.parametrize("key", ["", "   ", "\t\n"])
    def test_an_empty_key_never_reads_the_environment(self, monkeypatch: pytest.MonkeyPatch, key: str) -> None:
        monkeypatch.setenv("LENZ_API_KEY", "lenz_env_key")
        with respx.mock(base_url=BASE, assert_all_called=False) as r:
            route = r.get("/me/usage").respond(200, json=_USAGE)
            with make_client(api_key=key) as c, pytest.raises(LenzAuthError):
                c.usage()
        assert route.call_count == 0

    def test_an_empty_key_still_reads_the_public_library_without_a_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LENZ_API_KEY", "lenz_env_key")
        with respx.mock(base_url=BASE) as r:
            route = r.get("/library").respond(200, json={"items": [], "total": 0, "page": 1, "page_size": 20})
            with make_client(api_key="") as c:
                c.library.list()
        assert "Authorization" not in route.calls.last.request.headers

    def test_an_omitted_key_still_reads_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LENZ_API_KEY", "lenz_env_key")
        with respx.mock(base_url=BASE) as r:
            route = r.get("/me/usage").respond(200, json=_USAGE)
            with make_client() as c:
                c.usage()
        assert route.calls.last.request.headers["Authorization"] == "Bearer lenz_env_key"


class TestBorrowedClientUserAgent:
    def _sent_agent(self, **kwargs: Any) -> tuple[str, Any]:
        with respx.mock(base_url=BASE) as r:
            route = r.get("/me/usage").respond(200, json=_USAGE)
            with make_client(api_key=KEY, **kwargs) as c:
                c.usage()
        return str(route.calls.last.request.headers["User-Agent"]), route

    def test_user_agent_reaches_the_wire_through_a_borrowed_client(self) -> None:
        with make_http() as http:
            agent, _ = self._sent_agent(http_client=http, user_agent="my-app/1.0")
            assert agent == "my-app/1.0"
            # The borrowed client is not changed.
            assert http.headers["User-Agent"].startswith("python-httpx/")

    def test_the_sdk_user_agent_reaches_the_wire_through_a_borrowed_client(self) -> None:
        with make_http() as http:
            agent, _ = self._sent_agent(http_client=http)
        assert agent.startswith("lenz-io-python/")

    def test_a_borrowed_clients_own_user_agent_is_kept(self) -> None:
        with make_http(headers={"User-Agent": "corp-proxy/2"}) as http:
            agent, _ = self._sent_agent(http_client=http)
        assert agent == "corp-proxy/2"

    def test_user_agent_wins_over_a_borrowed_clients_own(self) -> None:
        with make_http(headers={"User-Agent": "corp-proxy/2"}) as http:
            agent, _ = self._sent_agent(http_client=http, user_agent="my-app/1.0")
        assert agent == "my-app/1.0"

    def test_extra_headers_still_win(self) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.get("/me/usage").respond(200, json=_USAGE)
            with make_http() as http, make_client(api_key=KEY, http_client=http, user_agent="my-app/1.0") as c:
                c.usage(extra_headers={"User-Agent": "per-call/3"})
        assert [v for k, v in route.calls.last.request.headers.multi_items() if k.lower() == "user-agent"] == [
            "per-call/3"
        ]

    def test_a_copy_keeps_the_user_agent(self) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.get("/me/usage").respond(200, json=_USAGE)
            with make_http() as http, make_client(api_key=KEY, http_client=http, user_agent="my-app/1.0") as c:
                c.with_options(max_retries=0).usage()
        assert route.calls.last.request.headers["User-Agent"] == "my-app/1.0"

    def test_the_version_header_is_still_sent(self) -> None:
        with make_http() as http:
            _, route = self._sent_agent(http_client=http, user_agent="my-app/1.0")
        assert route.calls.last.request.headers["X-Lenz-API-Version"]
