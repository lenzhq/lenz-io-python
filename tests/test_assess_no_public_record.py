"""A quick check that finds "No Public Record": an /assess row and a review's
quick check carry it with a confidence band (never "high") and no score, a
final one is a review issue, and an unresolved panel is a free failed row.

The bodies are the API's own (`tests/fixtures/contract/` and the canonical
goldens), read through the SDK's real models in both `legacy_aliases` modes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lenz_io import AssessResponse, ReviewFull, ReviewIssues
from lenz_io.models import LEGACY_ALIASES

TESTS = Path(__file__).parent / "fixtures"
CONTEXTS = [None, {LEGACY_ALIASES: False}]
IDS = ["aliases", "as_sent"]


def _contract(name: str) -> dict:
    return json.loads((TESTS / "contract" / name).read_text())


def _golden(name: str) -> dict:
    return json.loads((TESTS / "goldens" / "canonical" / name).read_text())["body"]


@pytest.mark.parametrize("context", CONTEXTS, ids=IDS)
def test_an_assess_row_can_say_no_public_record(context) -> None:
    resp = AssessResponse.model_validate(_contract("assess_no_public_record.json"), context=context)
    row = resp.claims[0]
    assert (row.status, row.verdict, row.confidence) == ("completed", "No Public Record", "medium")
    assert row.failure is None
    assert row.suggested_rewrite is None


def test_the_golden_rows_read_the_same() -> None:
    for name in ("assess__single_no_public_record.json", "assess__list_no_public_record.json"):
        resp = AssessResponse.model_validate(_golden(name))
        assert any(c.verdict == "No Public Record" for c in resp.claims), name
        assert all(c.confidence != "high" for c in resp.claims if c.verdict == "No Public Record")


@pytest.mark.parametrize("context", CONTEXTS, ids=IDS)
def test_an_unresolved_row_is_a_free_retryable_failure(context) -> None:
    row = AssessResponse.model_validate(_contract("assess_no_public_record.json"), context=context).claims[4]
    assert row.status == "failed"
    assert row.failure is not None
    assert row.failure.code == "upstream_unavailable"
    assert row.failure.detail == "The reviewers did not reach an answer."
    assert (row.failure.failure_class, row.failure.retryable) == ("upstream_unavailable", True)
    # It is not a No Public Record row, in either reading.
    assert row.verdict in ("Error", None)


def test_the_failed_rows_share_one_vocabulary() -> None:
    rows = AssessResponse.model_validate(_contract("assess_no_public_record.json")).claims[2:]
    assert [(r.failure.code, r.failure.retryable) for r in rows if r.failure] == [
        ("upstream_unavailable", True),
        ("internal", False),
        ("upstream_unavailable", True),
    ]
    # Resend on the failure's own `retryable`, never on a list of codes.
    assert [r.claim for r in rows if r.failure and r.failure.retryable] == [
        "The Eiffel Tower is 330 metres tall.",
        "The Halberd Ferry Company ran 61 sailings in its first winter.",
    ]


@pytest.mark.parametrize("context", CONTEXTS, ids=IDS)
def test_a_quick_no_public_record_is_a_review_issue(context) -> None:
    review = ReviewFull.model_validate(_contract("review_completed_quick_no_public_record.json"), context=context)
    assert review.outcome == "issues_found"
    first, second = review.claims
    assert first.assessment.verdict == "No Public Record"
    assert first.result is not None and first.result.is_issue is True
    assert second.result is not None and second.result.is_issue is False
    (issue,) = review.issues
    assert (issue.verdict, issue.source, issue.suggested_rewrite) == ("No Public Record", "assessment", None)


def test_the_default_escalation_list_includes_it() -> None:
    review = ReviewFull.model_validate(_contract("review_completed_quick_no_public_record.json"))
    assert "No Public Record" in review.policy.verdicts


def test_the_issues_view_reads_it_too() -> None:
    body = _golden("review__get_completed_no_public_record_issues_view.json")
    view = ReviewIssues.model_validate(body)
    assert view.outcome == "issues_found"
    assert any(i.verdict == "No Public Record" for i in view.issues)


@pytest.mark.parametrize("context", CONTEXTS, ids=IDS)
def test_an_unreadable_link_fails_the_review_as_page_unreadable(context) -> None:
    review = ReviewFull.model_validate(_contract("review_failed_page_unreadable.json"), context=context)
    assert review.status == "failed"
    assert review.failure is not None
    assert review.failure.code == "page_unreadable"
    assert (review.failure.failure_class, review.failure.retryable) == ("invalid_input", False)
    assert review.failure.detail == "The page at the link could not be read."
