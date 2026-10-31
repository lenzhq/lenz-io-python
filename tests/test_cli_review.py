"""``lenz review``: exit codes from the review's outcome, the three ways to hand
it a draft, ``--issues``, ``--json``, ``--detach`` / ``--resume``, and the
final render.

The client is the real SDK client with only the network stubbed (respx), so
the CLI is tested against the recorded server bodies it will actually read.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest
import respx
from rich.console import Console
from typer.testing import CliRunner

from lenz_io import Lenz, ReviewFull
from lenz_io.cli import _run, app as app_mod, config as cfg, normalize_argv
from lenz_io.cli.app import app
from lenz_io.cli.render import Output
from lenz_io.cli.review import exit_code_for_review, render_review

BASE = "https://lenz.io/api/v1"
FIXTURES = Path(__file__).parent / "fixtures" / "contract"
runner = CliRunner()


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.delenv("LENZ_BASE_URL", raising=False)
    monkeypatch.setenv("LENZ_API_KEY", "lenz_test_key")
    monkeypatch.setattr(cfg, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: None)
    monkeypatch.setattr(_run, "build_client", lambda **kw: Lenz(api_key="lenz_test_key"))


@pytest.fixture()
def human(monkeypatch):
    """Pretty output even though CliRunner's stdout is not a terminal."""

    class _Pretty(Output):
        def __init__(self, *, json_mode: bool, no_color: bool) -> None:
            super().__init__(json_mode=json_mode, no_color=True)
            self.json_mode = json_mode
            self.console = Console(file=None, no_color=True, width=200, highlight=False)

    monkeypatch.setattr(app_mod, "Output", _Pretty)


@pytest.fixture()
def draft(tmp_path):
    path = tmp_path / "draft.md"
    path.write_text("The EU AI Act entered into force on 1 August 2024.\n", encoding="utf-8")
    return str(path)


def _serve(r, *states: str, post: str = "review_accepted.json"):
    # The receipt names the review the last state belongs to, as the API would.
    receipt = dict(_load(post), review_id=_load(states[-1])["review_id"]) if states else _load(post)
    route_post = r.post("/review").respond(202, json=receipt)
    route_get = r.get(url__regex=r"/reviews/[0-9a-f]+$").mock(
        side_effect=[httpx.Response(200, json=_load(s)) for s in states]
    )
    return route_post, route_get


def _invoke(*args: str):
    return runner.invoke(app, normalize_argv(list(args)))


# ── exit codes ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("fixture", "code"),
    [
        ("review_completed.json", 1),
        ("review_incomplete.json", 2),
        ("review_failed_no_claim.json", 2),
        ("review_failed_insufficient_credits.json", 2),
    ],
)
def test_exit_code_follows_the_outcome(draft, fixture, code):
    with respx.mock(base_url=BASE) as r:
        _serve(r, fixture)
        result = _invoke("review", draft, "--json")
    assert result.exit_code == code, result.output
    assert json.loads(result.stdout)["review_id"] == _load(fixture)["review_id"]


def test_clean_review_exits_zero(draft):
    clean = _load("review_completed.json")
    clean["outcome"] = "clean"
    clean["issues"] = []
    with respx.mock(base_url=BASE) as r:
        r.post("/review").respond(202, json=_load("review_accepted.json"))
        r.get("/reviews/442b6aa9").respond(200, json=clean)
        result = _invoke("review", draft, "--json")
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    ("outcome", "code"), [("clean", 0), ("issues_found", 1), ("incomplete", 2), ("unchecked", 2), (None, 2), ("new", 2)]
)
def test_exit_code_table(outcome, code):
    assert exit_code_for_review(ReviewFull(outcome=outcome)) == code


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (402, {"detail": "No credits.", "code": "no_credits"}),
        (401, {"detail": "Bad key."}),
        (429, {"detail": "Busy.", "code": "review_in_flight", "retry_after_seconds": 60}),
        (503, {"detail": "Down.", "code": "upstream_unavailable", "retry_after": 90}),
    ],
)
def test_errors_exit_two_never_one(draft, status, body):
    # 1 means "issues found": a CI step must not read an outage or a bad key
    # as a draft with issues, nor any of them as clean.
    with respx.mock(base_url=BASE) as r:
        r.post("/review").respond(status, json=body, headers={"Retry-After": "90"})
        result = _invoke("review", draft, "--json")
    assert result.exit_code == 2, result.output
    assert "error" in json.loads(result.stdout)


def test_timeout_exits_two_with_the_resume_command(draft, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("lenz_io.client.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    with respx.mock(base_url=BASE) as r:
        r.post("/review").respond(202, json=_load("review_accepted.json"))
        r.get("/reviews/442b6aa9").respond(200, json=_load("review_verifying.json"))
        result = _invoke("review", draft, "--timeout", "30", "--json")
    assert result.exit_code == 2
    err = json.loads(result.stdout)["error"]
    assert err["code"] == "timeout"
    assert err["fix"] == "lenz review --resume 442b6aa9"


def test_no_key_exits_two(draft, monkeypatch):
    monkeypatch.delenv("LENZ_API_KEY")
    result = _invoke("review", draft, "--json")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "no_api_key"


# ── input ───────────────────────────────────────────────────────────────────


def test_reads_the_file_and_sends_the_flags(draft):
    with respx.mock(base_url=BASE) as r:
        post, _ = _serve(r, "review_completed.json")
        _invoke("review", draft, "--max-verifications", "2", "--depth", "low", "--json")
    body = json.loads(post.calls.last.request.content)
    assert body["text"] == "The EU AI Act entered into force on 1 August 2024.\n"
    assert body["escalate"] == {"max_verifications": 2, "depth": "low", "max_citations": 20}


def test_default_submit_checks_20_citations_and_nothing_else(draft):
    # The CLI's own default: the draft's sources are checked unless told
    # otherwise. Every other knob is left to the server.
    with respx.mock(base_url=BASE) as r:
        post, _ = _serve(r, "review_completed.json")
        _invoke("review", draft, "--json")
    assert json.loads(post.calls.last.request.content)["escalate"] == {"max_citations": 20}


def test_a_url_is_sent_as_the_text():
    with respx.mock(base_url=BASE) as r:
        post, _ = _serve(r, "review_completed.json")
        _invoke("review", "https://example.com/post", "--json")
    assert json.loads(post.calls.last.request.content)["text"] == "https://example.com/post"


def test_stdin(monkeypatch):
    with respx.mock(base_url=BASE) as r:
        post, _ = _serve(r, "review_completed.json")
        result = runner.invoke(app, normalize_argv(["review", "-", "--json"]), input="Piped draft.")
    assert result.exit_code == 1
    assert json.loads(post.calls.last.request.content)["text"] == "Piped draft."


def test_a_missing_file_is_refused_not_sent():
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        post = r.post("/review")
        result = _invoke("review", "drfat.md", "--json")
    assert result.exit_code == 2
    assert not post.called
    assert json.loads(result.stdout)["error"]["code"] == "no_input"


def test_bad_depth_is_refused():
    result = _invoke("review", "x.md", "--depth", "deep", "--json")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "invalid_depth"


# ── --issues, --detach, --resume ────────────────────────────────────────────


def test_issues_json_is_the_servers_issues_view(draft):
    with respx.mock(base_url=BASE) as r:
        r.post("/review").respond(202, json=_load("review_accepted.json"))
        get = r.get("/reviews/442b6aa9").mock(
            side_effect=[
                httpx.Response(200, json=_load("review_completed.json")),
                httpx.Response(200, json=_load("review_completed_issues.json")),
            ]
        )
        result = _invoke("review", draft, "--issues", "--json")
    assert result.exit_code == 1
    out = json.loads(result.stdout)
    assert out["view"] == "issues" and "claims" not in out
    assert get.calls.last.request.url.params["view"] == "issues"


def test_detach_prints_the_review_id_and_does_not_poll(draft):
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        _, get = _serve(r, "review_completed.json")
        result = _invoke("review", draft, "--detach", "--json")
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "submitted", "review_id": "442b6aa9"}
    assert not get.called


def test_detach_human(draft, human):
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        _serve(r, "review_completed.json")
        result = _invoke("review", draft, "--detach")
    assert "lenz review --resume 442b6aa9" in result.stdout


def test_resume_reads_without_submitting():
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        post, get = _serve(r, "review_verifying.json", "review_completed.json")
        result = _invoke("review", "--resume", "442b6aa9", "--json")
    assert result.exit_code == 1
    assert not post.called and get.call_count == 2


def test_resume_with_a_draft_is_refused(draft):
    result = _invoke("review", draft, "--resume", "442b6aa9", "--json")
    assert result.exit_code == 2


# ── the human render ────────────────────────────────────────────────────────


def _render(fixture: str, *, issues_only: bool = False) -> str:
    buf = io.StringIO()
    out = Output(json_mode=False, no_color=True)
    out.json_mode = False
    out.console = Console(file=buf, no_color=True, width=200, highlight=False)
    render_review(out, ReviewFull.model_validate(_load(fixture)), issues_only=issues_only)
    return buf.getvalue()


def test_full_render_lists_every_claim_with_its_final_verdict():
    text = _render("review_completed.json")
    assert text.startswith(
        "Review 442b6aa9: issues found — 4 claims: 2 issues, 2 true or mostly true"
        " · 2 of 2 deep-checked · 14 credits charged\n"
    )
    assert "[1/4] The EU AI Act entered into force in March 2024." in text
    assert "False (high) · deep check" in text
    assert "True (high) · quick check" in text
    assert "False (medium) · deep check" in text
    assert "EY reported 28% full compliance among surveyed organizations." in text
    assert "Suggested rewrite: The EU AI Act entered into force on 1 August 2024." in text
    assert "https://lenz.io/c/european-companies-ai-act-compliance-2024-86ea9355" in text
    assert "has not been verified itself" in text


def test_full_render_shows_where_the_draft_first_makes_each_claim():
    text = _render("review_completed_located.json")
    assert "[1/4] The EU AI Act entered into force in March 2024. · at 2-49\n" in text
    assert " · at " not in _render("review_completed.json")


def test_a_url_review_prints_no_span():
    """A URL draft's positions carry the passage but no offsets: no suffix."""
    body = _load("review_completed_located.json")
    for c in body["claims"]:
        for pos in c["positions"]:
            pos["start"] = pos["end"] = None
    text = _render_body(body)
    assert "[1/4] The EU AI Act entered into force in March 2024.\n" in text
    assert " · at " not in text


def test_issues_render_shows_quick_issues_and_failures():
    text = _render("review_incomplete.json", issues_only=True)
    assert "incomplete" in text
    assert "Mostly False (medium) · quick check" in text
    assert "Not deep-checked: the review's limit on deep checks was reached." in text
    assert "Quick check failed: The quick check ran out of time" in text
    assert [text.count(f"[{n}/4]") for n in (1, 2, 3, 4)] == [1, 1, 1, 1]  # each row once


def test_failed_review_render_says_why():
    text = _render("review_failed_no_claim.json")
    assert "unchecked" in text
    assert "Failed: The input requests a short poem" in text


def test_model_text_is_not_read_as_markup():
    body = _load("review_completed.json")
    body["issues"][0]["key_finding"] = "A [bold]tag[/bold] in page text."
    body["claims"][3]["verification"]["key_finding"] = "A [bold]tag[/bold] in page text."
    buf = io.StringIO()
    out = Output(json_mode=False, no_color=True)
    out.json_mode = False
    out.console = Console(file=buf, no_color=True, width=200, highlight=False)
    render_review(out, ReviewFull.model_validate(body))
    assert "A [bold]tag[/bold] in page text." in buf.getvalue()


def test_human_run_prints_the_final_view(draft, human):
    with respx.mock(base_url=BASE) as r:
        _serve(r, "review_queued.json", "review_assessing.json", "review_completed.json")
        result = _invoke("review", draft)
    assert result.exit_code == 1
    assert "Suggested rewrite:" in result.stdout


def test_progress_view_renders_every_state():
    from lenz_io.cli.review import render_progress

    console = Console(file=io.StringIO(), no_color=True, width=200)
    for name in (
        "review_queued.json",
        "review_assessing.json",
        "review_verifying.json",
        "review_completed.json",
        "review_incomplete.json",
    ):
        console.print(render_progress(ReviewFull.model_validate(_load(name))))
    console.print(render_progress(None))
    text = console.file.getvalue()
    assert "quick check…" in text and "deep check…" in text and "quick check failed" in text
    assert "(timeout)" not in text


def test_a_poll_error_after_submit_names_the_resume_command(draft):
    with respx.mock(base_url=BASE) as r:
        r.post("/review").respond(202, json=_load("review_accepted.json"))
        r.get("/reviews/442b6aa9").respond(404, json={"detail": "Review not found.", "code": "not_found"})
        result = _invoke("review", draft, "--json")
    assert result.exit_code == 2
    assert "lenz review --resume 442b6aa9" in json.loads(result.stdout)["error"]["fix"]


def test_ctrl_c_during_submit_exits_130(draft, monkeypatch):
    def interrupted(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(Lenz, "review", interrupted)
    result = _invoke("review", draft, "--json")
    assert result.exit_code == 130


def test_markup_in_a_confidence_value_is_printed_not_parsed():
    body = _load("review_completed.json")
    body["claims"][3]["verification"]["confidence"] = "[/bold]"
    buf = io.StringIO()
    out = Output(json_mode=False, no_color=True)
    out.json_mode = False
    out.console = Console(file=buf, no_color=True, width=200, highlight=False)
    render_review(out, ReviewFull.model_validate(body))
    assert "False ([/bold]) · deep check" in buf.getvalue()


@pytest.mark.parametrize(
    ("fixture", "summary"),
    [
        ("review_incomplete.json", "4 claims: 3 issues, 1 failed"),
        ("review_failed_insufficient_credits.json", "4 claims: 4 not checked"),
        ("review_failed_no_claim.json", "no claims checked"),
    ],
)
def test_summary_counts_every_claim(fixture, summary):
    assert summary in _render(fixture).splitlines()[0]


def test_no_line_carries_trailing_spaces():
    for fixture in ("review_completed.json", "review_incomplete.json"):
        for line in _render(fixture).splitlines():
            assert line == line.rstrip(), repr(line)


# ── --max-citations ─────────────────────────────────────────────────────────


def test_max_citations_is_sent_inside_escalate(draft):
    with respx.mock(base_url=BASE) as r:
        post, _ = _serve(r, "review_citations_constructed.json")
        _invoke("review", draft, "--max-citations", "10", "--json")
    body = json.loads(post.calls.last.request.content)
    assert body["escalate"] == {"max_citations": 10}
    assert "citations" not in body


def test_max_citations_zero_sends_no_citation_check(draft):
    with respx.mock(base_url=BASE) as r:
        post, _ = _serve(r, "review_completed.json")
        _invoke("review", draft, "--max-citations", "0", "--json")
    assert "escalate" not in json.loads(post.calls.last.request.content)


def test_the_sdk_default_checks_no_citation():
    # Only the CLI turns citations on: the library's review() sends nothing.
    import inspect

    assert inspect.signature(Lenz.review).parameters["max_citations"].default is None


def test_the_citations_flag_is_gone(draft):
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        post, _ = _serve(r, "review_completed.json")
        result = _invoke("review", draft, "--citations", "--json")
    assert result.exit_code == 2
    assert not post.called


@pytest.mark.parametrize("value", ["-1", "21"])
def test_max_citations_out_of_range_is_refused(draft, value):
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        post, _ = _serve(r, "review_completed.json")
        result = _invoke("review", draft, "--max-citations", value, "--json")
    assert result.exit_code == 2
    assert not post.called


@pytest.mark.parametrize(("outcome", "code"), [("issues_found", 1), ("incomplete", 2), ("clean", 0)])
def test_a_citation_outcome_sets_the_exit_code_like_a_claim_one(draft, outcome, code):
    body = _load("review_citations_constructed.json")
    body["outcome"] = outcome
    with respx.mock(base_url=BASE) as r:
        r.post("/review").respond(202, json=dict(_load("review_accepted.json"), review_id=body["review_id"]))
        r.get(f"/reviews/{body['review_id']}").respond(200, json=body)
        result = _invoke("review", draft, "--max-citations", "10", "--json")
    assert result.exit_code == code, result.output


def test_citation_render_count_summary_and_issues():
    text = _render("review_citations_constructed.json")
    lines = text.splitlines()
    i = lines.index("23 sources cited in your draft")
    assert lines[i + 1] == "6 checked, 6 with a problem. 4 could not be checked."
    assert lines[i + 2] == "13 more were not checked: one check covers 10."
    # each issue: the finding, the draft's sentence, the link, the evidence
    j = lines.index("[source 1/10] Contradicted")
    assert lines[j + 1].strip() == "Unemployment fell to 4.1% in 2024, according to a report from the ministry."
    assert lines[j + 2].strip() == "https://example.gov/report-2024"
    assert "The source says:" in lines[j + 3]
    assert "The unemployment rate averaged 4.6% in 2024, down from 4.9%." in lines[j + 4]
    assert "Reviewer's note: The report gives 4.6% for 2024; the draft says 4.1%." in text
    assert "[source 2/10] Quote not in the source" in text and "the best in a decade" in text
    assert "Your reference says 2015; the record says 2013." in text
    assert "[source 9/10] DOI not registered" in text
    assert "[source 7/10] Page not found" in text and "Part of the check failed:" in text
    assert "[source 8/10] Could not be checked this time." in text
    assert "[source 3/10] Partly supported" in text
    # an unchecked row is not an issue: it is listed once, to check by hand
    by_hand = text.index("Check these by hand")
    assert text.index("[source 4/10]") > by_hand
    assert text.count("[source 4/10]") == 1


def test_quote_finding_shows_the_missing_excerpt_only():
    body = _load("review_citations_constructed.json")
    body["citation_issues"][3]["quotes"] = ["found words here", "the best in a decade"]
    buf = io.StringIO()
    out = Output(json_mode=False, no_color=True)
    out.json_mode = False
    out.console = Console(file=buf, no_color=True, width=200, highlight=False)
    render_review(out, ReviewFull.model_validate(body))
    text = buf.getvalue()
    assert "\u201cthe best in a decade\u201d" in text
    assert "found words here" not in text


def test_citation_render_issues_only_view_keeps_the_citations():
    text = _render("review_citations_constructed.json", issues_only=True)
    assert "23 sources cited in your draft" in text and "[source 1/10] Contradicted" in text
    assert "No issues." not in text


def test_citation_count_line_cases():
    from lenz_io.cli.review import citation_count_lines

    base = _load("review_citations_constructed.json")

    def lines(**summary):
        body = json.loads(json.dumps(base))
        body["summary"].update(summary)
        return citation_count_lines(ReviewFull.model_validate(body))

    running = {"checked": 1, "unchecked": 0, "failed": 0}
    assert lines(citations_found=8, citations_selected=8, citation_checks=running) == [
        "8 sources cited in your draft",
        "Lenz checks each one: does the source say what your draft says it does.",
    ]
    assert lines(citations_found=1, citations_selected=1, citation_checks=None) == [
        "1 source cited in your draft",
        "Lenz checks each one: does the source say what your draft says it does.",
    ]
    assert lines(citation_checks=running) == [
        "23 sources cited in your draft",
        "Lenz checks the first 10: does the source say what your draft says it does. "
        "The other 13 are not checked; one check covers at most 10.",
    ]
    assert lines(citations_found=0, citations_selected=0) == [
        "No links found in the draft, so no sources were checked."
    ]
    assert lines(citations_found=None, citations_skipped="url_input") == [
        "Sources on a linked page are not checked. Paste the page's text instead to check them."
    ]
    assert lines(citations_skipped="switched_off") == []
    assert citation_count_lines(ReviewFull.model_validate(_load("review_completed.json"))) == []


def test_a_review_without_citations_renders_as_before():
    assert "source" not in _render("review_completed.json").lower().replace("sourced", "")


def test_citation_text_is_not_read_as_markup():
    body = _load("review_citations_constructed.json")
    body["citation_issues"][2]["snippet"] = "A [bold]tag[/bold] in page text."
    buf = io.StringIO()
    out = Output(json_mode=False, no_color=True)
    out.json_mode = False
    out.console = Console(file=buf, no_color=True, width=200, highlight=False)
    render_review(out, ReviewFull.model_validate(body))
    assert "A [bold]tag[/bold] in page text." in buf.getvalue()


def test_progress_view_counts_the_citation_checks():
    from lenz_io.cli.review import render_progress

    console = Console(file=io.StringIO(), no_color=True, width=200)
    console.print(render_progress(ReviewFull.model_validate(_load("review_citations_verifying.json"))))
    assert "1 of 10 sources checked" in console.file.getvalue()


def test_links_only_review_sends_max_assessments_zero(draft):
    with respx.mock(base_url=BASE) as r:
        post, _ = _serve(r, "review_citations_constructed.json")
        result = _invoke("review", draft, "--max-citations", "20", "--max-assessments", "0", "--json")
    assert result.exit_code == 2, result.output  # the fixture's outcome is incomplete
    body = json.loads(post.calls.last.request.content)
    assert body["escalate"] == {"max_assessments": 0, "max_citations": 20}


def test_max_assessments_is_sent(draft):
    with respx.mock(base_url=BASE) as r:
        post, _ = _serve(r, "review_completed.json")
        _invoke("review", draft, "--max-assessments", "5", "--json")
    assert json.loads(post.calls.last.request.content)["escalate"] == {"max_assessments": 5, "max_citations": 20}


@pytest.mark.parametrize("value", ["-1", "21"])
def test_max_assessments_out_of_range_is_refused(draft, value):
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        post, _ = _serve(r, "review_completed.json")
        result = _invoke("review", draft, "--max-assessments", value, "--json")
    assert result.exit_code == 2
    assert not post.called


@pytest.mark.parametrize(
    ("checks", "issues", "expected"),
    [
        ({"checked": 8, "unchecked": 1, "failed": 1}, 7, "8 checked, 7 with a problem. 2 could not be checked."),
        ({"checked": 8, "unchecked": 0, "failed": 0}, 0, "8 checked."),
        ({"checked": 3, "unchecked": 0, "failed": 0}, 3, "3 checked, 3 with a problem."),
        ({"checked": 0, "unchecked": 2, "failed": 0}, 0, "2 could not be checked."),
    ],
)
def test_citation_summary_line_is_the_key_numbers(checks, issues, expected):
    from lenz_io.cli.review import citation_summary_line

    body = _load("review_citations_constructed.json")
    body["summary"].update(citation_checks=checks, citation_issues=issues)
    body["citation_issues"] = body["citation_issues"][:issues]
    assert citation_summary_line(ReviewFull.model_validate(body)) == expected


def test_one_more_over_the_cap_is_singular():
    from lenz_io.cli.review import citation_count_lines

    body = _load("review_citations_constructed.json")
    body["summary"].update(citations_found=11)
    assert citation_count_lines(ReviewFull.model_validate(body))[-1] == "1 more was not checked: one check covers 10."


def test_citation_render_of_a_recorded_review():
    text = _render("review_citations_completed.json")
    lines = text.splitlines()
    i = lines.index("10 sources cited in your draft")
    assert lines[i + 1] == "6 checked, 5 with a problem. 4 could not be checked."
    assert "[source 3/10] Contradicted" in text and "[source 9/10] DOI not registered" in text
    # a supported row is counted, never listed
    assert "[source 1/10]" not in text
    assert "The source says:" in text


def test_citation_render_of_a_recorded_quote_finding():
    text = _render("review_citations_quote.json")
    assert "These quoted words were not found in the source:" in text
    assert "“built the tower to stand for a thousand years”" in text


def test_more_found_line():
    from lenz_io.cli.review import more_found_line

    def line(claims, citations):
        body = _load("review_citations_constructed.json")
        body["more_claims"] = claims
        body["more_citations"] = citations
        return more_found_line(ReviewFull.model_validate(body))

    cit = _load("review_citations_constructed.json")["more_citations"][0]
    assert line(["a", "b"], [cit] * 13) == "2 more claims and 13 more citations were found but not checked."
    assert line([], [cit]) == "1 more citation was found but not checked."
    assert line(["a"], []) == "1 more claim was found but not checked."
    assert line([], []) == "" and line(None, None) == ""


def test_the_more_line_prints_after_the_citations():
    text = _render("review_citations_constructed.json")
    assert text.rstrip().endswith("1 more citation was found but not checked.")


def test_skipped_for_credits_says_so():
    from lenz_io.cli.review import citation_count_lines

    body = _load("review_citations_constructed.json")
    body["summary"].update(citations_skipped="insufficient_credits", citations_found=None)
    assert citation_count_lines(ReviewFull.model_validate(body)) == [
        "The sources were not checked: not enough credits."
    ]


def test_no_max_citations_prints_no_sources_section():
    from lenz_io.cli.review import citation_count_lines

    body = _load("review_citations_constructed.json")
    body["policy"]["max_citations"] = 0
    assert citation_count_lines(ReviewFull.model_validate(body)) == []


# ── the editor's view: every reason, what to check by hand, clickable links ─


def test_a_not_in_source_issue_prints_its_reason():
    body = _load("review_citations_completed.json")
    body["citation_issues"] = [
        dict(
            body["citation_issues"][0],
            finding="not_in_source",
            source="support",
            snippet=None,
            rationale="The page does not mention the four risk tiers.",
        )
    ]
    text = _render_body(body)
    assert "Not in the source" in text
    assert "Reviewer's note: The page does not mention the four risk tiers." in text
    assert "The source says:" not in text


def test_sources_that_could_not_be_checked_are_listed_with_why():
    text = _render("review_citations_completed.json")
    assert "Check these by hand" in text
    after = text[text.index("Check these by hand") :]
    assert "Lenz does not read this site." in after or "not checked" in after.lower()
    assert "https://www.youtube.com/watch?v=dQw4w9WgXcQ" in after


def test_a_long_link_is_never_hard_wrapped():
    body = _load("review_citations_completed.json")
    long_url = "https://example.org/" + "a" * 250
    body["citation_issues"][0]["cited_url"] = long_url
    buf = io.StringIO()
    out = Output(json_mode=False, no_color=True)
    out.json_mode = False
    out.console = Console(file=buf, no_color=True, width=80, highlight=False)
    render_review(out, ReviewFull.model_validate(body), issues_only=True)
    assert "  " + long_url + "\n" in buf.getvalue()


def test_the_issues_view_shows_low_confidence_claims():
    body = _load("review_completed.json")
    target = next(c for c in body["claims"] if c["result"] and not c["result"]["is_issue"])
    target["result"]["confidence"] = "low"
    text = _render_body(body, issues_only=True)
    assert "Low confidence" in text
    assert target["claim"] in text[text.index("Low confidence") :]


def test_the_issues_view_has_no_low_confidence_section_without_one():
    assert "Low confidence" not in _render("review_completed.json", issues_only=True)


def test_language_is_sent(draft):
    with respx.mock(base_url=BASE) as r:
        post, _ = _serve(r, "review_completed.json")
        _invoke("review", draft, "--language", "DE", "--json")
    assert json.loads(post.calls.last.request.content)["language"] == "de"


def test_no_language_sends_none(draft):
    with respx.mock(base_url=BASE) as r:
        post, _ = _serve(r, "review_completed.json")
        _invoke("review", draft, "--json")
    assert "language" not in json.loads(post.calls.last.request.content)


def _render_body(body: dict, *, issues_only: bool = False) -> str:
    buf = io.StringIO()
    out = Output(json_mode=False, no_color=True)
    out.json_mode = False
    out.console = Console(file=buf, no_color=True, width=200, highlight=False)
    render_review(out, ReviewFull.model_validate(body), issues_only=issues_only)
    return buf.getvalue()


@pytest.mark.parametrize(
    ("args", "line"),
    [
        ((), "Checking claims and up to 20 citations…"),
        (("--max-citations", "5"), "Checking claims and up to 5 citations…"),
        (("--max-citations", "0"), "Checking claims…"),
        (("--max-assessments", "0"), "Checking up to 20 citations…"),
    ],
)
def test_the_run_opens_by_saying_what_it_checks(draft, human, args, line):
    with respx.mock(base_url=BASE) as r:
        _serve(r, "review_completed.json")
        result = _invoke("review", draft, *args)
    # `output` is stdout and stderr together, on every click version.
    assert line in result.output


def test_json_mode_prints_no_opening_line(draft):
    with respx.mock(base_url=BASE) as r:
        _serve(r, "review_completed.json")
        result = _invoke("review", draft, "--json")
    assert "Checking" not in result.output
    json.loads(result.stdout)


def test_help_states_the_citation_default_and_its_cost():
    result = runner.invoke(app, ["review", "--help"], env={"COLUMNS": "200"})
    assert "default 20" in result.stdout and "1 credit per checked citation" in result.stdout


# ── "No Public Record" and the collapsed failure codes ──────────────────────


def test_a_quick_no_public_record_is_listed_as_an_issue():
    text = _render("review_completed_quick_no_public_record.json")
    assert text.splitlines()[0].startswith("Review c41d7e02: issues found — 2 claims: 1 issue, 1 true or mostly true")
    assert "No Public Record (medium) · quick check" in text
    assert "/10" not in text


def test_a_low_confidence_no_public_record_is_an_issue_not_a_second_look():
    body = _load("review_completed_quick_no_public_record.json")
    body["claims"][0]["assessment"]["confidence"] = "low"
    body["claims"][0]["result"]["confidence"] = "low"
    body["issues"][0]["confidence"] = "low"
    text = _render_body(body, issues_only=True)
    assert text.count("No Public Record (low) · quick check") == 1
    assert "Low confidence" not in text


def test_a_quick_no_public_record_exits_one(draft):
    with respx.mock(base_url=BASE) as r:
        _serve(r, "review_completed_quick_no_public_record.json")
        result = _invoke("review", draft, "--json")
    assert result.exit_code == 1, result.output


def test_a_link_the_review_could_not_read_says_so_and_what_to_send():
    text = _render("review_failed_page_unreadable.json")
    assert "Failed: The page asks for a login." in text
    assert "Paste the page's text instead." in text


def test_a_page_unreadable_failure_without_a_hint_still_says_it():
    body = _load("review_failed_page_unreadable.json")
    body["failure"]["hint"] = None
    text = _render_body(body)
    assert "Failed: The page at the link could not be read." in text
    assert "Paste the page's text instead." in text


def test_another_failure_prints_no_paste_advice():
    assert "Paste the page" not in _render("review_failed_no_claim.json")


def test_a_review_of_an_unreadable_link_exits_two(draft):
    with respx.mock(base_url=BASE) as r:
        _serve(r, "review_failed_page_unreadable.json")
        result = _invoke("review", draft, "--json")
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["failure"]["code"] == "page_unreadable"


# ── plain words for what the server reports in codes ────────────────────────

DISPOSITION_WORDS = {
    "cap": "Not deep-checked: the review's limit on deep checks was reached.",
    "credits": "Not deep-checked: there were not enough credits.",
    "account_cap": "Not deep-checked: the account's limit on running deep checks was reached.",
    "not_selected": "Not deep-checked.",
}


def _with_disposition(disposition: str) -> dict:
    body = _load("review_incomplete.json")
    for row in body["claims"] + body["issues"]:
        if (row.get("escalation") or {}).get("disposition") == "cap":
            row["escalation"]["disposition"] = disposition
    return body


@pytest.mark.parametrize("disposition", sorted(DISPOSITION_WORDS))
@pytest.mark.parametrize("issues_only", [False, True])
def test_a_claim_not_deep_checked_says_why_in_words_never_a_code(disposition, issues_only):
    text = _render_body(_with_disposition(disposition), issues_only=issues_only)
    if disposition == "not_selected" and not issues_only:
        # The full view shows a claim no rule selected by its quick verdict alone.
        assert "Not deep-checked" not in text
    else:
        assert DISPOSITION_WORDS[disposition] in text
    assert f"({disposition})" not in text
    assert "not_selected" not in text and "account_cap" not in text


def test_an_unknown_or_empty_disposition_never_prints_a_code():
    body = _with_disposition("a_future_reason")
    text = _render_body(body, issues_only=True)
    assert "a_future_reason" not in text
    assert "Not deep-checked." in text
    assert "a_future_reason" not in _render_body(body)
    assert "()" not in _render_body(_with_disposition(""), issues_only=True)


def test_a_planned_deep_check_prints_no_not_deep_checked_line():
    assert "Not deep-checked" not in _render("review_completed.json", issues_only=True)


def test_a_failed_quick_check_reads_the_same_in_both_views():
    for issues_only in (False, True):
        text = _render("review_incomplete.json", issues_only=issues_only)
        assert "Quick check failed:" in text
        assert "Assessment failed" not in text
        assert "Verification failed" not in text


def test_a_failed_deep_check_in_the_failures_list_says_deep_check():
    body = _load("review_incomplete.json")
    body["failures"][0]["stage"] = "verification"
    text = _render_body(body, issues_only=True)
    assert "Deep check failed:" in text
    assert "Verification failed" not in text


def _failed_quick_row(failure: dict | None) -> dict:
    body = _load("review_incomplete.json")
    row = next(c for c in body["claims"] if c["assessment"]["status"] == "failed")
    row["assessment"]["failure"] = failure
    for f in body["failures"]:
        if f["claim_index"] == row["index"]:
            f["failure"] = failure
    return body


UNRESOLVED = {
    "code": "upstream_unavailable",
    "detail": "The reviewers did not reach an answer.",
    "hint": "The quick check ran out of time on this claim. Retry it; nothing was charged.",
    "failure_class": "upstream_unavailable",
    "retryable": True,
}


@pytest.mark.parametrize("issues_only", [False, True])
def test_a_failed_row_says_what_the_failure_says_it_is(issues_only):
    text = _render_body(_failed_quick_row(UNRESOLVED), issues_only=issues_only)
    assert "Quick check failed: The reviewers did not reach an answer." in text
    assert "ran out of time" not in text


@pytest.mark.parametrize("issues_only", [False, True])
def test_a_failed_row_without_a_detail_uses_the_hint(issues_only):
    failure = {k: v for k, v in UNRESOLVED.items() if k != "detail"}
    text = _render_body(_failed_quick_row(failure), issues_only=issues_only)
    assert "Quick check failed: The quick check ran out of time on this claim." in text


@pytest.mark.parametrize("issues_only", [False, True])
@pytest.mark.parametrize("failure", [None, {"code": "upstream_unavailable", "failure_class": "upstream_unavailable"}])
def test_a_failed_row_with_nothing_to_say_is_neutral_not_a_code(failure, issues_only):
    text = _render_body(_failed_quick_row(failure), issues_only=issues_only)
    assert "Quick check failed: Lenz could not check this just now. Try again." in text
    assert "upstream_unavailable" not in text


# ── File and Docx CLI support ───────────────────────────────────────────────


def test_review_binary_docx_file(tmp_path):
    docx_file = tmp_path / "report.docx"
    docx_file.write_bytes(b"PK\x03\x04fake docx content")

    with respx.mock(base_url=BASE) as r:
        post = r.post("/review/file").respond(
            202,
            json={"status": "queued", "review_id": "442b6aa9"},
        )
        r.get(f"{BASE}/reviews/442b6aa9").respond(200, json=_load("review_completed.json"))

        res = _invoke("review", str(docx_file))
        assert res.exit_code == 1  # review_completed.json outcome is issues_found
        assert post.called
        content = post.calls.last.request.content
        assert b'filename="report.docx"' in content


def test_review_binary_pdf_file(tmp_path):
    pdf_file = tmp_path / "memo.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 fake pdf content")

    with respx.mock(base_url=BASE) as r:
        post = r.post("/review/file").respond(
            202,
            json={"status": "queued", "review_id": "442b6aa9"},
        )
        r.get(f"{BASE}/reviews/442b6aa9").respond(200, json=_load("review_completed.json"))

        res = _invoke("review", str(pdf_file))
        assert res.exit_code == 1
        assert post.called
        content = post.calls.last.request.content
        assert b'filename="memo.pdf"' in content


def test_review_export_download_option(tmp_path):
    docx_file = tmp_path / "report.docx"
    docx_file.write_bytes(b"PK\x03\x04fake docx content")
    out_docx = tmp_path / "annotated.docx"

    with respx.mock(base_url=BASE) as r:
        post = r.post("/review/file").respond(
            202,
            json={"status": "queued", "review_id": "442b6aa9"},
        )
        r.get(f"{BASE}/reviews/442b6aa9").respond(200, json=_load("review_completed.json"))
        r.get(f"{BASE}/reviews/442b6aa9/export").respond(
            200,
            content=b"PK\x03\x04annotated docx downloaded",
            headers={"Content-Disposition": 'attachment; filename="annotated.docx"'},
        )

        res = _invoke("review", str(docx_file), "--export", str(out_docx))
        assert res.exit_code == 1
        assert out_docx.exists()
        assert out_docx.read_bytes() == b"PK\x03\x04annotated docx downloaded"
        # Verify suggest_edits=True was included in escalate JSON in multipart payload
        assert post.called
        content = post.calls.last.request.content
        assert b'"suggest_edits": true' in content or b'"suggest_edits":true' in content


def test_review_export_with_detach_fails(tmp_path):
    docx_file = tmp_path / "report.docx"
    docx_file.write_bytes(b"PK\x03\x04fake docx")
    out_docx = tmp_path / "out.docx"

    res = _invoke("review", str(docx_file), "--export", str(out_docx), "--detach")
    assert res.exit_code == 2
    assert "Cannot combine --export with --detach" in res.output


def test_review_export_unsupported_extension(tmp_path):
    docx_file = tmp_path / "report.docx"
    docx_file.write_bytes(b"PK\x03\x04fake docx")

    res = _invoke("review", str(docx_file), "--export", str(tmp_path / "out.pdf"))
    assert res.exit_code == 2
    assert "Unsupported --export extension" in res.output


def test_review_export_url_draft_fails():
    res = _invoke("review", "https://example.com/article", "--export", "out.docx")
    assert res.exit_code == 2
    assert "--export is not supported for URL drafts" in res.output


def test_review_export_overwriting_input_file(tmp_path):
    docx_file = tmp_path / "report.docx"
    docx_file.write_bytes(b"PK\x03\x04fake docx")

    res = _invoke("review", str(docx_file), "--export", str(docx_file))
    assert res.exit_code == 2
    assert "--export path must not overwrite the input draft file" in res.output


def test_review_empty_binary_file(tmp_path):
    empty_file = tmp_path / "empty.docx"
    empty_file.write_bytes(b"")

    res = _invoke("review", str(empty_file))
    assert res.exit_code == 2
    assert "File is empty:" in res.output


def test_review_export_nothing_to_mark(tmp_path):
    docx_file = tmp_path / "report.docx"
    docx_file.write_bytes(b"PK\x03\x04fake docx content")
    out_docx = tmp_path / "annotated.docx"

    clean = _load("review_completed.json")
    clean["outcome"] = "clean"
    clean["issues"] = []

    with respx.mock(base_url=BASE) as r:
        r.post("/review/file").respond(
            202,
            json={"status": "queued", "review_id": "442b6aa9"},
        )
        r.get(f"{BASE}/reviews/442b6aa9").respond(200, json=clean)
        r.get(f"{BASE}/reviews/442b6aa9/export").respond(
            409,
            json={"code": "nothing_to_mark", "detail": "No issues were flagged to mark in the export."},
        )

        res = _invoke("review", str(docx_file), "--export", str(out_docx))
        assert res.exit_code == 0
        assert "nothing to mark" in res.output.lower()
        assert not out_docx.exists()


def test_review_export_download_fails(tmp_path):
    docx_file = tmp_path / "report.docx"
    docx_file.write_bytes(b"PK\x03\x04fake docx content")
    out_docx = tmp_path / "annotated.docx"

    with respx.mock(base_url=BASE) as r:
        r.post("/review/file").respond(
            202,
            json={"status": "queued", "review_id": "442b6aa9"},
        )
        r.get(f"{BASE}/reviews/442b6aa9").respond(200, json=_load("review_completed.json"))
        r.get(f"{BASE}/reviews/442b6aa9/export").respond(
            500,
            json={"code": "internal", "detail": "Export generation failed."},
        )

        res = _invoke("review", str(docx_file), "--export", str(out_docx))
        assert res.exit_code == 2
        assert "Failed to export annotated document" in res.output
