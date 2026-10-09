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
import re
import warnings
from typing import TYPE_CHECKING, Any

from typing_extensions import deprecated

if TYPE_CHECKING:
    from .models import Citecheck, ReviewFull


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
      * ``retryable``  — whether sending the same request again can succeed:
        ``True`` for a network failure, a transport timeout, a 429, a 5xx and
        a 409 ``idempotency_conflict`` (the first request with that key is
        still running) or ``verification_not_ready``; ``False`` for any other
        4xx and a version error; ``None`` when there was no HTTP status (a
        missing key, a ``*_and_wait`` timeout, a needs-input pause, a bad
        webhook signature). A failed
        verification, review or citation check carries the server's value
        (``None`` when it sent none), and a boolean ``retryable`` in the
        response's ``failure`` block always wins. Since 3.0; set on every
        instance at construction, and a ``retryable=`` passed in wins.
    """

    retryable: bool | None
    #: The ``Idempotency-Key`` the failed call sent, or ``None`` when it sent
    #: none. To resend safely, pass it back (``idempotency_key=exc.idempotency_key``):
    #: the server then replays the first answer, or reports the first request
    #: still running, instead of running it twice. A plain new call sends a
    #: new key, and can run (and charge) the work a second time. Since 3.0.
    idempotency_key: str | None = None

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
        self.retryable = self._derived_retryable()
        # Per-subclass enrichment (retry_after, task_id, etc.). Set on the
        # instance so they're accessible as ``exc.task_id`` regardless of
        # which subclass raised.
        for k, v in extra.items():
            setattr(self, k, v)

    def _derived_retryable(self) -> bool | None:
        """``retryable`` when nothing more specific says (see the class
        docstring). A class with its own rule overrides this."""
        status = self.status_code
        if status == 429 or 500 <= status < 600:
            return True
        if status == 409 and _resendable_409(self):
            return True
        if 400 <= status < 500:
            return False
        return None

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


#: 409 codes that mean "not yet": the same request, sent again later (with the
#: same ``Idempotency-Key``), can succeed.
_RESENDABLE_409_CODES = ("idempotency_conflict", "verification_not_ready")


def _resendable_409(err: LenzError) -> bool:
    """Whether a 409 says "not yet". Reads the body as sent too: the 2.x
    attributes leave ``code`` empty on endpoints whose original body had none."""
    sent = err.body.get("code") if isinstance(err.body, dict) else None
    return err.code in _RESENDABLE_409_CODES or sent in _RESENDABLE_409_CODES


class LenzAuthError(LenzError):
    """401 / 403 — the credential is missing, invalid, expired, or revoked.

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
    @deprecated("Use `remaining`.", category=None)
    def credits_remaining(self) -> int:
        """Deprecated: use `remaining`. To be removed in a future major release.

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
            "credits_remaining is deprecated and will be removed in a future major release; "
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
    ``[{"loc": [...], "msg": "...", "type": "..."}]``, read from the body's
    ``detail`` list (the original response shape) or its ``errors`` list
    (the newer one, where ``detail`` is a sentence and becomes the message).
    """

    errors: list[dict[str, Any]] = []  # noqa: RUF012 — overridden per-instance


class LenzRateLimitError(LenzError):
    """429 — rate limited.

      * ``retry_after``       — seconds until the next allowed call, resolved
        from the ``Retry-After`` header or the body's ``reset_in_seconds`` /
        ``retry_after``.
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
    """500 / 502 / 503 / 504 / catch-all for unexpected server errors.

    ``retry_after`` is the wait the response stated (``Retry-After``), or
    ``None`` when it stated none.

    Network failures are :class:`LenzConnectionError`, a subclass.
    """

    retry_after: int | None = None

    def _derived_retryable(self) -> bool | None:
        # Any status this class is raised for is a 5xx; without one (built by
        # hand, or a request that failed with no diagnostic) it is unknown.
        return None if self.status_code == 0 else True


class LenzConnectionError(LenzAPIError):
    """The request never got an answer: the connection failed or broke
    (DNS, refused, reset, TLS, a server or proxy that hung up), after the
    SDK's own retries.

    A subclass of :class:`LenzAPIError`, which is what 2.x raised, so an
    existing ``except LenzAPIError`` keeps catching it. ``__cause__`` is the
    underlying ``httpx`` exception and ``status_code`` is 0. ``retryable`` is
    ``True``: the same request can succeed once the network is back (calls
    that charge send an ``Idempotency-Key``). Resend with the same key: pass
    ``idempotency_key=exc.idempotency_key`` back, and the server replays the
    first answer if the first request did arrive. A plain new call sends a new
    key, and can run (and charge) the work twice.
    """

    def _derived_retryable(self) -> bool | None:
        return True


class LenzRequestTimeoutError(LenzConnectionError):
    """One HTTP request got no answer within its timeout (``Lenz(timeout=)``
    or the call's ``timeout=``), after the SDK's own retries.

    Not :class:`LenzTimeoutError`, which is a ``*_and_wait`` helper reaching
    its own deadline while the job keeps running. A subclass of
    :class:`LenzConnectionError` and so of :class:`LenzAPIError`, which is
    what 2.x raised.

    The request may have reached the server and be running. Resend only with
    the same key (``idempotency_key=exc.idempotency_key``): the server then
    replays the first answer (or answers 409 ``idempotency_conflict`` while it
    still runs) instead of running it again. A plain new call sends a new key,
    and can run (and charge) the work twice.
    """


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
    The job keeps running server-side: read it later by its id rather than
    resubmit (``retryable`` is ``None``: no request failed). A single HTTP request that timed out is
    :class:`LenzRequestTimeoutError` instead.
    """

    task_id: str = ""


class LenzNeedsInputError(LenzError):
    """``verify_and_wait`` paused because the pipeline needs caller input.

    Carries ``task_id``, ``kind`` ("multi_claim"),
    ``hint`` (one sentence on what was unclear and how to
    resolve it via ``client.select`` — ``""`` from older servers) and
    ``payload`` (the full status response).
    """

    task_id: str = ""
    kind: str = ""
    hint: str = ""
    payload: dict[str, Any] = {}  # noqa: RUF012 — overridden per-instance


class LenzPipelineError(LenzError):
    """A verification run ended in a terminal ``failed`` state.

    Raised by ``verify_and_wait`` / ``wait``, and by ``verifications.get``
    when it is handed the ``task_id`` of a run that failed (a 409 with
    ``code`` ``verification_failed``).

    ``failure_class`` says WHY (closed set: ``upstream_unavailable`` |
    ``insufficient_evidence`` | ``invalid_input`` | ``cancelled`` |
    ``internal``); ``retryable`` is the derived signal — ``True`` means a
    transient provider-side exhaustion where resubmitting the same claim is
    the right move. ``None`` when an older server didn't say. ``hint`` is the
    server's one sentence on what to send instead (e.g. for ``not_a_claim``);
    ``""`` when it sent none.
    """

    task_id: str = ""
    failure_reason: str = ""
    failure_class: str = ""
    retryable: bool | None = None
    hint: str = ""

    def _derived_retryable(self) -> bool | None:
        # The server's value or nothing: never guessed from a status.
        return None


class LenzVerificationNotReadyError(LenzError):
    """409 — ``verifications.get`` was handed the ``task_id`` of a run that is
    still going, so there is no verification to return yet.

    ``status`` is ``"processing"`` or ``"needs_input"``, ``task_id`` echoes the
    id and ``hint`` is the server's one sentence on what to do next (also on
    ``fix``). Wait for the run with ``client.wait(task_id)``, or poll
    ``client.get_status(task_id)``, which also carries the options a
    ``needs_input`` run offers.

    A run that FAILED raises :class:`LenzPipelineError` instead: it will never
    be ready.
    """

    task_id: str = ""
    status: str = ""
    hint: str = ""


class LenzNotFoundError(LenzError):
    """404 — nothing was found under the id (or path) the request names, for
    the API key it was sent with.

    Check the id and the key: resending the same request will not find it
    (``retryable`` is ``False``). A subclass of :class:`LenzError`, which is
    what 2.x raised for a 404. An id whose verification its account's
    retention period removed answers 410 instead (:class:`LenzGoneError`).
    """


class LenzGoneError(LenzError):
    """410 — the verification existed, and its account's retention period has
    since removed it.

    ``code`` is ``"purged"`` and ``purged_at`` is the ISO-8601 time it was
    removed, or ``None`` when the server didn't say. Retrying cannot bring it
    back. A certificate issued for it stays available from
    ``verifications.get_certificate``.

    Raised by ``verifications.get``, ``get_status`` on a completed task,
    ``verifications.related``, ``ask.send`` and ``ask.history``. ``wait`` raises it at once instead
    of polling to the deadline. A 404 is :class:`LenzNotFoundError`: only an
    id you could read before answers 410.
    """

    purged_at: str | None = None


class ReviewTimeout(LenzTimeoutError):
    """``review_and_wait`` reached its ``timeout`` before the review ended.

    The review keeps running server-side. ``review_id`` resumes it
    (``client.get_review(review_id)``), and ``partial`` is the last body read,
    or ``None`` when no read succeeded: its ``claims`` already carry the quick
    verdicts that are in.
    """

    review_id: str = ""
    partial: ReviewFull | None = None


class ReviewFailed(LenzPipelineError):
    """A review ended ``failed``.

    ``error_code`` is ``review.failure.failure_reason`` (an open set:
    ``no_claim``, ``insufficient_credits``, ``upstream_unavailable``, …),
    ``hint`` the server's one sentence on what to do next, ``retryable``
    whether resending the same draft can help, and ``review`` the final body.
    A subclass of :class:`LenzPipelineError`, so an existing handler for
    failed verifications catches it too.
    """

    review_id: str = ""
    error_code: str = ""
    review: ReviewFull | None = None


class CitecheckTimeout(LenzTimeoutError):
    """``citecheck_and_wait`` reached its ``timeout`` before the check ended.

    The check keeps running server-side. ``citecheck_id`` resumes it
    (``client.get_citecheck(citecheck_id)``), and ``partial`` is the last body
    read, or ``None`` when no read succeeded.
    """

    citecheck_id: str = ""
    partial: Citecheck | None = None


class CitecheckFailed(LenzPipelineError):
    """A citation check ended ``failed``.

    ``error_code`` is ``citecheck.failure.failure_reason`` (an open set),
    ``hint`` the server's one sentence on what to do next, ``retryable``
    whether resending the same request can help, and ``citecheck`` the final
    body. A subclass of :class:`LenzPipelineError`.
    """

    citecheck_id: str = ""
    error_code: str = ""
    citecheck: Citecheck | None = None


class LenzWebhookSignatureError(LenzError):
    """``LenzWebhooks.parse`` rejected a payload.

    Possible reasons: tampered body (HMAC mismatch), missing
    ``X-Lenz-Signature`` header, replay window exceeded, malformed body.
    """


class LenzApiVersionError(LenzError):
    """A response named an API version this SDK does not read.

    Every API response names the version that served it in the
    ``X-Lenz-API-Version`` header. lenz-io 3.x asks for ``2026-10-11`` and reads
    that version's response shape only, so an answer in another version (in
    practice ``2026-05-13``, which a reply replayed from before the account's
    version changed, or a server pinned to the older version, still sends) is
    refused instead of being misread. Applies to every response of a client
    call, success or error; never to webhook payloads.

    Fields:
      * ``api_version`` — the version the response named.
      * ``status_code`` — the response's HTTP status.
      * ``body``        — the response body as sent (parsed JSON), or ``None``
        when it was not a JSON object.
    """

    def __init__(self, *, api_version: str = "", **kwargs: Any) -> None:
        super().__init__(api_version=api_version, **kwargs)
        self.api_version = api_version

    def _derived_retryable(self) -> bool | None:
        # The same request answers in the same version again.
        return False


#: The job errors under the names the other error classes follow (``...Error``),
#: the names the Node SDK uses. The same classes: catch either name.
ReviewFailedError = ReviewFailed
ReviewTimeoutError = ReviewTimeout
CitecheckFailedError = CitecheckFailed
CitecheckTimeoutError = CitecheckTimeout


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

# 429 ``code`` values the retry ladder never sleeps through, whatever wait they
# state. ``review_in_flight`` means the account already has its maximum of
# reviews running, and a review runs for minutes: sleeping the stated wait
# inside a submit would block the caller, most likely into the same answer.
NO_RETRY_429_CODES = ("review_in_flight",)

# The 429s that refuse a submit while the account's earlier ones run. They
# stated their wait as ``retry_after_seconds``, never ``reset_in_seconds``.
_IN_FLIGHT_429_CODES = ("review_in_flight", "citecheck_in_flight")

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


# The one 410 the API answers: a verification its account's retention period
# has removed. Keyed on ``code``, like the 409 and 503 tables: any other 410
# (a proxy, a future contract) stays a plain LenzError.
_GONE_410_CODES: dict[str, tuple[type[LenzError], str, str]] = {
    "purged": (
        LenzGoneError,
        "Verification removed",
        f"{_DOCS_BASE}/errors",
    ),
}


# The two 409s ``GET /verifications/{id}`` answers when handed the task_id of a
# run with no result yet. Keyed on ``code``, not the status: every other 409
# (an Idempotency-Key still in flight, a select with nothing pending) stays a
# plain LenzError, exactly as before.
_VERIFICATION_409_CODES: dict[str, tuple[type[LenzError], str, str]] = {
    "verification_not_ready": (
        LenzVerificationNotReadyError,
        "Verification not ready",
        f"{_DOCS_BASE}/verify",
    ),
    "verification_failed": (
        LenzPipelineError,
        "Verification failed",
        f"{_DOCS_BASE}/errors",
    ),
}


def _opt_str(value: Any) -> str:
    """String-typed only: anything else reads as absent, never as its repr."""
    return value if isinstance(value, str) else ""


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


# ── The original error bodies, read from a newer-shape body ─────────────
#
# The API's current response shape (what 3.0 asks for) gives every error one
# envelope: ``detail`` is a sentence, ``code`` is always set, a 422 lists its
# fields in ``errors``, and waits and links have one name each. The 2.x shape
# differed by endpoint. ``_original_error`` rebuilds the 2.x body by endpoint
# (the same rules as the Node SDK), so every attribute of the exception keeps
# its 2.x value; ``exc.body`` is the body as sent.

#: /review and /citecheck (submit and read) kept their own error envelope.
_REVIEW_FAMILY = re.compile(r"^/(?:review|reviews/[^/]+|citecheck|citechecks/[^/]+)$")

#: The cancel calls are new in 3.0: no earlier shape to read, so the server's
#: ``code`` stays on the error (``not_found``, ``use_review_cancel``, ...).
_CANCEL_PATH = re.compile(r"^/(?:verify|reviews|citechecks)/[^/]+/cancel$")
_SELECT_PATH = re.compile(r"^/verify/[^/]+/select$")

#: Codes the newer shape sends where the original error carried no ``code``
#: (outside /review and /citecheck, which always sent one).
_CODELESS = frozenset(
    {
        "not_authenticated",
        "not_found",
        "idempotency_body_mismatch",
        "idempotency_conflict",
        "malformed_body",
        "method_not_allowed",
        "validation_error",
        "blank_input",
        "unsupported_language",
        "too_many_items",
        # The fallback codes: any 4xx without its own, and a 500.
        "invalid_request",
        "internal_error",
    }
)


def _rename(o: dict[str, Any], old: str, new: str) -> None:
    """``old`` renamed to ``new`` when only ``old`` is there (in place)."""
    if old in o and new not in o:
        o[new] = o.pop(old)


def _original_wait_and_link(o: dict[str, Any], status: int) -> None:
    """A wait and a docs link under their original names."""
    code = o.get("code")
    if status == 429 and code == "extract_daily_limit":
        _rename(o, "retry_after", "reset_in_seconds")
    if status == 429 and code in ("review_in_flight", "citecheck_in_flight"):
        _rename(o, "retry_after", "retry_after_seconds")
    if status in (402, 429, 503):
        _rename(o, "docs_url", "doc_url")


def _original_error(status: int, parsed: dict[str, Any], method: str, path: str) -> dict[str, Any]:
    """``parsed`` as the original response shape sent it, as far as the newer
    body and the request's endpoint tell."""
    out = dict(parsed)
    code = out["code"] if isinstance(out.get("code"), str) else ""
    errors = out["errors"] if isinstance(out.get("errors"), list) else None
    path = path.split("?", 1)[0]
    if method.upper() == "POST" and _CANCEL_PATH.match(path):
        # The three cancel calls are new in 3.0: there is no 2.x reading of
        # their errors, so the body is read as sent (code, detail and errors),
        # as the Node SDK reads it.
        return out
    if _REVIEW_FAMILY.match(path):
        # A missing or unknown credential is refused before the endpoint runs.
        if code == "not_authenticated":
            del out["code"]
        if status == 422:
            detail = out.get("detail")
            if method.upper() == "POST" and path == "/review" and isinstance(detail, str):
                if code in ("blank_input", "unsupported_language"):
                    out["code"] = "validation_error"
                    if code == "unsupported_language" and not detail.startswith("language: "):
                        out["detail"] = f"language: {detail}"
            if errors is not None:
                first: dict[str, Any] = next((e for e in errors if isinstance(e, dict)), {})
                loc, msg = first.get("loc"), first.get("msg")
                if isinstance(loc, list) and len(loc) > 1 and loc[1] == "payload" and isinstance(msg, str):
                    # The original named the body parameter: ``payload.text: ...``.
                    out["detail"] = ".".join(str(p) for p in loc[1:]) + f": {msg}"
                items: list[Any] = []
                for item in errors:
                    if not isinstance(item, dict):
                        items.append(item)
                        continue
                    o: dict[str, Any] = {}
                    if "loc" in item:
                        o["loc"] = item["loc"]
                    if "msg" in item:
                        renamed = item["msg"] == detail and out.get("detail") != detail
                        o["msg"] = out["detail"] if renamed else item["msg"]
                    items.append(o)
                out["errors"] = items
            elif code == "idempotency_body_mismatch":
                out["errors"] = [{"loc": ["header"], "msg": out.get("detail")}]
        if (
            status == 402
            and method.upper() == "POST"
            and path == "/citecheck"
            and "credits_remaining" not in out
            and isinstance(out.get("remaining"), int)
        ):
            # One credit per citation: the pool equals ``remaining``.
            out["credits_remaining"] = out["remaining"]
        _original_wait_and_link(out, status)
        return out
    if status == 422 and code == "blank_input" and isinstance(out.get("detail"), str) and method.upper() == "POST":
        # A blank input said "Text is required." (or named its item, or
        # ``texts``), where the newer sentence names ``claim`` / ``claims``.
        loc = errors[0].get("loc") if errors and isinstance(errors[0], dict) else None
        if isinstance(loc, list) and loc and loc[0] == "body":
            if path in ("/verify", "/assess"):
                if len(loc) == 2 and loc[1] == "claim":
                    out["detail"] = "Text is required."
            elif path == "/verify/batch":
                if len(loc) == 4 and loc[1] == "claims" and isinstance(loc[2], int) and not isinstance(loc[2], bool):
                    out["detail"] = f"claims[{loc[2]}].text is required."
            elif _SELECT_PATH.match(path):
                if len(loc) == 2 and loc[1] == "claims":
                    out["detail"] = "texts is required and must be non-empty."
    if status == 422 and code == "blank_input" and path == "/assess":
        loc = errors[0].get("loc") if errors and isinstance(errors[0], dict) else None
        if isinstance(loc, list) and "claims" in loc:
            # A blank item in ``claims``: the original said ``blank_item``.
            out["code"] = "blank_item"
            out.pop("errors", None)
            return out
    if status == 422 and code == "unsupported_language" and path == "/verify/batch" and errors:
        loc = errors[0].get("loc") if isinstance(errors[0], dict) else None
        detail = out.get("detail")
        if isinstance(loc, list) and len(loc) > 2 and loc[1] == "claims" and isinstance(loc[2], int):
            if isinstance(detail, str) and not detail.startswith("claims["):
                # The original named the item: ``claims[1].Unsupported language ...``.
                out["detail"] = f"claims[{loc[2]}].{detail}"
    if (
        status == 422
        and code == "validation_error"
        and errors
        and all(isinstance(e, dict) and isinstance(e.get("type"), str) and e["type"] != code for e in errors)
    ):
        # Request-schema validation: the original ``detail`` was the list of
        # field errors itself, each ``{type, loc, msg, ...}``, with no ``code``.
        listed = []
        for item in errors:
            ordered = {k: item[k] for k in ("type", "loc", "msg") if k in item}
            ordered.update({k: v for k, v in item.items() if k not in ordered})
            listed.append(ordered)
        original: dict[str, Any] = {"detail": listed}
        for key, value in out.items():
            if key not in ("detail", "code", "errors"):
                original["doc_url" if key == "docs_url" else key] = value
        return original
    # /assess sent ``too_many_items``; /ask sent no code for an unfinished
    # verification (``GET /verifications/{id}`` still sends ``verification_not_ready``).
    codeless = (code in _CODELESS and not (code == "too_many_items" and path == "/assess")) or (
        code == "verification_not_ready" and path.startswith("/ask/")
    )
    if codeless:
        del out["code"]
    out.pop("errors", None)
    _original_wait_and_link(out, status)
    return out


def map_response_to_error(
    status_code: int,
    body: bytes | str | None,
    headers: dict[str, str] | None = None,
    *,
    endpoint: tuple[str, str] | None = None,
) -> LenzError:
    """Translate an HTTP error response into the right typed exception.

    Returns an *instance* (not raised) so callers can decide whether
    to raise, log, or surface. Keep this pure — no I/O.

    ``endpoint`` is the request's ``(method, path)``: the client passes it, and
    the exception gets the attributes 2.x gave (``code``, ``message``,
    ``errors``, waits), which differed by endpoint; ``exc.body`` is the body
    as sent. Without it the body is read as it stands.
    """
    raw = _parse_body(body)
    parsed = raw
    if endpoint is not None:
        parsed = _original_error(status_code, raw, *endpoint)
    headers = headers or {}
    request_id = headers.get("X-Request-ID") or headers.get("x-request-id") or ""

    # String-typed only, matching the Node SDK: a malformed `code: 42` becomes
    # "" rather than the string "42", so nothing downstream branches on a
    # value the server never meant as a code.
    code_raw = parsed.get("code")
    code = code_raw if isinstance(code_raw, str) else ""

    if status_code in _STATUS_MAP:
        cls, default_msg, doc_url = _STATUS_MAP[status_code]
    elif status_code == 404:
        cls, default_msg, doc_url = LenzNotFoundError, f"HTTP {status_code}", f"{_DOCS_BASE}/errors"
    elif status_code == 410 and code in _GONE_410_CODES:
        cls, default_msg, doc_url = _GONE_410_CODES[code]
    elif status_code == 409 and code in _VERIFICATION_409_CODES:
        cls, default_msg, doc_url = _VERIFICATION_409_CODES[code]
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
        body=raw,
    )

    # Class-specific enrichment from the response body. Each is set on the
    # instance so callers can access via the documented attribute name.
    if status_code == 409 and isinstance(err, (LenzVerificationNotReadyError, LenzPipelineError)):
        # The generic 4xx advice ("retry; file an issue") is wrong for both:
        # the server's own hint says what to do, with a class default behind it.
        # A failed run states its cause in one ``failure`` block.
        raw_failure = parsed.get("failure")
        failure: dict[str, Any] = raw_failure if isinstance(raw_failure, dict) else {}

        def _read(key: str, block_key: str) -> Any:
            return parsed[key] if key in parsed else failure.get(block_key)

        err.task_id = _opt_str(parsed.get("task_id"))
        err.hint = _opt_str(_read("hint", "hint"))
        if isinstance(err, LenzVerificationNotReadyError):
            err.status = _opt_str(parsed.get("status"))
            err.fix = err.hint or "Wait for the run with client.wait(task_id), then read its result."
        else:
            reason = _opt_str(_read("failure_reason", "code"))
            # The original spelling of "nothing checkable" on a verification.
            err.failure_reason = "not_a_claim" if reason == "no_checkable_claim" else reason
            err.failure_class = _opt_str(_read("failure_class", "failure_class"))
            # Only a real boolean is a retry signal, as in the wait path.
            retryable = _read("retryable", "retryable")
            err.retryable = retryable if isinstance(retryable, bool) else None
            err.doc_url = _opt_str(_read("docs_url", "docs_url")) or err.doc_url
            if err.hint:
                err.fix = err.hint
            elif err.retryable:
                # Same words as the wait path's LenzPipelineError (client.py).
                err.fix = "Transient provider outage — retry the same request after a short wait."
            else:
                err.fix = "This run will not produce a result. Resubmit with a different claim."

    if status_code == 409 and code == "use_review_cancel":
        # Not "not yet": the task belongs to a review, and the review is what
        # to cancel. Sending the same request again can never succeed.
        err.fix = "Cancel the review that started this task instead: client.cancel_review(review_id)."
        err.retryable = False

    if isinstance(err, LenzGoneError):
        err.purged_at = _opt_str(parsed.get("purged_at")) or None
        # Retrying cannot bring it back, so not the generic 4xx advice.
        err.fix = "Its account's retention period removed it. A certificate issued for it is still available."

    if isinstance(err, LenzAPIError) and not isinstance(err, LenzUpstreamUnavailableError):
        err.retry_after = _opt_wait(headers.get("Retry-After") or headers.get("retry-after"))

    if isinstance(err, LenzUpstreamUnavailableError):
        # Body ``retry_after`` first (both 503 shapes carry it), header as
        # the fallback for any proxy that strips the body.
        stated = _opt_wait(parsed.get("retry_after"))
        if stated is None:
            # The /review error body states its wait under this name.
            stated = _opt_wait(parsed.get("retry_after_seconds"))
        if stated is None:
            stated = _opt_wait(headers.get("Retry-After") or headers.get("retry-after"))
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
        if "remaining" not in parsed and "requested" not in parsed:
            # A body without the capability figure: the pool divided by this
            # one call's price.
            pool, price = _opt_int(parsed.get("credits_remaining")), _opt_int(parsed.get("cost"))
            if pool is not None and price:
                err.remaining = pool // price
        err.requested = _opt_int(parsed.get("requested"))
        # The pool behind the capability figure above, and this call's price in
        # credits. The body's ``credits_remaining`` lands on ``credit_balance``,
        # NOT on the same-named deprecated property — that one aliases
        # ``remaining`` and means a different quantity (see the class docstring).
        # (A citation check's current-shape 402 leaves the pool out because it
        # equals ``remaining``; ``_original_error`` restores it for that
        # endpoint only.)
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
        if (
            "reset_in_seconds" not in parsed
            and "retry_after_seconds" not in parsed
            and "retry_after" in parsed
            and code not in _IN_FLIGHT_429_CODES
        ):
            # The current shape names every wait ``retry_after``. The
            # in-flight refusals never carried ``reset_in_seconds``.
            err.reset_in_seconds = _opt_int(parsed.get("retry_after"))
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
            # ``review_in_flight`` states its wait here.
            parsed.get("retry_after_seconds"),
            parsed.get("retry_after"),
        ):
            resolved = _opt_wait(candidate)
            if resolved is not None:
                err.retry_after = resolved
                break
        else:
            err.retry_after = 0

    # A boolean ``retryable`` in the body's failure block, else at its top
    # level, is the server's own word and wins over the status. (A failed
    # verification's 409 read it above.)
    if not isinstance(err, LenzPipelineError):
        candidates = [
            block.get("retryable") for block in (raw.get("failure"), parsed.get("failure")) if isinstance(block, dict)
        ]
        candidates += [raw.get("retryable"), parsed.get("retryable")]
        stated = next((c for c in candidates if isinstance(c, bool)), None)
        if stated is not None:
            err.retryable = stated

    return err


#: The longest wait ``retry_after`` reports, in seconds (the client's
#: ``MAX_TIMEOUT_SECONDS``): a larger stated wait reads as this.
_MAX_STATED_WAIT = 2_147_483


def _opt_wait(value: Any) -> int | None:
    """A stated wait in seconds, as ``_opt_int`` reads it (a non-finite or
    unparseable value is ``None``), at most ``_MAX_STATED_WAIT``."""
    seconds = _opt_int(value)
    return None if seconds is None else min(seconds, _MAX_STATED_WAIT)


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
    except (TypeError, ValueError, OverflowError):
        # OverflowError: an infinite value (``"1e999"``) is unknown, as in Node.
        return None


def _fix_hint_for(status_code: int) -> str:
    return {
        401: "Your credential is missing, invalid or expired. Check the key you passed, or get a new one at https://lenz.io/api-credentials.",
        403: "This key doesn't have access to that resource.",
        402: "Top up or upgrade at https://lenz.io/plans, or wait for the period reset.",
        404: (
            "Check the id or key the call names: nothing with it is visible to this credential. Retrying will not help."
        ),
        422: "Check the request body against the OpenAPI spec.",
        429: "Wait Retry-After seconds and retry.",
    }.get(status_code, "Retry; if the error persists, file an issue with the Request ID.")


__all__ = [
    "MAX_RETRY_AFTER_SLEEP",
    "NO_RETRY_429_CODES",
    "UPSTREAM_503_CODES",
    "CitecheckFailed",
    "CitecheckFailedError",
    "CitecheckTimeout",
    "CitecheckTimeoutError",
    "LenzAPIError",
    "LenzApiVersionError",
    "LenzAuthError",
    "LenzConnectionError",
    "LenzError",
    "LenzGoneError",
    "LenzNeedsInputError",
    "LenzNotFoundError",
    "LenzPipelineError",
    "LenzQuotaExceededError",
    "LenzRateLimitError",
    "LenzRequestTimeoutError",
    "LenzTimeoutError",
    "LenzUpstreamUnavailableError",
    "LenzValidationError",
    "LenzVerificationNotReadyError",
    "LenzWebhookSignatureError",
    "ReviewFailed",
    "ReviewFailedError",
    "ReviewTimeout",
    "ReviewTimeoutError",
    "map_response_to_error",
]
