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
    1  issues_found   at least one claim is False, Mostly False or Mixed
    2  anything else  incomplete, unchecked, failed, timed out, or an error
                      (a bad key, no credits, a bad flag)

``--detach`` submits and prints the ``review_id``; ``lenz review --resume <id>``
picks it up again, as does Ctrl-C's hint.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape

from lenz_io import Lenz
from lenz_io.errors import LenzError, ReviewFailed, ReviewTimeout
from lenz_io.models import ReviewClaim, ReviewFull

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
    max_verifications: int = typer.Option(
        None, "--max-verifications", metavar="N", help="Deep-check at most N claims (default 5; 0 = quick checks only)."
    ),
    depth: str = typer.Option(
        None, "--depth", metavar="standard|low", help="Depth of every deep check. 'low' costs half the credits."
    ),
    detach: bool = typer.Option(False, "--detach", help="Submit and exit; print the review_id to resume."),
    resume: str = typer.Option(None, "--resume", metavar="REVIEW_ID", help="Pick up a review started earlier."),
    timeout: float = typer.Option(600.0, "--timeout", help="Max seconds to wait."),
) -> None:
    """Review a whole draft (2-4 min). Exit code: 0 clean, 1 issues found, 2 incomplete or failed.

    Costs 1 credit per claim checked, plus 10 per deep check (5 at --depth low).
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
            started = client.review(text, max_verifications=max_verifications, depth=chosen_depth)
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

    if review is None or not review.claims:
        return Spinner("dots", text=Text("Reading the draft…", style="dim"))
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="right", style="dim")
    table.add_column(min_width=26)
    table.add_column()
    n = len(review.claims)
    for c in review.claims:
        table.add_row(f"[{c.index + 1}/{n}]", _progress_cell(c), Text(_truncate(c.claim or "")))
    return table


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


def _render_claim(out: Output, c: ReviewClaim, n: int) -> None:
    out.console.print(f"[bold]\\[{c.index + 1}/{n}][/bold] {escape(c.claim or '')}")
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
            out.console.print(f"  [blue]{escape(v.url)}[/blue]")
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
        out.console.print(f"  [blue]{escape(i.url)}[/blue]")


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
        if not review.issues and not review.failures and review.failure is None:
            out.console.print("\n[green]No issues.[/green]")
        _rewrite_note(out, review)
        return
    for c in review.claims:
        out.console.print()
        _render_claim(out, c, n)
    _rewrite_note(out, review)


def _rewrite_note(out: Output, review: ReviewFull) -> None:
    if any(i.suggested_rewrite for i in review.issues):
        out.console.print(
            "\n[dim]A suggested rewrite has not been verified itself: review it, or run it through "
            "`lenz verify`, before you use it.[/dim]"
        )
