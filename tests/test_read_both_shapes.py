"""The current response shape gives the new attributes, and the 2.x names keep
their 2.x meaning. ``test_parity.py`` holds the 2.x attributes to the previous
release's output; this file checks the new ones. The SDK's own calls read only
the current shape; webhook payloads of both shapes are still parsed (the
webhook tests below), and the models they share with the polled bodies read
both."""

from __future__ import annotations

import json
import warnings

import pytest
import respx
from parity_observe import load

from lenz_io import Lenz
from lenz_io.client import DEFAULT_BASE_URL
from lenz_io.errors import LenzPipelineError, LenzRateLimitError, LenzValidationError, map_response_to_error
from lenz_io.models import (
    AssessClaim,
    AssessResponse,
    BatchAccepted,
    ExtractedClaims,
    FailureBlock,
    ReviewFull,
    TaskAccepted,
    TaskStatus,
    Usage,
    Verification,
    VerificationList,
)
from lenz_io.webhooks import (
    CitecheckEvent,
    ReviewEvent,
    VerificationCompleted,
    VerificationFailed,
    VerificationNeedsInput,
    parse_webhook,
)

pytestmark = pytest.mark.filterwarnings("error::DeprecationWarning")


def _both(name: str) -> tuple[dict, dict]:
    """The recorded body in the 2.x shape and in the current shape."""
    return load("legacy", name)["body"], load("canonical", name)["body"]


def _canonical(name: str) -> dict:
    return load("canonical", name)["body"]


# ── /extract ──


@pytest.mark.parametrize(
    "name",
    [
        "extract__ready_one_claim.json",
        "extract__ready_several_claims.json",
        "extract__locate_true.json",
        "extract__locate_some_dropped.json",
        "extract__url_input_located.json",
        "extract__no_match_with_focus.json",
        "extract__not_a_claim.json",
    ],
)
def test_extract_claims_and_the_2x_names(name):
    legacy_body, body = _both(name)
    out = ExtractedClaims.model_validate(body)
    names = [c["claim"] for c in body["claims"]]
    assert [c.claim for c in out.claims] == names
    # the 2.x names: ``claim`` is the first, ``identified_claims`` the list when several
    assert out.claim == legacy_body["claim"]
    assert out.identified_claims == legacy_body["identified_claims"]
    assert out.status == legacy_body["status"]  # "not_a_claim" in the 2.x spelling


def test_extract_one_claim_is_a_list_of_one():
    out = ExtractedClaims.model_validate(_canonical("extract__ready_one_claim.json"))
    assert [c.claim for c in out.claims] == ["Alpha rose 5% in 2024."]
    assert out.identified_claims == [] and out.claim == "Alpha rose 5% in 2024."


def test_extract_empty_claims_does_not_fall_back_to_old_fields():
    out = ExtractedClaims.model_validate({"status": "no_match", "claims": [], "claim": "stale"})
    assert out.claims == [] and out.claim == "stale"


# ── /assess ──


def test_assess_failed_rows():
    legacy_body, body = _both("assess__list_mixed_rows.json")
    resp = AssessResponse.model_validate(body)
    assert resp.status == "ok"
    assert [r.status for r in resp.claims] == ["completed", "failed", "failed", "failed"]
    assert [r.failure.code if r.failure else None for r in resp.claims] == [
        None,
        "no_checkable_claim",
        "upstream_unavailable",
        "framing_failed",
    ]
    assert resp.claims[1].failure.hint.startswith("The input is a greeting.")
    # the 2.x names: no verdict on a failed row reads as "Error" / "low"
    row = resp.claims[1]
    assert (row.verdict, row.confidence, row.error_code) == ("Error", "low", "no_claim")
    assert [r.error_code for r in resp.claims] == [r.get("error_code") for r in legacy_body["claims"]]


def test_assess_single_no_claim():
    resp = AssessResponse.model_validate(_canonical("assess__single_no_claim.json"))
    assert resp.status == "no_checkable_claim"
    assert resp.failure is not None and resp.failure.code == "no_checkable_claim"
    assert resp.error_code == "no_claim" and resp.error


def test_assess_more_claims_on_a_row():
    resp = AssessResponse.model_validate(_canonical("assess__list_compound_item.json"))
    assert resp.claims[0].more_claims == resp.claims[0].identified_claims == ["Second claim.", "Third claim."]


def test_assess_empty_more_claims_does_not_fall_back():
    row = AssessClaim.model_validate({"claim": "x", "status": "completed", "verdict": "True", "more_claims": []})
    assert row.more_claims == [] and row.identified_claims == []


# ── /verify ──


def test_receipt_claim():
    out = BatchAccepted.model_validate(_canonical("verify__batch_202.json"))
    assert [i.claim for i in out.items] == [i.claim_text for i in out.items] != []
    assert TaskAccepted.model_validate({"task_id": "t"}).claim == ""


@pytest.mark.parametrize(
    "name",
    [
        "verify__status_failed.json",
        "verify__status_failed_retryable.json",
        "verify__status_not_a_claim.json",
        "verify__status_cancelled_after_a_while.json",
    ],
)
def test_failed_status_failure_block(name):
    legacy_body, body = _both(name)
    canonical = TaskStatus.model_validate(body)
    assert canonical.failure is not None
    assert canonical.failure.code == body["failure"]["code"]
    assert canonical.failure.failure_class == canonical.failure_class
    assert canonical.failure.retryable is canonical.retryable
    assert canonical.failure.detail
    assert canonical.failure_reason == legacy_body["failure_reason"]
    # ``error`` keeps the 2.x sentence, rebuilt from the code (a failure
    # read back from storage said "Pipeline stopped: <code>." instead).
    if not legacy_body["error"].startswith("Pipeline stopped: "):
        assert canonical.error == legacy_body["error"]


def test_needs_input_options():
    legacy_body, body = _both("verify__status_needs_input.json")
    canonical = TaskStatus.model_validate(body)
    assert [c.text for c in canonical.claims] == [c["text"] for c in legacy_body["claims"]]
    assert [c.text for c in canonical.claims] == [c.claim for c in canonical.claims]


def test_completed_at_and_the_original_modified_at_rule():
    late, early = "2026-10-01T23:55:00+00:00", "2026-10-02T00:03:00+00:00"
    crossed = Verification.model_validate({"created_at": late, "completed_at": early})
    assert crossed.modified_at == early
    same_day = Verification.model_validate({"created_at": "2026-10-01T08:00:00Z", "completed_at": "2026-10-01T20:00Z"})
    assert same_day.modified_at is None and same_day.completed_at == "2026-10-01T20:00Z"
    # a UTC calendar day, whatever offset the strings carry
    offset = Verification.model_validate(
        {"created_at": "2026-10-01T22:00:00+00:00", "completed_at": "2026-10-02T01:00:00+02:00"}
    )
    assert offset.modified_at is None
    assert Verification.model_validate({"created_at": late}).completed_at is None


@pytest.mark.parametrize(
    "name",
    [
        "verify__verification_200_modified_at_crosses_midnight_by_minutes.json",
        "verify__verification_200_modified_at_same_day_hours_apart.json",
        "verify__list_200_modified_at_crosses_midnight_by_minutes.json",
    ],
)
def test_modified_at_follows_the_2x_rule(name):
    legacy_body, body = _both(name)
    if "list" in name:
        got, expected = VerificationList.model_validate(body).items[0], legacy_body["items"][0]
    else:
        got, expected = Verification.model_validate(body), legacy_body
    assert got.modified_at == expected["modified_at"]


# ── /me/usage ──


@pytest.mark.parametrize(
    "name",
    ["account__me_usage_pro_extra.json", "account__me_usage_free_partly_spent.json", "account__me_usage_free.json"],
)
def test_usage_blocks_are_computed_from_the_pool(name):
    legacy_body, body = _both(name)
    canonical = Usage.model_validate(body)
    for cap in ("verify", "ask", "assess"):
        dumped = getattr(canonical, cap).model_dump()
        assert {k: dumped[k] for k in legacy_body[cap] if k != "credits"} == {
            k: v for k, v in legacy_body[cap].items() if k != "credits"
        }
    assert canonical.quota_resets_at == canonical.credits.resets_at == legacy_body["quota_resets_at"]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert canonical.credits.bonus == legacy_body["credits"]["bonus"]


# ── /review ──


@pytest.mark.parametrize(
    "name",
    [
        "review__get_more_claims.json",
        "review__get_claim_limit_reached_exact.json",
        "review__get_citations_limit_reached.json",
        "review__get_completed_issues_full.json",
        "review__get_assessment_rows_full_fields.json",
    ],
)
def test_review_new_names_from_both_shapes(name):
    legacy, canonical = (ReviewFull.model_validate(b) for b in _both(name))
    # The original shape says only that the claim limit was REACHED.
    assert legacy.summary.claim_limit_exceeded is None
    assert canonical.summary.claim_limit_exceeded == bool(canonical.more_claims)
    assert legacy.summary.citation_limit_exceeded == canonical.summary.citation_limit_exceeded
    for a, b in zip(legacy.claims, canonical.claims, strict=True):
        assert a.assessment.more_claims == b.assessment.more_claims
        if a.assessment.failure is not None:
            assert a.assessment.failure.code == b.assessment.failure.code


def test_review_failure_code_reads_the_new_spelling():
    legacy, canonical = (ReviewFull.model_validate(b) for b in _both("review__get_failed_no_claim.json"))
    assert legacy.failure.code == canonical.failure.code == "no_checkable_claim"
    assert canonical.failure.failure_reason == "no_claim"
    assert FailureBlock.model_validate({"failure_reason": "not_a_claim"}).code == "no_checkable_claim"


# ── the models keep their 2.x layout ──


def test_newer_shape_dump_keeps_every_key_sent():
    canonical = load("canonical", "assess__list_mixed_rows.json")["body"]
    dumped = AssessResponse.model_validate(canonical).model_dump(mode="json")
    assert set(canonical) <= set(dumped)
    assert all(set(row) <= set(d) for row, d in zip(canonical["claims"], dumped["claims"], strict=True))


def test_explicit_null_retryable_is_kept():
    st = TaskStatus.model_validate({"status": "failed", "retryable": None, "failure": {"code": "x", "retryable": True}})
    assert st.retryable is None
    assert st.failure is not None and st.failure.retryable is True


def test_sparse_usage_dumps_every_default():
    assert set(Usage.model_validate({"plan": "free"}).model_dump()) == set(Usage.model_fields)


def test_new_names_are_properties_not_fields():
    for cls, name in (
        (AssessClaim, "status"),
        (AssessClaim, "failure"),
        (ExtractedClaims, "claims"),
        (TaskStatus, "failure"),
        (Verification, "completed_at"),
        (FailureBlock, "code"),
    ):
        assert name not in cls.model_fields


def test_model_copy_update_shows_in_the_dump():
    row = AssessClaim.model_validate({"claim": "x", "verdict": "True"})
    assert row.model_copy(update={"verdict": "False"}).model_dump()["verdict"] == "False"


# ── webhooks ──


@pytest.mark.parametrize(
    "name",
    [
        "webhook__verification_failed_not_a_claim.json",
        "webhook__verification_failed_upstream_unavailable.json",
    ],
)
def test_failed_webhook_from_both_shapes(name):
    legacy, canonical = (parse_webhook(b) for b in _both(name))
    assert isinstance(legacy, VerificationFailed) and isinstance(canonical, VerificationFailed)
    assert legacy.error == canonical.error
    assert legacy.failure.code == canonical.failure.code
    assert (legacy.failure_class, legacy.retryable) == (canonical.failure_class, canonical.retryable)
    assert canonical.event_id


def test_needs_input_webhook_from_both_shapes():
    legacy, canonical = (parse_webhook(b) for b in _both("webhook__verification_needs_input_multi_claim.json"))
    assert isinstance(canonical, VerificationNeedsInput)
    assert legacy.needs_input == canonical.needs_input
    assert [c.claim for c in legacy.claims] == [c.claim for c in canonical.claims]
    assert legacy.reason == canonical.reason == "multi_claim"


def test_completed_webhook_result_from_both_shapes():
    name = "webhook__verification_completed_modified_at_crosses_midnight_by_minutes.json"
    legacy, canonical = (parse_webhook(b) for b in _both(name))
    assert isinstance(canonical, VerificationCompleted)
    assert legacy.verification_id == canonical.verification_id
    assert legacy.result["modified_at"] == canonical.result["modified_at"]


def test_needs_input_with_a_null_domain_does_not_raise():
    event = parse_webhook(
        {
            "event": "verification.needs_input",
            "task_id": "t",
            "needs_input": {"claims": [{"text": "A", "domain": None}]},
        }
    )
    assert event.needs_input == {"claims": [{"text": "A", "domain": None}]}
    assert [c.claim for c in event.claims] == ["A"]


def test_legacy_webhook_event_still_serialises():
    import dataclasses

    event = parse_webhook(load("legacy", "webhook__verification_failed_not_a_claim.json")["body"])
    assert json.loads(json.dumps(dataclasses.asdict(event)))["error"] == "not_a_claim"
    assert "failure" not in dataclasses.asdict(event)


@pytest.mark.parametrize("name", ["webhook__review_completed.json", "webhook__citecheck_completed.json"])
def test_review_webhook_without_task_id(name):
    canonical = parse_webhook(load("canonical", name)["body"])
    assert isinstance(canonical, (ReviewEvent, CitecheckEvent))
    work_id = canonical.review_id if isinstance(canonical, ReviewEvent) else canonical.citecheck_id
    assert canonical.task_id == work_id != "" and canonical.event_id != ""
    legacy = parse_webhook(load("legacy", name)["body"])
    assert legacy.task_id == load("legacy", name)["body"]["task_id"]


# ── errors ──


def test_409_failed_run_reads_the_failure_block():
    _, canonical = _both("verify__verification_failed_409.json")
    err = map_response_to_error(409, json.dumps(canonical).encode())
    assert isinstance(err, LenzPipelineError)
    assert (err.failure_reason, err.failure_class, err.retryable) == (
        "research_empty",
        "insufficient_evidence",
        False,
    )
    _, canonical = _both("verify__verification_failed_409_not_a_claim_after_a_while.json")
    assert map_response_to_error(409, json.dumps(canonical).encode()).failure_reason == "not_a_claim"


def test_validation_errors():
    err = map_response_to_error(422, json.dumps(_canonical("errors__validation_wrong_type.json")).encode())
    assert isinstance(err, LenzValidationError)
    assert [e["msg"] for e in err.errors] == ["Input should be a valid string"]


def test_rate_limit_wait():
    err = map_response_to_error(429, json.dumps(_canonical("errors__rate_limited_extract.json")).encode())
    assert isinstance(err, LenzRateLimitError)
    assert (err.retry_after, err.reset_in_seconds) == (900, 900)
    err = map_response_to_error(429, json.dumps({"code": "review_in_flight", "retry_after": 0}).encode())
    assert (err.retry_after, err.reset_in_seconds) == (0, None)


def test_error_fields_follow_key_presence():
    err = map_response_to_error(
        429, json.dumps({"code": "rate_limited", "reset_in_seconds": None, "retry_after": 30}).encode()
    )
    assert err.reset_in_seconds is None
    body = {"code": "verification_failed", "hint": "", "failure": {"code": "x", "hint": "from the block"}}
    assert map_response_to_error(409, json.dumps(body).encode()).hint == ""


def test_quota_remaining_is_derived_when_absent():
    canonical = _canonical("citecheck__402_no_credits.json")
    assert map_response_to_error(402, json.dumps(canonical).encode()).remaining == 100


# ── requests ──


def _sent(call) -> dict:
    with respx.mock(base_url=DEFAULT_BASE_URL, assert_all_called=False) as mock:
        verify = mock.post("/verify").respond(202, json={"task_id": "t"})
        batch = mock.post("/verify/batch").respond(202, json={"batch_id": "b", "items": []})
        client = Lenz(api_key="lenz_" + "0" * 32)
        call(client)
        route = verify if verify.called else batch
        return json.loads(route.calls.last.request.content)


@pytest.mark.parametrize("webhook_url", [None, ""])
def test_verify_never_sends_an_empty_webhook_url(webhook_url):
    kwargs = {} if webhook_url is None else {"webhook_url": webhook_url}
    assert "webhook_url" not in _sent(lambda c: c._verify_submit(claim="x", **kwargs))
    assert "webhook_url" not in _sent(lambda c: c.verify_batch(claims=[{"claim": "x"}], **kwargs))
    item = _sent(lambda c: c.verify_batch(claims=[{"claim": "x", "webhook_url": ""}]))["claims"][0]
    assert "webhook_url" not in item


def test_verify_sends_a_webhook_url_that_was_set():
    body = _sent(lambda c: c.verify("x", webhook_url="https://hooks.example.test/x"))
    assert body["webhook_url"] == "https://hooks.example.test/x"
