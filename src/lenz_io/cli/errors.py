"""Error mapping — the single place the CLI turns any failure into either a
friendly stderr message or the locked ``--json`` error contract.

``--json`` failures emit ``{"error": {"code", "message", "status"}}`` (plus an
optional ``fix``) to **stdout** and exit nonzero, so a consumer (the future
Claude Code skill / MCP server) always parses valid JSON and branches on the
``error`` key. Human mode prints a friendly block to stderr. Never a traceback.

Status codes match the real server (verified against the API): no-credits is
**403** (not 402), extract rate-limit **429**, bad key **401**.
"""

from __future__ import annotations

from typing import Any

from lenz_io.errors import (
    LenzAuthError,
    LenzError,
    LenzNeedsInputError,
    LenzPipelineError,
    LenzQuotaExceededError,
    LenzRateLimitError,
    LenzTimeoutError,
    LenzValidationError,
)

from .config import ConfigError


class CLIError(Exception):
    """A CLI-layer error carrying a machine ``code`` and an ``exit_code``."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "cli_error",
        status: int = 0,
        fix: str = "",
        exit_code: int = 1,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.fix = fix
        self.exit_code = exit_code


_CODE_BY_TYPE: dict[type, str] = {
    LenzAuthError: "unauthorized",
    LenzQuotaExceededError: "no_credits",
    LenzRateLimitError: "rate_limited",
    LenzValidationError: "invalid_request",
    LenzTimeoutError: "timeout",
    LenzPipelineError: "pipeline_failed",
    LenzNeedsInputError: "needs_input",
}


def no_api_key_error() -> CLIError:
    return CLIError(
        "No API key found. Run `lenz login` to add one (free), or set LENZ_API_KEY.",
        code="no_api_key",
        fix="Run: lenz login",
        exit_code=1,
    )


def to_payload(exc: Exception) -> dict[str, Any]:
    """Normalize any handled error into the locked ``--json`` error shape."""
    if isinstance(exc, CLIError):
        code, message, status, fix = exc.code, exc.message, exc.status, exc.fix
    elif isinstance(exc, LenzError):
        code = _CODE_BY_TYPE.get(type(exc), "api_error")
        message = exc.message or str(exc)
        status = exc.status_code
        fix = exc.fix
    elif isinstance(exc, ConfigError):
        code, message, status, fix = "bad_config", str(exc), 0, "Fix or delete the config file."
    else:
        code, message, status, fix = "error", str(exc), 0, ""
    err: dict[str, Any] = {"code": code, "message": message, "status": status}
    if fix:
        err["fix"] = fix
    return {"error": err}


def exit_code_for(exc: Exception) -> int:
    return exc.exit_code if isinstance(exc, CLIError) else 1


def friendly_text(exc: Exception) -> str:
    """A single human-readable block for stderr (pretty mode)."""
    message = getattr(exc, "message", None) or str(exc)
    fix = getattr(exc, "fix", "")
    if isinstance(exc, LenzAuthError) and not fix:
        fix = "Run `lenz login` or check your API key."
    return f"{message}\n  Fix: {fix}" if fix else message
