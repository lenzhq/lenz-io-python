"""The version header goes on every request, and an answer in another version
is refused with a typed error instead of being misread."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from conftest import make_client, make_http
from parity_observe import load

import lenz_io
from lenz_io import Lenz, LenzApiVersionError, LenzError, LenzNotFoundError, LenzQuotaExceededError
from lenz_io.client import API_VERSION, DEFAULT_BASE_URL
from lenz_io.webhooks import parse_webhook

# Every test here runs on both clients (``any_client`` in conftest.py): the
# same calls and assertions against ``Lenz`` and ``AsyncLenz``.
pytestmark = pytest.mark.usefixtures("any_client")


KEY = "lenz_" + "0" * 32
OLD = "2026-05-13"


def _seen_versions(headers_on_client: dict[str, str] | None) -> list[str | None]:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-Lenz-API-Version"))
        return httpx.Response(200, json={"api": "ok"})

    http = make_http(transport=httpx.MockTransport(handler), headers=headers_on_client or {})
    with make_client(api_key=KEY, http_client=http) as c:
        c._request("GET", "/")
        c._request("POST", "/verify", json={"claim": "x"}, headers={"X-Lenz-API-Version": OLD})
    return seen


def test_a_custom_http_client_without_the_header_still_sends_it() -> None:
    assert _seen_versions(None) == [API_VERSION, API_VERSION]


def test_a_custom_http_client_with_a_stale_default_is_overridden() -> None:
    assert _seen_versions({"X-Lenz-API-Version": OLD}) == [API_VERSION, API_VERSION]


def test_the_default_client_sends_it() -> None:
    with make_client(api_key=KEY, max_retries=0) as c, respx.mock(base_url=DEFAULT_BASE_URL) as mock:
        route = mock.get("/me/usage").respond(200, json={})
        c._request("GET", "/me/usage")
    assert route.calls.last.request.headers["X-Lenz-API-Version"] == "2026-10-11"


# ── The answer's version ──


@pytest.fixture
def client() -> Any:
    c = make_client(api_key=KEY, max_retries=0)
    yield c
    c.close()


def test_an_answer_in_another_version_raises(client: Lenz) -> None:
    body = {"status": "ready", "claim": "A.", "identified_claims": []}
    with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
        mock.post("/extract").respond(200, json=body, headers={"X-Lenz-API-Version": OLD})
        with pytest.raises(LenzApiVersionError) as info:
            client.extract(text="A.")
    exc = info.value
    assert isinstance(exc, LenzError)
    assert exc.api_version == OLD
    assert exc.status_code == 200
    assert exc.body == body
    assert OLD in exc.message and "2026-10-11" in exc.message
    assert "lenz-io 2.x reads both versions" in exc.fix


def test_the_version_error_names_both_versions(client: Lenz) -> None:
    with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
        mock.get("/me/usage").respond(200, json={}, headers={"X-Lenz-API-Version": OLD})
        with pytest.raises(LenzApiVersionError) as info:
            client.usage()
    exc = info.value
    assert (exc.served_version, exc.expected_version, exc.api_version) == (OLD, API_VERSION, OLD)
    assert exc.message == f"The API answered {OLD}; this SDK reads 2026-10-11 only."
    assert exc.headers["x-lenz-api-version"] == OLD


def test_an_error_answered_in_another_version_raises_the_real_error(client: Lenz) -> None:
    """Since 3.2: a 402 keeps its balance, whatever version answered it."""
    body = {"detail": "No remaining credits.", "code": "no_credits", "credits_remaining": 4, "cost": 10}
    with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
        mock.post("/verify").respond(402, json=body, headers={"X-Lenz-API-Version": OLD})
        with pytest.raises(LenzQuotaExceededError) as info:
            client.verify("A.")
    assert not isinstance(info.value, LenzApiVersionError)
    assert info.value.status_code == 402
    assert info.value.body == body
    assert info.value.code == "no_credits"
    assert info.value.credit_balance == 4
    assert info.value.served_version == OLD


@pytest.mark.parametrize(
    ("status", "body", "headers", "cls", "wait"),
    [
        (429, {"detail": "slow", "code": "rate_limited"}, {"Retry-After": "7200"}, "LenzRateLimitError", 7200),
        (503, {"detail": "busy", "code": "capacity", "retry_after": 120}, {}, "LenzUpstreamUnavailableError", 120),
    ],
)
def test_a_wait_answered_in_another_version_keeps_its_retry_after(
    client: Lenz, status: int, body: dict[str, Any], headers: dict[str, str], cls: str, wait: int
) -> None:
    with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
        mock.post("/assess").respond(status, json=body, headers={**headers, "X-Lenz-API-Version": OLD})
        with pytest.raises(LenzError) as info:
            client.assess("A.")
    assert type(info.value).__name__ == cls
    assert info.value.retry_after == wait
    assert info.value.served_version == OLD


def test_an_error_in_the_current_version_names_it(client: Lenz) -> None:
    with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
        mock.get("/me/usage").respond(401, json={"detail": "no"}, headers={"X-Lenz-API-Version": API_VERSION})
        with pytest.raises(LenzError) as info:
            client.usage()
        assert info.value.served_version == API_VERSION
        mock.get("/me/usage").respond(401, json={"detail": "no"})
        with pytest.raises(LenzError) as info:
            client.usage()
        assert info.value.served_version is None


def test_a_404_delete_in_another_version_is_not_read_as_already_deleted(client: Lenz) -> None:
    with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
        mock.delete("/verifications/v1").respond(
            404, json={"detail": "Not found."}, headers={"X-Lenz-API-Version": OLD}
        )
        with pytest.raises(LenzNotFoundError) as info:
            client.verifications.delete("v1")
    assert info.value.served_version == OLD


def test_a_non_json_answer_in_another_version_still_raises(client: Lenz) -> None:
    with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
        mock.get("/me/usage").respond(200, content=b"<html>", headers={"X-Lenz-API-Version": OLD})
        with pytest.raises(LenzApiVersionError) as info:
            client.usage()
    assert info.value.body is None


def test_the_current_version_and_a_missing_header_proceed(client: Lenz) -> None:
    usage = load("canonical", "account__me_usage_free.json")["body"]
    with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
        mock.get("/me/usage").respond(200, json=usage, headers={"X-Lenz-API-Version": API_VERSION})
        assert client.usage().plan
        mock.get("/me/usage").respond(200, json=usage)
        assert client.usage().plan


@pytest.mark.parametrize(
    ("call", "name"),
    [
        (lambda c: c.verify("A.", idempotency_key="k"), "verify__stored_replay_202.json"),
        (lambda c: c.assess("A.", idempotency_key="k"), "assess__stored_replay_200.json"),
        (lambda c: c.extract(text="A.", idempotency_key="k"), "extract__stored_replay_200.json"),
        (lambda c: c.review("A.", idempotency_key="k"), "review__stored_replay_202.json"),
    ],
)
def test_a_stored_replay_in_the_old_shape_is_refused(client: Lenz, call: Any, name: str) -> None:
    """The API answers a replay of an idempotent request stored by an older
    release in the old shape, and says so in the header."""
    fixture = load("legacy", name)
    path = "/" + name.split("__", 1)[0]
    with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
        mock.post(path).respond(fixture["status"], json=fixture["body"], headers={"X-Lenz-API-Version": OLD})
        with pytest.raises(LenzApiVersionError) as info:
            call(client)
    assert info.value.api_version == OLD
    assert info.value.body == fixture["body"]


def test_the_guard_does_not_apply_to_webhook_parsing() -> None:
    event = load("legacy", "webhook__verification_completed.json")["body"]
    assert parse_webhook(event).event == "verification.completed"


def test_the_error_is_exported_and_pickles_its_fields() -> None:
    assert "LenzApiVersionError" in lenz_io.__all__
    exc = LenzApiVersionError(message="m", status_code=200, body={"a": 1}, api_version=OLD)
    assert (exc.api_version, exc.body, exc.status_code) == (OLD, {"a": 1}, 200)
    assert json.dumps(exc.body)


def test_waiting_never_swallows_the_version_error(client: Lenz) -> None:
    # A version error is never read as a slow poll: it stops that id at once
    # (``wait`` raises it; a batch marks that item failed).
    with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
        route = mock.get(url__regex=r".*/verify/.*").respond(
            200, json={"status": "processing"}, headers={"X-Lenz-API-Version": OLD}
        )
        terminal, timed_out, stopped = client._poll_to_terminal(["a" * 32], 5)
        with pytest.raises(LenzApiVersionError):
            client.wait("a" * 32, timeout=5)
    assert isinstance(stopped["a" * 32], LenzApiVersionError)
    assert terminal == {} and timed_out == set()
    assert route.call_count == 2
