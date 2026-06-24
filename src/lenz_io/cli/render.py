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
    # The API atomises a single input into ``atomic_claim`` and only fills
    # ``identified_claims`` for multi-claim text — so keying solely off the
    # latter renders "nothing found" for the common single-claim case. Prefer
    # the explicit list, fall back to the atomic claim, and only declare an
    # empty result when both are absent.
    claims = result.identified_claims or []
    atomic = (getattr(result, "atomic_claim", "") or "").strip()
    if claims:
        out.console.print(f"[bold]{len(claims)} claim(s) found:[/bold]")
        for i, claim in enumerate(claims, 1):
            out.console.print(f"  {i}. {claim}")
    elif atomic:
        out.console.print(f"[bold]Claim:[/bold] {atomic}")
    else:
        out.console.print("[dim]No verifiable claim found in that text.[/dim]")
        return

    context = []
    domain = (getattr(result, "domain", "") or "").strip()
    if domain:
        context.append(domain)
    entities = getattr(result, "key_entities", None) or []
    names = [getattr(e, "name", "") for e in entities if getattr(e, "name", "")]
    if names:
        context.append(", ".join(names[:4]))
    if context:
        out.console.print(f"[dim]{'  •  '.join(context)}[/dim]")
    if result.candidate_claims:
        out.console.print("\n[dim]Ambiguous — candidate readings:[/dim]")
        for c in result.candidate_claims:
            out.console.print(f"  • {c}")
    if atomic:
        out.console.print(f'\n[dim]Verify it:[/dim] lenz verify "{atomic}"')


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


def render_config(out: Output, payload: dict[str, Any]) -> None:
    if out.json_mode:
        out.emit_json(payload)
        return
    out.console.print("[bold]Lenz CLI config[/bold]")
    out.console.print(f"  API key:     {payload['api_key']}  [dim](source: {payload['key_source']})[/dim]")
    out.console.print(f"  Base URL:    {payload['base_url']}")
    out.console.print(f"  Config file: {payload['config_file']}")
