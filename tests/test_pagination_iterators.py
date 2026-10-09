"""``verifications.iter()`` and ``library.iter()``: every item, page after
page. The page size is read from each response (the API takes none on the
request), the start page is honoured, the walk ends on a short or empty page,
a page is fetched only when the items before it have been consumed, and
``sort="random"`` (not exhaustive) is refused."""

from __future__ import annotations

from itertools import islice

import pytest
import respx

from lenz_io import Lenz, LibraryItem, VerificationListItem

BASE = "https://lenz.io/api/v1"


def _page(start: int, count: int, page: int, page_size: int, total: int = 100) -> dict:
    items = [{"verification_id": f"v{start + i}", "claim": f"C{start + i}."} for i in range(count)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def _pager(route: respx.Route, pages: dict[int, dict]) -> None:
    def respond(request):
        import httpx

        return httpx.Response(200, json=pages[int(request.url.params["page"])])

    route.side_effect = respond


@pytest.mark.parametrize(
    ("path", "walk", "item_type"),
    [
        ("/verifications", lambda c, **kw: c.verifications.iter(**kw), VerificationListItem),
        ("/library", lambda c, **kw: c.library.iter(**kw), LibraryItem),
    ],
)
class TestIter:
    def test_every_page_until_a_short_one(self, client: Lenz, path, walk, item_type) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.get(path)
            _pager(route, {1: _page(0, 2, 1, 2), 2: _page(2, 2, 2, 2), 3: _page(4, 1, 3, 2)})
            items = list(walk(client))
        assert [i.verification_id for i in items] == ["v0", "v1", "v2", "v3", "v4"]
        assert all(isinstance(i, item_type) for i in items)
        assert [c.request.url.params["page"] for c in route.calls] == ["1", "2", "3"]
        assert all("page_size" not in c.request.url.params for c in route.calls)

    def test_a_full_last_page_ends_on_the_empty_one_after_it(self, client: Lenz, path, walk, item_type) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.get(path)
            _pager(route, {1: _page(0, 2, 1, 2), 2: _page(0, 0, 2, 2)})
            assert len(list(walk(client))) == 2
        assert route.call_count == 2

    def test_the_page_size_is_read_from_each_response(self, client: Lenz, path, walk, item_type) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.get(path)
            _pager(route, {1: _page(0, 3, 1, 3), 2: _page(3, 3, 2, 5)})
            assert len(list(walk(client))) == 6
        assert route.call_count == 2

    def test_the_start_page_is_honoured(self, client: Lenz, path, walk, item_type) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.get(path)
            _pager(route, {3: _page(40, 1, 3, 20)})
            items = list(walk(client, page=3))
        assert [i.verification_id for i in items] == ["v40"]
        assert route.calls.last.request.url.params["page"] == "3"

    def test_an_empty_first_page(self, client: Lenz, path, walk, item_type) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.get(path)
            _pager(route, {1: _page(0, 0, 1, 20, total=0)})
            assert list(walk(client)) == []
        assert route.call_count == 1

    def test_no_prefetch(self, client: Lenz, path, walk, item_type) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.get(path)
            _pager(route, {1: _page(0, 2, 1, 2), 2: _page(2, 2, 2, 2)})
            it = walk(client)
            assert route.call_count == 0, "nothing is fetched before the first item is asked for"
            assert [i.verification_id for i in islice(it, 2)] == ["v0", "v1"]
            assert route.call_count == 1
            next(it)
            assert route.call_count == 2


class TestFilters:
    def test_library_filters_reach_every_page(self, unauth_client: Lenz) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.get("/library")
            _pager(route, {1: _page(0, 2, 1, 2), 2: _page(2, 0, 2, 2)})
            list(unauth_client.library.iter(sort="most_true", search="moon", curated=["trivia"], verdict="True"))
        for call in route.calls:
            params = call.request.url.params
            assert params["sort"] == "most_true"
            assert params["search"] == "moon"
            assert params["curated"] == "trivia"
            assert params["verdict"] == "True"
            assert "Authorization" not in call.request.headers

    def test_random_order_is_refused(self, unauth_client: Lenz) -> None:
        with respx.mock(base_url=BASE, assert_all_called=False) as r:
            route = r.get("/library")
            with pytest.raises(ValueError, match="random"):
                unauth_client.library.iter(sort="random")
        assert route.call_count == 0
