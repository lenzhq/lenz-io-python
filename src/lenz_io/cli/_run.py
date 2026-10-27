"""The shared command wrapper — the DRY core every verb runs through.

``execute`` owns client construction, the no-key short-circuit, and uniform
error handling (JSON-to-stdout in ``--json`` mode, friendly text to stderr
otherwise) + nonzero exit. Commands shrink to "call the SDK, render the
result." This is the single place the error/render contract lives, so it can
never drift between commands (and the future MCP server reuses it).
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lenz_io import Lenz

from .client import build_client
from .context import CLIState
from .errors import CLIError, exit_code_for, friendly_text, no_api_key_error, to_payload


def read_text_arg(text: str | None) -> str:
    """Resolve a text argument from the positional value, ``-``, or stdin.

    Reads stdin only when it's explicitly requested (``-``) or actually piped.
    With no argument on an interactive terminal we must NOT call ``read()`` — it
    would block forever waiting for input the user doesn't know to give.
    """
    if text == "-":
        data = sys.stdin.read()
    elif text is None:
        if sys.stdin.isatty():
            raise CLIError(
                "No text provided. Pass it as an argument or pipe it via stdin.",
                code="no_input",
                exit_code=2,
            )
        data = sys.stdin.read()
    else:
        data = text
    data = data.strip()
    if not data:
        raise CLIError(
            "No text provided. Pass it as an argument or pipe it via stdin.",
            code="no_input",
            exit_code=2,
        )
    return data


def execute(state: CLIState, *, needs_key: bool, work: Callable[[Lenz], None], error_exit: int | None = None) -> None:
    """Build the client, run ``work``, translate any failure into the contract.

    ``error_exit`` replaces the exit code of every failure, for a command whose
    exit codes carry a result (``lenz review``: 1 means "issues found", so an
    error must not exit 1 as well).
    """
    out = state.output
    try:
        if needs_key and state.key_source == "none":
            raise no_api_key_error()
        client = build_client(api_key=state.api_key, base_url=state.base_url)
        try:
            work(client)
        finally:
            client.close()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        if os.environ.get("LENZ_CLI_TRACEBACK"):
            raise
        out.error(to_payload(exc), friendly_text(exc))
        raise SystemExit(exit_code_for(exc) if error_exit is None else error_exit) from None


_ALLOWED_SOURCE_META_KEYS = {"url", "doi", "cite", "title", "date", "published_date"}


def parse_source_file_arg(spec: str) -> dict[str, Any]:
    """Parse a ``--source-file`` argument of the form ``PATH``, ``[key=val,...:]PATH``,
    or ``key=val:...:PATH``.

    Allowed metadata keys: ``url``, ``doi``, ``cite``, ``title``, ``date``, ``published_date``.
    Reads the file at ``PATH`` as UTF-8 into ``text``. If ``title`` is not specified,
    it defaults to ``Path(path).name``.
    """
    raw = spec.strip()
    if not raw:
        raise CLIError("Source file specification cannot be empty.", code="invalid_source_file_arg", exit_code=2)

    meta_str = ""
    path_str = ""

    if raw.startswith("["):
        depth = 0
        idx = -1
        for i, ch in enumerate(raw):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    idx = i
                    break
        if idx == -1:
            raise CLIError(
                f"Invalid source file specification (unclosed '['): {spec}",
                code="invalid_source_file_arg",
                exit_code=2,
            )
        meta_str = raw[1:idx]
        rest = raw[idx + 1 :]
        if rest.startswith(":"):
            rest = rest[1:]
        path_str = rest
    else:
        has_meta_prefix = any(raw.startswith(f"{k}=") for k in _ALLOWED_SOURCE_META_KEYS) or bool(
            re.match(r"^[a-zA-Z_]+=.*:", raw)
        )
        if has_meta_prefix:
            colons = [i for i, ch in enumerate(raw) if ch == ":" and raw[i : i + 3] != "://"]
            if not colons:
                raise CLIError(
                    f"Invalid source file specification (missing ':' before path): {spec}",
                    code="invalid_source_file_arg",
                    exit_code=2,
                )
            split_idx = colons[-1]
            if (
                len(colons) >= 2
                and colons[-2] == split_idx - 2
                and raw[split_idx - 1 : split_idx].isalpha()
                and raw[split_idx + 1 : split_idx + 2] in ("\\", "/")
            ):
                split_idx = colons[-2]
            meta_str = raw[:split_idx]
            path_str = raw[split_idx + 1 :]
        else:
            meta_str = ""
            path_str = raw

    path_str = path_str.strip()
    if not path_str:
        raise CLIError(
            f"No file path provided in source file specification: {spec}",
            code="invalid_source_file_arg",
            exit_code=2,
        )

    file_path = Path(path_str)
    if not file_path.exists() or not file_path.is_file():
        raise CLIError(
            f"Source file not found: {path_str}",
            code="source_file_not_found",
            exit_code=2,
        )

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        raise CLIError(
            f"Could not read source file {path_str}: {exc}",
            code="source_file_read_error",
            exit_code=2,
        ) from exc

    if not content.strip():
        raise CLIError(
            f"Source file is empty: {path_str}",
            code="empty_source_file",
            exit_code=2,
        )

    meta: dict[str, str] = {}
    if meta_str:
        meta_str = meta_str.strip().rstrip(":")
        raw_matches = list(re.finditer(r"(?:^|[:,])\s*([a-zA-Z_]+)=", meta_str))
        if not raw_matches or raw_matches[0].start() != 0:
            raise CLIError(
                f"Invalid source file metadata format: {meta_str}",
                code="invalid_source_file_arg",
                exit_code=2,
            )
        matches: list[re.Match[str]] = []
        for m in raw_matches:
            key = m.group(1).strip()
            if matches and matches[-1].group(1).strip() == "url" and key not in _ALLOWED_SOURCE_META_KEYS:
                continue
            matches.append(m)
        for i, m in enumerate(matches):
            key = m.group(1).strip()
            if key not in _ALLOWED_SOURCE_META_KEYS:
                allowed = ", ".join(sorted(_ALLOWED_SOURCE_META_KEYS))
                raise CLIError(
                    f"Unknown source file metadata key '{key}'. Allowed keys: {allowed}",
                    code="invalid_source_file_arg",
                    exit_code=2,
                )
            val_start = m.end()
            val_end = matches[i + 1].start() if i + 1 < len(matches) else len(meta_str)
            val = meta_str[val_start:val_end].rstrip(":, ").strip()
            if key == "date":
                key = "published_date"
            meta[key] = val

    result: dict[str, Any] = {
        "text": content,
        "title": meta.get("title") or file_path.name,
    }
    for field in ("url", "doi", "cite", "published_date"):
        if meta.get(field):
            result[field] = meta[field]

    return result


def parse_source_file_args(specs: list[str] | None) -> list[dict[str, Any]] | None:
    """Parse multiple ``--source-file`` arguments.

    Returns ``None`` when ``specs`` is ``None`` or empty.
    """
    if not specs:
        return None
    return [parse_source_file_arg(s) for s in specs]
