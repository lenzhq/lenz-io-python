"""Typed exception hierarchy for the Lenz SDK.

All HTTP error responses funnel through ``map_response_to_error`` which is
table-driven: one place to update when the API adds new error contracts.
The TS SDK mirrors this exact mapping; the table is the cross-language
invariant.

Every exception carries a ``request_id`` (the ``X-Request-ID`` value from
the response headers) so customers can quote it on support tickets and we
can find the exact ``APICallLog`` row that produced their error.

Error messages follow the Tier 2 Rust-style format:

    Cause:  {what went wrong}
    Fix:    {what to do about it}
    Docs:   https://lenz.io/docs/{topic}
    Request ID: {id}
"""

from __future__ import annotations

import json
from typing import Any


class LenzError(Exception):
    """Base class for every error raised by the SDK.

    All Lenz exceptions accept a uniform set of context kwargs plus any
    class-specific enrichment. Subclasses may set per-class attributes
    (e.g. ``retry_after`` on rate-limit errors) — pass them as kwargs;
    unknown kwargs are stored on the instance for forward compatibility.

    Fields:
      * ``message``    — short human description
      * ``cause``      — why the server returned the error
      * ``fix``        — what to do next
      * ``doc_url``    — deep link to the relevant docs page
      * ``request_id`` — ``X-Request-ID`` header value; quote on support tickets
      * ``status_code``— HTTP status code (0 for client-side errors)
      * ``body``       — parsed JSON response body if available
    """

    def __init__(
        self,
        *,
        message: str = "",
        cause: str = "",
        fix: str = "",
        doc_url: str = "",
        request_id: str = "",
        status_code: int = 0,
        body: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(message or self.__class__.__name__)
        self.message = message
        self.cause = cause
        self.fix = fix
        self.doc_url = doc_url
        self.request_id = request_id
        self.status_code = status_code
        self.body = body
        # Per-subclass enrichment (retry_after, task_id, etc.). Set on the
        # instance so they're accessible as ``exc.task_id`` regardless of
        # which subclass raised.
        for k, v in extra.items():
            setattr(self, k, v)

    def __str__(self) -> str:  # pragma: no cover - trivial
        lines = [self.message or self.__class__.__name__]
        if self.cause:
            lines.append(f"  Cause:  {self.cause}")
        if self.fix:
            lines.append(f"  Fix:    {self.fix}")
        if self.doc_url:
            lines.append(f"  Docs:   {self.doc_url}")
        if self.request_id:
            lines.append(f"  Request ID: {self.request_id}")
        return "\n".join(lines)


class LenzAuthError(LenzError):
    """401 / 403 — the API key is missing, invalid, or revoked."""


class LenzQuotaExceededError(LenzError):
    """402 — you've spent all your credits this period.

    ``credits_remaining`` is set from the response body.
    """

    credits_remaining: int = 0


class LenzValidationError(LenzError):
    """422 — request body failed schema validation.

    ``errors`` is a list of per-field error dicts as returned by Ninja:
    ``[{"loc": [...], "msg": "...", "type": "..."}]``.
    """

    errors: list[dict[str, Any]] = []  # noqa: RUF012 — overridden per-instance


class LenzRateLimitError(LenzError):
    """429 — rate limited. ``retry_after`` is seconds until the next allowed call."""

    retry_after: int = 0


class LenzAPIError(LenzError):
    """500 / 502 / 503 / 504 / catch-all for unexpected server errors."""


class LenzTimeoutError(LenzError):
    """``verify_and_wait`` exceeded the configured timeout.

    ``task_id`` is set so callers can resume via ``client.get_status(task_id)``.
    """

    task_id: str = ""


class LenzNeedsInputError(LenzError):
    """``verify_and_wait`` paused because the pipeline needs caller input.

    Carries ``task_id``, ``kind`` ("multi_claim" / "clarification_required" /
    "duplicate_found"), and ``payload`` (the full status response).
    """

    task_id: str = ""
    kind: str = ""
    payload: dict[str, Any] = {}  # noqa: RUF012 — overridden per-instance


class LenzPipelineError(LenzError):
    """``verify_and_wait`` saw a terminal ``failed`` state from the pipeline."""

    task_id: str = ""
    failure_reason: str = ""


class LenzWebhookSignatureError(LenzError):
    """``LenzWebhooks.parse`` rejected a payload.

    Possible reasons: tampered body (HMAC mismatch), missing
    ``X-Lenz-Signature`` header, replay window exceeded, malformed body.
    """


# ── Mapping table ────────────────────────────────────────────────────────
#
# Single source of truth for HTTP status -> exception class + default
# message text. The TS SDK ships an equivalent table; both must stay in
# sync. Tests pin the mapping (test_errors.py).

_DOCS_BASE = "https://lenz.io/docs"

_STATUS_MAP: dict[int, tuple[type[LenzError], str, str]] = {
    401: (
        LenzAuthError,
        "Unauthorized",
        f"{_DOCS_BASE}/auth",
    ),
    403: (
        LenzAuthError,
        "Forbidden",
        f"{_DOCS_BASE}/auth",
    ),
    402: (
        LenzQuotaExceededError,
        "Payment required",
        f"{_DOCS_BASE}/billing",
    ),
    422: (
        LenzValidationError,
        "Validation failed",
        f"{_DOCS_BASE}/errors/validation",
    ),
    429: (
        LenzRateLimitError,
        "Rate limit exceeded",
        f"{_DOCS_BASE}/rate-limits",
    ),
}


def _parse_body(raw: bytes | str | None) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def map_response_to_error(
    status_code: int,
    body: bytes | str | None,
    headers: dict[str, str] | None = None,
) -> LenzError:
    """Translate an HTTP error response into the right typed exception.

    Returns an *instance* (not raised) so callers can decide whether
    to raise, log, or surface. Keep this pure — no I/O.
    """
    parsed = _parse_body(body)
    headers = headers or {}
    request_id = headers.get("X-Request-ID") or headers.get("x-request-id") or ""

    if status_code in _STATUS_MAP:
        cls, default_msg, doc_url = _STATUS_MAP[status_code]
    elif 500 <= status_code < 600:
        cls, default_msg, doc_url = LenzAPIError, "Server error", f"{_DOCS_BASE}/errors"
    else:
        cls, default_msg, doc_url = LenzError, f"HTTP {status_code}", f"{_DOCS_BASE}/errors"

    detail = parsed.get("detail") or default_msg
    err = cls(
        message=str(detail),
        cause=str(detail),
        fix=_fix_hint_for(status_code),
        doc_url=doc_url,
        request_id=request_id,
        status_code=status_code,
        body=parsed,
    )

    # Class-specific enrichment from the response body. Each is set on the
    # instance so callers can access via the documented attribute name.
    if isinstance(err, LenzQuotaExceededError):
        err.credits_remaining = int(parsed.get("credits_remaining", 0) or 0)
    elif isinstance(err, LenzValidationError):
        # Ninja returns errors as `detail: [...]` (a list of per-field dicts).
        # Older or alternative shapes can put them under `errors`.
        if isinstance(parsed.get("detail"), list):
            err.errors = parsed["detail"]
        elif isinstance(parsed.get("errors"), list):
            err.errors = parsed["errors"]
        else:
            err.errors = []
    elif isinstance(err, LenzRateLimitError):
        ra = headers.get("Retry-After") or headers.get("retry-after") or parsed.get("retry_after", 0)
        try:
            err.retry_after = int(ra)
        except (TypeError, ValueError):
            err.retry_after = 0

    return err


def _fix_hint_for(status_code: int) -> str:
    return {
        401: "Generate a new key at https://lenz.io/api-integration.",
        403: "This key doesn't have access to that resource.",
        402: "Upgrade your plan or wait for the period reset.",
        422: "Check the request body against the OpenAPI spec.",
        429: "Wait Retry-After seconds and retry.",
    }.get(status_code, "Retry; if the error persists, file an issue with the Request ID.")


__all__ = [
    "LenzAPIError",
    "LenzAuthError",
    "LenzError",
    "LenzNeedsInputError",
    "LenzPipelineError",
    "LenzQuotaExceededError",
    "LenzRateLimitError",
    "LenzTimeoutError",
    "LenzValidationError",
    "LenzWebhookSignatureError",
    "map_response_to_error",
]
