"""A completed "No Public Record" check: no score, a confidence band, a
verdict label of its own. Not a failure, and not an ``Error``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lenz_io import TaskStatus, Verification, VerificationList, VerificationListItem
from lenz_io.cli.render import _VERDICT_COLOR, _batch_status_cell
from lenz_io.models import LEGACY_ALIASES

FIXTURES = Path(__file__).parent / "fixtures" / "contract"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.parametrize("context", [None, {LEGACY_ALIASES: False}], ids=["aliases", "as_sent"])
def test_a_no_public_record_verification_reads(context) -> None:
    v = Verification.model_validate(_load("verifications_detail_no_public_record.json"), context=context)
    assert v.verdict == "No Public Record"
    assert v.lenz_score is None
    assert v.confidence == "low"
    assert v.is_no_public_record is True
    assert v.has_score is False
    assert not (v.model_extra or {})


def test_a_scored_verification_has_a_score_and_is_not_no_public_record() -> None:
    v = Verification(verdict="True", confidence="high", lenz_score=9)
    assert (v.has_score, v.is_no_public_record) == (True, False)
    assert Verification().has_score is False
    assert Verification().is_no_public_record is False


def test_the_helpers_are_on_list_items_too() -> None:
    listed = VerificationList.model_validate(_load("verifications_list_no_public_record.json"))
    (item,) = listed.items
    assert (item.has_score, item.is_no_public_record) == (False, True)
    scored = VerificationListItem(verdict="False", lenz_score=2)
    assert (scored.has_score, scored.is_no_public_record) == (True, False)


def test_a_completed_status_carries_it_in_the_result() -> None:
    st = TaskStatus.model_validate(_load("verify_status_completed_no_public_record.json"))
    assert st.status == "completed"
    assert st.result is not None and st.result.is_no_public_record


def test_a_collapsed_code_failure_reads() -> None:
    st = TaskStatus.model_validate(_load("verify_status_failed_collapsed_code.json"))
    assert st.failure is not None
    assert (st.failure.code, st.failure.failure_class, st.failure.retryable) == ("internal", "internal", False)


def test_the_label_has_a_neutral_colour() -> None:
    assert _VERDICT_COLOR["No Public Record"] == "grey62"
    assert _VERDICT_COLOR["No Public Record"] not in {
        _VERDICT_COLOR[k] for k in ("True", "Mostly True", "Mixed", "Mostly False", "False")
    }


def test_the_batch_cell_shows_no_score() -> None:
    v = Verification.model_validate(_load("verifications_detail_no_public_record.json"))
    cell = _batch_status_cell(TaskStatus(status="completed", result=v))
    assert cell.plain == "No Public Record (low)"
    assert cell.style == "bold grey62"
    assert "/10" not in cell.plain


def test_the_cli_prints_the_verdict_without_a_score(monkeypatch) -> None:
    from lenz_io.cli import render

    printed: list[str] = []

    class _Console:
        def print(self, *args, **kwargs) -> None:
            printed.append(" ".join(str(a) for a in args))

    class _Out:
        json_mode = False
        console = _Console()

    v = Verification.model_validate(_load("verifications_detail_no_public_record.json"))
    render._verdict_header(_Out(), v)  # type: ignore[arg-type]
    (line,) = printed
    assert "No Public Record" in line and "(low)" in line
    assert "/10" not in line and "score" not in line
    assert "grey62" in line


def test_a_no_public_record_deep_check_is_a_review_issue() -> None:
    from lenz_io import ReviewIssues

    r = ReviewIssues.model_validate(_load("review_completed_issues_no_public_record.json"))
    assert r.outcome == "issues_found"
    (issue,) = r.issues
    assert (issue.verdict, issue.source, issue.suggested_rewrite) == ("No Public Record", "verification", None)
