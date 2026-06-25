"""``lenz verify`` — submit a claim, poll to a verdict, and handle the
interactive branches the status endpoint can return.

The lifecycle is NOT just poll→verdict. ``GET /verify/status/{task_id}`` can
return ``needs_input`` (``multi_claim`` / ``clarification_required`` /
``duplicate_found``); ignoring those would hang or misrender. Ctrl-C prints a
``--resume <task_id>`` handle so an in-flight ~90s run isn't lost. The task
handle is server-cached only ~10 min, so ``--resume`` also accepts a durable
``verification_id`` (falls back to ``verifications.get``).

Lifecycle::

    verify(claim) ──► task_id ──► poll get_status ─┬─ processing ─► (loop)
                                                    ├─ needs_input ─► select ─► new task_id ─► (loop)
                                                    ├─ completed ──► render verdict
                                                    └─ failed ─────► friendly error
"""

from __future__ import annotations

import contextlib
import sys
import time
import uuid
from typing import Any

import typer

from lenz_io import Lenz
from lenz_io.errors import LenzError
from lenz_io.models import TaskStatus

from ._run import execute, read_text_arg
from .context import CLIState
from .errors import CLIError
from .render import Output, render_verification

POLL_INTERVAL = 2.5  # seconds — never sub-second; the pipeline runs ~90s.

# Friendlier spinner copy for the raw pipeline step names the status endpoint
# reports (a mix of bare 'research' and decorated 'Framing...'). Unknown steps
# fall back to a cleaned-up version of whatever the server sent.
_STEP_LABELS = {
    "starting": "Starting",
    "framing": "Framing the claim",
    "research": "Gathering evidence",
    "researcher": "Gathering evidence",
    "debate": "Weighing both sides",
    "adjudication": "Adjudicating across models",
    "conclusion": "Writing the verdict",
}


def _step_label(step: str | None) -> str:
    if not step:
        return "Verifying… (~90s)"
    key = step.strip().rstrip(".").strip().lower()
    pretty = _STEP_LABELS.get(key) or key.replace("_", " ").capitalize() or "Working"
    return f"Verifying… {pretty}"


def verify(
    ctx: typer.Context,
    claim: str = typer.Argument(None, help="Claim to verify ('-' or pipe = stdin)."),
    resume: str = typer.Option(None, "--resume", metavar="ID", help="Re-attach to a task_id or verification_id."),
    timeout: float = typer.Option(180.0, "--timeout", help="Max seconds to wait."),
    pick: int = typer.Option(None, "--claim", help="Pre-pick the Nth claim (1-based) on a multi-claim input."),
    detach: bool = typer.Option(False, "--detach", help="Submit and exit immediately; print the re-attach command."),
) -> None:
    """Full fact-check pipeline (~90s). Needs a key; spends a credit on a fresh claim."""
    state: CLIState = ctx.obj
    out = state.output
    preselect = (pick - 1) if pick is not None else None

    def work(client: Lenz) -> None:
        if resume:
            _resume(client, out, resume, timeout, preselect=preselect)
            return
        text = read_text_arg(claim)
        accepted = client.verify(text, idempotency_key=uuid.uuid4().hex)
        if detach:
            _emit_detached(out, accepted.task_id)
            return
        _poll(client, out, accepted.task_id, timeout, preselect=preselect)

    execute(state, needs_key=True, work=work)


def _emit_detached(out: Output, task_id: str) -> None:
    """Print the handle for a fire-and-forget submit (``--detach``)."""
    if out.json_mode:
        out.emit_json({"status": "submitted", "task_id": task_id})
    else:
        out.console.print(f"Verification started (task {task_id}).")
        out.console.print(f"[dim]Read the verdict with:[/dim] lenz verify --resume {task_id}")


def _poll(client: Lenz, out: Output, task_id: str, timeout: float, *, preselect: int | None = None) -> None:
    deadline = time.monotonic() + timeout
    # Shared across needs_input loops so the "Ctrl-C to detach" hint prints at
    # most once for the whole verify (not before each picker, not twice).
    hint = {"shown": False}
    while True:
        st = _wait_until_actionable(client, out, task_id, deadline, hint)
        if st.status == "completed":
            render_verification(out, st.result)
            return
        if st.status == "failed":
            raise CLIError(st.error or st.failure_detail or "Verification failed.", code="pipeline_failed")
        if st.status == "needs_input":
            task_id = _needs_input(client, out, task_id, st, preselect)
            preselect = None
            # A pick spawns a fresh ~90s pipeline. Reset the clock so the new
            # run gets the full --timeout budget, not what's left after the
            # (possibly slow, interactive) selection.
            deadline = time.monotonic() + timeout
            continue
        raise CLIError(f"Unexpected status {st.status!r}.", code="unexpected_status")


def _wait_until_actionable(
    client: Lenz, out: Output, task_id: str, deadline: float, hint: dict[str, bool]
) -> TaskStatus:
    """Poll until the task is completed/failed/needs_input (i.e. not processing)."""
    show_spinner = not out.json_mode and sys.stderr.isatty()
    spinner = out.err.status(_step_label(None)) if show_spinner else None
    cm = spinner if spinner is not None else contextlib.nullcontext()
    try:
        with cm:
            while True:
                if time.monotonic() > deadline:
                    raise CLIError(
                        "Timed out; the verification may still finish server-side.",
                        code="timeout",
                        fix=f"lenz verify --resume {task_id}",
                    )
                st = client.get_status(task_id)
                if st.status != "processing":
                    return st
                # Only hint once we're actually waiting on the pipeline — not
                # before a picker, where there's nothing to detach from yet.
                if spinner is not None and not hint["shown"]:
                    out.err.print("[dim]Ctrl-C to detach — the verification keeps running.[/dim]")
                    hint["shown"] = True
                if spinner is not None:
                    step = (st.progress or {}).get("step")
                    spinner.update(_step_label(step))
                time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        out.resume_hint(task_id)
        raise SystemExit(130) from None


def _needs_input(client: Lenz, out: Output, task_id: str, st: TaskStatus, preselect: int | None) -> str:
    """Resolve a needs_input pause; returns the new task_id to keep polling.

    ``--claim`` (preselect) resolves multi/clarification non-interactively even
    in ``--json`` mode. Otherwise json mode emits the object and exits (never
    hangs waiting on a prompt that can't happen); TTY mode prompts.
    """
    reason = st.reason

    if reason in ("multi_claim", "clarification_required"):
        options = [c.text for c in st.claims] if reason == "multi_claim" else list(st.candidates)
        if preselect is not None:
            idx = preselect
        elif out.json_mode:
            _emit_needs_input(out, task_id, st)
            raise SystemExit(3)
        else:
            label = "Multiple claims found — pick one:" if reason == "multi_claim" else "Ambiguous — pick a reading:"
            idx = _choose(out, label, options)
        _validate_index(idx, options)
        # Select by text. /select fans out one pipeline per chosen claim and
        # returns a batch; the CLI picker chooses exactly one, so read the
        # single spawned task off items[0]. ``options[idx]`` is the chosen
        # claim's wording for both multi_claim and clarification.
        return client.select(task_id, texts=[options[idx]]).items[0].task_id

    if reason == "duplicate_found":
        if out.json_mode:
            _emit_needs_input(out, task_id, st)
            raise SystemExit(3)
        _render_similar(out, st.similar_claims)
        raise SystemExit(0)

    raise CLIError(f"Unexpected needs_input reason: {reason!r}.", code="needs_input")


def _emit_needs_input(out: Output, task_id: str, st: TaskStatus) -> None:
    out.emit_json(
        {
            "status": "needs_input",
            "reason": st.reason,
            "task_id": task_id,
            "claims": [c.model_dump(mode="json") for c in st.claims],
            "candidates": list(st.candidates),
            "similar": [s.model_dump(mode="json") for s in st.similar_claims],
        }
    )


def _choose(out: Output, prompt: str, options: list[str]) -> int:
    out.err.print(f"[bold]{prompt}[/bold]")
    for i, option in enumerate(options, 1):
        out.err.print(f"  {i}. {option}")
    return int(typer.prompt("Number", type=int)) - 1


def _validate_index(idx: int | None, options: list[str]) -> None:
    if idx is None or idx < 0 or idx >= len(options):
        raise CLIError(f"Selection out of range (1-{len(options)}).", code="invalid_selection", exit_code=2)


def _render_similar(out: Output, similar: list[Any]) -> None:
    out.console.print("[yellow]This claim was already verified:[/yellow]")
    for s in similar[:5]:
        score = "" if s.lenz_score is None else f" (score {s.lenz_score}/10)"
        out.console.print(f"  • [bold]{s.verdict or '?'}[/bold]{score}  [dim]id: {s.verification_id}[/dim]")


def _resume(client: Lenz, out: Output, ident: str, timeout: float, *, preselect: int | None = None) -> None:
    try:
        st = client.get_status(ident)
    except LenzError as exc:
        if exc.status_code == 404:
            _resume_as_verification(client, out, ident)
            return
        raise
    if st.status == "completed":
        render_verification(out, st.result)
    elif st.status == "failed":
        raise CLIError(st.error or "Verification failed.", code="pipeline_failed")
    else:  # processing / needs_input → keep polling from here, honoring --claim
        _poll(client, out, ident, timeout, preselect=preselect)


def _resume_as_verification(client: Lenz, out: Output, ident: str) -> None:
    try:
        verification = client.verifications.get(ident)
    except LenzError as exc:
        if exc.status_code == 404:
            raise CLIError(
                f"No live task or verification found for {ident!r}. Tasks expire ~10 min after "
                "completion — pass the verification_id instead.",
                code="not_found",
            ) from None
        raise
    render_verification(out, verification)
