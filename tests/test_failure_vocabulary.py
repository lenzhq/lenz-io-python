"""One failure vocabulary on the 2026-10-11 shape, and what the transport does
with a failure that will not change.

`/extract` answers an unreadable link with 422 `page_unreadable` (it used to be
a 502), work that failed on our side with 502 `internal`; a canonical 5xx that
says it will not succeed on a retry is not retried.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from lenz_io import Lenz, LenzAPIError, LenzValidationError
from lenz_io.errors import map_response_to_error

CONTRACT = Path(__file__).parent / "fixtures" / "contract"
BASE = "https://lenz.io/api/v1"
CANONICAL = {"X-Lenz-API-Version": "2026-10-11"}
LEGACY = {"X-Lenz-API-Version": "2026-05-13"}


def _load(name: str) -> dict:
    return json.loads((CONTRACT / name).read_text())


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: None)
    return Lenz(api_key="lenz_test_key", max_retries=3)


def test_an_unreadable_link_on_extract_is_a_422_that_says_what_to_send() -> None:
    body = _load("error_extract_page_unreadable_422.json")
    err = map_response_to_error(422, json.dumps(body), CANONICAL, endpoint=("POST", "/extract"))
    assert isinstance(err, LenzValidationError)
    assert err.status_code == 422
    assert err.code == "page_unreadable"
    assert err.message == "The page at the link could not be read."
    assert err.fix == "Paste the page's text instead."
    assert err.retryable is False


def test_a_422_without_a_hint_keeps_the_generic_advice() -> None:
    err = map_response_to_error(422, json.dumps({"detail": "bad", "code": "validation_error"}), {})
    assert err.fix == "Check the request body against the OpenAPI spec."


def test_extract_stops_at_the_first_422_page_unreadable(client) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.post("/extract").respond(422, json=_load("error_extract_page_unreadable_422.json"), headers=CANONICAL)
        with pytest.raises(LenzValidationError) as info:
            client.extract(text="https://example.com/paywalled")
    assert route.call_count == 1
    assert info.value.code == "page_unreadable"


def test_work_that_failed_on_our_side_is_a_502_internal_that_will_not_help_again() -> None:
    body = _load("error_extract_internal_502.json")
    err = map_response_to_error(502, json.dumps(body), CANONICAL, endpoint=("POST", "/extract"))
    assert isinstance(err, LenzAPIError)
    assert (err.status_code, err.code, err.message) == (502, "internal", "Extraction failed")
    assert err.retryable is False
    assert "will not help" in err.fix


def test_a_5xx_without_a_verdict_on_retrying_keeps_the_generic_advice() -> None:
    err = map_response_to_error(502, json.dumps({"detail": "bad gateway"}), {})
    assert err.fix == "Retry; if the error persists, file an issue with the Request ID."


# ── V8: a canonical 5xx that will not succeed on a retry is not retried ──


def _usage_ok() -> httpx.Response:
    return httpx.Response(200, json={"plan": "free", "credits_used": 0, "credits_total": 10}, headers=CANONICAL)


@pytest.mark.parametrize(
    "body",
    [
        {"detail": "Extraction failed", "code": "internal"},
        {"detail": "Failed.", "code": "something_new", "retryable": False},
        {"detail": "Failed.", "failure": {"code": "internal", "retryable": False}},
    ],
    ids=["internal", "retryable_false", "failure_block"],
)
def test_a_canonical_5xx_that_says_it_will_not_succeed_is_sent_once(client, body) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.get("/me/usage").respond(502, json=body, headers=CANONICAL)
        with pytest.raises(LenzAPIError) as info:
            client.usage()
    assert route.call_count == 1
    assert info.value.status_code == 502
    assert info.value.retryable is False


def test_the_async_client_stops_too() -> None:
    import asyncio

    from lenz_io import AsyncLenz

    async def run() -> int:
        async with AsyncLenz(api_key="lenz_test_key", max_retries=3) as c:
            with respx.mock(base_url=BASE) as r:
                route = r.get("/me/usage").respond(502, json={"detail": "x", "code": "internal"}, headers=CANONICAL)
                with pytest.raises(LenzAPIError):
                    await c.usage()
                return route.call_count

    assert asyncio.run(run()) == 1


@pytest.mark.parametrize(
    ("status", "headers", "body"),
    [
        (502, LEGACY, {"detail": "Extraction failed", "code": "extraction_failed", "retryable": False}),
        (502, LEGACY, {"detail": "x", "code": "internal"}),
        (502, {}, {"detail": "x", "code": "internal", "retryable": False}),
        (502, CANONICAL, {"detail": "bad gateway"}),
        (500, CANONICAL, {"detail": "Internal server error.", "code": "internal_error"}),
        (503, CANONICAL, {"detail": "x", "code": "something_new", "retryable": True}),
    ],
    ids=["legacy", "legacy_internal", "gateway", "no_signal", "generic_500", "retryable_true"],
)
def test_every_other_5xx_is_retried_as_before(client, status, headers, body) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.get("/me/usage")
        route.side_effect = [httpx.Response(status, json=body, headers=headers), _usage_ok()]
        client.usage()
    assert route.call_count == 2


def test_the_cli_reports_an_unreadable_link_by_its_own_code_and_advice() -> None:
    from lenz_io.cli.errors import friendly_text, to_payload

    body = _load("error_extract_page_unreadable_422.json")
    err = map_response_to_error(422, json.dumps(body), CANONICAL, endpoint=("POST", "/extract"))
    payload = to_payload(err)["error"]
    assert (payload["code"], payload["status"]) == ("page_unreadable", 422)
    assert payload["fix"] == "Paste the page's text instead."
    assert friendly_text(err).endswith("Fix: Paste the page's text instead.")
    # Any other 422 keeps its code.
    other = map_response_to_error(422, json.dumps({"detail": "bad", "code": "validation_error"}), {})
    assert to_payload(other)["error"]["code"] == "invalid_request"


def test_a_gateway_page_is_retried_as_before(client) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.get("/me/usage")
        route.side_effect = [httpx.Response(502, text="<html>Bad Gateway</html>"), _usage_ok()]
        client.usage()
    assert route.call_count == 2


def test_a_canonical_html_5xx_is_retried_as_before(client) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.get("/me/usage")
        route.side_effect = [httpx.Response(502, text="<html>Bad Gateway</html>", headers=CANONICAL), _usage_ok()]
        client.usage()
    assert route.call_count == 2
