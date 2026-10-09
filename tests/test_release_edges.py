"""Edge inputs found in the final 3.0 reviews: an empty webhook secret, a
timeout too large for a socket, an infinite stated wait, the 2.x wording of
a blank claim, and a cancel's 422 keeping the server's code."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from lenz_io import Lenz, LenzRateLimitError, LenzWebhookSignatureError, verify_signature
from lenz_io.client import DEFAULT_BASE_URL
from lenz_io.errors import map_response_to_error
from lenz_io.webhooks import _sign

KEY = "lenz_" + "0" * 32


# ── 1. a webhook secret is never empty ─────────────────────────────────────


@pytest.mark.parametrize("secret", ["", None])
def test_verify_signature_refuses_an_empty_secret(secret: Any) -> None:
    body = b'{"event": "verification.completed"}'
    with pytest.raises(ValueError, match="non-empty secret"):
        verify_signature(body, _sign(body, ""), secret)


def test_a_real_secret_still_verifies() -> None:
    body = b"{}"
    assert verify_signature(body, _sign(body, "s3cret"), "s3cret") is True
    with pytest.raises(LenzWebhookSignatureError):
        verify_signature(body, _sign(body, "other"), "s3cret")


# ── 5. a timeout a socket can take ─────────────────────────────────────────

_TOO_LONG = [1e10, 2_147_484, (5, 1e10, 5, 5)]


@pytest.mark.parametrize("bad", _TOO_LONG)
def test_a_timeout_past_the_limit_is_refused_everywhere(bad: Any) -> None:
    for call in (
        lambda: Lenz(api_key=KEY, timeout=bad),
        lambda: Lenz(api_key=KEY).with_options(timeout=bad),
        lambda: Lenz(api_key=KEY).usage(timeout=bad),
        lambda: Lenz(api_key=KEY).extract(text="Doc.", timeout=bad),
    ):
        with pytest.raises(ValueError, match="2,147,483"):
            call()


def test_an_httpx_timeout_past_the_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="2,147,483"):
        Lenz(api_key=KEY, timeout=httpx.Timeout(5, read=1e10))


def test_the_limit_itself_is_accepted() -> None:
    Lenz(api_key=KEY, timeout=2_147_483).with_options(timeout=(1, 2_147_483, None, 1)).close()


# ── 6. an infinite stated wait ─────────────────────────────────────────────


@pytest.mark.parametrize("header", ["1e999", "inf", "-inf", "-1e999", "nan"])
def test_a_non_finite_retry_after_is_no_stated_wait(header: str, monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("lenz_io.client.time.sleep", slept.append)
    with respx.mock(base_url=DEFAULT_BASE_URL) as r:
        route = r.get("/me/usage").respond(
            429, json={"code": "rate_limited", "detail": "slow"}, headers={"Retry-After": header}
        )
        with pytest.raises(LenzRateLimitError):
            Lenz(api_key=KEY).usage()
    # As in Node: no stated wait, so the normal backoff ladder runs.
    assert route.call_count == 4
    assert slept == [1.0, 2.0, 4.0]


def test_a_huge_finite_retry_after_is_clamped_and_raises_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: None)
    with respx.mock(base_url=DEFAULT_BASE_URL) as r:
        route = r.get("/me/usage").respond(
            429, json={"code": "rate_limited", "detail": "slow"}, headers={"Retry-After": "1e300"}
        )
        with pytest.raises(LenzRateLimitError):
            Lenz(api_key=KEY).usage()
    assert route.call_count == 1


def test_an_infinite_retry_after_in_an_error_body_reads_as_unknown() -> None:
    err = map_response_to_error(
        503, json.dumps({"code": "capacity", "detail": "busy", "retry_after": "1e999"}).encode(), {}
    )
    assert err.retry_after is None


# ── 7. a blank claim reads as 2.x worded it ────────────────────────────────


def _blank(loc: list[Any], detail: str) -> bytes:
    return json.dumps(
        {"detail": detail, "code": "blank_input", "errors": [{"loc": loc, "msg": detail, "type": "blank_input"}]}
    ).encode()


@pytest.mark.parametrize(
    ("method", "path", "loc", "expected"),
    [
        ("POST", "/verify", ["body", "claim"], "Text is required."),
        ("POST", "/assess", ["body", "claim"], "Text is required."),
        ("POST", "/verify/batch", ["body", "claims", 1, "claim"], "claims[1].text is required."),
        ("POST", "/verify/t1/select", ["body", "claims"], "texts is required and must be non-empty."),
    ],
)
def test_a_blank_claim_keeps_the_2x_sentence(method: str, path: str, loc: list[Any], expected: str) -> None:
    err = map_response_to_error(422, _blank(loc, "claim is required."), {}, endpoint=(method, path))
    assert err.message == expected


def test_another_blank_field_keeps_the_servers_sentence() -> None:
    err = map_response_to_error(422, _blank(["body", "text"], "text is required."), {}, endpoint=("POST", "/extract"))
    assert err.message == "text is required."


# ── 11. a cancel's 422 keeps the server's code ─────────────────────────────


@pytest.mark.parametrize("path", ["/verify/t1/cancel", "/reviews/r1/cancel", "/citechecks/c1/cancel"])
def test_a_cancel_schema_422_keeps_its_code_and_errors(path: str) -> None:
    errors = [{"type": "missing", "loc": ["body", "x"], "msg": "Field required"}]
    body = {"detail": "Field required", "code": "validation_error", "errors": errors}
    err = map_response_to_error(422, json.dumps(body).encode(), {}, endpoint=("POST", path))
    assert err.code == "validation_error"
    assert err.message == "Field required"


# ── a cancelled task status carries the failure block 2.21 built ───────────

_CANCELLED_BLOCK = {
    "code": "cancelled",
    "detail": "Cancelled.",
    "hint": None,
    "failure_class": "cancelled",
    "retryable": False,
    "docs_url": "https://lenz.io/docs/errors#cancelled",
    "failure_reason": "cancelled",
}


def _block(failure: Any) -> dict[str, Any]:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return {k: getattr(failure, k) for k in _CANCELLED_BLOCK}


def test_a_cancelled_task_status_has_the_cancelled_failure_block() -> None:
    from lenz_io import TaskStatus

    status = TaskStatus.model_validate({"status": "cancelled", "task_id": "t1"})
    assert status.failure is not None
    assert _block(status.failure) == _CANCELLED_BLOCK


def test_a_sent_failure_block_on_a_cancelled_status_is_read_as_sent() -> None:
    from lenz_io import TaskStatus

    status = TaskStatus.model_validate(
        {"status": "cancelled", "task_id": "t1", "failure": {"code": "other", "detail": "x."}}
    )
    assert status.failure is not None and status.failure.code == "other"


def test_other_statuses_still_have_none() -> None:
    from lenz_io import TaskStatus

    assert TaskStatus.model_validate({"status": "processing", "task_id": "t1"}).failure is None
    assert TaskStatus.model_validate({"status": "completed", "task_id": "t1"}).failure is None


def test_the_cancelled_webhooks_verification_carries_it_too() -> None:
    from lenz_io import VerificationCancelled, parse_webhook

    event = parse_webhook(
        {
            "event": "verification.cancelled",
            "event_id": "e1",
            "task_id": "t1",
            "status": "cancelled",
            "verification": {"status": "cancelled", "task_id": "t1"},
        }
    )
    assert isinstance(event, VerificationCancelled)
    assert event.verification is not None and _block(event.verification.failure) == _CANCELLED_BLOCK


def test_a_batch_items_cancelled_status_detail_carries_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: None)
    with respx.mock(base_url=DEFAULT_BASE_URL) as r:
        r.post("/verify/batch").respond(200, json={"batch_id": "b", "items": [{"task_id": "t1", "claim": "A."}]})
        r.get("/verify/status/t1").respond(200, json={"status": "cancelled", "task_id": "t1"})
        [item] = Lenz(api_key=KEY).verify_batch_and_wait(claims=[{"claim": "A."}])
    assert item.status == "failed" and item.status_detail is not None
    assert _block(item.status_detail.failure) == _CANCELLED_BLOCK


def test_reviews_and_citation_checks_keep_the_servers_null() -> None:
    from lenz_io import Citecheck, ReviewFull

    review = ReviewFull.model_validate(
        {"review_id": "r1", "status": "cancelled", "issues": [], "failures": [], "claims": []}
    )
    check = Citecheck.model_validate(
        {"citecheck_id": "c1", "status": "cancelled", "citations": [], "citation_issues": [], "citation_failures": []}
    )
    assert review.failure is None and check.failure is None


# ── the public retry_after follows the same rule as the retry ladder ───────


@pytest.mark.parametrize(
    ("status", "body", "header", "expected"),
    [
        (503, {"detail": "busy"}, "1e300", 2_147_483),
        (503, {"detail": "busy"}, "inf", None),
        (503, {"detail": "busy"}, "nan", None),
        (503, {"detail": "busy"}, "30", 30),
        (503, {"code": "capacity", "detail": "busy", "retry_after": 1e300}, None, 2_147_483),
        (503, {"code": "capacity", "detail": "busy", "retry_after": "inf"}, "45", 45),
        (429, {"code": "rate_limited", "detail": "slow"}, "1e300", 2_147_483),
        (429, {"code": "rate_limited", "detail": "slow", "reset_in_seconds": 7}, "inf", 7),
        # A 429's retry_after is a number, 0 when nothing usable is stated (as in Node).
        (429, {"code": "rate_limited", "detail": "slow"}, "inf", 0),
    ],
)
def test_the_public_retry_after(status: int, body: dict[str, Any], header: str | None, expected: Any) -> None:
    headers = {"Retry-After": header} if header is not None else {}
    err = map_response_to_error(status, json.dumps(body).encode(), headers)
    assert err.retry_after == expected
