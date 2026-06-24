"""Output rendering — the single boundary between pretty (TTY) and machine
(``--json``) output.

``--json`` is forced on whenever stdout is not a TTY, so piping
(``lenz verify ... | jq``) always yields clean JSON even without the flag.
Progress/spinners go to **stderr** so ``--json`` stdout stays a single object.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from rich.console import Console

from lenz_io.models import (
    AssessResponse,
    ExtractedClaims,
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
        self.err = Console(stderr=True, no_color=no_color)

    # ── primitives ──
    def emit_json(self, payload: Any) -> None:
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    def note(self, text: str) -> None:
        """Human-only aside to stderr (suppressed in json mode)."""
        if not self.json_mode:
            self.err.print(text)

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
def render_extract(out: Output, result: ExtractedClaims) -> None:
    if out.json_mode:
        out.emit_json(_model_json(result))
        return
    claims = result.identified_claims or []
    if not claims:
        out.console.print("[dim]No verifiable claims found.[/dim]")
        return
    out.console.print(f"[bold]{len(claims)} claim(s) found:[/bold]")
    for i, claim in enumerate(claims, 1):
        out.console.print(f"  {i}. {claim}")
    if result.candidate_claims:
        out.console.print("\n[dim]Ambiguous — candidate readings:[/dim]")
        for c in result.candidate_claims:
            out.console.print(f"  • {c}")


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


def render_ask(out: Output, reply: Any) -> None:
    if out.json_mode:
        out.emit_json(_model_json(reply))
        return
    out.console.print(reply.content or "[dim](empty reply)[/dim]")


def render_config(out: Output, payload: dict[str, Any]) -> None:
    if out.json_mode:
        out.emit_json(payload)
        return
    out.console.print("[bold]Lenz CLI config[/bold]")
    out.console.print(f"  API key:     {payload['api_key']}  [dim](source: {payload['key_source']})[/dim]")
    out.console.print(f"  Base URL:    {payload['base_url']}")
    out.console.print(f"  Config file: {payload['config_file']}")
