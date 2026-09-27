"""``lenz citecheck`` — check a draft's citations, or statement-source pairs,
without the rest of a review: does each cited source say what the draft says
it does?

``lenz citecheck draft.md`` reads the draft's links (keep them as markdown
links in the file); ``lenz citecheck --pairs pairs.json`` sends a JSON list of
``{"statement": ..., "url": ...}`` (or ``"doi"``) objects, each checked as it
is. The count, the key numbers and each source issue print when it ends;
``--json`` (or a non-terminal stdout) prints the check's body instead.

The exit code is the check's ``outcome``, as for ``lenz review``::

    0  clean          every selected citation checked, no issue
    1  issues_found   a source does not say what the draft says it does
    2  anything else  incomplete, unchecked, failed, timed out, or an error

``--detach`` submits and prints the ``citecheck_id``;
``lenz citecheck --resume <id>`` picks it up again.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape

from lenz_io import Lenz
from lenz_io.errors import CitecheckFailed, CitecheckTimeout, LenzError
from lenz_io.models import Citecheck

from ._run import execute, read_text_arg
from .context import CLIState
from .errors import CLIError
from .render import Output
from .review import _OUTCOME_LABEL, EXIT_OTHER, _count, more_found_line, render_citations

_EXIT_BY_OUTCOME = {"clean": 0, "issues_found": 1}
_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)
URL_INPUT_HINT = "Send the page's text, with its links, to check the sources it cites."


def citecheck(
    ctx: typer.Context,
    draft: str = typer.Argument(None, help="The draft: a file path, or '-' (or a pipe) for stdin."),
    pairs: str = typer.Option(
        None, "--pairs", metavar="FILE.json", help="Check statement-source pairs from a JSON file instead."
    ),
    max_citations: int = typer.Option(
        None,
        "--max-citations",
        metavar="N",
        min=1,
        max=20,
        help="With a draft: check its first N sources (1-20, default 20).",
    ),
    detach: bool = typer.Option(False, "--detach", help="Submit and exit; print the citecheck_id to resume."),
    resume: str = typer.Option(None, "--resume", metavar="CITECHECK_ID", help="Pick up a check started earlier."),
    timeout: float = typer.Option(600.0, "--timeout", help="Max seconds to wait."),
) -> None:
    """Check a draft's sources: does each link say what the draft says? Exit code: 0 clean, 1 issues, 2 other."""
    state: CLIState = ctx.obj
    out = state.output

    def work(client: Lenz) -> None:
        try:
            _work(client)
        except KeyboardInterrupt:
            raise SystemExit(130) from None

    def _work(client: Lenz) -> None:
        if resume:
            if draft or pairs:
                raise CLIError("Pass either a draft, --pairs or --resume.", code="invalid_usage", exit_code=2)
            citecheck_id = resume
        else:
            if draft and pairs:
                raise CLIError("Pass either a draft or --pairs, not both.", code="invalid_usage", exit_code=2)
            if pairs:
                if max_citations is not None:
                    raise CLIError(
                        "--max-citations goes with a draft: every pair is checked.",
                        code="invalid_usage",
                        exit_code=2,
                    )
                started = client.citecheck(pairs=_read_pairs(pairs))
            else:
                started = client.citecheck(_read_draft(draft), max_citations=max_citations)
            citecheck_id = started.citecheck_id
            if detach:
                _emit_detached(out, citecheck_id)
                return
        try:
            final = _wait(client, out, citecheck_id, timeout)
        except LenzError as exc:
            exc.fix = f"{exc.fix} Resume with: lenz citecheck --resume {citecheck_id}".strip()
            raise
        if out.json_mode:
            out.emit_json(final.model_dump(mode="json"))
        else:
            render_citecheck(out, final)
        raise SystemExit(exit_code_for_citecheck(final))

    execute(state, needs_key=True, work=work, error_exit=EXIT_OTHER)


def exit_code_for_citecheck(check: Citecheck) -> int:
    """0 clean, 1 issues_found, 2 for every other outcome (or none)."""
    return _EXIT_BY_OUTCOME.get(check.outcome or "", EXIT_OTHER)


def _read_draft(arg: str | None) -> str:
    """A file path or ``-`` / a pipe for stdin. One URL is refused here, as
    the API refuses it: a linked page's own links cannot be read."""
    if arg is not None and _URL.match(arg.strip()):
        raise CLIError(URL_INPUT_HINT, code="url_input", exit_code=2)
    if arg is None or arg == "-":
        text = read_text_arg(arg)
    else:
        path = Path(arg)
        if not path.is_file():
            raise CLIError(
                f"No such file: {arg}",
                code="no_input",
                fix="Pass a file, '-' to read stdin, or --pairs FILE.json.",
                exit_code=2,
            )
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise CLIError(f"{arg} is not UTF-8 text.", code="no_input", exit_code=2) from None
        if not text.strip():
            raise CLIError(f"{arg} is empty.", code="no_input", exit_code=2)
    if _URL.match(text.strip()):
        raise CLIError(URL_INPUT_HINT, code="url_input", exit_code=2)
    return text


def _read_pairs(arg: str) -> list[Any]:
    """A JSON list of pair objects, or ``{"pairs": [...]}``."""
    path = Path(arg)
    if arg != "-" and not path.is_file():
        raise CLIError(f"No such file: {arg}", code="no_input", exit_code=2)
    raw = sys.stdin.read() if arg == "-" else path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except ValueError:
        raise CLIError(f"{arg} is not JSON.", code="no_input", exit_code=2) from None
    if isinstance(data, dict):
        data = data.get("pairs")
    if not isinstance(data, list) or not data or not all(isinstance(p, dict) for p in data):
        raise CLIError(
            f"{arg} must hold a list of pairs.",
            code="no_input",
            fix='Each pair is {"statement": "...", "url": "https://..."} or {"statement": "...", "doi": "10..."}.',
            exit_code=2,
        )
    return data


def _emit_detached(out: Output, citecheck_id: str) -> None:
    if out.json_mode:
        out.emit_json({"status": "submitted", "citecheck_id": citecheck_id})
    else:
        out.console.print(f"Citation check started ({citecheck_id}).")
        out.console.print(f"[dim]Read it with:[/dim] lenz citecheck --resume {citecheck_id}")


def _wait(client: Lenz, out: Output, citecheck_id: str, timeout: float) -> Citecheck:
    """Poll to the end, with a progress line on a terminal. A failed check
    returns its body (the caller renders it and exits 2)."""
    live_ok = not out.json_mode and sys.stderr.isatty()
    try:
        if live_ok:
            from rich.live import Live

            with Live(render_progress(None), console=out.err, refresh_per_second=8, transient=True) as live:
                return _wait_quiet(client, citecheck_id, timeout, on_update=lambda c: live.update(render_progress(c)))
        return _wait_quiet(client, citecheck_id, timeout, on_update=None)
    except KeyboardInterrupt:
        if out.json_mode:
            out.emit_json({"status": "interrupted", "citecheck_id": citecheck_id})
        else:
            out.err.print(
                f"\n[yellow]Still running server-side.[/yellow] Resume with:\n  lenz citecheck --resume {citecheck_id}"
            )
        raise SystemExit(130) from None
    except CitecheckTimeout:
        raise CLIError(
            f"Timed out after {timeout:g}s; the check keeps running server-side.",
            code="timeout",
            fix=f"lenz citecheck --resume {citecheck_id}",
            exit_code=EXIT_OTHER,
        ) from None


def _wait_quiet(client: Lenz, citecheck_id: str, timeout: float, *, on_update: Any) -> Citecheck:
    try:
        return client._wait_citecheck(citecheck_id, timeout=timeout, on_update=on_update)
    except CitecheckFailed as exc:
        if exc.citecheck is None:
            raise
        return exc.citecheck


def render_progress(check: Citecheck | None) -> Any:
    """The live stderr line: "Checking sources… 3 of 10"."""
    from rich.spinner import Spinner
    from rich.text import Text

    if check is None or not check.summary.citations_selected:
        return Spinner("dots", text=Text("Reading the draft…", style="dim"))
    s = check.summary
    counts = s.citation_checks
    done = (counts.checked + counts.unchecked + counts.failed) if counts is not None else 0
    return Spinner("dots", text=Text(f"Checking sources… {done} of {s.citations_selected}", style="dim"))


def render_citecheck(out: Output, check: Citecheck) -> None:
    """The final result on stdout: one line with the outcome, then the count,
    the key numbers, each source issue, and what was found but not checked."""
    outcome = _OUTCOME_LABEL.get(check.outcome or "", escape(check.outcome or check.status or "?"))
    out.console.print(
        f"Citation check {escape(check.citecheck_id)}: {outcome} — {_count(check.credits.charged, 'credit')} charged"
    )
    if check.failure is not None:
        hint = check.failure.hint or check.failure.failure_reason or "no reason given"
        out.console.print(f"[red]Failed:[/red] {escape(hint)}")
    render_citations(out, check)
    if check.status == "completed" and not check.citation_issues and not check.citation_failures:
        out.console.print("\n[green]No issues.[/green]")
    line = more_found_line(check)
    if line:
        out.console.print(f"\n[dim]{escape(line)}[/dim]")
