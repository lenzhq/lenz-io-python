"""``lenz review`` — review a whole draft in one call: every claim gets a quick
verdict, the ones that look wrong or uncertain get a deep check, and the issues
come back with suggested rewrites.

Progress is shown by default on a terminal: the quick verdicts print as soon as
they are in, and each row is rewritten when its deep check lands. The final
result goes to stdout; ``--json`` (or a non-terminal stdout) prints the review
body instead.

The exit code is the review's ``outcome``, so a CI step can gate on it and
never read an outage as a clean draft::

    0  clean          every selected claim checked, no issue
    1  issues_found   at least one claim is False, Mostly False or Mixed, or
                      a source does not say what the draft says it does
    2  anything else  incomplete, unchecked, failed, timed out, or an error
                      (a bad key, no credits, a bad flag)

The draft's first 20 sources (its links and DOIs; in a file, keep links as
markdown links) are checked too, at 1 credit per checked citation: their
count and their issues print after the claims. ``--max-citations N`` checks
the first N, and ``--max-citations 0`` none. ``--max-assessments 0`` checks
the sources and no claim. This default is the CLI's own: the SDK's
``review()`` checks no citation unless asked.

``--detach`` submits and prints the ``review_id``; ``lenz review --resume <id>``
picks it up again, as does Ctrl-C's hint.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Union

import typer
from rich.markup import escape

from lenz_io import Lenz
from lenz_io.errors import LenzError, ReviewFailed, ReviewTimeout
from lenz_io.models import Citecheck, ReviewCitationIssue, ReviewClaim, ReviewFull, ReviewIssues

from ._run import execute, read_text_arg
from .context import CLIState
from .errors import CLIError
from .render import _VERDICT_COLOR, Output, _truncate

DEPTH_CHOICES = ("standard", "low")

EXIT_CLEAN = 0
EXIT_ISSUES = 1
EXIT_OTHER = 2
_EXIT_BY_OUTCOME = {"clean": EXIT_CLEAN, "issues_found": EXIT_ISSUES}

_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)

_OUTCOME_LABEL = {
    "clean": "[green]clean[/green]",
    "issues_found": "[red]issues found[/red]",
    "incomplete": "[yellow]incomplete[/yellow]",
    "unchecked": "[yellow]unchecked[/yellow]",
}


def review(
    ctx: typer.Context,
    draft: str = typer.Argument(None, help="The draft: a file path, '-' (or a pipe) for stdin, or one http(s) URL."),
    issues: bool = typer.Option(False, "--issues", help="Print only the issues (and any failed rows)."),
    max_assessments: int = typer.Option(
        None,
        "--max-assessments",
        metavar="N",
        min=0,
        max=20,
        help="Quick-check at most N claims (0-20, default 20; 0 = no claim, e.g. with --max-citations).",
    ),
    max_verifications: int = typer.Option(
        None, "--max-verifications", metavar="N", help="Deep-check at most N claims (default 5; 0 = quick checks only)."
    ),
    depth: str = typer.Option(
        None, "--depth", metavar="standard|low", help="Depth of every deep check. 'low' costs half the credits."
    ),
    max_citations: int = typer.Option(
        None,
        "--max-citations",
        metavar="N",
        min=0,
        max=20,
        help="Check the draft's first N sources (0-20, default 20; 0 = none). 1 credit per checked citation.",
    ),
    language: str = typer.Option(
        None,
        "--language",
        metavar="CODE",
        help="Write the results in this language (ISO 639-1, e.g. de). Default English.",
    ),
    detach: bool = typer.Option(False, "--detach", help="Submit and exit; print the review_id to resume."),
    resume: str = typer.Option(None, "--resume", metavar="REVIEW_ID", help="Pick up a review started earlier."),
    timeout: float = typer.Option(600.0, "--timeout", help="Max seconds to wait."),
) -> None:
    """Review a draft and its sources. Exit codes: 0 clean, 1 issues, 2 anything else.

    Costs 1 credit per claim checked, plus 10 per deep check (5 at --depth low), plus 1 per checked citation.
    """
    state: CLIState = ctx.obj
    out = state.output

    def work(client: Lenz) -> None:
        try:
            _work(client)
        except KeyboardInterrupt:
            # Never left to the CLI framework: an older typer turns it into
            # exit 1, which here means "issues found".
            raise SystemExit(130) from None

    def _work(client: Lenz) -> None:
        if resume:
            if draft:
                raise CLIError("Pass either a draft or --resume, not both.", code="invalid_usage", exit_code=2)
            review_id = resume
        else:
            chosen_depth = _parse_depth(depth)
            text = _read_draft(draft)
            citations = DEFAULT_MAX_CITATIONS if max_citations is None else max_citations
            if not out.json_mode:
                out.err.print(f"[dim]{escape(opening_line(max_assessments, citations))}[/dim]")
            started = client.review(
                text,
                max_assessments=max_assessments,
                max_verifications=max_verifications,
                depth=chosen_depth,
                max_citations=citations,
                language=(language or "").strip().lower(),
            )
            review_id = started.review_id
            if detach:
                _emit_detached(out, review_id)
                return
        try:
            final = _wait(client, out, review_id, timeout)
        except LenzError as exc:
            # The review was accepted and may still finish: say how to get it.
            exc.fix = f"{exc.fix} Resume with: lenz review --resume {review_id}".strip()
            raise
        if out.json_mode:
            if issues:
                out.emit_json(client.get_review(review_id, view="issues").model_dump(mode="json"))
            else:
                out.emit_json(final.model_dump(mode="json"))
        else:
            render_review(out, final, issues_only=issues)
        raise SystemExit(exit_code_for_review(final))

    execute(state, needs_key=True, work=work, error_exit=EXIT_OTHER)


#: The CLI checks a draft's sources unless told otherwise. The SDK's own
#: ``review()`` default stays none, as the API's does.
DEFAULT_MAX_CITATIONS = 20


def opening_line(max_assessments: int | None, max_citations: int) -> str:
    """What the run is about to do: "Checking claims and up to 20 citations…"."""
    claims = max_assessments != 0
    if claims and max_citations:
        return f"Checking claims and up to {max_citations} citations…"
    if max_citations:
        return f"Checking up to {max_citations} citations…"
    return "Checking claims…"


def exit_code_for_review(review: ReviewFull) -> int:
    """0 clean, 1 issues_found, 2 for every other outcome (or none)."""
    return _EXIT_BY_OUTCOME.get(review.outcome or "", EXIT_OTHER)


def _parse_depth(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = raw.strip().lower()
    if cleaned not in DEPTH_CHOICES:
        raise CLIError(
            f"--depth expects one of {'|'.join(DEPTH_CHOICES)} (got {raw!r}).", code="invalid_depth", exit_code=2
        )
    return cleaned


def _read_draft(arg: str | None) -> str:
    """A file path, ``-`` / a pipe for stdin, or one URL. Anything else is
    refused rather than sent as text: a mistyped file name submitted as the
    draft would be charged for a review of the file name."""
    if arg is None or arg == "-":
        return read_text_arg(arg)
    if _URL.match(arg.strip()):
        return arg.strip()
    path = Path(arg)
    if not path.is_file():
        raise CLIError(
            f"No such file: {arg}",
            code="no_input",
            fix="Pass a file, '-' to read stdin, or one http(s) URL.",
            exit_code=2,
        )
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise CLIError(f"{arg} is not UTF-8 text.", code="no_input", exit_code=2) from None
    if not text.strip():
        raise CLIError(f"{arg} is empty.", code="no_input", exit_code=2)
    return text


def _emit_detached(out: Output, review_id: str) -> None:
    if out.json_mode:
        out.emit_json({"status": "submitted", "review_id": review_id})
    else:
        out.console.print(f"Review started (review {review_id}).")
        out.console.print(f"[dim]Read it with:[/dim] lenz review --resume {review_id}")


def _wait(client: Lenz, out: Output, review_id: str, timeout: float) -> ReviewFull:
    """Poll to the end, with a live per-claim view on a terminal. A failed
    review returns its body (the caller renders it and exits 2); a timeout and
    Ctrl-C print the resume command."""
    live_ok = not out.json_mode and sys.stderr.isatty()
    try:
        if live_ok:
            from rich.live import Live

            with Live(render_progress(None), console=out.err, refresh_per_second=8, transient=True) as live:
                return _wait_quiet(client, review_id, timeout, on_update=lambda rv: live.update(render_progress(rv)))
        return _wait_quiet(client, review_id, timeout, on_update=None)
    except KeyboardInterrupt:
        if out.json_mode:
            out.emit_json({"status": "interrupted", "review_id": review_id})
        else:
            out.err.print(
                f"\n[yellow]Still running server-side.[/yellow] Resume with:\n  lenz review --resume {review_id}"
            )
        raise SystemExit(130) from None
    except ReviewTimeout:
        raise CLIError(
            f"Timed out after {timeout:g}s; the review keeps running server-side.",
            code="timeout",
            fix=f"lenz review --resume {review_id}",
            exit_code=EXIT_OTHER,
        ) from None


def _wait_quiet(client: Lenz, review_id: str, timeout: float, *, on_update: Any) -> ReviewFull:
    try:
        return client._wait_review(review_id, timeout=timeout, on_update=on_update)
    except ReviewFailed as exc:
        if exc.review is None:
            raise
        return exc.review


# ── rendering ───────────────────────────────────────────────────────────────


def _verdict_text(verdict: str | None, confidence: str | None) -> str:
    color = _VERDICT_COLOR.get(verdict or "", "white")
    conf = f" ({escape(confidence)})" if confidence else ""
    return f"[bold {color}]{escape(verdict or '?')}[/bold {color}]{conf}"


def _progress_cell(c: ReviewClaim) -> Any:
    from rich.spinner import Spinner
    from rich.text import Text

    a, v = c.assessment, c.verification
    if a.status in ("pending", "running", ""):
        return Spinner("dots", text=Text("quick check…", style="dim"))
    if a.status == "failed":
        return Text(f"failed ({a.error_code or 'error'})", style="red")
    quick = Text.from_markup(_verdict_text(a.verdict, a.confidence))
    if v is None:
        return quick
    if v.status == "processing":
        return Spinner("dots", text=quick + Text("  deep check…", style="dim"))
    if v.status == "failed":
        return quick + Text("  deep check failed", style="red")
    return Text.from_markup(_verdict_text(v.verdict, v.confidence)) + Text("  deep", style="dim")


def render_progress(review: ReviewFull | None) -> Any:
    """The live stderr view: one row per claim, each rewritten as it moves."""
    from rich.spinner import Spinner
    from rich.table import Table
    from rich.text import Text

    if review is None or (not review.claims and not review.summary.citations_selected):
        return Spinner("dots", text=Text("Reading the draft…", style="dim"))
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="right", style="dim")
    table.add_column(min_width=26)
    table.add_column()
    n = len(review.claims)
    for c in review.claims:
        table.add_row(f"[{c.index + 1}/{n}]", _progress_cell(c), Text(_truncate(c.claim or "")))
    progress = _citation_progress(review)
    if progress:
        table.add_row("", Text(progress, style="dim"), Text(""))
    return table


def _citation_progress(review: ReviewFull) -> str:
    """ "7 of 10 sources checked" while the citation checks run."""
    s = review.summary
    if not review.policy.max_citations or not s.citations_selected:
        return ""
    counts = s.citation_checks
    done = (counts.checked + counts.unchecked + counts.failed) if counts is not None else 0
    return f"{done} of {s.citations_selected} {'source' if s.citations_selected == 1 else 'sources'} checked"


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _summary_line(review: ReviewFull) -> str:
    """One line: the outcome, then where every claim landed. The issue set is
    fixed (False, Mostly False, Mixed), so a claim is an issue, true or mostly
    true, failed, or not checked: four buckets say it all."""
    s = review.summary
    selected = s.claims_selected or 0
    if not selected:
        parts = ["no claims checked"]
    else:
        issues = len(review.issues)
        fine = sum(1 for c in review.claims if c.result is not None and not c.result.is_issue)
        failed = len(review.failures)
        unchecked = max(0, selected - issues - fine - failed)
        buckets = [
            _count(issues, "issue") if issues else "",
            f"{fine} true or mostly true" if fine else "",
            f"{failed} failed" if failed else "",
            f"{unchecked} not checked" if unchecked else "",
        ]
        parts = [f"{_count(selected, 'claim')}: " + ", ".join(b for b in buckets if b)]
    if s.verifications and s.verifications.planned:
        parts.append(f"{s.verifications.completed} of {s.verifications.planned} deep-checked")
    parts.append(f"{_count(review.credits.charged, 'credit')} charged")
    outcome = _OUTCOME_LABEL.get(review.outcome or "", escape(review.outcome or review.status or "?"))
    return f"Review {escape(review.review_id)}: {outcome} — " + " · ".join(parts)


def _print_text(out: Output, text: str | None, *, style: str = "") -> None:
    """Model-written text as plain text (never markup), wrapped by hand so
    continuation lines keep the indent and no line carries trailing spaces
    into a copy or a pipe."""
    if not text:
        return
    import textwrap

    from rich.text import Text

    width = max(20, out.console.width - 2)
    for paragraph in text.splitlines() or [""]:
        for line in textwrap.wrap(paragraph, width) or [""]:
            out.console.print(Text("  " + line, style=style), soft_wrap=True)


def _print_link(out: Output, url: str) -> None:
    """A link on its own line, never wrapped by us: a terminal wraps it
    itself and keeps it one clickable link."""
    from rich.text import Text

    out.console.print(Text("  " + url, style="blue"), soft_wrap=True)


def _position_suffix(c: ReviewClaim) -> str:
    """`` · at 2-49``: where the draft first makes the claim, when the server
    placed it by offset (never for a URL draft, whose positions carry the
    passage only, or an older server)."""
    first = c.positions[0] if c.positions else None
    if first is None or first.start is None or first.end is None:
        return ""
    return f" [dim]· at {first.start}-{first.end}[/dim]"


def _render_claim(out: Output, c: ReviewClaim, n: int) -> None:
    out.console.print(f"[bold]\\[{c.index + 1}/{n}][/bold] {escape(c.claim or '')}{_position_suffix(c)}")
    a, v = c.assessment, c.verification
    if a.status == "failed":
        hint = (a.failure.hint if a.failure else None) or a.hint or a.error_code or "failed"
        out.console.print(f"  [red]Quick check failed:[/red] {escape(hint)}")
        return
    if v is not None and v.status == "completed":
        out.console.print(f"  {_verdict_text(v.verdict, v.confidence)} [dim]· deep check[/dim]")
        if v.content_status != "available":
            out.console.print(f"  [dim]Its text has since been removed ({escape(v.verification_id or '')}).[/dim]")
        _print_text(out, v.key_finding)
        if v.suggested_rewrite:
            out.console.print(f"  [dim]Suggested rewrite:[/dim] {escape(v.suggested_rewrite)}")
        if v.url:
            _print_link(out, v.url)
        return
    out.console.print(f"  {_verdict_text(a.verdict, a.confidence)} [dim]· quick check[/dim]")
    _print_text(out, a.rationale, style="dim")
    if v is not None and v.status == "failed":
        hint = (v.failure.hint if v.failure else None) or "The deep check failed."
        out.console.print(f"  [red]Deep check failed:[/red] {escape(hint)}")
    elif c.escalation is not None and c.escalation.disposition in ("cap", "credits", "account_cap"):
        out.console.print(f"  [dim]Not deep-checked ({escape(c.escalation.disposition)}).[/dim]")


def _render_issue(out: Output, i: Any, n: int) -> None:
    out.console.print(f"[bold]\\[{i.claim_index + 1}/{n}][/bold] {escape(i.claim or '')}")
    kind = "deep check" if i.source == "verification" else "quick check"
    out.console.print(f"  {_verdict_text(i.verdict, i.confidence)} [dim]· {kind}[/dim]")
    if i.source == "verification":
        _print_text(out, i.key_finding)
    else:
        _print_text(out, i.rationale, style="dim")
    if i.suggested_rewrite:
        out.console.print(f"  [dim]Suggested rewrite:[/dim] {escape(i.suggested_rewrite)}")
    if i.failure is not None:
        out.console.print(
            f"  [red]Deep check failed:[/red] {escape(i.failure.hint or i.failure.failure_reason or 'failed')}"
        )
    elif i.source != "verification" and i.escalation is not None and i.escalation.disposition != "planned":
        out.console.print(f"  [dim]Not deep-checked ({escape(i.escalation.disposition)}).[/dim]")
    if i.url:
        _print_link(out, i.url)


def render_review(out: Output, review: ReviewFull, *, issues_only: bool = False) -> None:
    """The final result on stdout: a summary line, then one block per claim
    (or per issue with ``--issues``): claim, verdict, the key finding (deep)
    or the reviewers' note (quick), and the suggested rewrite."""
    out.console.print(_summary_line(review))
    if review.failure is not None:
        hint = review.failure.hint or review.failure.failure_reason or "no reason given"
        out.console.print(f"[red]Failed:[/red] {escape(hint)}")
    n = review.summary.claims_selected or len(review.claims)
    if issues_only:
        for i in review.issues:
            out.console.print()
            _render_issue(out, i, n)
        for f in review.failures:
            out.console.print()
            out.console.print(f"[bold]\\[{f.claim_index + 1}/{n}][/bold] {escape(f.claim or '')}")
            hint = (f.failure.hint if f.failure else None) or "failed"
            out.console.print(f"  [red]{escape(f.stage.capitalize() or 'Check')} failed:[/red] {escape(hint)}")
        uncertain = _low_confidence_claims(review)
        if uncertain:
            out.console.print("\n[bold]Low confidence[/bold] [dim](not an issue, but worth a second look)[/dim]")
            for c in uncertain:
                out.console.print()
                _render_claim(out, c, n)
        if (
            not review.issues
            and not review.failures
            and not review.citation_issues
            and not review.citation_failures
            and review.failure is None
        ):
            out.console.print("\n[green]No issues.[/green]")
        _rewrite_note(out, review)
        render_citations(out, review)
        _more_note(out, review)
        return
    for c in review.claims:
        out.console.print()
        _render_claim(out, c, n)
    _rewrite_note(out, review)
    render_citations(out, review)
    _more_note(out, review)


def _low_confidence_claims(review: ReviewFull) -> list[ReviewClaim]:
    """Claims whose final verdict is not an issue but carries low confidence."""
    return [c for c in review.claims if c.result is not None and not c.result.is_issue and c.result.confidence == "low"]


def _rewrite_note(out: Output, review: ReviewFull) -> None:
    if any(i.suggested_rewrite for i in review.issues):
        out.console.print(
            "\n[dim]A suggested rewrite has not been verified itself: review it, or run it through "
            "`lenz verify`, before you use it.[/dim]"
        )


# ── citations (--citations) ─────────────────────────────────────────────────

#: The issue findings, most serious first, with the words each issue uses for
#: them.
CITATION_FINDING_WORDS = {
    "doi_not_found": "DOI not registered",
    "page_not_found": "page not found",
    "contradicted": "contradicted",
    "quote_not_in_source": "quote not in the source",
    "not_in_source": "not in the source",
    "partly_supported": "partly supported",
    "metadata_mismatch": "reference details differ",
}


def _sources(n: int) -> str:
    return f"{n} {'source' if n == 1 else 'sources'}"


#: A body that carries citation rows: a review (either view) or a citation check.
#: ``Union``, not ``|``: this alias is evaluated at import, and Python 3.9 has
#: no ``|`` between classes.
CitedBody = Union[ReviewFull, ReviewIssues, Citecheck]


#: Why a source could not be checked, when the server sent no hint.
_UNCHECKED_WORDS = {
    "no_text": "The page gave no text to read.",
    "partial_text": "Only part of the page could be read, so a missing passage proves nothing.",
    "login_required": "The page needs a login.",
    "unsupported_site": "Lenz does not read this site.",
    "no_statement": "No sentence in the draft rests on this source.",
    "other_version": "Only a preprint could be read, and its wording may differ from the published paper.",
    "inconclusive": "The check could not decide from this page.",
    "ambiguous_reference": "The DOI could not be read off the reference with certainty.",
    "invalid_url": "This is not a public web address.",
}


def citation_count_lines(review: CitedBody) -> list[str]:
    """The count heading and the line under it, as plain text; ``[]`` when
    the review checks no citation (``policy.max_citations`` is 0) or
    ``citations_skipped`` is ``switched_off``.

    While checks run: the heading, then "Lenz checks…". Once every row has
    ended: the heading, then the key numbers, then (over the cap) how many
    more were not checked. A citation check always checks its citations."""
    s = review.summary
    skipped = getattr(s, "citations_skipped", None)
    if skipped == "switched_off":
        return []
    if skipped == "url_input":
        return ["Sources on a linked page are not checked. Paste the page's text instead to check them."]
    if skipped == "insufficient_credits":
        return ["The sources were not checked: not enough credits."]
    if skipped:
        return ["The sources were not checked."]
    is_check = isinstance(review, Citecheck)
    if not is_check and not review.policy.max_citations:
        return []
    found = s.citations_found
    if found is None:
        return []
    if found == 0:
        return ["No links found in the draft, so no sources were checked."]
    heading = f"{_sources(found)} cited in your draft"
    selected = s.citations_selected or 0
    other = max(0, found - selected)
    counts = s.citation_checks
    ended = counts is not None and counts.checked + counts.unchecked + counts.failed >= selected
    if not ended or counts is None:
        if other:
            verb = "is" if other == 1 else "are"
            return [
                heading,
                f"Lenz checks the first {selected}: does the source say what your draft says it does. "
                f"The other {other} {verb} not checked; one check covers at most {selected}.",
            ]
        return [heading, "Lenz checks each one: does the source say what your draft says it does."]
    lines = [heading, citation_summary_line(review)]
    if other:
        verb = "was" if other == 1 else "were"
        lines.append(f"{other} more {verb} not checked: one check covers {selected}.")
    return lines


def citation_summary_line(review: CitedBody) -> str:
    """ "8 checked, 7 with a problem. 2 could not be checked."

    The key numbers only: citations checked, those with a problem (the
    citation issues) and those that could not be checked (for a reason of the
    page, the draft or ours). Each part only when it is not zero."""
    s = review.summary
    counts = s.citation_checks
    checked = counts.checked if counts is not None else 0
    not_checked = (counts.unchecked + counts.failed) if counts is not None else 0
    problems = s.citation_issues or len(review.citation_issues)
    first = [f"{checked} checked"] if checked else []
    if problems:
        first.append(f"{problems} with a problem")
    parts = [", ".join(first) + "."] if first else []
    if not_checked:
        parts.append(f"{not_checked} could not be checked.")
    return " ".join(parts) or "0 checked."


def _finding_words(finding: str) -> str:
    words = CITATION_FINDING_WORDS.get(finding, finding.replace("_", " "))
    return words[:1].upper() + words[1:]


def _render_citation_issue(out: Output, i: ReviewCitationIssue, total: int) -> None:
    """Finding, the draft's sentence, the link, then the evidence."""
    label = f"source {i.citation_index + 1}/{total}" if total else f"source {i.citation_index + 1}"
    out.console.print(f"[bold]\\[{label}][/bold] [red]{escape(_finding_words(i.finding))}[/red]")
    _print_text(out, i.statement)
    link = i.cited_url or (f"doi:{i.doi}" if i.doi else None)
    if link:
        _print_link(out, link)
    elif i.reference:
        _print_text(out, i.reference, style="dim")
    if i.source == "support":
        # "Not in the source" carries no passage, only the reason: print the
        # reason whenever there is one.
        if i.snippet:
            out.console.print("  [dim]The source says:[/dim]")
            _print_text(out, f"\u201c{i.snippet}\u201d")
        if i.rationale:
            _print_text(out, f"Reviewer's note: {i.rationale}", style="dim")
    elif i.source == "quote" and (i.missing_quote or i.quotes):
        out.console.print("  [dim]These quoted words were not found in the source:[/dim]")
        # The excerpt the check did not find; an older body names none, so
        # every quote is shown.
        for q in [i.missing_quote] if i.missing_quote else i.quotes:
            _print_text(out, f"\u201c{q}\u201d")
    elif i.source == "metadata":
        for d in i.metadata_differences:
            _print_text(out, f"Your reference says {d.cited or '(none)'}; the record says {d.registered or '(none)'}.")
    if i.failure is not None:
        hint = i.failure.hint or i.failure.failure_reason or "failed"
        out.console.print(f"  [yellow]Part of the check failed:[/yellow] {escape(hint)}")


def render_citations(out: Output, review: CitedBody) -> None:
    """After the claims: the count line and the summary line, then the
    citation issues, then the citations that could not be checked this time."""
    lines = citation_count_lines(review)
    if not lines:
        return
    out.console.print()
    out.console.print(f"[bold]{escape(lines[0])}[/bold]")
    for line in lines[1:]:
        out.console.print(escape(line))
    total = review.summary.citations_selected or 0
    for i in review.citation_issues:
        out.console.print()
        _render_citation_issue(out, i, total)
    for f in review.citation_failures:
        out.console.print()
        label = f"source {f.citation_index + 1}/{total}" if total else f"source {f.citation_index + 1}"
        link = f.cited_url or (f"doi:{f.doi}" if f.doi else f.reference or "")
        out.console.print(f"[bold]\\[{label}][/bold] [yellow]Could not be checked this time.[/yellow]")
        if link:
            _print_link(out, link)
        hint = f.failure.hint if f.failure else None
        if hint:
            _print_text(out, hint, style="dim")
    unchecked = [
        c for c in getattr(review, "citations", []) if c.result is not None and c.result.finding == "unchecked"
    ]
    if unchecked:
        out.console.print("\n[bold]Check these by hand[/bold] [dim](Lenz could not check them)[/dim]")
        for c in unchecked:
            label = f"source {c.index + 1}/{total}" if total else f"source {c.index + 1}"
            link = c.cited_url or (f"doi:{c.doi}" if c.doi else c.reference or "")
            reason = c.check.hint or _UNCHECKED_WORDS.get(c.check.unchecked_reason or "", "It could not be checked.")
            out.console.print(f"[bold]\\[{label}][/bold] {escape(reason)}")
            if link:
                _print_link(out, link)
    if review.citation_issues:
        out.console.print(
            "\n[dim]A reviewer's note is reasoning, not a checked source; the quoted passage is from the page.[/dim]"
        )


def more_found_line(review: CitedBody) -> str:
    """ "2 more claims and 13 more citations were found but not checked.";
    ``""`` when there are none (or the draft has not been read)."""
    claims = len(getattr(review, "more_claims", None) or [])
    citations = len(review.more_citations or [])
    parts = []
    if claims:
        parts.append(f"{claims} more {'claim' if claims == 1 else 'claims'}")
    if citations:
        parts.append(f"{citations} more {'citation' if citations == 1 else 'citations'}")
    if not parts:
        return ""
    verb = "was" if claims + citations == 1 else "were"
    return f"{' and '.join(parts)} {verb} found but not checked."


def _more_note(out: Output, review: ReviewFull | ReviewIssues) -> None:
    line = more_found_line(review)
    if line:
        out.console.print(f"\n[dim]{escape(line)}[/dim]")
