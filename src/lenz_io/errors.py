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
import warnings
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
      * ``code``       — the server's machine-readable error code, e.g.
        ``"no_credits"``. Present on 402, 403 and 429; ``""`` when the
        server sent none. Branch on this rather than on message text.
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
        code: str = "",
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
        self.code = code
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
    """401 / 403 — the API key is missing, invalid, or revoked.

    Note: an out-of-credits response is NOT this error. It used to be —
    the API returned 403 for quota, which landed here — but the API now
    returns 402 and that maps to :class:`LenzQuotaExceededError`. If you were
    catching ``LenzAuthError`` to handle an empty balance, catch the quota
    error instead. The two do not share a parent on purpose: "fix your key"
    and "top up your account" are different actions.
    """


class LenzQuotaExceededError(LenzError):
    """402 — you're out of balance, or your plan doesn't cover this call.

    Fields set from the response body:

      * ``upgrade_url``  — where the wall lifts (the plans page).
      * ``remaining``    — usable capacity left for the capability, or
        ``None`` when the server didn't say. **Nullable on purpose**: the
        old ``credits_remaining: int = 0`` could not tell "0 left" apart
        from "server said nothing", which made it useless to branch on.
      * ``resets_at``    — ISO-8601 timestamp of the next monthly reset,
        or ``None``.
      * ``requested``    — for a batch call, how many units were asked for.
      * ``credit_balance`` — credits left in the account's pool, or ``None``
        when the server didn't say. This is the server's ``credits_remaining``
        body field; the SDK spells it differently because
        ``exc.credits_remaining`` is a long-standing deprecated alias of
        ``remaining`` with a DIFFERENT meaning (see below). The raw key is
        always available as ``exc.body["credits_remaining"]``.
      * ``cost``         — credits the rejected call would have taken (a
        ``/verify`` is 10, ``/assess`` and ``/ask`` are 1), or ``None``.

    ``remaining`` and ``requested`` are in the **capability's** unit —
    verifications, assessments, follow-ups. ``credit_balance`` and ``cost``
    are in **credits**, so together they separate "you have 4 credits and this
    verify costs 10" from "you have nothing".

    ``cost`` is **depth-aware**, not a fixed multiple of the standard price: a
    rejected ``depth="low"`` verify reports 5, and a rejected batch that mixes
    depths reports its real summed total rather than ``n x 10`` or ``n x 5``.
    Read it rather than recomputing it from ``requested`` and a price you
    assumed. The charge follows the depth **requested** — a ``low`` request is
    priced at ``low`` even when the server could have answered it from a
    cached ``standard`` verdict.
    """

    upgrade_url: str = ""
    remaining: int | None = None
    resets_at: str | None = None
    requested: int | None = None
    credit_balance: int | None = None
    cost: int | None = None

    @property
    def credits_remaining(self) -> int:
        """Deprecated alias for ``remaining``. Removed in 3.0.

        Returns 0 when ``remaining`` is unknown, which is exactly the
        ambiguity ``remaining`` exists to fix — migrate to ``remaining``.

        Note this is NOT the server's ``credits_remaining`` body field, which
        arrived later and reports the credit POOL. That one is
        ``credit_balance``. The two differ by the capability's weight — a
        verify's ``remaining`` of 0 sits next to a ``credit_balance`` of 4 —
        so this alias is deliberately left pointing where it always pointed.
        """
        self._warn_credits_remaining()
        return self.remaining or 0

    @credits_remaining.setter
    def credits_remaining(self, value: int | None) -> None:
        """Writes through to ``remaining``.

        A read-only property here would be a breaking change in a MINOR
        release: ``LenzError.__init__`` splats unknown kwargs onto the
        instance via ``setattr``, and its docstring advertises that as the
        forward-compatibility path — so
        ``LenzQuotaExceededError(credits_remaining=0)`` was legal in 2.6.0 and
        appears in real test fixtures and retry shims. Without this setter,
        those raise ``AttributeError`` on upgrade.
        """
        self._warn_credits_remaining()
        self.remaining = value

    @staticmethod
    def _warn_credits_remaining() -> None:
        warnings.warn(
            "credits_remaining is deprecated and will be removed in 3.0; "
            "use `remaining`, which is None when the server didn't report a "
            "balance (credits_remaining reports that as 0). It is NOT the "
            "API's `credits_remaining` body field — that is the credit pool, "
            "and the SDK reports it as `credit_balance`.",
            DeprecationWarning,
            stacklevel=3,
        )


class LenzValidationError(LenzError):
    """422 — request body failed schema validation.

    ``errors`` is a list of per-field error dicts as returned by Ninja:
    ``[{"loc": [...], "msg": "...", "type": "..."}]``.
    """

    errors: list[dict[str, Any]] = []  # noqa: RUF012 — overridden per-instance


class LenzRateLimitError(LenzError):
    """429 — rate limited.

      * ``retry_after``       — seconds until the next allowed call, resolved
        from the ``Retry-After`` header or the body's ``reset_in_seconds``.
      * ``limit``             — the cap that was hit, when the server states it.
      * ``reset_in_seconds``  — the body's raw echo of the same wait.

    Seeing this raised does not always mean the automatic retry ladder was
    exhausted: waits longer than ``MAX_RETRY_AFTER_SLEEP`` raise immediately
    so a call can't block for hours inside a sleeping retry.
    """

    retry_after: int = 0
    limit: int | None = None
    reset_in_seconds: int | None = None
    #: Where the cap lifts. The server sends this on 429 as well as 402,
    #: deliberately — someone hitting the daily /extract cap also wants to
    #: know a paid plan raises it.
    upgrade_url: str = ""


class LenzAPIError(LenzError):
    """500 / 502 / 503 / 504 / catch-all for unexpected server errors."""


class LenzUpstreamUnavailableError(LenzAPIError):
    """503 with ``code`` ``upstream_unavailable`` or ``capacity``.

    The server is telling you the truth about a *transient* condition: its
    model/search providers are exhausted (``upstream_unavailable`` — nothing
    was charged, the same request succeeds once they recover) or the pipeline
    is at capacity (``capacity`` — nothing was accepted or charged). Retry the
    SAME request after ``retry_after`` seconds.

    Subclasses :class:`LenzAPIError`, so existing ``except LenzAPIError``
    handlers keep catching it. Waits up to ``MAX_RETRY_AFTER_SLEEP`` are
    already slept through by the automatic retry ladder — seeing this raised
    means the stated wait was longer, and ``retry_after`` carries it.
    """

    retry_after: int | None = None


class LenzTimeoutError(LenzError):
    """``verify_and_wait`` exceeded the configured timeout.

    ``task_id`` is set so callers can resume via ``client.get_status(task_id)``.
    """

    task_id: str = ""


class LenzNeedsInputError(LenzError):
    """``verify_and_wait`` paused because the pipeline needs caller input.

    Carries ``task_id``, ``kind`` ("multi_claim" / "clarification_required" /
    "duplicate_found"), ``hint`` (one sentence on what was unclear and how to
    resolve it via ``client.select`` — ``""`` from older servers) and
    ``payload`` (the full status response).
    """

    task_id: str = ""
    kind: str = ""
    hint: str = ""
    payload: dict[str, Any] = {}  # noqa: RUF012 — overridden per-instance


class LenzPipelineError(LenzError):
    """``verify_and_wait`` saw a terminal ``failed`` state from the pipeline.

    ``failure_class`` says WHY (closed set: ``upstream_unavailable`` |
    ``insufficient_evidence`` | ``invalid_input`` | ``cancelled`` |
    ``internal``); ``retryable`` is the derived signal — ``True`` means a
    transient provider-side exhaustion where resubmitting the same claim is
    the right move. ``None`` when an older server didn't say.
    """

    task_id: str = ""
    failure_reason: str = ""
    failure_class: str = ""
    retryable: bool | None = None


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

# NOTE: quota is 402 and only 402. There is deliberately no "403 carrying a
# quota code also means quota" fallback here — the API emits 402, and the only
# thing such a fallback would buy is coverage for a server rollback. The MCP
# server keeps an equivalent branch because it is a separate Cloud Run service
# that deploys non-atomically alongside the API; an SDK has no such window.
#
# Longest Retry-After we'll sleep through inside the automatic retry ladder.
# The /extract daily cap sends seconds-until-UTC-midnight, so honoring the raw
# value could block a call for ~24h (three times over). Above this we raise
# immediately with the true retry_after so the caller can schedule the work.
MAX_RETRY_AFTER_SLEEP = 60

# The body ``code`` values the server sends on a 503 it produced deliberately:
# providers exhausted mid-pipeline, or a submission shed at the door. Both map
# to LenzUpstreamUnavailableError and both state an honest wait.
#
# The retry ladder in ``client.py`` keys its immediate-abort decision on THIS,
# not on the status number: an ordinary Cloud Run / CDN / load-balancer 503
# carries no Lenz code, states a maintenance-window wait, and must keep being
# retried exactly as it was before 2.8.0.
UPSTREAM_503_CODES = ("upstream_unavailable", "capacity")

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

    # String-typed only, matching the Node SDK: a malformed `code: 42` becomes
    # "" rather than the string "42", so nothing downstream branches on a
    # value the server never meant as a code.
    code_raw = parsed.get("code")
    code = code_raw if isinstance(code_raw, str) else ""

    if status_code in _STATUS_MAP:
        cls, default_msg, doc_url = _STATUS_MAP[status_code]
    elif status_code == 503 and code in UPSTREAM_503_CODES:
        cls, default_msg, doc_url = (
            LenzUpstreamUnavailableError,
            "Service temporarily unavailable",
            f"{_DOCS_BASE}/errors#unavailable",
        )
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
        code=code,
        body=parsed,
    )

    # Class-specific enrichment from the response body. Each is set on the
    # instance so callers can access via the documented attribute name.
    if isinstance(err, LenzUpstreamUnavailableError):
        # Body ``retry_after`` first (both 503 shapes carry it), header as
        # the fallback for any proxy that strips the body.
        stated = _opt_int(parsed.get("retry_after"))
        if stated is None:
            stated = _opt_int(headers.get("Retry-After") or headers.get("retry-after"))
        err.retry_after = stated

    if isinstance(err, LenzQuotaExceededError):
        # String-typed only. `str(...)` on a malformed dict would render
        # "{'a': 1}" and friendly_text would show that to a user as a URL.
        upgrade_url = parsed.get("upgrade_url")
        err.upgrade_url = upgrade_url if isinstance(upgrade_url, str) else ""
        # None, not 0, when absent — the server omits these rather than
        # sending null precisely so "unknown" stays distinguishable from
        # "zero". Collapsing that here would throw the distinction away.
        err.remaining = _opt_int(parsed.get("remaining"))
        err.requested = _opt_int(parsed.get("requested"))
        # The pool behind the capability figure above, and this call's price in
        # credits. The body's ``credits_remaining`` lands on ``credit_balance``,
        # NOT on the same-named deprecated property — that one aliases
        # ``remaining`` and means a different quantity (see the class docstring).
        err.credit_balance = _opt_int(parsed.get("credits_remaining"))
        err.cost = _opt_int(parsed.get("cost"))
        resets_at = parsed.get("resets_at")
        err.resets_at = resets_at if isinstance(resets_at, str) and resets_at else None
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
        err.limit = _opt_int(parsed.get("limit"))
        err.reset_in_seconds = _opt_int(parsed.get("reset_in_seconds"))
        rl_upgrade_url = parsed.get("upgrade_url")
        err.upgrade_url = rl_upgrade_url if isinstance(rl_upgrade_url, str) else ""
        # Header first, then the body. `reset_in_seconds` is what the server
        # actually sends; `retry_after` was an SDK-side invention that the
        # server has never emitted — kept last purely as a defensive read.
        #
        # Each candidate is COERCED before being accepted, rather than picking
        # the first truthy raw value and coercing once at the end. `Retry-After`
        # may legally be an HTTP-date (RFC 7231), and a truthy-but-unparseable
        # header would otherwise win the `or` chain, coerce to None, and land
        # on 0 — discarding a perfectly good `reset_in_seconds` and telling the
        # caller to retry immediately against a server that just throttled it.
        for candidate in (
            headers.get("Retry-After"),
            headers.get("retry-after"),
            parsed.get("reset_in_seconds"),
            parsed.get("retry_after"),
        ):
            resolved = _opt_int(candidate)
            if resolved is not None:
                err.retry_after = resolved
                break
        else:
            err.retry_after = 0

    return err


def _opt_int(value: Any) -> int | None:
    """Coerce to int, or None when absent/unparseable.

    Blank input returns None rather than 0. In JS ``Number("")`` and
    ``Number(" ")`` are both ``0``, so the Node counterpart trims before the
    same check — a whitespace-only ``remaining`` must read as "unknown", not
    "balance is empty", which is the whole reason these fields are nullable.

    Numeric strings with a fractional part are accepted and truncated, so
    ``"42.7"`` and ``42.7`` agree. Every field this parses (counts, seconds)
    is an integer on the wire; a float is malformed either way, and the two
    SDKs disagreeing about it is worse than either answer.
    """
    if isinstance(value, str):
        value = value.strip()
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _fix_hint_for(status_code: int) -> str:
    return {
        401: "Generate a new key at https://lenz.io/api-credentials.",
        403: "This key doesn't have access to that resource.",
        402: "Top up or upgrade at https://lenz.io/plans, or wait for the period reset.",
        422: "Check the request body against the OpenAPI spec.",
        429: "Wait Retry-After seconds and retry.",
    }.get(status_code, "Retry; if the error persists, file an issue with the Request ID.")


__all__ = [
    "MAX_RETRY_AFTER_SLEEP",
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
