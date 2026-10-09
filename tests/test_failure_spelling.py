"""How "nothing checkable" is spelled in a failure block.

2.x spelled it ``not_a_claim`` for a verification (``TaskStatus.failure_reason``,
a ``verification.failed`` webhook's ``error``) and ``no_claim`` for an
assessment or a review. ``failure.failure_reason`` keeps each spelling, and
``failure.code`` is the same ``no_checkable_claim`` everywhere.
"""

from __future__ import annotations

import warnings

import pytest
from parity_observe import load

from lenz_io.models import AssessResponse, ReviewFull, TaskStatus
from lenz_io.webhooks import VerificationFailed, parse_webhook


def test_a_verifications_failure_block_spells_it_not_a_claim() -> None:
    status = TaskStatus.model_validate(load("canonical", "verify__status_not_a_claim.json")["body"])
    assert status.failure_reason == "not_a_claim"
    assert status.failure is not None
    assert status.failure.failure_reason == "not_a_claim"
    assert status.failure.code == "no_checkable_claim"
    assert TaskStatus.model_validate(load("legacy", "verify__status_not_a_claim.json")["body"]).failure_reason == (
        "not_a_claim"
    )


@pytest.mark.parametrize("shape", ["legacy", "canonical"])
def test_a_verification_failed_event_spells_it_not_a_claim(shape: str) -> None:
    event = parse_webhook(load(shape, "webhook__verification_failed_not_a_claim.json")["body"])
    assert isinstance(event, VerificationFailed)
    assert event.error == "not_a_claim"
    assert event.failure is not None
    assert event.failure.failure_reason == "not_a_claim"


def test_another_failure_code_is_untouched() -> None:
    body = load("canonical", "webhook__verification_failed_upstream_unavailable.json")["body"]
    event = parse_webhook(body)
    assert isinstance(event, VerificationFailed)
    assert event.failure is not None
    assert event.failure.failure_reason == event.failure.code == body["verification"]["failure"]["code"]


def test_an_assessment_and_a_review_keep_no_claim() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assessed = AssessResponse.model_validate(load("canonical", "assess__single_no_claim.json")["body"])
        assert assessed.failure is not None
        assert assessed.failure.failure_reason == "no_claim"
        assert assessed.error_code == "no_claim"
        review = ReviewFull.model_validate(load("canonical", "review__get_failed_no_claim.json")["body"])
        assert review.failure is not None
        assert review.failure.failure_reason == "no_claim"
