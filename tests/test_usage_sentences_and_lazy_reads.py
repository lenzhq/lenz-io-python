"""3.2, batch 4, on both clients:

* A blank input or an empty list is refused before sending with the sentence
  the API's ``2026-10-11`` 422 gives for the same request (``code`` and
  ``param`` are the SDK's own).
* ``select`` refuses a blank item, ``ask.send`` a blank message.
* Every property that reads a block on access raises
  ``LenzInvalidResponseError`` for a value of the wrong type, instead of
  reading it as not sent.
* ``Result``: every top-level result's ``http_status`` / ``headers`` are an
  ``int`` and a ``ResponseHeaders``.
* ``LenzMissingKeyError`` for a call that needs a key and has none.
* A ``completed`` poll with no ``result`` is a run that ended unreadable.
"""

from __future__ import annotations

import pickle
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import respx
from conftest import make_client

from lenz_io import (
    LenzAuthError,
    LenzInvalidKeyError,
    LenzInvalidResponseError,
    LenzMissingKeyError,
    LenzUsageError,
    Result,
    TaskAcceptedResult,
    VerificationResult,
)
from lenz_io.errors import ResponseHeaders
from lenz_io.models import (
    AssessResponse,
    CandidateClaim,
    Citecheck,
    CitecheckSummary,
    FailureBlock,
    ReviewAssessment,
    ReviewFull,
    ReviewSummary,
    ReviewVerification,
    TaskAccepted,
    TaskStatus,
    Verification,
    VerificationListItem,
)

pytestmark = pytest.mark.usefixtures("any_client")

BASE = "https://lenz.io/api/v1"
KEY = "lenz_test_abc123"


@pytest.fixture()
def client() -> Iterator[Any]:
    with make_client(api_key=KEY, max_retries=0) as c:
        yield c


# ── 1 + 2. the API's own sentences ────────────────────────────────────────

#: (call, code, param, the API's 422 ``detail`` for the request the SDK would
#: have sent, in API version 2026-10-11).
_REFUSED: list[tuple[str, Callable[[Any], Any], str, str, str]] = [
    ("verify", lambda c: c.verify(""), "blank_input", "claim", "claim is required."),
    ("verify_ws", lambda c: c.verify("   "), "blank_input", "claim", "claim is required."),
    ("verify_and_wait", lambda c: c.verify_and_wait(" "), "blank_input", "claim", "claim is required."),
    ("assess", lambda c: c.assess(""), "blank_input", "claim", "claim is required."),
    ("assess_text", lambda c: c.assess(text="\n"), "blank_input", "claim", "claim is required."),
    ("assess_empty_list", lambda c: c.assess(claims=[]), "empty_list", "claims", "claims is required."),
    ("assess_blank_item", lambda c: c.assess(claims=["A.", " "]), "blank_item", "claims[1]", "claims[1] is blank."),
    ("select_empty", lambda c: c.select("t1", claims=[]), "empty_list", "claims", "claims is required."),
    ("select_none", lambda c: c.select("t1"), "empty_list", "claims", "claims is required."),
    (
        "select_blank_item",
        lambda c: c.select("t1", claims=["A.", "  "]),
        "blank_item",
        "claims[1]",
        "claims[1] is blank.",
    ),
    ("select_blank_alias", lambda c: c.select("t1", texts=[""]), "blank_item", "claims[0]", "claims[0] is blank."),
    (
        "review",
        lambda c: c.review("  "),
        "blank_input",
        "text",
        "text: send the draft, or one public http(s) URL.",
    ),
    (
        "review_and_wait",
        lambda c: c.review_and_wait(""),
        "blank_input",
        "text",
        "text: send the draft, or one public http(s) URL.",
    ),
    (
        "citecheck",
        lambda c: c.citecheck(" "),
        "blank_input",
        "text",
        "payload: Value error, send exactly one of text and pairs",
    ),
    (
        "citecheck_none",
        lambda c: c.citecheck(None),
        "blank_input",
        "text",
        "payload: Value error, send exactly one of text and pairs",
    ),
    ("ask_send", lambda c: c.ask.send("v1", message=""), "blank_input", "message", "Message cannot be empty."),
    ("ask_send_ws", lambda c: c.ask.send("v1", message=" \t"), "blank_input", "message", "Message cannot be empty."),
    ("ask_send_none", lambda c: c.ask.send("v1", message=None), "blank_input", "message", "Message cannot be empty."),
]


@pytest.mark.parametrize(("name", "call", "code", "param", "sentence"), _REFUSED, ids=[r[0] for r in _REFUSED])
def test_a_blank_input_is_refused_with_the_apis_sentence(
    client: Any, name: str, call: Callable[[Any], Any], code: str, param: str, sentence: str
) -> None:
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        with pytest.raises(LenzUsageError) as ei:
            call(client)
        assert r.calls.call_count == 0
    assert str(ei.value) == sentence
    assert ei.value.code == code
    assert ei.value.param == param


@pytest.mark.parametrize(("name", "call", "code", "param", "sentence"), _REFUSED, ids=[r[0] for r in _REFUSED])
def test_the_sentence_is_the_same_without_legacy_aliases(
    name: str, call: Callable[[Any], Any], code: str, param: str, sentence: str
) -> None:
    with make_client(api_key=KEY, max_retries=0, legacy_aliases=False) as c, pytest.raises(LenzUsageError) as ei:
        call(c)
    assert (str(ei.value), ei.value.code, ei.value.param) == (sentence, code, param)


def test_citecheck_and_wait_refuses_a_blank_text(client: Any) -> None:
    with pytest.raises(LenzUsageError) as ei:
        client.citecheck_and_wait("  ")
    assert str(ei.value) == "payload: Value error, send exactly one of text and pairs"
    assert (ei.value.code, ei.value.param) == ("blank_input", "text")


def test_a_bom_is_content_not_blank(client: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        sel = r.post("/verify/t1/select").respond(200, json={"items": []})
        ask = r.post("/ask/v1").respond(200, json={"role": "expert", "content": "Because."})
        client.select("t1", claims=["\ufeff"])
        client.ask.send("v1", message="\ufeff")
    assert sel.call_count == 1 and ask.call_count == 1


def test_select_claims_that_are_not_a_list_are_sent_as_before(client: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.post("/verify/t1/select").respond(200, json={"items": []})
        client.select("t1", claims="A. ")  # type: ignore[arg-type]
    assert route.calls[0].request.content == b'{"texts":"A. "}'


def test_other_codes_keep_the_sdks_message(client: Any) -> None:
    with pytest.raises(LenzUsageError) as ei:
        client.citecheck("Draft [1].", pairs=[{"statement": "A.", "url": "https://example.com"}])
    assert ei.value.code == "conflicting_input"
    assert str(ei.value) == "citecheck() needs exactly one of text and pairs."


def test_select_with_every_item_filled_is_sent_unchanged(client: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.post("/verify/t1/select").respond(200, json={"items": []})
        client.select("t1", claims=["A.", "B."])
    assert route.calls[0].request.content == b'{"texts":["A.","B."]}'


def test_ask_send_with_a_message_is_sent_unchanged(client: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.post("/ask/v1").respond(200, json={"role": "expert", "content": "Because."})
        client.ask.send("v1", message=" Why? ")
    assert route.calls[0].request.content == b'{"message":" Why? "}'


# ── 3. lazy reads of the wrong type ───────────────────────────────────────

#: (model, body, the property read, the field the message names).
_WRONG: list[tuple[str, type, dict[str, Any], Callable[[Any], Any], str]] = [
    ("candidate.claim", CandidateClaim, {"text": "A.", "claim": 5}, lambda m: m.claim, "claim"),
    ("verification.completed_at", Verification, {"completed_at": 5}, lambda m: m.completed_at, "completed_at"),
    ("list_item.completed_at", VerificationListItem, {"completed_at": []}, lambda m: m.completed_at, "completed_at"),
    ("assess.status", AssessResponse, {"claims": [], "status": 1}, lambda m: m.status, "status"),
    ("assess.failure", AssessResponse, {"claims": [], "failure": "x"}, lambda m: m.failure, "failure"),
    ("assess.failure_list", AssessResponse, {"claims": [], "failure": []}, lambda m: m.failure, "failure"),
    (
        "row.status",
        AssessResponse,
        {"claims": [{"claim": "A.", "status": True}]},
        lambda m: m.claims[0].status,
        "status",
    ),
    (
        "row.failure",
        AssessResponse,
        {"claims": [{"claim": "A.", "status": "failed", "failure": "x"}]},
        lambda m: m.claims[0].failure,
        "failure",
    ),
    (
        "row.more_claims",
        AssessResponse,
        {"claims": [{"claim": "A.", "more_claims": "B."}]},
        lambda m: m.claims[0].more_claims,
        "more_claims",
    ),
    (
        "row.more_claims_item",
        AssessResponse,
        {"claims": [{"claim": "A.", "more_claims": ["B.", 2]}]},
        lambda m: m.claims[0].more_claims,
        "more_claims[1]",
    ),
    ("accepted.claim", TaskAccepted, {"task_id": "t1", "claim": 1}, lambda m: m.claim, "claim"),
    ("accepted.chain_id", TaskAccepted, {"task_id": "t1", "chain_id": 1}, lambda m: m.chain_id, "chain_id"),
    ("status.failure", TaskStatus, {"status": "failed", "failure": "x"}, lambda m: m.failure, "failure"),
    ("cancelled.failure", TaskStatus, {"status": "cancelled", "failure": 0}, lambda m: m.failure, "failure"),
    ("failure.code", FailureBlock, {"code": 5}, lambda m: m.code, "code"),
    ("failure.detail", FailureBlock, {"detail": {}}, lambda m: m.detail, "detail"),
    ("summary.claims_found", ReviewSummary, {"claims_found": "3"}, lambda m: m.claims_found, "claims_found"),
    ("summary.claims_found_bool", ReviewSummary, {"claims_found": True}, lambda m: m.claims_found, "claims_found"),
    (
        "summary.claim_limit_exceeded",
        ReviewSummary,
        {"claim_limit_exceeded": 1},
        lambda m: m.claim_limit_exceeded,
        "claim_limit_exceeded",
    ),
    (
        "summary.citation_limit_exceeded",
        ReviewSummary,
        {"citation_limit_exceeded": "no"},
        lambda m: m.citation_limit_exceeded,
        "citation_limit_exceeded",
    ),
    ("review_row.more_claims", ReviewAssessment, {"more_claims": 3}, lambda m: m.more_claims, "more_claims"),
    (
        "review_verification.completed_at",
        ReviewVerification,
        {"completed_at": 1},
        lambda m: m.completed_at,
        "completed_at",
    ),
    (
        "citecheck_summary.citation_limit_exceeded",
        CitecheckSummary,
        {"citation_limit_exceeded": 0},
        lambda m: m.citation_limit_exceeded,
        "citation_limit_exceeded",
    ),
]


#: Cases whose 2.x alias is filled in from the value on the read itself.
_READ_FAILS_WITH_ALIASES = {"accepted.claim", "failure.code", "review_row.more_claims"}


@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize(("name", "cls", "body", "read", "field"), _WRONG, ids=[w[0] for w in _WRONG])
def test_a_lazy_read_of_the_wrong_type_raises(
    name: str, cls: type, body: dict[str, Any], read: Callable[[Any], Any], field: str, legacy: bool
) -> None:
    if legacy and name in _READ_FAILS_WITH_ALIASES:
        # Filling in the 2.x alias reads the value on the read itself (a
        # client raises ``LenzInvalidResponseError`` there).
        pytest.skip("read on the read itself with legacy_aliases=True")
    model = cls.model_validate(body, context={"legacy_aliases": legacy})
    with pytest.raises(LenzInvalidResponseError) as ei:
        read(model)
    assert f"({field}:" in ei.value.message


def test_extracted_claims_of_the_wrong_type_raise(client: Any) -> None:
    for claims, field in (("x", "claims"), ([{"claim": "A."}, 42], "claims[1]")):
        with respx.mock(base_url=BASE) as r:
            r.post("/extract").respond(200, json={"claims": claims, "status": "ok"}, headers={"X-Request-ID": "req_e"})
            out = client.extract(text="Text.")
        with pytest.raises(LenzInvalidResponseError) as ei:
            out.claims  # noqa: B018
        assert f"({field}:" in ei.value.message
        # The answer's status and headers, as on the read itself.
        assert ei.value.status_code == 200
        assert ei.value.request_id == "req_e"


def test_a_wrongly_typed_failure_on_a_call_raises_with_the_answer(client: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        r.get("/verify/status/t1").respond(200, json={"status": "failed", "failure": "boom"})
        out = client.get_status("t1")
    with pytest.raises(LenzInvalidResponseError) as ei:
        out.failure  # noqa: B018
    assert ei.value.status_code == 200
    assert ei.value.body == {"status": "failed", "failure": "boom"}


@pytest.mark.parametrize("legacy", [True, False])
def test_null_and_absent_still_read_as_not_sent(legacy: bool) -> None:
    ctx = {"legacy_aliases": legacy}
    assess = AssessResponse.model_validate(
        {"claims": [{"claim": "A.", "status": None, "failure": None, "more_claims": None}], "failure": None},
        context=ctx,
    )
    assert assess.failure is None
    assert assess.claims[0].failure is None
    assert assess.claims[0].more_claims == []
    assert assess.claims[0].status == ""
    assert Verification.model_validate({"completed_at": None}, context=ctx).completed_at is None
    assert ReviewSummary.model_validate({"claims_found": None}, context=ctx).claims_found is None
    assert FailureBlock.model_validate({}, context=ctx).detail is None


# ── 4. Result ─────────────────────────────────────────────────────────────

_ACCEPTED = {"task_id": "t1", "claim": "A."}
_DONE = {"status": "completed", "task_id": "t1", "result": {"verification_id": "v1", "claim": "A."}}


def _results(client: Any, r: respx.Router) -> list[Any]:
    """One result of every call that returns a top-level result."""
    r.post("/verify").respond(202, json=_ACCEPTED, headers={"Location": "/api/v1/verify/status/t1"})
    r.get("/verify/status/t1").respond(200, json=_DONE)
    r.get("/verifications/v1").respond(200, json={"verification_id": "v1", "claim": "A."})
    r.post("/assess").respond(200, json={"claims": []})
    r.post("/extract").respond(200, json={"claims": [], "status": "ok"})
    return [
        client.verify("A."),
        client.get_status("t1"),
        client.verifications.get("v1"),
        client.assess("A."),
        client.extract(text="Text."),
    ]


def test_every_top_level_result_is_a_result(client: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        verify, status, verification, assess, extract = _results(client, r)
    for out in (verify, status, verification, assess, extract):
        assert isinstance(out, Result)
        assert type(out.http_status) is int
        assert isinstance(out.headers, ResponseHeaders)
    assert isinstance(verify, TaskAcceptedResult) and isinstance(verify, TaskAccepted)
    assert verify.http_status == 202
    assert verify.headers["location"] == "/api/v1/verify/status/t1"
    assert isinstance(verification, VerificationResult) and isinstance(verification, Verification)
    assert verification.http_status == 200
    # A nested model is not a Result and keeps None.
    assert status.result is not None and not isinstance(status.result, Result)
    assert status.result.http_status is None and status.result.headers is None


def test_a_result_survives_pickling(client: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        verify = _results(client, r)[0]
    again = pickle.loads(pickle.dumps(verify))
    assert isinstance(again, TaskAcceptedResult)
    assert again.http_status == 202
    assert again.model_dump() == verify.model_dump()


def test_a_result_not_read_from_an_answer_has_zero_and_no_headers() -> None:
    for model in (
        AssessResponse.model_validate({"claims": []}),
        ReviewFull.model_validate({"review_id": "r1"}),
        Citecheck.model_validate({"citecheck_id": "c1"}),
        VerificationResult.model_validate({"verification_id": "v1"}),
    ):
        assert model.http_status == 0
        assert model.headers == ResponseHeaders()
        assert len(model.headers) == 0


def test_verify_and_wait_returns_a_nested_verification(client: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        r.post("/verify").respond(202, json=_ACCEPTED)
        r.get("/verify/status/t1").respond(200, json=_DONE)
        out = client.verify_and_wait("A.", timeout=5)
    assert type(out) is Verification
    assert out.http_status is None


# ── 5. LenzMissingKeyError ────────────────────────────────────────────────


def test_a_call_with_no_key_raises_missing_key_before_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LENZ_API_KEY", raising=False)
    with make_client(api_key=None) as keyless, respx.mock(base_url=BASE, assert_all_called=False) as r:
        with pytest.raises(LenzMissingKeyError) as ei:
            keyless.usage()
        assert r.calls.call_count == 0
    err = ei.value
    assert isinstance(err, LenzAuthError)
    assert not isinstance(err, LenzInvalidKeyError)
    assert err.status_code == 0
    assert err.retryable is None
    assert err.message == "API key required"
    assert type(pickle.loads(pickle.dumps(err))) is LenzMissingKeyError


def test_a_copy_with_an_empty_key_raises_missing_key(client: Any) -> None:
    with pytest.raises(LenzMissingKeyError):
        client.with_options(api_key="  ").usage()


def test_an_unsendable_key_and_a_refused_one_are_not_missing_key(client: Any) -> None:
    with pytest.raises(LenzInvalidKeyError) as invalid:
        make_client(api_key="lenz_bad key")
    assert not isinstance(invalid.value, LenzMissingKeyError)
    with respx.mock(base_url=BASE) as r:
        r.get("/me/usage").respond(401, json={"detail": "Invalid API key.", "code": "not_authenticated"})
        with pytest.raises(LenzAuthError) as refused:
            client.usage()
    assert type(refused.value) is LenzAuthError


# ── 6. a completed poll with no result ────────────────────────────────────


@pytest.mark.parametrize("result", ["absent", None])
def test_wait_raises_unreadable_for_a_completed_poll_with_no_result(client: Any, result: Any) -> None:
    body: dict[str, Any] = {"status": "completed", "task_id": "t1"}
    if result != "absent":
        body["result"] = result
    with respx.mock(base_url=BASE) as r:
        route = r.get("/verify/status/t1").respond(200, json=body, headers={"X-Request-ID": "req_w"})
        with pytest.raises(LenzInvalidResponseError) as ei:
            client.wait("t1", timeout=30)
    # Ended: one poll, no retry until the deadline.
    assert route.call_count == 1
    err = ei.value
    assert err.status_code == 200
    assert err.request_id == "req_w"
    assert err.body == body
    assert err.message == (
        "The API answered HTTP 200 with status completed and no result: "
        "the run ended, but its verification cannot be read."
    )


def test_a_batch_row_completed_with_no_result_fails_with_the_error(client: Any) -> None:
    batch = {"batch_id": "b1", "items": [{"task_id": "t1", "claim": "A."}, {"task_id": "t2", "claim": "B."}]}
    with respx.mock(base_url=BASE) as r:
        r.post("/verify/batch").respond(202, json=batch)
        r.get("/verify/status/t1").respond(200, json={"status": "completed", "task_id": "t1"})
        r.get("/verify/status/t2").respond(
            200, json={"status": "completed", "task_id": "t2", "result": {"verification_id": "v2", "claim": "B."}}
        )
        rows = client.verify_batch_and_wait(claims=[{"claim": "A."}, {"claim": "B."}], timeout=30)
    first, second = rows
    assert first.status == "failed"
    assert first.status_detail is not None and first.status_detail.status == "completed"
    assert isinstance(first.error, LenzInvalidResponseError)
    assert first.error.status_code == 200
    assert second.status == "completed" and second.error is None
    # A plain failed row carries no error.
    with respx.mock(base_url=BASE) as r:
        r.post("/verify/batch").respond(202, json={"batch_id": "b2", "items": [{"task_id": "t3", "claim": "C."}]})
        r.get("/verify/status/t3").respond(200, json={"status": "failed", "task_id": "t3"})
        (failed,) = client.verify_batch_and_wait(claims=[{"claim": "C."}], timeout=30)
    assert failed.status == "failed" and failed.error is None
