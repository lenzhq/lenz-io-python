from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from lenz_io import AsyncLenz, Lenz, LenzUsageError, ReviewList, ReviewListItem
from lenz_io.errors import ResponseHeaders

BASE = "https://lenz.io/api/v1"
KEY = "lenz_test_abc123"


def _sample_item(idx: int, **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "review_id": f"rev_{idx}",
        "status": "completed",
        "outcome": "clean",
        "created_at": "2026-10-01T12:00:00Z",
        "completed_at": "2026-10-01T12:05:00Z",
        "language": "en",
        "policy": {
            "verdicts": ["False", "Mostly False", "Mixed"],
            "confidence": ["low"],
            "max_assessments": 20,
            "max_verifications": 5,
            "depth": "standard",
            "max_citations": 0,
            "suggest_edits": False,
        },
        "summary": {
            "claims_found": 2,
            "claims_selected": 2,
            "claim_limit": 20,
            "claim_limit_exceeded": False,
            "input_truncated": False,
            "assessments": {"completed": 2, "failed": 0},
            "verifications": {"planned": 0, "completed": 0, "failed": 0},
            "issues": 0,
        },
        "credits": {"charged": 2},
        "failure": None,
    }
    item.update(overrides)
    return item


def _reviews_page(start: int, count: int, page: int, page_size: int, total: int = 100) -> dict[str, Any]:
    items = [_sample_item(start + i) for i in range(count)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@pytest.fixture()
def client() -> Iterator[Lenz]:
    with Lenz(api_key=KEY) as c:
        yield c


@pytest.fixture()
def async_client() -> Iterator[AsyncLenz]:
    c = AsyncLenz(api_key=KEY)
    yield c


# ── 1. list_reviews query and arguments ──────────────────────────────────────


def test_list_reviews_default_query(client: Lenz) -> None:
    body = _reviews_page(0, 2, 1, 20, total=2)
    with respx.mock(base_url=BASE) as r:
        route = r.get("/reviews").respond(200, json=body)
        res = client.list_reviews()
    assert route.called
    assert route.calls.last.request.url.params["page"] == "1"
    assert "page_size" not in route.calls.last.request.url.params
    assert isinstance(res, ReviewList)
    assert len(res.items) == 2
    assert res.total == 2
    assert res.page == 1
    assert res.page_size == 20
    assert all(isinstance(item, ReviewListItem) for item in res.items)
    assert res.items[0].review_id == "rev_0"


@pytest.mark.asyncio
async def test_async_list_reviews_default_query(async_client: AsyncLenz) -> None:
    body = _reviews_page(0, 2, 1, 20, total=2)
    with respx.mock(base_url=BASE) as r:
        route = r.get("/reviews").respond(200, json=body)
        async with async_client as c:
            res = await c.list_reviews()
    assert route.called
    assert route.calls.last.request.url.params["page"] == "1"
    assert "page_size" not in route.calls.last.request.url.params
    assert isinstance(res, ReviewList)
    assert len(res.items) == 2


def test_list_reviews_explicit_page_and_page_size(client: Lenz) -> None:
    body = _reviews_page(50, 10, 2, 50, total=60)
    with respx.mock(base_url=BASE) as r:
        route = r.get("/reviews").respond(200, json=body)
        res = client.list_reviews(page=2, page_size=50)
    assert route.calls.last.request.url.params["page"] == "2"
    assert route.calls.last.request.url.params["page_size"] == "50"
    assert res.page == 2
    assert res.page_size == 50
    assert len(res.items) == 10


@pytest.mark.asyncio
async def test_async_list_reviews_explicit_page_and_page_size(async_client: AsyncLenz) -> None:
    body = _reviews_page(50, 10, 2, 50, total=60)
    with respx.mock(base_url=BASE) as r:
        route = r.get("/reviews").respond(200, json=body)
        async with async_client as c:
            res = await c.list_reviews(page=2, page_size=50)
    assert route.calls.last.request.url.params["page"] == "2"
    assert route.calls.last.request.url.params["page_size"] == "50"
    assert res.page == 2
    assert res.page_size == 50


@pytest.mark.parametrize("size", [0, -1, 101, True, False, 2.5, "20"])
def test_list_reviews_page_size_validation(client: Lenz, size: Any) -> None:
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        route = r.get("/reviews").respond(200, json=_reviews_page(0, 0, 1, 20, total=0))
        with pytest.raises(LenzUsageError) as exc_info:
            client.list_reviews(page_size=size)
        assert exc_info.value.code == "invalid_page_size"
        assert exc_info.value.param == "page_size"
    assert route.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, -1, 101, True, False, "50"])
async def test_async_list_reviews_page_size_validation(async_client: AsyncLenz, size: Any) -> None:
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        route = r.get("/reviews").respond(200, json=_reviews_page(0, 0, 1, 20, total=0))
        async with async_client as c:
            with pytest.raises(LenzUsageError) as exc_info:
                await c.list_reviews(page_size=size)
            assert exc_info.value.code == "invalid_page_size"
            assert exc_info.value.param == "page_size"
    assert route.call_count == 0


# ── 2. iter_reviews argument validation (eager before any request) ────────────


@pytest.mark.parametrize("bad_page", [0, -1, -5, False, True, "1"])
def test_iter_reviews_page_validation(client: Lenz, bad_page: Any) -> None:
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        route = r.get("/reviews").respond(200, json=_reviews_page(0, 0, 1, 20, total=0))
        with pytest.raises(LenzUsageError) as exc_info:
            client.iter_reviews(page=bad_page)
        assert exc_info.value.code == "invalid_page"
        assert exc_info.value.param == "page"
    assert route.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_page", [0, -1, -5, False, True, "1"])
async def test_async_iter_reviews_page_validation(async_client: AsyncLenz, bad_page: Any) -> None:
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        route = r.get("/reviews").respond(200, json=_reviews_page(0, 0, 1, 20, total=0))
        async with async_client as c:
            with pytest.raises(LenzUsageError) as exc_info:
                c.iter_reviews(page=bad_page)
            assert exc_info.value.code == "invalid_page"
            assert exc_info.value.param == "page"
    assert route.call_count == 0


@pytest.mark.parametrize("size", [0, -1, 101, True, False, "20"])
def test_iter_reviews_page_size_validation(client: Lenz, size: Any) -> None:
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        route = r.get("/reviews").respond(200, json=_reviews_page(0, 0, 1, 20, total=0))
        with pytest.raises(LenzUsageError) as exc_info:
            client.iter_reviews(page_size=size)
        assert exc_info.value.code == "invalid_page_size"
        assert exc_info.value.param == "page_size"
    assert route.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, -1, 101, True, False, "20"])
async def test_async_iter_reviews_page_size_validation(async_client: AsyncLenz, size: Any) -> None:
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        route = r.get("/reviews").respond(200, json=_reviews_page(0, 0, 1, 20, total=0))
        async with async_client as c:
            with pytest.raises(LenzUsageError) as exc_info:
                c.iter_reviews(page_size=size)
            assert exc_info.value.code == "invalid_page_size"
            assert exc_info.value.param == "page_size"
    assert route.call_count == 0


# ── 3. iter_reviews pagination stopping conditions ───────────────────────────


def test_iter_reviews_stops_on_short_page(client: Lenz) -> None:
    pages = {
        1: _reviews_page(0, 3, 1, 3, total=10),
        2: _reviews_page(3, 2, 2, 3, total=10),  # short page: 2 < 3
    }
    with respx.mock(base_url=BASE) as r:
        route = r.get("/reviews")
        route.side_effect = lambda req: httpx.Response(200, json=pages[int(req.url.params["page"])])
        items = list(client.iter_reviews(page_size=3))
    assert [i.review_id for i in items] == [f"rev_{n}" for n in range(5)]
    assert [c.request.url.params["page"] for c in route.calls] == ["1", "2"]
    assert all(c.request.url.params["page_size"] == "3" for c in route.calls)


@pytest.mark.asyncio
async def test_async_iter_reviews_stops_on_short_page(async_client: AsyncLenz) -> None:
    pages = {
        1: _reviews_page(0, 3, 1, 3, total=10),
        2: _reviews_page(3, 2, 2, 3, total=10),
    }
    with respx.mock(base_url=BASE) as r:
        route = r.get("/reviews")
        route.side_effect = lambda req: httpx.Response(200, json=pages[int(req.url.params["page"])])
        items: list[ReviewListItem] = []
        async with async_client as c:
            async for item in c.iter_reviews(page_size=3):
                items.append(item)
    assert [i.review_id for i in items] == [f"rev_{n}" for n in range(5)]
    assert [c.request.url.params["page"] for c in route.calls] == ["1", "2"]


def test_iter_reviews_stops_when_reaching_total(client: Lenz) -> None:
    # 2 pages of 2 items, total=4 -> stops when page * page_size >= total
    pages = {
        1: _reviews_page(0, 2, 1, 2, total=4),
        2: _reviews_page(2, 2, 2, 2, total=4),
    }
    with respx.mock(base_url=BASE) as r:
        route = r.get("/reviews")
        route.side_effect = lambda req: httpx.Response(200, json=pages[int(req.url.params["page"])])
        items = list(client.iter_reviews(page_size=2))
    assert [i.review_id for i in items] == [f"rev_{n}" for n in range(4)]
    assert [c.request.url.params["page"] for c in route.calls] == ["1", "2"]


@pytest.mark.asyncio
async def test_async_iter_reviews_stops_when_reaching_total(async_client: AsyncLenz) -> None:
    pages = {
        1: _reviews_page(0, 2, 1, 2, total=4),
        2: _reviews_page(2, 2, 2, 2, total=4),
    }
    with respx.mock(base_url=BASE) as r:
        route = r.get("/reviews")
        route.side_effect = lambda req: httpx.Response(200, json=pages[int(req.url.params["page"])])
        items: list[ReviewListItem] = []
        async with async_client as c:
            async for item in c.iter_reviews(page_size=2):
                items.append(item)
    assert [i.review_id for i in items] == [f"rev_{n}" for n in range(4)]
    assert [c.request.url.params["page"] for c in route.calls] == ["1", "2"]


def test_iter_reviews_empty_first_page(client: Lenz) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.get("/reviews").respond(200, json=_reviews_page(0, 0, 1, 20, total=0))
        items = list(client.iter_reviews())
    assert items == []
    assert route.call_count == 1


def test_iter_reviews_honours_start_page(client: Lenz) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.get("/reviews").respond(200, json=_reviews_page(40, 1, 3, 20, total=41))
        items = list(client.iter_reviews(page=3))
    assert [i.review_id for i in items] == ["rev_40"]
    assert route.calls.last.request.url.params["page"] == "3"


# ── 4. Item normalization: Canonical (2026-10-11) & Legacy (2026-05-13) ──────


def test_review_list_item_canonical_shape() -> None:
    raw_item = {
        "review_id": "rev_canonical_1",
        "status": "failed",
        "outcome": "unchecked",
        "created_at": "2026-10-11T10:00:00Z",
        "completed_at": "2026-10-11T10:01:00Z",
        "language": "en",
        "policy": {
            "verdicts": ["False"],
            "confidence": ["low"],
            "max_assessments": 20,
            "max_verifications": 5,
            "depth": "standard",
            "max_citations": 0,
            "suggest_edits": False,
        },
        "summary": {
            "claims_found": 2,
            "claims_selected": 2,
            "claim_limit": 20,
            "claim_limit_exceeded": False,
            "input_truncated": False,
            "assessments": {"completed": 0, "failed": 2},
            "verifications": {"planned": 0, "completed": 0, "failed": 0},
            "issues": 0,
        },
        "credits": {"charged": 0},
        "failure": {
            "code": "no_checkable_claim",
            "failure_class": "content",
            "retryable": False,
            "detail": "No checkable claims found in the document.",
            "hint": "Check the document text.",
            "docs_url": "https://lenz.io/docs/errors",
        },
    }

    # Default client reading (legacy_aliases=True):
    # 2.x aliases are populated, newer properties remain available
    item_default = ReviewListItem.model_validate(raw_item)
    assert item_default.review_id == "rev_canonical_1"
    assert item_default.status == "failed"
    assert item_default.outcome == "unchecked"
    assert item_default.summary.claim_limit_reached is False
    assert item_default.summary.claim_limit_exceeded is False
    assert item_default.summary.claims_found == 2
    assert item_default.failure is not None
    assert item_default.failure.failure_reason == "no_claim"
    assert item_default.failure.code == "no_checkable_claim"
    assert item_default.failure.detail == "No checkable claims found in the document."

    # Explicit canonical reading (legacy_aliases=False):
    item_canonical = ReviewListItem.model_validate(raw_item, context={"legacy_aliases": False})
    assert item_canonical.summary.claim_limit_reached is None
    assert item_canonical.summary.claim_limit_exceeded is False
    assert item_canonical.summary.claims_found == 2
    assert item_canonical.failure is not None
    assert item_canonical.failure.failure_reason == ""
    assert item_canonical.failure.code == "no_checkable_claim"
    assert item_canonical.failure.detail == "No checkable claims found in the document."


def test_list_reviews_respects_client_legacy_aliases_false() -> None:
    raw_item = {
        "review_id": "rev_canonical_1",
        "status": "completed",
        "outcome": "clean",
        "created_at": "2026-10-11T10:00:00Z",
        "completed_at": "2026-10-11T10:01:00Z",
        "language": "en",
        "policy": {},
        "summary": {
            "claims_found": 2,
            "claims_selected": 2,
            "claim_limit": 20,
            "claim_limit_exceeded": False,
            "input_truncated": False,
            "assessments": {"completed": 2, "failed": 0},
            "verifications": {"planned": 0, "completed": 0, "failed": 0},
            "issues": 0,
        },
        "credits": {"charged": 2},
        "failure": None,
    }
    body = {"items": [raw_item], "total": 1, "page": 1, "page_size": 20}
    with Lenz(api_key=KEY, legacy_aliases=False) as client:
        with respx.mock(base_url=BASE) as r:
            r.get("/reviews").respond(200, json=body)
            result = client.list_reviews()
    assert len(result.items) == 1
    item = result.items[0]
    assert item.summary.claim_limit_reached is None
    assert item.summary.claim_limit_exceeded is False
    assert item.summary.claims_found == 2


@pytest.mark.asyncio
async def test_async_list_reviews_respects_client_legacy_aliases_false() -> None:
    raw_item = {
        "review_id": "rev_canonical_1",
        "status": "completed",
        "outcome": "clean",
        "created_at": "2026-10-11T10:00:00Z",
        "completed_at": "2026-10-11T10:01:00Z",
        "language": "en",
        "policy": {},
        "summary": {
            "claims_found": 2,
            "claims_selected": 2,
            "claim_limit": 20,
            "claim_limit_exceeded": False,
            "input_truncated": False,
            "assessments": {"completed": 2, "failed": 0},
            "verifications": {"planned": 0, "completed": 0, "failed": 0},
            "issues": 0,
        },
        "credits": {"charged": 2},
        "failure": None,
    }
    body = {"items": [raw_item], "total": 1, "page": 1, "page_size": 20}
    async with AsyncLenz(api_key=KEY, legacy_aliases=False) as client:
        with respx.mock(base_url=BASE) as r:
            r.get("/reviews").respond(200, json=body)
            result = await client.list_reviews()
    assert len(result.items) == 1
    item = result.items[0]
    assert item.summary.claim_limit_reached is None
    assert item.summary.claim_limit_exceeded is False
    assert item.summary.claims_found == 2


def test_review_list_item_legacy_shape() -> None:
    raw_item = {
        "review_id": "rev_legacy_1",
        "status": "completed",
        "outcome": "issues_found",
        "created_at": "2026-05-13T08:00:00Z",
        "completed_at": "2026-05-13T08:02:00Z",
        "language": "en",
        "policy": {
            "verdicts": ["False", "Mostly False"],
            "confidence": ["low"],
            "max_assessments": 20,
            "max_verifications": 5,
            "depth": "standard",
            "max_citations": 0,
            "suggest_edits": False,
        },
        "summary": {
            "claims_selected": 5,
            "claim_limit": 20,
            "claim_limit_reached": True,
            "input_truncated": False,
            "assessments": {"completed": 5, "failed": 0},
            "verifications": {"planned": 1, "completed": 1, "failed": 0},
            "issues": 1,
        },
        "credits": {"charged": 12},
        "failure": {
            "failure_reason": "no_claim",
            "failure_class": "content",
            "retryable": False,
            "hint": "Check the document text.",
            "docs_url": "https://lenz.io/docs/errors",
        },
    }

    item = ReviewListItem.model_validate(raw_item)
    assert item.review_id == "rev_legacy_1"
    assert item.status == "completed"
    assert item.outcome == "issues_found"
    assert item.summary.claim_limit_reached is True
    assert item.failure is not None
    assert item.failure.failure_reason == "no_claim"
    assert item.failure.code == "no_checkable_claim"


# ── 5. HTTP metadata and raw dict preservation on ReviewList ─────────────────


def test_review_list_metadata(client: Lenz) -> None:
    body = _reviews_page(0, 2, 1, 20, total=2)
    headers = {"X-Request-ID": "req_rev_123", "Content-Type": "application/json"}
    with respx.mock(base_url=BASE) as r:
        r.get("/reviews").respond(200, json=body, headers=headers)
        result = client.list_reviews()

    # Result level: status and ResponseHeaders
    assert result.http_status == 200
    assert isinstance(result.headers, ResponseHeaders)
    assert result.headers["x-request-id"] == "req_rev_123"
    assert result.headers["X-REQUEST-ID"] == "req_rev_123"
    assert type(result.raw) is dict
    assert result.raw == body

    # Nested items: item has raw dict, but top-level HTTP metadata is None
    assert result.items[0].http_status is None
    assert result.items[0].headers is None
    assert type(result.items[0].raw) is dict
    assert result.items[0].raw == body["items"][0]

    # Outside the model dump
    dump = result.model_dump()
    assert "http_status" not in dump
    assert "headers" not in dump
