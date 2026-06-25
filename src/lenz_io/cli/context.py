"""Per-invocation CLI state, built once in ``main``'s callback and stashed on
the Typer ``Context`` so every command reads the same resolved key / base URL /
output mode without re-resolving."""

from __future__ import annotations

from dataclasses import dataclass

from .render import Output


@dataclass
class CLIState:
    output: Output
    api_key: str
    key_source: str
    base_url: str
