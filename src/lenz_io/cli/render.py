"""Output rendering — the single boundary between pretty (TTY) and machine
(``--json``) output.

``--json`` is forced on whenever stdout is not a TTY, so piping
(``lenz verify ... | jq``) always yields clean JSON even without the flag.
Progress/spinners go to **stderr** so ``--json`` stdout stays a single object.
"""

from __future__ import annotations

import contextlib
import json
import sys
from typing import Any

from rich.console import Console
from rich.markdown import Markdown

from lenz_io.models import (
    AssessResponse,
    ExtractedClaims,
    Usage,
    UsageCapacity,
    Verification,
)

_VERDICT_COLOR = {
    "True": "green",
    "Mostly True": "green",
    "Misleading": "yellow",
    "False": "red",
    "Error": "red",
}


def _model_json(model: Any) -> Any:
    """JSON-safe dump of a pydantic model (or passthrough for plain types)."""
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model


class Output:
    """Owns the stdout/stderr consoles and the json-vs-pretty decision."""

    def __init__(self, *, json_mode: bool, no_color: bool) -> None:
        self._stdout_tty = sys.stdout.isatty()
        # Non-tty stdout → JSON contract (documented), even without --json.
        self.json_mode = json_mode or not self._stdout_tty
        self.console = Console(no_color=no_color or not self._stdout_tty, highlight=False)
        # highlight=False: stop Rich from auto-coloring paths/numbers in error
        # text — it colors a path only up to the first space ("Application
        # Support" → half-colored), which looks broken. We add our own markup.
        self.err = Console(stderr=True, no_color=no_color, highlight=False)

    # ── primitives ──
    def emit_json(self, payload: Any) -> None:
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    def note(self, text: str) -> None:
        """Human-only aside to stderr (suppressed in json mode)."""
        if not self.json_mode:
            self.err.print(text)

    def working(self, label: str) -> contextlib.AbstractContextManager[Any]:
        """A spinner on stderr for blocking calls — TTY + human-mode only.

        No-op (a null context) under ``--json`` or when stderr isn't a TTY, so
        machine output and piped/CI runs stay clean. Used by every command that
        makes a blocking API call (extract/assess/ask) so the terminal never
        looks frozen; ``verify`` keeps its own step-aware spinner.
        """
        if self.json_mode or not sys.stderr.isatty():
            return contextlib.nullcontext()
        return self.err.status(label)

    def error(self, payload: dict[str, Any], text: str) -> None:
        if self.json_mode:
            self.emit_json(payload)
        else:
            self.err.print(f"[red]Error:[/red] {text}")

    def resume_hint(self, task_id: str) -> None:
        """Printed on Ctrl-C during verify so work isn't lost."""
        if self.json_mode:
            self.emit_json({"status": "interrupted", "task_id": task_id})
        else:
            self.err.print(
                f"\n[yellow]Still running server-side.[/yellow] Re-attach with:\n  lenz verify --resume {task_id}"
            )


# ── per-result renderers ──
def _verification_id_from_url(url: str | None) -> str:
    """Pull the 8-char verification_id off an assess ``verification_url``."""
    return url.rstrip("/").rsplit("/", 1)[-1] if url else ""


def render_extract(out: Output, result: ExtractedClaims) -> None:
    if out.json_mode:
        out.emit_json(_model_json(result))
        return
    # The server splits a claim set across two fields: the primary claim lands
    # in ``claim`` and any extras in ``identified_claims``. Neither alone is the
    # full list — the primary is usually NOT echoed into ``identified_claims`` —
    # so render the union so the primary is never dropped (and the count is
    # right). Single-claim input → just ``claim``.
    primary = (getattr(result, "claim", "") or "").strip()
    claims = [primary] if primary else []
    for c in result.identified_claims or []:
        c = (c or "").strip()
        if c and c not in claims:
            claims.append(c)
    if len(claims) > 1:
        out.console.print(f"[bold]{len(claims)} claims found:[/bold]")
        for i, claim in enumerate(claims, 1):
            out.console.print(f"  {i}. {claim}")
    elif claims:
        out.console.print(f"[bold]Claim:[/bold] {claims[0]}")
    else:
        out.console.print("[dim]No verifiable claim found in that text.[/dim]")
        return

    if result.candidate_claims:
        out.console.print("\n[dim]Ambiguous — candidate readings:[/dim]")
        for c in result.candidate_claims:
            out.console.print(f"  • {c}")
    # Only nudge to verify when there's a single, unambiguous claim — a lone
    # hint next to a multi-claim list reads as if it belongs to one of them.
    if len(claims) == 1:
        out.console.print(f'\n[dim]Verify it:[/dim] lenz verify "{claims[0]}"')


def render_assess(out: Output, result: AssessResponse) -> None:
    if out.json_mode:
        out.emit_json(_model_json(result))
        return
    claims = result.claims or []
    if not claims:
        out.console.print("[dim]No claims assessed.[/dim]")
        return
    for c in claims:
        color = _VERDICT_COLOR.get(c.verdict, "white")
        out.console.print(f"[{color}]{c.verdict or '?'}[/{color}] ({c.confidence}) — {c.claim}")
        vid = _verification_id_from_url(getattr(c, "verification_url", ""))
        if vid:
            out.console.print(f'    [dim]ask follow-ups:[/dim] lenz ask {vid} "<your question>"')


def render_verification(out: Output, v: Verification | None) -> None:
    if v is None:
        out.error(
            {"error": {"code": "empty_result", "message": "No verification returned.", "status": 0}},
            "No verification returned.",
        )
        raise SystemExit(1)
    if out.json_mode:
        out.emit_json(_model_json(v))
        return
    color = _VERDICT_COLOR.get(v.verdict, "white")
    score = "" if v.lenz_score is None else f"  [dim]score {v.lenz_score}/10[/dim]"
    out.console.print(f"[bold {color}]{v.verdict or '?'}[/bold {color}]  ({v.confidence}){score}")
    if v.executive_summary:
        out.console.print(f"\n{v.executive_summary}")
    if v.sources:
        out.console.print(f"\n[bold]Sources ({len(v.sources)}):[/bold]")
        for s in v.sources[:8]:
            title = s.title or s.source_name or s.url
            out.console.print(f"  • {title}\n    [blue]{s.url}[/blue]")
    out.console.print(f"\n[dim]verification_id: {v.verification_id}[/dim]")
    if v.verification_id:
        out.console.print(f'[dim]ask follow-ups:[/dim] lenz ask {v.verification_id} "<your question>"')


def _truncate(text: str, width: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _batch_status_cell(st: Any) -> Any:
    """The per-row status: an animated spinner while processing, else a verdict."""
    from rich.spinner import Spinner
    from rich.text import Text

    if st is None:
        return Spinner("dots", text=Text("Verifying…", style="dim"))
    if st.status == "processing":
        step = (st.progress or {}).get("step")
        from lenz_io.cli.verify import _step_label  # local: friendly step copy

        label = _step_label(step).removeprefix("Verifying… ")
        return Spinner("dots", text=Text(label, style="dim"))
    if st.status == "completed" and st.result is not None:
        v = st.result
        color = _VERDICT_COLOR.get(v.verdict, "white")
        score = "" if v.lenz_score is None else f" {v.lenz_score}/10"
        return Text(f"{v.verdict or '?'} ({v.confidence}){score}", style=f"bold {color}")
    if st.status == "failed":
        return Text("failed", style="red")
    return Text(st.status or "?", style="yellow")


def render_batch_table(picks: list[tuple[str, str]], statuses: dict[str, Any]) -> Any:
    """Live table for N concurrent verifications — one row per claim, each
    updating independently as its own pipeline progresses."""
    from rich.table import Table

    table = Table.grid(padding=(0, 2))
    table.add_column(justify="right", style="dim")  # [i/N]
    table.add_column(min_width=22)  # status
    table.add_column()  # claim
    n = len(picks)
    for i, (tid, text) in enumerate(picks, 1):
        table.add_row(f"[{i}/{n}]", _batch_status_cell(statuses.get(tid)), _truncate(text))
    return table


def _batch_verdict_block(out: Output, v: Verification) -> None:
    """Compact per-claim verdict for a batch: verdict line, summary, and a
    single dim footer (source count + verification_id). Deliberately omits the
    full source list and the ``ask`` hint that single-claim ``verify`` shows —
    in a batch of N those repeat into a wall. Full sources live on the web page
    behind the ``verification_id`` (or a single ``lenz verify`` of that claim)."""
    color = _VERDICT_COLOR.get(v.verdict, "white")
    score = "" if v.lenz_score is None else f"  [dim]score {v.lenz_score}/10[/dim]"
    out.console.print(f"[bold {color}]{v.verdict or '?'}[/bold {color}]  ({v.confidence}){score}")
    if v.executive_summary:
        out.console.print(v.executive_summary)
    footer = []
    if v.sources:
        footer.append(f"{len(v.sources)} source{'s' if len(v.sources) != 1 else ''}")
    if v.verification_id:
        footer.append(f"verification_id: {v.verification_id}")
    if footer:
        out.console.print(f"[dim]{'  ·  '.join(footer)}[/dim]")


def render_batch_details(out: Output, picks: list[tuple[str, str]], statuses: dict[str, Any]) -> None:
    """Final verdicts for each claim, after the live table settles — printed to
    stdout. Compact per claim (see ``_batch_verdict_block``) so N verdicts stay
    scannable instead of becoming a wall of sources."""
    n = len(picks)
    for i, (tid, text) in enumerate(picks, 1):
        if i > 1:
            out.console.print("[dim]" + "─" * 70 + "[/dim]")
        out.console.print(f"[bold]\\[{i}/{n}][/bold] {text}")
        st = statuses.get(tid)
        if st is not None and st.status == "completed" and st.result is not None:
            _batch_verdict_block(out, st.result)
        elif st is not None and st.status == "failed":
            out.console.print(f"[red]Failed:[/red] {st.error or st.failure_detail or 'pipeline error'}")
        else:
            label = "timed out" if st is None else (st.status or "unknown")
            out.console.print(f"[yellow]{label}[/yellow] — resume: lenz verify --resume {tid}")


def render_ask(out: Output, reply: Any) -> None:
    if out.json_mode:
        out.emit_json(_model_json(reply))
        return
    content = (getattr(reply, "content", "") or "").strip()
    if not content:
        out.console.print("[dim](empty reply)[/dim]")
        return
    # Ask replies are markdown (the chat subset: bold/italic/lists/paragraphs).
    # Render it instead of dumping raw, so '**600 Nm**' shows bold, not literal
    # asterisks, and paragraph spacing is normalized.
    out.console.print(Markdown(content))


def _capacity_row(out: Output, label: str, cap: UsageCapacity) -> None:
    """One credit-based capability (verify / ask): usable total + breakdown.

    ``remaining`` is the headline (monthly quota left + bonus credits); the dim
    tail shows the split — ``used / total quota`` plus ``+N bonus`` when the key
    holds one-off top-up credits."""
    detail = f"{cap.quota_used} / {cap.quota_total} quota"
    if cap.credits:
        detail += f" + {cap.credits} bonus"
    out.console.print(f"  {label + ':':<9} {cap.remaining} left  [dim]({detail})[/dim]")


def render_usage(out: Output, u: Usage) -> None:
    if out.json_mode:
        out.emit_json(_model_json(u))
        return
    out.console.print(f"[bold]Lenz usage[/bold]  [dim]({u.plan or '—'} plan)[/dim]")
    _capacity_row(out, "Verify", u.verify)
    _capacity_row(out, "Ask", u.ask)
    _capacity_row(out, "Assess", u.assess)
    ex = u.extract
    label = f"{'Extract:':<9}"
    if ex.unlimited:
        out.console.print(f"  {label} [dim]unlimited[/dim]")
    else:
        out.console.print(f"  {label} {ex.calls_today} / {ex.daily_limit} today  [dim](free — no credit charge)[/dim]")
    if u.quota_resets_at:
        out.console.print(f"  [dim]Quota resets {u.quota_resets_at}[/dim]")


def render_config(out: Output, payload: dict[str, Any]) -> None:
    if out.json_mode:
        out.emit_json(payload)
        return
    out.console.print("[bold]Lenz CLI config[/bold]")
    out.console.print(f"  API key:     {payload['api_key']}  [dim](source: {payload['key_source']})[/dim]")
    out.console.print(f"  Base URL:    {payload['base_url']}")
    out.console.print(f"  Config file: {payload['config_file']}")
