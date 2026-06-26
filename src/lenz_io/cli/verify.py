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
from .render import Output, render_batch_details, render_batch_table, render_verification

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
    pick: str = typer.Option(
        None, "--claim", metavar="N|N,M|all", help="Pre-pick claim(s) on a multi-claim input: '2', '1,3', or 'all'."
    ),
    detach: bool = typer.Option(False, "--detach", help="Submit and exit immediately; print the re-attach command."),
) -> None:
    """Full fact-check pipeline (~90s). Needs a key; spends a credit on a fresh claim."""
    state: CLIState = ctx.obj
    out = state.output
    selection = _parse_claim_selection(pick)

    def work(client: Lenz) -> None:
        if resume:
            _resume(client, out, resume, timeout, selection=selection, detach=detach)
            return
        text = read_text_arg(claim)
        accepted = client.verify(text, idempotency_key=uuid.uuid4().hex)
        # --detach alone fires off the submitted task immediately. With --claim
        # it instead flows through the multi-claim picker and detaches the
        # selected pipelines (so `--claim 1,3 --detach` submits exactly those).
        if detach and selection is None:
            _emit_detached(out, accepted.task_id)
            return
        _poll(client, out, accepted.task_id, timeout, selection=selection, detach=detach)

    execute(state, needs_key=True, work=work)


def _parse_claim_selection(raw: str | None) -> list[int] | str | None:
    """Parse ``--claim``: ``None`` (unset), ``"all"``, or 0-based indices.

    Accepts ``"2"``, ``"1,3"``, ``"1, 3"``, or ``"all"`` (case-insensitive).
    """
    if raw is None:
        return None
    cleaned = raw.strip().lower()
    if cleaned == "all":
        return "all"
    try:
        nums = [int(x) for x in cleaned.replace(" ", "").split(",") if x]
    except ValueError:
        nums = []
    if not nums:
        raise CLIError(
            f"--claim expects numbers like '1,3' or 'all' (got {raw!r}).",
            code="invalid_selection",
            exit_code=2,
        )
    # 1-based input → 0-based indices, order-preserving dedup (a typo like
    # '1,1,2' must not spawn the same claim's pipeline twice).
    return list(dict.fromkeys(n - 1 for n in nums))


def _emit_detached(out: Output, task_id: str) -> None:
    """Print the handle for a fire-and-forget submit (``--detach``)."""
    if out.json_mode:
        out.emit_json({"status": "submitted", "task_id": task_id})
    else:
        out.console.print(f"Verification started (task {task_id}).")
        out.console.print(f"[dim]Read the verdict with:[/dim] lenz verify --resume {task_id}")


def _poll(
    client: Lenz,
    out: Output,
    task_id: str,
    timeout: float,
    *,
    selection: list[int] | str | None = None,
    detach: bool = False,
) -> None:
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
            if st.reason == "multi_claim":
                texts = _resolve_multi_claim(out, task_id, st, selection)
                if not texts:  # cancelled / nothing picked
                    raise SystemExit(0)
                items = client.select(task_id, texts=texts).items
                picks = [(it.task_id, it.claim_text or txt) for it, txt in zip(items, texts)]
                # detach, or >1 claim → batch path; exactly one → keep the
                # single-verdict flow (nicer than a 1-row table).
                if detach or len(picks) > 1:
                    _verify_batch(client, out, picks, timeout, detach=detach)
                    return
                task_id = picks[0][0]
            else:  # clarification_required / duplicate_found → single-pick / terminal
                task_id = _needs_input_single(client, out, task_id, st, selection)
            selection = None
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


def _resolve_multi_claim(out: Output, task_id: str, st: TaskStatus, selection: list[int] | str | None) -> list[str]:
    """Return the claim texts to verify on a ``multi_claim`` pause (may be many).

    ``--claim`` (``selection``) resolves non-interactively, even in ``--json``.
    Otherwise json mode emits the needs_input object and exits 3 (never hangs on
    a prompt that can't happen); a TTY shows the checkbox picker.
    """
    options = [c.text for c in st.claims]
    if selection is not None:
        return [options[i] for i in _selection_to_indices(selection, len(options))]
    if out.json_mode:
        _emit_needs_input(out, task_id, st)
        raise SystemExit(3)
    return _checkbox_picker("Select claims to verify:", options)


def _selection_to_indices(selection: list[int] | str, n: int) -> list[int]:
    if selection == "all":
        return list(range(n))
    assert isinstance(selection, list)
    out_of_range = [i + 1 for i in selection if i < 0 or i >= n]
    if out_of_range:
        raise CLIError(f"--claim out of range: {out_of_range} (valid 1-{n}).", code="invalid_selection", exit_code=2)
    return selection


def _checkbox_picker(message: str, options: list[str]) -> list[str]:
    """Interactive multi-select. Returns the chosen texts ([] = cancel/none)."""
    import questionary

    # Explicit short instruction replaces questionary's verbose default
    # ("Use arrow keys… <a> to toggle all, <i> to invert…").
    picks = questionary.checkbox(message, choices=options, instruction="(space toggle, enter submit)").ask()
    return picks or []  # None on Ctrl-C, [] on empty submit → cancel


def _needs_input_single(
    client: Lenz, out: Output, task_id: str, st: TaskStatus, selection: list[int] | str | None
) -> str:
    """Resolve a single-pick pause (clarification) or terminal one (duplicate);
    returns the new task_id to keep polling."""
    reason = st.reason

    if reason == "clarification_required":
        options = list(st.candidates)
        if selection is not None:
            idx = _selection_to_indices(selection, len(options))[0]  # one reading
        elif out.json_mode:
            _emit_needs_input(out, task_id, st)
            raise SystemExit(3)
        else:
            idx = _choose(out, "Ambiguous — pick a reading:", options)
        _validate_index(idx, options)
        return client.select(task_id, texts=[options[idx]]).items[0].task_id

    if reason == "duplicate_found":
        if out.json_mode:
            _emit_needs_input(out, task_id, st)
            raise SystemExit(3)
        _render_similar(out, st.similar_claims)
        raise SystemExit(0)

    raise CLIError(f"Unexpected needs_input reason: {reason!r}.", code="needs_input")


def _verify_batch(client: Lenz, out: Output, picks: list[tuple[str, str]], timeout: float, *, detach: bool) -> None:
    """Verify N selected claims, each pipeline running independently.

    ``picks`` is ``[(task_id, claim_text), ...]``. With a TTY we show a live
    table where each row resolves on its own; ``--json`` emits an array; with
    ``--detach`` we just print the re-attach commands.
    """
    if detach:
        _emit_detached_batch(out, picks)
        return

    statuses: dict[str, TaskStatus | None] = {tid: None for tid, _ in picks}
    use_live = not out.json_mode and sys.stderr.isatty()
    if use_live:
        from rich.live import Live

        table = render_batch_table(picks, statuses)
        with Live(table, console=out.err, refresh_per_second=8) as live:
            _poll_all(
                client,
                out,
                picks,
                timeout,
                statuses,
                on_update=lambda: live.update(render_batch_table(picks, statuses)),
            )
    else:
        _poll_all(client, out, picks, timeout, statuses, on_update=None)

    if out.json_mode:
        out.emit_json([_batch_item_json(tid, text, statuses[tid]) for tid, text in picks])
    else:
        render_batch_details(out, picks, statuses)


def _poll_all(
    client: Lenz,
    out: Output,
    picks: list[tuple[str, str]],
    timeout: float,
    statuses: dict[str, TaskStatus | None],
    *,
    on_update: Any,
) -> None:
    """Poll every task concurrently until all reach a terminal state (or timeout).

    Mutates ``statuses`` in place so the live table reflects each task's own
    progress; tasks still pending at the deadline stay ``None`` (rendered as a
    timeout)."""
    deadline = time.monotonic() + timeout
    pending = {tid for tid, _ in picks}
    try:
        while pending and time.monotonic() <= deadline:
            for tid in list(pending):
                st = client.get_status(tid)
                statuses[tid] = st
                if st.status != "processing":
                    pending.discard(tid)
            if on_update:
                on_update()
            if pending:
                time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        out.err.print("\n[yellow]Detached — these keep running. Re-attach:[/yellow]")
        for tid in (t for t, _ in picks if t in pending):
            out.err.print(f"  lenz verify --resume {tid}")
        raise SystemExit(130) from None


def _batch_item_json(task_id: str, claim_text: str, st: TaskStatus | None) -> dict[str, Any]:
    if st is None:
        return {"task_id": task_id, "claim": claim_text, "status": "timeout"}
    if st.status == "completed" and st.result is not None:
        return {
            "task_id": task_id,
            "claim": claim_text,
            "status": "completed",
            "verification": st.result.model_dump(mode="json"),
        }
    if st.status == "failed":
        return {"task_id": task_id, "claim": claim_text, "status": "failed", "error": st.error or st.failure_detail}
    return {"task_id": task_id, "claim": claim_text, "status": st.status or "unknown"}


def _emit_detached_batch(out: Output, picks: list[tuple[str, str]]) -> None:
    if out.json_mode:
        out.emit_json([{"status": "submitted", "task_id": tid, "claim": text} for tid, text in picks])
        return
    out.console.print(f"Started {len(picks)} verification(s):")
    for tid, text in picks:
        out.console.print(f"  • {text}\n    [dim]lenz verify --resume {tid}[/dim]")


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


def _resume(
    client: Lenz,
    out: Output,
    ident: str,
    timeout: float,
    *,
    selection: list[int] | str | None = None,
    detach: bool = False,
) -> None:
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
    else:  # processing / needs_input → keep polling from here, honoring --claim/--detach
        _poll(client, out, ident, timeout, selection=selection, detach=detach)


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
