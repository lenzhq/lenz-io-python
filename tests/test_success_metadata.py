"""3.2, batch 3, on both clients:

* Every result read from a response carries ``http_status`` and ``headers``
  (the top-level result only), outside ``model_dump()`` like ``raw``.
* A ``ReviewStarted`` / ``CitecheckStarted`` a 409 naming the job settled says
  ``settled_by_conflict``.
* ``LenzUsageError`` carries ``code`` and ``param`` (the Node SDK's strings).
* A key that cannot be sent raises ``LenzInvalidKeyError`` (a ``LenzAuthError``).
* A JSON object with a field of the wrong type raises
  ``LenzInvalidResponseError`` (the pydantic error is its ``__cause__``).
"""

from __future__ import annotations

import pickle
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import respx
from conftest import make_client
from pydantic import ValidationError

from lenz_io import (
    LenzAuthError,
    LenzError,
    LenzInvalidKeyError,
    LenzInvalidResponseError,
    LenzUsageError,
    LenzWebhooks,
    parse_webhook,
)
from lenz_io.errors import USAGE_ERROR_CODES, ResponseHeaders
from lenz_io.models import AssessResponse, BatchItemResult, ReviewStarted, TaskAccepted
from lenz_io.webhooks import verify_signature

pytestmark = pytest.mark.usefixtures("any_client")

BASE = "https://lenz.io/api/v1"
KEY = "lenz_test_abc123"
_ROW = {"claim": "A.", "verdict": "True", "confidence": "high"}


@pytest.fixture()
def client() -> Iterator[Any]:
    with make_client(api_key=KEY, max_retries=0) as c:
        yield c


# ── 1. http_status and headers ────────────────────────────────────────────


def test_a_result_carries_the_status_and_headers_of_its_answer(client: Any) -> None:
    body = {"claims": [_ROW], "more_claims": []}
    with respx.mock(base_url=BASE) as r:
        r.post("/assess").respond(200, json=body, headers={"X-Request-ID": "req_1"})
        out = client.assess("A.")
    assert out.http_status == 200
    assert isinstance(out.headers, ResponseHeaders)
    assert out.headers["x-request-id"] == "req_1"
    assert out.headers["X-REQUEST-ID"] == "req_1"
    # The top-level result only.
    assert out.claims[0].http_status is None
    assert out.claims[0].headers is None
    # Outside the dump, like raw; raw is a plain dict.
    assert "http_status" not in out.model_dump()
    assert "headers" not in out.model_dump()
    assert type(out.raw) is dict
    assert out.raw == body


def test_a_receipt_reports_202_and_its_location_and_retry_after(client: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        r.post("/verify").respond(
            202,
            json={"task_id": "t1", "status": "processing", "poll_url": "/api/v1/verify/status/t1"},
            headers={"Location": "/api/v1/verify/status/t1", "Retry-After": "5"},
        )
        accepted = client.verify("A.")
    assert accepted.http_status == 202
    assert accepted.headers is not None
    assert accepted.headers["location"] == "/api/v1/verify/status/t1"
    assert accepted.headers["retry-after"] == "5"


def test_a_nested_result_and_a_built_one_have_none(client: Any) -> None:
    body = {"status": "completed", "task_id": "t1", "result": {"verification_id": "v1", "claim": "A."}}
    with respx.mock(base_url=BASE) as r:
        r.get("/verify/status/t1").respond(200, json=body)
        status = client.get_status("t1")
    assert status.http_status == 200
    assert status.result is not None and status.result.http_status is None
    assert BatchItemResult(task_id="t", status="timeout").http_status is None
    assert TaskAccepted(task_id="t").headers is None
    assert AssessResponse.model_validate(body).http_status is None


def test_metadata_survives_pickling_and_copying(client: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        r.post("/assess").respond(200, json={"claims": [_ROW]}, headers={"X-Request-ID": "req_2"})
        out = client.assess("A.")
    again = pickle.loads(pickle.dumps(out))
    assert again.http_status == 200
    assert again.headers is not None and again.headers["x-request-id"] == "req_2"
    assert type(again.raw) is dict
    assert out.model_copy(deep=True).http_status == 200


# ── 2. settled_by_conflict ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "field", "call"),
    [
        ("/review", "review_id", lambda c: c.review("Draft.", idempotency_key="pinned")),
        ("/citecheck", "citecheck_id", lambda c: c.citecheck("Draft.", idempotency_key="pinned")),
    ],
)
def test_a_409_naming_the_job_is_settled_by_conflict(client: Any, path: str, field: str, call: Any) -> None:
    conflict = {"detail": "in flight", "code": "idempotency_conflict", field: "j9"}
    with respx.mock(base_url=BASE) as r:
        r.post(path).respond(409, json=conflict, headers={"X-Request-ID": "req_409"})
        started = call(client)
    assert started.settled_by_conflict is True
    assert started.http_status == 409
    assert started.headers["x-request-id"] == "req_409"
    assert started.raw == conflict
    assert "settled_by_conflict" not in started.model_dump()

    with respx.mock(base_url=BASE) as r:
        r.post(path).respond(202, json={field: "j9", "status": "queued"})
        receipt = call(client)
    assert receipt.settled_by_conflict is False
    assert receipt.http_status == 202


def test_a_receipt_built_by_hand_was_not_settled_by_a_conflict() -> None:
    assert ReviewStarted(review_id="r").settled_by_conflict is False


# ── 3. LenzUsageError.code and .param ─────────────────────────────────────


_USAGE: list[tuple[str, Callable[[Any], Any], str, str | None]] = [
    ("blank verify", lambda c: c.verify("  "), "blank_input", "claim"),
    ("blank assess", lambda c: c.assess(""), "blank_input", "claim"),
    ("blank review", lambda c: c.review(" "), "blank_input", "text"),
    ("citecheck neither", lambda c: c.citecheck(" "), "blank_input", "text"),
    ("blank item", lambda c: c.assess(claims=["A.", "B.", " "]), "blank_item", "claims[2]"),
    ("empty list", lambda c: c.assess(claims=[]), "empty_list", "claims"),
    ("select empty", lambda c: c.select("t1", claims=[]), "empty_list", "claims"),
    ("both forms", lambda c: c.assess("A.", claims=["B."]), "conflicting_input", "claims"),
    ("citecheck both", lambda c: c.citecheck("Draft.", pairs=[]), "conflicting_input", "text"),
    (
        "citecheck pairs max",
        lambda c: c.citecheck(pairs=[{"statement": "A.", "url": "https://a.example"}], max_citations=2),
        "conflicting_input",
        "max_citations",
    ),
    ("page size", lambda c: c.verifications.list(page_size=0), "invalid_page_size", "page_size"),
    ("page", lambda c: next(iter(c.verifications.iter(page=0))), "invalid_page", "page"),
    ("empty id", lambda c: c.get_status(""), "invalid_id", "task_id"),
    ("dots id", lambda c: c.verifications.get(".."), "invalid_id", "verification_id"),
    ("surrogate id", lambda c: c.get_review("\ud800"), "invalid_id", "review_id"),
    ("citecheck id", lambda c: c.get_citecheck(""), "invalid_id", "citecheck_id"),
    (
        "reserved header",
        lambda c: c.usage(extra_headers={"Authorization": "x"}),
        "invalid_header",
        "extra_headers",
    ),
    ("header value", lambda c: c.usage(extra_headers={"X-A": "a\nb"}), "invalid_header", "extra_headers"),
    ("user agent", lambda c: make_client(api_key=KEY, user_agent="a\nb"), "invalid_header", "user_agent"),
    ("call timeout", lambda c: c.usage(timeout=-1), "invalid_option", "timeout"),
    ("call retries", lambda c: c.usage(max_retries=-1), "invalid_option", "max_retries"),
    ("copy timeout", lambda c: c.with_options(timeout=0), "invalid_option", "timeout"),
    ("copy key", lambda c: c.with_options(api_key=5), "invalid_option", "api_key"),
    ("ctor retries", lambda c: make_client(api_key=KEY, max_retries=-1), "invalid_option", "max_retries"),
    ("ctor legacy", lambda c: make_client(api_key=KEY, legacy_aliases=1), "invalid_option", "legacy_aliases"),
    ("view", lambda c: c.get_review("r1", view="x"), "invalid_argument", "view"),
    ("selector", lambda c: c.review("Draft.", verdicts="False"), "invalid_argument", "verdicts"),
    ("random iter", lambda c: next(iter(c.library.iter(sort="random"))), "invalid_argument", "sort"),
]


@pytest.mark.parametrize(("case", "call", "code", "param"), _USAGE, ids=[u[0] for u in _USAGE])
def test_a_usage_error_names_its_code_and_param(
    client: Any, case: str, call: Callable[[Any], Any], code: str, param: str | None
) -> None:
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        with pytest.raises(LenzUsageError) as ei:
            call(client)
        assert not r.calls  # refused before anything was sent
    assert ei.value.code == code
    assert ei.value.param == param
    assert code in USAGE_ERROR_CODES
    assert isinstance(ei.value, ValueError)
    assert not isinstance(ei.value, LenzError)


def test_the_message_is_unchanged_and_the_fields_survive_pickling(client: Any) -> None:
    with pytest.raises(LenzUsageError) as ei:
        client.assess(claims=["A.", " "])
    assert str(ei.value) == "claims[1] is blank."
    assert ei.value.args == ("claims[1] is blank.",)
    again = pickle.loads(pickle.dumps(ei.value))
    assert (str(again), again.code, again.param) == ("claims[1] is blank.", "blank_item", "claims[1]")


def test_a_usage_error_built_by_hand_is_invalid_argument() -> None:
    err = LenzUsageError("Something.")
    assert (err.code, err.param, str(err)) == ("invalid_argument", None, "Something.")


@pytest.mark.sync_only
def test_webhook_argument_errors_carry_codes() -> None:
    with pytest.raises(LenzUsageError) as ei:
        LenzWebhooks(secret="")
    assert (ei.value.code, ei.value.param) == ("invalid_option", "secret")
    with pytest.raises(LenzUsageError) as ei:
        verify_signature(b"{}", "t=1,v1=x", "")
    assert (ei.value.code, ei.value.param) == ("invalid_argument", "secret")
    with pytest.raises(LenzUsageError) as ei:
        parse_webhook(b"[]")
    assert (ei.value.code, ei.value.param) == ("invalid_argument", "body")


# ── 4. LenzInvalidKeyError ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "make", [lambda: make_client(api_key="lenz_a b"), lambda: make_client().with_options(api_key="lenz_é")]
)
def test_a_key_that_cannot_be_sent_is_an_invalid_key_error(make: Callable[[], Any]) -> None:
    with pytest.raises(LenzInvalidKeyError) as ei:
        make()
    assert isinstance(ei.value, LenzAuthError)
    assert ei.value.status_code == 0
    assert ei.value.retryable is None


def test_a_key_the_server_refuses_is_a_plain_auth_error(client: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        r.get("/me/usage").respond(401, json={"detail": "Invalid api key", "code": "not_authenticated"})
        with pytest.raises(LenzAuthError) as ei:
            client.usage()
    assert type(ei.value) is LenzAuthError


def test_whitespace_around_a_key_is_dropped_silently(recwarn: pytest.WarningsRecorder) -> None:
    with make_client(api_key=" \t" + KEY + "\n") as c, respx.mock(base_url=BASE) as r:
        route = r.get("/me/usage").respond(200, json={"plan": "free"})
        c.usage()
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {KEY}"
    assert not recwarn.list


# ── 5. a field of the wrong type ──────────────────────────────────────────


@pytest.mark.parametrize("body", [{"claims": "x"}, {"claims": [42]}])
def test_a_field_of_the_wrong_type_is_an_invalid_response(client: Any, body: dict[str, Any]) -> None:
    with respx.mock(base_url=BASE) as r:
        r.post("/assess").respond(200, json=body, headers={"X-Request-ID": "req_bad"})
        with pytest.raises(LenzInvalidResponseError) as ei:
            client.assess("A.", idempotency_key="k")
    err = ei.value
    assert err.status_code == 200
    assert err.request_id == "req_bad"
    assert err.headers["x-request-id"] == "req_bad"
    assert err.body == body
    assert err.body_text.startswith('{"claims":')
    assert err.retryable is None
    assert err.idempotency_key == "k"
    assert isinstance(err.__cause__, ValidationError)
    assert "claims" in err.message


def test_the_null_rules_apply_before_the_type_check(client: Any) -> None:
    body = {"claims": [{"claim": "A.", "verdict": None, "confidence": None, "hint": None}]}
    with respx.mock(base_url=BASE) as r:
        r.post("/assess").respond(200, json=body)
        out = client.assess("A.")
    assert out.claims[0].claim == "A."
    assert out.http_status == 200
