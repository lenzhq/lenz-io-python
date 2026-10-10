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
import typing
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
import respx
from conftest import make_client
from pydantic import ValidationError

from lenz_io import (
    LenzApiVersionError,
    LenzAuthError,
    LenzError,
    LenzInvalidKeyError,
    LenzInvalidResponseError,
    LenzQuotaExceededError,
    LenzTimeoutError,
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
    # A ``Result`` not read from an answer: 0 and empty headers, never None.
    assert AssessResponse.model_validate(body).http_status == 0
    assert AssessResponse.model_validate(body).headers == ResponseHeaders()


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


# ── 6. an unreadable poll ─────────────────────────────────────────────────


@pytest.fixture()
def clock(any_client: Any) -> list[float]:
    return any_client.install_clock([0.0])  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    "body",
    [
        {"status": "completed", "task_id": "t1", "result": 42},
        {"status": "failed", "task_id": "t1", "failure": {"code": ["x"]}},
        {"status": "cancelled", "task_id": "t1", "failure": {"retryable": "x"}},
        {"status": "needs_input", "task_id": "t1", "claims": "x"},
    ],
)
def test_an_unreadable_poll_of_an_ended_run_raises_at_once(client: Any, clock: list[float], body: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.get("/verify/status/t1").respond(200, json=body)
        with pytest.raises(LenzInvalidResponseError) as ei:
            client.wait("t1", timeout=300)
    assert route.call_count == 1
    assert ei.value.body == body
    assert isinstance(ei.value.__cause__, ValidationError)


def test_an_unreadable_poll_of_an_ended_run_fails_only_its_batch_item(client: Any, clock: list[float]) -> None:
    accepted = {"items": [{"task_id": "t1", "claim": "A."}, {"task_id": "t2", "claim": "B."}]}
    with respx.mock(base_url=BASE) as r:
        r.post("/verify/batch").respond(202, json=accepted)
        r.get("/verify/status/t1").respond(200, json={"status": "completed", "task_id": "t1", "result": 42})
        r.get("/verify/status/t2").respond(
            200, json={"status": "completed", "task_id": "t2", "result": {"verification_id": "v2", "claim": "B."}}
        )
        out = client.verify_batch_and_wait(claims=[{"claim": "A."}, {"claim": "B."}], timeout=60)
    assert [item.status for item in out] == ["failed", "completed"]
    assert out[0].status_detail is None


def test_an_unreadable_running_poll_is_polled_again_and_named_by_the_timeout(client: Any, clock: list[float]) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.get("/verify/status/t1").respond(200, json={"status": "processing", "task_id": "t1", "claims": 5})
        with pytest.raises(LenzTimeoutError) as ei:
            client.wait("t1", timeout=30)
    assert route.call_count > 1
    err = ei.value
    assert isinstance(err.__cause__, LenzInvalidResponseError)
    assert err.message == "wait timed out after 30s; the last poll's answer could not be read"
    assert "unknown" in err.cause


def test_a_readable_poll_after_an_unreadable_one_clears_it(client: Any, clock: list[float]) -> None:
    bad = httpx.Response(200, json={"status": "processing", "task_id": "t1", "claims": 5})
    running = httpx.Response(200, json={"status": "processing", "task_id": "t1"})
    with respx.mock(base_url=BASE) as r:
        r.get("/verify/status/t1").mock(side_effect=[bad, running, running, running, running, running, running])
        with pytest.raises(LenzTimeoutError) as ei:
            client.wait("t1", timeout=20)
    assert ei.value.__cause__ is None
    assert ei.value.message == "wait timed out after 20s"


def _review_body(status: str, claims: Any) -> dict[str, Any]:
    return {"review_id": "r1", "status": status, "issues": [], "failures": [], "claims": claims}


def test_an_unreadable_review_that_ended_raises_at_once(client: Any, clock: list[float]) -> None:
    with respx.mock(base_url=BASE) as r:
        r.post("/review").respond(202, json={"review_id": "r1", "status": "queued"})
        route = r.get("/reviews/r1").respond(200, json=_review_body("completed", [42]))
        with pytest.raises(LenzInvalidResponseError):
            client.review_and_wait("Draft.", timeout=300)
    assert route.call_count == 1


def test_an_unreadable_running_review_is_named_by_the_timeout(client: Any, clock: list[float]) -> None:
    with respx.mock(base_url=BASE) as r:
        r.post("/review").respond(202, json={"review_id": "r1", "status": "queued"})
        route = r.get("/reviews/r1").respond(200, json=_review_body("running", [42]))
        with pytest.raises(LenzTimeoutError) as ei:
            client.review_and_wait("Draft.", timeout=30)
    assert route.call_count > 1
    assert isinstance(ei.value.__cause__, LenzInvalidResponseError)
    assert ei.value.message.endswith("; the last poll's answer could not be read")


# ── 7. a block read on access ─────────────────────────────────────────────


def test_a_block_read_on_access_raises_an_invalid_response(client: Any) -> None:
    body = {"status": "ready", "claims": [{"claim": "A."}, {"claim": ["x"]}]}
    with respx.mock(base_url=BASE) as r:
        r.post("/extract").respond(200, json=body, headers={"X-Request-ID": "req_x"})
        out = client.extract(text="A.")
    with pytest.raises(LenzInvalidResponseError) as ei:
        out.claims  # noqa: B018 - the read is the test
    err = ei.value
    assert (err.status_code, err.request_id, err.body) == (200, "req_x", body)
    assert isinstance(err.__cause__, ValidationError)
    assert "claims[1].claim" in err.message


@pytest.mark.sync_only
def test_a_nested_rows_failure_read_on_access_names_the_answer() -> None:
    from lenz_io.errors import ResponseHeaders as RH
    from lenz_io.models import _Body

    body = {"claims": [{"claim": "A.", "status": "failed", "failure": {"retryable": "x"}}]}
    out = AssessResponse.model_validate(
        _Body(body, http_status=200, headers=RH({"X-Request-ID": "q"})), context={"legacy_aliases": False}
    )
    with pytest.raises(LenzInvalidResponseError) as ei:
        out.claims[0].failure  # noqa: B018 - the read is the test
    assert ei.value.status_code == 200
    assert ei.value.request_id == "q"
    # The row's own body and path: the row is the model that read the block.
    assert "failure.retryable" in ei.value.message
    assert ei.value.body == body["claims"][0]


def test_a_failed_run_with_a_malformed_failure_without_legacy_aliases(clock: list[float]) -> None:
    body = {"status": "failed", "task_id": "t1", "failure": {"hint": 3}}
    with make_client(api_key=KEY, legacy_aliases=False) as c, respx.mock(base_url=BASE) as r:
        r.get("/verify/status/t1").respond(200, json=body)
        with pytest.raises(LenzInvalidResponseError) as ei:
            c.wait("t1", timeout=30)
    assert ei.value.status_code == 200
    assert isinstance(ei.value.__cause__, ValidationError)


# ── 8. odds and ends ──────────────────────────────────────────────────────


def test_the_message_lists_every_field_and_the_fix_suggests_upgrading(client: Any) -> None:
    body = {"claims": [{"claim": 1}, {"claim": 2}], "more_claims": 5}
    with respx.mock(base_url=BASE) as r:
        r.post("/assess").respond(200, json=body)
        with pytest.raises(LenzInvalidResponseError) as ei:
            client.assess("A.")
    message = ei.value.message
    assert "claims[0].claim" in message and "claims[1].claim" in message
    assert "pip install -U lenz-io" in ei.value.fix


def test_items_an_iterator_yields_have_no_metadata(client: Any) -> None:
    page = {"items": [{"verification_id": "v1", "claim": "A."}], "total": 1, "page": 1, "page_size": 20}
    with respx.mock(base_url=BASE) as r:
        r.get("/verifications").respond(200, json=page)
        items = list(client.verifications.iter())
    assert items and items[0].http_status is None and items[0].headers is None


@pytest.mark.sync_only
@pytest.mark.parametrize(
    "err",
    [
        LenzInvalidKeyError(message="m", cause="c", status_code=0),
        LenzInvalidResponseError(message="m", status_code=200, body={"a": 1}, body_text="t"),
        LenzTimeoutError(message="m", task_id="t1"),
        LenzQuotaExceededError(message="m", status_code=402, remaining=3),
        LenzApiVersionError(api_version="2026-05-13", expected_version="2026-10-11", status_code=200),
    ],
)
def test_every_error_pickles(err: LenzError) -> None:
    again = pickle.loads(pickle.dumps(err))
    assert type(again) is type(err)
    assert str(again) == str(err)
    assert vars(again) == vars(err)


@pytest.mark.sync_only
def test_the_codes_are_exported_at_the_top_level() -> None:
    import lenz_io

    assert lenz_io.USAGE_ERROR_CODES == USAGE_ERROR_CODES
    assert typing.get_args(lenz_io.UsageErrorCode) == USAGE_ERROR_CODES


# ── 9. parity with the Node SDK's follow-ups ──────────────────────────────


@pytest.mark.parametrize("item", [42, None, ["x"]])
def test_an_assess_item_that_is_not_a_string_is_refused_locally(client: Any, item: Any) -> None:
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        with pytest.raises(LenzUsageError) as ei:
            client.assess(claims=["A.", item])
        assert not r.calls
    assert (ei.value.code, ei.value.param) == ("invalid_argument", "claims[1]")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "5", True])
@pytest.mark.parametrize(
    "call",
    [
        lambda c, t: c.wait("t1", timeout=t),
        lambda c, t: c.verify_and_wait("A.", timeout=t),
        lambda c, t: c.verify_batch_and_wait(claims=[{"claim": "A."}], timeout=t),
        lambda c, t: c.review_and_wait("Draft.", timeout=t),
        lambda c, t: c.citecheck_and_wait("Draft.", timeout=t),
    ],
)
def test_a_wait_budget_that_is_not_a_finite_number_is_refused(client: Any, bad: Any, call: Any) -> None:
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        with pytest.raises(LenzUsageError) as ei:
            call(client, bad)
        assert not r.calls
    assert (ei.value.code, ei.value.param) == ("invalid_option", "timeout")


def test_a_wait_budget_of_none_is_the_default(client: Any, clock: list[float]) -> None:
    done = {"status": "completed", "task_id": "t1", "result": {"verification_id": "v1", "claim": "A."}}
    with respx.mock(base_url=BASE) as r:
        r.get("/verify/status/t1").respond(200, json=done)
        assert client.wait("t1", timeout=None).verification_id == "v1"


@pytest.mark.parametrize(
    ("call", "path"),
    [
        (lambda c: c.cancel("t1"), "/verify/t1/cancel"),
        (lambda c: c.cancel_review("r1"), "/reviews/r1/cancel"),
        (lambda c: c.cancel_citecheck("c1"), "/citechecks/c1/cancel"),
    ],
)
def test_a_cancel_answered_with_another_jobs_body_is_an_invalid_response(client: Any, call: Any, path: str) -> None:
    other = {"task_id": "other", "review_id": "other", "citecheck_id": "other", "cancelled": True, "status": "x"}
    with respx.mock(base_url=BASE) as r:
        r.post(path).respond(200, json=other, headers={"X-Request-ID": "req_c"})
        with pytest.raises(LenzInvalidResponseError) as ei:
            call(client)
    err = ei.value
    assert (err.status_code, err.request_id, err.body) == (200, "req_c", other)
    assert err.body_text.startswith("{")


def test_an_ended_but_unreadable_batch_item_carries_its_error(client: Any, clock: list[float]) -> None:
    accepted = {"items": [{"task_id": "t1", "claim": "A."}, {"task_id": "t2", "claim": "B."}]}
    with respx.mock(base_url=BASE) as r:
        r.post("/verify/batch").respond(202, json=accepted)
        r.get("/verify/status/t1").respond(200, json={"status": "failed", "task_id": "t1", "failure": {"code": [1]}})
        r.get("/verify/status/t2").respond(
            200, json={"status": "completed", "task_id": "t2", "result": {"verification_id": "v2", "claim": "B."}}
        )
        out = client.verify_batch_and_wait(claims=[{"claim": "A."}, {"claim": "B."}], timeout=60)
    assert out[0].status == "failed"
    assert out[0].status_detail is None
    assert isinstance(out[0].error, LenzInvalidResponseError)
    assert "error" not in out[0].model_dump()
    assert out[1].status == "completed" and out[1].error is None


def test_an_unreadable_body_of_another_review_is_polled_again(client: Any, clock: list[float]) -> None:
    from lenz_io._polling import _job_of, _unreadable_end

    err = LenzInvalidResponseError(message="m", status_code=200, body={"review_id": "r2", "status": "completed"})
    assert _job_of("/reviews/r1") == ("review_id", "r1")
    assert not _unreadable_end(err, ("completed",), ("review_id", "r1"))
    err.body = {"status": "completed"}
    assert not _unreadable_end(err, ("completed",), ("review_id", "r1"))
    err.body = {"review_id": "r1", "status": "completed"}
    assert _unreadable_end(err, ("completed",), ("review_id", "r1"))


def test_results_compare_by_value_whatever_answer_they_came_in(client: Any) -> None:
    body = {"claims": [_ROW]}
    with respx.mock(base_url=BASE) as r:
        r.post("/assess").respond(200, json=body, headers={"X-Request-ID": "a"})
        first = client.assess("A.")
        r.post("/assess").respond(200, json=body, headers={"X-Request-ID": "b"})
        second = client.assess("A.")
    assert first == second
    assert first == first.model_copy(deep=True)
    assert first == pickle.loads(pickle.dumps(first))
    assert first.claims[0] == second.claims[0]
    assert first == AssessResponse.model_validate(body)
