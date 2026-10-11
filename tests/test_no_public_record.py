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


def _render_full(v: Verification, *, concise: bool = False) -> str:
    import io

    from rich.console import Console

    from lenz_io.cli.render import Output, render_verification, render_verification_full

    buf = io.StringIO()
    out = Output(json_mode=False, no_color=True)
    out.json_mode = False
    out.console = Console(file=buf, no_color=True, width=120)
    (render_verification if concise else render_verification_full)(out, v)
    return buf.getvalue()


def test_the_full_view_omits_the_empty_panel_and_debate() -> None:
    # The wire carries empty debate sides and no assessments on this outcome.
    v = Verification.model_validate(_load("verifications_detail_no_public_record.json"))
    v.audit.adjudication_summary = ""
    text = _render_full(v)
    assert "No Public Record" in text
    assert "Panel" not in text
    assert "Debate" not in text
    assert "PRO" not in text and "CON" not in text


def test_a_debate_side_with_only_one_argument_still_prints_just_that_side() -> None:
    from lenz_io import Audit, DebateSide

    v = Verification(
        verdict="False",
        lenz_score=2,
        audit=Audit(debate_pro=DebateSide(argument="It is so."), debate_con=DebateSide()),
    )
    text = _render_full(v)
    assert "Debate" in text and "PRO" in text and "It is so." in text
    assert "CON" not in text


def test_a_panel_summary_alone_prints_without_an_empty_debate() -> None:
    v = Verification.model_validate(_load("verifications_detail_no_public_record.json"))
    text = _render_full(v)
    assert "No public record was found to weigh." in text
    assert "Debate" not in text


def test_expected_record_reads_and_defaults_empty() -> None:
    body = _load("verifications_detail_no_public_record.json")
    assert Verification.model_validate(body).expected_record == ""
    body["expected_record"] = "A county court record of the sale."
    v = Verification.model_validate(body)
    assert v.expected_record == "A county court record of the sale."
    assert not (v.model_extra or {})
    body["expected_record"] = None
    assert Verification.model_validate(body).expected_record == ""
    assert VerificationListItem().expected_record == ""


@pytest.mark.parametrize("concise", [False, True], ids=["full", "concise"])
def test_the_cli_names_the_record_that_would_show_it(concise: bool) -> None:
    v = Verification.model_validate(_load("verifications_detail_no_public_record.json"))
    assert "Record that would show it" not in _render_full(v, concise=concise)
    v.expected_record = "A county court record of the sale."
    text = _render_full(v, concise=concise)
    assert "Record that would show it: A county court record of the sale." in text


def test_expected_record_text_is_printed_without_markup() -> None:
    v = Verification(verdict="No Public Record", expected_record="A [bold]filing[/bold].")
    assert "A [bold]filing[/bold]." in _render_full(v)
