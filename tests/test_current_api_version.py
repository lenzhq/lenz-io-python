"""3.0.0 asks for the API's current response shape (``2026-10-11``) and keeps
every 2.x attribute's meaning.

``test_parity.py`` runs every recorded response pair through the models, the
error mapping, the CLI and the webhook parser. This file covers what the pair
fixtures cannot show on their own: the header the client sends, the request
bodies that depend on it, and the error mapping as the client drives it (the
request's endpoint decides a 422's original ``code`` and wording).
"""

from __future__ import annotations

import json
import warnings
from typing import Any

import pytest
import respx
from parity_observe import load

from lenz_io import Lenz
from lenz_io.client import API_VERSION, DEFAULT_BASE_URL
from lenz_io.errors import LenzQuotaExceededError, LenzValidationError
from lenz_io.models import AssessResponse, ReviewVerification, Usage
from lenz_io.webhooks import VerificationCompleted, parse_webhook

KEY = "lenz_" + "0" * 32


@pytest.fixture
def client() -> Any:
    c = Lenz(api_key=KEY, max_retries=0)
    yield c
    c.close()


def _canonical(name: str) -> dict[str, Any]:
    return load("canonical", name)


# ── The version the client asks for ──


def test_the_client_asks_for_the_current_shape(client: Lenz) -> None:
    assert API_VERSION == "2026-10-11"
    with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
        route = mock.get("/me/usage").respond(200, json=_canonical("account__me_usage_free.json")["body"])
        client.usage()
    assert route.calls.last.request.headers["X-Lenz-API-Version"] == "2026-10-11"


# ── Request bodies: ``webhook_url`` keeps its 2.x meaning ──
#
# In the current shape ``webhook_url`` means the same on every endpoint:
# left out (or null) = the key's default webhook, ``""`` = no webhook for this
# request. The SDK's parameters keep what they meant in 2.x.


def _sent(route: Any) -> dict[str, Any]:
    return json.loads(route.calls.last.request.content)


def test_verify_without_a_webhook_url_leaves_it_out(client: Lenz) -> None:
    """2.x: ``webhook_url=""`` (the default) meant the key's default webhook."""
    with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
        route = mock.post("/verify").respond(202, json={"task_id": "t" * 32, "status": "queued"})
        client.verify("The Earth is round.")
        client.verify("The Earth is round.", webhook_url="")
    assert "webhook_url" not in _sent(route)


def test_verify_batch_leaves_out_every_empty_webhook_url(client: Lenz) -> None:
    with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
        route = mock.post("/verify/batch").respond(202, json={"batch_id": "b", "items": []})
        client.verify_batch(
            claims=[{"claim": "A.", "webhook_url": ""}, {"text": "B.", "webhook_url": "https://h.test/x"}]
        )
    body = _sent(route)
    assert "webhook_url" not in body
    assert "webhook_url" not in body["claims"][0]
    assert body["claims"][1]["webhook_url"] == "https://h.test/x"


@pytest.mark.parametrize(
    ("method", "path", "receipt"),
    [
        ("review", "/review", {"review_id": "r1", "status": "queued"}),
        ("citecheck", "/citecheck", {"citecheck_id": "c1", "status": "queued"}),
    ],
)
def test_review_and_citecheck_keep_their_webhook_url_meaning(
    client: Lenz, method: str, path: str, receipt: dict[str, Any]
) -> None:
    """2.x: ``None`` = the key's default (left out), ``""`` = no webhook
    (sent as ``""``, which means the same in the current shape)."""
    with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
        route = mock.post(path).respond(202, json=receipt)
        getattr(client, method)("Draft text with a claim.")
        assert "webhook_url" not in _sent(route)
        getattr(client, method)("Draft text with a claim.", webhook_url="")
        assert _sent(route)["webhook_url"] == ""


def test_a_202_receipt_reads_as_the_200_did(client: Lenz) -> None:
    """A repeated /verify answered from the first submission is a 202 now (a
    200 before), without ``chain_id``: the SDK returns the same receipt."""
    with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
        mock.post("/verify").respond(202, json=_canonical("verify__implicit_repeat_replay.json")["body"])
        receipt = client.verify("The Earth is round.")
    assert receipt.task_id == _canonical("verify__implicit_repeat_replay.json")["body"]["task_id"]


# ── Errors, as the client maps them ──


def _raise(client: Lenz, method: str, path: str, fixture: str, call: Any) -> Any:
    response = _canonical(fixture)
    with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
        mock.route(method=method, path=path).respond(response["status"], json=response["body"])
        with pytest.raises(Exception) as caught:
            call()
    return caught.value


def test_a_review_422_keeps_its_original_code_and_wording(client: Lenz) -> None:
    err = _raise(client, "POST", "/review", "review__422_unsupported_language.json", lambda: client.review("Draft."))
    original = load("legacy", "review__422_unsupported_language.json")["body"]
    assert isinstance(err, LenzValidationError)
    assert err.code == "validation_error" == original["code"]
    assert err.message == original["detail"]
    assert err.errors == original["errors"]
    # The body as sent is still there.
    assert err.body["code"] == "unsupported_language"


def test_a_review_schema_422_names_the_body_parameter_as_before(client: Lenz) -> None:
    err = _raise(client, "POST", "/review", "review__422_depth_unknown.json", lambda: client.review("Draft."))
    original = load("legacy", "review__422_depth_unknown.json")["body"]
    assert err.message == original["detail"]
    assert err.errors == original["errors"]


def test_a_blank_assess_item_keeps_blank_item(client: Lenz) -> None:
    # ``assess`` refuses a blank item before sending (since 3.2): the request
    # is sent as it is to read the server's answer.
    err = _raise(
        client,
        "POST",
        "/assess",
        "assess__422_blank_item.json",
        lambda: client._request("POST", "/assess", json={"claims": ["A.", " "]}),
    )
    assert err.code == "blank_item"
    assert err.errors == []


def test_a_schema_422_lists_its_field_errors_as_the_message(client: Lenz) -> None:
    """The original shape's ``detail`` was the list itself; the exception's
    message and ``errors`` read exactly as they did."""
    err = _raise(client, "POST", "/verify", "verify__invalid_depth_422.json", lambda: client.verify("A claim."))
    original = load("legacy", "verify__invalid_depth_422.json")["body"]["detail"]
    assert err.message == str(original)
    assert err.code == ""
    assert [list(i) for i in err.errors] == [list(i) for i in original]


def test_a_citation_402_reports_the_credit_pool(client: Lenz) -> None:
    err = _raise(client, "POST", "/citecheck", "citecheck__402_no_credits.json", lambda: client.citecheck("Draft."))
    assert isinstance(err, LenzQuotaExceededError)
    assert (err.remaining, err.credit_balance, err.cost) == (100, 100, 1)


# ── Values the current shape leaves out, rebuilt with their 2.x meaning ──


def test_a_no_claim_assess_keeps_its_original_error_sentence() -> None:
    out = AssessResponse.model_validate(_canonical("assess__single_no_claim.json")["body"])
    assert out.error == "No verifiable claim detected"
    assert out.error_code == "no_claim"


def test_a_failed_deep_check_in_a_review_says_not_a_claim() -> None:
    row = ReviewVerification.model_validate(
        {"status": "failed", "failure": {"code": "no_checkable_claim", "detail": "x", "hint": None}}
    )
    assert row.failure is not None
    assert row.failure.failure_reason == "not_a_claim"
    assert row.failure.code == "no_checkable_claim"


def test_usage_blocks_without_published_prices_use_the_original_ones() -> None:
    usage = Usage.model_validate({"plan": "free", "credits": {"total": 100, "used": 0, "remaining": 100, "extra": 5}})
    assert usage.verify.quota_total == 10
    assert usage.ask.quota_total == 100
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert usage.assess.credits == 5


def test_usage_quota_used_never_goes_below_zero() -> None:
    usage = Usage.model_validate(
        {"plan": "free", "credits": {"total": 5, "used": 0, "remaining": 25, "extra": 20}, "costs": {"verify": 10}}
    )
    assert usage.verify.quota_used == 0


def test_a_sparse_completed_webhook_result_has_every_original_key() -> None:
    event = parse_webhook(_canonical("webhook__verification_completed_sparse_result.json")["body"])
    original = parse_webhook(load("legacy", "webhook__verification_completed_sparse_result.json")["body"])
    assert isinstance(event, VerificationCompleted)
    assert isinstance(original, VerificationCompleted)
    assert event.result == original.result
    assert list(event.result) == list(original.result)


def test_a_failed_deep_check_says_not_a_claim_on_issues_and_failures() -> None:
    from lenz_io.models import ReviewFailure, ReviewIssue

    block = {"code": "no_checkable_claim", "detail": "x", "hint": None}
    issue = ReviewIssue.model_validate({"claim_index": 0, "verdict": "Mixed", "failure": dict(block)})
    deep = ReviewFailure.model_validate({"claim_index": 0, "stage": "verification", "failure": dict(block)})
    quick = ReviewFailure.model_validate({"claim_index": 0, "stage": "assessment", "failure": dict(block)})
    assert issue.failure is not None and issue.failure.failure_reason == "not_a_claim"
    assert deep.failure is not None and deep.failure.failure_reason == "not_a_claim"
    assert quick.failure is not None and quick.failure.failure_reason == "no_claim"


def test_a_receipt_without_chain_id_reads_it_as_empty() -> None:
    from lenz_io.models import TaskAccepted

    current = TaskAccepted.model_validate(_canonical("verify__submit_202.json")["body"])
    assert current.chain_id == ""


@pytest.mark.parametrize("blank", ["", "   ", "\t\n", None])
def test_a_blank_verify_webhook_url_is_left_out(client: Lenz, blank: Any) -> None:
    """2.x: the API read a blank ``webhook_url`` on /verify as left out, so
    the key's default webhook fired. The current shape reads a blank value as
    "no webhook", so the SDK leaves it out to keep the 2.x meaning."""
    with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
        single = mock.post("/verify").respond(202, json={"task_id": "t" * 32, "status": "queued"})
        batch = mock.post("/verify/batch").respond(202, json={"batch_id": "b", "items": []})
        client.verify("The Earth is round.", webhook_url=blank)
        client.verify_batch(claims=[{"claim": "A.", "webhook_url": blank}], webhook_url=blank)
    assert "webhook_url" not in _sent(single)
    body = _sent(batch)
    assert "webhook_url" not in body
    assert "webhook_url" not in body["claims"][0]


@pytest.mark.parametrize(
    ("fixture", "method", "path", "code"),
    [
        ("verify__status_unknown_404.json", "GET", "/verify/status/t1", ""),
        ("verify__unauthenticated_401.json", "POST", "/verify", ""),
        ("review__401_no_credentials.json", "POST", "/review", ""),
        ("errors__ask_not_completed.json", "POST", "/ask/v1", ""),
        ("verify__idempotency_conflict_409.json", "POST", "/verify", ""),
        ("verify__verification_not_ready_409.json", "GET", "/verifications/t1", "verification_not_ready"),
        ("review__get_404_not_found.json", "GET", "/reviews/r1", "not_found"),
    ],
)
def test_an_error_code_reads_as_it_did_in_2x(fixture: str, method: str, path: str, code: str) -> None:
    """The current shape names a ``code`` on every error; where the 2.x
    error carried none, the exception's ``code`` stays ``""``."""
    from lenz_io.errors import map_response_to_error

    response = _canonical(fixture)
    err = map_response_to_error(response["status"], json.dumps(response["body"]), endpoint=(method, path))
    assert err.code == code == (load("legacy", fixture)["body"].get("code") or "")
    assert err.body == response["body"]


@pytest.mark.parametrize(
    ("code", "sentence"),
    [
        ("research_empty", "Pipeline stopped at: research_empty"),
        ("cancelled", "Cancelled."),
        ("task_stuck", "The task was never completed and has been marked failed."),
        ("no_checkable_claim", "Not a verifiable claim."),
    ],
)
def test_a_failed_poll_reads_its_2x_sentence(code: str, sentence: str) -> None:
    from lenz_io.models import TaskStatus

    status = TaskStatus.model_validate(
        {"status": "failed", "task_id": "t", "failure": {"code": code, "detail": "Current wording."}}
    )
    assert status.error == sentence
    assert status.failure is not None and status.failure.detail == "Current wording."


@pytest.mark.parametrize(
    ("status", "code"), [(500, "internal_error"), (400, "invalid_request"), (422, "invalid_request")]
)
def test_the_fallback_codes_read_as_none(status: int, code: str) -> None:
    from lenz_io.errors import map_response_to_error

    err = map_response_to_error(status, json.dumps({"detail": "x", "code": code}), endpoint=("POST", "/verify"))
    assert err.code == ""
    assert err.body == {"detail": "x", "code": code}


def test_a_batch_item_language_error_names_its_item() -> None:
    from lenz_io.errors import map_response_to_error

    sentence = "Unsupported language 'xx'. Supported: en."
    body = {
        "detail": sentence,
        "code": "unsupported_language",
        "errors": [{"loc": ["body", "claims", 1, "language"], "msg": sentence, "type": "unsupported_language"}],
    }
    err = map_response_to_error(422, json.dumps(body), endpoint=("POST", "/verify/batch"))
    assert err.message == f"claims[1].{sentence}"
    assert err.code == ""
    assert err.errors == []


def test_deprecated_property_aliases_are_marked_without_warning() -> None:
    """PEP 702 markers (editors and type checkers see them); no runtime warning,
    so code under ``-W error`` keeps working."""
    import warnings

    from lenz_io.errors import LenzQuotaExceededError
    from lenz_io.models import TaskAccepted

    for prop in (TaskAccepted.chain_id, LenzQuotaExceededError.credits_remaining):
        assert isinstance(prop, property) and isinstance(getattr(prop.fget, "__deprecated__", None), str)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert TaskAccepted.model_validate({"task_id": "t"}).chain_id == ""


# ── ``credit_balance`` is restored for the citation check only ──


def _no_credits(path: str, body: dict[str, Any], client: Lenz) -> LenzQuotaExceededError:
    with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
        mock.post(path).respond(402, json=body)
        with pytest.raises(LenzQuotaExceededError) as info:
            if path == "/citecheck":
                client.citecheck(text="See https://example.org/a.")
            else:
                client.review("A claim.")
    return info.value


def test_a_citecheck_402_without_the_pool_reads_it_as_remaining(client: Lenz) -> None:
    body = _canonical("citecheck__402_no_credits.json")["body"]
    assert "credits_remaining" not in body
    assert _no_credits("/citecheck", body, client).credit_balance == body["remaining"]


def test_another_endpoint_does_not_invent_a_credit_balance(client: Lenz) -> None:
    body = {"detail": "No remaining credits.", "code": "no_credits", "remaining": 7, "cost": 1}
    assert _no_credits("/review", body, client).credit_balance is None
    from lenz_io.errors import map_response_to_error

    assert map_response_to_error(402, json.dumps(body)).credit_balance is None  # type: ignore[attr-defined]
